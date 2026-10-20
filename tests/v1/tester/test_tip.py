"""Tip runs on the on-tip cadence, red tip, bisect to first_bad, the derived fix task, green again,
and auto-revert (§15.4–§15.6)."""

from gud.core.tasks import fix_task_id
from tests.v1.kit import events_of
from tests.v1.tester.util import chain_tests, make_tester, repo

MIN = 60_000


def _links(k, g, edits):
    out, parent = [], g
    for e in edits:
        p = k.push("@alice", parent, e)
        k.run()
        parent = p
        out.append(p)
    return out


def test_tip_cadence_every_n_links():
    k, g = repo("command sh check.sh\non-tip links=2 time=1h\n")
    t = make_tester(k)
    assert t.step() == []                               # genesis alone: 1 link since "never"
    l1, l2 = _links(k, g, [{"a.txt": "a2\n"}, {"b.txt": "b2\n"}])
    t.step()
    tips = chain_tests(k, "tip")
    assert [e.get("Gud-Target") for e in tips] == [l2.id]          # seq 2 - (-1) >= 2
    (l3,) = _links(k, l2, [{"c.txt": "c2\n"}])
    t.step()
    assert len(chain_tests(k, "tip")) == 1                          # one link since the last run
    (l4,) = _links(k, l3, [{"d.txt": "d2\n"}])
    t.step()
    assert [e.get("Gud-Target") for e in chain_tests(k, "tip")] == [l2.id, l4.id]
    assert t.step() == []                                           # never twice for one link


def test_tip_cadence_by_time():
    k, g = repo("command sh check.sh\non-tip links=100 time=10m\n")
    (l1,) = _links(k, g, [{"a.txt": "a2\n"}])
    t = make_tester(k)
    assert t.step() == []
    k.tick(10 * MIN)
    k.tick_entry()
    t.step()
    assert [e.get("Gud-Target") for e in chain_tests(k, "tip")] == [l1.id]
    k.tick(10 * MIN)
    assert t.step() == []                                           # time passed but no new link


def test_on_tip_off_runs_nothing():
    k, g = repo("command sh check.sh\non-tip off\n")
    _links(k, g, [{"a.txt": "a2\n"}, {"b.txt": "b2\n"}])
    k.tick(60 * MIN)
    assert make_tester(k).step() == []


def test_red_tip_bisects_to_first_bad_then_fix_turns_green():
    k, g = repo("command sh check.sh\non-tip links=4 time=1h\n")
    l1, l2, l3 = _links(k, g, [{"a.txt": "a2\n"}, {"b.txt": "BAD\n"}, {"c.txt": "c2\n"}])
    t = make_tester(k)
    t.step()                                                        # tip L3 (4 links) fails: red
    chain, events = k.run()
    assert chain.red.red and chain.red.red_link == l3.id and chain.red.first_bad is None
    fix = chain.tasks[fix_task_id(l3.id)]
    assert fix.kind == "fix" and fix.status == "open"
    t.step()                                                        # bisect
    chain, events = k.run()
    assert chain.red.first_bad == l2.id and chain.red.last_green == l1.id
    fix = chain.tasks[fix_task_id(l3.id)]
    assert fix.cited_link == l2.id
    tested = {e.get("Gud-Target"): e.get("Gud-Result") for e in chain_tests(k, "tip")}
    assert tested == {l3.id: "fail", l1.id: "pass", l2.id: "fail"}
    assert events_of(events, "tip.bisected")[0].data["first_bad"] == l2.id

    (l4,) = _links(k, l3, [{"b.txt": "b fixed\n"}])
    t.step()                                                        # red: every new tip is tested
    chain, events = k.run()
    assert not chain.red.red
    assert chain.tasks[fix_task_id(l3.id)].status == "done"
    assert chain_tests(k, "tip")[-1].get("Gud-Target") == l4.id


def test_bisect_without_any_green_link_reaches_genesis():
    files = {"check.sh": ("100755", "#!/bin/sh\ntest -f ok.flag\n"), "a.txt": "a\n"}
    k, g = repo("command sh check.sh\non-tip links=2 time=1h\n", files)
    (l1,) = _links(k, g, [{"a.txt": "a2\n"}])
    t = make_tester(k)
    t.step()
    t.step()
    chain, _ = k.run()
    assert chain.red.red and chain.red.first_bad == g.id


def test_auto_revert_after_timer():
    k, g = repo("command sh check.sh\non-tip links=3 time=1h\nauto-revert 5m\n")
    l1, l2 = _links(k, g, [{"b.txt": "BAD\n"}, {"c.txt": "c2\n"}])
    t = make_tester(k)
    t.step()
    t.step()
    chain, _ = k.run()
    assert chain.red.first_bad == l1.id
    assert t.maybe_auto_revert() is None                            # timer not yet due
    k.tick(5 * MIN)
    k.tick_entry()
    sent = t.step()
    chain, _ = k.run()
    revert = [link for link in chain.links if link.kind == "revert"]
    assert revert and revert[0].revert_of == l1.id and revert[0].author == "@t1"
    assert revert[0].push in sent
    assert k.replayer().reader().files()["b.txt"][1] == b"b\n"
    t.step()                                                        # the reverted tip passes: green
    chain, _ = k.run()
    assert not chain.red.red
    assert t.step() == []                                           # no second revert
