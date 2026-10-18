"""Webhook receiver, the cache mailbox, and the mirror long-poll. No network."""

import json
import stat
import threading
import time
from datetime import datetime, timezone

import httpx
from starlette.testclient import TestClient

from gud.api import Ctx, create_app
from gud.config import Config
from gud.inbox import Gate
from gud.mail_hosted import HostedMailbox
from gud.mail_primitive import PrimitiveMailbox
from gud.mail_remote import RemoteMailbox
from gud.mailbox import open_mailbox
from gud.replay import replay
from gud.testkit import REPO, World
from gud.types import Email
from gud.webhook import (
    catch_up, create_app as webhook_app, delivery_recipients, setup_endpoint,
    signature_header, verify_signature,
)
from gud.cli import main as cli_main

WHEN = "2026-10-02T00:00:00Z"
WHEN_TS = datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()
SECRET = "not-base64-use-as-utf8"
EID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _cfg(tmp_path, **overrides) -> Config:
    fields = dict(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None,
        instabox_api_key=None, armature_api_key=None, openrouter_key=None, agent_model="test",
    )
    fields.update(overrides)
    return Config(**fields)


def _hosted(tmp_path) -> HostedMailbox:
    return HostedMailbox(tmp_path / "mailbox.jsonl", REPO, "gud.local", object())


