"""The v1 envelope (SPEC §3): strict codec, ids, Ed25519 signing, kind validation.

Also home to two helpers replay needs that sit below `transport` and `events` in the
dependency graph: the RFC 8601 Authentication-Results parser (§8.1) and event ids (§19.1).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from email.utils import parseaddr
from typing import Any, Callable, Iterable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .types import (
    EVENT_TYPES, KINDS, MODES, REGULAR_MODES, ROLES, SIGNED_KINDS, Envelope, Event, Handle, Hex, Ms,
    Record, Seen, _split_list,
)

VERSION: str = "1"
SIG_PREFIX: str = "ed25519:"


class EnvelopeError(ValueError):
    pass


_NAME_RE = re.compile(r"Gud(?:-[A-Z][a-z0-9]*)+")
_RNAME_RE = re.compile(rb"[a-z][a-z0-9-]*")
_LENGTH_RE = re.compile(rb"0|[1-9][0-9]*")
_ARG_RE = re.compile(rb"[\x21-\x7e]+")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_UINT_RE = re.compile(r"0|[1-9][0-9]*")
_HANDLE_RE = re.compile(r"@[a-z0-9][a-z0-9._-]{0,63}")
_PCT_HEX = frozenset("0123456789ABCDEF")
_PATH_ARGS: dict[str, tuple[int, ...]] = {
    "patch": (0,), "put": (0,), "delete": (0,), "rename": (0, 1), "mode": (0,), "evidence": (0,),
}


def sha256_hex(data: bytes) -> Hex:
    return hashlib.sha256(data).hexdigest()


def is_hex64(value: str | None) -> bool:
    return isinstance(value, str) and _HEX64_RE.fullmatch(value) is not None


def is_handle(value: str | None) -> bool:
    return isinstance(value, str) and _HANDLE_RE.fullmatch(value) is not None


# ---------------------------------------------------------------- names, paths, lists

def canonical_name(name: str) -> str:
    """`gud-parent-task` (or `parent-task`) -> `Gud-Parent-Task`. Raises EnvelopeError."""
    parts = name.strip().split("-")
    if parts and parts[0].lower() == "gud":
        parts = parts[1:]
    out = "Gud-" + "-".join(part[:1].upper() + part[1:].lower() for part in parts)
    if not _NAME_RE.fullmatch(out):
        raise EnvelopeError(f"bad header name {name!r}")
    return out


def encode_path(path: str) -> str:
    """§3.2: every byte outside %x21-7E, and `%`, as `%XX` (uppercase)."""
    try:
        raw = path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EnvelopeError(f"path is not valid UTF-8: {path!r}") from exc
    if not raw:
        raise EnvelopeError("empty path")
    return "".join(chr(b) if 0x21 <= b <= 0x7E and b != 0x25 else f"%{b:02X}" for b in raw)


def decode_path(arg: str) -> str:
    """Strict inverse of encode_path. Raises EnvelopeError."""
    out = bytearray()
    i = 0
    while i < len(arg):
        ch = arg[i]
        code = ord(ch)
        if not 0x21 <= code <= 0x7E:
            raise EnvelopeError(f"unencoded byte in path {arg!r}")
        if ch != "%":
            out.append(code)
            i += 1
            continue
        pair = arg[i + 1:i + 3]
        if len(pair) != 2 or not set(pair) <= _PCT_HEX:
            raise EnvelopeError(f"bad percent-encoding in {arg!r}")
        value = int(pair, 16)
        if 0x21 <= value <= 0x7E and value != 0x25:
            raise EnvelopeError(f"needless percent-encoding in {arg!r}")
        out.append(value)
        i += 3
    if not out:
        raise EnvelopeError("empty path")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvelopeError(f"path is not valid UTF-8: {arg!r}") from exc


def render_list(items: Sequence[str]) -> str:
    """§0 list form; `%` and `,` inside items are percent-encoded."""
    return ",".join(item.replace("%", "%25").replace(",", "%2C") for item in items)


def parse_list(value: str | None) -> tuple[str, ...]:
    return _split_list(value)


# ---------------------------------------------------------------- codec

def _check_value(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"{name}: empty value")
    if value[0] == " " or value[-1] == " ":
        raise EnvelopeError(f"{name}: leading or trailing space")
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise EnvelopeError(f"{name}: forbidden control byte")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EnvelopeError(f"{name}: not valid UTF-8") from exc


def _check_sig(sig: str) -> None:
    if not isinstance(sig, str) or not sig.startswith(SIG_PREFIX) or _b64(sig[len(SIG_PREFIX):], 64) is None:
        raise EnvelopeError("bad Gud-Sig")


def _b64(text: str, size: int) -> bytes | None:
    """Canonical padded standard base64 of exactly `size` bytes, else None."""
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    if len(raw) != size or base64.b64encode(raw).decode("ascii") != text:
        return None
    return raw


def _check_headers(headers: Sequence[tuple[str, str]]) -> None:
    prev = None
    for name, value in headers:
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise EnvelopeError(f"bad header name {name!r}")
        if name == "Gud-Sig":
            raise EnvelopeError("Gud-Sig is not a header line")
        if prev is not None and name <= prev:
            raise EnvelopeError(f"headers unsorted or duplicated at {name}")
        _check_value(name, value)
        prev = name
    if dict(headers).get("Gud-Version") != VERSION:
        raise EnvelopeError("missing Gud-Version: 1")


def _record_line(record: Record) -> bytes:
    if not isinstance(record.name, str) or not _RNAME_RE.fullmatch(record.name.encode("ascii", "replace")):
        raise EnvelopeError(f"bad record name {record.name!r}")
    if not isinstance(record.data, (bytes, bytearray)):
        raise EnvelopeError("record data must be bytes")
    paths = _PATH_ARGS.get(record.name, ())
    parts = [record.name.encode("ascii"), str(len(record.data)).encode("ascii")]
    for i, arg in enumerate(record.args):
        text = encode_path(arg) if i in paths else arg
        raw = text.encode("utf-8", "replace") if isinstance(text, str) else b""
        if not _ARG_RE.fullmatch(raw):
            raise EnvelopeError(f"bad record argument {arg!r}")
        parts.append(raw)
    return b" ".join(parts) + b"\n"


def encode(env: Envelope) -> bytes:
    """Exact §3.1 bytes; the sig line (if any) is the last header line."""
    out = bytearray(_encode_unsigned_headers(env))
    if env.sig is not None:
        _check_sig(env.sig)
        out += b"Gud-Sig: " + env.sig.encode("ascii") + b"\n"
    out += b"\n"
    out += _encode_payload(env)
    return bytes(out)


def _encode_unsigned_headers(env: Envelope) -> bytes:
    _check_headers(env.headers)
    return b"".join(f"{name}: {value}\n".encode("utf-8") for name, value in env.headers)


def _encode_payload(env: Envelope) -> bytes:
    out = bytearray()
    for record in env.records:
        out += _record_line(record)
        out += bytes(record.data)
        out += b"\n"
    return bytes(out)


def signing_bytes(env: Envelope) -> bytes:
    return _encode_unsigned_headers(env) + b"\n" + _encode_payload(env)


def decode(data: bytes) -> Envelope:
    """Strict §3.1 decoder. Raises EnvelopeError on any deviation."""
    if not isinstance(data, (bytes, bytearray)):
        raise EnvelopeError("envelope must be bytes")
    data = bytes(data)
    pos = 0
    headers: list[tuple[str, str]] = []
    sig: str | None = None
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise EnvelopeError("missing empty line after headers")
        line = data[pos:nl]
        pos = nl + 1
        if not line:
            break
        if sig is not None:
            raise EnvelopeError("Gud-Sig is not the last header line")
        colon = line.find(b":")
        if colon < 0 or line[colon + 1:colon + 2] != b" ":
            raise EnvelopeError("bad header line")
        try:
            name = line[:colon].decode("ascii")
            value = line[colon + 2:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvelopeError("header is not valid UTF-8") from exc
        if not _NAME_RE.fullmatch(name):
            raise EnvelopeError(f"bad header name {name!r}")
        _check_value(name, value)
        if name == "Gud-Sig":
            _check_sig(value)
            sig = value
            continue
        if headers and name <= headers[-1][0]:
            raise EnvelopeError(f"headers unsorted or duplicated at {name}")
        headers.append((name, value))
    if dict(headers).get("Gud-Version") != VERSION:
        raise EnvelopeError("missing Gud-Version: 1")
    records: list[Record] = []
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise EnvelopeError("trailing bytes after the last record")
        parts = data[pos:nl].split(b" ")
        if len(parts) < 2 or not _RNAME_RE.fullmatch(parts[0]) or not _LENGTH_RE.fullmatch(parts[1]):
            raise EnvelopeError("bad record line")
        for arg in parts[2:]:
            if not _ARG_RE.fullmatch(arg):
                raise EnvelopeError("bad record argument")
        name = parts[0].decode("ascii")
        start = nl + 1
        end = start + int(parts[1])
        if end >= len(data) or data[end:end + 1] != b"\n":
            raise EnvelopeError(f"record {name} data not followed by LF")
        paths = _PATH_ARGS.get(name, ())
        args = tuple(decode_path(arg.decode("ascii")) if i in paths else arg.decode("ascii")
                     for i, arg in enumerate(parts[2:]))
        records.append(Record(name, args, data[start:end]))
        pos = end + 1
    return Envelope(tuple(headers), tuple(records), sig)


def gud_id(env: Envelope) -> Hex:
    return sha256_hex(signing_bytes(env))


def unsigned_id(raw_mime: bytes) -> Hex:
    return sha256_hex(raw_mime)


def build(kind: str, *, author: Handle | None, headers: Mapping[str, str],
          records: Sequence[Record] = (), sent: int | None = None,
          seen: Seen | None = None) -> Envelope:
    """Adds Version/Kind/Author/Sent/Seen, canonicalises and sorts headers, checks the grammar.

    Kind-level rules (§7) are checked by `validate_kind`, not here."""
    merged: dict[str, str] = {"Gud-Version": VERSION, "Gud-Kind": kind}
    if author is not None:
        merged["Gud-Author"] = author
    if sent is not None:
        merged["Gud-Sent"] = str(int(sent))
    if seen is not None:
        merged["Gud-Seen"] = seen.render()
    for name, value in headers.items():
        canon = canonical_name(name)
        if canon == "Gud-Sig":
            raise EnvelopeError("Gud-Sig is set by sign()")
        if canon in merged and merged[canon] != value:
            raise EnvelopeError(f"header {canon} given twice")
        merged[canon] = value
    env = Envelope(tuple(sorted(merged.items())), tuple(records), None)
    encode(env)
    return env


# ---------------------------------------------------------------- signatures

def generate_key() -> tuple[bytes, str]:
    """(private raw 32 bytes, "ed25519:<b64 pub>")."""
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    return raw, public_of(raw)


def public_of(private_raw: bytes) -> str:
    pub = Ed25519PrivateKey.from_private_bytes(private_raw).public_key()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return SIG_PREFIX + base64.b64encode(raw).decode("ascii")


def is_public_key(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(SIG_PREFIX) and _b64(value[len(SIG_PREFIX):], 32) is not None


def is_signature(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(SIG_PREFIX) and _b64(value[len(SIG_PREFIX):], 64) is not None


def sign_bytes(private_raw: bytes, payload: bytes) -> str:
    signature = Ed25519PrivateKey.from_private_bytes(private_raw).sign(payload)
    return SIG_PREFIX + base64.b64encode(signature).decode("ascii")


def verify_bytes(public_key: str | None, payload: bytes, signature: str | None) -> bool:
    """Never raises."""
    try:
        if not isinstance(public_key, str) or not isinstance(signature, str):
            return False
        if not public_key.startswith(SIG_PREFIX) or not signature.startswith(SIG_PREFIX):
            return False
        pub = _b64(public_key[len(SIG_PREFIX):], 32)
        sig = _b64(signature[len(SIG_PREFIX):], 64)
        if pub is None or sig is None:
            return False
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, bytes(payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sign(env: Envelope, private_raw: bytes) -> Envelope:
    return replace(env, sig=sign_bytes(private_raw, signing_bytes(env)))


def verify(env: Envelope, public_key: str | None) -> bool:
    """Never raises."""
    try:
        return verify_bytes(public_key, signing_bytes(env), env.sig)
    except Exception:
        return False


# ---------------------------------------------------------------- small helpers

def parse_seen(value: str) -> Seen:
    """`<seq>:<entry hash>`. Raises EnvelopeError."""
    seq, sep, entry = (value or "").partition(":")
    if not sep or not _UINT_RE.fullmatch(seq) or not is_hex64(entry):
        raise EnvelopeError(f"bad seen value {value!r}")
    return Seen(int(seq), entry)


def now_sent(clock: Callable[[], float]) -> int:
    return int(math.floor(clock()))


def subject_tag(gud_id: Hex) -> str:
    return f"[gud:{gud_id[:12]}]"


def jcs(value: Any) -> bytes:
    """RFC 8785 canonical JSON. Integers only (|n| < 2**53); floats are rejected."""
    return _jcs(value).encode("utf-8")


def _jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) >= 2 ** 53:
            raise ValueError("integer out of JCS range")
        return str(value)
    if isinstance(value, float):
        raise ValueError("floats are not allowed in canonical JSON")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("lone surrogate in string") from exc
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        keys = list(value)
        for key in keys:
            if not isinstance(key, str):
                raise ValueError("object keys must be strings")
        keys.sort(key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(_jcs(k) + ":" + _jcs(value[k]) for k in keys) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    raise ValueError(f"not JSON-serialisable: {type(value).__name__}")


# ---------------------------------------------------------------- §7 kind rules

_OPS = frozenset({"patch", "put", "delete", "rename", "mode"})
_REQUIRED: dict[str, tuple[str, ...]] = {
    "push": ("Gud-Parent", "Gud-Tree"),
    "revert": ("Gud-Parent", "Gud-Revert", "Gud-Tree"),
    "object": ("Gud-Target",),
    "approve": ("Gud-Target",),
    "hold": ("Gud-Target",),
    "comment": ("Gud-Target",),
    "test": ("Gud-Scope", "Gud-Target", "Gud-Tree", "Gud-Result"),
    "claim": ("Gud-Task", "Gud-Files"),
    "task": ("Gud-Title",),
    "subtask": ("Gud-Parent-Task", "Gud-Title"),
    "no-change": ("Gud-Task", "Gud-Tip"),
    "checkpoint": ("Gud-Log", "Gud-Link", "Gud-Tree", "Gud-Snapshot"),
    "join": ("Gud-Role", "Gud-Address", "Gud-Key"),
    "notify": ("Gud-Event", "Gud-Event-Type"),
    "blob": ("Gud-Blob", "Gud-Size", "Gud-Chunk", "Gud-Chunk-Size"),
}
_OPTIONAL: dict[str, tuple[str, ...]] = {
    "push": ("Gud-Task", "Gud-Revises", "Gud-Carry", "Gud-Join"),
    "revert": ("Gud-Task",),
    "comment": ("Gud-Task", "Gud-Escalate", "Gud-Verdict"),
    "test": ("Gud-Stack", "Gud-On", "Gud-Base-Result", "Gud-Link"),
    "claim": ("Gud-Revises", "Gud-Release", "Gud-Done", "Gud-After"),
    "task": ("Gud-Requester", "Gud-Accept", "Gud-Bug"),
    "subtask": ("Gud-After", "Gud-Accept", "Gud-Files"),
    "join": ("Gud-Sponsor",),
    "notify": ("Gud-Target", "Gud-Task"),
}
# (record name, min count, max count) in required order; None = the push op group.
_RECORDS: dict[str, tuple[tuple[str, int, int], ...]] = {
    "push": (("text", 0, 1), ("@ops", 1, 1 << 62)),
    "revert": (("text", 0, 1),),
    "object": (("text", 1, 1), ("evidence", 0, 1)),
    "approve": (("text", 0, 1),),
    "hold": (("text", 0, 1),),
    "comment": (("text", 1, 1),),
    "test": (("text", 0, 1),),
    "claim": (("text", 0, 1),),
    "task": (("text", 0, 1), ("accept", 0, 1)),
    "subtask": (("text", 0, 1), ("accept", 0, 1)),
    "no-change": (("text", 1, 1),),
    "checkpoint": (),
    "join": (("text", 0, 1),),
    "notify": (("text", 0, 1),),
    "blob": (("data", 1, 1),),
}
_STRICT_RECORDS = frozenset({"push", "revert"})
_ID_HEADERS = frozenset({
    "Gud-Parent", "Gud-Target", "Gud-Revises", "Gud-Task", "Gud-Parent-Task", "Gud-Revert", "Gud-On",
    "Gud-Join", "Gud-Bug", "Gud-Tip", "Gud-Link", "Gud-Tree", "Gud-Snapshot", "Gud-Blob", "Gud-Event",
})
_ENUMS: dict[str, frozenset[str]] = {
    "Gud-Carry": frozenset({"none"}),
    "Gud-Scope": frozenset({"push", "stack", "tip", "accept", "evidence"}),
    "Gud-Result": frozenset({"pass", "fail"}),
    "Gud-Base-Result": frozenset({"pass", "fail"}),
    "Gud-Release": frozenset({"yes"}),
    "Gud-Done": frozenset({"yes"}),
    "Gud-Escalate": frozenset({"budget", "stuck"}),
    "Gud-Verdict": frozenset({"reject"}),
    "Gud-Role": ROLES,
    "Gud-Event-Type": frozenset(EVENT_TYPES),
}


def _check_header_value(name: str, value: str) -> str | None:
    if name in _ID_HEADERS:
        return None if is_hex64(value) else f"{name} is not a 64-hex id"
    if name in ("Gud-After", "Gud-Stack"):
        items = _split_list(value)
        if not items or not all(is_hex64(item) for item in items):
            return f"{name} is not a list of ids"
        return None
    if name in ("Gud-Files", "Gud-Accept"):
        return None if _split_list(value) else f"{name} is empty"
    if name in _ENUMS:
        return None if value in _ENUMS[name] else f"bad {name} value {value!r}"
    if name in ("Gud-Author", "Gud-Requester", "Gud-Sponsor"):
        return None if is_handle(value) else f"{name} is not a handle"
    if name == "Gud-Sent":
        return None if _UINT_RE.fullmatch(value) else "bad Gud-Sent"
    if name in ("Gud-Seen", "Gud-Log"):
        try:
            parse_seen(value)
        except EnvelopeError:
            return f"bad {name}"
        return None
    if name == "Gud-Key":
        return None if is_public_key(value) else "bad Gud-Key"
    if name == "Gud-Address":
        addr = parseaddr(value)[1]
        local, at, domain = addr.rpartition("@")
        return None if at and local and domain else "bad Gud-Address"
    if name in ("Gud-Size", "Gud-Chunk-Size"):
        return None if _UINT_RE.fullmatch(value) and int(value) > 0 else f"bad {name}"
    if name == "Gud-Chunk":
        i, sep, n = value.partition("/")
        if sep and _UINT_RE.fullmatch(i) and _UINT_RE.fullmatch(n) and int(i) < int(n):
            return None
        return "bad Gud-Chunk"
    return None


def _check_sized(record: Record, size_arg: str, sha_arg: str) -> str | None:
    if not is_hex64(sha_arg):
        return f"{record.name}: bad sha256"
    if not _UINT_RE.fullmatch(size_arg):
        return f"{record.name}: bad size"
    size = int(size_arg)
    if len(record.data) == size or (len(record.data) == 0 and size > 0):
        return None
    return f"{record.name}: length is neither the size nor a blob reference"


def _check_record(record: Record, kind: str) -> str | None:
    name, args, data = record.name, record.args, record.data
    arity = {"text": 0, "accept": 0, "data": 0, "patch": 1, "put": 4, "delete": 1, "rename": 2,
             "mode": 2, "evidence": 3}[name]
    if len(args) != arity:
        return f"{name}: expected {arity} arguments"
    if name in ("text", "accept"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"{name} is not valid UTF-8"
        if name == "text" and kind in ("object", "comment", "no-change") and not text:
            return "text must not be empty"
        if name == "text" and kind == "no-change" and ("\n" in text or "\r" in text):
            return "no-change reason must be one line"
        if name == "text" and kind == "test" and len(data) > 65536:
            return "test log tail exceeds 65536 bytes"
    elif name == "patch":
        if not data:
            return "patch: empty data"
    elif name == "put":
        if args[1] not in MODES:
            return "put: bad mode"
        return _check_sized(record, args[3], args[2])
    elif name == "evidence":
        return _check_sized(record, args[2], args[1])
    elif name in ("delete", "rename"):
        if data:
            return f"{name}: data must be empty"
    elif name == "mode":
        if args[1] not in REGULAR_MODES:
            return "mode: bad mode"
        if data:
            return "mode: data must be empty"
    return None


def validate_kind(env: Envelope) -> str | None:
    """§3.4/§7 required headers, header formats and record order. None if ok, else a reason."""
    if env.get("Gud-Version") != VERSION:
        return "missing Gud-Version: 1"
    kind = env.kind
    if kind not in KINDS:
        return f"unknown kind {kind!r}"
    required = list(_REQUIRED[kind])
    if kind in SIGNED_KINDS:
        required += ["Gud-Author", "Gud-Sent"]
        if kind != "join":
            required.append("Gud-Seen")
    for name in required:
        if env.get(name) is None:
            return f"missing {name}"
    checked = set(required) | set(_OPTIONAL.get(kind, ())) | {"Gud-Author", "Gud-Sent", "Gud-Seen"}
    for name, value in env.headers:
        if name in checked:
            reason = _check_header_value(name, value)
            if reason:
                return reason
    if kind == "test":
        scope = env.get("Gud-Scope")
        need = {"stack": "Gud-Stack", "accept": "Gud-Link", "evidence": "Gud-On"}.get(scope or "")
        if need and env.get(need) is None:
            return f"scope {scope} needs {need}"
    pattern = _RECORDS[kind]
    allowed = {name for name, _, _ in pattern} | (_OPS if kind == "push" else set())
    records = list(env.records)
    if kind in _STRICT_RECORDS:
        for record in records:
            if record.name not in allowed:
                return f"record {record.name} not allowed in {kind}"
    else:
        records = [r for r in records if r.name in allowed]
    i = 0
    for name, low, high in pattern:
        count = 0
        while i < len(records) and (records[i].name in _OPS if name == "@ops" else records[i].name == name):
            count += 1
            i += 1
        if count < low or count > high:
            return f"{kind}: wrong number of {name.lstrip('@')} records"
    if i != len(records):
        return f"{kind}: records out of order"
    for record in records:
        reason = _check_record(record, kind)
        if reason:
            return reason
    if kind == "blob":
        return _check_blob(env)
    return None


def _check_blob(env: Envelope) -> str | None:
    size = int(env.get("Gud-Size") or 0)
    chunk_size = int(env.get("Gud-Chunk-Size") or 0)
    i, _, n = (env.get("Gud-Chunk") or "").partition("/")
    index, count = int(i), int(n)
    if count != -(-size // chunk_size):
        return "blob: chunk count does not match size"
    expected = chunk_size if index < count - 1 else size - (count - 1) * chunk_size
    if len(env.records_named("data")[0].data) != expected:
        return "blob: chunk has the wrong length"
    return None


# ---------------------------------------------------------------- Authentication-Results (§8.1)

@dataclass(frozen=True)
class AuthResult:
    authserv_id: str
    results: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...]


def _strip_comments(value: str) -> str:
    out = []
    depth = 0
    quoted = False
    i = 0
    while i < len(value):
        ch = value[i]
        if quoted:
            out.append(ch)
            if ch == "\\" and i + 1 < len(value):
                out.append(value[i + 1])
                i += 1
            elif ch == '"':
                quoted = False
        elif depth:
            if ch == "\\":
                i += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        elif ch == "(":
            depth = 1
            out.append(" ")
        elif ch == '"':
            quoted = True
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _ar_split(value: str, sep: str) -> list[str]:
    """Split on `sep` outside quoted strings."""
    parts, cur, quoted, i = [], [], False, 0
    while i < len(value):
        ch = value[i]
        if quoted and ch == "\\" and i + 1 < len(value):
            cur.append(ch + value[i + 1])
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _ar_words(text: str) -> list[str]:
    """Whitespace-separated words, with spaces around `=` and `/` (outside quotes) removed."""
    words: list[str] = []
    for chunk in _ar_split(" ".join(text.split()), " "):
        if not chunk:
            continue
        prev = words[-1] if words else ""
        prev_open = prev.endswith("/") or (prev.endswith("=") and prev.count("=") == 1)
        if words and ((chunk[0] in "=/" and "=" not in prev) or prev_open):
            words[-1] += chunk
        else:
            words.append(chunk)
    return words


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def parse_authentication_results(value: str) -> AuthResult | None:
    """RFC 8601 header value -> AuthResult; comments stripped. Never raises.

    Lenient on result syntax (malformed resinfo is skipped) so that only a value with no
    authserv-id at all yields None."""
    try:
        parts = _ar_split(_strip_comments(value.replace("\r", " ").replace("\n", " ")), ";")
        head = parts[0].split()
        if not head:
            return None
        authserv = _unquote(head[0])
        if not authserv:
            return None
        results = []
        for part in parts[1:]:
            words = _ar_words(part)
            if not words or "=" not in words[0]:
                continue
            method, _, result = words[0].partition("=")
            method = method.split("/", 1)[0].strip().lower()
            props = []
            for word in words[1:]:
                key, eq, val = word.partition("=")
                if not eq:
                    continue
                props.append((key.lower(), _unquote(val)))
            results.append((method, _unquote(result).lower(), tuple(props)))
        return AuthResult(authserv, tuple(results))
    except Exception:
        return None


def authserv_pins(pinned: str) -> tuple[str, ...]:
    """§10.5 `authserv-id`: one or more ids, separated by spaces or commas, lowercased.
    `*.example.net` names every subdomain of example.net (not example.net itself)."""
    return tuple(p for p in re.split(r"[\s,]+", (pinned or "").lower()) if p)


def authserv_pin_error(pin: str) -> str | None:
    """Why `pin` is not a valid §10.5 pin, or None. A wildcard is one leading `*.` over at least
    two labels, so a pin can never match every receiving server."""
    if pin.startswith("*."):
        rest = pin[2:]
        if "*" in rest or rest.count(".") < 1 or not all(rest.split(".")):
            return f"{pin!r}: a wildcard is *.<domain> with at least two labels"
    elif "*" in pin:
        return f"{pin!r}: * is allowed only as a leading *."
    return None


def authserv_matches(pinned: str, authserv_id: str) -> bool:
    """Whether a header's authserv-id is one of the pinned ids (case-insensitive)."""
    got = authserv_id.lower()
    for pin in authserv_pins(pinned):
        if pin.startswith("*.") and got.endswith(pin[1:]) and len(got) > len(pin) - 1:
            return True
        if pin == got:
            return True
    return False


