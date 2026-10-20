"""The host log (SPEC §11): signed, hash-chained entries; verification, fork proofs, handover."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Literal, Sequence

from .envelope import is_hex64, is_signature, is_public_key, public_of, sha256_hex, sign_bytes, verify_bytes
from .types import ZERO, Hex, LogEntry, Ms, Seen


class LogError(ValueError):
    pass


_FIELDS: dict[str, tuple[str, ...]] = {
    "open": ("repo", "key", "from"),
    "mail": ("id",),
    "tick": (),
    "handover": ("next",),
}
_UINT_RE = re.compile(r"0|[1-9][0-9]*")
_SEEN_RE = re.compile(r"(0|[1-9][0-9]*):([0-9a-f]{64})")


def entry_bytes(seq: int, prev: Hex, time: Ms, type: str, fields: Sequence[tuple[str, str]]) -> bytes:
    """The signed part of an entry: every line up to and including the LF before `sig`."""
    lines = ["gud-log 1", f"seq {seq}", f"prev {prev}", f"time {time}", f"type {type}"]
    lines += [f"{key} {value}" for key, value in fields]
    return ("\n".join(lines) + "\n").encode("utf-8")


def encode_entry(entry: LogEntry) -> bytes:
    return entry_bytes(entry.seq, entry.prev, entry.time, entry.type, entry.fields) + \
        f"sig {entry.sig}\n".encode("ascii")


def _check_fields(type: str, fields: Sequence[tuple[str, str]], seq: int) -> None:
    names = [k for k, _ in fields]
    allowed = _FIELDS.get(type)
    if allowed is None:
        raise LogError(f"unknown entry type {type!r}")
    if type == "open":
        if names not in (["repo", "key"], ["repo", "key", "from"]):
            raise LogError("open needs repo, key and optionally from")
        if (seq == 0) != (len(names) == 2):
            raise LogError("from is required on a continuing open and forbidden at seq 0")
    elif names != list(allowed):
        raise LogError(f"{type} fields must be {', '.join(allowed) or 'empty'}")
    for key, value in fields:
        if not value or any(ord(c) < 0x21 or ord(c) == 0x7F for c in value):
            raise LogError(f"bad {key} value")
        if key in ("key", "next") and not is_public_key(value):
            raise LogError(f"bad {key} public key")
        if key == "id" and not is_hex64(value):
            raise LogError("bad mail id")
        if key == "from" and not _SEEN_RE.fullmatch(value):
            raise LogError("bad from value")


def decode_entry(text: bytes) -> LogEntry:
    """Strict: exact line layout, canonical numbers, lowercase hex. Raises LogError."""
    try:
        lines = bytes(text).decode("utf-8").split("\n")
    except (UnicodeDecodeError, TypeError) as exc:
        raise LogError("entry is not UTF-8 text") from exc
    if len(lines) < 7 or lines[-1] != "" or lines[0] != "gud-log 1":
        raise LogError("bad entry framing")
    body = lines[1:-1]
    pairs = []
    for line in body:
        key, sep, value = line.partition(" ")
        if not sep:
            raise LogError(f"bad entry line {line!r}")
        pairs.append((key, value))
    if [k for k, _ in pairs[:4]] != ["seq", "prev", "time", "type"] or pairs[-1][0] != "sig":
        raise LogError("entry lines out of order")
    seq_s, prev, time_s, type = (v for _, v in pairs[:4])
    if not _UINT_RE.fullmatch(seq_s) or not _UINT_RE.fullmatch(time_s) or not (prev == ZERO or is_hex64(prev)):
        raise LogError("bad seq, prev or time")
    fields = tuple(pairs[4:-1])
    seq, time = int(seq_s), int(time_s)
    _check_fields(type, fields, seq)
    sig = pairs[-1][1]
    if not is_signature(sig):
        raise LogError("bad sig value")
    signed = entry_bytes(seq, prev, time, type, fields)
    entry = LogEntry(seq, prev, time, type, fields, sig, sha256_hex(signed))  # type: ignore[arg-type]
    if encode_entry(entry) != bytes(text):
        raise LogError("entry is not in canonical form")
    return entry


def _rehash(entry: LogEntry) -> Hex:
    return sha256_hex(entry_bytes(entry.seq, entry.prev, entry.time, entry.type, entry.fields))


@dataclass(frozen=True)
class ForkProof:
    a: LogEntry
    b: LogEntry

    def to_json(self) -> dict[str, str]:
        return {"a": encode_entry(self.a).decode("utf-8"), "b": encode_entry(self.b).decode("utf-8")}


class HostLog:
    """A verified log prefix. Append is host-only; extend/verify is what members do."""

    def __init__(self, entries: Sequence[LogEntry] = (), *, trusted_keys: Sequence[str] = ()) -> None:
        self._entries: list[LogEntry] = []
        self._keys: list[str] = []          # signing key of each entry
        self._next_key: str | None = None   # key current for the next entry
        self._repo: str | None = None
        self._mail: dict[Hex, int] = {}
        self._trusted = tuple(trusted_keys)
        if entries:
            self.extend(entries)

    # ------------------------------------------------------------ reading

    @property
    def head(self) -> LogEntry | None:
        return self._entries[-1] if self._entries else None

    @property
    def key(self) -> str:
        if self._next_key is None:
            raise LogError("empty log has no key")
        return self._next_key

    @property
    def repo(self) -> str | None:
        return self._repo

    def __len__(self) -> int:
        return len(self._entries)

    def at(self, seq: int) -> LogEntry:
        if not 0 <= seq < len(self._entries):
            raise LogError(f"no entry {seq}")
        return self._entries[seq]

    def key_at(self, seq: int) -> str:
        """The key that signed entry `seq` (for seq == len, the key the next entry must use)."""
        if seq == len(self._entries):
            return self.key
        if not 0 <= seq < len(self._entries):
            raise LogError(f"no entry {seq}")
        return self._keys[seq]

    def seq_of(self, gud_id: Hex) -> int | None:
        return self._mail.get(gud_id)

    def mail_ids(self, since: int = 0) -> Iterator[tuple[int, Hex, Ms]]:
        for entry in self._entries[max(since, 0):]:
            if entry.type == "mail":
                yield entry.seq, entry.fields[0][1], entry.time

    def entries(self, since: int = 0, limit: int | None = None) -> list[LogEntry]:
        end = None if limit is None else max(since, 0) + limit
        return self._entries[max(since, 0):end]

    # ------------------------------------------------------------ verification

    def _check_next(self, entry: LogEntry) -> str:
        """Verify `entry` as the next entry; return the key that signed it. Raises LogError."""
        n = len(self._entries)
        if entry.seq != n:
            raise LogError(f"entry {entry.seq}: expected seq {n}")
        _check_fields(entry.type, entry.fields, entry.seq)
        if entry.hash != _rehash(entry):
            raise LogError(f"entry {n}: hash does not match its contents")
        head = self.head
        if entry.prev != (head.hash if head else ZERO):
            raise LogError(f"entry {n}: prev does not match")
        if head is not None and entry.time <= head.time:
            raise LogError(f"entry {n}: time does not increase")
        if n == 0:
            if entry.type != "open":
                raise LogError("entry 0 must be open")
            key = entry.field("key") or ""
        elif entry.type == "open":
            key = entry.field("key") or ""
            if entry.field("repo") != self._repo:
                raise LogError(f"entry {n}: open names a different repo")
            if entry.field("from") != f"{head.seq}:{head.hash}":  # type: ignore[union-attr]
                raise LogError(f"entry {n}: open must cite the previous entry in from")
            planned = head.type == "handover" and head.field("next") == key  # type: ignore[union-attr]
            if not planned and key not in self._trusted:
                raise LogError(f"entry {n}: open with an untrusted key")
        else:
            if head.type == "handover":  # type: ignore[union-attr]
                raise LogError(f"entry {n}: a handover must be followed by open")
            key = self.key
        signed = entry_bytes(entry.seq, entry.prev, entry.time, entry.type, entry.fields)
        if not verify_bytes(key, signed, entry.sig):
            raise LogError(f"entry {n}: bad signature")
        return key

    def _append(self, entry: LogEntry, key: str) -> None:
        self._entries.append(entry)
        self._keys.append(key)
        if entry.type == "open":
            self._repo = entry.field("repo")
            self._next_key = key
        elif entry.type == "handover":
            self._next_key = entry.field("next")
        elif entry.type == "mail":
            self._mail.setdefault(entry.fields[0][1], entry.seq)

    def extend(self, entries: Sequence[LogEntry]) -> int:
        """Verify and append; entries already held must match exactly. Returns how many were new.

        On the first bad entry, the good ones before it stay appended and LogError is raised."""
        added = 0
        for entry in entries:
            if entry.seq < len(self._entries):
                if self._entries[entry.seq].hash != entry.hash or _rehash(entry) != entry.hash:
                    raise LogError(f"entry {entry.seq} differs from the one already verified")
                continue
            self._append(entry, self._check_next(entry))
            added += 1
        return added

    def verify(self) -> None:
        """Full re-verification from entry 0. Raises LogError."""
        HostLog(self._entries, trusted_keys=self._trusted)

    # ------------------------------------------------------------ host side

    def _sign(self, type: str, fields: Sequence[tuple[str, str]], time: Ms, private: bytes) -> LogEntry:
        head = self.head
        seq = len(self._entries)
        prev = head.hash if head else ZERO
        signed = entry_bytes(seq, prev, time, type, fields)
        entry = LogEntry(seq, prev, time, type, tuple(fields), sign_bytes(private, signed),  # type: ignore[arg-type]
                         sha256_hex(signed))
        self._append(entry, self._check_next(entry))
        return entry

    @classmethod
    def open(cls, repo: str, host_private: bytes, now: Ms) -> HostLog:
        log = cls()
        log._sign("open", (("repo", repo), ("key", public_of(host_private))), now, host_private)
        return log

    def _time_after_head(self, now: Ms) -> Ms:
        head = self.head
        if head is None:
            raise LogError("log is not open")
        if now <= head.time:
            raise LogError("clock is not after the last entry")
        return now

    def append_mail(self, gud_id: Hex, provider_time: Ms, host_private: bytes, now: Ms) -> LogEntry:
        """time = max(provider_time, head.time + 1), which must not exceed `now`."""
        head = self.head
        if head is None:
            raise LogError("log is not open")
        if gud_id in self._mail:
            raise LogError(f"{gud_id[:12]} is already logged")
        time = max(int(provider_time), head.time + 1)
        if time > now:
            raise LogError("received time is ahead of the host clock")
        return self._sign("mail", (("id", gud_id),), time, host_private)

    def append_tick(self, host_private: bytes, now: Ms) -> LogEntry:
        return self._sign("tick", (), self._time_after_head(now), host_private)

    def handover(self, old_private: bytes, new_public: str, now: Ms) -> LogEntry:
        return self._sign("handover", (("next", new_public),), self._time_after_head(now), old_private)

    def continue_after_handover(self, new_private: bytes, now: Ms) -> LogEntry:
        """The new host's first entry: open citing the previous entry (planned, or a trusted key)."""
        head = self.head
        if head is None or self._repo is None:
            raise LogError("log is not open")
        fields = (("repo", self._repo), ("key", public_of(new_private)), ("from", f"{head.seq}:{head.hash}"))
        return self._sign("open", fields, self._time_after_head(now), new_private)

    # ------------------------------------------------------------ members

    def check_seen(self, seen: Seen, own_seq: int) -> Literal["ok", "future", "mismatch", "unknown"]:
        """§9 Gud-Seen check for a message logged at `own_seq`."""
        if seen.seq >= own_seq:
            return "future"
        if seen.seq >= len(self._entries):
            return "unknown"
        return "ok" if self._entries[seen.seq].hash == seen.entry_hash else "mismatch"


def fork_proof(a: LogEntry, b: LogEntry, key: str) -> ForkProof | None:
    """Two validly signed entries with the same seq and different hashes, else None."""
    if a.seq != b.seq:
        return None
    for entry in (a, b):
        if _rehash(entry) != entry.hash:
            return None
        signed = entry_bytes(entry.seq, entry.prev, entry.time, entry.type, entry.fields)
        if not verify_bytes(key, signed, entry.sig):
            return None
    if a.hash == b.hash:
        return None
    first, second = sorted((a, b), key=lambda e: e.hash)
    return ForkProof(first, second)


def replay_time(log: HostLog, now: Ms, first_missing_seq: int | None) -> Ms:
    """§11.3: min(now, last log time), and before the first entry whose mail is missing."""
    head = log.head
    t = now if head is None else min(now, head.time)
    if first_missing_seq is not None:
        t = min(t, log.at(first_missing_seq).time - 1)
    return t
