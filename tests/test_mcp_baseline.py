"""Two agents sharing a bare origin: clean merges land, conflicts resolve, non-fast-forward is rejected."""

from __future__ import annotations

import asyncio

import pytest

from gud.analytics import observe, recent, reset
from gud.mcp_baseline import TOOL_NAMES, GitTools, build_server, clone, make_origin


def _pair(tmp_path, files):
    origin = make_origin(tmp_path, files)
    alice = clone(origin, tmp_path / "alice", "@alice")
    bob = clone(origin, tmp_path / "bob", "@bob")
    return alice, bob


def test_nonconflicting_edits_both_land(tmp_path):
    alice, bob = _pair(tmp_path, {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    assert alice.write("a.py", "a = 2\n").startswith("wrote")
    assert alice.git_commit("alice").startswith("committed")
    assert alice.git_push().startswith("pushed")
    assert bob.write("b.py", "b = 2\n").startswith("wrote")
    assert bob.git_commit("bob").startswith("committed")
    rejected = bob.git_push()
    assert "rejected" in rejected
    assert "non-fast-forward" in rejected
    pulled = bob.git_pull()
    assert pulled.startswith("updated")
    assert "conflict" not in pulled
    assert bob.git_push().startswith("pushed")
    assert alice.git_pull().startswith("updated")
    assert alice.read("a.py") == "a = 2\n"
    assert alice.read("b.py") == "b = 2\n"
    assert bob.read("a.py") == "a = 2\n"
    assert bob.read("b.py") == "b = 2\n"
    assert "alice" in alice.log()


def test_conflicting_edits_resolve(tmp_path):
    alice, bob = _pair(tmp_path, {"a.py": "a = 1\n"})
    alice.write("a.py", "a = 2\n")
    alice.git_commit("alice")
    alice.git_push()
    bob.write("a.py", "a = 3\n")
    bob.git_commit("bob")
    assert "non-fast-forward" in bob.git_push()
    pulled = bob.git_pull()
    assert pulled.startswith("conflict:")
    assert "a.py" in pulled
    assert bob.resolve("a.py", "a = 4\n").startswith("resolved")
    assert bob.git_commit("merge").startswith("committed")
    assert bob.git_push().startswith("pushed")
    assert alice.git_pull().startswith("updated")
    assert alice.read("a.py") == "a = 4\n"
    assert bob.read("a.py") == "a = 4\n"


def test_push_rejected_on_non_fast_forward(tmp_path):
    alice, bob = _pair(tmp_path, {"a.py": "a = 1\n"})
    alice.write("a.py", "a = 2\n")
    alice.git_commit("move")
    assert alice.git_push().startswith("pushed")
    bob.write("a.py", "a = 9\n")
    bob.git_commit("other")
    rejected = bob.git_push()
    assert rejected.startswith("rejected: non-fast-forward")
    assert bob.read("a.py") == "a = 9\n"


def test_server_lists_git_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("ARMATURE_API_KEY", raising=False)
    origin = make_origin(tmp_path, {"a.py": "a = 1\n"})
    tools = clone(origin, tmp_path / "alice", "@alice")
    server = build_server(tools)
    listed = asyncio.run(server.list_tools())
    assert sorted(t.name for t in listed) == sorted(TOOL_NAMES)
    write = next(t for t in listed if t.name == "write")
    assert "content" in write.input_schema["properties"]
    assert "self" not in write.input_schema["properties"]


def test_analytics_records_size_latency_and_error(monkeypatch):
    monkeypatch.delenv("ARMATURE_API_KEY", raising=False)
    reset()
    assert observe("git", "read", {"path": "a.py"}, lambda: "a = 1\n", actor="@alice") == "a = 1\n"
    row = recent()[-1]
    assert row["tool"] == "read" and row["server"] == "git"
    assert row["args_size"] > 0 and row["latency_ms"] >= 0 and row["error"] is False

    def boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        observe("git", "git_push", {}, boom, actor="@bob")
    assert recent()[-1]["error"] is True
    assert recent()[-1]["tool"] == "git_push"
