"""Member-side client of the host mirror (SPEC-v1 §21), and the local host mode.

`RepoClient` is what every member tool (CLI, working copy, tester, agent) talks to. It

- reads the host mirror (§21) with signed requests (mirror auth v1: `X-Gud-Handle`,
  `X-Gud-Time`, `X-Gud-Sig` over `"{METHOD} {path_with_query} {time} {sha256(body)}"`);
- syncs the host log and the logged mail incrementally into a persisted `Replayer` under
  `GUD_HOME/cache/<repo>/` (objects in `GUD_HOME/objects/`), verifying every entry (§11.4) and
  that each fetched message's gud id is the one its entry names;
- signs messages with the member's key and sends them through the member's own transport
  (`send_via`, normally from `gud.transport.registry`), blobs first (§6);
- can start a fresh cache from a valid checkpoint (§17) and rebuild the cache from scratch
  for `gud verify` (§22).

Two mirror back ends implement the same small interface (`Mirror`):

- `HttpMirror`: the §21 HTTP endpoints of a host (`gud host serve`, `gud.host_v1`).
- `LocalHost`: **local host mode**. The client is itself the host: it reads the repo address's
  mailbox through its transport, appends signed host log entries (§11.1), keeps the raw mail,
  and serves the same calls in process. A `host_url` of `local:<dir>` selects it; several
  members on one machine may share the directory (an `flock` serialises intake).

GUD_HOME layout used here:

    objects/                         content-addressed store (DiskStore)
    cache/<repo>/state.json          the member's Replayer (disposable, rebuildable)
    cache/<repo>/client.json         {"checkpoint": bool, "trusted": [...]} (local choices)
    members/<handle>/key             the member's private key, `ed25519:<base64 seed>`
    members/<handle>/transport.toml  the member's transport (gud.transport.registry)
    host/<repo>/                     a local host (key, log.jsonl, mail/, transport.toml, ...)
"""

from __future__ import annotations

import base64
import fcntl
import json
import math
import os
import re
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import quote, urlsplit

from gud.core import envelope as E
from gud.core.config import RepoRules, bare_address
from gud.core.envelope import sha256_hex
from gud.core.hostlog import HostLog, LogError, decode_entry, encode_entry
from gud.core.objects import DiskStore
from gud.core.replay import ChainReader, ReplayError, Replayer, Snapshot
from gud.core.replay import snapshot_at as _core_snapshot_at
from gud.core.types import (
    Chain, ChainDelta, Email, Envelope, Handle, Hex, LogEntry, Member, Ms, ObjectStoreLike, Record, Seen,
)
from gud.events.dispatch import (
    AUTH_HANDLE, AUTH_SIG, AUTH_TIME, MAX_SKEW_S, format_time, request_bytes, sign_request, verify_request,
)
from gud.transport import mime
from gud.transport.base import Mailbox, Outgoing, envelope_budget

# Mirror auth v1 (§21) has one implementation, in gud.events.dispatch (the host verifies with
# the same code); these names are re-exported for members.
HANDLE_HEADER, TIME_HEADER, SIG_HEADER = AUTH_HANDLE, AUTH_TIME, AUTH_SIG
MAX_SKEW = MAX_SKEW_S
API = "/api/v1"
PAGE = 500
_BLOB_HEADROOM = 2048            # bytes of a blob envelope that are not chunk data
_TICK_GAP_MS = 1000              # local host: append a tick when the head is this old


class RemoteError(RuntimeError):
    """The mirror is unreachable, refused a request, or served data that fails verification."""


# ---------------------------------------------------------------- keys and small helpers

def repo_slug(repo: str) -> str:
    return quote(repo.strip().lower(), safe="@.+-_")


def encode_private_key(private_raw: bytes) -> str:
    return "ed25519:" + base64.b64encode(private_raw).decode("ascii")


def decode_private_key(text: str) -> bytes:
    value = text.strip()
    if value.startswith("ed25519:"):
        value = value[len("ed25519:"):]
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise RemoteError("not a gud private key") from exc
    if len(raw) != 32:
        raise RemoteError("not a gud private key")
    return raw


def key_path(home: Path, handle: Handle) -> Path:
    return Path(home) / "members" / handle / "key"