def dmarc_pass(auth_results: Sequence[str], authserv_id: str, from_domain: str) -> bool:
    """§8.1(2): the topmost header whose authserv-id matches the pinned `authserv_id` (one or
    more ids, see `authserv_pins`) reports dmarc=pass with header.from == from_domain (both
    compared case-insensitively)."""
    if not authserv_pins(authserv_id) or not from_domain:
        return False
    for value in auth_results:
        parsed = parse_authentication_results(value)
        if parsed is None or not authserv_matches(authserv_id, parsed.authserv_id):
            continue
        for method, result, props in parsed.results:
            if method == "dmarc" and result == "pass":
                if any(k == "header.from" and v.lower() == from_domain.lower() for k, v in props):
                    return True
        return False
    return False


# ---------------------------------------------------------------- events (§19.1)

EVENT_KEYS: Mapping[str, tuple[str, ...]] = {
    "push.received": ("push",),
    "review.requested": ("candidate", "owner"),
    "objected": ("objection",),
    "commented": ("comment",),
    "approved": ("approve",),
    "held": ("hold",),
    "test.passed": ("test",),
    "test.failed": ("test",),
    "window.closing": ("candidate", "closes_at"),
    "superseded": ("push", "by_link"),
    "push.rejected": ("push", "status"),
    "carried": ("candidate",),
    "linked": ("link",),
    "task.opened": ("task",),
    "subtask.posted": ("subtask",),
    "claim.won": ("claim",),
    "claim.lost": ("claim",),
    "claim.lapsed": ("claim", "lapsed_at"),
    "claim.overlap": ("claims",),
    "scope.exceeded": ("push", "claim"),
    "task.ready": ("task", "link"),
    "task.done": ("task",),
    "escalated": ("escalation",),
    "escalation.resolved": ("escalation",),
    "tip.red": ("red_link",),
    "tip.bisected": ("red_link", "first_bad"),
    "tip.green": ("link",),
    "fix.opened": ("task",),
    "host.fork": ("message",),
}


