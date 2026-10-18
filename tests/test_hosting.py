"""Mirror access, mail paging, and RemoteMailbox. No network."""

import time

from starlette.testclient import TestClient

from gud import config, sig
from gud.api import Ctx, create_app
from gud.chain import Repo
from gud.config import Config, configured_address
from gud.hostauth import request_bytes, sign_request, verify_bytes, verify_request
from gud.mail_fake import FileMailbox
from gud.mail_remote import RemoteMailbox
from gud.mailbox import open_member_mailbox, open_mailbox
from gud.members import propose_remove
from gud.replay import replay
from gud.testkit import REPO, Pushed, World
from gud.types import Member
from gud.wire import parse_body


def _cfg(tmp_path, **overrides) -> Config:
    fields = dict(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None, instabox_api_key=None,
        armature_api_key=None, openrouter_key=None, agent_model="test",
    )
    fields.update(overrides)
    return Config(**fields)


def _app(tmp_path, world, **ctx_kw):
    cfg = _cfg(tmp_path)
    ctx = Ctx(cfg, mailbox=world.mailbox, clock=time.time, auto_welcome=False, **ctx_kw)
    return cfg, ctx, create_app(ctx)


def _remote(app):
    return TestClient(app, client=("203.0.113.5", 1234))


def _pushed(email) -> Pushed:
    body = parse_body(email.body)
    return Pushed(email, body.get("Gud-State") or "", body.get("Gud-Tree") or "", {})


def test_sign_and_verify_request():
    from gud.compose import Identity
    raw, public = sig.generate()
    ident = Identity("@alice", "alice@gud.local", raw)
    now = 1_700_000_000.0
    path = "/api/mail?since=1&limit=2"
    headers = sign_request(ident, "GET", path, now)
    assert headers["X-Gud-Handle"] == "@alice"
    assert headers["X-Gud-Time"] == "1700000000"
    payload = request_bytes("GET", path, "1700000000")
    assert payload == b"GET /api/mail?since=1&limit=2 1700000000"
    assert verify_bytes(public, payload, headers["X-Gud-Sig"])
    frac = sign_request(ident, "GET", "/api/mail", 1000.5)
    assert frac["X-Gud-Time"] == "1000.5"
    assert verify_bytes(public, request_bytes("GET", "/api/mail", "1000.5"), frac["X-Gud-Sig"])

    alice = Member("@alice", "agent", "alice@gud.local", public, "@nelson")
    members = {"@alice": alice}
    assert verify_request(members, headers, "GET", path, now) == "@alice"
    assert verify_request(members, headers, "GET", path, now + 300) == "@alice"
    assert verify_request(members, headers, "GET", path, now - 300) == "@alice"
    assert verify_request(members, headers, "GET", path, now + 300.001) is None
    assert verify_request(members, headers, "GET", path, now - 301) is None
    assert verify_request(members, headers, "GET", path, now, max_skew=10) == "@alice"
    assert verify_request(members, headers, "GET", path, now + 11, max_skew=10) is None
    assert verify_request(members, headers, "POST", path, now) is None
    assert verify_request(members, headers, "GET", "/api/mail", now) is None
    tampered = dict(headers)
    tampered["X-Gud-Time"] = "1700000001"
    assert verify_request(members, tampered, "GET", path, now) is None
    assert verify_request({}, headers, "GET", path, now) is None
    _, bob_pub = sig.generate()
    bob = Member("@bob", "agent", "bob@gud.local", bob_pub, None)
    assert verify_request({"@bob": bob}, headers, "GET", path, now) is None
    human = Member("@alice", "human", "alice@gud.local", None, None)
    assert verify_request({"@alice": human}, headers, "GET", path, now) is None
    assert verify_request(members, {}, "GET", path, now) is None


def test_config_member_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("GUD_HOME", str(tmp_path))
    monkeypatch.setenv("GUD_DOMAIN", "gud.local")
    monkeypatch.setenv("GUD_HOST_URL", "http://h.example")
    monkeypatch.setenv("GUD_HANDLE", "@ada")
    monkeypatch.setenv("GUD_ADDRESS", "ada@other.test")
    monkeypatch.setenv("GUD_AUDIT_SKEW", "15")
    cfg = config.load()
    assert cfg.host_url == "http://h.example"
    assert cfg.handle == "@ada"
    assert cfg.member_address == "ada@other.test"
    assert configured_address(cfg) == "ada@other.test"
    assert cfg.audit_skew == 15.0
    assert cfg.address("@ada") == "ada@other.test"
    assert cfg.address("ada") == "ada@other.test"
    assert cfg.address("@bob") == "bob@gud.local"
    assert cfg.address_for("@ada") == "ada@gud.local"

    monkeypatch.delenv("GUD_HOST_URL")
    monkeypatch.delenv("GUD_HANDLE")
    monkeypatch.delenv("GUD_ADDRESS")
    monkeypatch.delenv("GUD_AUDIT_SKEW")
    bare = config.load()
    assert bare.host_url is None and bare.handle is None
    assert bare.member_address is None
    assert bare.audit_skew == 120.0
    assert bare.address("@ada") == "ada@gud.local"


