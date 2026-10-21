"""Integration regressions on RepoClient against the real host: the tester and audit work through
a member's RepoClient, and members follow a repo that moved address."""

from __future__ import annotations

from pathlib import Path

from gud.core.types import Record
from gud.ops.audit import audit
from gud.tester import LocalRunner, Tester, TesterIdentity

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


def test_sync_after_the_tip_built_on_my_own_linked_change(tmp_path: Path):
    """Regression (e2e): a member whose push linked, and who did not sync since, conflicted with
    a later link that rewrote the same lines on top of that push. Sync now merges each changed
    path from the latest link where it equals the working tree."""
    net = Net(tmp_path)
    util = "def triple(x):\n    return 3 * x\n"
    net.genesis({"util.py": util})
    bob, alice = net.clone("@bob"), net.clone("@alice")
    (bob.root / "util.py").write_text(util.replace("3 * x", "x * 3"))
    p = bob.push("bob's edit").push
    net.settle()
    net.host.approve("@nelson", p)
    net.settle()
    bob.client.sync_log()
    assert status(bob.client.chain(), p) == "linked"
    assert alice.sync().ok
    (alice.root / "util.py").write_text('def triple(x):\n    """Three times x."""\n    return x * 3\n')
    q = alice.push("docstring").push
    net.settle()
    net.host.approve("@nelson", q)                     # the insertion is inside his def
    net.settle()
    alice.client.sync_log()
    assert status(alice.client.chain(), q) == "linked"
    result = bob.sync()                                # bob's base is still the genesis
    assert result.ok, result.conflicts
    assert (bob.root / "util.py").read_text() == 'def triple(x):\n    """Three times x."""\n    return x * 3\n'
    assert bob.status().clean


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


def test_build_output_is_never_pushed(tmp_path: Path):
    """Regression (live run): __pycache__ and friends were linked. Without a .gudignore the default
    rules apply; with one that lacks them, the push refuses build output unless allowed."""
    import pytest
    from gud.wc.copy import ArtifactsFound
    net = Net(tmp_path)
    net.genesis({"app.py": "x = 1\n"})
    alice = net.clone("@alice")
    (alice.root / "__pycache__").mkdir()
    (alice.root / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\0pyc")
    (alice.root / ".pytest_cache").mkdir()
    (alice.root / ".pytest_cache" / "README").write_text("cache\n")
    (alice.root / "new.py").write_text("y = 2\n")
    assert alice.status().added == ("new.py",)                # default rules: no .gudignore anywhere
    (alice.root / ".gudignore").write_text("*.log\n")         # a .gudignore that forgets them
    with pytest.raises(ArtifactsFound) as exc:
        alice.push("oops")
    assert "__pycache__/app.cpython-312.pyc" in exc.value.paths and "__pycache__/" in str(exc.value)
    result = alice.push("on purpose", allow_artifacts=True)
    assert "__pycache__/app.cpython-312.pyc" in result.files


def test_on_delta_hook_sees_working_copy_syncs(tmp_path: Path):
    net = Net(tmp_path)
    net.genesis({"a.txt": "a\n"})
    alice = net.clone("@alice")
    deltas: list = []
    alice.client.on_delta.append(deltas.append)
    (alice.root / "b.txt").write_text("b\n")
    p = alice.push("b").push                                 # push syncs inside WorkingCopy
    net.settle()
    alice.sync()
    events = [e for d in deltas for e in d.events]
    assert deltas and any(e.type == "linked" and e.data["push"] == p for e in events)


def test_host_transport_secrets_come_from_the_environment(tmp_path: Path, monkeypatch):
    """Regression (live run): a host directory's transport.toml with `<key>_env` (so the API key
    is not on disk) was read without resolving it, so `gud host serve` had no key."""
    from gud.transport.registry import read_transport_toml
    from gud.wc.remote import LocalHost
    monkeypatch.setenv("GUD_TEST_MAIL_ROOT", str(tmp_path / "mail"))
    host = LocalHost.create(tmp_path / "host", repo="repo@example.org", transport="fake",
                            cfg={"root_env": "GUD_TEST_MAIL_ROOT"})
    text = (tmp_path / "host" / "transport.toml").read_text()
    assert "root_env" in text and str(tmp_path / "mail") not in text
    name, cfg = read_transport_toml(tmp_path / "host" / "transport.toml")
    assert name == "fake" and cfg["root"] == str(tmp_path / "mail")
    assert Path(host.mailbox().root) == (tmp_path / "mail").resolve()


def test_resolve_accepts_mail_forms(tmp_path: Path):
    net = Net(tmp_path)
    g = net.genesis({"a.txt": "a\n"})
    c = net.client("@alice")
    c.sync_log()
    email = c.reader().email(g)
    assert c.resolve(f"[gud:{g[:12]}]") == g
    assert c.resolve(f"Re: [gud] push genesis [gud:{g[:12]}]") == g
    assert c.resolve(f"<{g}>") == g and c.resolve(f"gud:{g[:8]}") == g
    assert email.message_id and c.resolve(email.message_id) == g
