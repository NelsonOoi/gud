"""Evidence objections (§13.5): the tester runs the objector's test file on the parent and on the
push; pass-then-fail confirms, anything else refutes; revisions are re-run and a revision that
carries the file and passes clears it."""

from tests.v1.kit import status
from tests.v1.tester.util import PY, chain_tests, make_tester, repo

W, G = 60_000, 30_000
POLICY = f"command {PY} -m pytest -q -p no:cacheprovider\non-tip off\n"
CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
BROKEN = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a + b\n"
FIXED = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b  # kept\n"
EVIDENCE = b"from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"


def setup():
    k, g = repo(POLICY, {"calc.py": CALC})
    p = k.push("@bob", g, {"calc.py": BROKEN})            # alice owns the lines: she is asked
    return k, g, p


def objection_state(chain, push, oid):
    prop = chain.push_status(push.id)
    return next(o for o in prop.objections if o.id == oid)


def test_confirmed_evidence_blocks_the_push():
    k, g, p = setup()
    o = k.object("@alice", p, "sub is broken", evidence=("tests/test_sub.py", EVIDENCE))
    t = make_tester(k)
    t.step()
    env = chain_tests(k, "evidence")[0]
    assert env.get("Gud-Target") == o.id and env.get("Gud-On") == p.id
    assert env.get("Gud-Base-Result") == "pass" and env.get("Gud-Result") == "fail"
    assert "evidence confirmed" in env.text
    expected = k.tree(p).set("tests/test_sub.py", "100644", EVIDENCE).hash()
    assert env.get("Gud-Tree") == expected
    chain, _ = k.advance(W + G + 10)
    assert objection_state(chain, p, o.id).status == "confirmed"
    assert status(chain, p) != "linked"
    assert t.step() == []                                    # one verdict per objection and push


def test_refuted_evidence_does_not_block():
    k, g, p = setup()
    wrong = b"from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 3\n"   # fails on the parent too
    o = k.object("@alice", p, "sub is broken", evidence=("tests/test_sub.py", wrong))
    make_tester(k).step()
    env = chain_tests(k, "evidence")[0]
    assert env.get("Gud-Base-Result") == "fail" and "evidence refuted" in env.text
    chain, _ = k.run()
    assert objection_state(chain, p, o.id).status in ("refuted", "advisory")
    k.comment("@bob", o, "the test is wrong")                # answering a reason-only objection
    chain, _ = k.advance(W + G + 10)
    assert status(chain, p) == "linked"


def test_revision_is_rerun_and_clears_when_it_carries_the_file():
    k, g, p = setup()
    o = k.object("@alice", p, "sub is broken", evidence=("tests/test_sub.py", EVIDENCE))
    t = make_tester(k)
    t.step()
    r = k.push("@bob", g, {"calc.py": FIXED, "tests/test_sub.py": EVIDENCE}, revises=p)
    t.step()
    envs = chain_tests(k, "evidence")
    assert len(envs) == 2
    rerun = envs[1]
    assert rerun.get("Gud-On") == r.id and rerun.get("Gud-Result") == "pass"
    assert rerun.get("Gud-Base-Result") is None            # only required on the objected push
    assert rerun.get("Gud-Tree") == k.tree(r).hash()        # the revision already holds the file
    chain, _ = k.advance(2 * (W + G) + 10)
    assert status(chain, r) == "linked"
    assert status(chain, p) != "linked"


def test_revision_still_failing_is_reported():
    k, g, p = setup()
    k.object("@alice", p, "sub is broken", evidence=("tests/test_sub.py", EVIDENCE))
    t = make_tester(k)
    t.step()
    r = k.push("@bob", g, {"calc.py": BROKEN.replace("a + b\n", "a + b  # still\n", 1)}, revises=p)
    t.step()
    rerun = chain_tests(k, "evidence")[-1]
    assert rerun.get("Gud-On") == r.id and rerun.get("Gud-Result") == "fail"
