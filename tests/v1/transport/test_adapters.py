"""Adapter-specific behaviour beyond the shared conformance checks."""

from __future__ import annotations

import base64
import imaplib
import json
import threading
import time

import pytest

from gud.core import envelope as E
from gud.core.types import Record
from gud.transport import mime, registry
from gud.transport.base import Outgoing, TransportError
from gud.transport.imap_smtp import ImapSmtpMailbox, decode_cursor, encode_cursor, parse_internaldate
from gud.transport.primitive import (
    AUTHSERV_ID, PrimitiveMailbox, PrimitiveQuotaError, decode_cursor as p_decode, dmarc_for,
    signature_header, verify_signature,
)

from . import greenmail
from .primitive_mock import SECRET, MockPrimitive

# ---------------------------------------------------------------- IMAP


def _imap_box(gm, user: str, **kw) -> ImapSmtpMailbox:
    return ImapSmtpMailbox(gm.host, gm.imap_port, gm.host, gm.smtp_port, user, greenmail.PASSWORD,
                           gm.address(user), imap_tls="none", smtp_tls="none", idle_poll=2.0, **kw)


def test_internaldate_and_cursor_codec() -> None:
    assert parse_internaldate(b'1 (INTERNALDATE "20-Oct-2026 01:08:38 -0700" UID 3)') == 1792483718000
    assert parse_internaldate(b'INTERNALDATE " 2-Jan-2026 00:00:00 +0000"') == 1767312000000
    assert parse_internaldate(b"no date") is None
    state = {"INBOX": (7, 12), "Junk:odd;name%": (9, 0)}
    text = encode_cursor(state, ["INBOX", "Junk:odd;name%"])
    assert decode_cursor(text) == state and decode_cursor(None) == {}
    with pytest.raises(TransportError):
        decode_cursor("garbage")


def test_imap_reads_junk_and_rescans_on_uidvalidity_change(greenmail_server) -> None:
    gm = greenmail_server
    conn = imaplib.IMAP4(gm.host, gm.imap_port)
    conn.login("box1", greenmail.PASSWORD)
    conn.create("Junk")
    junk_raw = mime.build(Outgoing(from_addr="spammy@example.org", to=(gm.address("box1"),), subject="junk one",
                                   text="approve\n"))
    box = _imap_box(gm, "box1")
    alice = _imap_box(gm, "alice")
    start = box.head()
    alice.send(Outgoing(from_addr=alice.address, to=(box.address,), subject="inbox one", text="x"))
    time.sleep(1.1)
    assert conn.append("Junk", None, imaplib.Time2Internaldate(time.time()), junk_raw)[0] == "OK"
    conn.logout()
    items, cursor = box.list(since=start)
    subjects = [mime.parse(i.raw_mime).subject for i in items]
    assert subjects == ["inbox one", "junk one"]
    assert items[1].provider_id.startswith("Junk:") and items[1].received_at >= items[0].received_at
    parts = decode_cursor(cursor)
    assert set(parts) == {"INBOX", "Junk"}
    assert box.list(since=cursor) == ([], cursor)
    # A UIDVALIDITY change (cursor from another epoch) rescans that folder from UID 1.
    stale = encode_cursor({"INBOX": (parts["INBOX"][0] + 1, parts["INBOX"][1]), "Junk": parts["Junk"]}, ["INBOX", "Junk"])
    again, _ = box.list(since=stale)
    assert [mime.parse(i.raw_mime).subject for i in again][-1] == "inbox one"
    assert all(i.provider_id.startswith("INBOX:") for i in again)
    box.close()


def test_imap_missing_junk_folder_is_skipped(greenmail_server) -> None:
    box = _imap_box(greenmail_server, "box2", folders=("INBOX", "Junk", "Archive"))
    assert box.head().startswith("INBOX:") and ";" not in box.head()
    assert box.capabilities().push == "idle"
    box.close()


