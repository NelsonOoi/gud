"""Postmark adapter: send shape, webhook auth (Basic + IP), RawEmail, catch-up, quota."""

from __future__ import annotations

import base64
import json
import time

import pytest

from gud.core import envelope as E
from gud.core.types import Record
from gud.transport import auth, mime, registry
from gud.transport.base import Outgoing, TransportError
from gud.transport.postmark import (
    REMOTE_ADDR_HEADER, WEBHOOK_IPS, PostmarkError, PostmarkMailbox, PostmarkQuotaError, basic_auth_header,
)

from .postmark_mock import HOOK_PASSWORD, HOOK_USER, MockPostmark


def _box(mock: MockPostmark, local: str, tmp_path, **kw) -> PostmarkMailbox:
    kw.setdefault("webhook_username", HOOK_USER)
    kw.setdefault("webhook_password", HOOK_PASSWORD)
    kw.setdefault("allowed_ips", WEBHOOK_IPS)
    return PostmarkMailbox(f"pm-{local}", f"{local}@{mock.inbound_domain}", base_url="https://mock.postmark",
                           client=mock.client(), store=tmp_path / f"spool-{local}", sleep=lambda s: None, **kw)


def _capture(mock: MockPostmark) -> list:
    got: list = []
    mock.webhook_sink = lambda headers, body: got.append((headers, body))
    return got


def _wait_for(got: list, n: int = 1) -> None:
    deadline = time.monotonic() + 3
    while len(got) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(got) >= n


def _env(data: bytes):
    return E.build("blob", author=None, headers={"Gud-Blob": E.sha256_hex(data), "Gud-Size": str(len(data)),
                                                 "Gud-Chunk": "0/1", "Gud-Chunk-Size": str(len(data))},
                   records=[Record("data", (), data)])


def test_send_shape_and_message_id_rewrite(tmp_path) -> None:
    mock = MockPostmark()
    got = _capture(mock)
    repo, agent = _box(mock, "repo", tmp_path), _box(mock, "agent", tmp_path)
    env = _env(b"\x00\r\nbytes\xff")
    sent = agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), cc=("cc@example.org",), subject="s",
                               text="human part", envelope=E.encode(env), message_id="<mine@pm.test>",
                               in_reply_to="<parent@pm.test>", references=("<root@pm.test>", "<parent@pm.test>")))
    body = mock.sent[sent.provider_id]
    assert body["To"] == repo.address and body["Cc"] == "cc@example.org" and body["TextBody"] == "human part"
    assert body["Attachments"][0]["Name"] == "gud.env" and base64.b64decode(body["Attachments"][0]["Content"]) == E.encode(env)
    assert {"Name": "Message-ID", "Value": "<mine@pm.test>"} in body["Headers"]
    assert sent.message_id == f"<{sent.provider_id}@mtasv.net>"
    _wait_for(got)
    item = repo.accept_webhook(*got[0])[0]
    parsed = mime.parse(item.raw_mime)
    assert parsed.message_id == sent.message_id, "Postmark's Message-ID, as Sent reported"
    assert parsed.in_reply_to == "<parent@pm.test>" and parsed.references == ("<root@pm.test>", "<parent@pm.test>")
    assert mime.to_email(item.raw_mime).gud_id == E.gud_id(env)
    # A raw-bytes send is re-rendered with the envelope intact.
    agent.send(mime.build(Outgoing(from_addr=agent.address, to=(repo.address,), subject="raw", envelope=E.encode(env))))
    _wait_for(got, 2)
    assert mime.to_email(repo.accept_webhook(*got[1])[0].raw_mime).gud_id == E.gud_id(env)
    kept = _box(mock, "agent", tmp_path, preserves_message_id=True)
    assert kept.send(Outgoing(from_addr=kept.address, to=("x@else.org",), subject="k", text="t",
                              message_id="<keep@pm.test>")).message_id == "<keep@pm.test>"


def test_webhook_basic_auth_and_ip_allow_list(tmp_path) -> None:
    mock = MockPostmark()
    got = _capture(mock)
    repo, agent = _box(mock, "repo", tmp_path), _box(mock, "agent", tmp_path)
    agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), subject="hook", text="x"))
    _wait_for(got)
    headers, body = got[0]
    assert repo.verify(headers, body, 0)
    assert not repo.verify({**headers, "Authorization": basic_auth_header(HOOK_USER, "wrong")}, body, 0)
    assert not repo.verify({k: v for k, v in headers.items() if k != "Authorization"}, body, 0)
    assert not repo.verify({**headers, REMOTE_ADDR_HEADER: "203.0.113.9"}, body, 0)
    assert repo.verify({k: v for k, v in headers.items() if k != REMOTE_ADDR_HEADER}, body, 0,
                       remote_addr=WEBHOOK_IPS[1])
    with pytest.raises(TransportError):
        repo.accept_webhook({**headers, REMOTE_ADDR_HEADER: "203.0.113.9"}, body)
    nothing = _box(mock, "repo", tmp_path, webhook_username=None, webhook_password=None, allowed_ips=())
    assert not nothing.verify(headers, body, 0), "with no credentials and no allow-list nothing verifies"
    ip_only = _box(mock, "repo", tmp_path, webhook_username=None, webhook_password=None)
    assert ip_only.verify({REMOTE_ADDR_HEADER: WEBHOOK_IPS[0]}, body, 0)
    assert len(repo.accept_webhook(headers, body)) == 1 and repo.accept_webhook(headers, body) == []


