"""Task acceptance (§16.2, §16.6): criteria run on the tip once work is done; a pass makes the task
done, a fail keeps it at work_done until a later link passes; nothing runs while red."""

from tests.v1.kit import status
from tests.v1.tester.util import PY, chain_tests, make_tester, repo

PYTEST = f"command {PY} -m pytest -q -p no:cacheprovider\n"
FILES = {"calc.py": "def add(a, b):\n    return 0\n", "a.txt": "a\n",
         "check.sh": ("100755", "#!/bin/sh\nfor f in *.txt; do grep -q BAD \"$f\" && exit 1; done; exit 0\n")}
FEATURE_TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def test_accept_paths_pass_makes_task_done():
    k, g = repo(PYTEST, FILES)
    task = k.task("@alice", "add works", accept=["tests/test_*.py"])
    k.push("@alice", g, {"calc.py": "def add(a, b):\n    return a + b\n", "tests/test_add.py": FEATURE_TEST},
           task=task.id)
    k.declare("@alice", task)
    chain, _ = k.run()
    assert chain.tasks[task.id].status == "work_done"
    t = make_tester(k)
    t.step()
    chain, _ = k.run()
    assert chain.tasks[task.id].status == "done"
    env = chain_tests(k, "accept")[0]
    assert env.get("Gud-Target") == task.id and env.get("Gud-Link") == chain.tip.state
    assert env.get("Gud-Result") == "pass"
    assert t.runner.calls[-1][1] == ("tests/test_add.py",)          # the matched paths are the arguments


def test_accept_fail_then_fix_on_a_later_link():
    k, g = repo(PYTEST, FILES)
    task = k.task("@alice", "add works", accept=["tests/test_add.py"])
    p = k.push("@alice", g, {"tests/test_add.py": FEATURE_TEST}, task=task.id)     # test only: fails
    k.declare("@alice", task)
    chain, _ = k.run()
    t = make_tester(k)
    t.step()
    chain, _ = k.run()
    assert chain.tasks[task.id].status == "work_done"
    assert chain_tests(k, "accept")[0].get("Gud-Result") == "fail"
    assert t.step() == []                                                         # same link: no rerun
    k.push("@alice", p, {"calc.py": "def add(a, b):\n    return a + b\n"})
    k.run()
    t.step()
    chain, _ = k.run()
    assert chain.tasks[task.id].status == "done"
    assert [e.get("Gud-Result") for e in chain_tests(k, "accept")] == ["fail", "pass"]


def test_accept_commands():
    k, g = repo("command sh check.sh\n", FILES)
    task = k.task("@alice", "flag", cmds=["test -f done.flag", "grep -q yes done.flag"])
    k.push("@alice", g, {"done.flag": "no\n"}, task=task.id)
    k.declare("@alice", task)
    k.run()
    t = make_tester(k)
    t.step()
    chain, _ = k.run()
    assert chain.tasks[task.id].status == "work_done"
    env = chain_tests(k, "accept")[0]
    assert env.get("Gud-Result") == "fail" and "grep -q yes done.flag: fail" in env.text
    k.push("@alice", chain.tip.state, {"done.flag": "yes\n"})
    k.run()
    t.step()
    chain, _ = k.run()
    assert chain.tasks[task.id].status == "done"


def test_glob_matching_nothing_fails():
    k, g = repo(PYTEST, FILES)
    task = k.task("@alice", "x", accept=["tests/missing_*.py"])
    k.push("@alice", g, {"a.txt": "a2\n"}, task=task.id)
    k.declare("@alice", task)
    k.run()
    make_tester(k).step()
    env = chain_tests(k, "accept")[0]
    assert env.get("Gud-Result") == "fail" and "matches no file" in env.text


def test_no_test_command_cannot_pass():
    k, g = repo("on-tip off\n", FILES)
    task = k.task("@alice", "x", cmds=["true"])
    k.push("@alice", g, {"a.txt": "a2\n"}, task=task.id)
    k.declare("@alice", task)
    k.run()
    t = make_tester(k)
    t.step()
    chain, _ = k.run()
    assert chain.tasks[task.id].status == "work_done"
    env = chain_tests(k, "accept")[0]
    assert env.get("Gud-Result") == "fail" and "cannot pass" in env.text
    assert t.runner.calls == []


def test_no_acceptance_runs_while_red():
    k, g = repo("command sh check.sh\non-tip links=2 time=1h\n", FILES)
    task = k.task("@alice", "flag", cmds=["true"])
    p = k.push("@alice", g, {"a.txt": "BAD\n"}, task=task.id)
    k.declare("@alice", task)
    k.run()
    t = make_tester(k)
    t.step()                       # the tip run fails first; acceptance is skipped once red is seen
    t.step()
    chain, _ = k.run()
    assert chain.red.red and chain.tasks[task.id].status == "work_done"
    assert status(chain, p) == "linked"
    assert chain_tests(k, "accept") == []
