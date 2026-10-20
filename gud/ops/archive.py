"""Repo archives for backup and host migration (PLAN Phase 6, SPEC §11, §11.6, §17).

An archive is a directory, or a `.tar` file holding the same members:

    manifest.json   format, repo, log head, the hash of every other member, and per logged
                    message {seq, id, sha256, size, time}; optionally signed by the exporter
    log.txt         the signed host log: every entry's exact text (§11.1), in seq order
    mail.mbox       the raw MIME of every logged message exactly as the host stored it, in log
                    order, mboxrd-escaped (lines matching `>*From ` get one more `>`)
    snapshot.json   the §17 snapshot of replaying the archive itself at its log head (when the
                    archive replays), so a verifier can confirm it reproduces the same state

Exports are deterministic: the same log and mail give the same bytes. `verify_archive` checks
everything offline: the hashes in the manifest, the log (format, seq, prev, strictly increasing
time, every signature under the key current at that entry, handovers), that every `mail` entry's
message is present with the recorded hash and the logged gud id, log order of the mbox, the
manifest signature, and that replay reproduces the archived snapshot.

Planned host migration (§11.6): the old host appends `handover` (`plan_handover`), exports, and
the new host runs `import_archive` with its new key, which verifies the archive, continues the
same log with an `open` citing the handover, re-delivers the raw mail byte-exactly into the new
mailbox (when the mailbox can inject raw bytes) and/or writes it to a mail store. The members
push for the new host key is `gud.ops.keys.host_key_push`.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence

from gud.core import envelope as E
from gud.core.hostlog import HostLog, LogError, decode_entry, encode_entry
from gud.core.objects import MemoryStore
from gud.core.replay import Replayer, ReplayError
from gud.core.types import Email, Hex, LogEntry, Ms
from gud.transport import mime

FORMAT = "gud-archive/1"
MANIFEST, LOG_FILE, MBOX_FILE, SNAPSHOT_FILE = "manifest.json", "log.txt", "mail.mbox", "snapshot.json"
MEMBERS = (MANIFEST, LOG_FILE, MBOX_FILE, SNAPSHOT_FILE)
_PAGE = 1000


class ArchiveError(ValueError):
    pass


class ArchiveSource(Protocol):
    """What an export reads: the host's log and raw mail (`gud.events.intake.Intake` provides
    both). A member client with a local `replayer()` works too (see `export_archive`)."""

    def log_entries(self, since: int, limit: int) -> list[LogEntry]: ...

    def raw(self, gud_id: Hex) -> bytes | None: ...


# ---------------------------------------------------------------- log text


def encode_log(entries: Iterable[LogEntry]) -> bytes:
    return b"".join(encode_entry(e) for e in entries)


def decode_log(data: bytes) -> list[LogEntry]:
    """Entries of a log text (concatenated §11.1 entries). Raises LogError."""
    chunks: list[list[bytes]] = []
    for line in _split_lines(data):
        if line == b"gud-log 1\n":
            chunks.append([])
        if not chunks:
            raise LogError("log text does not start with an entry")
        chunks[-1].append(line)
    return [decode_entry(b"".join(chunk)) for chunk in chunks]


def _split_lines(data: bytes) -> list[bytes]:
    """Split after each LF only (CR and other bytes are ordinary). The last piece may lack LF."""
    out, start = [], 0
    while start < len(data):
        end = data.find(b"\n", start)
        end = len(data) if end < 0 else end + 1
        out.append(data[start:end])
        start = end
    return out


# ---------------------------------------------------------------- mbox (mboxrd)


_FROM_ESC = re.compile(rb">*From ")
_FROM_UNESC = re.compile(rb">+From ")


def _from_line(time_ms: Ms) -> bytes:
    stamp = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(time_ms // 1000))
    return f"From gud-archive@gud.invalid {stamp}\n".encode("ascii")


def mbox_encode(messages: Iterable[tuple[bytes, Ms]]) -> bytes:
    """mboxrd: From_ line, escaped message, LF if the message lacked a final LF, blank line."""
    out = []
    for raw, time_ms in messages:
        out.append(_from_line(time_ms))
        for line in _split_lines(raw):
            out.append(b">" + line if _FROM_ESC.match(line) else line)
        if not raw.endswith(b"\n"):
            out.append(b"\n")
        out.append(b"\n")
    return b"".join(out)


def mbox_blocks(data: bytes) -> list[bytes]:
    """The unescaped messages of an mboxrd file. A message that had no final LF comes back with
    one; the manifest's size and hash say which (see `_resolve`). Raises ArchiveError."""
    blocks: list[list[bytes]] = []
    for line in _split_lines(data):
        if line.startswith(b"From "):
            blocks.append([])
            continue
        if not blocks:
            raise ArchiveError("mbox does not start with a From_ line")
        blocks[-1].append(line[1:] if _FROM_UNESC.match(line) else line)
    out = []
    for lines in blocks:
        body = b"".join(lines)
        if not body.endswith(b"\n"):
            raise ArchiveError("mbox message is not followed by a blank line")
        out.append(body[:-1])
    return out


