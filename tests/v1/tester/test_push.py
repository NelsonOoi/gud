"""on-paths push duty (§15.1): real subprocess runs, signed push-scope results that replay accepts,
carried candidates, the concurrency limit, and sending through a transport adapter."""

import threading
import time

from gud.core.replay import Replayer
from gud.runners.base import RunResult
from gud.tester import LocalClient, Tester, TesterIdentity, mailbox_sender
from gud.transport import mime
from gud.transport.fake import FakeMailbox
from tests.v1.kit import REPO, status
from tests.v1.tester.util import PY, chain_tests, make_tester, repo

POLICY = "command sh check.sh\non-paths a.txt,b.txt,c.txt\n"


def test_on_paths_pass_links_and_fail_kills():
    k, g = repo(POLICY)
    good = k.push("@alice", g, {"a.txt": "a2\n"})
    t = make_tester(k)
    sent = t.step()
    assert len(sent) == 1
    chain, _ = k.run()
    assert status(chain, good) == "linked"
    env = chain_tests(k, "push")[0]
    assert env.get("Gud-Result") == "pass" and env.get("Gud-Target") == good.id
    assert env.get("Gud-Tree") == good.tree.hash() and env.author == "@t1"
    assert "exit 0" in env.text and "runner local" in env.text

    bad = k.push("@alice", chain.tip.state, {"b.txt": "BAD\n"})
    t.step()
    chain, events = k.run()
    assert status(chain, bad) == "tests_failed"
    failed = chain_tests(k, "push")[-1]
    assert failed.get("Gud-Result") == "fail" and "FAIL b.txt" in failed.text


def test_push_outside_on_paths_is_not_tested():
    k, g = repo(POLICY)
    p = k.push("@alice", g, {"d.txt": "d2\n"})
    t = make_tester(k)
    assert t.step() == []
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_no_command_means_nothing_to_do():
    k, g = repo("on-paths a.txt\n")
    k.push("@alice", g, {"a.txt": "a2\n"})
    t = make_tester(k)
    assert t.step() == []
    assert t.runner.calls == []


def test_pytest_command_pass_and_fail():
    files = {"calc.py": "def add(a, b):\n    return a + b\n",
             "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"}
    k, g = repo(f"command {PY} -m pytest -q -p no:cacheprovider\non-paths calc.py\n", files)
    ok = k.push("@alice", g, {"calc.py": "def add(a, b):\n    return b + a\n"})
    t = make_tester(k)
    t.step()
    chain, _ = k.run()
    assert status(chain, ok) == "linked"
    broken = k.push("@alice", chain.tip.state, {"calc.py": "def add(a, b):\n    return a - b\n"})
    t.step()
    chain, _ = k.run()
    assert status(chain, broken) == "tests_failed"
    env = chain_tests(k, "push")[-1]
    assert "1 failed" in env.text


def test_carried_candidate_is_tested_on_its_carried_tree():
    k, g = repo(POLICY)
    first = k.push("@alice", g, {"d.txt": "d2\n"})          # needs no test, links first
    p = k.push("@alice", g, {"a.txt": "a2\n"})
    chain, _ = k.run()
    cand = chain.push_status(p.id)
    assert cand.id == p.carried(first) and cand.status == "testing"
    t = make_tester(k)
    t.step()
    chain, _ = k.run()
    assert status(chain, p) == "linked"
    env = chain_tests(k, "push")[0]
    assert env.get("Gud-Target") == cand.id and env.get("Gud-Tree") == cand.tree_hash


class _SlowRunner:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def run(self, files, command, *, image, timeout_ms, extra_args=()):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.15)
        with self.lock:
            self.active -= 1
        return RunResult(True, "ok\n", 0.15, "slow")


def test_parallel_limit_caps_concurrent_runs():
    k, g = repo("command true\non-paths *.txt\n")
    for f in ("a.txt", "b.txt", "c.txt", "d.txt"):
        k.push("@bob", g, {f: f"{f}2\n"})          # bob's pushes wait on alice's window: all stay candidates
    runner = _SlowRunner()
    t = make_tester(k, runner=runner, parallel=2)
    sent = t.step()
    assert len(sent) == 4
    assert runner.peak == 2


def test_results_go_through_a_transport_adapter(tmp_path):
    k, g = repo(POLICY)
    p = k.push("@alice", g, {"a.txt": "a2\n"})
    t1_box = FakeMailbox(tmp_path, k.address("@t1"), clock=lambda: k.now / 1000)
    repo_box = FakeMailbox(tmp_path, REPO, clock=lambda: k.now / 1000)
    cursor = [None]

    def fetch():
        # a minimal host intake: log whatever reached the repo mailbox
        items, cursor[0] = repo_box.list(since=cursor[0])
        for item in items:
            email = mime.to_email(item.raw_mime, received_at=item.received_at)
            if email is not None and email.gud_id not in {e.gud_id for e in k.emails}:
                k.emails.append(email)
                k.log.append_mail(email.gud_id, k.now, k.host_key, max(k.now, k.log.head.time + 1))
                k.now = k.log.head.time + 1
        return list(k.emails), k.entries()

    send = mailbox_sender(t1_box, from_addr=k.address("@t1"), repo=REPO)
    client = LocalClient(Replayer(k.store), fetch, send, now=lambda: k.now)
    t = Tester(client, make_tester(k).runner, identity=TesterIdentity("@t1", k.key("@t1")), clock=lambda: k.now / 1000)
    sent = t.step()
    fetch()
    chain, _ = k.run()
    assert status(chain, p) == "linked"
    assert sent and sent[0] in {e.gud_id for e in k.emails}
    raw = repo_box.list()[0][-1].raw_mime
    assert b"gud.env" in raw and b"[gud:" + sent[0][:12].encode() + b"]" in raw
