"""Test policy (§15): on-paths, every-link batched stacks, tip verdicts, red/green, bisect, fix tasks,
revert and auto-revert, and no task done on a red tip."""

from gud.core.replay import red_state, test_required
from gud.core.config import TestPolicy
from gud.core.tasks import fix_task_id
from tests.v1.kit import Kit, events_of, link_of, status

W, G = 60_000, 30_000


def base(test, **kw):
    k = Kit(test=test, **kw)
    g = k.genesis({"src/app.py": "a = 1\n", "docs/x.md": "x\n", "a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"},
                  author="@alice")
    return k, g


# ---------------------------------------------------------------- on-paths (§15.1)

def test_on_paths_push_does_not_link_without_pass():
    k, g = base("command pytest -q\non-paths src/**\n")
    p = k.push("@alice", g, {"src/app.py": "a = 2\n"})
    chain, events = k.run()
    assert status(chain, p) == "testing"
    assert "@t1" in events_of(events, "push.received")[0].concerns
    chain, _ = k.advance(W + G + 10)
    assert status(chain, p) != "linked"
    t = k.test_push(p.id, p.tree)
    chain, _ = k.run()
    assert status(chain, p) == "linked" and link_of(chain, p).linked_at == t.logged_at


def test_on_paths_failing_test_kills_candidate():
    k, g = base("command pytest -q\non-paths src/**\n")
    p = k.push("@alice", g, {"src/app.py": "a = 2\n"})
    k.test_push(p.id, p.tree, "fail")
    chain, events = k.run()
    assert status(chain, p) == "tests_failed"
    assert events_of(events, "test.failed")


def test_pass_on_wrong_tree_kills_candidate():
    k, g = base("command pytest -q\non-paths src/**\n")
    p = k.push("@alice", g, {"src/app.py": "a = 2\n"})
    k.test("push", p.id, g.tree.hash(), "pass")
    chain, _ = k.run()
    assert status(chain, p) == "tests_failed"


def test_push_id_result_vouches_for_carried_candidate_with_matching_tree():
    k, g = base("command pytest -q\non-paths src/**\n")
    first = k.push("@alice", g, {"docs/x.md": "y\n"})
    p = k.push("@alice", g, {"src/app.py": "a = 2\n"})
    chain, _ = k.run()
    cand = chain.push_status(p.id)
    assert cand.id == p.carried(first) and cand.status == "testing"
    k.test_push(p.id, p.tree)                                 # native tree: does not match the carried one
    chain, _ = k.run()
    assert status(chain, p) == "testing"
    carried_tree = k.replayer().reader().candidate_tree(cand.id)
    k.test_push(p.id, carried_tree)                           # push id, carried tree: vouches
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_push_outside_on_paths_needs_no_test():
    k, g = base("command pytest -q\non-paths src/**\n")
    p = k.push("@alice", g, {"docs/x.md": "y\n"})
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_no_command_means_no_test_requirement():
    assert not test_required(TestPolicy(command=None, every_link=True), ["a"])
    assert not test_required(TestPolicy(command="x", every_link=True), [".gud/members"])
    assert test_required(TestPolicy(command="x", every_link=True), ["a"])
    assert test_required(TestPolicy(command="x", on_paths=("src/**",)), ["src/a"])
    assert not test_required(TestPolicy(command="x", on_paths=("src/**",)), ["doc/a"])


def test_testing_candidate_parks_after_timeout():
    k, g = base("command pytest -q\non-paths src/**\ntimeout 1m\n")
    p = k.push("@alice", g, {"src/app.py": "a = 2\n"})
    q = k.push("@alice", g, {"docs/x.md": "y\n"})
    chain, _ = k.advance(W + G + 30_000)
    assert status(chain, q) != "linked"
    chain, _ = k.advance(40_000)
    assert status(chain, q) == "linked"


# ---------------------------------------------------------------- batched stacks (§15.3)

def stack_setup():
    k, g = base("command pytest -q\nevery-link on\n")
    ps = [k.push("@alice", g, {f: f"{f} 2\n"}) for f in ("a.txt", "b.txt", "c.txt")]
    final = k.tree(g)
    for f in ("a.txt", "b.txt", "c.txt"):
        final = final.set(f, "100644", f"{f} 2\n".encode())
    return k, g, ps, final


