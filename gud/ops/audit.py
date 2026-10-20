"""`gud audit` v1 (PLAN Phase 0c/6, SPEC §11.4, §11.5): check the host from one member's view.

The audit fetches the log and mail the mirror serves now and checks:

1. **The log** verifies (§11.4): format, seq, prev, strictly increasing time, every signature
   under the key current at that entry, handovers.
2. **Fork proofs** (§11.5): every entry the member retained earlier (its local verified log,
   plus entries other members hand over as evidence) must be the served entry at that seq. Two
   validly signed entries for one seq are a `ForkProof`; a served log shorter than what the
   member verified, or a difference that is not provable, fails the log check.
3. **Gud-Seen**: every signed message the member observed (served mail and its own inbox
   copies) must cite an entry on the served log with the same hash, at a seq before its own.
4. **Mail**: every logged id is served (`missing_mail`) and its bytes give the logged id
   (`altered`).
5. **Inbox copies** (mail addressed to the repo that also reached the member, e.g. by Cc): a
   copy whose id is logged must have a log time within `skew` of the member's own received time
   (`retimed`); a copy the log lacks, when the log has moved on by more than `skew`, is
   `withheld`; a served message with the same Message-ID but different content is `altered`.
6. **Late votes**: `approve`/`object`/`hold` logged more than `grace` after their `Gud-Sent`
   (reported, not a failure: §13.4 already discounts them; a pattern of them points at a host
   holding votes back).

`AuditReport.exit_status` is the CLI's exit code: 0 clean, 1 discrepancies (missing, altered,
retimed or withheld mail, Gud-Seen mismatches), 2 the host provably misbehaved or its log does
not verify (fork proof, bad or truncated log).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from gud.core import envelope as E
from gud.core.config import RepoConfig, bare_address
from gud.core.hostlog import ForkProof, HostLog, LogError, entry_bytes, fork_proof
from gud.core.types import SIGNED_KINDS, ZERO, Email, Hex, LogEntry, Ms, Seen
from gud.transport import mime
from gud.transport.base import Received

from .archive import read_log, read_mail

VOTE_KINDS = frozenset({"approve", "object", "hold"})


@dataclass(frozen=True)
class AuditReport:
    log_ok: bool
    log_error: str | None
    seen_mismatches: tuple[tuple[Hex, Seen, Hex], ...]   # (message, cited, served hash or ZERO if absent)
    fork_proofs: tuple[ForkProof, ...]
    late_votes: tuple[tuple[Hex, Ms], ...]               # (vote, received − sent) beyond G
    missing_mail: tuple[Hex, ...]                        # logged ids the mirror does not serve
    altered: tuple[tuple[Hex, str], ...] = ()            # (gud id, why)
    retimed: tuple[tuple[Hex, Ms, Ms], ...] = ()         # (gud id, own received time, log time)
    withheld: tuple[Hex, ...] = ()                       # inbox copies the served log lacks

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    @property
    def exit_status(self) -> int:
        if self.fork_proofs or not self.log_ok:
            return 2
        if self.seen_mismatches or self.missing_mail or self.altered or self.retimed or self.withheld:
            return 1
        return 0

    def to_json(self) -> dict:
        """JSON-compatible form (also the `gud audit --evidence` file)."""
        return {
            "ok": self.ok, "exit_status": self.exit_status, "log_ok": self.log_ok, "log_error": self.log_error,
            "seen_mismatches": [{"message": m, "cited": s.render(), "log_hash": h}
                                for m, s, h in self.seen_mismatches],
            "fork_proofs": [p.to_json() for p in self.fork_proofs],
            "late_votes": [{"message": m, "late_ms": d} for m, d in self.late_votes],
            "missing_mail": list(self.missing_mail),
            "altered": [{"message": m, "why": w} for m, w in self.altered],
            "retimed": [{"message": m, "received": r, "logged": t} for m, r, t in self.retimed],
            "withheld": list(self.withheld),
        }


def _signed_with(entry: LogEntry, key: str | None) -> bool:
    signed = entry_bytes(entry.seq, entry.prev, entry.time, entry.type, entry.fields)
    return E.sha256_hex(signed) == entry.hash and E.verify_bytes(key, signed, entry.sig)


def _prove(a: LogEntry, a_key: str | None, b: LogEntry, b_key: str | None) -> ForkProof | None:
    """§11.5: same seq, different hashes, each signed by the key current at that seq in its log."""
    if a_key is not None and a_key == b_key:
        return fork_proof(a, b, a_key)
    if a.seq != b.seq or a.hash == b.hash or not _signed_with(a, a_key) or not _signed_with(b, b_key):
        return None
    first, second = sorted((a, b), key=lambda e: e.hash)
    return ForkProof(first, second)


def _lenient_log(entries: Iterable[LogEntry], trusted: Sequence[str]) -> HostLog:
    """The longest verifiable prefix of `entries` (sorted by seq, deduplicated)."""
    log = HostLog(trusted_keys=trusted)
    for entry in sorted({e.hash: e for e in entries}.values(), key=lambda e: e.seq):
        try:
            log.extend([entry])
        except LogError:
            break
    return log


def _config_of(source) -> RepoConfig:
    reader = getattr(source, "reader", None)
    if callable(reader):
        try:
            return reader().rules().config
        except Exception:  # no chain yet: defaults
            pass
    return RepoConfig()


def _inbox_item(item: Received | bytes | tuple[bytes, Ms]) -> tuple[bytes, Ms | None]:
    if isinstance(item, Received):
        return item.raw_mime, item.received_at
    if isinstance(item, (bytes, bytearray)):
        return bytes(item), None
    raw, when = item
    return bytes(raw), when


def audit(mirror, *, retained: Sequence[LogEntry] | None = None,
          inbox: Iterable[Received | bytes | tuple[bytes, Ms]] = (), trusted_keys: Sequence[str] = (),
          config: RepoConfig | None = None) -> AuditReport:
    """Audit the host behind `mirror` (anything with `log_entries(since, limit)` and
    `raw(gud_id)`, e.g. a member's mirror client). `retained`: log entries the member (or other
    members, as evidence) verified before; defaults to the client's local verified log when the
    mirror has a `replayer()`. `inbox`: the member's own copies of repo mail. `config` gives
    `skew` and `grace` (default: the client's tip config, else §10.5 defaults). Never raises on
    host misbehaviour; it is all in the report."""
    cfg = config or _config_of(mirror)
    if retained is None:
        retained = ()
        replayer = getattr(mirror, "replayer", None)
        if replayer is not None:
            retained = list((replayer() if callable(replayer) else replayer).log().entries())
    served = read_log(mirror)
    log = HostLog(trusted_keys=trusted_keys)
    errors: list[str] = []
    try:
        log.extend(served)
    except LogError as exc:
        errors.append(f"served log does not verify: {exc}")

    # 2. retained entries against the served log
    proofs: list[ForkProof] = []
    if retained:
        own = _lenient_log(retained, trusted_keys)
        own_keys = {own.at(n).hash: own.key_at(n) for n in range(len(own))}
        by_seq = {e.seq: e for e in sorted(retained, key=lambda e: e.seq)}
        for seq in sorted(by_seq):
            mine = by_seq[seq]
            if seq >= len(served):
                errors.append(f"served log has {len(served)} entries, but this member verified entry {seq}")
                break
            theirs = served[seq]
            if theirs.hash == mine.hash:
                continue
            fallback = log.key_at(seq) if seq <= len(log) and len(log) else None
            mine_key = own_keys.get(mine.hash, fallback)
            theirs_key = log.key_at(seq) if seq < len(log) else None
            proof = _prove(mine, mine_key, theirs, theirs_key)
            if proof is not None:
                proofs.append(proof)
            else:
                errors.append(f"served entry {seq} differs from the retained copy (signatures do not prove a fork)")
            break      # later entries chain from the divergent one

    # 3-6. mail
    seen_bad: dict[Hex, tuple[Hex, Seen, Hex]] = {}
    late: list[tuple[Hex, Ms]] = []
    missing: list[Hex] = []
    altered: dict[Hex, str] = {}
    served_mail: dict[Hex, Email] = {}
    by_message_id: dict[str, Hex] = {}

    def check_seen(email: Email, own_seq: int | None) -> None:
        env = email.envelope
        if env is None or env.kind not in SIGNED_KINDS or env.kind == "join" or email.gud_id in seen_bad:
            return
        try:
            seen = E.parse_seen(env.get("Gud-Seen") or "")
        except E.EnvelopeError:
            return                                   # void on its own; nothing to say about the host
        known = seen.seq < len(log)
        log_hash = log.at(seen.seq).hash if known else ZERO
        if (own_seq is not None and seen.seq >= own_seq) or not known or log_hash != seen.entry_hash:
            seen_bad[email.gud_id] = (email.gud_id, seen, log_hash)

    first: dict[Hex, int] = {}
    for seq, gid, when in log.mail_ids():
        if gid in first:
            continue
        first[gid] = seq
        raw = read_mail(mirror, gid)
        if raw is None:
            missing.append(gid)
            continue
        email = mime.to_email(raw)
        if email is None or email.gud_id != gid:
            altered[gid] = "the served bytes do not give the logged id"
            continue
        served_mail[gid] = email
        if email.message_id:
            by_message_id.setdefault(email.message_id, gid)
        check_seen(email, seq)
        env = email.envelope
        if env is not None and env.kind in VOTE_KINDS and env.sent_ms is not None and when - env.sent_ms > cfg.grace:
            late.append((gid, when - env.sent_ms))

    repos = {bare_address(r) for r in log.repos}       # a handover may have moved the address
    head_time = log.head.time if log.head is not None else 0
    retimed: list[tuple[Hex, Ms, Ms]] = []
    withheld: list[Hex] = []
    done: set[Hex] = set()
    for item in inbox:
        raw, received = _inbox_item(item)
        email = mime.to_email(raw)
        if email is None or email.gud_id in done:
            continue
        if repos and not repos & {bare_address(a) for a in (*email.to, *email.cc)}:
            continue
        done.add(email.gud_id)
        gid = email.gud_id
        seq = first.get(gid)
        if seq is None and email.message_id in by_message_id:
            other = served_mail[by_message_id[email.message_id]]
            if email.envelope is None and other.envelope is None and other.text == email.text:
                gid, seq = other.gud_id, first[other.gud_id]   # same unsigned mail, other headers
            else:
                altered.setdefault(gid, "the mirror holds a different message with this Message-ID")
                continue
        check_seen(email, seq)
        if seq is None:
            if received is None or head_time > received + cfg.skew:
                withheld.append(gid)
            continue
        logged = log.at(seq).time
        if received is not None and abs(logged - received) > cfg.skew:
            retimed.append((gid, received, logged))

    return AuditReport(
        log_ok=not errors, log_error="; ".join(errors) or None,
        seen_mismatches=tuple(seen_bad.values()), fork_proofs=tuple(proofs), late_votes=tuple(late),
        missing_mail=tuple(missing), altered=tuple(altered.items()), retimed=tuple(retimed),
        withheld=tuple(withheld),
    )
