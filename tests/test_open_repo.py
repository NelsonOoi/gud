"""GUI genesis, agent provisioning, and the optional in-process swarm."""

import base64

from starlette.testclient import TestClient

from gud import config, sig
from gud.api import Ctx, create_app
from gud.config import Config, parse_swarm, resolved_swarm
from gud.mail_fake import FileMailbox
from gud.memberfile import MEMBERS

REPO = "repo@gud.local"


def _cfg(tmp_path, **overrides) -> Config:
    fields = dict(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None, instabox_api_key=None,
        armature_api_key=None, openrouter_key=None, agent_model="test",
    )
    fields.update(overrides)
    return Config(**fields)


def _app(tmp_path, **overrides):
    cfg = _cfg(tmp_path, **overrides)
    mailbox = FileMailbox(tmp_path / "mailbox.jsonl", cfg.repo_address, cfg.domain)
    ctx = Ctx(cfg, mailbox=mailbox, auto_welcome=False, start_welcome_thread=False)
    return ctx, TestClient(create_app(ctx))


def test_empty_humans_is_400(tmp_path):
    _ctx, client = _app(tmp_path)
    response = client.post("/api/repos", json={"name": "acme", "humans": []})
    assert response.status_code == 400
    spaced = client.post("/api/repos", json={"name": "my repo", "humans": ["ada@example.com"]})
    assert spaced.status_code == 400


