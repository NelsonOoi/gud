"""GudTools against the fake mailbox. Core tests skip until replay/chain/rules exist."""

from __future__ import annotations

import asyncio
import base64
import shutil
from pathlib import Path

import pytest

from gud import compose, sig
from gud.mcp_server import INSTRUCTIONS, TOOL_NAMES, GudTools, build_server, main, three_way
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


def _as(w: World, handle: str) -> dict:
    """handle and private_key arguments for a signing call, from the World fixture."""
    raw = w.ids[handle].private_raw
    assert raw is not None
    return {"handle": handle, "private_key": base64.b64encode(raw).decode()}


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
    auth = _as(w, "@alice")
    assert alice.push("x", [{"path": "../secret", "new": "no\n"}], **auth).startswith("error:")
    assert "binary" in alice.push("x", [{"path": "a.txt", "new": "a\x00b"}], **auth)
    assert alice.push("x", [], **auth).startswith("error:")
    assert alice.push("x", [{"path": "/etc/passwd", "new": "no\n"}], **auth).startswith("error:")


def _schema(tool) -> dict:
    return (getattr(tool, "input_schema", None) or tool.inputSchema)["properties"]


def test_server_lists_gud_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("ARMATURE_API_KEY", raising=False)
    w = World.create(tmp_path)
    server = build_server(GudTools(w.mailbox, window_seconds=60, clock=w.clock))
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    by_name = {tool.name: tool for tool in tools}
    assert "path" in _schema(by_name["read"])
    assert "self" not in _schema(by_name["read"])
    assert "handle" not in _schema(by_name["read"])
    push_props = _schema(by_name["push"])
    assert "edits" in push_props
    assert "handle" in push_props and "private_key" in push_props
    assert "handle" in _schema(by_name["reply"]) and "private_key" in _schema(by_name["reply"])
    assert "handle" in _schema(by_name["rebase"]) and "private_key" in _schema(by_name["rebase"])
    assert "handle" in _schema(by_name["status"])
    assert "private_key" not in _schema(by_name["status"])
    assert "handle" in _schema(by_name["wait"])
    assert "private_key" not in _schema(by_name["wait"])
    description = by_name["push"].description or ""
    assert "wait" in description.lower() or "full" in description.lower()
    assert server.instructions == INSTRUCTIONS
    assert "private_key" in server.instructions
    assert "{handle}" not in server.instructions


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
    out = _tools(w, "@alice").push("add readme", [{"path": "README.md", "new": "hi\n"}], **_as(w, "@alice"))
    assert out.splitlines()[0].startswith("<")
    assert "no affected owners" in out
    assert "affected: none" in out


def test_push_rewriting_bob_is_lazy_consensus(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "def f():\n    return 1\n"}, author="@bob")
    out = _tools(w, "@alice").push(
        "return 2", [{"path": "app.py", "old": "return 1", "new": "return 2"}], **_as(w, "@alice"),
    )
    assert "@bob" in out
    assert "lazy consensus" in out
    assert "unless @bob objects" in out


def test_reply_approve_and_object_are_signed(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "def f():\n    return 1\n"}, author="@bob")
    alice, bob = _tools(w, "@alice"), _tools(w, "@bob")
    pushed = alice.push(
        "return 2", [{"path": "app.py", "old": "return 1", "new": "return 2"}], **_as(w, "@alice"),
    )
    target = pushed.splitlines()[0]
    assert bob.reply(target, "approve", "lgtm", **_as(w, "@bob")).startswith("approve ")
    assert bob.reply(target, "object", "not yet", **_as(w, "@bob")).startswith("object ")
    assert bob.reply(target, "dance", **_as(w, "@bob")).startswith("error:")
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
    id_a = alice.push("add A", [{"path": "A.txt", "new": "A\n"}], **_as(w, "@alice")).splitlines()[0]
    id_b = bob.push("add B", [{"path": "B.txt", "new": "B\n"}], **_as(w, "@bob")).splitlines()[0]
    _pass(w, id_a)
    _pass(w, id_b)
    w.tick(5)
    landed = _landed_ids(alice)
    assert id_a in landed or id_b in landed, alice.status()
    loser_id, loser = (id_b, bob) if id_a in landed else (id_a, alice)
    assert loser_id not in landed, alice.status()
    reb = loser.rebase(loser_id, **_as(w, "@bob" if loser is bob else "@alice"))
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
    id_a = alice.push(
        "value 2", [{"path": "app.py", "old": "value = 1", "new": "value = 2"}], **_as(w, "@alice"),
    ).splitlines()[0]
    id_b = bob.push(
        "value 3", [{"path": "app.py", "old": "value = 1", "new": "value = 3"}], **_as(w, "@bob"),
    ).splitlines()[0]
    _pass(w, id_a)
    _pass(w, id_b)
    w.tick(120)
    landed = _landed_ids(alice)
    assert id_a in landed or id_b in landed, alice.status()
    loser_id, loser = (id_b, bob) if id_a in landed else (id_a, alice)
    reb = loser.rebase(loser_id, **_as(w, "@bob" if loser is bob else "@alice"))
    assert reb.startswith("conflict"), reb
    assert "app.py" in reb
    assert "line 1" in reb