def test_primitive_domain_follows_primitive_email(monkeypatch, tmp_path):
    monkeypatch.setenv("GUD_HOME", str(tmp_path))
    monkeypatch.delenv("GUD_DOMAIN", raising=False)
    monkeypatch.delenv("GUD_REPO_ADDRESS", raising=False)
    monkeypatch.delenv("GUD_BUGS_ADDRESS", raising=False)
    monkeypatch.setenv("GUD_MAILBOX", "primitive")
    monkeypatch.setenv("PRIMITIVE_EMAIL", "example@lean-ridge.primitive.email")
    cfg = config.load()
    assert cfg.domain == "lean-ridge.primitive.email"
    assert cfg.repo_address == "repo@lean-ridge.primitive.email"
    assert cfg.bugs_address == "bugs@lean-ridge.primitive.email"

    monkeypatch.setenv("GUD_DOMAIN", "gud.local")
    assert config.load().domain == "gud.local"

    monkeypatch.delenv("GUD_DOMAIN", raising=False)
    monkeypatch.setenv("GUD_MAILBOX", "fake")
    assert config.load().domain == "gud.local"

    monkeypatch.setenv("GUD_MAILBOX", "primitive-webhook")
    monkeypatch.setenv("PRIMITIVE_EMAIL", "")
    assert config.load().domain == "gud.local"


def test_open_member_mailbox_switches_on_host_url(tmp_path):
    cfg = _cfg(tmp_path)
    local = open_member_mailbox(cfg)
    assert isinstance(local, FileMailbox)
    assert isinstance(open_mailbox(cfg), FileMailbox)

    try:
        open_member_mailbox(_cfg(tmp_path, host_url="http://h.example"))
        raised = False
    except RuntimeError as exc:
        raised = "GUD_HANDLE" in str(exc)
    assert raised

    raw, _public = sig.generate()
    member = _cfg(tmp_path, host_url="http://h.example", handle="@ada", member_address="ada@other.test")
    sig.save_private(member.key_path("@ada"), raw)
    remote = open_member_mailbox(member)
    assert isinstance(remote, RemoteMailbox)
    assert remote.host_url == "http://h.example"
    assert remote.identity.handle == "@ada"
    assert remote.identity.address == "ada@other.test"
    assert remote.repo_address == REPO
    assert isinstance(remote.send_via, FileMailbox)


def test_mirror_access_and_info(tmp_path):
    world = World.create(tmp_path / "repo")
    _cfg_obj, ctx, app = _app(tmp_path, world)
    sig.save_private(ctx.cfg.key_path("@console"), world.ids["@console"].private_raw)
    remote = _remote(app)
    local = TestClient(app)

    info = remote.get("/api/info")
    assert info.status_code == 200
    assert info.json() == {"repo": REPO, "window_seconds": 60.0, "tip_seq": None}
    assert remote.get("/api/mail").status_code == 401
    assert remote.get("/api/state").status_code == 401
    assert remote.post("/api/tasks", json={"title": "T", "description": "d", "requester": "@nelson"}).status_code == 403

    world.genesis({"app.py": "x = 1\n"})
    assert remote.get("/api/info").json()["tip_seq"] == 0

    alice = world.ids["@alice"]
    fresh = sign_request(alice, "GET", "/api/mail", time.time())
    assert remote.get("/api/mail", headers=fresh).status_code == 200
    state_headers = sign_request(alice, "GET", "/api/state", time.time())
    assert remote.get("/api/state", headers=state_headers).status_code == 200
    assert remote.get("/api/mail", headers=state_headers).status_code == 401
    stale = sign_request(alice, "GET", "/api/mail", time.time() - 301)
    assert remote.get("/api/mail", headers=stale).status_code == 401
    outsider_raw, _outsider_pub = sig.generate()
    from gud.compose import Identity
    outsider = Identity("@outsider", "outsider@their.domain", outsider_raw)
    forged = sign_request(outsider, "GET", "/api/mail", time.time())
    assert remote.get("/api/mail", headers=forged).status_code == 401
    signed_post = sign_request(alice, "POST", "/api/tasks", time.time())
    assert remote.post(
        "/api/tasks", headers=signed_post, json={"title": "T", "description": "d", "requester": "@nelson"},
    ).status_code == 403

    assert local.get("/api/mail").status_code == 200
    assert local.post("/api/tasks", json={"title": "T", "description": "d", "requester": "@nelson"}).status_code == 200

    ctx.loopback_hosts = set()
    assert local.get("/api/mail").status_code == 401
    assert local.get("/api/info").status_code == 200
    ctx.loopback_hosts = {"127.0.0.1", "::1", "testclient"}
    assert local.get("/api/mail").status_code == 200