def test_every_link_stack_pass_links_all_k():
    k, g, ps, final = stack_setup()
    chain, _ = k.run()
    assert all(status(chain, p) == "testing" for p in ps)
    t = k.test("stack", g, final.hash(), "pass", stack=[p.id for p in ps])
    chain, _ = k.run()
    assert [l.push for l in chain.links[1:]] == [p.id for p in ps]
    assert all(l.linked_at == t.logged_at for l in chain.links[1:])
    assert chain.tip.tree_hash == final.hash()


def test_every_link_stack_with_wrong_tree_is_ignored():
    k, g, ps, final = stack_setup()
    k.test("stack", g, g.tree.hash(), "pass", stack=[p.id for p in ps])
    chain, _ = k.run()
    assert chain.tip.state == g.id


def test_every_link_stack_fail_bisects():
    k, g, ps, final = stack_setup()
    p1, p2, p3 = ps
    k.test("stack", g, final.hash(), "fail", stack=[p.id for p in ps])
    chain, _ = k.run()
    assert all(status(chain, p) == "testing" for p in ps)     # a k > 1 fail kills nothing
    k.test("stack", g, p1.tree.hash(), "pass", stack=[p1.id])
    chain, _ = k.run()
    l1 = link_of(chain, p1)
    assert l1 is not None
    c2 = p2.carried(l1.state)
    t2 = k.tree(l1.state).set("b.txt", "100644", b"b.txt 2\n")
    k.test("stack", l1.state, t2.hash(), "fail", stack=[p2.id])
    chain, _ = k.run()
    assert status(chain, p2) == "tests_failed"
    c3_tree = k.tree(l1.state).set("c.txt", "100644", b"c.txt 2\n")
    k.test("stack", l1.state, c3_tree.hash(), "pass", stack=[p3.id])
    chain, _ = k.run()
    assert status(chain, p3) == "linked"
    assert chain.tip.tree_hash == c3_tree.hash()


# ---------------------------------------------------------------- tip verdicts and red (§15.4, §15.5)

def red_setup(test="command pytest -q\n", **kw):
    k, g = base(test, **kw)
    l1 = k.push("@alice", g, {"a.txt": "a2\n"})
    l2 = k.push("@bob", l1, {"b.txt": "b2\n"}, log=False)
    k.log_msg(l2)
    k.approve("@alice", l2)
    l3 = k.push("@carol", l2, {"new.txt": "n\n"})
    chain, _ = k.run()
    assert chain.tip.state == l3.id
    return k, g, l1, l2, l3


def test_red_tip_opens_fix_task_naming_first_bad_from_bisect():
    k, g, l1, l2, l3 = red_setup()
    k.test_tip(l3.id, l3.tree, "fail")
    chain, events = k.run()
    assert chain.red.red and chain.red.red_link == l3.id and chain.red.first_bad is None
    fix = chain.tasks[fix_task_id(l3.id)]
    assert fix.kind == "fix" and fix.cited_link == l3.id
    assert events_of(events, "tip.red") and events_of(events, "fix.opened")
    k.test_tip(l1.id, l1.tree, "pass")
    k.test_tip(l2.id, l2.tree, "fail")
    chain, events = k.run()
    assert chain.red.first_bad == l2.id and chain.red.last_green == l1.id
    bis = events_of(events, "tip.bisected")
    assert bis and bis[0].data["first_bad"] == l2.id and bis[0].data["author"] == "@bob"
    fix = chain.tasks[fix_task_id(l3.id)]
    assert fix.cited_link == l2.id and fix.offered_to == "@bob"
    assert fix.status == "open"


def test_green_after_later_pass_closes_fix_task():
    k, g, l1, l2, l3 = red_setup()
    k.test_tip(l3.id, l3.tree, "fail")
    k.run()
    l4 = k.push("@alice", l3, {"b.txt": "fixed\n"})
    chain, _ = k.run()
    assert status(chain, l4) == "window"                    # @bob owns b.txt
    k.approve("@bob", l4)
    chain, _ = k.run()
    k.test_tip(link_of(chain, l4).state, k.tree(link_of(chain, l4).state), "pass")
    chain, events = k.run()
    assert not chain.red.red and events_of(events, "tip.green")
    assert chain.tasks[fix_task_id(l3.id)].status == "done"


