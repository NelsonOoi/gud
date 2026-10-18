"""The hosted feed: three tools, a ui:// widget, and an Ed25519 key on writes."""

from __future__ import annotations

import asyncio
import base64

from gud import sig
from gud.config import Config
from gud.console import Console
from gud.mcp_app import APP_MIME, WIDGET_URI, build_server, default_console
from gud.testkit import REPO, World
from gud.wire import parse_body


def _server(w: World, tmp_path, token: str | None = None):
    cfg = Config(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None, instabox_api_key=None,
        armature_api_key=None, openrouter_key=None, agent_model="test",
        mcp_token=token,
    )
    console = Console(cfg, mailbox=w.mailbox, clock=w.clock, identity=w.ids["@console"], refresh_seconds=0)
    return build_server(console)


def _text(result) -> str:
    return result.content[0].text


def _data(result):
    return getattr(result, "structured_content", None) if getattr(result, "structured_content", None) is not None else getattr(result, "structuredContent", None)


def _error(result) -> bool:
    flag = getattr(result, "is_error", None)
    if flag is None:
        flag = getattr(result, "isError", False)
    return bool(flag)


def _field(item, snake: str, camel: str):
    """mcp 2 names fields in snake_case; mcp 1 (pulled in by mcp-use) uses camelCase."""
    value = getattr(item, snake, None)
    return value if value is not None else getattr(item, camel)


def _meta(tool) -> dict:
    meta = getattr(tool, "meta", None)
    if meta:
        return meta
    dumped = tool.model_dump(by_alias=True)
    return dumped.get("_meta") or {}


def test_default_console_opens_the_repo_the_gui_saved(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "repo.txt").write_text("repo-test3@lean-ridge.primitive.email\n")
    monkeypatch.setenv("GUD_HOME", str(home))
    monkeypatch.setenv("GUD_MAILBOX", "fake")
    monkeypatch.delenv("GUD_REPO_ADDRESS", raising=False)
    console = default_console()
    assert console.mailbox.repo_address == "repo-test3@lean-ridge.primitive.email"


def test_server_lists_exactly_the_three_feed_tools(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "x = 1\n"})
    tools = asyncio.run(_server(w, tmp_path).list_tools())
    assert sorted(tool.name for tool in tools) == ["gud", "gud_decide", "gud_push", "gud_task"]
    gud = next(tool for tool in tools if tool.name == "gud")
    assert _meta(gud)["ui"]["resourceUri"] == WIDGET_URI
    props = _field(gud, "input_schema", "inputSchema")["properties"]
    assert "since" in props and "focus" in props
    for name in ("gud", "gud_decide", "gud_push", "gud_task"):
        tool = next(item for item in tools if item.name == name)
        fields = _field(tool, "input_schema", "inputSchema")["properties"]
        assert {"handle", "repo", "private_key", "token"} <= set(fields)


def test_widget_resource_uses_the_mcp_app_mime(tmp_path):
    w = World.create(tmp_path)
    server = _server(w, tmp_path)
    listed = asyncio.run(server.list_resources())
    uris = {str(item.uri) for item in listed}
    assert WIDGET_URI in uris
    found = next(item for item in listed if str(item.uri) == WIDGET_URI)
    assert _field(found, "mime_type", "mimeType") == APP_MIME
    contents = list(asyncio.run(server.read_resource(WIDGET_URI)))
    assert contents[0].mime_type == APP_MIME
    html = contents[0].content
    assert isinstance(html, str)
    assert "<" in html and "gud" in html.lower()


def test_gud_text_fallback_lists_needs_you_ids(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "def f():\n    return 1\n"})
    tree = dict(parent.tree)
    tree["app.py"] = "def f():\n    return 2\n"
    pushed = w.push("@alice", parent=parent, tree=tree, intent="return 2")
    bug = w.mailbox.send(
        from_addr="sam@acme.example", to="bugs@gud.local", subject="Save crashes", body="It throws.\n",
    )
    result = asyncio.run(_server(w, tmp_path).call_tool("gud", {}))
    assert not _error(result)
    text = _text(result)
    assert pushed.id in text
    assert bug.message_id in text
    assert "approve" in text and "object" in text and "veto" in text
    data = _data(result)
    assert data["tip"]["seq"] == 0
    assert any(item["id"] == bug.message_id for item in data["needs_you"])


def _key(w: World, handle: str) -> str:
    return base64.b64encode(w.ids[handle].private_raw).decode()


def test_writes_require_the_ed25519_key_and_repo_selects_the_inbox(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "x = 1\n"})
    server = _server(w, tmp_path, token="secret")
    missing = asyncio.run(server.call_tool("gud_task", {"title": "Ship it", "description": "now"}))
    assert _error(missing)
    assert "ed25519" in _text(missing).lower()
    wrong = asyncio.run(server.call_tool("gud_decide", {"target": "<nope>", "verdict": "approve", "token": "nope"}))
    assert _error(wrong)
    assert "ed25519" in _text(wrong).lower()

    key = _key(w, "@tester")
    opened = asyncio.run(server.call_tool("gud_task", {
        "title": "Ship it", "description": "now", "handle": "@tester", "private_key": key,
    }))
    assert not _error(opened)
    assert _text(opened) == "T1"
    email = next(item for item in w.repo_mail() if parse_body(item.body).get("Gud-Task") == "T1")
    parsed = parse_body(email.body)
    assert parsed.get("Gud-Author") == "@tester"
    assert parsed.get("Gud-Requester") == "@tester"
    assert email.from_addr == w.members["@tester"].address
    assert sig.verify(w.members["@tester"].public_key, parsed.trailers, parsed.diff)

    aliased = asyncio.run(server.call_tool("gud_task", {"title": "Again", "description": "now", "token": key}))
    assert not _error(aliased)
    assert _text(aliased) == "T2"
    second = next(item for item in w.repo_mail() if parse_body(item.body).get("Gud-Task") == "T2")
    assert parse_body(second.body).get("Gud-Author") == "@tester"

    pushed = asyncio.run(server.call_tool("gud_push", {
        "intent": "set x",
        "edits": [{"path": "app.py", "old": "x = 1\n", "new": "x = 2\n"}],
        "handle": "@alice",
        "private_key": _key(w, "@alice"),
    }))
    assert not _error(pushed)
    push_id = _text(pushed).splitlines()[0]
    push = next(item for item in w.repo_mail() if item.message_id == push_id)
    push_body = parse_body(push.body)
    assert push_body.kind == "push"
    assert push_body.get("Gud-Author") == "@alice"
    assert sig.verify(w.members["@alice"].public_key, push_body.trailers, push_body.diff)

    listed = asyncio.run(server.call_tool("gud", {"repo": "other@gud.local", "focus": "app.py"}))
    assert not _error(listed)
    assert w.mailbox.repo_address == "other@gud.local"