def save_key(home: Path, handle: Handle, private_raw: bytes, *, name: str = "key", overwrite: bool = False) -> Path:
    path = key_path(home, handle).with_name(name)
    if path.exists() and not overwrite:
        raise RemoteError(f"{path} exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(encode_private_key(private_raw) + "\n")
    os.replace(tmp, path)
    return path


def load_key(home: Path, handle: Handle, *, name: str = "key") -> bytes:
    path = key_path(home, handle).with_name(name)
    try:
        return decode_private_key(path.read_text("ascii"))
    except FileNotFoundError as exc:
        raise RemoteError(f"no key for {handle} at {path} (run `gud keys new`)") from exc


def now_ms(clock: Callable[[], float]) -> Ms:
    return int(clock() * 1000)


@contextmanager
def _flock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_toml_transport(path: Path) -> tuple[str, dict[str, str]]:
    from gud.transport.registry import read_transport_toml
    return read_transport_toml(path)


def write_transport_toml(path: Path, name: str, cfg: Mapping[str, str]) -> None:
    lines = [f"transport = {json.dumps(name)}"]
    lines += [f"{key} = {json.dumps(str(cfg[key]))}" for key in sorted(cfg)]
    _write_atomic(Path(path), ("\n".join(lines) + "\n").encode("utf-8"))


def snapshot_at(entries: Sequence[LogEntry], mail: Callable[[Hex], Email | None], seq: int,
                store: ObjectStoreLike, trusted_host_keys: Sequence[str] = ()) -> bytes:
    """§17 snapshot bytes for log position `seq` (`gud.core.replay.snapshot_at`)."""
    try:
        return _core_snapshot_at(entries, mail, seq, store, trusted_host_keys)
    except ReplayError as exc:
        raise RemoteError(str(exc)) from exc


# ---------------------------------------------------------------- mirror back ends

class Mirror(Protocol):
    """The §21 read API (plus local intake) as plain calls."""
    def info(self) -> dict: ...
    def log(self, since: int, limit: int) -> dict: ...
    def mail_batch(self, since: int, limit: int) -> dict: ...
    def mail(self, gud_id: Hex) -> bytes | None: ...
    def object(self, sha256: Hex) -> bytes | None: ...
    def snapshot(self, sha256: Hex) -> bytes | None: ...
    def delivery(self, body: Mapping[str, Any]) -> dict: ...
    def pull(self) -> None: ...


class HttpMirror:
    """§21 over HTTP. Every request except `/api/v1/info` is signed (mirror auth v1)."""

    def __init__(self, base_url: str, handle: Handle, private_raw: bytes, *, http: Any = None,
                 clock: Callable[[], float] = time.time, timeout: float = 30.0) -> None:
        import httpx
        self.base_url = base_url.rstrip("/")
        self._prefix = urlsplit(self.base_url).path.rstrip("/")
        self.handle = handle
        self._key = private_raw
        self._clock = clock
        self._http = http if http is not None else httpx.Client(timeout=timeout)

    def _request(self, method: str, path: str, body: bytes = b"", *, signed: bool = True,
                 ok404: bool = False) -> Any:
        import httpx
        headers = {"Content-Type": "application/json"} if body else {}
        if signed:
            # X-Gud-Time is whole unix seconds (wc note 7)
            headers.update(sign_request(self.handle, self._key, method, self._prefix + path, body,
                                        math.floor(self._clock())))
        try:
            resp = self._http.request(method, self.base_url + path, content=body or None, headers=headers)
        except httpx.HTTPError as exc:
            raise RemoteError(f"{method} {path}: {exc}") from exc
        if ok404 and resp.status_code == 404:
            return None
        if resp.status_code >= 300:
            raise RemoteError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp

    def info(self) -> dict:
        return self._request("GET", f"{API}/info", signed=False).json()

    def log(self, since: int, limit: int) -> dict:
        return self._request("GET", f"{API}/log?since={since}&limit={limit}").json()

    def mail_batch(self, since: int, limit: int) -> dict:
        return self._request("GET", f"{API}/mail?since={since}&limit={limit}").json()

    def mail(self, gud_id: Hex) -> bytes | None:
        resp = self._request("GET", f"{API}/mail/{gud_id}", ok404=True)
        return None if resp is None else resp.content

    def object(self, sha256: Hex) -> bytes | None:
        resp = self._request("GET", f"{API}/object/{sha256}", ok404=True)
        return None if resp is None else resp.content

    def snapshot(self, sha256: Hex) -> bytes | None:
        resp = self._request("GET", f"{API}/snapshot/{sha256}", ok404=True)
        return None if resp is None else resp.content

    def delivery(self, body: Mapping[str, Any]) -> dict:
        data = json.dumps(dict(body), sort_keys=True).encode("utf-8")
        resp = self._request("POST", f"{API}/delivery", data)
        return resp.json() if resp.content else {}

    def pull(self) -> None:
        return None


class LocalHost:
    """Local host mode: this process is the host of the repo address (§1, §11).

    Directory layout:

        key                 host private key (`ed25519:<base64 seed>`, mode 0600)
        host.json           {"repo", "bugs"}
        transport.toml      the repo mailbox's transport (gud.transport.registry form)
        log.jsonl           host log, one JSON string (entry text incl. sig line) per line
        mail/<2 hex>/<id>.eml  raw MIME of every logged message, as received (the host_v1 layout)
        cursor              the repo mailbox cursor
        checkpoints.json    snapshot sha256 -> log seq, from logged checkpoint messages
        snapshots/<sha256>  computed snapshot bytes
        deliveries.json     delivery preferences (§19.3), per handle
        replay/             the host's own replay (for the unsigned-mail rule of §11.1)

    `pull()` is the intake: list the repo mailbox from the stored cursor, log every message
    §11.1 says to log (each decoding v1 envelope message, and unsigned mail that passes §8.1
    (2) and (3) at the tip), skip ids already logged, and append a `tick` when the head is older
    than a second, so replay time follows the clock while members sync."""

    def __init__(self, root: Path, *, mailbox: Mailbox | None = None, clock: Callable[[], float] = time.time,
                 store: ObjectStoreLike | None = None, home: Path | None = None) -> None:
        self.root = Path(root)
        if not (self.root / "host.json").exists():
            raise RemoteError(f"{self.root} is not a gud local host (run `gud host init`)")
        meta = json.loads((self.root / "host.json").read_text("utf-8"))
        self.repo: str = meta["repo"]
        self.bugs: str | None = meta.get("bugs")
        self._clock = clock
        self._key = decode_private_key((self.root / "key").read_text("ascii"))
        self._mailbox = mailbox
        objects = Path(meta.get("objects") or (self.root / "objects"))
        self.store: ObjectStoreLike = store if store is not None else DiskStore(objects)
        self._log = self._load_log()

    # ------------------------------------------------------------ setup

    @classmethod
    def create(cls, root: Path, *, repo: str, transport: str, cfg: Mapping[str, str],
               host_private: bytes | None = None, bugs: str | None = None, objects: Path | None = None,
               clock: Callable[[], float] = time.time, mailbox: Mailbox | None = None) -> LocalHost:
        """Create a host directory: key, transport config, and the log's `open` entry."""
        root = Path(root)
        if (root / "host.json").exists():
            raise RemoteError(f"{root} already holds a host")
        root.mkdir(parents=True, exist_ok=True)
        raw = host_private if host_private is not None else E.generate_key()[0]
        fd = os.open(root / "key", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(encode_private_key(raw) + "\n")
        write_transport_toml(root / "transport.toml", transport, {**cfg, "address": repo})
        log = HostLog.open(repo, raw, now_ms(clock))
        (root / "log.jsonl").write_text(json.dumps(encode_entry(log.at(0)).decode("utf-8")) + "\n", "utf-8")
        meta = {"repo": repo, "bugs": bugs}
        if objects is not None:
            meta["objects"] = str(objects)
        (root / "host.json").write_text(json.dumps(meta, indent=1) + "\n", "utf-8")
        return cls(root, clock=clock, mailbox=mailbox)

    @classmethod
    def install(cls, root: Path, *, log: HostLog, transport: str, cfg: Mapping[str, str], host_private: bytes,
                bugs: str | None = None, objects: Path | None = None) -> LocalHost:
        """A host directory for a log continued from an archive (`gud import-archive`): the key,
        the log (which must already be signed up to its `open` by this key), the transport for
        the log's current repo address. Mail goes in `mail/` (flat or sharded)."""
        root = Path(root)
        if (root / "host.json").exists():
            raise RemoteError(f"{root} already holds a host")
        if log.key != E.public_of(host_private):
            raise RemoteError("the log's current key is not this host's key")
        root.mkdir(parents=True, exist_ok=True)
        key_file = root / "key"
        if not key_file.exists():
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(encode_private_key(host_private) + "\n")
        repo = log.repo or ""
        write_transport_toml(root / "transport.toml", transport, {**cfg, "address": repo})
        _write_atomic(root / "log.jsonl", "".join(json.dumps(encode_entry(e).decode("utf-8")) + "\n"
                                                   for e in log.entries()).encode("utf-8"))
        meta = {"repo": repo, "bugs": bugs}
        if objects is not None:
            meta["objects"] = str(objects)
        (root / "host.json").write_text(json.dumps(meta, indent=1) + "\n", "utf-8")
        return cls(root)

    def handover(self, new_public: str, *, repo: str | None = None) -> LogEntry:
        """Old host, before `gud export-archive --host`: sign the §11.6 handover to `new_public`
        (and the repo's new address, integration note 4). Nothing is logged after it."""
        if self.served():
            raise RemoteError(f"{self.root} is being served; stop `gud host serve` first")
        with _flock(self.root / "lock"):
            self._log = self._load_log()
            head = self._log.head
            if head is None or head.type == "handover":
                raise RemoteError("the log already ends in a handover")
            entry = self._log.handover(self._key, new_public, max(now_ms(self._clock), head.time + 1), repo=repo)
            self._append(entry)
            return entry

    @property
    def host_key(self) -> str:
        return E.public_of(self._key)

    def mailbox(self) -> Mailbox:
        if self._mailbox is None:
            from gud.transport.registry import open_mailbox
            name, cfg = _read_toml_transport(self.root / "transport.toml")
            self._mailbox = open_mailbox(name, cfg)
        return self._mailbox

    def _load_log(self) -> HostLog:
        path = self.root / "log.jsonl"
        entries = []
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(decode_entry(json.loads(line).encode("utf-8")))
            except (ValueError, LogError):
                break             # a torn last line while `gud host serve` appends
        return HostLog(entries)

    def _append(self, entry: LogEntry) -> None:
        with open(self.root / "log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(encode_entry(entry).decode("utf-8")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # ------------------------------------------------------------ intake

    def served(self) -> bool:
        """Whether `gud host serve` runs on this directory (it holds `serve.lock`). Its intake
        then owns the log: local-mode members only read."""
        path = self.root / "serve.lock"
        if not path.exists():
            return False
        with open(path, "a+b") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False

    def pull(self) -> None:
        if self.served():
            self._log = self._load_log()
            return
        with _flock(self.root / "lock"):
            self._log = self._load_log()
            if self._log.head is not None and self._log.head.type == "handover":
                return                    # handed over: this host logs nothing more (§11.6)
            cursor_path = self.root / "cursor"
            cursor = cursor_path.read_text("utf-8").strip() if cursor_path.exists() else None
            mailbox = self.mailbox()
            appended = False
            while True:
                items, next_cursor = mailbox.list(since=cursor or None, limit=PAGE)
                if not items:
                    break
                for item in items:
                    appended |= self._intake(item.raw_mime, item.received_at)
                cursor = next_cursor
                _write_atomic(cursor_path, (cursor or "").encode("utf-8"))
            head = self._log.head
            now = now_ms(self._clock)
            if head is not None and not appended and now - head.time >= _TICK_GAP_MS:
                entry = self._log.append_tick(self._key, now)
                self._append(entry)

    def _intake(self, raw: bytes, received_at: Ms) -> bool:
        email = mime.to_email(raw)
        if email is None or self._log.seq_of(email.gud_id) is not None:
            return False
        if email.envelope is None and not self._unsigned_counts(email):
            return False
        head = self._log.head
        assert head is not None
        host_now = max(now_ms(self._clock), head.time + 1)
        _write_atomic(self.root / "mail" / email.gud_id[:2] / f"{email.gud_id}.eml", raw)
        entry = self._log.append_mail(email.gud_id, min(int(received_at), host_now), self._key, host_now)
        self._append(entry)
        env = email.envelope
        if env is not None and env.kind == "checkpoint" and env.get("Gud-Snapshot") and env.get("Gud-Log"):
            path = self.root / "checkpoints.json"
            known = json.loads(path.read_text("utf-8")) if path.exists() else {}
            try:
                known.setdefault(env.get("Gud-Snapshot"), E.parse_seen(env.get("Gud-Log") or "").seq)
            except E.EnvelopeError:
                pass
            _write_atomic(path, json.dumps(known, sort_keys=True).encode("utf-8"))
        return True

    def _host_replayer(self) -> Replayer:
        state = self.root / "replay"
        r = Replayer.load(state, self.store) if (state / "state.json").exists() else Replayer(self.store, state_dir=state)
        have = len(r.log())
        entries = self._log.entries(have)
        emails = [e for e in (self._email(gid) for _, gid, _ in self._log.mail_ids(have)) if e is not None]
        try:
            r.feed(emails, entries, now_ms(self._clock))
        except ReplayError:
            pass
        r.save()
        return r

    def _unsigned_counts(self, email: Email) -> bool:
        """§11.1 / §8.1 (2) and (3) at the tip."""
        r = self._host_replayer()
        if r.tip is None:
            return False
        rules: RepoRules = r.reader().rules()
        authserv = rules.config.authserv_id
        if not authserv:
            return False
        sender = bare_address(email.from_addr)
        member = rules.members.by_address(sender)
        if member is None or member.role != "human":
            return False
        return E.dmarc_pass(email.auth_results, authserv, sender.rpartition("@")[2])

    def _email(self, gud_id: Hex) -> Email | None:
        raw = self.mail(gud_id)
        return mime.to_email(raw) if raw is not None else None

    # ------------------------------------------------------------ the §21 read API

    def info(self) -> dict:
        head = self._log.head
        mailbox = self._mailbox
        try:
            caps = (mailbox or self.mailbox()).capabilities()
            max_bytes, transport = caps.max_message_bytes, caps.name
        except Exception:
            max_bytes, transport = 10 * 1024 * 1024, "unknown"
        return {
            "repo": self.repo, "bugs_address": self.bugs or f"bugs@{self.repo.rpartition('@')[2]}",
            "host_key": self._log.key,
            "log_head": {"seq": head.seq, "hash": head.hash, "time": head.time} if head else None,
            "max_message_bytes": max_bytes, "transport": transport,
        }

    def log(self, since: int, limit: int) -> dict:
        self._log = self._load_log()
        head = self._log.head
        return {"entries": [encode_entry(e).decode("utf-8") for e in self._log.entries(since, limit)],
                "head": {"seq": head.seq, "hash": head.hash} if head else None}

    def mail_batch(self, since: int, limit: int) -> dict:
        self._log = self._load_log()
        out = []
        nxt = None
        for entry in self._log.entries(max(0, since)):
            if entry.type != "mail":
                continue
            if len(out) >= limit:
                nxt = entry.seq
                break
            raw = self.mail(entry.fields[0][1])
            if raw is not None:
                out.append({"seq": entry.seq, "id": entry.fields[0][1], "raw": base64.b64encode(raw).decode("ascii")})
        return {"mail": out, "next": nxt}

    def mail(self, gud_id: Hex) -> bytes | None:
        if not E.is_hex64(gud_id):
            return None
        # the host_v1 layout (mail/<2 hex>/<id>.eml), so `gud host serve` can serve this directory;
        # flat mail/<id>.eml is read too
        for path in (self.root / "mail" / gud_id[:2] / f"{gud_id}.eml", self.root / "mail" / f"{gud_id}.eml"):
            if path.exists():
                return path.read_bytes()
        return None

    def object(self, sha256: Hex) -> bytes | None:
        try:
            return self.store.get(sha256) if self.store.has(sha256) else None
        except KeyError:
            return None

    def snapshot(self, sha256: Hex) -> bytes | None:
        if not E.is_hex64(sha256):
            return None
        cached = self.root / "snapshots" / sha256
        if cached.exists():
            return cached.read_bytes()
        path = self.root / "checkpoints.json"
        known = json.loads(path.read_text("utf-8")) if path.exists() else {}
        seq = known.get(sha256)
        if seq is None:
            return None
        try:
            data = snapshot_at(self._log.entries(), self._email, int(seq), self.store)
        except (RemoteError, ReplayError):
            return None
        if sha256_hex(data) != sha256:
            return None
        _write_atomic(cached, data)
        return data

    def delivery(self, body: Mapping[str, Any], handle: Handle | None = None) -> dict:
        path = self.root / "deliveries.json"
        with _flock(self.root / "lock"):
            known = json.loads(path.read_text("utf-8")) if path.exists() else {}
            known[handle or "-"] = dict(body)
            _write_atomic(path, json.dumps(known, sort_keys=True).encode("utf-8"))
        return {"ok": True}


def open_mirror(host_url: str, handle: Handle, private_raw: bytes, *, http: Any = None,
                clock: Callable[[], float] = time.time, mailbox: Mailbox | None = None) -> Mirror:
    """`local:<dir>` -> LocalHost; `http(s)://...` -> HttpMirror."""
    if host_url.startswith("local:"):
        return LocalHost(Path(host_url[len("local:"):]).expanduser(), clock=clock, mailbox=mailbox)
    if host_url.startswith(("http://", "https://")):
        return HttpMirror(host_url, handle, private_raw, http=http, clock=clock)
    raise RemoteError(f"unsupported host url {host_url!r} (use http(s)://... or local:<dir>)")


def client_from_home(home: Path, handle: Handle | None = None, *, host_url: str | None = None,
                     repo: str | None = None, key: bytes | None = None,
                     clock: Callable[[], float] = time.time) -> "RepoClient":
    """A `RepoClient` from a member's GUD_HOME: `remote.json` (written by `gud init`, `clone`,
    `join`) gives the host URL, repo and handle unless passed; the key is
    `members/<handle>/key`; the member's transport (`members/<handle>/transport.toml`) sends."""
    home = Path(home)
    path = home / "remote.json"
    remote = json.loads(path.read_text("utf-8")) if path.exists() else {}
    handle = handle or remote.get("handle")
    host_url = host_url or remote.get("host_url")
    if not handle or not host_url:
        raise RemoteError(f"no repo in {home} (run `gud clone` or pass the host URL and handle)")
    handle = handle if handle.startswith("@") else "@" + handle
    raw = key if key is not None else load_key(home, handle)
    mailbox = None
    if (home / "members" / handle / "transport.toml").exists():
        from gud.transport.registry import member_config, open_mailbox
        name, cfg = member_config(home, handle)
        mailbox = open_mailbox(name, cfg)
    if not repo:
        repo = remote.get("repo") if remote.get("host_url") == host_url else None
    if not repo:
        repo = open_mirror(host_url, handle, raw, clock=clock).info()["repo"]
    return RepoClient(host_url, handle, raw, send_via=mailbox, repo_address=repo, home=home, clock=clock)


class FetchingStore:
    """A local store that fetches missing objects from the mirror (checked by hash). Used by
    caches that started from a checkpoint (§17), whose older trees are not local."""

    def __init__(self, local: ObjectStoreLike, fetch: Callable[[Hex], bytes | None]) -> None:
        self.local = local
        self._fetch = fetch
        self._missing: set[Hex] = set()

    def _pull(self, key: Hex) -> bool:
        if key in self._missing:
            return False
        try:
            data = self._fetch(key)
        except RemoteError:
            data = None
        if data is None or sha256_hex(data) != key:
            self._missing.add(key)
            return False
        self.local.put(data)
        return True

    def get(self, key: Hex) -> bytes:
        if self.local.has(key) or self._pull(key):
            return self.local.get(key)
        raise KeyError(key)

    def put(self, data: bytes) -> Hex:
        key = self.local.put(data)
        self._missing.discard(key)
        return key

    def has(self, key: Hex) -> bool:
        return self.local.has(key) or self._pull(key)


# ---------------------------------------------------------------- the client

def _entries_of(doc: Mapping[str, Any]) -> list[LogEntry]:
    out = []
    for text in doc.get("entries") or ():
        try:
            out.append(decode_entry(text.encode("utf-8")))
        except LogError as exc:
            raise RemoteError(f"mirror served a malformed log entry: {exc}") from exc
    return out


class RepoClient:
    """A member's view of one repo: mirror reads, the local replay cache, signing and sending.

    `host_url` is `http(s)://...` (a §21 mirror) or `local:<dir>` (local host mode); `mirror`
    overrides both (an object with the `Mirror` calls). `send_via` is the member's own
    mailbox; it may be None for read-only use. `clock` returns unix seconds (float)."""

    def __init__(self, host_url: str, handle: Handle, private_raw: bytes, *, send_via: Mailbox | None,
                 repo_address: str, home: Path, http: Any = None, clock: Callable[[], float] = time.time,
                 mirror: Mirror | None = None, trusted_host_keys: Sequence[str] = ()) -> None:
        self.host_url = host_url
        self.handle = handle
        self._key = private_raw
        self.send_via = send_via
        self.repo = repo_address
        self.home = Path(home)
        self.clock = clock
        self.mirror: Mirror = mirror if mirror is not None else open_mirror(
            host_url, handle, private_raw, http=http, clock=clock)
        self.cache_dir = self.home / "cache" / repo_slug(repo_address)
        self.objects: ObjectStoreLike = DiskStore(self.home / "objects")
        meta = self._meta()
        trusted = tuple(dict.fromkeys(list(meta.get("trusted", [])) + list(trusted_host_keys)))
        if tuple(meta.get("trusted", [])) != trusted:
            self._save_meta({**meta, "trusted": list(trusted)})
        self.trusted_host_keys = trusted
        self.store: ObjectStoreLike = (FetchingStore(self.objects, self._fetch_object_raw)
                                       if meta.get("checkpoint") else self.objects)
        self._replayer = self._load_replayer()
        self._info: dict | None = None
        self.last_problems: list[str] = []
        # called with every delta `sync_log` produces (including the syncs WorkingCopy runs), so
        # tools that journal events (gud.mcp_v1, gud.agent) see all of them
        self.on_delta: list[Callable[[ChainDelta], None]] = []

    # ------------------------------------------------------------ cache

    def _meta(self) -> dict:
        path = self.cache_dir / "client.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else {}

    def _save_meta(self, meta: Mapping[str, Any]) -> None:
        _write_atomic(self.cache_dir / "client.json", json.dumps(dict(meta), sort_keys=True).encode("utf-8"))

    def _load_replayer(self) -> Replayer:
        if (self.cache_dir / "state.json").exists():
            try:
                return Replayer.load(self.cache_dir, self.store)
            except (ValueError, KeyError, OSError) as exc:
                raise RemoteError(f"replay cache {self.cache_dir} is unreadable ({exc}); "
                                  f"delete it and sync again") from exc
        return Replayer(self.store, state_dir=self.cache_dir, trusted_host_keys=self.trusted_host_keys)

    def reset_cache(self) -> None:
        """Drop the replay cache (it is disposable); the next sync replays from scratch."""
        for name in ("state.json", "head"):
            (self.cache_dir / name).unlink(missing_ok=True)
        meta = self._meta()
        meta.pop("checkpoint", None)
        self._save_meta(meta)
        self.store = self.objects
        self._replayer = self._load_replayer()

    def trust_host_key(self, key: str) -> None:
        """§11.6 unplanned handover: trust `key` for a continued log, then rebuild the cache."""
        if not E.is_public_key(key):
            raise RemoteError(f"not a public key: {key!r}")
        meta = self._meta()
        meta["trusted"] = sorted(set(meta.get("trusted", [])) | {key})
        self._save_meta(meta)
        self.trusted_host_keys = tuple(meta["trusted"])
        self.reset_cache()

    # ------------------------------------------------------------ reading

    def now_ms(self) -> Ms:
        return now_ms(self.clock)

    def info(self) -> dict:
        self._info = self.mirror.info()
        return self._info

    def replayer(self) -> Replayer:
        return self._replayer

    @property
    def repo_address(self) -> str:
        """Where this member sends: the repo address of the latest `open` in the verified log, so
        members follow a handover that moved the repo (integration note 4). `repo` stays the
        address the cache is named after."""
        log = self._replayer.log()
        return (log.repo if len(log) else None) or self.repo

    def chain(self) -> Chain:
        return self._replayer.chain()

    def reader(self) -> ChainReader:
        return self._replayer.reader()

    def seen(self) -> Seen:
        head = self._replayer.log().head
        if head is None:
            raise RemoteError("no verified host log yet (sync first)")
        return Seen(head.seq, head.hash)

    def member(self, handle: Handle | None = None) -> Member | None:
        if self.chain().tip is None:
            return None
        return self.reader().members().get(handle or self.handle)

    def _fetch_object_raw(self, sha256: Hex) -> bytes | None:
        return self.mirror.object(sha256)

    @property
    def private_raw(self) -> bytes:
        """The member's signing key (for `EventClient.stream`, which signs its own SSE request)."""
        return self._key

    @property
    def http(self) -> Any:
        return getattr(self.mirror, "_http", None)

    def log_entries(self, since: int, limit: int) -> list[LogEntry]:
        """Log entries as the mirror serves them now, decoded but not verified against the cache
        (for `gud audit` and `gud export-archive`, which verify them themselves). Stops at the
        first entry that does not decode."""
        self.mirror.pull()
        out: list[LogEntry] = []
        for text in self.mirror.log(since, limit).get("entries") or ():
            try:
                out.append(decode_entry(text.encode("utf-8")))
            except LogError:
                break
        return out

    def raw(self, gud_id: Hex) -> bytes | None:
        """Raw MIME of one logged message as the mirror serves it now (None if it does not)."""
        return self.mirror.mail(gud_id)

    def fetch_object(self, sha256: Hex) -> bytes:
        data = self.mirror.object(sha256)
        if data is None:
            raise RemoteError(f"mirror has no object {sha256[:12]}")
        if sha256_hex(data) != sha256:
            raise RemoteError(f"object {sha256[:12]} does not match its hash")
        return data

    def fetch_snapshot(self, sha256: Hex) -> bytes:
        data = self.mirror.snapshot(sha256)
        if data is None:
            raise RemoteError(f"mirror has no snapshot {sha256[:12]}")
        if sha256_hex(data) != sha256:
            raise RemoteError(f"snapshot {sha256[:12]} does not match its hash")
        return data

    def _fetch_log(self, since: int, upto: int | None = None) -> list[LogEntry]:
        out: list[LogEntry] = []
        while True:
            limit = PAGE if upto is None else min(PAGE, upto - since)
            if limit <= 0:
                break
            doc = self.mirror.log(since, limit)
            self._check_head(doc.get("head"))
            batch = _entries_of(doc)
            if not batch:
                break
            for e in batch:
                if e.seq != since:
                    raise RemoteError(f"mirror served log entry {e.seq} where {since} was expected")
                out.append(e)
                since += 1
            if len(batch) < limit:
                break
        return out

    def _check_head(self, head: Any) -> None:
        """The served head must extend the verified log: a shorter or different history is a
        rollback or fork by the host (§11.4, §11.5)."""
        log = self._replayer.log()
        if not head or not len(log):
            return
        seq, h = int(head.get("seq", -1)), head.get("hash")
        if seq < len(log) - 1:
            raise RemoteError(f"host log verification failed: the mirror's head {seq} is behind the "
                              f"verified log ({len(log) - 1}); the host rolled its log back")
        if seq < len(log) and log.at(seq).hash != h:
            raise RemoteError(f"host log verification failed: the mirror's entry {seq} differs from the "
                              f"verified one (fork)")

    def _fetch_mail(self, wanted: Mapping[Hex, int], problems: list[str]) -> list[Email]:
        """Raw mail for `wanted` (id -> seq), batch first, then one by one; ids checked (§11.4)."""
        got: dict[Hex, Email] = {}
        if wanted:
            since = min(wanted.values())
            last = max(wanted.values())
            while since is not None and since <= last and len(got) < len(wanted):
                doc = self.mirror.mail_batch(since, PAGE)
                for item in doc.get("mail") or ():
                    gid = item.get("id")
                    if gid in wanted and gid not in got:
                        email = self._check_mail(gid, base64.b64decode(item.get("raw") or ""), problems)
                        if email is not None:
                            got[gid] = email
                nxt = doc.get("next")
                since = int(nxt) if nxt is not None else None
        for gid in wanted:
            if gid in got:
                continue
            raw = self.mirror.mail(gid)
            if raw is None:
                problems.append(f"mirror has no mail for logged id {gid[:12]}")
                continue
            email = self._check_mail(gid, raw, problems)
            if email is not None:
                got[gid] = email
        return [got[g] for g in wanted if g in got]

    @staticmethod
    def _check_mail(gid: Hex, raw: bytes, problems: list[str]) -> Email | None:
        email = mime.to_email(raw)
        if email is None or email.gud_id != gid:
            problems.append(f"mail served for {gid[:12]} has a different gud id")
            return None
        return email

    def sync_log(self) -> ChainDelta:
        """Fetch new log entries and the mail they name, verify, feed the cache, save."""
        with _flock(self.cache_dir / "lock"):
            self.mirror.pull()
            r = self._replayer
            entries = self._fetch_log(len(r.log()))
            processed = r.chain().log_head.seq
            wanted: dict[Hex, int] = {}
            for e in entries:
                if e.type == "mail" and e.seq > processed:
                    wanted.setdefault(e.fields[0][1], e.seq)
            for gid in r.wanted_mail():
                seq = r.log().seq_of(gid)
                wanted.setdefault(gid, seq if seq is not None else 0)
            problems: list[str] = []
            emails = self._fetch_mail(wanted, problems)
            self.last_problems = problems
            try:
                delta = r.feed(emails, entries, self.now_ms())
            except ReplayError as exc:
                r.save()
                if exc.delta is not None:
                    self._deliver_delta(exc.delta)
                raise RemoteError(f"host log verification failed: {exc}") from exc
            r.save()
        self._deliver_delta(delta)
        return delta

    def _deliver_delta(self, delta: ChainDelta) -> None:
        for hook in list(self.on_delta):
            hook(delta)

    def bootstrap(self, *, checkpoint: bool = True) -> str:
        """Prepare an empty cache: from the newest valid checkpoint when `checkpoint` and one
        exists ("checkpoint"), else from genesis ("genesis"). "cache" if a cache exists."""
        if len(self._replayer.log()) > 0:
            return "cache"
        if checkpoint:
            found = self._find_checkpoint()
            if found is not None:
                with _flock(self.cache_dir / "lock"):
                    self.store = FetchingStore(self.objects, self._fetch_object_raw)
                    self._replayer = Replayer.from_checkpoint(found, self.store, state_dir=self.cache_dir,
                                                              trusted_host_keys=self.trusted_host_keys)
                    self._replayer.save()
                    self._save_meta({**self._meta(), "checkpoint": True})
                return "checkpoint"
        return "genesis"

    def _find_checkpoint(self, *, scan: int = 5000) -> bytes | None:
        """Newest snapshot named by a valid checkpoint (§17), found by reading logged mail from
        the log head backwards (at most `scan` entries). The signers are checked against the
        voters in the snapshot's own tip rules; `gud verify --from-genesis` removes that trust."""
        self.mirror.pull()
        entries = self._fetch_log(0)
        groups: dict[tuple[str, str, str], dict[Handle, Envelope]] = {}
        for entry in reversed(entries[-scan:]):
            if entry.type != "mail":
                continue
            gid = entry.fields[0][1]
            raw = self.mirror.mail(gid)
            email = mime.to_email(raw) if raw is not None else None
            env = email.envelope if email is not None and email.gud_id == gid else None
            if env is None or env.kind != "checkpoint" or env.author is None:
                continue
            key = (env.get("Gud-Log") or "", env.get("Gud-Link") or "", env.get("Gud-Snapshot") or "")
            groups.setdefault(key, {})[env.author] = env
        best: tuple[int, bytes] | None = None
        for (log, link, snap), signers in groups.items():
            try:
                seen = E.parse_seen(log)
            except E.EnvelopeError:
                continue
            if seen.seq >= len(entries) or entries[seen.seq].hash != seen.entry_hash:
                continue
            if best is not None and seen.seq <= best[0]:
                continue
            try:
                data = self.fetch_snapshot(snap)
                state = Snapshot.from_bytes(data).state
                rules = RepoRules.decode(state["tip"]["rules"].encode("utf-8"))
            except (RemoteError, ReplayError, KeyError, TypeError, ValueError):
                continue
            links = state.get("links") or []
            if not links or links[-1].get("state") != link:
                continue
            voters = set(rules.members.voters())
            good = [h for h, env in signers.items() if h in voters and
                    E.verify(env, rules.members.get(h).public_key)]  # type: ignore[union-attr]
            if len(good) * 2 > len(voters):
                best = (seen.seq, data)
        return best[1] if best else None

    def rebuild(self, *, from_checkpoint: bool = True, upto: int | None = None,
                store: ObjectStoreLike | None = None) -> Replayer:
        """A fresh replay (not saved) from the mirror's log and mail, from genesis or from the
        newest valid checkpoint known to the cache. `upto` stops at that many log entries."""
        from gud.core.objects import MemoryStore
        store = store if store is not None else MemoryStore()
        n = len(self._replayer.log()) if upto is None else upto
        entries = self._fetch_log(0, n)
        fresh: Replayer | None = None
        start = -1
        if from_checkpoint:
            groups = self._replayer.valid_checkpoints()
            for cp in sorted(groups, key=lambda c: E.parse_seen(c["log"]).seq, reverse=True):
                try:
                    data = self.fetch_snapshot(cp["snapshot"])
                    fresh = Replayer.from_checkpoint(data, FetchingStore(store, self._fetch_object_raw),
                                                     trusted_host_keys=self.trusted_host_keys)
                    start = E.parse_seen(cp["log"]).seq
                    break
                except (RemoteError, ReplayError):
                    fresh = None
        if fresh is None:
            fresh = Replayer(store, trusted_host_keys=self.trusted_host_keys)
        wanted = {e.fields[0][1]: e.seq for e in entries if e.type == "mail" and e.seq > start}
        problems: list[str] = []
        emails = self._fetch_mail(wanted, problems)
        self.last_problems = problems
        fresh.feed(emails, entries, self.now_ms())
        return fresh

    # ------------------------------------------------------------ checkpoints

    def snapshot_at(self, seq: int) -> bytes:
        """§17 snapshot bytes at log position `seq`, from the cache's log and mail."""
        reader = self.reader()
        return snapshot_at(self._replayer.log().entries(), reader.email, seq, self.store,
                           self.trusted_host_keys)

    def recommended_checkpoint(self) -> int | None:
        """§17 "When": the last entry with time <= linked_at of the newest link whose seq is a
        multiple of the `checkpoint` setting."""
        chain = self.chain()
        if chain.tip is None:
            return None
        every = self.reader().rules().config.checkpoint
        link = max((l for l in chain.links if l.seq % every == 0), key=lambda l: l.seq)
        best = None
        for entry in self._replayer.log().entries():
            if entry.time <= link.linked_at:
                best = entry.seq
        return best

    # ------------------------------------------------------------ writing

    def budget(self) -> int:
        """§6 envelope budget for this member's transport and the host."""
        host = (self._info or self.info()).get("max_message_bytes")
        mine = self.send_via.capabilities().max_message_bytes if self.send_via is not None else host
        return envelope_budget(int(mine or host or 10 * 1024 * 1024), int(host) if host else None)

    def _address(self) -> str:
        if self.send_via is None:
            raise RemoteError("this client has no transport to send with")
        return self.send_via.address

    def _blob_envelopes(self, data: bytes) -> list[Envelope]:
        chunk = max(1, self.budget() - _BLOB_HEADROOM)
        sha = sha256_hex(data)
        n = max(1, -(-len(data) // chunk))
        out = []
        for i in range(n):
            env = E.build("blob", author=None, headers={
                "Gud-Blob": sha, "Gud-Size": str(len(data)), "Gud-Chunk": f"{i}/{n}",
                "Gud-Chunk-Size": str(chunk)}, records=(Record("data", (), data[i * chunk:(i + 1) * chunk]),))
            out.append(env)
        return out

    def _send_env(self, env: Envelope, summary: str, text: str, in_reply_to: Hex | None) -> Hex:
        gid = E.gud_id(env)
        data = E.encode(env)
        if len(data) > self.budget():
            raise RemoteError(f"envelope is {len(data)} bytes; the budget is {self.budget()} (§6)")
        reply, refs = None, ()
        if in_reply_to:
            target = self.reader().email(in_reply_to)
            if target is not None and target.message_id:
                reply, refs = target.message_id, (target.message_id,)
        self.send_via.send(Outgoing(  # type: ignore[union-attr]
            from_addr=self._address(), to=(self.repo_address,), subject=mime.subject_for(env, gid, summary),
            text=mime.render_human_part(env, gid, text), envelope=data, in_reply_to=reply, references=refs,
        ))
        return gid

    def send(self, env: Envelope, *, summary: str, text: str = "", in_reply_to: Hex | None = None,
             blobs: Sequence[bytes] = ()) -> Hex:
        """Send blobs (as `blob` messages) first, then the message; returns its gud id."""
        self._address()
        for data in blobs:
            for benv in self._blob_envelopes(data):
                self._send_env(benv, f"{(benv.get('Gud-Blob') or '')[:12]} {benv.get('Gud-Chunk')}", "", None)
        return self._send_env(env, summary, text, in_reply_to)

    def build_signed(self, kind: str, headers: Mapping[str, str], records: Sequence[Record] = (), *,
                     key: bytes | None = None, author: Handle | None = None, seen: bool = True) -> Envelope:
        """Build, check (§7) and sign one message as this member."""
        env = E.build(kind, author=author or self.handle, headers=dict(headers), records=tuple(records),
                      sent=E.now_sent(self.clock), seen=self.seen() if seen else None)
        problem = E.validate_kind(env)
        if problem:
            raise RemoteError(f"{kind}: {problem}")
        return E.sign(env, key if key is not None else self._key)

    def sign_and_send(self, kind: str, headers: Mapping[str, str], records: Sequence[Record] = (), *,
                      summary: str, in_reply_to: Hex | None = None, text: str = "",
                      blobs: Sequence[bytes] = ()) -> Hex:
        env = self.build_signed(kind, headers, records)
        return self.send(env, summary=summary, text=text, in_reply_to=in_reply_to, blobs=blobs)

    def set_delivery(self, delivery: Any) -> None:
        """§19.3 delivery preference: a `Delivery` (channel, url) or a §21 body mapping."""
        if isinstance(delivery, Mapping):
            body = dict(delivery)
        else:
            channel = getattr(delivery, "channel", None)
            body = {"webhook": delivery.url} if channel == "webhook" else {channel: True}
        if isinstance(self.mirror, LocalHost):
            self.mirror.delivery(body, self.handle)
        else:
            self.mirror.delivery(body)

    # ------------------------------------------------------------ ids

    def resolve(self, ref: str) -> Hex:
        """A full id, a hex prefix (>= 4) of any known id, a task alias (`T3`, `T3.1`), or `tip`; also
        the forms people paste from mail: a subject tag `[gud:<hex>]`, `gud:<hex>`, `<id>`, and a
        `<Message-ID>` of a logged message."""
        ref = ref.strip()
        tag = re.search(r"\[gud:([0-9a-fA-F]{4,64})\]", ref)
        if tag:
            ref = tag.group(1).lower()
        elif ref.startswith("<") and ref.endswith(">"):
            inner = ref[1:-1].strip()
            if "@" in inner:
                found = self._replayer.by_message_id(ref) or self._replayer.by_message_id(inner)
                if found:
                    return found
                inner = inner.split("@", 1)[0]
            ref = inner
        if ref.lower().startswith("gud:"):
            ref = ref[4:]
        chain = self.chain()
        if ref == "tip":
            if chain.tip is None:
                raise RemoteError("no tip yet")
            return chain.tip.state
        if E.is_hex64(ref):
            return ref
        for task in chain.tasks.values():
            if task.alias.lower() == ref.lower():
                return task.id
        low = ref.lower()
        if len(low) < 4 or any(c not in "0123456789abcdef" for c in low):
            raise RemoteError(f"cannot resolve {ref!r}")
        ids: set[Hex] = set()
        for link in chain.links:
            ids.update((link.state, link.push))
        for key, prop in chain.proposals.items():
            ids.update((key, prop.push))
            ids.update(o.id for o in prop.objections)
        ids.update(chain.tasks)
        ids.update(chain.claims)
        ids.update(c.latest for c in chain.claims.values())
        ids.update(chain.escalations)
        ids.update(gid for _, gid, _ in self._replayer.log().mail_ids())
        found = sorted(i for i in ids if i.startswith(low))
        if len(found) == 1:
            return found[0]
        if not found:
            raise RemoteError(f"no id starts with {ref!r}")
        raise RemoteError(f"{ref!r} is ambiguous ({len(found)} ids)")
