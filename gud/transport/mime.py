"""Build and parse the v1 MIME layout (SPEC §2).

`build` writes the §2.1 layout byte for byte: CRLF line endings, a quoted-printable UTF-8 text
part, and the envelope as a base64 `gud.env` attachment in 76-character lines. An `Outgoing`
without an envelope becomes a single-part text/plain message (an unsigned human message, §2.2).

`parse` reads any RFC 5322 message, including ones another MIME producer rendered (providers
re-render API sends). It never raises on bad bodies. `to_email` turns raw bytes as stored by the
host into a `gud.core.types.Email`, computing the gud id from the envelope (§3.3) or, for an
unsigned message, from the raw bytes themselves.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from dataclasses import dataclass
from email import policy
from email.header import Header, decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import collapse_rfc2231_value, formataddr, formatdate, getaddresses, parseaddr

from gud.core import envelope as core_env
from gud.core.types import Email, Envelope, Hex, Ms

from .base import Outgoing

PREVIEW_LINES: int = 200
ENV_FILENAME = "gud.env"
_CRLF = b"\r\n"
_QP_MAX = 76
_B64_LINE = 76
_MSGID_RE = re.compile(r"<[^<>\s]+>")


class MimeError(ValueError):
    """The message has no parseable header block."""


# ---------------------------------------------------------------- building


def _clean(value: str) -> str:
    """Header values never carry CR or LF (no header injection)."""
    return re.sub(r"[\r\n]+", " ", value or "").strip()


def _domain(addr: str) -> str:
    bare = parseaddr(addr or "")[1] or (addr or "")
    return bare.rsplit("@", 1)[-1].lower() if "@" in bare else ""


def make_message_id(from_addr: str) -> str:
    return f"<{uuid.uuid4().hex}@{_domain(from_addr) or 'gud.invalid'}>"


def _format_addr(value: str) -> str:
    name, addr = parseaddr(_clean(value))
    if not addr:
        return _clean(value)
    return formataddr((name, addr), charset="utf-8")


def _fold_tokens(name: str, tokens: list[str]) -> bytes:
    """`Name: t1 t2 ...`, folded at token boundaries so lines stay near 78 characters."""
    line = f"{name}:"
    out: list[str] = []
    for tok in tokens:
        if len(line) + 1 + len(tok) > 78 and line != f"{name}:":
            out.append(line)
            line = " " + tok
        else:
            line += " " + tok
    out.append(line)
    return "\r\n".join(out).encode("utf-8") + _CRLF


def _header(name: str, value: str) -> bytes:
    value = _clean(value)
    if value.isascii() and all(32 <= ord(c) < 127 for c in value):
        line = f"{name}: {value}"
        if len(line) <= 998:
            return line.encode("ascii") + _CRLF
    encoded = Header(value, "utf-8", header_name=name).encode(linesep="\r\n")
    return f"{name}: {encoded}".encode("ascii") + _CRLF


def _addr_header(name: str, addrs: tuple[str, ...]) -> bytes:
    rendered = [_format_addr(a) for a in addrs if _clean(a)]
    tokens = [r + "," for r in rendered[:-1]] + rendered[-1:]
    return _fold_tokens(name, tokens)


def qp_encode(text: str) -> bytes:
    """Quoted-printable (RFC 2045 §6.7) of UTF-8 text with CRLF hard breaks; deterministic."""
    normal = text.replace("\r\n", "\n").replace("\r", "\n")
    out_lines: list[bytes] = []
    for raw_line in normal.split("\n"):
        data = raw_line.encode("utf-8")
        tokens: list[bytes] = []
        for i, b in enumerate(data):
            last = i == len(data) - 1
            if (33 <= b <= 126 and b != 61) or (b in (9, 32) and not last):
                tokens.append(bytes([b]))
            else:
                tokens.append(b"=%02X" % b)
        line = b""
        for tok in tokens:
            if len(line) + len(tok) > _QP_MAX - 1:
                out_lines.append(line + b"=")
                line = b""
            line += tok
        out_lines.append(line)
    return _CRLF.join(out_lines)


def b64_lines(data: bytes) -> bytes:
    text = base64.b64encode(data)
    return _CRLF.join(text[i:i + _B64_LINE] for i in range(0, len(text), _B64_LINE))


def _boundary_for(seed: bytes, bodies: list[bytes]) -> str:
    n = 0
    while True:
        cand = "gud-" + hashlib.sha256(seed + str(n).encode()).hexdigest()[:40]
        marker = ("--" + cand).encode()
        if not any(marker in body for body in bodies):
            return cand
        n += 1


def build(message: Outgoing, *, date: str | None = None, boundary: str | None = None) -> bytes:
    """§2.1 layout (or a plain text/plain message when `message.envelope` is None)."""
    if not message.to:
        raise MimeError("a message needs at least one To address")
    head = b"".join([
        b"MIME-Version: 1.0\r\n",
        _addr_header("From", (message.from_addr,)),
        _addr_header("To", message.to),
        _addr_header("Cc", message.cc) if message.cc else b"",
        _header("Subject", message.subject or ""),
        _header("Message-ID", message.message_id or make_message_id(message.from_addr)),
        _header("Date", date or formatdate(usegmt=True)),
        _header("In-Reply-To", message.in_reply_to) if message.in_reply_to else b"",
        _fold_tokens("References", [_clean(r) for r in message.references if _clean(r)])
        if message.references else b"",
    ])
    text_body = qp_encode(message.text or "")
    text_headers = b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n"
    if message.envelope is None:
        # The body runs to the end of the message, so a final CRLF would add a newline to the
        # text; a soft line break ("=" CRLF) ends the message cleanly when the text has none.
        tail = b"" if text_body.endswith(_CRLF) else b"=" + _CRLF
        return head + text_headers + _CRLF + text_body + tail
    env_body = b64_lines(message.envelope)
    if boundary is None:
        boundary = _boundary_for(head + message.envelope, [text_body, env_body])
    delim = b"--" + boundary.encode("ascii")
    return b"".join([
        head,
        b'Content-Type: multipart/mixed; boundary="' + boundary.encode("ascii") + b'"\r\n',
        _CRLF,
        delim, _CRLF,
        text_headers, _CRLF,
        text_body, _CRLF,
        delim, _CRLF,
        b'Content-Type: application/octet-stream; name="gud.env"\r\n',
        b'Content-Disposition: attachment; filename="gud.env"\r\n',
        b"Content-Transfer-Encoding: base64\r\n",
        _CRLF,
        env_body, _CRLF,
        delim, b"--", _CRLF,
    ])


def render_human_part(env: Envelope, gud_id: Hex, preview: str = "") -> str:
    """§2.1 SHOULD content: free text, the headers as `Name: value` lines, `Gud-Id`, preview."""
    parts: list[str] = []
    if env.text:
        parts.append(env.text.rstrip("\n"))
        parts.append("")
    parts.extend(f"{name}: {value}" for name, value in env.headers)
    parts.append(f"Gud-Id: {gud_id}")
    if preview:
        lines = preview.splitlines()
        if len(lines) > PREVIEW_LINES:
            lines = lines[:PREVIEW_LINES] + [f"[... {len(lines) - PREVIEW_LINES} more lines]"]
        parts.append("")
        parts.extend(lines)
    return "\n".join(parts) + "\n"


def subject_for(env: Envelope, gud_id: Hex, summary: str) -> str:
    """`[gud] <kind> <summary> [gud:<12>]` (§2.1 subject tag)."""
    summary = _clean(summary)
    if len(summary) > 800:
        summary = summary[:797] + "..."
    words = ["[gud]", env.kind or "message"]
    if summary:
        words.append(summary)
    words.append(core_env.subject_tag(gud_id))
    return " ".join(words)


# ---------------------------------------------------------------- parsing


@dataclass(frozen=True)
class ParsedMime:
    headers: tuple[tuple[str, str], ...]             # top-level, decoded, in order
    message_id: str
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    in_reply_to: str | None
    references: tuple[str, ...]
    auth_results: tuple[str, ...]
    text: str
    envelope_bytes: bytes | None
    envelope_parts: int                              # number of gud.env parts found
    has_text: bool = False                           # a text/plain part exists (§2.2)


def _unfold(value: str) -> str:
    return re.sub(r"\r?\n(?=[ \t])", "", value).replace("\r", "").replace("\n", "")


def _undo_escape(value: str) -> str:
    try:
        return value.encode("ascii", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        return value


def _decode_header(value: str) -> str:
    value = _unfold(str(value))
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return _undo_escape(value)


def _addresses(values: list[str]) -> tuple[str, ...]:
    out = []
    for _name, addr in getaddresses([_unfold(_undo_escape(str(v))) for v in values]):
        addr = addr.strip()
        if addr:
            out.append(addr)
    return tuple(out)


def _param(part: Message, name: str, header: str) -> str | None:
    value = part.get_param(name, header=header)
    if value is None:
        return None
    try:
        return collapse_rfc2231_value(value).strip()
    except Exception:
        return None


def is_envelope_part(part: Message) -> bool:
    if part.is_multipart():
        return False
    return (_param(part, "filename", "content-disposition") == ENV_FILENAME
            or _param(part, "name", "content-type") == ENV_FILENAME)


def _envelope_bytes(part: Message) -> bytes | None:
    cte = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if cte == "base64":
        payload = part.get_payload()
        if not isinstance(payload, str):
            return None
        compact = re.sub(r"\s+", "", payload)
        try:
            return binascii.a2b_base64(compact.encode("ascii"), strict_mode=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            return None
    try:
        data = part.get_payload(decode=True)
    except Exception:
        return None
    return data if isinstance(data, bytes) else None


def _text_of(part: Message) -> str:
    try:
        data = part.get_payload(decode=True)
    except Exception:
        data = None
    if not isinstance(data, bytes):
        payload = part.get_payload()
        data = payload.encode("utf-8", "surrogateescape") if isinstance(payload, str) else b""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = data.decode(charset, "replace")
    except LookupError:
        text = data.decode("utf-8", "replace")
    return text.replace("\r\n", "\n")


def parse(raw: bytes) -> ParsedMime:
    """Parse any RFC 5322 message. Raises MimeError only when there is no header block."""
    try:
        msg = BytesParser(policy=policy.compat32).parsebytes(raw)
    except Exception as exc:  # pragma: no cover - compat32 is lenient
        raise MimeError(f"unparseable message: {exc}") from exc
    items = msg.items()
    if not items:
        raise MimeError("message has no header block")
    headers = tuple((name, _decode_header(value)) for name, value in items)
    first = {}
    for name, value in items:
        first.setdefault(name.lower(), value)
    from_list = _addresses(msg.get_all("From") or [])
    refs_raw = _unfold(str(first.get("references", "")))
    irt_raw = _unfold(str(first.get("in-reply-to", ""))).strip()
    irt_ids = _MSGID_RE.findall(irt_raw)
    env_parts: list[Message] = []
    text: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        if is_envelope_part(part):
            env_parts.append(part)
            continue
        if text is None and part.get_content_type() == "text/plain":
            text = _text_of(part)
    envelope_bytes = _envelope_bytes(env_parts[0]) if len(env_parts) == 1 else None
    return ParsedMime(
        headers=headers,
        message_id=_unfold(str(first.get("message-id", ""))).strip(),
        from_addr=from_list[0] if from_list else "",
        to=_addresses(msg.get_all("To") or []),
        cc=_addresses(msg.get_all("Cc") or []),
        subject=_decode_header(first.get("subject", "")),
        in_reply_to=irt_ids[0] if irt_ids else (irt_raw or None),
        references=tuple(_MSGID_RE.findall(refs_raw)),
        auth_results=tuple(_unfold(str(v)).strip() for v in (msg.get_all("Authentication-Results") or [])),
        text=text or "",
        envelope_bytes=envelope_bytes,
        envelope_parts=len(env_parts),
        has_text=text is not None,
    )


def to_email(raw: bytes, *, received_at: Ms | None = None, provider_id: str | None = None) -> Email | None:
    """The `Email` for raw bytes as stored by the host, or None when replay ignores the message:
    two or more gud.env parts, a gud.env part that does not decode (§2.1), or an unsigned
    message with no text/plain part (§2.2)."""
    try:
        parsed = parse(raw)
    except MimeError:
        return None
    env: Envelope | None = None
    if parsed.envelope_parts > 1:
        return None
    if parsed.envelope_parts == 1:
        if parsed.envelope_bytes is None:
            return None
        try:
            env = core_env.decode(parsed.envelope_bytes)
        except core_env.EnvelopeError:
            return None
        gud_id = core_env.gud_id(env)
    else:
        if not parsed.has_text:
            return None
        gud_id = core_env.unsigned_id(raw)
    return Email(
        gud_id=gud_id, raw_mime=raw, message_id=parsed.message_id, from_addr=parsed.from_addr,
        to=parsed.to, cc=parsed.cc, subject=parsed.subject, in_reply_to=parsed.in_reply_to,
        references=parsed.references, auth_results=parsed.auth_results, text=parsed.text,
        envelope=env, received_at=received_at, provider_id=provider_id,
    )


def outgoing_from_raw(raw: bytes) -> Outgoing:
    """Recover an `Outgoing` from raw bytes, for adapters whose provider only takes structured
    sends (the envelope bytes are kept exactly; other headers are re-rendered by the provider)."""
    parsed = parse(raw)
    if parsed.envelope_parts > 1:
        raise MimeError("message has more than one gud.env part")
    if parsed.envelope_parts == 1 and parsed.envelope_bytes is None:
        raise MimeError("gud.env part does not decode")
    return Outgoing(
        from_addr=parsed.from_addr, to=parsed.to, cc=parsed.cc, subject=parsed.subject,
        text=parsed.text, envelope=parsed.envelope_bytes, message_id=parsed.message_id or None,
        in_reply_to=parsed.in_reply_to, references=parsed.references,
    )


def recipients(raw: bytes) -> tuple[str, ...]:
    """To and Cc addresses of raw bytes, deduped case-insensitively in order."""
    parsed = parse(raw)
    seen: set[str] = set()
    out = []
    for addr in (*parsed.to, *parsed.cc):
        if addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return tuple(out)

