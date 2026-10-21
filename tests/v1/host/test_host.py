"""The v1 host app (SPEC §11.2, §19.3, §21): mirror endpoints, signed member requests, delivery
registration, SSE with resume, provider webhooks and loopback-only admin."""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gud.core import envelope as E
from gud.core.hostlog import decode_entry
from gud.events.dispatch import EventClient, parse_sse
from gud.events.model import from_json
from gud.host import open_host, sign_request, verify_request
from gud.transport import mime
from gud.transport.base import Outgoing
from gud.transport.primitive import PrimitiveMailbox, signature_header
from tests.v1.events.harness import World
from tests.v1.kit import REPO, Kit
from tests.v1.transport.primitive_mock import SECRET, MockPrimitive

W, G = 60_000, 30_000


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.genesis({"app.py": "a = 1\n"}, author="@alice")
    yield w
    w.close()


def client(w: World, *, loopback=frozenset()) -> TestClient:
    return TestClient(w.host.app(loopback_hosts=loopback, sse_keepalive_s=0.05))


def signed(w: World, handle: str, method: str, path: str, body: bytes = b"", *, now: float | None = None,
           key: bytes | None = None) -> dict[str, str]:
    t = w.net.clock() if now is None else now
    return sign_request(handle, key or w.kit.key(handle), method, path, body, t)


def get(c: TestClient, w: World, handle: str, path: str, **kw):
    return c.get(path, headers=signed(w, handle, "GET", path, **kw))


# ---------------------------------------------------------------- mirror auth v1

def test_info_is_open_and_everything_else_needs_a_signature(world):
    c = client(world)
    info = c.get("/api/v1/info").json()
    assert info["repo"] == REPO and info["host_key"] == world.kit.pub("@host")
    assert info["log_head"]["seq"] == len(world.intake.log) - 1 and info["transport"] == "fake"
    for path in ("/api/v1/log", "/api/v1/mail", "/api/v1/bugs", "/api/v1/events?member=%40alice&follow=0"):
        assert c.get(path).status_code == 401, path
    assert get(c, world, "@alice", "/api/v1/log?since=0").status_code == 200


def test_signed_requests_negative_cases(world):
    c = client(world)
    path = "/api/v1/log?since=0"
    assert get(c, world, "@bob", path).status_code == 200
    bad = signed(world, "@bob", "GET", path)
    bad["X-Gud-Sig"] = signed(world, "@bob", "GET", "/api/v1/log?since=1")["X-Gud-Sig"]
    assert c.get(path, headers=bad).status_code == 401                         # signature over another path
    assert c.get(path, headers=signed(world, "@bob", "GET", path, key=world.kit.key("@carol"))).status_code == 401
    stale = signed(world, "@bob", "GET", path, now=world.net.clock() - 301)
    assert c.get(path, headers=stale).status_code == 401                      # |now - time| > 300
    assert c.get(path, headers=signed(world, "@bob", "GET", path, now=world.net.clock() - 299)).status_code == 200
    assert c.get(path, headers=signed(world, "@mallory", "GET", path, key=b"\x01" * 32)).status_code == 401


def test_removed_member_loses_access_when_removal_links(world):
    w = world
    c = client(w)
    path = "/api/v1/log?since=0"
    assert get(c, w, "@carol", path).status_code == 200
    g = w.chain().tip.state
    members = [m for m in w.kit.members if m[0] != "@carol"]
    rm = w.push("@nelson", g, {".gud/members": w.kit.members_file(members)})
    w.approve("@dana", rm)
    w.advance(W + G + 10)
    assert w.chain().tip.push == rm.id
    assert get(c, w, "@carol", path).status_code == 401
    assert get(c, w, "@bob", path).status_code == 200


