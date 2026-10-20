"""Carry-forward (§14): file mode default, line mode opt-in, Gud-Carry: none, derived ids, consent."""

from gud.core.replay import carried_state, carryable
from tests.v1.kit import Kit, events_of, link_of, status

W, G = 60_000, 30_000


def base(config="", **kw):
    k = Kit(config=config, **kw)
    g = k.genesis({"app.py": "a = 1\nb = 1\nc = 1\nd = 1\ne = 1\n", "lib.py": "x = 1\n", "z.txt": "z\n"},
                  author="@alice")
    return k, g


def test_disjoint_push_is_carried_with_derived_id():
    k, g = base()
    p1 = k.push("@alice", g, {"lib.py": "x = 2\n"})
    p2 = k.push("@alice", g, {"z.txt": "zz\n"})
    chain, events = k.run()
    l2 = link_of(chain, p2)
    assert l2.state == carried_state(p2.id, p1.id) == p2.carried(p1)
    assert l2.carried_from == g.id and l2.parent == p1.id and l2.named_parent == g.id
    assert l2.author == "@alice" and l2.push == p2.id
    files = k.replayer().reader().files()
    assert files["lib.py"][1] == b"x = 2\n" and files["z.txt"][1] == b"zz\n"
    carried = events_of(events, "carried")
    assert carried and carried[0].data["candidate"] == l2.state and carried[0].data["onto"] == p1.id


def test_file_overlap_is_not_carried_in_file_mode():
    k, g = base()
    p1 = k.push("@alice", g, {"app.py": "a = 2\nb = 1\nc = 1\nd = 1\ne = 1\n"})
    p2 = k.push("@alice", g, {"app.py": "a = 1\nb = 1\nc = 1\nd = 1\ne = 2\n"})
    chain, events = k.run()
    assert status(chain, p1) == "linked"
    assert status(chain, p2) == "superseded"
    sup = events_of(events, "superseded")
    assert sup and sup[0].data["push"] == p2.id and sup[0].data["by_link"] == p1.id


def test_line_mode_carries_disjoint_lines():
    k, g = base(config="carry line\n")
    p1 = k.push("@alice", g, {"app.py": "a = 2\nb = 1\nc = 1\nd = 1\ne = 1\n"})
    p2 = k.push("@bob", g, {"app.py": "a = 1\nb = 1\nc = 1\nd = 1\ne = 2\n"}, log=False)
    k.log_msg(p2)
    chain, _ = k.advance(W + G + 5)     # @bob touches @alice's line: window
    assert status(chain, p2) == "linked"
    reader = k.replayer().reader()
    assert reader.files()["app.py"][1] == b"a = 2\nb = 1\nc = 1\nd = 1\ne = 2\n"
    blame = reader.blame("app.py")
    assert blame[0][0] == "@alice" and blame[4][0] == "@bob"
    assert link_of(chain, p2).carried_from == g.id


def test_line_mode_same_line_is_not_carried():
    k, g = base(config="carry line\n")
    k.push("@alice", g, {"app.py": "a = 2\nb = 1\nc = 1\nd = 1\ne = 1\n"})
    p2 = k.push("@alice", g, {"app.py": "a = 3\nb = 1\nc = 1\nd = 1\ne = 1\n"})
    chain, _ = k.run()
    assert status(chain, p2) == "superseded"


def test_carry_none_is_honoured():
    k, g = base()
    k.push("@alice", g, {"lib.py": "x = 2\n"})
    p2 = k.push("@alice", g, {"z.txt": "zz\n"}, carry=False)
    chain, _ = k.run()
    assert status(chain, p2) == "superseded"
    assert "Gud-Carry: none" in chain.push_status(p2.id).reason


def test_late_push_on_old_parent_is_carried_straight_onto_tip():
    k, g = base()
    a = k.push("@alice", g, {"lib.py": "x = 2\n"})
    b = k.push("@alice", a, {"lib.py": "x = 3\n"})
    late = k.push("@bob", g, {"new.txt": "n\n"})
    chain, _ = k.run()
    link = link_of(chain, late)
    assert link.parent == b.id and link.state == late.carried(b) and link.linked_at == late.logged_at


def test_late_push_on_old_parent_overlapping_is_superseded():
    k, g = base()
    a = k.push("@alice", g, {"lib.py": "x = 2\n"})
    late = k.push("@alice", g, {"lib.py": "x = 9\n"})
    chain, _ = k.run()
    assert status(chain, late) == "superseded"


def test_carried_candidate_asks_only_new_owners_and_keeps_window():
    k, g = base()
    slow = k.push("@bob", g, {"app.py": "a = 9\nb = 1\nc = 1\nd = 1\ne = 1\n"})   # asks @alice
    first = k.push("@alice", g, {"lib.py": "x = 2\n"}, log=False)
    k.log_msg(first)
    chain, events = k.run()
    # `slow` waits for @alice; `first` waits behind it until `slow` parks or links.
    k.approve("@alice", slow)
    chain, events = k.run()
    assert status(chain, slow) == "linked" and status(chain, first) == "linked"
    l = link_of(chain, first)
    assert l.state == first.carried(slow)
    # carried candidate had no new askers (first touches only @alice's own file)
    assert not [e for e in events_of(events, "review.requested") if e.data["candidate"] == l.state]


def test_votes_on_push_id_count_for_carried_candidate():
    k, g = base()
    blocker = k.push("@bob", g, {"z.txt": "z2\n"})          # asks @alice: window
    p = k.push("@bob", g, {"lib.py": "x = 5\n"})            # asks @alice too
    k.approve("@alice", blocker)
    chain, _ = k.run()
    assert status(chain, blocker) == "linked"
    k.approve("@alice", p)                                  # targets the push id, not the carried id
    chain, _ = k.run()
    assert status(chain, p) == "linked"
    assert link_of(chain, p).state == p.carried(blocker)


def test_carryable_building_block():
    k, g = base()
    p1 = k.push("@alice", g, {"lib.py": "x = 2\n"})
    p2 = k.push("@alice", g, {"z.txt": "zz\n"}, log=False)
    r = k.replayer()
    reader = r.reader()
    assert reader.ops_of(p1.id)
    tree = carryable(reader.chain.links, k.edit_ops(k.tree(g), {"z.txt": "zz\n"})[1],
                     ["z.txt"], "file", reader, g.id, p1.id)
    assert tree is not None and tree.read("z.txt") == b"zz\n" and tree.read("lib.py") == b"x = 2\n"
    ops = k.edit_ops(k.tree(g), {"lib.py": "x = 3\n"})[1]
    assert carryable(reader.chain.links, ops, ["lib.py"], "file", reader, g.id, p1.id) is None