def test_imap_from_config_and_bad_login(greenmail_server) -> None:
    gm = greenmail_server
    box = registry.open_mailbox("imap_smtp", {
        "imap_host": gm.host, "imap_port": str(gm.imap_port), "smtp_port": str(gm.smtp_port),
        "username": "box2", "password": greenmail.PASSWORD, "imap_tls": "none", "smtp_tls": "none",
        "address": gm.address("box2"), "max_message_bytes": "5000000", "send_quota_per_day": "500"})
    caps = box.capabilities()
    assert caps.max_message_bytes == 5_000_000 and caps.send_quota_per_day == 500
    assert box.list()[0] == [] or isinstance(box.list()[0], list)
    bad = ImapSmtpMailbox(gm.host, gm.imap_port, gm.host, gm.smtp_port, "box2", "wrong", gm.address("box2"),
                          imap_tls="none", smtp_tls="none")
    with pytest.raises(TransportError):
        bad.list()
    with pytest.raises(TransportError):
        registry.open_mailbox("imap_smtp", {"imap_host": "h"})


# ---------------------------------------------------------------- Primitive (mock)


def _prim(mock: MockPrimitive, local: str, **kw) -> PrimitiveMailbox:
    kw.setdefault("sleep", lambda s: time.sleep(min(s, 0.01)))
    return PrimitiveMailbox("test-key", f"{local}@{mock.domain}", base_url="https://mock.primitive/v1",
                            client=mock.client(), webhook_secret=SECRET, **kw)


def _env(text: str):
    return E.build("blob", author=None, headers={"Gud-Blob": E.sha256_hex(text.encode()), "Gud-Size": str(len(text)),
                                                 "Gud-Chunk": "0/1", "Gud-Chunk-Size": str(len(text))},
                   records=[Record("data", (), text.encode())])


def test_primitive_send_shape_and_synthesized_auth() -> None:
    mock = MockPrimitive()
    repo, agent = _prim(mock, "repo"), _prim(mock, "agent")
    env = _env("payload\x00")
    sent = agent.send(Outgoing(from_addr=agent.address, to=(repo.address, f"other@{mock.domain}"),
                               cc=("cc@example.org",), subject="s", text="t", envelope=E.encode(env)))
    body = mock.sent[sent.provider_id]
    assert body["to"] == repo.address and body["cc"] == [f"other@{mock.domain}", "cc@example.org"]
    assert body["attachments"][0]["filename"] == "gud.env"
    assert base64.b64decode(body["attachments"][0]["content_base64"]) == E.encode(env)
    assert sent.message_id == f"<primitive-direct-{sent.provider_id}@{mock.domain}>"
    item = repo.list()[0][0]
    parsed = mime.parse(item.raw_mime)
    assert parsed.auth_results[0] == f"{AUTHSERV_ID}; dmarc=pass header.from={mock.domain}"
    assert parsed.message_id == sent.message_id
    assert mime.to_email(item.raw_mime).gud_id == E.gud_id(env)
    # raw-bytes send is re-rendered with the envelope intact
    raw = mime.build(Outgoing(from_addr=agent.address, to=(repo.address,), subject="raw", envelope=E.encode(env)))
    agent.send(raw)
    assert mime.to_email(repo.list()[0][-1].raw_mime).gud_id == E.gud_id(env)


def test_primitive_dmarc_alignment_rule() -> None:
    assert dmarc_for({"dmarc": "pass", "dmarcFromDomain": "primitive.email"}, "x.primitive.email") == "pass"
    assert dmarc_for({"dmarc": "pass", "dmarcFromDomain": "primitive.email"}, "primitive.email") == "pass"
    assert dmarc_for({"dmarc": "pass", "dmarcFromDomain": "evil.org"}, "example.com") == "fail"
    assert dmarc_for({"dmarc": "fail"}, "example.com") == "fail"
    assert dmarc_for(None, "example.com") == "none"


