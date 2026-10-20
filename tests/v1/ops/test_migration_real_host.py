"""Host migration on the real host (gud.host_v1): planned handover, archive, import at a new
host, optionally under a new repo address (integration note 4)."""

from __future__ import annotations

import pytest

from gud.core import envelope as E
from gud.core.hostlog import HostLog
from gud.host_v1 import open_host
from gud.ops.archive import ArchiveError, export_archive, import_archive, verify_archive
from tests.v1.events.harness import World
from tests.v1.kit import REPO

NEW = "moved@new.example.org"


def _migrate(w: World, tmp, *, repo: str | None):
    new_private, new_pub = E.generate_key()
    before = w.chain()
    handover = w.intake.handover(new_pub, repo=repo)
    # the old host logs nothing after its handover
    w.push("@bob", before.tip.state, {"late.txt": "late\n"})
    w.advance(1000)
    assert w.intake.log.head.hash == handover.hash
    out = export_archive(w.intake, tmp / "archive", signer=w.kit.host_key)
    assert verify_archive(out).ok
    home = tmp / "new"
    log = import_archive(out, repo=repo, host_private=new_private, mail_store=home / "host" / "mail",
                         log_path=home / "host" / "log.jsonl")
    box = w.net.mailbox(repo or REPO)          # same address: the new host reads the same mailbox
    host = open_host(home, repo=log.repo, mailbox=box, host_private=new_private, clock=w.net.clock)
    sub = box.subscribe(host.intake.on_received)
    return host, log, new_private, before, sub


def test_migration_to_a_new_repo_address(tmp_path):
    w = World(tmp_path)
    try:
        g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
        p = w.push("@bob", g.id, {"notes.txt": "hi\n"})
        w.advance(1000)
        assert w.chain().tip.push == p.id
        host, log, new_private, before, sub = _migrate(w, tmp_path, repo=NEW)
        try:
            assert log.repo == NEW and log.repos == (REPO, NEW)
            HostLog(log.entries()).verify()
            chain = host.replayer.chain()
            assert chain.links == before.links                       # the chain continues unchanged
            # members follow: their mail goes to the new address and the new host logs it
            seen = host.intake.seen()
            w.kit.now = w.net.now_ms
            msg = w.kit.push("@carol", chain.tip.state, {"more.txt": "more\n"}, log=False, seen=seen)
            from gud.transport import mime
            from gud.transport.base import Outgoing
            w.net.mailbox(w.kit.address("@carol")).send(Outgoing(
                from_addr=w.kit.address("@carol"), to=(NEW,), subject=mime.subject_for(msg.env, msg.id, "push"),
                text="", envelope=E.encode(msg.env)))
            w.net.advance(1000)
            host.intake.tick()
            assert host.replayer.chain().tip.push == msg.id
            # re-importing with the old address is refused: only the handover moves it
            with pytest.raises(ArchiveError, match="moves the repo"):
                import_archive(tmp_path / "archive", repo=REPO, host_private=new_private)
        finally:
            sub.close()
    finally:
        w.close()


def test_migration_keeping_the_address(tmp_path):
    w = World(tmp_path)
    try:
        w.genesis({"app.py": "a = 1\n"}, author="@alice")
        host, log, new_private, before, sub = _migrate(w, tmp_path, repo=None)
        sub.close()
        assert log.repo == REPO and log.repos == (REPO,) and log.key == E.public_of(new_private)
        assert host.replayer.chain().links == before.links
    finally:
        w.close()
