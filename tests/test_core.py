import random

import pytest

from gud.blame import enclosing_owners, genesis_blame, removed_owners, update_blame
from gud.memberfile import MEMBERS, PROTECTED, format_members
from gud.patch import PatchError, apply_diff, diff_trees, diffstat
from gud.rules import affected_owners, protection, quorum_voters
from gud.types import LinkState, Member
from gud.wire import normalize


def _norm(tree):
    return {path: normalize(text) for path, text in tree.items()}


def test_diffstat_counts_body_lines_only():
    diff = diff_trees({"a": "1\n", "b": "x\ny\n"}, {"a": "2\n3\n", "c": "z\n"})
    stat = diffstat(diff)
    assert stat["files"] == ["a", "b", "c"]
    assert stat["added"] == 3
    assert stat["removed"] == 3
    assert diffstat("") == {"files": [], "added": 0, "removed": 0}
    assert diffstat(diff_trees({"a": "1\n"}, {"a": "1\n"})) == {"files": [], "added": 0, "removed": 0}


def test_apply_diff_add_delete_modify_and_two_hunks():
    old = {"a": "1\n", "b": "x\ny\n"}
    new = {"a": "2\n3\n", "c": "z\n"}
    assert apply_diff(old, diff_trees(old, new)) == new

    lines = [f"L{i}" for i in range(40)]
    src = "\n".join(lines) + "\n"
    lines[2] = "X2"
    lines[30] = "X30"
    dst = "\n".join(lines) + "\n"
    assert apply_diff({"f": src}, diff_trees({"f": src}, {"f": dst})) == {"f": dst}

    untouched = {"a": "1"}
    assert apply_diff(untouched, diff_trees(untouched, {"a": "2\n"})) == {"a": "2\n"}
    assert untouched == {"a": "1"}
    assert apply_diff({"a": "1"}, "") == {"a": "1\n"}


def test_apply_diff_is_strict():
    tree = {"a": "one\ntwo\n"}
    added = "diff --git a/a b/a\n--- /dev/null\n+++ b/a\n@@ -0,0 +1 @@\n+one\n"
    missing = "diff --git a/missing b/missing\n--- a/missing\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    mismatch = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,2 +1,2 @@\n-nope\n-two\n+one\n+two\n"
    offset = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-two\n+TWO\n"
    for diff in (added, missing, mismatch, offset):
        with pytest.raises(PatchError):
            apply_diff(tree, diff)


def test_apply_diff_roundtrip_property():
    rng = random.Random(2026)
    names = ["a.py", "b.txt", "dir/c.py", "README", "x/y/z.md", ".gud/members", "api/x.py", "empty"]
    samples = ["", "def f():", "    return 1", "+++ not a header", "--- a/a", "@@ -1 +1 @@",
               "hello world", "diff --git a/a b/a", "héllo"]

    def rand_tree():
        tree = {}
        for name in rng.sample(names, rng.randint(0, len(names))):
            lines = [rng.choice(samples) + ("x" * rng.randint(0, 12) if rng.random() < 0.3 else "")
                     for _ in range(rng.randint(0, 12))]
            text = "\n".join(lines)
            if lines and rng.random() < 0.7:
                text += "\n"
            if rng.random() < 0.2:
                text += "\n"
            tree[name] = text
        return tree

    for _ in range(40):
        old = rand_tree()
        new = rand_tree()
        diff = diff_trees(old, new)
        assert apply_diff(old, diff) == _norm(new), diff


def test_blame_tracks_equal_insert_and_delete():
    old = {"a.py": "a\nb\nc\n", "gone.txt": "z\n"}
    blame = genesis_blame(old, "@lead", "L0")
    new = {"a.py": "a\nB\nc\nd\n"}
    updated = update_blame(old, blame, new, "@alice", "L1")
    assert "gone.txt" not in updated
    assert updated["a.py"] == [("@lead", "L0"), ("@alice", "L1"), ("@lead", "L0"), ("@alice", "L1")]
    assert blame["a.py"][0] == ("@lead", "L0")


def test_removed_and_enclosing_owners():
    src = "class C:\n    def m(self):\n        return 1\n"
    old = {"a.py": src}
    blame = {"a.py": [("@carol", "c"), ("@bob", "b"), ("@alice", "a")]}
    rewritten = {"a.py": "class C:\n    def m(self):\n        return 2\n"}
    diff = diff_trees(old, rewritten)
    assert removed_owners(old, blame, diff) == {"@alice"}
    assert enclosing_owners(old, blame, diff) == {"@bob", "@carol"}

    inserted = {"a.py": "class C:\n    def m(self):\n        x = 1\n        return 1\n"}
    assert removed_owners(old, blame, diff_trees(old, inserted)) == set()
    assert enclosing_owners(old, blame, diff_trees(old, inserted)) == {"@bob", "@carol"}

    above = {"a.py": "x = 1\n" + src}
    assert enclosing_owners(old, blame, diff_trees(old, above)) == set()
    assert removed_owners(old, blame, diff_trees(old, above)) == set()

    broken = {"a.py": "def f(\n"}
    assert enclosing_owners(broken, {"a.py": [("@lead", "L")]}, diff_trees(broken, {"a.py": "def f(\n    x = 1\n"})) == set()


def _link(tree, blame, members):
    return LinkState(
        message_id="L", seq=0, author="@lead", state="s", tree_hash="t",
        tree=tree, blame=blame, members=members, received_at=1.0,
    )


def _member(handle, role):
    key = None if role == "human" else f"ed25519:{handle}"
    return Member(handle, role, f"{handle[1:]}@gud.local", key, None)


def test_affected_owners_drop_author_and_non_voters():
    members = {"@alice": _member("@alice", "agent"), "@bob": _member("@bob", "tester"),
               "@carol": _member("@carol", "agent")}
    old = {"n.txt": "a\nb\n"}
    blame = {"n.txt": [("@bob", "1"), ("@carol", "1")]}
    diff = diff_trees(old, {"n.txt": "a\nB\n"})
    parent = _link(old, blame, members)
    assert affected_owners(parent, diff, "@alice") == ["@carol"]
    assert affected_owners(parent, diff, "@carol") == []


def test_protection_and_quorum_voters():
    members = {"@alice": _member("@alice", "agent"), "@bob": _member("@bob", "agent"),
               "@carol": _member("@carol", "agent"), "@tester": _member("@tester", "tester")}
    tree = {
        PROTECTED: "api/* explicit\n",
        MEMBERS: format_members(members),
        "api/a.py": "x\n",
        "app.py": "y\n",
    }
    parent = _link(tree, {}, members)
    assert protection(parent, diff_trees(tree, {**tree, "app.py": "z\n"})) is None
    assert protection(parent, diff_trees(tree, {**tree, "api/a.py": "z\n"})) == "explicit"
    assert protection(parent, diff_trees(tree, {**tree, MEMBERS: "nope\n"})) is None

    changed = dict(members)
    changed["@bob"] = Member("@bob", "agent", "bob@gud.local", "ed25519:rotated", None)
    new_tree = {**tree, MEMBERS: format_members(changed)}
    assert quorum_voters(parent, tree) == ["@alice", "@bob", "@carol"]
    assert quorum_voters(parent, new_tree) == ["@alice", "@carol"]
