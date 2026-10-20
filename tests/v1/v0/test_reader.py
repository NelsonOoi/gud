"""The v0 legacy reader adapter (§23)."""

from gud.core import v0
from gud.core.objects import BlameMap, MemoryStore, Tree
from gud.core.replay import ChainReader
from gud.replay import replay as v0_replay
from gud.testkit import World
from tests.v1.kit import Kit


def _world(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    p = w.edit("@alice", g, "new.py", "n = 1\n", intent="add new", task="T1")
    w.test(p)
    q = w.edit("@bob", p, "app.py", "a = 2\n", intent="bump")
    w.test(q)
    w.tick(61)
    w.test(q)
    return w, g, p, q


def test_read_v0_matches_the_v0_reader(tmp_path):
    w, g, p, q = _world(tmp_path)
    mail = w.repo_mail()
    assert v0.is_v0_mailbox(mail)
    store = MemoryStore()
    out = v0.read_v0(mail, 60, w.clock.t, store)
    ref = v0_replay(mail, 60, w.clock.t)
    assert out.chain.tip.state == ref.tip.state == out.tip_state
    assert [l.state for l in out.links] == [l.state for l in ref.links]
    for adapted, orig in zip(out.links, ref.links):
        tree = Tree.load(store, adapted.tree_root)
        assert {path: tree.read(path).decode() for path in tree.paths()} == orig.tree
        blame = BlameMap.load(store, adapted.blame_root)
        for path, lines in orig.blame.items():
            assert [o for o, _ in blame.lines(path)] == [o for o, _ in lines]
        assert adapted.seq == orig.seq and adapted.author == orig.author
        assert adapted.linked_at == round(orig.linked_at * 1000)
    assert out.links[1].files == ("new.py",) and out.links[1].task == "T1"
    view = out.view()
    assert view.tip.state == ref.tip.state
    reader = ChainReader(view, store)
    assert reader.files()["app.py"][1] == ref.tip.tree["app.py"].encode()
    assert reader.members().get("@lead").role == "lead"       # v0 rules stay readable in the view
    assert len(out.links) == 3


def test_v1_messages_are_not_v0(tmp_path):
    k = Kit()
    g = k.genesis({"a": "1\n"})
    assert v0.is_v1_message(g.email)
    assert not v0.is_v0_mailbox([])
    w = World.create(tmp_path)
    w.genesis({"a.py": "a\n"})
    human = w.mailbox.send(from_addr="x@gud.local", to="repo@gud.local", subject="hi", body="hello\n")
    assert not v0.is_v0_mailbox([human])