def event_id(type: str, key: Sequence[str | int]) -> Hex:
    return sha256_hex(jcs({"type": type, "key": list(key)}))


def _event_key(type: str, at: Ms, data: Mapping[str, Any]) -> list[str | int]:
    if type == "escalated" and data.get("escalation") is None:
        return [data["task"], data["reason"], at]
    if type == "host.fork" and data.get("message") is None:
        return [sha256_hex(jcs(data["proof"]))]
    key: list[str | int] = []
    for name in EVENT_KEYS[type]:
        value = data[name]
        if isinstance(value, (list, tuple)):
            key.extend(sorted(value))
        elif isinstance(value, (str, int)) and not isinstance(value, bool):
            key.append(value)
        else:
            raise EnvelopeError(f"{type}: key field {name} must be a string or integer")
    return key


def make_event(type: str, at: Ms, concerns: Iterable[Handle], data: Mapping[str, Any]) -> Event:
    """Event with its §19.1 id from EVENT_KEYS; concerns sorted and deduplicated."""
    if type not in EVENT_KEYS:
        raise EnvelopeError(f"unknown event type {type!r}")
    try:
        key = _event_key(type, at, data)
    except KeyError as exc:
        raise EnvelopeError(f"{type}: missing key field {exc}") from exc
    return Event(event_id(type, key), type, int(at), tuple(sorted(set(concerns))), dict(data))
