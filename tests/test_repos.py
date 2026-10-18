"""The GUI lists genesis addresses in one mailbox and switches among them."""

from urllib.parse import quote

import httpx
from starlette.testclient import TestClient

from gud.api import Ctx, create_app
from gud.config import Config
from gud.open import catalog_addresses
from gud.repos import genesis_addresses
from gud.testkit import REPO, World

OTHER = "repo-other@gud.local"


def _cfg(tmp_path, **overrides) -> Config:
    fields = dict(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None, instabox_api_key=None,
        armature_api_key=None, openrouter_key=None, agent_model="test",
    )
    fields.update(overrides)
    return Config(**fields)


def _two_repos(tmp_path):
    world = World.create(tmp_path / "mail")
    world.genesis({"app.py": "x = 1\n"})
    world.mailbox.send(from_addr="lead@gud.local", to="noise@gud.local", subject="hello", body="no")
    world.tick(10)
    world.mailbox.repo_address = OTHER
    second = world.genesis({"other.py": "y = 2\n"})
    cfg = _cfg(tmp_path)
    ctx = Ctx(cfg, mailbox=world.mailbox, clock=world.clock, auto_welcome=False, start_welcome_thread=False)
    return world, second, TestClient(create_app(ctx))


def test_catalog_keeps_named_repos_and_drops_delivery_copies():
    rows = [
        {"subject": "[gud] genesis", "received_at": 2, "to": ["Acme@gud.local"], "recipient": "lead@gud.local"},
        {"subject": "[gud] genesis", "received_at": 1, "recipient": "sub-1@gud.local"},
        {"subject": "[gud] genesis", "received_at": 3, "to": ["repo@gud.local", "also@gud.local"]},
        {"subject": "not genesis", "received_at": 9, "to": ["other@gud.local"]},
    ]
    assert catalog_addresses(rows) == ["also@gud.local", REPO, "acme@gud.local"]


def test_link_diff_returns_the_push_and_404s_an_unknown_id(tmp_path):
    _world, second, client = _two_repos(tmp_path)
    other = client.get("/api/links/" + quote(second.id, safe="") + "/diff")
    assert other.status_code == 200, other.text
    assert other.json()["id"] == second.id
    assert "diff --git a/other.py b/other.py" in other.json()["diff"]
    assert "+y = 2" in other.json()["diff"]

    switched = client.post("/api/repo", json={"address": REPO})
    assert switched.status_code == 200
    tip = client.get("/api/state").json()["tip"]
    got = client.get("/api/links/" + quote(tip["id"], safe="") + "/diff")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["id"] == tip["id"]
    assert "diff --git a/.gud/members b/.gud/members" in body["diff"]
    assert "diff --git a/app.py b/app.py" in body["diff"]
    assert "+x = 1" in body["diff"]
    assert tip["diffstat"]["files"] == [".gud/members", "app.py"]
    assert tip["diffstat"]["added"] >= 1

    missing = client.get("/api/links/" + quote("<missing@gud.local>", safe="") + "/diff")
    assert missing.status_code == 404


def test_genesis_addresses_drop_contributor_names():
    rows = [
        {"subject": "[gud] genesis", "received_at": 2, "to": ["repo-20261017@gud.local"], "recipient": "lead@gud.local"},
        {"subject": "[gud] genesis", "received_at": 2, "recipient": "auth-agent@gud.local"},
        {"subject": "[gud] genesis", "received_at": 1, "to": [REPO], "cc": ["sub-1@gud.local", "nelson@gud.local"]},
        {"subject": "[gud] genesis", "received_at": 3, "recipient": "tester@gud.local"},
    ]
    assert genesis_addresses(rows) == ["repo-20261017@gud.local", REPO]


def test_saved_repo_stays_selected_when_the_scan_misses_it(tmp_path):
    world = World.create(tmp_path / "mail")
    world.genesis({"app.py": "x = 1\n"})
    cfg = _cfg(tmp_path)
    cfg.home.mkdir(parents=True)
    (cfg.home / "repo.txt").write_text("repo-new@gud.local\n")
    world.mailbox.repo_address = "repo-new@gud.local"
    ctx = Ctx(cfg, mailbox=world.mailbox, clock=world.clock, auto_welcome=False, start_welcome_thread=False)
    client = TestClient(create_app(ctx))

    body = client.get("/api/repos").json()
    assert body["active"] == "repo-new@gud.local"
    assert body["repos"][0] == "repo-new@gud.local"
    assert REPO in body["repos"]
    assert client.get("/api/state").json()["repo"] == "repo-new@gud.local"
    assert world.mailbox.repo_address == "repo-new@gud.local"


def test_list_keeps_genesis_addresses_and_switch_changes_state(tmp_path):
    _world, second, client = _two_repos(tmp_path)
    listed = client.get("/api/repos")
    assert listed.status_code == 200
    body = listed.json()
    assert body["needs_token"] is False
    assert body["repos"] == [OTHER, REPO]
    assert body["active"] == OTHER
    assert "alice@gud.local" not in body["repos"]
    assert "noise@gud.local" not in body["repos"]

    state = client.get("/api/state").json()
    assert state["repo"] == OTHER
    assert state["tip"]["id"] == second.id

    switched = client.post("/api/repo", json={"address": REPO})
    assert switched.status_code == 200
    after = client.get("/api/state").json()
    assert after["repo"] == REPO
    assert after["tip"]["id"] != second.id
    assert (tmp_path / "home" / "repo.txt").read_text().strip() == REPO
    assert client.post("/api/repo", json={"address": "noise@gud.local"}).status_code == 404


def test_rejected_primitive_token_is_not_stored(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})

    cfg = _cfg(tmp_path, mailbox="primitive")
    ctx = Ctx(cfg, auto_welcome=False, start_welcome_thread=False)
    ctx.token_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TestClient(create_app(ctx))

    response = client.post("/api/mailbox", json={"token": "prim_bad"})
    assert response.status_code == 401
    assert not (tmp_path / "home" / "mailbox.token").exists()
    assert client.get("/api/repos").json()["needs_token"] is True


def test_accepted_primitive_token_is_stored_mode_0600(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": [], "meta": {}})

    cfg = _cfg(tmp_path, mailbox="primitive")
    ctx = Ctx(cfg, auto_welcome=False, start_welcome_thread=False)
    ctx.token_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TestClient(create_app(ctx))

    response = client.post("/api/mailbox", json={"token": "prim_ok"})
    assert response.status_code == 200
    assert response.json()["needs_token"] is False
    path = tmp_path / "home" / "mailbox.token"
    assert path.read_text().strip() == "prim_ok"
    assert path.stat().st_mode & 0o777 == 0o600
