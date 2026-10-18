from gud import sig
from gud.memberfile import members_of, rule_for
from gud.patch import diff_trees
from gud.testkit import REPO, World, parsed
from gud.wire import parse_body, render_body, state_hash, tree_hash


def test_render_parse_roundtrip():
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    body = render_body("hello\nworld", {"Gud-Kind": "push", "Gud-Author": "@a"}, diff)
    p = parse_body(body)
    assert p.text == "hello\nworld"
    assert p.kind == "push" and p.get("Gud-Author") == "@a"
    assert p.diff == diff


def test_trailer_case_insensitive_and_last_fence_wins():
    body = "hi\n\ngud-kind: approve\n\n```diff\nold\n```\n\n```diff\nnew\n```\n"
    p = parse_body(body)
    assert p.kind == "approve"
    assert p.diff == "new\n"


def test_sign_verify(tmp_path):
    raw, pub = sig.generate()
    t = {"Gud-Kind": "push", "Gud-Author": "@a"}
    t["Gud-Sig"] = sig.sign(raw, t, "d\n")
    assert sig.verify(pub, t, "d\n")
    assert not sig.verify(pub, t, "e\n")
    _, other = sig.generate()
    assert not sig.verify(other, t, "d\n")


def test_hashes_stable():
    assert tree_hash({}) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert state_hash("0" * 64, "@a", ["<2>", "<1>"], "x") == state_hash("0" * 64, "@a", ["<1>", "<2>"], "x")


def test_diff_trees_add_delete_modify():
    d = diff_trees({"a": "1\n", "b": "x\n"}, {"a": "2\n", "c": "y\n"})
    assert "--- /dev/null\n+++ b/c" in d
    assert "--- a/b\n+++ /dev/null" in d
    assert "-1\n+2\n" in d


def test_world_genesis_and_push(tmp_path):
    w = World.create(tmp_path, protected="api/* explicit\n")
    g = w.genesis({"app.py": "def f():\n    return 1\n", "api/x.py": "X = 1\n"})
    p = w.edit("@alice", g, "app.py", "def f():\n    return 2\n", intent="return 2")
    w.test(p)
    mail = w.repo_mail()
    assert [parsed(e).kind for e in mail] == ["push", "push", "test"]
    assert all(REPO in e.to for e in mail)
    pp = parsed(p.email)
    assert pp.get("Gud-Parent") == g.id and pp.get("Gud-Base") == g.state
    assert sig.verify(w.members["@alice"].public_key, pp.trailers, pp.diff)
    # agents are CC'd and receive their own copy
    assert w.mailbox.list(recipient="bob@gud.local")[1].message_id == p.id
    assert set(members_of(g.tree)) == set(w.members)
    assert rule_for(g.tree, ["api/x.py"]) == "explicit"
    assert rule_for(g.tree, [".gud/members", "api/x.py"]) == "explicit"
    assert rule_for(g.tree, [".gud/members"]) is None
    assert rule_for(g.tree, ["app.py"]) is None