def test_pushes_keep_linking_while_red():
    k, g, l1, l2, l3 = red_setup()
    k.test_tip(l3.id, l3.tree, "fail")
    p = k.push("@carol", l3, {"more.txt": "m\n"})
    chain, _ = k.run()
    assert chain.red.red and status(chain, p) == "linked"


def test_red_state_pure_function():
    k, g, l1, l2, l3 = red_setup()
    chain = k.replay()
    links = chain.links
    rs = red_state(links, {l3.id: ("fail", 10), l1.id: ("pass", 20), l2.id: ("fail", 30)}, 100)
    assert rs.red and rs.red_link == l3.id and rs.red_since == 10 and rs.first_bad == l2.id
    assert rs.last_green == l1.id and rs.fix_task == fix_task_id(l3.id)
    green = red_state(links, {l2.id: ("fail", 10), l3.id: ("pass", 20)}, 100)
    assert not green.red and green.last_green == l3.id
    assert red_state(links, {l2.id: ("fail", 10)}, 5).red is False


def test_auto_revert_lands_after_T():
    k, g, l1, l2, l3 = red_setup("command pytest -q\nauto-revert 5m\n")
    k.test_tip(l3.id, l3.tree, "fail")
    k.test_tip(l1.id, l1.tree, "pass")
    k.test_tip(l2.id, l2.tree, "fail")
    chain, _ = k.run()
    red_since = chain.red.red_since
    rev = k.revert("@t1", l3, l2, before=l1)
    k.object("@bob", rev, "do not revert me")               # consent would block it
    chain, _ = k.run()
    assert status(chain, rev) != "linked"
    k.tick(red_since + 5 * 60_000 - k.now - 1)
    k.tick_entry()
    chain = k.replay()                                       # replay time = the tick at T - 1 ms
    assert chain.t == red_since + 5 * 60_000 - 1
    assert status(chain, rev) != "linked"
    chain, _ = k.advance(10)
    link = link_of(chain, rev)
    assert link is not None and link.kind == "revert" and link.revert_of == l2.id
    assert link.linked_at == red_since + 5 * 60_000
    files = k.replayer().reader().files()
    assert files["b.txt"][1] == b"b\n" and files["a.txt"][1] == b"a2\n" and "new.txt" in files


def test_revert_without_auto_revert_needs_consent():
    k, g, l1, l2, l3 = red_setup()
    rev = k.revert("@t1", l3, l2, before=l1)
    k.object("@bob", rev, "no")
    chain, _ = k.advance(10 * 60_000)
    assert status(chain, rev) != "linked"


def test_auto_revert_only_for_first_bad():
    k, g, l1, l2, l3 = red_setup("command pytest -q\nauto-revert 1m\n")
    k.test_tip(l3.id, l3.tree, "fail")
    k.test_tip(l1.id, l1.tree, "pass")
    k.test_tip(l2.id, l2.tree, "fail")
    rev = k.revert("@t1", l3, l1, before=g)                 # not first_bad
    k.object("@alice", rev, "no")
    chain, _ = k.advance(2 * 60_000)
    assert status(chain, rev) != "linked"


def test_no_task_done_on_red_tip():
    k, g, l1, l2, l3 = red_setup()
    task = k.task("@carol", "do the thing")                  # requester: @carol's sponsor @nelson
    c = k.claim("@carol", task, ["new2.txt"])
    k.test_tip(l3.id, l3.tree, "fail")
    p = k.push("@carol", l3, {"new2.txt": "n\n"}, task=task.id)
    chain, _ = k.run()
    assert status(chain, p) == "linked" and chain.red.red
    k.claim("@carol", task, ["new2.txt"], revises=c, done=True)     # the holder declares the work complete
    k.unsigned("@nelson", "accept, looks good", reply_to=task)
    chain, _ = k.run()
    t = chain.tasks[task.id]
    assert t.work_done_at is not None and t.status == "work_done"
    pl = link_of(chain, p)
    k.test_tip(pl.state, k.tree(pl.state), "pass")
    chain, events = k.run()
    assert not chain.red.red
    assert chain.tasks[task.id].status == "done"
    assert events_of(events, "task.done")