def test_create_writes_genesis_and_duplicate_name_is_409(tmp_path):
    ctx, client = _app(tmp_path)
    raw, public = sig.generate()
    sig.save_private(ctx.cfg.key_path("@lead"), raw)
    before = ctx.cfg.key_path("@lead").read_bytes()

    created = client.post("/api/repos", json={
        "name": "Acme",
        "humans": ["Ada@Example.com", "ada@other.test"],
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["needs_token"] is False
    assert body["active"] == "repo-acme@gud.local"
    assert "repo-acme@gud.local" in body["repos"]
    assert (ctx.cfg.home / "repo.txt").read_text().strip() == "repo-acme@gud.local"
    assert ctx.cfg.key_path("@lead").read_bytes() == before
    assert not (ctx.cfg.home / "swarm.txt").exists()
    assert ctx.swarm_enabled is False

    chain = ctx.repo.refresh()
    tip = chain.tip
    assert tip is not None
    assert tip.author == "@lead"
    assert set(tip.tree) == {MEMBERS}
    members = tip.members
    assert members["@lead"].role == "lead"
    assert members["@lead"].public_key == public
    assert members["@lead"].sponsor == "@ada"
    assert members["@tester"].role == "tester"
    assert members["@console"].role == "console"
    assert members["@sub-1"].role == "agent"
    humans = {member.handle: member for member in members.values() if member.role == "human"}
    assert set(humans) == {"@ada", "@ada-2"}
    assert humans["@ada"].address == "ada@example.com"
    assert humans["@ada"].public_key is None
    assert humans["@ada"].sponsor is None
    assert humans["@ada-2"].address == "ada@other.test"
    listed = client.get("/api/repos").json()
    assert listed["active"] == "repo-acme@gud.local"
    assert "repo-acme@gud.local" in listed["repos"]

    again = client.post("/api/repos", json={"name": "acme", "humans": ["ada@example.com"]})
    assert again.status_code == 409
    prefixed = client.post("/api/repos", json={"name": "repo-acme", "humans": ["ada@example.com"]})
    assert prefixed.status_code == 409
    bare = client.post("/api/repos", json={"name": "repo", "humans": ["ada@example.com"]})
    assert bare.status_code == 200, bare.text
    assert bare.json()["active"] == "repo@gud.local"
    assert ctx.cfg.key_path("@lead").read_bytes() == before
    mail = ctx.mailbox.list(recipient="repo-acme@gud.local")
    genesis = [email for email in mail if email.subject == "[gud] genesis"]
    assert len(genesis) == 1
    assert "ada@example.com" not in genesis[0].cc
    notes = [
        email for email in ctx.mailbox.list(recipient="ada@example.com")
        if email.to == ("ada@example.com",)
    ]
    assert [email.subject for email in notes] == ["[gud] repo-acme@gud.local", "[gud] repo@gud.local"]
    assert notes[0].cc == ("repo-acme@gud.local",)


def test_gud_swarm_unset_is_false(monkeypatch, tmp_path):
    monkeypatch.delenv("GUD_SWARM", raising=False)
    monkeypatch.setenv("GUD_HOME", str(tmp_path))
    assert config.load().swarm is False
    assert parse_swarm(None) is False
    for raw in ("", "0", "false", "no", "off", "FALSE", "No"):
        assert parse_swarm(raw) is False
    for raw in ("1", "true", "yes", "on", " YES ", "On"):
        assert parse_swarm(raw) is True

    cfg = _cfg(tmp_path, swarm=True)
    cfg.home.mkdir(parents=True, exist_ok=True)
    assert resolved_swarm(cfg) is True
    (cfg.home / "swarm.txt").write_text("off\n")
    assert resolved_swarm(cfg) is False
    (cfg.home / "swarm.txt").write_text("on\n")
    assert resolved_swarm(_cfg(tmp_path, swarm=False)) is True


def test_off_file_does_not_start_and_enable_without_genesis_is_409(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("swarm started")

    monkeypatch.setattr("gud.swarm.Swarm", boom)
    cfg = _cfg(tmp_path, swarm=True)
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "swarm.txt").write_text("off\n")
    mailbox = FileMailbox(tmp_path / "mailbox.jsonl", cfg.repo_address, cfg.domain)
    ctx = Ctx(cfg, mailbox=mailbox, auto_welcome=False, start_welcome_thread=False)
    client = TestClient(create_app(ctx))
    assert client.get("/api/swarm").json() == {"enabled": False}
    monkeypatch.undo()

    fresh, fresh_client = _app(tmp_path / "other")
    denied = fresh_client.post("/api/swarm", json={"enabled": True})
    assert denied.status_code == 409
    assert "genesis" in denied.json()["detail"]
    marker = fresh.cfg.home / "swarm.txt"
    assert not marker.exists() or marker.read_text().strip() != "on"
    assert fresh_client.get("/api/swarm").json()["enabled"] is False
    stopped = fresh_client.post("/api/swarm", json={"enabled": False})
    assert stopped.status_code == 200
    assert stopped.json() == {"enabled": False}
    assert marker.read_text().strip() == "off"


def test_boot_respects_file_and_missing_genesis_stays_off(tmp_path, monkeypatch):
    seen = []

    class FakeSwarm:
        def __init__(self, cfg, mailbox, mode="scripted", workers=1, clock=None, genesis_timeout=0):
            seen.append({
                "mode": mode, "workers": workers, "genesis_timeout": genesis_timeout, "mailbox": mailbox,
            })

        def start(self):
            seen.append("start")

        def stop(self):
            seen.append("stop")

    monkeypatch.setattr("gud.swarm.Swarm", FakeSwarm)
    cfg = _cfg(tmp_path, swarm=False, openrouter_key="present")
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "swarm.txt").write_text("on\n")
    mailbox = FileMailbox(tmp_path / "mailbox.jsonl", cfg.repo_address, cfg.domain)
    ctx = Ctx(cfg, mailbox=mailbox, auto_welcome=False, start_welcome_thread=False)
    client = TestClient(create_app(ctx))
    assert seen[0]["mode"] == "llm"
    assert seen[0]["workers"] == 1
    assert seen[0]["genesis_timeout"] == 0
    assert seen[0]["mailbox"] is mailbox
    assert "start" in seen
    assert client.get("/api/swarm").json() == {"enabled": True}
    stopped = client.post("/api/swarm", json={"enabled": False})
    assert stopped.status_code == 200
    assert stopped.json() == {"enabled": False}
    assert "stop" in seen
    assert (cfg.home / "swarm.txt").read_text().strip() == "off"
    monkeypatch.undo()

    bare = _cfg(tmp_path / "bare", swarm=False)
    bare.home.mkdir(parents=True, exist_ok=True)
    (bare.home / "swarm.txt").write_text("on\n")
    bare_box = FileMailbox(tmp_path / "bare-mail.jsonl", bare.repo_address, bare.domain)
    bare_ctx = Ctx(bare, mailbox=bare_box, auto_welcome=False, start_welcome_thread=False)
    bare_client = TestClient(create_app(bare_ctx))
    assert bare_client.get("/api/swarm").json() == {"enabled": False}
    assert (bare.home / "swarm.txt").read_text().strip() == "on"


def test_provision_reuses_key_and_returns_private_key(tmp_path):
    ctx, client = _app(tmp_path)
    missing = client.post("/api/agents", json={"handle": "extra"})
    assert missing.status_code == 409
    assert "genesis" in missing.json()["detail"]

    opened = client.post("/api/repos", json={"name": "acme", "humans": ["nelson@example.com"]})
    assert opened.status_code == 200, opened.text

    raw, public = sig.generate()
    path = ctx.cfg.key_path("@extra")
    sig.save_private(path, raw)
    before = path.read_text()

    response = client.post("/api/agents", json={"handle": "extra", "role": "agent"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["handle"] == "@extra"
    assert body["address"] == "extra@gud.local"
    assert body["public_key"] == public
    assert body["private_key"] == base64.b64encode(raw).decode()
    assert path.read_text() == before
    assert base64.b64decode(body["private_key"]) == raw
    tip = ctx.repo.refresh().tip
    assert tip.members["@extra"].public_key == public