def test_body_hash_is_signed(world):
    w = world
    c = client(w)
    body = json.dumps({"sse": True}).encode()
    headers = signed(w, "@bob", "POST", "/api/v1/delivery", body)
    tampered = json.dumps({"webhook": "http://evil.example/hook"}).encode()
    assert c.post("/api/v1/delivery", content=tampered, headers=headers).status_code == 401
    r = c.post("/api/v1/delivery", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["channel"] == "sse"
    assert w.host.registry.get("@bob").channel == "sse"


def test_verify_request_unit():
    k = Kit()
    from gud.core.config import parse_members
    members = parse_members(k.members_file())
    h = sign_request("@alice", k.key("@alice"), "POST", "/x?y=1", b"{}", 1000.5)
    assert h["X-Gud-Time"] == "1000.5"
    assert verify_request(members, "POST", "/x?y=1", b"{}", h, 1000.5) == "@alice"
    assert verify_request(members, "POST", "/x?y=1", b"{ }", h, 1000.5) is None
    assert verify_request(members, "GET", "/x?y=1", b"{}", h, 1000.5) is None
    assert verify_request(members, "POST", "/x?y=1", b"{}", h, 1301) is None
    assert verify_request(None, "POST", "/x?y=1", b"{}", h, 1000.5) is None


# ---------------------------------------------------------------- log and mail mirror

def test_log_and_mail_endpoints(world):
    w = world
    p = w.push("@bob", w.chain().tip.state, {"app.py": "a = 2\n"})
    c = client(w)
    body = get(c, w, "@bob", "/api/v1/log?since=1&limit=1").json()
    assert len(body["entries"]) == 1 and body["head"]["seq"] == len(w.intake.log) - 1
    entry = decode_entry(body["entries"][0].encode())
    assert entry.seq == 1 and entry.type == "mail"
    r = get(c, w, "@bob", f"/api/v1/mail/{p.id}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("message/rfc822")
    assert mime.to_email(r.content).gud_id == p.id
    assert get(c, w, "@bob", "/api/v1/mail/" + "0" * 64).status_code == 404
    mail = get(c, w, "@bob", "/api/v1/mail?since=0&limit=1").json()
    assert len(mail["mail"]) == 1 and mail["next"] == 2
    assert mime.to_email(base64.b64decode(mail["mail"][0]["raw"])).gud_id == mail["mail"][0]["id"]
    rest = get(c, w, "@bob", "/api/v1/mail?since=2").json()
    assert [m["id"] for m in rest["mail"]] == [p.id] and rest["next"] is None
    blob = w.chain().tip.tree_root
    assert get(c, w, "@bob", f"/api/v1/object/{blob}").status_code == 200
    sha, data = w.intake.snapshot()
    assert get(c, w, "@bob", f"/api/v1/snapshot/{sha}").content == data


def test_log_long_poll_wakes_on_new_entry(world):
    w = world
    c = client(w)
    since = len(w.intake.log)
    out = {}

    def poll():
        t0 = time.monotonic()
        out["body"] = get(c, w, "@bob", f"/api/v1/log?since={since}&wait=5").json()
        out["s"] = time.monotonic() - t0

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(0.2)
    w.task("@nelson", "wake up")
    th.join(10)
    assert len(out["body"]["entries"]) == 1 and out["s"] < 4


# ---------------------------------------------------------------- SSE

def _frames(text: str):
    return [(i, from_json(d.encode())[1]) for i, d in parse_sse(text.splitlines())]


def test_sse_filters_by_member_and_resumes_from_last_event_id(world):
    w = world
    c = client(w)
    g = w.chain().tip.state
    w.push("@bob", g, {"app.py": "a = 2\n"})
    w.push("@carol", g, {"app.py": "a = 3\n"})
    path = "/api/v1/events?member=%40alice&follow=0"
    r = get(c, w, "@alice", path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    frames = _frames(r.text)
    journal = w.dispatcher.journal.since("@alice", 0)
    assert [i for i, _ in frames] == [it.cursor for it in journal] and len(frames) >= 3
    assert all("@alice" in e.concerns for _, e in frames)
    headers = signed(w, "@alice", "GET", path)
    headers["Last-Event-ID"] = "2"
    resumed = _frames(c.get(path, headers=headers).text)
    assert [i for i, _ in resumed] == [it.cursor for it in journal[2:]]
    assert get(c, w, "@bob", path).status_code == 403                    # not your stream


def test_sse_follow_delivers_new_events_live(world):
    w = world
    c = client(w)
    path = "/api/v1/events?member=%40alice&max=3"
    head = w.dispatcher.journal.head("@alice")
    got = []

    def read():
        headers = signed(w, "@alice", "GET", path)
        headers["Last-Event-ID"] = str(head)
        with c.stream("GET", path, headers=headers) as resp:
            for cursor, data in parse_sse(resp.iter_lines()):
                got.append((cursor, from_json(data.encode())[1].type))
                break

    th = threading.Thread(target=read)
    th.start()
    time.sleep(0.3)
    w.push("@bob", w.chain().tip.state, {"app.py": "a = 2\n"})
    th.join(10)
    assert got and got[0][0] == head + 1 and got[0][1] == "review.requested"


def test_event_client_stream_resumes_and_dedupes(world, tmp_path):
    w = world
    c = client(w)
    g = w.chain().tip.state
    w.push("@bob", g, {"app.py": "a = 2\n"})

    class Mirror:
        host_url = "http://testserver"
        private_raw = w.kit.key("@alice")
        http = c

    ec = EventClient(tmp_path / "alice-sse.json", w.intake.host_keys, repo=REPO)
    first = list(ec.stream(Mirror, "@alice", follow=False, clock=w.net.clock))
    assert first and ec.cursor == w.dispatcher.journal.head("@alice")
    w.push("@carol", g, {"app.py": "a = 3\n"})
    ec2 = EventClient(tmp_path / "alice-sse.json", w.intake.host_keys, repo=REPO)    # restarted agent
    second = list(ec2.stream(Mirror, "@alice", follow=False, clock=w.net.clock))
    assert second and not ({e.id for e in first} & {e.id for e in second})
    assert ec2.cursor == w.dispatcher.journal.head("@alice")


# ---------------------------------------------------------------- delivery registration

def test_delivery_registration_validates(world):
    w = world
    c = client(w)

    def post(handle, obj):
        body = json.dumps(obj).encode()
        return c.post("/api/v1/delivery", content=body, headers=signed(w, handle, "POST", "/api/v1/delivery", body))

    assert post("@alice", {"webhook": "http://127.0.0.1:9/hook"}).json()["channel"] == "webhook"
    assert w.host.registry.get("@alice").url == "http://127.0.0.1:9/hook"
    assert post("@alice", {"webhook": "ftp://x"}).status_code == 400
    assert post("@alice", {"nothing": 1}).status_code == 400
    assert post("@alice", {"email": True}).json()["channel"] == "email"


# ---------------------------------------------------------------- admin

def test_admin_endpoints_are_loopback_only(world):
    w = world
    remote = client(w)
    for path in ("/api/v1/admin/tick", "/api/v1/admin/catch-up", "/api/v1/admin/pump", "/api/v1/admin/inject"):
        assert remote.post(path, headers=signed(w, "@nelson", "POST", path)).status_code == 403, path
    local = client(w, loopback=frozenset({"testclient"}))
    w.net.advance(1000)
    assert local.post("/api/v1/admin/tick").json()["ticked"] is True
    env = w.task("@nelson", "injected", send=False).env
    gid = E.gud_id(env)
    raw = mime.build(Outgoing(from_addr=w.kit.address("@nelson"), to=(REPO,), subject="x", envelope=E.encode(env)))
    r = local.post("/api/v1/admin/inject", content=raw).json()
    assert r["logged"] and "task.opened" in r["events"] and w.intake.log.seq_of(gid) is not None
    assert local.get("/api/v1/log").status_code == 200          # loopback reads are trusted


# ---------------------------------------------------------------- provider webhooks

def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class PrimitiveHost:
    """A host whose repo mailbox is Primitive (mocked HTTP); webhooks are built and signed here."""

    def __init__(self, tmp, **kw):
        self.mock = MockPrimitive(**kw)
        self.kit = Kit(config="authserv-id primitive.gud.invalid\n")
        self.kit.now = int(time.time() * 1000)
        self.box = PrimitiveMailbox("test-key", f"repo@{self.mock.domain}", base_url="https://mock.primitive/v1",
                                    client=self.mock.client(), webhook_secret=SECRET, subscribe_wait=1)
        self.host = open_host(tmp / "home", repo=REPO, mailbox=self.box, host_private=self.kit.host_key,
                              persist_replay=False)
        self.app = TestClient(self.host.app(loopback_hosts=frozenset()))
        self.n = 0

    def raw(self, msg) -> bytes:
        env = msg.env
        return mime.build(Outgoing(from_addr=f"agent@{self.mock.domain}", to=(self.box.address,),
                                   subject=mime.subject_for(env, msg.id, env.kind), envelope=E.encode(env)))

    def payload(self, raw: bytes) -> tuple[bytes, dict[str, str]]:
        self.n += 1
        body = json.dumps({"event": "email.received", "id": f"evt_{self.n}", "email": {
            "id": f"row-{self.n}", "received_at": _iso(int(time.time() * 1000)), "recipient": self.box.address,
            "content": {"raw": {"included": True, "encoding": "base64", "data": base64.b64encode(raw).decode()}},
            "auth": {"dmarc": "pass", "dmarcFromDomain": self.mock.org_domain}}}).encode()
        return body, {"Primitive-Signature": signature_header(SECRET, body, int(time.time())),
                      "Content-Type": "application/json"}

    def post(self, body, headers, provider="primitive"):
        return self.app.post(f"/api/v1/webhook/{provider}", content=body, headers=headers)

    def sign(self, name, *a, **kw):
        self.kit.now = max(self.kit.now, int(time.time() * 1000))
        head = self.host.intake.head()
        from gud.core.types import Seen
        return getattr(self.kit, name)(*a, log=False, seen=Seen(head.seq, head.hash), **kw)


def test_primitive_webhooks_dedupe_and_keep_arrival_order(tmp_path):
    ph = PrimitiveHost(tmp_path)
    g = ph.sign("genesis", {"app.py": "a = 1\n"}, author="@alice")
    assert ph.post(*ph.payload(ph.raw(g))).json() == {"accepted": 1}
    t1, t2 = ph.sign("task", "@nelson", "one"), ph.sign("task", "@nelson", "two")
    w2, w1 = ph.payload(ph.raw(t2)), ph.payload(ph.raw(t1))
    assert ph.post(*w2).json() == {"accepted": 1}                # out of order: t2 first
    assert ph.post(*w2).json() == {"accepted": 0}                # duplicate delivery
    assert ph.post(*w1).json() == {"accepted": 1}
    assert ph.post(*ph.payload(ph.raw(t1))).json() == {"accepted": 0}   # a resend with a new provider id
    ids = [gid for _, gid, _ in ph.host.intake.log.mail_ids()]
    assert ids == [g.id, t2.id, t1.id]
    alice = [i.event for i in ph.host.dispatcher.journal.since("@alice", 0) if i.event.type == "task.opened"]
    assert [e.data["task"] for e in alice] == [t2.id, t1.id]


def test_primitive_webhook_rejects_bad_signature_and_unknown_provider(tmp_path):
    ph = PrimitiveHost(tmp_path)
    g = ph.sign("genesis", {"app.py": "a = 1\n"}, author="@alice")
    body, headers = ph.payload(ph.raw(g))
    bad = dict(headers, **{"Primitive-Signature": signature_header("wrong", body, int(time.time()))})
    assert ph.post(body, bad).status_code == 401
    assert ph.post(body, headers, provider="postmark").status_code == 404
    assert ph.post(b"not json", {"Primitive-Signature": signature_header(SECRET, b"not json", int(time.time()))}
                   ).status_code == 400
    assert len(ph.host.intake.log) == 1


def test_registered_verifier_plugs_in(tmp_path):
    ph = PrimitiveHost(tmp_path)

    class Hooky:
        def verify(self, headers, body, now):
            return headers.get("x-ok") == "yes"

        def parse(self, headers, body):
            from gud.transport.base import Received
            return [Received(raw_mime=body, received_at=int(time.time() * 1000), provider_id="h", cursor="")]

    ph.host.intake.register_verifier("hooky", Hooky())
    g = ph.sign("genesis", {"app.py": "a = 1\n"}, author="@alice")
    assert ph.post(ph.raw(g), {"x-ok": "no"}, provider="hooky").status_code == 401
    assert ph.post(ph.raw(g), {"x-ok": "yes"}, provider="hooky").json() == {"accepted": 1}


def test_primitive_webhook_path_latency(tmp_path, capsys):
    """Member sends through Primitive (mock) -> signed webhook -> host -> agent webhook."""
    from tests.v1.events.harness import Receiver

    ph = PrimitiveHost(tmp_path)
    alice = Receiver(tmp_path / "alice.json", ph.host.intake.host_keys, time.time)
    from gud.events.model import Delivery
    ph.host.registry.set(Delivery("@alice", "webhook", alice.url))
    ph.mock.webhook_sink = lambda headers, body: ph.post(body, headers)
    sender = PrimitiveMailbox("test-key", f"agent@{ph.mock.domain}", base_url="https://mock.primitive/v1",
                              client=ph.mock.client(), webhook_secret=SECRET)
    stop = threading.Event()
    ph.host.dispatcher.start(stop)
    try:
        g = ph.sign("genesis", {"app.py": "a = 1\n"}, author="@alice")
        sender.send(ph.raw(g))
        deadline = time.monotonic() + 5
        while ph.host.replayer.tip is None and time.monotonic() < deadline:
            time.sleep(0.005)
        totals = []
        for i in range(3):
            p = ph.sign("push", "@bob", g, {"app.py": f"a = {i + 2}\n"})
            t0 = time.perf_counter()
            sender.send(ph.raw(p))
            deadline = time.monotonic() + 5
            while not [e for e in alice.of("review.requested") if e.data["push"] == p.id]:
                assert time.monotonic() < deadline, "no review.requested within 5 s"
                time.sleep(0.002)
            ev = [e for e in alice.of("review.requested") if e.data["push"] == p.id][0]
            totals.append(alice.arrivals[ev.id] - t0)
    finally:
        stop.set()
        ph.host.dispatcher.wake()
        alice.close()
    with capsys.disabled():
        print("\nprimitive-mock webhook path, send -> agent webhook (ms): "
              + ", ".join(f"{t * 1000:.1f}" for t in totals))
    assert max(totals) < 5.0

