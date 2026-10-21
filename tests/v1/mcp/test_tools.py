"""gud.mcp_v1 tool bodies in process (the same code the stdio server runs): guards and edge cases."""

from __future__ import annotations

import json

from gud.mcp_v1 import artifacts, describe_event, is_echo
from gud.core.envelope import make_event

from tests.v1.mcp.conftest import LocalRepo


def test_push_refuses_build_output_until_it_is_ignored(repo: LocalRepo):
    repo.genesis({"app.py": "x = 1\n"})
    repo.clone("@alice")
    alice = repo.tools("@alice")
    root = repo.wc_dir("@alice")
    (root / "lib.py").write_text("def f():\n    return 1\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "lib.cpython-313.pyc").write_bytes(b"\x00\x01")
    (root / ".pytest_cache" / "v").mkdir(parents=True)
    (root / ".pytest_cache" / "v" / "nodeids").write_text("[]")
    (root / ".gudignore").write_text("*.log\n")                # a .gudignore that forgets build output
    out = json.loads(alice.push("add lib"))
    assert "error" in out and ".gudignore" in out["error"] and "__pycache__/" in out["error"]
    assert ".pytest_cache/" in out["error"]
    assert repo.client("@alice").chain().tip.seq == 0           # nothing was sent
    (root / ".gudignore").write_text("__pycache__/\n.pytest_cache/\n")
    out = json.loads(alice.push("add lib"))
    assert out["status"] == "linked" and out["files"] == [".gudignore", "lib.py"]


def test_build_output_is_ignored_by_default(repo: LocalRepo):
    """With no .gudignore at all, the default rules (gud.wc.ignore.DEFAULT_GUDIGNORE) apply."""
    repo.genesis({"app.py": "x = 1\n"})
    repo.clone("@alice")
    alice = repo.tools("@alice")
    root = repo.wc_dir("@alice")
    (root / "lib.py").write_text("def f():\n    return 1\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "lib.cpython-313.pyc").write_bytes(b"\x00\x01")
    out = json.loads(alice.push("add lib"))
    assert out["status"] == "linked" and out["files"] == ["lib.py"]


def test_push_with_edits_for_members_without_a_working_copy(repo: LocalRepo):
    g = repo.genesis({"app.py": "x = 1\n", "old.txt": "bye\n"})
    bob = repo.tools("@bob")
    assert bob.wc_root is None
    out = json.loads(bob.push("edit without files", edits=[
        {"path": "app.py", "content": "x = 2\n"}, {"path": "new.bin", "content_b64": "AAEC"},
        {"path": "old.txt", "delete": True}]))
    assert out["status"] in ("window", "linked") and sorted(out["files"]) == ["app.py", "new.bin", "old.txt"]
    tree = json.loads(bob.read("new.bin", at=out["push"][:8]))
    assert tree["base64"] == "AAEC"
    bad = json.loads(bob.push("bad", edits=[{"path": "nope.txt", "delete": True}]))
    assert "not in the tree" in bad["error"]
    assert json.loads(bob.status())["tip"]["state"] in (g, out["push"])
    assert "clone" in json.loads(bob.diff())["error"]            # no working copy: says what to do


def test_wait_ignores_echoes_of_my_own_actions(repo: LocalRepo):
    repo.genesis({"app.py": "x = 1\n"})
    repo.task("Docs")
    repo.clone("@alice")
    alice = repo.tools("@alice")
    assert json.loads(alice.claim("T1", ["docs.md"]))["status"] == "active"
    out = json.loads(alice.wait(timeout=1))
    assert out["timeout"] is True                               # her own claim.won is not news
    repo.client("@bob").sync_log()
    claim = json.loads(alice.status())["claims"][0]["id"]
    repo.tools("@bob").comment(claim[:10], "can I take part of this?")
    out = json.loads(alice.wait(timeout=5))
    assert [e["type"] for e in out["events"]] == ["commented"]


def test_claims_on_held_tasks_and_subtasks_need_the_parent_claim(repo: LocalRepo):
    repo.genesis({"app.py": "x = 1\n"})
    repo.task("Big task")
    alice, bob = repo.tools("@alice"), repo.tools("@bob")
    assert json.loads(alice.claim("T1", ["**"]))["status"] == "active"
    err = json.loads(bob.claim("T1", ["a.py"]))["error"]
    assert "claimed by @alice" in err
    assert "hold the active claim" in json.loads(bob.subtask("T1", "part"))["error"]
    sub = json.loads(alice.subtask("T1", "part b", files=["b.py"]))
    assert sub["alias"] == "T1.1"
    repo.client("@bob").sync_log()
    assert json.loads(bob.claim("T1.1", ["b.py"]))["status"] == "active"
    assert "error" in json.loads(bob.claim_update("T1", ["c.py"]))          # not bob's claim
    upd = json.loads(alice.claim_update("T1", ["a.py"]))
    assert upd["files"] == ["a.py"]


def test_helpers():
    assert artifacts(["a.py", "tests/__pycache__/x.pyc", "x.pyc", "node_modules/a/b.js", ".DS_Store"]) == [
        ".DS_Store", "node_modules/a/b.js", "tests/__pycache__/x.pyc", "x.pyc"]
    won = make_event("claim.won", 1, ("@a",), {"claim": "a" * 64, "task": "b" * 64, "holder": "@a", "files": ["x"]})
    assert is_echo(won, "@a") and not is_echo(won, "@b")
    odd = make_event("claim.overlap", 1, ("@a",), {"claims": ["a" * 64, "b" * 64], "holders": ["@a", "@b"],
                                                  "globs": [["x"], ["y"]]})
    assert "x, y" in describe_event(odd)


# ---------------------------------------------------------------- coordination fixes (evals/v1 v1-fixes)

def _in_thread(fn):
    import threading
    box: dict = {}

    def run():
        box["out"] = fn()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, box


def test_wait_for_linked_returns_when_another_agents_push_links(repo: LocalRepo):
    """The eval repro: @alice claims T1 with files=["todo.py"], @bob holds nothing and waits for `linked`;
    @alice's push links and @bob's wait returns at once instead of timing out."""
    import time as _time
    repo.genesis({"app.py": "x = 1\n"})
    repo.task("Todo")
    repo.clone("@alice")
    alice, bob = repo.tools("@alice"), repo.tools("@bob")
    assert json.loads(alice.claim("T1", ["todo.py"]))["status"] == "active"
    json.loads(bob.inbox())                                       # bob has seen the board so far
    t0 = _time.monotonic()
    th, box = _in_thread(lambda: json.loads(bob.wait(timeout=60, types=["linked"])))
    _time.sleep(0.5)
    (repo.wc_dir("@alice") / "todo.py").write_text("print('todo')\n")
    assert json.loads(alice.push("todo cli", task="T1"))["status"] == "linked"
    th.join(30)
    out = box["out"]
    assert _time.monotonic() - t0 < 30 and not out.get("timeout")
    assert [e["type"] for e in out["events"]] == ["linked"] and out["events"][0]["data"]["author"] == "@alice"
    assert "@bob" in out["events"][0]["concerns"]                 # every agent follows the tip (§19.2)


def test_wait_returns_when_the_tip_moves_even_without_an_event(repo: LocalRepo):
    """A member no `linked` event concerns (a human here) still sees the tip move through its own sync."""
    import time as _time
    repo.genesis({"app.py": "x = 1\n"})
    repo.clone("@alice")
    alice, dana = repo.tools("@alice"), repo.tools("@dana")
    th, box = _in_thread(lambda: json.loads(dana.wait(timeout=60)))
    _time.sleep(0.5)
    (repo.wc_dir("@alice") / "lib.py").write_text("y = 2\n")
    assert json.loads(alice.push("lib"))["status"] == "linked"
    th.join(30)
    out = box["out"]
    assert out["tip_moved"] is True and out["events"] == [] and out["tip"]["seq"] == 1
    assert json.loads(dana.wait(timeout=0.5, types=["review.requested"]))["timeout"] is True   # filter respected


def test_claiming_everything_while_others_are_idle_is_flagged(repo: LocalRepo):
    repo.genesis({"app.py": "x = 1\n"})
    repo.task("Big task")
    alice, bob = repo.tools("@alice"), repo.tools("@bob")
    board = json.loads(bob.tasks())
    assert board["idle_agents"] == ["@alice", "@carol"]
    assert "2 idle" in repo.tools("@bob", json_out=False).tasks()
    out = json.loads(alice.claim("T1", ["**"]))
    assert out["status"] == "active" and out["idle_agents"] == ["@bob", "@carol"]
    assert "split it now" in out["warning"] and "@bob, @carol" in out["warning"]
    err = json.loads(bob.claim("T1", ["a.py"]))["error"]
    assert "subtask.posted" in err
    assert json.loads(alice.subtask("T1", "part b", files=["b.py"]))["alias"] == "T1.1"
    repo.client("@bob").sync_log()
    err = json.loads(bob.claim("T1", ["a.py"]))["error"]
    assert "open subtasks (T1.1)" in err
    text = repo.tools("@carol", json_out=False).tasks()
    assert "Open subtasks to claim: T1.1" in text
    narrow = json.loads(bob.claim("T1.1", ["b.py"]))                # a narrow subtask claim: no warning
    assert narrow["status"] == "active" and "warning" not in narrow


def test_tool_results_point_at_subtasks_nobody_holds(repo: LocalRepo):
    """Regression (gud1-cc todo-stack v1-fixes t1): with T1 split into CLI, API and docs, an agent refused on a
    held subtask was told to wait for its holder to split it, and the agents who finished their subtask
    stopped with the CLI subtask unclaimed. Refusals and push results now name the open subtasks."""
    repo.genesis({"app.py": "x = 1\n"})
    repo.task("Todo stack")
    repo.clone("@bob")
    alice, bob, carol = repo.tools("@alice"), repo.tools("@bob"), repo.tools("@carol")
    alice.claim("T1", ["todolib.py"])
    for title, name in (("CLI", "cli.py"), ("API", "api.py"), ("docs", "docs.md")):
        alice.subtask("T1", title, files=[name])
    repo.client("@bob").sync_log()
    assert json.loads(bob.claim("T1.2", ["api.py"]))["status"] == "active"
    repo.client("@carol").sync_log()
    err = json.loads(carol.claim("T1.2", ["api.py"]))["error"]
    assert "open subtask nobody holds (T1.1, T1.3)" in err and "split it" not in err
    err = json.loads(carol.subtask("T1", "my own part"))["error"]
    assert "open subtasks you can claim instead: T1.1, T1.3" in err
    (repo.wc_dir("@bob") / "api.py").write_text("print('api')\n")
    out = json.loads(bob.push("api", task="T1.2"))
    assert out["status"] == "linked" and out["open_subtasks"] == ["T1.1", "T1.3"]
    err = json.loads(bob.release("T1.2"))["error"]
    assert "T1.2 is done" in err and "nothing to release" in err and "T1.1 CLI" in err


def test_declaring_done_through_claim_update_and_release(repo: LocalRepo):
    repo.genesis({"app.py": "x = 1\n"})
    repo.task("Docs")
    repo.task("Notes")
    repo.clone("@alice")
    alice = repo.tools("@alice")
    json.loads(alice.claim("T1", ["docs.md"]))
    (repo.wc_dir("@alice") / "docs.md").write_text("# docs\n")
    assert json.loads(alice.push("docs", task="T1"))["status"] == "linked"
    assert json.loads(alice.task("T1"))["task"]["status"] == "claimed"       # the link alone is not done
    out = json.loads(alice.claim_update("T1", done=True))
    assert out["declared"] is True and out["task_status"] == "work_done"     # requester @nelson accepts next
    assert out["files"] == ["docs.md"]
    json.loads(alice.claim("T2", ["notes.md"]))
    rel = json.loads(alice.release("T2", done=True))
    assert rel["status"] == "released" and rel["declared"] is True
    assert "error" in json.loads(alice.claim_update("T1"))                   # no files and no done
