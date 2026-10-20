"""gud.transport.mime and gud.transport.auth."""

from __future__ import annotations

import base64
import hashlib
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from gud.core import envelope as E
from gud.core.types import Record
from gud.transport import auth, mime
from gud.transport.base import Outgoing, envelope_budget

DATE = "Tue, 20 Oct 2026 08:00:00 +0000"


def _blob(data: bytes):
    return E.build("blob", author=None, headers={
        "Gud-Blob": E.sha256_hex(data), "Gud-Size": str(len(data)), "Gud-Chunk": "0/1",
        "Gud-Chunk-Size": str(len(data))}, records=[Record("data", (), data)])


def _msg(env, **kw) -> Outgoing:
    fields = dict(from_addr="a@x.org", to=("repo@gud.test",), subject="s", text="hello\n",
                  envelope=E.encode(env), message_id="<m1@x.org>")
    fields.update(kw)
    return Outgoing(**fields)


def test_build_layout_is_byte_exact() -> None:
    env = _blob(b"abc")
    raw = mime.build(_msg(env, subject="[gud] blob x [gud:0123456789ab]"), date=DATE, boundary="BOUND")
    b64 = mime.b64_lines(E.encode(env))
    assert b"\r\n" in b64
    assert raw == (
        b"MIME-Version: 1.0\r\nFrom: a@x.org\r\nTo: repo@gud.test\r\n"
        b"Subject: [gud] blob x [gud:0123456789ab]\r\nMessage-ID: <m1@x.org>\r\nDate: " + DATE.encode() + b"\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUND"\r\n\r\n'
        b"--BOUND\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"hello\r\n\r\n"
        b'--BOUND\r\nContent-Type: application/octet-stream; name="gud.env"\r\n'
        b'Content-Disposition: attachment; filename="gud.env"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
        + b64 + b"\r\n--BOUND--\r\n"
    )


def test_build_is_deterministic_and_lines_are_short() -> None:
    env = _blob(bytes(range(256)) * 400 + b"\r\n" * 10)
    msg = _msg(env, text="x" * 500 + " trailing space \n\ttab\n=equals\nünï\n")
    a, b = mime.build(msg, date=DATE), mime.build(msg, date=DATE)
    assert a == b
    body = a.split(b"\r\n\r\n", 1)[1]
    assert max(len(line) for line in body.split(b"\r\n")) <= 76
    assert b"\n" not in a.replace(b"\r\n", b"")
    parsed = mime.parse(a)
    assert parsed.text == msg.text
    assert parsed.envelope_bytes == E.encode(env)


@pytest.mark.parametrize("data", [
    b"", b"\x00", b"\r", b"\n", b"\r\n", b"x" * 5000, b"\xff\xfe", "ünï ☃".encode(), bytes(range(256)),
])
def test_envelope_roundtrip_bytes(data: bytes) -> None:
    env = _blob(data) if data else E.build("comment", author="@a", headers={"Gud-Target": "ab" * 32},
                                           records=[Record("text", (), "ünï".encode())], sent=1, seen=None)
    email = mime.to_email(mime.build(_msg(env)))
    assert email is not None and email.gud_id == E.gud_id(env)
    assert E.encode(email.envelope) == E.encode(env)


def test_non_ascii_headers_round_trip() -> None:
    env = _blob(b"x")
    gid = E.gud_id(env)
    subject = mime.subject_for(env, gid, "ünïcödé summary ☃")
    raw = mime.build(_msg(env, from_addr="Ägënt Smith <a@x.org>", subject=subject,
                          cc=("Bö <b@y.org>", "c@z.org"), references=("<r1@x>", "<r2@x>"), in_reply_to="<r2@x>"))
    p = mime.parse(raw)
    assert p.subject == subject and subject.endswith(E.subject_tag(gid))
    assert p.from_addr == "a@x.org" and p.cc == ("b@y.org", "c@z.org")
    assert p.references == ("<r1@x>", "<r2@x>") and p.in_reply_to == "<r2@x>"
    assert raw.isascii()


def test_header_injection_is_neutralised() -> None:
    env = _blob(b"x")
    raw = mime.build(_msg(env, subject="hi\r\nBcc: victim@x.org"))
    assert b"\r\nBcc:" not in raw
    assert mime.parse(raw).subject == "hi Bcc: victim@x.org"


def test_unsigned_plain_message() -> None:
    for text in ("approve", "", "a\n\n"):
        raw = mime.build(Outgoing(from_addr="h@x.org", to=("r@x.org",), text=text))
        assert mime.parse(raw).text == text
    raw = mime.build(Outgoing(from_addr="h@x.org", to=("repo@gud.test",), subject="Re: x", text="approve\nthanks\n"))
    assert b"multipart" not in raw
    email = mime.to_email(raw)
    assert email.envelope is None and email.text == "approve\nthanks\n"
    assert email.gud_id == hashlib.sha256(raw).hexdigest()


def test_parse_foreign_layouts() -> None:
    """A provider re-renders the MIME (here: Python's email package, nested multipart)."""
    env = _blob(b"\x00\r\n" * 100)
    inner = EmailMessage(policy=SMTP)
    inner["From"] = '"Someone" <s@x.org>'
    inner["To"] = "repo@gud.test"
    inner["Subject"] = "=?utf-8?q?caf=C3=A9?= [gud:abc]"
    inner["Message-ID"] = "<folded@x.org>"
    inner.set_content("body text ünï\n")
    inner.add_alternative("<p>html</p>", subtype="html")
    inner.add_attachment(E.encode(env), maintype="application", subtype="octet-stream", filename="gud.env")
    p = mime.parse(inner.as_bytes().replace(b"Message-ID: <folded@x.org>", b"Message-ID: \r\n <folded@x.org>"))
    assert p.envelope_parts == 1 and p.envelope_bytes == E.encode(env)
    assert p.text == "body text ünï\n" and p.subject == "café [gud:abc]"
    assert p.message_id == "<folded@x.org>" and p.from_addr == "s@x.org"