# ---------------------------------------------------------------- container


def _read_members(path: Path) -> dict[str, bytes]:
    path = Path(path)
    out: dict[str, bytes] = {}
    if path.is_dir():
        for name in MEMBERS:
            if (path / name).is_file():
                out[name] = (path / name).read_bytes()
        return out
    if path.is_file():
        try:
            with tarfile.open(path, "r") as tar:
                for info in tar.getmembers():
                    if info.isfile() and info.name in MEMBERS:
                        fh = tar.extractfile(info)
                        if fh is not None:
                            out[info.name] = fh.read()
        except tarfile.TarError as exc:
            raise ArchiveError(f"{path} is not an archive: {exc}") from exc
        return out
    raise ArchiveError(f"no archive at {path}")


def _write_members(out: Path, files: Mapping[str, bytes]) -> None:
    out = Path(out)
    if out.suffix == ".tar":
        if out.exists():
            raise ArchiveError(f"{out} already exists")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.tmp")
        with tarfile.open(tmp, "w", format=tarfile.USTAR_FORMAT) as tar:
            for name in sorted(files):
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(files[name]), 0, 0o644
                tar.addfile(info, io.BytesIO(files[name]))
        tmp.replace(out)
        return
    if out.exists() and any(out.iterdir()):
        raise ArchiveError(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    for name in sorted(files):
        (out / name).write_bytes(files[name])


# ---------------------------------------------------------------- the replay dependency


def _replay_state(emails: Sequence[Email], entries: Sequence[LogEntry],
                  trusted_keys: Sequence[str]) -> tuple[bytes, Hex | None] | None:
    """The archive's only use of the replay engine: replay it at its log head and return
    (§17 snapshot bytes, tip state). None when the log does not replay."""
    if not entries:
        return None
    replayer = Replayer(MemoryStore(), trusted_host_keys=trusted_keys)
    try:
        replayer.feed(emails, entries, entries[-1].time)
    except ReplayError:
        return None
    tip = replayer.tip
    return replayer.snapshot().to_bytes(), (tip.state if tip is not None else None)


# ---------------------------------------------------------------- export


def read_log(source) -> list[LogEntry]:
    """Every log entry `source` serves (paged `log_entries`), or a client's local verified log."""
    if hasattr(source, "log_entries"):
        out: list[LogEntry] = []
        while True:
            page = list(source.log_entries(len(out), _PAGE))
            out.extend(page)
            if len(page) < _PAGE:
                return out
    replayer = getattr(source, "replayer", None)
    if replayer is not None:
        replayer = replayer() if callable(replayer) else replayer
        return list(replayer.log().entries())
    raise ArchiveError("source has neither log_entries() nor replayer()")


def read_mail(source, gud_id: Hex) -> bytes | None:
    """Raw MIME of one logged message from `source`, or None."""
    if hasattr(source, "raw"):
        return source.raw(gud_id)
    email = source.reader().email(gud_id)
    return email.raw_mime if email is not None else None


def _gud_id_of(raw: bytes) -> Hex | None:
    email = mime.to_email(raw)
    return email.gud_id if email is not None else None


def _unsigned_manifest(manifest: Mapping) -> bytes:
    return E.jcs({k: v for k, v in manifest.items() if k != "sig"})


def export_archive(source: "ArchiveSource", out: Path, *, signer: bytes | None = None,
                   trusted_keys: Sequence[str] = ()) -> Path:
    """Write the archive of `source` (an Intake-like host store, or a member client with a local
    replayer) to `out` (a new directory, or a `.tar` path). `signer` (normally the host key)
    signs the manifest. A logged message the source cannot produce, or whose bytes do not give
    the logged gud id, is listed under `missing`. Returns `out`."""
    entries = read_log(source)
    log = HostLog(trusted_keys=trusted_keys)
    try:
        log.extend(entries)
    except LogError as exc:
        raise ArchiveError(f"the source log does not verify: {exc}") from exc
    if log.head is None:
        raise ArchiveError("the source log is empty")
    mail_rows, blocks, missing, emails = [], [], [], []
    done: set[Hex] = set()
    for seq, gud_id, when in log.mail_ids():
        if gud_id in done:
            continue
        done.add(gud_id)
        raw = read_mail(source, gud_id)
        if raw is None or _gud_id_of(raw) != gud_id:
            missing.append(gud_id)
            continue
        email = mime.to_email(raw)
        assert email is not None
        emails.append(email)
        blocks.append((raw, when))
        mail_rows.append({"seq": seq, "id": gud_id, "sha256": E.sha256_hex(raw), "size": len(raw), "time": when})
    log_bytes = encode_log(log.entries())
    mbox = mbox_encode(blocks)
    files = {LOG_FILE: log_bytes, MBOX_FILE: mbox}
    manifest: dict = {
        "format": FORMAT,
        "repo": log.repo,
        "log": {"sha256": E.sha256_hex(log_bytes), "entries": len(log), "head": log.head.seq,
                "head_hash": log.head.hash, "key": log.key},
        "mbox": {"sha256": E.sha256_hex(mbox), "messages": len(mail_rows)},
        "mail": mail_rows,
        "missing": missing,
        "snapshot": None,
    }
    state = _replay_state(emails, log.entries(), trusted_keys)
    if state is not None:
        snap, tip = state
        files[SNAPSHOT_FILE] = snap
        manifest["snapshot"] = {"sha256": E.sha256_hex(snap), "tip": tip}
    if signer is not None:
        manifest["signer"] = E.public_of(signer)
        manifest["sig"] = E.sign_bytes(signer, _unsigned_manifest(manifest))
    files[MANIFEST] = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8") + b"\n"
    _write_members(out, files)
    return Path(out)


# ---------------------------------------------------------------- verify


@dataclass(frozen=True)
class ArchiveReport:
    problems: tuple[str, ...]                # human-readable, one per finding; empty = ok
    log_ok: bool
    log_error: str | None
    entries: int                             # verified log entries
    messages: int                            # messages found with their recorded bytes
    missing: tuple[Hex, ...]                 # logged ids with no message in the archive
    altered: tuple[Hex, ...]                 # logged ids whose archived bytes do not match
    out_of_order: bool                       # mbox not in log order
    signer: str | None                       # manifest signer key, when the signature verifies
    snapshot: Hex | None                     # sha256 of the replayed snapshot
    tip: Hex | None                          # replayed tip state

    @property
    def ok(self) -> bool:
        return not self.problems


def _resolve(body: bytes, by_sha: Mapping[str, dict]) -> tuple[bytes, dict | None]:
    """The message bytes of one mbox block: the block, or the block minus the LF the writer
    added, whichever has a recorded hash."""
    options = [body] + ([body[:-1]] if body.endswith(b"\n") else [])
    for raw in options:
        row = by_sha.get(E.sha256_hex(raw))
        if row is not None and row["size"] == len(raw):
            return raw, row
    return body, None


def verify_archive(path: Path, *, host_key: str | None = None, trusted_keys: Sequence[str] = (),
                   replay: bool = True) -> ArchiveReport:
    """Offline verification (never raises). `host_key`, when given, must be one of the log's
    keys (the trust anchor). `trusted_keys` admits unplanned handovers (§11.6)."""
    problems: list[str] = []
    missing: list[Hex] = []
    altered: list[Hex] = []
    log_error: str | None = None
    signer: str | None = None
    try:
        files = _read_members(path)
    except ArchiveError as exc:
        files = {}
        problems.append(str(exc))
    try:
        manifest = json.loads(files[MANIFEST].decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise ValueError(f"not a {FORMAT} manifest")
        rows = list(manifest["mail"])
        listed_missing = list(manifest.get("missing") or [])
    except (KeyError, ValueError, TypeError) as exc:
        problems.append(f"manifest: {exc}")
        return ArchiveReport(tuple(problems), False, "no manifest", 0, 0, (), (), False, None, None, None)

    # manifest signature
    if "sig" in manifest:
        try:
            if E.verify_bytes(manifest.get("signer"), _unsigned_manifest(manifest), manifest.get("sig")):
                signer = manifest["signer"]
            else:
                problems.append("manifest signature does not verify")
        except (ValueError, TypeError):
            problems.append("manifest signature does not verify")

    # log
    log = HostLog(trusted_keys=trusted_keys)
    log_bytes = files.get(LOG_FILE, b"")
    if E.sha256_hex(log_bytes) != (manifest.get("log") or {}).get("sha256"):
        problems.append(f"{LOG_FILE} does not match its manifest hash")
    try:
        log.extend(decode_log(log_bytes))
    except LogError as exc:
        log_error = str(exc)
        problems.append(f"host log: {exc}")
    if log.head is None and log_error is None:
        log_error = "empty log"
        problems.append("host log is empty")
    elif log_error is None:
        if (manifest["log"].get("head"), manifest["log"].get("head_hash")) != (log.head.seq, log.head.hash):
            problems.append("manifest log head differs from the log")
        if manifest.get("repo") != log.repo:
            problems.append("manifest repo differs from the log")
    keys = {log.key_at(n) for n in range(len(log))}
    if host_key is not None and host_key not in keys:
        problems.append("the log is not signed by the expected host key")
    if signer is not None and signer not in keys and signer not in trusted_keys:
        problems.append("manifest is signed by a key that is not a host log key")

    # mail
    mbox = files.get(MBOX_FILE, b"")
    if E.sha256_hex(mbox) != (manifest.get("mbox") or {}).get("sha256"):
        problems.append(f"{MBOX_FILE} does not match its manifest hash")
    try:
        bodies = mbox_blocks(mbox)
    except ArchiveError as exc:
        problems.append(f"{MBOX_FILE}: {exc}")
        bodies = []
    by_sha = {row["sha256"]: row for row in rows}
    by_id = {row["id"]: row for row in rows}
    found: dict[Hex, bytes] = {}
    order: list[Hex] = []
    for body in bodies:
        raw, row = _resolve(body, by_sha)
        if row is None:
            gid = _gud_id_of(raw) or (_gud_id_of(raw[:-1]) if raw.endswith(b"\n") else None)
            if gid in by_id:
                altered.append(gid)  # type: ignore[arg-type]
                order.append(gid)  # type: ignore[arg-type]
            else:
                problems.append("mbox holds a message the manifest does not list")
            continue
        if row["id"] in found:
            problems.append(f"message {row['id'][:12]} appears twice")
            continue
        if _gud_id_of(raw) != row["id"]:
            altered.append(row["id"])
        else:
            found[row["id"]] = raw
        order.append(row["id"])
    listed = [row["id"] for row in rows]
    if [g for g in listed if g in set(order)] != order:
        problems.append("mbox messages are not in log order")
        out_of_order = True
    else:
        out_of_order = False
    if [row["seq"] for row in rows] != sorted(row["seq"] for row in rows):
        problems.append("manifest mail is not in log order")
    for row in rows:
        if row["seq"] < len(log):
            entry = log.at(row["seq"])
            if entry.type != "mail" or entry.field("id") != row["id"]:
                problems.append(f"manifest names {row['id'][:12]} at seq {row['seq']}, the log does not")
    logged: list[Hex] = list(dict.fromkeys(gid for _, gid, _ in log.mail_ids()))
    for gid in logged:
        if gid in found or gid in altered:
            continue
        missing.append(gid)
    for gid in sorted(set(altered)):
        problems.append(f"message {gid[:12]} does not match its recorded bytes")
    for gid in missing:
        note = " (the exporter did not have it)" if gid in listed_missing else ""
        problems.append(f"message {gid[:12]} logged at seq {log.seq_of(gid)} is missing{note}")

    # replay
    snap_hash: Hex | None = None
    tip: Hex | None = None
    recorded = manifest.get("snapshot")
    if recorded is not None and E.sha256_hex(files.get(SNAPSHOT_FILE, b"")) != recorded.get("sha256"):
        problems.append(f"{SNAPSHOT_FILE} does not match its manifest hash")
    if replay and log_error is None and not missing and not altered:
        emails = [e for e in (mime.to_email(found[g]) for g in logged if g in found) if e is not None]
        state = _replay_state(emails, log.entries(), trusted_keys)
        if state is not None:
            snap_hash, tip = E.sha256_hex(state[0]), state[1]
        if recorded is not None and (snap_hash, tip) != (recorded.get("sha256"), recorded.get("tip")):
            problems.append("replaying the archive does not reproduce its snapshot")
    return ArchiveReport(
        problems=tuple(problems), log_ok=log_error is None, log_error=log_error, entries=len(log),
        messages=len(found), missing=tuple(missing), altered=tuple(dict.fromkeys(altered)),
        out_of_order=out_of_order, signer=signer, snapshot=snap_hash, tip=tip,
    )


# ---------------------------------------------------------------- load / import


@dataclass(frozen=True)
class Archive:
    manifest: Mapping
    entries: tuple[LogEntry, ...]
    mail: Mapping[Hex, bytes]                # raw MIME by gud id, in log order
    snapshot: bytes | None


def load_archive(path: Path, *, trusted_keys: Sequence[str] = (), host_key: str | None = None) -> Archive:
    """Verify (`verify_archive`) and load. Raises ArchiveError listing the problems."""
    report = verify_archive(path, host_key=host_key, trusted_keys=trusted_keys)
    if not report.ok:
        raise ArchiveError("archive does not verify: " + "; ".join(report.problems))
    files = _read_members(path)
    manifest = json.loads(files[MANIFEST].decode("utf-8"))
    by_sha = {row["sha256"]: row for row in manifest["mail"]}
    mail: dict[Hex, bytes] = {}
    for body in mbox_blocks(files[MBOX_FILE]):
        raw, row = _resolve(body, by_sha)
        assert row is not None
        mail[row["id"]] = raw
    return Archive(manifest, tuple(decode_log(files[LOG_FILE])), mail, files.get(SNAPSHOT_FILE))


def plan_handover(log: HostLog, old_private: bytes, new_public: str, now: Ms) -> LogEntry:
    """Old host, before exporting: append the `handover` entry naming the new host key (§11.6).
    The old host must log nothing after it."""
    if log.head is None:
        raise ArchiveError("the log is not open")
    if E.public_of(old_private) != log.key:
        raise ArchiveError("old_private is not the current log key")
    if log.head.type == "handover":
        raise ArchiveError("the log already ends in a handover")
    return log.handover(old_private, new_public, max(now, log.head.time + 1))


def import_archive(archive: Path, *, repo: str, host_private: bytes, mailbox=None, now: Ms | None = None,
                   mail_store: Path | None = None, log_path: Path | None = None,
                   trust_unplanned: bool = False) -> HostLog:
    """New host: verify the archive and continue its log under `host_private` (§11.6).

    * The archived log ends in `handover` naming this key (planned): append `open` citing it.
    * Its current key already is this key: a restore by the same host; nothing is appended.
    * Otherwise (unplanned) only with `trust_unplanned=True`: `open` citing the last entry, with
      this key trusted locally; members must `gud host trust` it too.

    The repo address stays the log's (`repo` must equal it). Raw mail is re-delivered
    byte-exactly to `mailbox` when it can inject raw bytes (`mailbox.deliver`), in log order, and
    written to `mail_store/<gud id>.eml` when given; `log_path` receives the log text. Returns
    the continued log."""
    new_public = E.public_of(host_private)
    trusted = (new_public,) if trust_unplanned else ()
    loaded = load_archive(archive, trusted_keys=trusted)
    log = HostLog(loaded.entries, trusted_keys=trusted)
    if log.repo != repo:
        raise ArchiveError(f"the archived log is for {log.repo}, not {repo}; a continued log keeps its repo")
    head = log.head
    assert head is not None
    clock = int(time.time() * 1000) if now is None else int(now)
    if head.type == "handover":
        if head.field("next") != new_public:
            raise ArchiveError("the archived handover names a different host key")
        log.continue_after_handover(host_private, max(clock, head.time + 1))
    elif log.key != new_public:
        if not trust_unplanned:
            raise ArchiveError("the archived log has no handover to this key (unplanned handover needs "
                               "trust_unplanned=True)")
        log.continue_after_handover(host_private, max(clock, head.time + 1))
    if mailbox is not None:
        deliver = getattr(mailbox, "deliver", None)
        if deliver is None:
            raise ArchiveError("this mailbox cannot inject raw mail; load mail_store instead")
        for raw in loaded.mail.values():
            deliver(raw)
    if mail_store is not None:
        mail_store = Path(mail_store)
        mail_store.mkdir(parents=True, exist_ok=True)
        for gid, raw in loaded.mail.items():
            (mail_store / f"{gid}.eml").write_bytes(raw)
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_bytes(encode_log(log.entries()))
    return log
