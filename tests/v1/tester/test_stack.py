"""every-link batched stacks (§15.3): one run and one signed result vouch for the whole stack; a
failing stack is bisected over its prefixes and only a failing stack of one kills a candidate."""

from tests.v1.kit import status
from tests.v1.tester.util import chain_tests, make_tester, repo

POLICY = "command sh check.sh\nevery-link on\n"


def test_stack_pass_links_all_with_one_run():
    k, g = repo(POLICY)
    ps = [k.push("@alice", g, {f: f"{f}2\n"}) for f in ("a.txt", "b.txt", "c.txt")]
    t = make_tester(k)
    sent = t.step()
    assert len(sent) == 1 and len(t.runner.calls) == 1
    chain, _ = k.run()
    assert [l.push for l in chain.links[1:]] == [p.id for p in ps]
    env = chain_tests(k, "stack")[0]
    assert env.get("Gud-Target") == g.id and list(env.get_list("Gud-Stack")) == [p.id for p in ps]
    assert env.get("Gud-Tree") == chain.tip.tree_hash
    assert t.step() == []                        # nothing left to test


def test_stack_failure_bisects_and_kills_only_the_culprit():
    k, g = repo(POLICY)
    p1 = k.push("@alice", g, {"a.txt": "a2\n"})
    p2 = k.push("@alice", g, {"b.txt": "BAD\n"})
    p3 = k.push("@alice", g, {"c.txt": "c2\n"})
    t = make_tester(k)
    t.step()
    stacks = [(len(e.get_list("Gud-Stack")), e.get("Gud-Result")) for e in chain_tests(k, "stack")]
    assert stacks == [(3, "fail"), (1, "pass"), (2, "fail")]       # whole stack, then prefixes
    chain, _ = k.run()
    assert status(chain, p1) == "linked"
    assert status(chain, p2) == "testing" and status(chain, p3) == "testing"   # carried onto p1's link
    for _ in range(3):
        t.step()
        k.run()
    chain, _ = k.run()
    assert status(chain, p2) == "tests_failed"
    assert status(chain, p3) == "linked"
    assert [l.push for l in chain.links[1:]] == [p1.id, p3.id]
    lone = [e for e in chain_tests(k, "stack") if list(e.get_list("Gud-Stack")) == [p2.id]]
    assert lone and lone[0].get("Gud-Result") == "fail"


def test_stack_max_limits_the_batch():
    k, g = repo("command sh check.sh\nevery-link on\nstack-max 2\n")
    ps = [k.push("@alice", g, {f: f"{f}2\n"}) for f in ("a.txt", "b.txt", "c.txt")]
    t = make_tester(k)
    t.step()
    assert len(chain_tests(k, "stack")[0].get_list("Gud-Stack")) == 2
    chain, _ = k.run()
    assert [l.push for l in chain.links[1:]] == [ps[0].id, ps[1].id]
    t.step()
    chain, _ = k.run()
    assert [l.push for l in chain.links[1:]] == [p.id for p in ps]


def test_conflicting_push_ends_the_stack():
    k, g = repo(POLICY)
    p1 = k.push("@alice", g, {"a.txt": "a2\n"})
    p2 = k.push("@alice", g, {"a.txt": "a3\n"})        # same file: not carryable after p1
    p3 = k.push("@alice", g, {"c.txt": "c2\n"})
    t = make_tester(k)
    t.step()
    assert [list(e.get_list("Gud-Stack")) for e in chain_tests(k, "stack")] == [[p1.id]]
    chain, _ = k.run()
    assert status(chain, p1) == "linked" and status(chain, p2) == "superseded"
    t.step()
    chain, _ = k.run()
    assert status(chain, p3) == "linked"


def test_stack_waits_behind_a_candidate_waiting_for_consent():
    k, g = repo(POLICY)
    slow = k.push("@bob", g, {"a.txt": "a2\n"})          # touches alice's lines: waits for her window
    fast = k.push("@alice", g, {"b.txt": "b2\n"})
    t = make_tester(k)
    t.step()
    t.step()
    assert len(chain_tests(k, "stack")) == 1             # [slow, fast] covers both; no re-runs
    chain, _ = k.advance(60_000 + 30_000 + 10)
    assert status(chain, slow) == "linked" and status(chain, fast) == "linked"