FIVE = "one = 1\ntwo = 2\nthree = 3\nfour = 4\nfive = 5\n"


def _land(w: World, tools: GudTools, out: str) -> str:
    push_id = out.splitlines()[0]
    assert push_id.startswith("<"), out
    _pass(w, push_id)
    w.tick(120)
    assert push_id in _landed_ids(tools), tools.status()
    return push_id


def _diff_lines(w: World, push_id: str) -> list[str]:
    email = next(item for item in w.repo_mail() if item.message_id == push_id)
    return [line for line in parse_body(email.body).diff.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def test_replace_touches_only_matched_lines(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": FIVE})
    alice, bob = _tools(w, "@alice"), _tools(w, "@bob")
    _land(w, bob, bob.push("five", [{"path": "app.py", "old": "five = 5\n", "new": "five = 50\n"}], **_as(w, "@bob")))
    out = alice.push("one", [{"path": "app.py", "old": "one = 1\n", "new": "one = 10\n"}], **_as(w, "@alice"))
    assert "affected: @lead" in out
    assert "@bob" not in out
    assert _diff_lines(w, out.splitlines()[0]) == ["-one = 1", "+one = 10"]


def test_stale_read_keeps_lines_landed_since(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": FIVE})
    alice, bob = _tools(w, "@alice"), _tools(w, "@bob")
    stale = alice.read("app.py")
    _land(w, bob, bob.push("one", [{"path": "app.py", "old": "one = 1\n", "new": "one = 10\n"}], **_as(w, "@bob")))
    line5 = stale.splitlines(keepends=True)[4]
    _land(w, alice, alice.push("five", [{"path": "app.py", "old": line5, "new": "five = 50\n"}], **_as(w, "@alice")))
    tip = alice.read("app.py")
    assert "one = 10\n" in tip
    assert "five = 50\n" in tip


def test_old_not_found_or_ambiguous_sends_nothing(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "x = 1\nx = 1\ny = 2\n"})
    alice = _tools(w, "@alice")
    before = len(w.repo_mail())
    missing = alice.push("z", [{"path": "app.py", "old": "z = 3\n", "new": "z = 4\n"}], **_as(w, "@alice"))
    assert missing.startswith("error: old text not found in app.py at <"), missing
    assert "read it again" in missing
    twice = alice.push("x", [{"path": "app.py", "old": "x = 1\n", "new": "x = 2\n"}], **_as(w, "@alice"))
    assert twice == "error: old text matches 2 places in app.py; include more surrounding lines"
    assert len(w.repo_mail()) == before


def test_content_rejected_and_create_existing_errors(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": FIVE})
    alice = _tools(w, "@alice")
    out = alice.push("whole", [{"path": "app.py", "content": FIVE.replace("1", "9")}], **_as(w, "@alice"))
    assert out == "error: app.py: send old/new edits, not whole files"
    out = alice.push("again", [{"path": "app.py", "new": "x = 1\n"}], **_as(w, "@alice"))
    assert out.startswith("error: app.py already exists"), out


def test_delete_and_ordered_edits(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": FIVE, "old.py": "gone = 1\n"})
    alice = _tools(w, "@alice")
    _land(w, alice, alice.push("tidy", [
        {"path": "app.py", "old": "two = 2\n", "new": "two = 20\n"},
        {"path": "app.py", "old": "two = 20\n", "new": "two = 200\n"},
        {"path": "old.py", "delete": True},
        {"path": "new.py", "new": "fresh = 1"},
    ], **_as(w, "@alice")))
    tree = alice.repo().refresh().tip.tree
    assert "old.py" not in tree
    assert "two = 200\n" in tree["app.py"]
    assert tree["new.py"] == "fresh = 1\n"


def test_export_writes_tree_plus_edits(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": FIVE, "src/util.py": "X = 1\n"})
    alice = _tools(w, "@alice")
    out_dir = tmp_path / "out"
    out = alice.export(str(out_dir), edits=[{"path": "app.py", "old": "one = 1\n", "new": "one = 10\n"}])
    lines = out.splitlines()
    assert lines[0] == str(out_dir)
    assert lines[1].startswith("at: <")
    assert lines[3] == "edits: 1"
    assert (out_dir / "app.py").read_text().startswith("one = 10\n")
    assert (out_dir / "src/util.py").read_text() == "X = 1\n"
    assert (out_dir / ".gud/members").exists()
    assert alice.export(str(out_dir)).startswith("error:")
    scratch = Path(alice.export().splitlines()[0])
    try:
        assert scratch.name.startswith("gud-")
        assert (scratch / "app.py").read_text() == FIVE
    finally:
        shutil.rmtree(scratch)
    assert alice.export(edits=[{"path": "app.py", "old": "nope\n", "new": "x\n"}]).startswith("error: old text")


def test_wait_on_failed_tests_includes_log(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": FIVE})
    alice = _tools(w, "@alice")
    push_id = alice.push(
        "one", [{"path": "app.py", "old": "one = 1\n", "new": "one = 10\n"}], **_as(w, "@alice"),
    ).splitlines()[0]
    email = next(item for item in w.repo_mail() if item.message_id == push_id)
    tree = parse_body(email.body).get("Gud-Tree") or ""
    blob = compose.test_body(w.ids["@tester"], push_id, False, tree, "FAILED test_one: assert 10 == 1")
    compose.send(w.mailbox, w.ids["@tester"], w.members, f"Re: test {push_id}", blob, in_reply_to=push_id)
    out = alice.wait(push_id, timeout=1)
    assert out.startswith("tests_failed "), out
    assert "FAILED test_one: assert 10 == 1" in out


def test_main_takes_no_handle_and_binds_no_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("GUD_HOME", str(tmp_path))
    monkeypatch.setenv("GUD_MAILBOX", "fake")
    monkeypatch.setenv("GUD_HANDLE", "@alice")
    monkeypatch.setenv("GUD_HOST_URL", "")
    seen = {}

    def fake_build(tools):
        seen["identity"] = tools.identity

        class Server:
            def run(self, transport):
                seen["transport"] = transport

        return Server()

    monkeypatch.setattr("gud.mcp_server.build_server", fake_build)
    with pytest.raises(SystemExit) as exc:
        main(["--handle", "@alice"])
    assert exc.value.code not in (0, None)
    main([])
    assert seen["identity"] is None
    assert seen["transport"] == "stdio"


def test_signing_uses_the_call_key_and_handle(tmp_path):
    w = World.create(tmp_path)
    bootstrap(w, {"app.py": "def f():\n    return 1\n"})
    tools = GudTools(w.mailbox, window_seconds=60, clock=w.clock)
    assert tools.push("x", [{"path": "n.txt", "new": "n\n"}], handle="@alice").startswith("error: private_key")
    assert tools.status().startswith("error: handle")
    raw, public = sig.generate()
    out = tools.push(
        "note",
        [{"path": "note.txt", "new": "hi\n"}],
        handle="@lead",
        private_key=base64.b64encode(raw).decode(),
    )
    push_id = out.splitlines()[0]
    assert push_id.startswith("<"), out
    email = next(item for item in w.repo_mail() if item.message_id == push_id)
    body = parsed(email)
    assert body.get("Gud-Author") == "@lead"
    assert email.from_addr == w.members["@lead"].address
    assert sig.verify(public, body.trailers, body.diff)
    assert not sig.verify(w.members["@lead"].public_key, body.trailers, body.diff)
    assert "mine: none" in tools.status(handle="@alice")
    assert push_id in tools.status(handle="@lead")

    bound = _tools(w, "@alice")
    bob_key = _as(w, "@bob")["private_key"]
    other = bound.push("bob file", [{"path": "bob.txt", "new": "b\n"}], handle="@bob", private_key=bob_key)
    other_id = other.splitlines()[0]
    assert other_id.startswith("<"), other
    sent = next(item for item in w.repo_mail() if item.message_id == other_id)
    sent_body = parsed(sent)
    assert sent_body.get("Gud-Author") == "@bob"
    assert sent.from_addr == w.members["@bob"].address
    assert sig.verify(w.members["@bob"].public_key, sent_body.trailers, sent_body.diff)
    assert not sig.verify(w.members["@alice"].public_key, sent_body.trailers, sent_body.diff)