def test_raw_email_required_and_recipient_filter(tmp_path) -> None:
    mock = MockPostmark(raw_enabled=False)
    got = _capture(mock)
    repo, agent = _box(mock, "repo", tmp_path), _box(mock, "agent", tmp_path)
    assert repo.capabilities().raw_mime is False, "RawEmailEnabled off: this server cannot host a repo"
    agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), subject="no raw", text="x"))
    _wait_for(got)
    with pytest.raises(TransportError, match="RawEmail"):
        repo.accept_webhook(*got[0])
    payload = json.loads(got[0][1])
    payload.update(RawEmail="Subject: x\r\n\r\nbody", OriginalRecipient="other@pm.test",
                   ToFull=[{"Email": "other@pm.test"}], CcFull=[])
    assert repo.parse({}, json.dumps(payload).encode()) == []
    alias = _box(mock, "repo", tmp_path, inbound_aliases=("other@pm.test",))
    assert len(alias.parse({}, json.dumps(payload).encode())) == 1


def test_catch_up_from_message_search(tmp_path) -> None:
    mock = MockPostmark()
    got = _capture(mock)
    repo, agent = _box(mock, "repo", tmp_path), _box(mock, "agent", tmp_path)
    for i in range(4):
        agent.send(Outgoing(from_addr=agent.address, to=(repo.address,), subject=f"m{i}", text="x"))
        time.sleep(0.002)
    _wait_for(got, 4)
    repo.accept_webhook(*got[0])
    assert repo.catch_up() == 3 and repo.catch_up() == 0
    items, cursor = repo.list()
    assert [mime.parse(i.raw_mime).subject for i in items] == ["m0", "m1", "m2", "m3"], "oldest first"
    assert all(i.received_at > 1_500_000_000_000 for i in items)
    assert _box(mock, "repo", tmp_path).list(since=items[1].cursor)[0] == items[2:]
    for headers, body in got:
        assert repo.accept_webhook(headers, body) == []


def test_unsigned_mail_never_verifies(tmp_path) -> None:
    mock = MockPostmark()
    got = _capture(mock)
    repo = _box(mock, "repo", tmp_path)
    assert repo.capabilities().auth_results == "none" and repo.capabilities().authserv_id is None
    forged = (b"Authentication-Results: mx.example; dmarc=pass header.from=example.org\r\n"
              + mime.build(Outgoing(from_addr="human@example.org", to=(repo.address,), subject="f", text="approve\n")))
    mock.inject(forged, repo.address)
    _wait_for(got)
    parsed = mime.parse(repo.accept_webhook(*got[0])[0].raw_mime)
    assert parsed.from_addr == "human@example.org"
    assert not auth.sender_verified(parsed.auth_results, parsed.from_addr, repo.authserv_id)
    # Why the adapter pins nothing by default: Postmark writes no A-R of its own, so the topmost
    # header is the sender's, and pinning its authserv-id would let the forgery count.
    assert parsed.auth_results[0].startswith("mx.example")


def test_errors_quota_size_and_config(tmp_path) -> None:
    mock = MockPostmark()
    agent = _box(mock, "agent", tmp_path)
    with pytest.raises(PostmarkError) as exc:
        agent.send(Outgoing(from_addr="human@example.org", to=("repo@pm.test",), subject="s", text="t"))
    assert exc.value.code == 400
    mock.fail_next = [405]
    with pytest.raises(PostmarkQuotaError):
        agent.send(Outgoing(from_addr=agent.address, to=("repo@pm.test",), subject="credits", text="t"))
    mock.fail_next = [429, 500]
    agent.send(Outgoing(from_addr=agent.address, to=("x@else.org",), subject="retried", text="t"))
    mock.fail_next = [429] * 4
    with pytest.raises(PostmarkQuotaError):
        agent.send(Outgoing(from_addr=agent.address, to=("x@else.org",), subject="throttled", text="t"))
    calls = len(mock.requests)
    with pytest.raises(PostmarkError) as exc:
        agent.send(Outgoing(from_addr=agent.address, to=("x@else.org",), envelope=b"\0" * 7_400_000))
    assert exc.value.code == 300 and len(mock.requests) == calls, "refused before the API call"
    with pytest.raises(TransportError):
        registry.open_mailbox("postmark", {"address": "a@b"})
    box = registry.open_mailbox("postmark", {"server_token": "t", "address": "a@b.c", "allowed_ips": "postmark",
                                             "store": str(tmp_path / "cfg"), "webhook_username": "u",
                                             "webhook_password": "p"})
    assert isinstance(box, PostmarkMailbox) and box.allowed_ips == WEBHOOK_IPS
