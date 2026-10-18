"""PrimitiveMailbox against httpx.MockTransport. Payloads follow docs/primitive-openapi.json."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from gud.config import Config
from gud.mail_fake import FileMailbox
from gud.mail_primitive import DEFAULT_BASE_URL, PrimitiveError, PrimitiveMailbox
from gud.mailbox import open_mailbox

REPO = "repo@gud.local"
SENT_ID = "11111111-1111-4111-8111-111111111111"
WHEN = "2026-10-02T00:00:00Z"
WHEN_TS = datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()


def _cfg(tmp_path, **overrides):
    fields = dict(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None,
        instabox_api_key=None, armature_api_key=None, openrouter_key=None, agent_model="test",
    )
    fields.update(overrides)
    return Config(**fields)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _mailbox(handler, repo=REPO) -> PrimitiveMailbox:
    return PrimitiveMailbox("prim_test", repo, client=_client(handler))


def _ok(data, *, cursor=None, total=None) -> httpx.Response:
    body = {"success": True, "data": data}
    if cursor is not None or total is not None or isinstance(data, list):
        body["meta"] = {"total": total if total is not None else len(data), "limit": 100, "cursor": cursor}
    return httpx.Response(200, json=body)


def _err(status, code, message, headers=None) -> httpx.Response:
    return httpx.Response(status, headers=headers, json={
        "success": False, "error": {"code": code, "message": message},
    })


def _summary(eid, message_id, received_at, snippet="snip"):
    return {
        "id": eid, "status": "accepted", "sender": "alice@gud.local", "recipient": REPO,
        "domain": "gud.local", "created_at": received_at, "received_at": received_at,
        "subject": f"subj {eid}", "message_id": message_id, "snippet": snippet,
        "webhook_attempt_count": 1, "reply_count": 0, "last_replied_at": None,
        "awaiting": "you", "automated": False, "automated_reasons": [],
    }


def _auth(dmarc="pass", aligned=True, spf="pass", dkim=None):
    return {
        "spf": spf, "dmarc": dmarc, "dmarcPolicy": "reject", "dmarcFromDomain": "gud.local",
        "dmarcSpfAligned": aligned, "dmarcDkimAligned": aligned,
        "dkimSignatures": dkim if dkim is not None else [{
            "domain": "gud.local", "selector": "primitive", "result": "pass", "aligned": True,
        }],
    }


def test_send_fetches_stored_email_and_maps_message_id():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        assert request.headers["authorization"] == "Bearer prim_test"
        assert str(request.url).startswith(DEFAULT_BASE_URL + "/")
        if request.url.path.endswith("/send-mail"):
            body = json.loads(request.content)
            assert body["from"] == "alice@gud.local"
            assert body["to"] == REPO
            assert body["cc"] == ["bob@gud.local"]
            assert body["subject"] == "[gud] hello"
            assert body["body_text"] == "intent\n"
            assert body["in_reply_to"] == "<parent@gud.local>"
            assert body["references"] == ["<root@gud.local>", "<parent@gud.local>"]
            assert request.headers["idempotency-key"]
            return _ok({
                "id": SENT_ID, "status": "submitted_to_agent", "from": "alice@gud.local",
                "queue_id": "q-1", "accepted": [REPO], "rejected": [],
                "client_idempotency_key": request.headers["idempotency-key"],
                "request_id": "req-1", "content_hash": "abc", "idempotent_replay": False,
            })
        if request.url.path.endswith(f"/sent-emails/{SENT_ID}"):
            return _ok({
                "id": SENT_ID, "status": "delivered", "status_changed_at": WHEN, "created_at": WHEN,
                "updated_at": WHEN, "content_hash": "abc",
                "from_header": "alice@gud.local", "from_address": "alice@gud.local",
                "to_header": REPO, "to_address": REPO, "to_addresses": [REPO],
                "cc": ["bob@gud.local"], "subject": "[gud] hello", "body_text": "intent\n",
                "body_size_bytes": 7, "message_id": "abc123@gud.local",
                "in_reply_to": "<parent@gud.local>",
                "email_references": "<root@gud.local> <parent@gud.local>",
                "interaction_hint": "none", "interaction_kind": None, "snippet": "intent",
            })
        return _err(404, "not_found", "no such route")

    mb = _mailbox(handler)
    email = mb.send(from_addr="alice@gud.local", to=REPO, cc=["bob@gud.local"], subject="[gud] hello",
                    body="intent\n", in_reply_to="<parent@gud.local>",
                    references=["<root@gud.local>", "<parent@gud.local>"])
    assert email.message_id == "<abc123@gud.local>"
    assert email.provider_id == SENT_ID
    assert email.from_addr == "alice@gud.local"
    assert email.to == (REPO,)
    assert email.cc == ("bob@gud.local",)
    assert email.body == "intent\n"
    assert email.received_at == WHEN_TS
    assert email.sender_verified is True
    assert email.in_reply_to == "<parent@gud.local>"
    assert email.references == ("<root@gud.local>", "<parent@gud.local>")
    assert [path for _, path, _ in seen] == ["/v1/send-mail", f"/v1/sent-emails/{SENT_ID}"]
    assert mb.get(email.message_id) is email or mb.get(email.message_id).message_id == email.message_id
    assert len(seen) == 2


def test_send_falls_back_when_stored_email_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/send-mail"):
            return _ok({
                "id": SENT_ID, "status": "submitted_to_agent", "from": "alice@gud.local",
                "queue_id": None, "accepted": [REPO], "rejected": [],
                "client_idempotency_key": "k", "request_id": "req", "content_hash": "h",
                "idempotent_replay": False,
            })
        return _err(404, "not_found", "Sent email not found")

    email = _mailbox(handler).send(from_addr="alice@gud.local", to=REPO, subject="s", body="hello\n")
    assert email.provider_id == SENT_ID
    # The id Primitive gives our own send when it is delivered to the repo.
    assert email.message_id == f"<primitive-direct-{SENT_ID}@gud.local>"
    assert email.body == "hello\n"
    assert email.sender_verified is True
    assert email.to == (REPO,)


def test_list_paginates_oldest_first_and_caches_bodies():
    ids = {
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1": ("<e1@gud.local>", "2026-10-02T00:00:01Z"),
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2": ("<e2@gud.local>", "2026-10-02T00:00:02Z"),
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3": ("<e3@gud.local>", "2026-10-02T00:00:03Z"),
    }
    detail_gets = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        if path.endswith("/emails"):
            assert request.url.params["recipient"] == REPO
            assert "date_from" not in request.url.params
            assert "wait" not in request.url.params
            cursor = request.url.params.get("cursor")
            if not cursor:
                assert "cursor" not in request.url.params
                rows = [
                    _summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3", "<e3@gud.local>", "2026-10-02T00:00:03Z", "snip-3"),
                    _summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2", "<e2@gud.local>", "2026-10-02T00:00:02Z", "snip-2"),
                ]
                return _ok(rows, cursor="page-2", total=3)
            assert cursor == "page-2"
            rows = [_summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "<e1@gud.local>", "2026-10-02T00:00:01Z", "snip-1")]
            return _ok(rows, cursor=None, total=3)
        eid = path.rsplit("/", 1)[-1]
        detail_gets.append(eid)
        mid, received = ids[eid]
        return _ok({
            **_summary(eid, mid, received),
            "from_email": "alice@gud.local", "to_email": REPO, "sender_verified": True,
            "body_text": f"body-{eid[-1]}",
            "auth": _auth(),
            "parsed": {"status": "complete", "body_text": f"body-{eid[-1]}",
                       "to_addresses": [{"address": REPO}], "cc": [{"address": "bob@gud.local"}]},
        })

    mb = _mailbox(handler)
    emails = mb.list()
    assert [e.message_id for e in emails] == ["<e1@gud.local>", "<e2@gud.local>", "<e3@gud.local>"]
    assert [e.body for e in emails] == ["body-1", "body-2", "body-3"]
    assert all(e.sender_verified for e in emails)
    assert emails[0].cc == ("bob@gud.local",)
    assert detail_gets == [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
    ]
    again = mb.list()
    assert [e.message_id for e in again] == [e.message_id for e in emails]
    assert detail_gets == [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
    ]
    assert mb.get("<e2@gud.local>").body == "body-2"
    assert detail_gets[-1] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"


def test_failed_detail_fetch_is_retried_not_cached_as_snippet(monkeypatch):
    monkeypatch.setattr("gud.mail_primitive.time.sleep", lambda s: None)
    eid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    limited = {"left": 4}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        if path.endswith("/emails"):
            return _ok([_summary(eid, "<push@gud.local>", WHEN, snippet="intent without trailers")])
        if limited["left"]:
            limited["left"] -= 1
            return _err(429, "rate_limited", "slow down")
        return _ok({**_summary(eid, "<push@gud.local>", WHEN), "from_email": "alice@gud.local",
                    "sender_verified": True, "body_text": "intent\n\nGud-Kind: push\n"})

    mb = _mailbox(handler)
    assert mb.list() == []
    assert [e.body for e in mb.list()] == ["intent\n\nGud-Kind: push\n"]


def test_list_ttl_shares_one_listing_until_a_send():
    lists = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/send-mail"):
            return _ok({"id": SENT_ID, "message_id": "<s@gud.local>", "created_at": WHEN, "body_text": "x"})
        lists.append(1)
        return _ok([], cursor=None, total=0)

    mb = PrimitiveMailbox("prim_test", REPO, client=_client(handler), list_ttl=60)
    mb.list()
    mb.list()
    assert len(lists) == 1
    mb.send(from_addr="alice@gud.local", to=REPO, subject="s", body="x")
    mb.list()
    assert len(lists) == 2


def test_list_since_uses_date_from_and_is_exclusive():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["date_from"] == "2026-10-02T00:00:00Z"
        assert request.url.params["recipient"] == "alice@gud.local"
        rows = [
            _summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "<old@gud.local>", "2026-10-02T00:00:00Z"),
            _summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2", "<new@gud.local>", "2026-10-02T00:00:05Z"),
        ]
        for row in rows:
            row["body_text"] = "full"
            row["from_email"] = "bob@example.com"
            row["sender_verified"] = True
        return _ok(rows, cursor=None, total=2)

    since = WHEN_TS + 0.5
    emails = _mailbox(handler).list(recipient="Alice@gud.local", since=since)
    assert [e.message_id for e in emails] == ["<new@gud.local>"]


def test_wait_long_polls_in_30s_slices_until_mail_arrives():
    calls = []
    since = WHEN_TS

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        if "wait" not in params:
            assert params["date_from"] == "2026-10-02T00:00:00Z"
            return _ok([], cursor=None, total=0)
        if params["wait"] == "30":
            assert params["since"].endswith("|00000000-0000-0000-0000-000000000000")
            return _ok([], cursor="2026-10-02T00:00:00Z|cursor-2", total=0)
        assert params["wait"] == "15"
        assert params["since"] == "2026-10-02T00:00:00Z|cursor-2"
        row = _summary("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa9", "<late@gud.local>", "2026-10-02T00:00:05Z")
        row["body_text"] = "arrived"
        row["from_email"] = "alice@gud.local"
        row["sender_verified"] = True
        return _ok([row], cursor="2026-10-02T00:00:05Z|cursor-3", total=1)

    emails = _mailbox(handler).wait(recipient=REPO, since=since, timeout=45)
    assert [e.message_id for e in emails] == ["<late@gud.local>"]
    assert emails[0].body == "arrived"
    assert [c.get("wait") for c in calls] == [None, "30", "15"]


def test_wait_timeout_zero_does_not_long_poll():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return _ok([], cursor=None, total=0)

    assert _mailbox(handler).wait(recipient=REPO, since=WHEN_TS, timeout=0) == []
    assert calls and all("wait" not in c for c in calls)


def test_get_by_provider_id():
    eid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/emails/{eid}")
        return _ok({
            "id": eid, "message_id": "<byid@gud.local>", "from_email": "carol@gud.local",
            "recipient": REPO, "to_email": REPO, "subject": "hi", "body_text": "body",
            "received_at": WHEN, "sender_verified": True, "auth": _auth(),
        })

    email = _mailbox(handler).get(eid)
    assert email is not None
    assert email.message_id == "<byid@gud.local>"
    assert email.provider_id == eid
    assert email.body == "body"


def test_parse_webhook_inbound_event():
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"webhook parse must not call the network ({request.url})")

    mb = _mailbox(explode)
    verified = mb.parse_webhook({
        "event": "email.received",
        "email": {
            "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "message_id": "msg-1@gud.local",
            "from_email": "nelson@example.com",
            "sender": "nelson@example.com",
            "recipient": REPO,
            "to_email": REPO,
            "subject": "approve",
            "body_text": "approve\n",
            "received_at": WHEN,
            "status": "accepted",
            "auth": _auth(dmarc="pass", aligned=True),
            "parsed": {
                "status": "complete", "body_text": "approve\n",
                "to_addresses": [{"name": None, "address": REPO}],
                "cc": [{"name": None, "address": "lead@gud.local"}],
                "in_reply_to": ["parent@gud.local"],
                "references": ["parent@gud.local", "msg-1@gud.local"],
            },
        },
    })
    assert verified.message_id == "<msg-1@gud.local>"
    assert verified.provider_id == "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    assert verified.from_addr == "nelson@example.com"
    assert verified.to == (REPO,)
    assert verified.cc == ("lead@gud.local",)
    assert verified.in_reply_to == "<parent@gud.local>"
    assert verified.references[0] == "<parent@gud.local>"
    assert verified.body == "approve\n"
    assert verified.received_at == WHEN_TS
    assert verified.sender_verified is True

    own = mb.parse_webhook({
        "event": "email.received",
        "email": {
            "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "message_id": "<own@gud.local>",
            "from_email": "alice@gud.local",
            "recipient": REPO,
            "subject": "push",
            "body_text": "from us",
            "received_at": WHEN,
            "relay": {"hop": "primitive"},
            "auth": _auth(dmarc="none", aligned=False, spf="fail", dkim=[]),
        },
    })
    assert own.sender_verified is True

    dkim_only = mb.parse_webhook({
        "event": "email.received",
        "email": {
            "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "message_id": "<dkim@example.com>",
            "from_email": "pat@example.com",
            "recipient": REPO,
            "body_text": "x",
            "received_at": WHEN,
            "auth": _auth(dmarc="none", aligned=False, spf="fail", dkim=[{
                "domain": "example.com", "selector": "s", "result": "pass", "aligned": True,
            }]),
        },
    })
    assert dkim_only.sender_verified is True

    spoof = mb.parse_webhook({
        "event": "email.received",
        "email": {
            "id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "message_id": "<spoof@gud.local>",
            "from_email": "alice@gud.local",
            "recipient": REPO,
            "body_text": "nope",
            "received_at": WHEN,
            "auth": _auth(dmarc="fail", aligned=False, spf="fail", dkim=[]),
        },
    })
    assert spoof.sender_verified is False

    explicit = mb.parse_webhook({
        "event": "email.received",
        "email": {
            "id": "99999999-9999-4999-8999-999999999999",
            "message_id": "<nope@example.com>",
            "from_email": "eve@example.com",
            "recipient": REPO,
            "body_text": "hi",
            "received_at": WHEN,
            "sender_verified": False,
            "auth": _auth(),
        },
    })
    assert explicit.sender_verified is False


def test_recipient_not_allowed_is_reply_only_and_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _err(403, "recipient_not_allowed", "Recipient is outside the send policy")

    with pytest.raises(PrimitiveError) as caught:
        _mailbox(handler).send(from_addr="a@gud.local", to="new@example.com", subject="s", body="b")
    assert calls["n"] == 1
    assert caught.value.code == "recipient_not_allowed"
    assert caught.value.status == 403
    assert "reply-only" in caught.value.message
    assert "upgrade" in caught.value.message.lower()


def test_retries_429_respecting_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr("gud.mail_primitive.time.sleep", lambda seconds: slept.append(seconds))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _err(429, "rate_limit_exceeded", "Rate limit exceeded", headers={"Retry-After": "2"})
        return _ok({
            "id": SENT_ID, "status": "delivered", "from": "alice@gud.local",
            "queue_id": "q", "accepted": [REPO], "rejected": [],
            "client_idempotency_key": "k", "request_id": "r", "content_hash": "h",
            "idempotent_replay": False, "message_id": "<done@gud.local>",
            "created_at": WHEN, "body_text": "ok\n", "to_address": REPO, "subject": "s",
        })

    email = _mailbox(handler).send(from_addr="alice@gud.local", to=REPO, subject="s", body="ok\n")
    assert email.message_id == "<done@gud.local>"
    assert calls["n"] == 2
    assert slept == [2]


def test_retries_5xx_with_backoff_then_raises(monkeypatch):
    slept = []
    monkeypatch.setattr("gud.mail_primitive.time.sleep", lambda seconds: slept.append(seconds))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return _err(503, "outbound_capacity_exhausted", "Outbound capacity is temporarily exhausted")
        if calls["n"] == 3:
            return _err(500, "internal_error", "Internal server error")
        return _err(502, "outbound_unreachable", "Outbound SMTP service request failed")

    with pytest.raises(PrimitiveError) as caught:
        _mailbox(handler).send(from_addr="alice@gud.local", to=REPO, subject="s", body="b")
    assert calls["n"] == 4
    assert caught.value.code == "outbound_unreachable"
    assert slept == [0.5, 1.0, 2.0]


def test_open_mailbox_fake_and_primitive(tmp_path):
    fake = open_mailbox(_cfg(tmp_path))
    assert isinstance(fake, FileMailbox)
    assert fake.path == tmp_path / "home" / "mailbox.jsonl"
    sent = fake.send(from_addr="a@gud.local", to=REPO, subject="s", body="b\n")
    assert fake.list()[0].message_id == sent.message_id

    with pytest.raises(RuntimeError, match="PRIMITIVE_API_KEY"):
        open_mailbox(_cfg(tmp_path, mailbox="primitive"))

    primitive = open_mailbox(_cfg(tmp_path, mailbox="primitive", primitive_api_key="prim_live"))
    assert isinstance(primitive, PrimitiveMailbox)
    assert primitive.repo_address == REPO
    assert primitive.base_url == DEFAULT_BASE_URL

    with pytest.raises(ValueError, match="unknown GUD_MAILBOX"):
        open_mailbox(_cfg(tmp_path, mailbox="smtp"))
