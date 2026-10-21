"""gud.mcp over a real stdio MCP client session (`python -m gud.mcp --json`), against a repo in
local host mode over the fake transport. @alice talks through the MCP server; @bob acts in process
with the same tool bodies."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from gud.mcp import TOOL_NAMES

from tests.v1.kit import status
from tests.v1.mcp.conftest import LocalRepo


async def _call(client: Any, name: str, **args: Any) -> Any:
    result = await client.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in result.content)
    return json.loads(text)


def _session(repo: LocalRepo, handle: str, script) -> Any:
    from mcp.client import Client

    async def main() -> Any:
        async with Client(repo.server_params(handle)) as client:
            return await script(client)

    return asyncio.run(main())


def test_tools_listed_with_instructions(repo: LocalRepo):
    repo.genesis({"app.py": "x = 1\n"})
    repo.clone("@alice")

    async def script(client):
        listed = await client.list_tools()
        return sorted(t.name for t in listed.tools), {t.name: t.description for t in listed.tools}

    names, desc = _session(repo, "@alice", script)
    assert names == sorted(TOOL_NAMES)
    assert "veto" not in names
    assert "working copy" in desc["push"] and "full diff" in desc["inflight"]


def test_push_links_and_inflight_shows_another_members_push(repo: LocalRepo):
    g = repo.genesis({"app.py": "x = 1\n"})
    repo.task("Build the lib", "lib.py with f()")
    repo.clone("@alice")
    repo.clone("@bob")
    bob = repo.tools("@bob")
    (repo.wc_dir("@alice") / "lib.py").write_text("def f():\n    return 1\n")

    async def first(client):
        st = await _call(client, "status")
        claim = await _call(client, "claim", task="T1", files=["lib.py"])
        push = await _call(client, "push", intent="add lib", task="t1")
        return st, claim, push

    st, claim, push = _session(repo, "@alice", first)
    assert st["tip"]["state"] == g and st["working_copy"]["clean"] is False
    assert claim["status"] == "active" and claim["files"] == ["lib.py"]
    assert push["status"] == "linked" and push["files"] == ["lib.py"]
    alice_link = push["push"]

    # bob changes alice's freshly written line: she is asked, and his push stays in its window
    assert json.loads(bob.sync())["ok"]
    (repo.wc_dir("@bob") / "lib.py").write_text("def f():\n    return 2\n")
    bpush = json.loads(bob.push("f returns 2"))
    assert bpush["status"] == "window" and bpush["forecast"]["askers"] == ["@alice"]

    async def second(client):
        inflight = await _call(client, "inflight")
        inbox = await _call(client, "inbox")
        narrowed = await _call(client, "inflight", paths=["README"])
        return inflight, inbox, narrowed

    inflight, inbox, narrowed = _session(repo, "@alice", second)
    [prop] = inflight["proposals"]
    assert prop["push"] == bpush["push"] and prop["author"] == "@bob" and prop["intent"] == "f returns 2"
    assert "-    return 1\n+    return 2" in prop["diff"]
    assert [c["holder"] for c in prop["claims"]] == ["@alice"]          # her claim covers lib.py
    needs = [n for n in inbox["needs"] if n["type"] == "review"]
    assert needs and needs[0]["push"] == bpush["push"] and needs[0]["author"] == "@bob"
    assert any(e["type"] == "review.requested" for e in inbox["events"])
    assert narrowed["proposals"] == []
    assert alice_link


def test_claim_overlap_is_visible_to_both(repo: LocalRepo):
    repo.genesis({"style.css": "a {}\n"})
    repo.task("Home page")
    repo.task("About page")
    repo.clone("@alice")
    repo.clone("@bob")
    bob = repo.tools("@bob")
    first = json.loads(bob.claim("T2", ["about.html", "style.css"]))
    assert first["status"] == "active" and first["overlaps"] == []

    async def script(client):
        mine = await _call(client, "claim", task="T1", files=["index.html", "*.css"])
        board = await _call(client, "tasks")
        inflight = await _call(client, "inflight")
        inbox = await _call(client, "inbox")
        return mine, board, inflight, inbox

    mine, board, inflight, inbox = _session(repo, "@alice", script)
    assert mine["status"] == "active"
    assert [o["holder"] for o in mine["overlaps"]] == ["@bob"]
    claims = {c["holder"]: c for c in board["claims"]}
    assert claims["@bob"]["files"] == ["about.html", "style.css"] and claims["@alice"]["files"] == ["index.html", "*.css"]
    bob_claim = next(c for c in inflight["claims"] if c["holder"] == "@bob")
    assert bob_claim["overlaps_your_claims"] == [mine["claim"]]
    assert any(n["type"] == "overlap" for n in inbox["needs"])
    assert any(e["type"] == "claim.overlap" for e in inbox["events"])
    # bob sees it too, from his own replay of the chain
    assert any(n["type"] == "overlap" and n["holder"] == "@alice" for n in json.loads(bob.inbox())["needs"])


def test_object_with_evidence_and_bracketed_prefix_ids(repo: LocalRepo):
    repo.genesis({"app.py": "x = 1\n"})
    repo.clone("@alice")
    repo.clone("@bob")
    alice = repo.tools("@alice")
    bob = repo.tools("@bob")
    (repo.wc_dir("@alice") / "lib.py").write_text("def f():\n    return 1\n")
    assert json.loads(alice.push("add lib"))["status"] == "linked"
    bob.sync()
    (repo.wc_dir("@bob") / "lib.py").write_text("def f():\n    return 2\n")
    pid = json.loads(bob.push("f returns 2"))["push"]
    guard = repo.wc_dir("@alice") / "tests" / "test_guard.py"
    guard.parent.mkdir()
    guard.write_text("from lib import f\n\ndef test_f():\n    assert f() == 1\n")

    async def script(client):
        objection = await _call(client, "object", target=f"<{pid[:8]}>", reason="f must return 1",
                                evidence_file="tests/test_guard.py")
        thread = await _call(client, "thread", id=f"[gud:{pid[:12]}]")
        bad = await _call(client, "approve", target="zzzz")
        return objection, thread, bad

    objection, thread, bad = _session(repo, "@alice", script)
    assert objection["target"] == pid and objection["evidence"] == "tests/test_guard.py" and objection["logged"]
    kinds = [(m["kind"], m["author"]) for m in thread["messages"]]
    assert kinds == [("push", "@bob"), ("object", "@alice")]
    assert thread["messages"][1]["evidence"]["path"] == "tests/test_guard.py"
    assert "error" in bad and "hex prefix" in bad["error"]
    repo.client("@bob").sync_log()
    chain = repo.client("@bob").chain()
    assert status(chain, pid) == "objected"
    prop = chain.push_status(pid)
    assert prop.objections[0].evidence is not None and prop.objections[0].evidence.path == "tests/test_guard.py"
    need = [n for n in json.loads(bob.inbox())["needs"] if n["type"] == "objection"]
    assert need and need[0]["evidence"] == "tests/test_guard.py"


def test_read_blame_and_search_at_old_links_and_in_flight(repo: LocalRepo):
    g = repo.genesis({"app.py": "x = 1\ny = 2\n"})
    repo.clone("@alice")
    repo.clone("@bob")
    alice = repo.tools("@alice")
    bob = repo.tools("@bob")
    (repo.wc_dir("@alice") / "lib.py").write_text("one\n")
    l1 = json.loads(alice.push("lib one"))["push"]
    alice.sync()
    (repo.wc_dir("@alice") / "lib.py").write_text("one\ntwo\n")
    l2 = json.loads(alice.push("lib two"))["push"]
    bob.sync()
    (repo.wc_dir("@bob") / "lib.py").write_text("one\nTWO\n")       # rewrites alice's line: stays in its window
    inflight_push = json.loads(bob.push("lib TWO"))["push"]

    async def script(client):
        return {
            "old": await _call(client, "read", path="lib.py", at=l1[:6]),
            "tip": await _call(client, "read", path="lib.py"),
            "proposed": await _call(client, "read", path="lib.py", at=inflight_push[:10]),
            "genesis_ls": await _call(client, "ls", at=g[:8]),
            "blame_old": await _call(client, "blame", path="lib.py", at=l1[:8]),
            "blame": await _call(client, "blame", path="lib.py"),
            "app_blame": await _call(client, "blame", path="app.py", at="tip"),
            "search_old": await _call(client, "search", query="two", at=l1[:8]),
            "search_wc": await _call(client, "search", query="tw.", regex=True),
            "log": await _call(client, "log", path="lib.py"),
            "missing": await _call(client, "read", path="nope.txt"),
        }

    out = _session(repo, "@alice", script)
    assert out["old"]["text"] == "one\n" and out["tip"]["text"] == "one\ntwo\n"
    assert out["proposed"]["text"] == "one\nTWO\n" and "proposed tree" in out["proposed"]["at"]
    assert "lib.py" not in [p["path"] for p in out["genesis_ls"]["paths"]]
    assert [(r["start"], r["end"], r["owner"]) for r in out["blame_old"]["ranges"]] == [(1, 1, "@alice")]
    seqs = [(r["start"], r["end"], r["seq"]) for r in out["blame"]["ranges"]]
    assert seqs == [(1, 1, 1), (2, 2, 2)] and all(r["live"] for r in out["blame"]["ranges"])
    assert out["app_blame"]["ranges"][0]["owner"] == "@nelson"
    assert out["search_old"]["total"] == 0
    assert [(m["path"], m["line"]) for m in out["search_wc"]["matches"]] == [("lib.py", 2)]
    assert [l["push"] for l in out["log"]["links"]] == [l2, l1]
    assert "error" in out["missing"]


def test_wait_for_a_push_and_task_aliases(repo: LocalRepo):
    repo.genesis({"app.py": "x = 1\n"})
    repo.task("Docs")
    repo.clone("@alice")
    (repo.wc_dir("@alice") / "docs.md").write_text("# docs\n")

    async def script(client):
        claim = await _call(client, "claim", task="t1", files=["docs.md"])
        push = await _call(client, "push", intent="docs", task="T1")
        waited = await _call(client, "wait", push=push["push"][:7], timeout=5)
        task = await _call(client, "task", id="T1")
        sub = await _call(client, "subtask", parent="T1", title="more docs", files=["docs/**"])
        board = await _call(client, "tasks")
        released = await _call(client, "release", claim="T1")
        return claim, push, waited, task, sub, board, released

    claim, push, waited, task, sub, board, released = _session(repo, "@alice", script)
    assert push["status"] == "linked" and "linked" in waited["outcome"]
    assert task["task"]["status"] == "claimed" and task["links"]      # a root task waits for its holder (§16.6)
    assert sub["alias"] == "T1.1" and sub["valid"]
    assert [t["alias"] for t in board["tasks"]] == ["T1", "T1.1"]
    assert released["status"] == "released"
