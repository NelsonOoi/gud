"""Tester agent against the fake mailbox. Skips until replay and apply_diff exist."""

from __future__ import annotations

import threading
import time

import pytest

from gud import sig
from gud.testkit import World
from gud.types import RunResult
from gud.wire import parse_body, tree_hash

PASSING = "def test_x():\n    assert 1 + 1 == 2\n"
BROKEN = "def test_x():\n    assert 1 + 1 == 3\n"
APP = "def f():\n    return 1\n"
APP_NEXT = "def f():\n    return 2\n"


def require_core():
    replay_mod = pytest.importorskip("gud.replay")
    if not callable(getattr(replay_mod, "replay", None)):
        pytest.skip("gud.replay.replay is not implemented yet")
    from gud.patch import apply_diff, diff_trees

    old = {"a.py": "a = 1\n"}
    try:
        got = apply_diff(dict(old), diff_trees(old, {"a.py": "a = 2\n"}))
    except NotImplementedError:
        pytest.skip("gud.patch.apply_diff is not implemented yet")
    if not isinstance(got, dict) or got.get("a.py") != "a = 2\n":
        pytest.fail(f"apply_diff did not round-trip diff_trees: {got!r}")


@pytest.fixture(autouse=True)
def _core():
    require_core()


@pytest.fixture
def open_tester():
    from gud.build import LocalRunner
    from gud.tester import Tester

    opened = []

    def make(world, runner=None):
        tester = Tester(
            world.mailbox, world.ids["@tester"], runner or LocalRunner(), 60, world.clock,
        )
        opened.append(tester)
        return tester

    yield make
    for tester in opened:
        tester.close()


def _step(tester):
    try:
        return tester.step()
    except NotImplementedError:
        pytest.skip("gud.replay.replay is not implemented yet")


def _targets(emails):
    return [parse_body(email.body).get("Gud-Target") for email in emails]


def _one(emails, target: str):
    matched = [email for email in emails if parse_body(email.body).get("Gud-Target") == target]
    assert len(matched) == 1, _targets(emails)
    return matched[0]


def _chain_text(world) -> str:
    from gud.replay import replay

    chain = replay(world.repo_mail(), 60, world.clock.t)
    lines = [f"link {link.seq} {link.author} {link.subject}" for link in chain.links]
    for prop in chain.proposals.values():
        lines.append(f"proposal {prop.status} {prop.author} {prop.subject} {prop.reason}")
    return "\n".join(lines)


def test_push_gets_a_pass_reply_for_its_tree(tmp_path, open_tester):
    world = World.create(tmp_path)
    genesis = world.genesis({"test_x.py": PASSING})
    pushed = world.edit("@alice", genesis, "readme.txt", "hello\n", intent="add readme")
    tester = open_tester(world)
    sent = _step(tester)
    assert pushed.id in _targets(sent), _chain_text(world)
    email = _one(sent, pushed.id)
    parsed = parse_body(email.body)
    assert parsed.kind == "test"
    assert parsed.get("Gud-Result") == "pass"
    assert parsed.get("Gud-Tree") == pushed.tree_hash
    assert parsed.get("Gud-Target") == pushed.id
    assert parsed.get("Gud-Author") == "@tester"
    assert parsed.diff == ""
    assert email.in_reply_to == pushed.id
    assert email.subject == "Re: " + pushed.email.subject
    assert email.from_addr == world.ids["@tester"].address
    assert sig.verify(world.members["@tester"].public_key, parsed.trailers, parsed.diff)
    assert _targets(sent).count(pushed.id) == 1


def test_push_that_breaks_the_suite_gets_a_fail(tmp_path, open_tester):
    world = World.create(tmp_path)
    genesis = world.genesis({"test_x.py": PASSING})
    pushed = world.edit("@alice", genesis, "test_x.py", BROKEN, intent="break the test")
    sent = _step(open_tester(world))
    parsed = parse_body(_one(sent, pushed.id).body)
    assert parsed.get("Gud-Result") == "fail"
    assert parsed.get("Gud-Tree") == pushed.tree_hash
    assert "assert" in parsed.text or "FAILED" in parsed.text


def test_tester_does_not_answer_twice(tmp_path, open_tester):
    world = World.create(tmp_path)
    genesis = world.genesis({"test_x.py": PASSING})
    pushed = world.edit("@alice", genesis, "readme.txt", "hello\n", intent="add readme")
    tester = open_tester(world)
    first = _step(tester)
    second = _step(tester)
    assert pushed.id in _targets(first)
    assert second == []
    tests = [
        email for email in world.repo_mail()
        if parse_body(email.body).kind == "test" and parse_body(email.body).get("Gud-Target") == pushed.id
    ]
    assert len(tests) == 1


def test_child_waits_until_parent_lands(tmp_path, open_tester):
    """A push whose parent is not a link is not tested. After the window, it is.

    Alice rewrites `f`, which genesis attributed to @lead, so consent waits out
    the 60s window even after the suite has passed. Bob's child stays untested
    until that push is a link.
    """
    world = World.create(tmp_path)
    genesis = world.genesis({"test_x.py": PASSING, "app.py": APP})
    parent = world.edit("@alice", genesis, "app.py", APP_NEXT, intent="return 2")
    child = world.edit("@bob", parent, "extra.py", "VALUE = 1\n", intent="add extra")
    tester = open_tester(world)

    first = _step(tester)
    assert parent.id in _targets(first), _chain_text(world)
    assert child.id not in _targets(first), _chain_text(world)
    assert parse_body(_one(first, parent.id).body).get("Gud-Result") == "pass"
    assert _step(tester) == [], _chain_text(world)

    world.tick(61)
    third = _step(tester)
    assert _targets(third) == [child.id], _chain_text(world)
    assert parse_body(third[0].body).get("Gud-Result") == "pass"
    assert parse_body(third[0].body).get("Gud-Tree") == child.tree_hash
    assert _step(tester) == []


def test_sibling_pushes_run_concurrently(tmp_path, open_tester):
    world = World.create(tmp_path)
    genesis = world.genesis({"test_x.py": PASSING})
    pushed = [
        world.edit(author, genesis, f"f{index}.py", f"VALUE = {index}\n", intent=f"add f{index}")
        for index, author in enumerate(("@alice", "@bob", "@carol"))
    ]

    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    class Slow:
        def run(self, tree):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            try:
                time.sleep(0.4)
            finally:
                with lock:
                    state["current"] -= 1
            return RunResult(True, "ok\n", tree_hash(tree), 0.4, "slow")

    started = time.monotonic()
    sent = _step(open_tester(world, Slow()))
    elapsed = time.monotonic() - started
    wanted = {push.id for push in pushed}
    assert wanted <= set(_targets(sent))
    assert {parse_body(email.body).get("Gud-Result") for email in sent} == {"pass"}
    assert state["peak"] >= 2
    assert elapsed < 1.3
