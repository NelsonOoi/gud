"""Integration regressions on RepoClient against the real host: the tester and audit work through
a member's RepoClient, and members follow a repo that moved address."""

from __future__ import annotations

from pathlib import Path

from gud.core.types import Record
from gud.ops.audit import audit
from gud.tester_v1 import LocalRunner, Tester, TesterIdentity

from tests.v1.kit import Kit, status
from tests.v1.wc.conftest import Net


def test_tester_runs_over_a_repo_client(tmp_path: Path):
    net = Net(tmp_path, test="command test -f a.txt\non-paths **\n")
    net.genesis({"a.txt": "a\n"})
    alice = net.clone("@alice")
    (alice.root / "b.txt").write_text("b\n")
    push = alice.push("b").push
    net.settle()
    alice.client.sync_log()
    assert status(alice.client.chain(), push) == "testing"
    t1 = net.client("@t1")
    tester = Tester(t1, LocalRunner(), identity=TesterIdentity("@t1", net.kit.key("@t1")),
                    state_dir=tmp_path / "tester", clock=net.clock)
    sent = tester.step()
    assert len(sent) == 1
    net.settle()
    alice.client.sync_log()
    assert status(alice.client.chain(), push) == "linked"
    assert tester.step() == []                                  # one result per key


def test_audit_reads_the_served_log_not_the_cache(tmp_path: Path):
    """Regression: with a RepoClient, audit used the member's own verified log as the "served"
    log, so it compared the cache with itself and passed a forked host."""
    net = Net(tmp_path)
    net.genesis({"a.txt": "a\n"})
    c = net.client("@alice")
    c.sync_log()
    assert audit(c).exit_status == 0
    other = Kit()
    other.host_key = b"\x01" * 32
    net.host.forge_log(other.host_key)
    net.clock.advance(2000)
    for _ in range(4):
        net.clock.advance(1000)
        net.host.forged_tick()
    report = audit(c)
    assert report.exit_status == 2 and not report.ok


def test_members_send_to_the_current_repo_address(tmp_path: Path):
    """After a handover that moved the repo, a member's RepoClient sends to the new address
    (the verified log's latest open), while its cache keeps its name."""
    from gud.core import envelope as E
    net = Net(tmp_path)
    net.genesis({"a.txt": "a\n"})
    c = net.client("@alice")
    c.sync_log()
    assert c.repo_address == c.repo
    new_private, new_pub = E.generate_key()
    net.host.intake.handover(new_pub, repo="moved@example.org")
    log = net.host.intake.log
    log.continue_after_handover(new_private, net.clock.ms + 10)   # what the new host's log begins with
    net.clock.advance(1000)
    c.sync_log()
    assert c.replayer().log().repos == (c.repo, "moved@example.org")
    assert c.repo_address == "moved@example.org" and c.cache_dir.name == c.repo
    c.sign_and_send("comment", {"Gud-Target": c.chain().tip.state},
                    (Record("text", (), b"hi"),), summary="hi")
    moved = net.host.mailbox_for("moved@example.org").list()[0]
    assert len(moved) == 1