def test_two_envelopes_or_bad_envelope_are_ignored() -> None:
    env = _blob(b"x")
    msg = EmailMessage(policy=SMTP)
    msg["From"], msg["To"], msg["Subject"] = "a@x.org", "r@x.org", "two"
    msg.set_content("t")
    msg.add_attachment(E.encode(env), maintype="application", subtype="octet-stream", filename="gud.env")
    msg.add_attachment(E.encode(env), maintype="application", subtype="octet-stream", filename="gud.env")
    assert mime.parse(msg.as_bytes()).envelope_parts == 2
    assert mime.to_email(msg.as_bytes()) is None
    bad = mime.build(Outgoing(from_addr="a@x.org", to=("r@x.org",), envelope=b"not an envelope\n"))
    assert mime.parse(bad).envelope_bytes == b"not an envelope\n"
    assert mime.to_email(bad) is None
    garbage = mime.build(_msg(env)).replace(base64.b64encode(E.encode(env))[:8], b"!!!!!!!!")
    assert mime.to_email(garbage) is None


def test_envelope_found_by_content_type_name_and_qp_cte() -> None:
    env = _blob(b"data")
    b64 = base64.b64encode(E.encode(env))
    raw = (b"From: a@x.org\r\nTo: r@x.org\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
           b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nhi\r\n'
           b'--b\r\nContent-Type: application/octet-stream; name="gud.env"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
           + b"\r\n ".join(b64[i:i + 10] for i in range(0, len(b64), 10)) + b"\r\n--b--\r\n")
    email = mime.to_email(raw)
    assert email is not None and email.gud_id == E.gud_id(env)


def test_no_text_part_unsigned_is_ignored() -> None:
    raw = b"From: a@x.org\r\nTo: r@x.org\r\nSubject: s\r\nContent-Type: text/html\r\n\r\n<p>x</p>\r\n"
    assert mime.parse(raw).has_text is False
    assert mime.to_email(raw) is None


def test_headers_unparseable() -> None:
    with pytest.raises(mime.MimeError):
        mime.parse(b"")
    assert mime.to_email(b"") is None


def test_auth_results_order_and_raw_kept() -> None:
    raw = (b"Authentication-Results: mx.a; dmarc=fail header.from=x.org\r\n"
           b"Authentication-Results: mx.a;\r\n dmarc=pass header.from=x.org\r\n"
           b"From: h@x.org\r\nTo: r@x.org\r\nSubject: s\r\n\r\napprove\r\n")
    p = mime.parse(raw)
    assert p.auth_results == ("mx.a; dmarc=fail header.from=x.org", "mx.a; dmarc=pass header.from=x.org")
    assert not auth.sender_verified(p.auth_results, p.from_addr, "mx.a")   # topmost decides
    assert auth.sender_verified(p.auth_results[1:], p.from_addr, "MX.A")
    assert not auth.sender_verified(p.auth_results, p.from_addr, None)


def test_synthesize_and_prepend() -> None:
    value = auth.synthesize("primitive.gud.invalid", "PASS", "Agents.Example.org")
    assert value == "primitive.gud.invalid; dmarc=pass header.from=agents.example.org"
    raw = b"From: h@agents.example.org\r\nSubject: s\r\n\r\nx\r\n"
    out = auth.prepend(raw, value)
    assert out.startswith(b"Authentication-Results: " + value.encode() + b"\r\n") and out.endswith(raw)
    assert auth.prepend(b"From: a@b\n\nx\n", value).split(b"\n")[0].endswith(b"agents.example.org")
    with pytest.raises(ValueError):
        auth.synthesize("bad id;", "pass", "x.org")
    assert auth.parse_authentication_results is E.parse_authentication_results


def test_render_human_part_truncates_preview() -> None:
    env = E.build("comment", author="@a", headers={"Gud-Target": "ab" * 32},
                  records=[Record("text", (), b"why\n")], sent=1, seen=None)
    text = mime.render_human_part(env, "f" * 64, "\n".join(f"l{i}" for i in range(250)))
    assert text.startswith("why\n\n") and "Gud-Id: " + "f" * 64 in text and "Gud-Target: " in text
    assert "l199" in text and "l200" not in text and "[... 50 more lines]" in text


def test_outgoing_from_raw_keeps_envelope() -> None:
    env = _blob(b"\x00\x01")
    raw = mime.build(_msg(env, cc=("c@x.org",)))
    out = mime.outgoing_from_raw(raw)
    assert out.envelope == E.encode(env) and out.to == ("repo@gud.test",) and out.cc == ("c@x.org",)


def test_envelope_budget() -> None:
    assert envelope_budget(10 * 1024 * 1024) == 7_340_032
    assert envelope_budget(25 * 1024 * 1024, 10 * 1024 * 1024) == 7_340_032
    small = 8 * 1024 * 1024
    b = envelope_budget(small)
    assert b < (small - 65536) * 3 // 4                 # never above the §6 value
    raw = mime.build(Outgoing(from_addr="a@x.org", to=("r@x.org",), subject="s" * 200, text="t" * 2000,
                              envelope=b"\x00" * b))
    assert len(raw) <= small                            # a full-budget message fits M
    assert envelope_budget(60_000) == 0