def test_removed_member_loses_mirror(tmp_path):
    world = World.create(tmp_path / "repo")
    world.genesis({"app.py": "x = 1\n"})
    _cfg_obj, _ctx, app = _app(tmp_path, world)
    remote = _remote(app)
    alice = world.ids["@alice"]
    assert remote.get("/api/mail", headers=sign_request(alice, "GET", "/api/mail", time.time())).status_code == 200

    now = time.time()
    chain = replay(world.repo_mail(), 60, now)
    removal = propose_remove(world.mailbox, world.ids["@lead"], chain.tip, "@alice", reason="left")
    removal_push = _pushed(removal)
    world.human("@nelson", removal_push, "approve")
    world.test(removal_push)
    chain = replay(world.repo_mail(), 60, time.time())
    assert chain.tip is not None and chain.tip.message_id == removal.message_id, (
        f"remove did not land; proposals="
        f"{[(p.subject, p.status, p.reason) for p in chain.proposals.values()]}"
    )
    assert "@alice" not in chain.tip.members

    assert remote.get("/api/mail", headers=sign_request(alice, "GET", "/api/mail", time.time())).status_code == 401
    lead = world.ids["@lead"]
    assert remote.get("/api/state", headers=sign_request(lead, "GET", "/api/state", time.time())).status_code == 200


def test_mail_paging(tmp_path):
    world = World.create(tmp_path / "repo")
    genesis = world.genesis({"app.py": "x = 1\n"})
    edited = world.edit("@alice", genesis, "app.py", "x = 2\n", intent="bump")
    world.test(edited)
    _cfg_obj, _ctx, app = _app(tmp_path, world)
    local = TestClient(app)
    full = local.get("/api/mail").json()
    assert full["next_since"] is None
    ids = [row["message_id"] for row in full["emails"]]
    assert ids == [email.message_id for email in world.repo_mail()]
    assert ids == sorted(ids, key=lambda mid: next(e.received_at for e in world.repo_mail() if e.message_id == mid))
    assert [row["received_at"] for row in full["emails"]] == sorted(row["received_at"] for row in full["emails"])

    collected = []
    since = None
    pages = 0
    while True:
        params = {"limit": 2}
        if since is not None:
            params["since"] = since
        page = local.get("/api/mail", params=params).json()
        pages += 1
        collected.extend(page["emails"])
        if page["next_since"] is None:
            assert len(page["emails"]) < 2
            break
        assert len(page["emails"]) == 2
        assert page["next_since"] == page["emails"][-1]["received_at"]
        since = page["next_since"]
        assert pages < 10
    assert [row["message_id"] for row in collected] == ids
    first = full["emails"][0]["received_at"]
    after = local.get("/api/mail", params={"since": first}).json()
    assert [row["message_id"] for row in after["emails"]] == ids[1:]
    assert all(row["received_at"] > first for row in after["emails"])


def test_remote_mailbox_replays_like_the_host(tmp_path):
    world = World.create(tmp_path / "repo")
    genesis = world.genesis({"app.py": "x = 1\n"})
    edited = world.edit("@alice", genesis, "app.py", "x = 2\n", intent="bump")
    world.test(edited)
    _cfg_obj, _ctx, app = _app(tmp_path, world)
    client = _remote(app)
    remote = RemoteMailbox("http://testserver", world.ids["@alice"], world.mailbox, REPO, client=client)
    remote.page_limit = 1

    mirrored = remote.list()
    host_mail = world.repo_mail()
    assert [(e.message_id, e.body, e.received_at, e.sender_verified, e.from_addr, list(e.to)) for e in mirrored] == [
        (e.message_id, e.body, e.received_at, e.sender_verified, e.from_addr, list(e.to)) for e in host_mail
    ]
    assert remote.list(recipient="nobody@example") == mirrored
    assert [e.message_id for e in remote.list(since=mirrored[0].received_at)] == [
        e.message_id for e in mirrored[1:]
    ]

    now = time.time()
    host = Repo(world.mailbox, 60, clock=lambda: now).refresh()
    member = replay(remote.list(), 60, now)
    assert host.tip is not None and member.tip is not None
    assert host.tip.state == member.tip.state
    assert host.tip.tree_hash == member.tip.tree_hash
    assert host.tip.seq == member.tip.seq

    sent = remote.send(from_addr=world.ids["@alice"].address, to=REPO, subject="[gud] ping", body="hello\n")
    assert any(email.message_id == sent.message_id for email in world.repo_mail())
    assert any(email.message_id == sent.message_id for email in remote.list())
    again = replay(remote.list(), 60, time.time())
    assert again.tip.state == host.tip.state

    ready = remote.wait(recipient="ignored", since=0, timeout=2)
    assert ready and ready[0].message_id == mirrored[0].message_id
    started = time.monotonic()
    assert remote.wait(recipient="ignored", since=1e18, timeout=0.3) == []
    assert time.monotonic() - started < 2
