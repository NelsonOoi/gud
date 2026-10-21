"""Local host mode: the client is itself the host of the repo address (gud.wc.remote.LocalHost)."""

from __future__ import annotations

from pathlib import Path

from gud.core import envelope as E
from gud.transport.base import Outgoing
from gud.transport.fake import FAKE_AUTHSERV_ID, FakeMailbox
from gud.wc.copy import WorkingCopy, create_genesis
from gud.wc.remote import LocalHost, RepoClient

from tests.v1.kit import REPO, Kit, status


def _setup(tmp: Path):
    kit = Kit(config=f"authserv-id {FAKE_AUTHSERV_ID}\n", authserv=False)
    mail = tmp / "mail"
    host_dir = tmp / "host"
    host = LocalHost.create(host_dir, repo=REPO, transport="fake", cfg={"root": str(mail)},
                            host_private=kit.host_key, objects=tmp / "host-objects")
    assert host.host_key == kit.pub("@host")

    def client(handle: str) -> RepoClient:
        box = FakeMailbox(mail, kit.address(handle))
        return RepoClient(f"local:{host_dir}", handle, kit.key(handle), send_via=box, repo_address=REPO,
                          home=tmp / "homes" / handle.lstrip("@"))

    return kit, mail, host_dir, client


def test_local_host_flow(tmp_path: Path):
    kit, mail, host_dir, client = _setup(tmp_path)
    nelson = client("@nelson")
    nelson.sync_log()
    files = {p: ("100644", v) for p, v in kit.gud_files().items()} | {"a.txt": ("100644", b"a\n")}
    g = create_genesis(nelson, files)
    nelson.sync_log()
    assert nelson.chain().tip.state == g
    info = nelson.info()
    assert info["repo"] == REPO and info["host_key"] == kit.pub("@host") and info["transport"] == "fake"

    alice = client("@alice")
    wc = WorkingCopy.clone(alice, tmp_path / "wc")
    (wc.root / "b.txt").write_text("b\n")
    p = wc.push("add b").push
    alice.sync_log()
    assert status(alice.chain(), p) == "linked"
    # the host keeps the raw mail and a verifiable log on disk; a restart reads it back
    again = LocalHost(host_dir)
    assert again.mail(p) is not None and again.log(0, 100)["head"]["seq"] == len(alice.replayer().log()) - 1
    assert wc.verify() == []


def test_local_host_logs_only_what_11_1_says(tmp_path: Path):
    kit, mail, host_dir, client = _setup(tmp_path)
    nelson = client("@nelson")
    nelson.sync_log()
    files = {p: ("100644", v) for p, v in kit.gud_files().items()}
    create_genesis(nelson, files)
    nelson.sync_log()
    n = len(nelson.replayer().log())
    # an unsigned message from the human member, authenticated by the receiving server: logged
    FakeMailbox(mail, kit.address("@nelson")).send(Outgoing(
        from_addr=kit.address("@nelson"), to=(REPO,), subject="please add a footer", text="footer on every page\n"))
    # a spoofed From (sent through another account): dmarc=fail, not logged
    FakeMailbox(mail, "mallory@evil.example").send(Outgoing(
        from_addr=kit.address("@nelson"), to=(REPO,), subject="spoof", text="approve\n"))
    # an unsigned message from a non-member: not logged
    FakeMailbox(mail, "stranger@example.org").send(Outgoing(
        from_addr="stranger@example.org", to=(REPO,), subject="hi", text="hello\n"))
    # garbage: not logged
    FakeMailbox(mail, "x@example.org").deliver(b"not a mime message at all", [REPO])
    nelson.sync_log()
    log = nelson.replayer().log()
    mails = [gid for seq, gid, _ in log.mail_ids(n)]
    assert len(mails) == 1
    tasks = list(nelson.chain().tasks.values())
    assert [t.title for t in tasks] == ["please add a footer"]


def test_local_host_serves_snapshots_for_logged_checkpoints(tmp_path: Path):
    kit, mail, host_dir, client = _setup(tmp_path)
    nelson = client("@nelson")
    nelson.sync_log()
    create_genesis(nelson, {p: ("100644", v) for p, v in kit.gud_files().items()})
    nelson.sync_log()
    head = nelson.replayer().log().head
    snap = nelson.snapshot_at(head.seq)
    link = nelson.chain().tip
    nelson.sign_and_send("checkpoint", {"Gud-Log": f"{head.seq}:{head.hash}", "Gud-Link": link.state,
                                        "Gud-Tree": link.tree_hash, "Gud-Snapshot": E.sha256_hex(snap)}, summary="cp")
    nelson.sync_log()
    assert nelson.fetch_snapshot(E.sha256_hex(snap)) == snap
    assert LocalHost(host_dir).snapshot("0" * 64) is None