def _primitive(handler) -> PrimitiveMailbox:
    return PrimitiveMailbox("prim_test", REPO, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _auth():
    return {
        "spf": "pass", "dmarc": "pass", "dmarcSpfAligned": True, "dmarcDkimAligned": True,
        "dkimSignatures": [{"domain": "gud.local", "result": "pass", "aligned": True}],
    }


def _payload(recipient=REPO, body="approve\n"):
    email = {
        "id": EID,
        "message_id": "msg-1@gud.local",
        "from_email": "nelson@example.com",
        "recipient": recipient,
        "to_email": REPO,
        "subject": "approve",
        "body_text": body,
        "received_at": WHEN,
        "sender_verified": True,
        "auth": _auth(),
        "parsed": {
            "body_text": body,
            "to_addresses": [{"address": REPO}],
            "cc": [{"address": "lead@gud.local"}],
        },
    }
    if recipient is None:
        email.pop("recipient")
    return {"event": "email.received", "email": email}


def _post(client, payload, secret, timestamp, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    header = signature_header(secret, body, timestamp)
    return client.post(
        "/primitive/webhook", content=body,
        headers={"Primitive-Signature": header, "content-type": "application/json"},
    )


def test_signature_accepts_the_raw_body_and_rejects_the_rest():
    now = 1_800_000_000
    raw = b'{"event":"email.received"}'
    header = signature_header(SECRET, raw, now)
    assert verify_signature(SECRET, header, raw, now) is None
    assert verify_signature(SECRET, header.replace("v1=", "v1=00"), raw, now)
    assert verify_signature(SECRET, signature_header(SECRET, raw, now - 301), raw, now)
    assert verify_signature(SECRET, header, raw + b" ", now)


def test_receiver_stores_a_signed_delivery_once_per_recipient(tmp_path):
    hosted = _hosted(tmp_path)

    def explode(request):
        raise AssertionError(f"parse must not call the network ({request.url})")

    app = webhook_app(hosted, SECRET, "gud.local", _primitive(explode))
    paths = sorted(getattr(route, "path", "") for route in app.routes if getattr(route, "path", None))
    assert paths == ["/primitive/webhook"]
    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/nope").status_code == 404

    now = int(time.time())
    payload = _payload()
    raw = json.dumps(payload).encode()
    ok = _post(client, payload, SECRET, now, raw)
    assert ok.status_code == 200, ok.text
    assert [email.body for email in hosted.list(recipient=REPO)] == ["approve\n"]
    assert hosted.list(recipient="lead@gud.local") == []
    again = _post(client, payload, SECRET, now, raw)
    assert again.status_code == 200
    assert len(hosted.list(recipient=REPO)) == 1

    wrong = client.post(
        "/primitive/webhook", content=raw,
        headers={"Primitive-Signature": signature_header("other", raw, now)},
    )
    assert wrong.status_code == 401
    stale = _post(client, payload, SECRET, now - 301, raw)
    assert stale.status_code == 401
    pretty = json.dumps(payload, indent=2).encode()
    resigned = client.post(
        "/primitive/webhook", content=pretty,
        headers={"Primitive-Signature": signature_header(SECRET, raw, now)},
    )
    assert resigned.status_code == 401
    assert len(hosted.list(recipient=REPO)) == 1

    ignored = _post(client, {"event": "email.bounced", "email": payload["email"]}, SECRET, now)
    assert ignored.status_code == 200
    assert ignored.json()["ignored"] == "email.bounced"
    assert len(hosted.list(recipient=REPO)) == 1


def test_a_delivery_with_no_body_is_fetched_by_id(tmp_path):
    hosted = _hosted(tmp_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path.endswith(f"/emails/{EID}")
        return httpx.Response(200, json={"success": True, "data": _row(
            EID, "<msg-1@gud.local>", WHEN, "fetched\n",
        )})

    app = webhook_app(hosted, SECRET, "gud.local", _primitive(handler))
    client = TestClient(app)
    payload = _payload(body="")
    payload["email"]["body_text"] = None
    payload["email"]["parsed"]["body_text"] = None
    response = _post(client, payload, SECRET, int(time.time()))
    assert response.status_code == 200, response.text
    assert response.json()["fetched"] is True
    assert hosted.list(recipient=REPO)[0].body == "fetched\n"
    assert calls == [f"/v1/emails/{EID}"]


def test_delivery_without_a_recipient_goes_to_every_local_address(tmp_path):
    hosted = _hosted(tmp_path)
    app = webhook_app(hosted, SECRET, "gud.local", _primitive(lambda request: httpx.Response(500)))
    client = TestClient(app)
    payload = _payload(recipient=None)
    assert _post(client, payload, SECRET, int(time.time())).status_code == 200
    assert len(hosted.list(recipient=REPO)) == 1
    assert len(hosted.list(recipient="lead@gud.local")) == 1
    assert delivery_recipients(payload, hosted.list()[0], "gud.local") == [REPO, "lead@gud.local"]


def test_catch_up_ingests_only_mail_newer_than_the_cache(tmp_path):
    hosted = _hosted(tmp_path)
    older = Email(
        "<old@gud.local>", "alice@gud.local", (REPO,), (), "old", "old\n",
        WHEN_TS, True, provider_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    hosted.ingest(older, [REPO])
    seen = []
    newer_at = "2026-10-03T00:00:00Z"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.params.get("recipient"), request.url.params.get("date_from")))
        recipient = request.url.params.get("recipient")
        if recipient != REPO:
            return httpx.Response(200, json={"success": True, "data": [], "meta": {"cursor": None}})
        if request.url.params.get("date_from"):
            rows = [_row("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "<new@gud.local>", newer_at, "new\n")]
        else:
            rows = []
        return httpx.Response(200, json={"success": True, "data": rows, "meta": {"cursor": None}})

    primitive = _primitive(handler)
    wrote = catch_up(hosted, primitive, [REPO, "bugs@gud.local"])
    assert wrote == 1
    assert [email.message_id for email in hosted.list(recipient=REPO)] == ["<old@gud.local>", "<new@gud.local>"]
    assert any(recipient == REPO and date_from for recipient, date_from in seen)
    assert catch_up(hosted, primitive, [REPO]) == 0
    assert len(hosted.list(recipient=REPO)) == 2


def test_out_of_order_deliveries_replay_the_same(tmp_path):
    world = World.create(tmp_path / "src")
    genesis = world.genesis({"a.py": "x = 1\n"})
    child = world.edit("@alice", genesis, "a.py", "x = 2\n")
    world.test(child)
    emails = world.repo_mail()

    def tip_of(order, name):
        box = HostedMailbox(tmp_path / name, REPO, "gud.local", object())
        for email in order:
            box.ingest(email, [REPO])
        chain = replay(box.list(recipient=REPO), 60, time.time())
        return chain.tip.state, [email.message_id for email in box.list(recipient=REPO)]

    forward, forward_ids = tip_of(emails, "forward.jsonl")
    backward, backward_ids = tip_of(list(reversed(emails)), "backward.jsonl")
    assert forward == backward
    assert forward_ids == backward_ids


def test_hosted_send_uses_primitive_and_reads_do_not(tmp_path):
    class Spy:
        def __init__(self):
            self.sends = 0
            self.reads = 0

        def send(self, **kwargs):
            self.sends += 1
            return Email(
                "<sent@gud.local>", kwargs["from_addr"], (kwargs["to"],), (),
                kwargs["subject"], kwargs["body"], time.time(), True,
            )

        def list(self, **kwargs):
            self.reads += 1
            return []

        def wait(self, **kwargs):
            self.reads += 1
            return []

    spy = Spy()
    hosted = HostedMailbox(tmp_path / "box.jsonl", REPO, "gud.local", spy)
    sent = hosted.send(from_addr="alice@gud.local", to=REPO, subject="s", body="b\n")
    assert spy.sends == 1
    assert hosted.list() == []
    assert hosted.wait(recipient=REPO, since=0, timeout=0.1) == []
    assert spy.reads == 0
    hosted.ingest(sent, [REPO])
    assert [email.message_id for email in hosted.list()] == ["<sent@gud.local>"]
    assert hosted.wait(recipient=REPO, since=0, timeout=0.2)[0].message_id == "<sent@gud.local>"
    assert spy.reads == 0


def test_open_mailbox_primitive_webhook_reads_the_cache(tmp_path):
    opened = open_mailbox(_cfg(
        tmp_path, mailbox="primitive-webhook", primitive_api_key="prim_test",
    ))
    assert isinstance(opened, HostedMailbox)
    assert opened.path == tmp_path / "home" / "mailbox.jsonl"
    assert isinstance(opened.send_via, PrimitiveMailbox)


def test_gate_wakes_once_when_the_window_closes():
    gate = Gate(busy_every=100, idle_every=100)
    assert gate.due(False, False)
    assert not gate.due(False, False)
    assert not gate.due(False, False, wake_at=time.time() + 100)
    assert gate.due(False, False, wake_at=time.time() - 1)
    assert not gate.due(False, False, wake_at=gate._woken_at)
    assert gate.due(False, False, wake_at=time.time() - 0.5)


def test_setup_stores_the_secret_and_requires_a_200(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/endpoints"):
            body = json.loads(request.content)
            assert body["kind"] == "http"
            assert body["url"] == "https://tunnel.example/primitive/webhook"
            assert body["rules"]["event_types"] == ["email.received"]
            return httpx.Response(200, json={"success": True, "data": {"id": "ep-1"}})
        if request.url.path.endswith("/account/webhook-secret"):
            return httpx.Response(200, json={"success": True, "data": {"secret": SECRET}})
        if request.url.path.endswith("/endpoints/ep-1/test"):
            return httpx.Response(200, json={"success": True, "data": {"status": 200, "body": "ok"}})
        raise AssertionError(request.method + " " + request.url.path)

    primitive = _primitive(handler)
    path = tmp_path / "webhook_secret"
    result = setup_endpoint(primitive, "https://tunnel.example/primitive/webhook", path)
    assert result["id"] == "ep-1"
    assert result["status"] == 200
    assert path.read_text().strip() == SECRET
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_webhook_setup_refuses_a_fake_mailbox(tmp_path, monkeypatch):
    monkeypatch.setenv("GUD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GUD_MAILBOX", "fake")
    try:
        cli_main(["webhook", "setup", "--url", "https://tunnel.example"])
    except SystemExit as exc:
        assert "primitive-webhook" in str(exc)
    else:
        raise AssertionError("setup should refuse the fake mailbox")


def test_mail_wait_and_remote_wait_return_when_a_row_is_ingested(tmp_path):
    world = World.create(tmp_path / "repo")
    genesis = world.genesis({"a.py": "x = 1\n"})
    cfg = _cfg(tmp_path)
    ctx = Ctx(cfg, mailbox=world.mailbox, auto_welcome=False, start_welcome_thread=False)
    app = create_app(ctx)
    client = TestClient(app)
    since = genesis.email.received_at
    direct = {}

    def fetch():
        direct["body"] = client.get("/api/mail", params={"since": since, "wait": 5}).json()

    thread = threading.Thread(target=fetch)
    thread.start()
    time.sleep(0.3)
    pushed = world.edit("@alice", genesis, "a.py", "x = 2\n")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert direct["body"]["emails"][0]["message_id"] == pushed.email.message_id

    remote = RemoteMailbox("http://testserver", world.ids["@alice"], world.mailbox, REPO, client=client)
    held = {}

    def wait_remote():
        held["rows"] = remote.wait(recipient="ignored@gud.local", since=pushed.email.received_at, timeout=5)

    thread = threading.Thread(target=wait_remote)
    thread.start()
    time.sleep(0.3)
    later = world.test(pushed)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert [email.message_id for email in held["rows"]] == [later.message_id]


def _row(eid, message_id, received_at, body):
    return {
        "id": eid, "message_id": message_id, "from_email": "alice@gud.local",
        "recipient": REPO, "to_email": REPO, "subject": "s", "body_text": body,
        "received_at": received_at, "sender_verified": True, "auth": _auth(),
    }
