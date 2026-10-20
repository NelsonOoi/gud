"""Idempotence and restart safety: never two results for one (scope, target, tree); a crash between
persisting and sending re-sends the same bytes; a lost state directory is rebuilt from the log."""

import json

import pytest

from gud.core.types import Event
from tests.v1.kit import status
from tests.v1.tester.util import chain_tests, make_tester, repo

POLICY = "command sh check.sh\non-paths a.txt\non-tip off\n"


def test_restart_with_state_does_not_duplicate(tmp_path):
    k, g = repo(POLICY)
    p = k.push("@alice", g, {"a.txt": "a2\n"})
    first = make_tester(k, state_dir=tmp_path)
    sent = first.step()
    assert sent
    before = len(chain_tests(k))
    again = make_tester(k, state_dir=tmp_path)
    assert again.step() == [] and again.runner.calls == []
    assert len(chain_tests(k)) == before
    keys = [(e.get("Gud-Scope"), e.get("Gud-Target"), e.get("Gud-Tree")) for e in chain_tests(k)]
    assert len(keys) == len(set(keys))
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_restart_runs_nothing_already_answered(tmp_path):
    k, g = repo(POLICY.replace("on-tip off", "on-tip links=1 time=1h"))
    k.push("@alice", g, {"a.txt": "a2\n"})
    t = make_tester(k, state_dir=tmp_path)
    t.step()
    t.step()                                              # tip run on the new link
    n = len(chain_tests(k))
    fresh = make_tester(k, state_dir=tmp_path)
    assert fresh.step() == [] and fresh.runner.calls == []
    assert len(chain_tests(k)) == n
    doc = json.loads((tmp_path / "tester.json").read_text())
    assert doc["handle"] == "@t1" and all(rec["sent"] for rec in doc["sent"].values())


def test_crash_between_persist_and_send_resends_the_same_message(tmp_path):
    k, g = repo(POLICY)
    p = k.push("@alice", g, {"a.txt": "a2\n"})

    def broken(env, summary, text):
        raise ConnectionError("smtp down")

    t = make_tester(k, state_dir=tmp_path, send=broken)
    with pytest.raises(ConnectionError):
        t.step()
    assert chain_tests(k) == []
    doc = json.loads((tmp_path / "tester.json").read_text())
    (rec,) = doc["sent"].values()
    assert rec["sent"] is False
    revived = make_tester(k, state_dir=tmp_path)
    sent = revived.step()
    assert sent[0] == rec["id"]
    assert revived.runner.calls == []                     # the run result was persisted with the envelope
    assert [k_.gud_id for k_ in k.emails if k_.envelope and k_.envelope.kind == "test"][0] == rec["id"]
    chain, _ = k.run()
    assert status(chain, p) == "linked"


def test_lost_state_is_rebuilt_from_the_log():
    k, g = repo("command sh check.sh\non-tip links=1 time=1h\n")
    task = k.task("@alice", "flag", cmds=["test -f flag"])
    k.push("@alice", g, {"b.txt": "b2\n"}, task=task.id)
    k.run()
    t = make_tester(k)                                     # no state dir
    t.step()
    assert {e.get("Gud-Scope") for e in chain_tests(k)} == {"tip", "accept"}
    n = len(chain_tests(k))
    amnesiac = make_tester(k)
    assert amnesiac.step() == []
    assert amnesiac.runner.calls == [] and len(chain_tests(k)) == n


def test_run_forever_steps_per_event():
    k, g = repo(POLICY)
    t = make_tester(k)
    calls = []
    t.step = lambda: calls.append(1) or []                 # type: ignore[method-assign]
    ev = Event("0" * 64, "push.received", 0, (), {})
    t.run_forever(iter([ev, ev]))
    assert len(calls) == 3
