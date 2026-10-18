"""GudTools against the fake mailbox. Core tests skip until replay/chain/rules exist."""

from __future__ import annotations

import asyncio

import pytest

from gud import compose, sig
from gud.mcp_server import TOOL_NAMES, GudTools, build_server, three_way
from gud.testkit import World, parsed
from gud.wire import normalize, parse_body


def _core():
    pytest.importorskip("gud.replay")
    pytest.importorskip("gud.chain")
    pytest.importorskip("gud.rules")


def _skip_if_stub(exc: BaseException):
    if isinstance(exc, NotImplementedError):
        pytest.skip("gud.replay not implemented yet")
    raise exc


def bootstrap(w: World, files: dict, author: str = "@lead"):
    _core()
    try:
        genesis = w.genesis(files, author=author)
        w.test(genesis)
        for handle, ident in w.ids.items():
            member = w.members.get(handle)
            if handle != author and ident.private_raw and member and member.votes:
                w.approve(handle, genesis)
        w.tick(120)
        alice = GudTools(w.mailbox, w.ids["@alice"], window_seconds=60, clock=w.clock)
        alice.status()
    except NotImplementedError as exc:
        _skip_if_stub(exc)
    return genesis


def _pass(w: World, message_id: str):
    email = next(item for item in w.repo_mail() if item.message_id == message_id)
    body = parse_body(email.body)
    blob = compose.test_body(w.ids["@tester"], message_id, True, body.get("Gud-Tree") or "", "ok")
    compose.send(w.mailbox, w.ids["@tester"], w.members, f"Re: test {message_id}", blob, in_reply_to=message_id)


def _tools(w: World, handle: str, window: float = 60) -> GudTools:
    return GudTools(w.mailbox, w.ids[handle], window_seconds=window, clock=w.clock)


def test_three_way_merges_disjoint_lines_and_reports_overlap():
    base = {"calc.py": "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"}
    mine = {"calc.py": "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b * 2\n"}
    tip = {"calc.py": "def add(a, b):\n    return a + b + 1\n\ndef mul(a, b):\n    return a * b\n"}
    merged, conflicts = three_way(base, mine, tip)
    assert conflicts == []
    assert "return a + b + 1" in merged["calc.py"]
    assert "return a * b * 2" in merged["calc.py"]
    clash, conflicts = three_way(
        {"app.py": "value = 1\n"},
        {"app.py": "value = 2\n"},
        {"app.py": "value = 3\n"},
    )
    assert conflicts and "app.py" in conflicts[0]
    assert clash["app.py"] == "value = 3\n"


def test_push_rejects_escape_and_binary(tmp_path):
    w = World.create(tmp_path)
    alice = _tools(w, "@alice")
    assert alice.push("x", [{"path": "../secret", "content": "no\n"}]).startswith("error:")
    assert "binary" in alice.push("x", [{"path": "a.txt", "content": "a\x00b"}])
    assert alice.push("x", []).startswith("error:")
    assert alice.push("x", [{"path": "/etc/passwd", "content": "no\n"}]).startswith("error:")


def test_server_lists_gud_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("ARMATURE_API_KEY", raising=False)
    w = World.create(tmp_path)
    server = build_server(_tools(w, "@alice"))
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    read = next(t for t in tools if t.name == "read")
    push = next(t for t in tools if t.name == "push")
    read_props = (getattr(read, "input_schema", None) or read.inputSchema)["properties"]
    push_props = (getattr(push, "input_schema", None) or push.inputSchema)["properties"]
    assert "path" in read_props
    assert "self" not in read_props
    assert "edits" in push_props
    assert "wait" in (push.description or "").lower() or "full" in (push.description or "").lower()


def test_status_on_fresh_repo(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "def f():\n    return 1\n"})
    text = _tools(w, "@alice").status()
    assert text.startswith("tip ")
    assert "@lead" in text
    assert "mine: none" in text
    assert "subtasks: none" in text


def test_read_ls_blame(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "def f():\n    return 1\n", "src/util.py": "X = 1\n"})
    alice = _tools(w, "@alice")
    assert "return 1" in alice.read("app.py")
    listing = alice.ls()
    assert "app.py" in listing.splitlines()
    assert ".gud/members" in listing.splitlines()
    assert alice.ls("src/") == "src/util.py"
    blame = alice.blame("app.py")
    assert "@lead" in blame
    assert blame.splitlines()[0].endswith("@lead")
    assert alice.read("missing.py").startswith("error:")