def test_primitive_cursor_inside_pages_and_resume() -> None:
    mock = MockPrimitive()
    repo, agent = _prim(mock, "repo"), _prim(mock, "agent")
    for i in range(5):
        agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), subject=f"m{i}", text="x"))
    seen, cursor = [], None
    while True:
        items, cursor = repo.list(since=cursor, limit=2)
        if not items:
            break
        seen += [mime.parse(i.raw_mime).subject for i in items]
        fresh = _prim(mock, "repo")          # every cursor resumes in a new process
        rest = fresh.list(since=items[0].cursor, limit=100)[0]
        assert [r.provider_id for r in rest[:len(items) - 1]] == [i.provider_id for i in items[1:]]
    assert seen == [f"m{i}" for i in range(5)]
    assert p_decode(cursor)[1] == 0, "an exhausted page settles on the exact tail cursor"
    assert repo.list(since=cursor) == ([], cursor)
    head = repo.head()
    assert repo.list(since=head)[0] == []


def test_primitive_webhook_intake() -> None:
    mock = MockPrimitive()
    repo, agent = _prim(mock, "repo", subscribe_wait=30), _prim(mock, "agent")
    mock.webhook_sink = lambda headers, body: repo.accept_webhook(headers, body)
    got = []
    event = threading.Event()
    sub = repo.subscribe(lambda r: (got.append((time.monotonic(), r)), event.set()))
    time.sleep(0.2)
    t0 = time.monotonic()
    agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), subject="hook", text="x"))
    assert event.wait(5)
    sub.close()
    assert got[0][0] - t0 < 1.0
    assert len({r.provider_id for _, r in got}) == len(got), "webhook and long-poll copies deduped"
    assert mime.parse(got[0][1].raw_mime).auth_results[0].startswith(AUTHSERV_ID)


def test_primitive_webhook_signature_and_fallbacks() -> None:
    mock = MockPrimitive(inline_raw_in_webhook=False)
    captured = []
    mock.webhook_sink = lambda headers, body: captured.append((headers, body))
    repo, agent = _prim(mock, "repo"), _prim(mock, "agent")
    agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), subject="hook2", text="x"))
    deadline = time.monotonic() + 2
    while not captured and time.monotonic() < deadline:
        time.sleep(0.01)
    headers, body = captured[0]
    now = int(time.time() * 1000)
    assert repo.verify(headers, body, now)
    assert repo.verify({"mymx-signature": headers["Primitive-Signature"]}, body, now)
    assert not repo.verify(headers, body + b" ", now)
    assert not repo.verify(headers, body, now + 301_000)
    assert not repo.verify({}, body, now)
    with pytest.raises(TransportError):
        repo.accept_webhook(headers, body.replace(b"email.received", b"email.receivee"))
    items = repo.parse(headers, body)                 # raw not inline: fetched by id
    assert len(items) == 1 and mime.parse(items[0].raw_mime).subject == "hook2" and items[0].cursor == ""
    other = json.dumps({"event": "email.bounced", "email": {"id": "x"}}).encode()
    assert repo.parse({}, other) == []
    assert verify_signature(SECRET, signature_header(SECRET, b"{}", 100), b"{}", 100) is None
    assert verify_signature("", "t=1,v1=00", b"{}", 1) is not None


def test_primitive_retries_and_quota() -> None:
    mock = MockPrimitive()
    agent = _prim(mock, "agent", sleep=lambda s: None)
    mock.fail_next = [429, 503]
    agent.send(Outgoing(from_addr=agent.address, to=(f"repo@{mock.domain}",), subject="retry", text="x"))
    assert mock.send_count == 1
    mock.fail_next = [429] * 4
    with pytest.raises(PrimitiveQuotaError):
        agent.send(Outgoing(from_addr=agent.address, to=(f"repo@{mock.domain}",), subject="quota", text="x"))
    assert mock.send_count == 1
    with pytest.raises(TransportError):
        agent.send(Outgoing(from_addr="someone@example.org", to=(f"repo@{mock.domain}",), subject="x", text="x"))
    caps = agent.capabilities()
    assert caps.send_quota_per_day == 10_000 and caps.auth_results == "synthesized"
    with pytest.raises(TransportError):
        registry.open_mailbox("primitive", {"address": "a@b"})
