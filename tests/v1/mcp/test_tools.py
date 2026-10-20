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
    out = json.loads(alice.push("add lib"))
    assert "error" in out and ".gudignore" in out["error"] and "__pycache__/" in out["error"]
    assert ".pytest_cache/" in out["error"]
    assert repo.client("@alice").chain().tip.seq == 0           # nothing was sent
    (root / ".gudignore").write_text("__pycache__/\n.pytest_cache/\n")
    out = json.loads(alice.push("add lib"))
    assert out["status"] == "linked" and out["files"] == [".gudignore", "lib.py"]


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