def test_push_new_file_has_no_affected_owners(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "def f():\n    return 1\n"})
    out = _tools(w, "@alice").push("add readme", [{"path": "README.md", "content": "hi\n"}])
    assert out.splitlines()[0].startswith("<")
    assert "no affected owners" in out
    assert "affected: none" in out


def test_push_rewriting_bob_is_lazy_consensus(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "def f():\n    return 1\n"}, author="@bob")
    out = _tools(w, "@alice").push(
        "return 2", [{"path": "app.py", "content": "def f():\n    return 2\n"}],
    )
    assert "@bob" in out
    assert "lazy consensus" in out
    assert "unless @bob objects" in out


def test_reply_approve_and_object_are_signed(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "def f():\n    return 1\n"}, author="@bob")
    alice, bob = _tools(w, "@alice"), _tools(w, "@bob")
    pushed = alice.push("return 2", [{"path": "app.py", "content": "def f():\n    return 2\n"}])
    target = pushed.splitlines()[0]
    assert bob.reply(target, "approve", "lgtm").startswith("approve ")
    assert bob.reply(target, "object", "not yet").startswith("object ")
    assert bob.reply(target, "dance").startswith("error:")
    kinds = []
    for email in w.repo_mail():
        body = parsed(email)
        if body.kind in {"approve", "object"} and body.get("Gud-Target") == target:
            kinds.append(body.kind)
            assert sig.verify(w.members["@bob"].public_key, body.trailers, body.diff)
            assert body.get("Gud-Author") == "@bob"
    assert kinds == ["approve", "object"]


def test_tasks_parsing(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"lib.py": "def ready():\n    return True\n"})
    task = compose.task_body(w.ids["@console"], "T3", "@nelson", "helpers")
    compose.send(w.mailbox, w.ids["@console"], w.members, "helpers", task)
    sub = compose.subtask_body(w.ids["@lead"], "T3.1", "T3", "@alice", "add sub()")
    compose.send(w.mailbox, w.ids["@lead"], w.members, "add sub()", sub)
    alice = _tools(w, "@alice")
    listing = alice.tasks()
    assert "T3" in listing and "T3.1" in listing and "@alice" in listing
    assert "open" in listing
    assert "T3.1" in alice.status()
    other = _tools(w, "@bob").status()
    assert "subtasks: none" in other


def _landed_ids(tools: GudTools) -> set[str]:
    chain = tools.repo().refresh()
    return {link.message_id for link in chain.links}


def test_rebase_after_superseded(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"keep.py": "keep = 1\n"})
    alice, bob = _tools(w, "@alice"), _tools(w, "@bob")
    id_a = alice.push("add A", [{"path": "A.txt", "content": "A\n"}]).splitlines()[0]
    id_b = bob.push("add B", [{"path": "B.txt", "content": "B\n"}]).splitlines()[0]
    _pass(w, id_a)
    _pass(w, id_b)
    w.tick(5)
    landed = _landed_ids(alice)
    assert id_a in landed or id_b in landed, alice.status()
    loser_id, loser = (id_b, bob) if id_a in landed else (id_a, alice)
    assert loser_id not in landed, alice.status()
    reb = loser.rebase(loser_id)
    assert not reb.startswith("error:"), reb
    assert not reb.startswith("conflict"), reb
    new_id = reb.splitlines()[0]
    _pass(w, new_id)
    w.tick(5)
    tip = alice.repo().refresh().tip
    assert tip is not None
    assert normalize(tip.tree["A.txt"]) == "A\n"
    assert normalize(tip.tree["B.txt"]) == "B\n"
    assert loser.wait(new_id, timeout=1).startswith("landed ")


def test_rebase_reports_conflict_on_same_lines(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "value = 1\n"})
    alice, bob = _tools(w, "@alice"), _tools(w, "@bob")
    id_a = alice.push("value 2", [{"path": "app.py", "content": "value = 2\n"}]).splitlines()[0]
    id_b = bob.push("value 3", [{"path": "app.py", "content": "value = 3\n"}]).splitlines()[0]
    _pass(w, id_a)
    _pass(w, id_b)
    w.tick(120)
    landed = _landed_ids(alice)
    assert id_a in landed or id_b in landed, alice.status()
    loser_id, loser = (id_b, bob) if id_a in landed else (id_a, alice)
    reb = loser.rebase(loser_id)
    assert reb.startswith("conflict"), reb
    assert "app.py" in reb
    assert "line 1" in reb
