"""Per-hop latency over real IMAP IDLE (GreenMail): SMTP send -> IDLE wake (received) -> host log
-> replay delta -> journal -> agent webhook. Reported, and bounded by §20.2's 30 s IDLE wake."""

from __future__ import annotations

import statistics
import threading
import time

from gud.core import envelope as E
from gud.core.types import Seen
from gud.events.model import Delivery
from gud.host_v1 import open_host
from gud.transport import mime
from gud.transport.base import Outgoing
from gud.transport.imap_smtp import ImapSmtpMailbox
from tests.v1.kit import REPO, Kit
from tests.v1.transport import greenmail

from .harness import Receiver

ROUNDS = 5


def _box(gm, user: str) -> ImapSmtpMailbox:
    return ImapSmtpMailbox(gm.host, gm.imap_port, gm.host, gm.smtp_port, user, greenmail.PASSWORD,
                           gm.address(user), imap_tls="none", smtp_tls="none", idle_poll=10.0,
                           folders=("INBOX",))


def test_imap_idle_pipeline_latency(greenmail_server, tmp_path, capsys):
    gm = greenmail_server
    repo_user = "human"           # a mailbox no other test writes to
    kit = Kit()
    repo_box, sender = _box(gm, repo_user), _box(gm, "alice")
    host = open_host(tmp_path / "home", repo=REPO, mailbox=repo_box, host_private=kit.host_key,
                     persist_replay=False, intake_kw={"catch_up_interval_s": 3600.0,
                                                      "on_hop": lambda n, g, t: hops.append((n, g, t))})
    hops: list[tuple[str, str, float]] = []
    alice = Receiver(tmp_path / "alice.json", host.intake.host_keys, time.time)
    host.registry.set(Delivery("@alice", "webhook", alice.url))
    stop = threading.Event()
    runner = threading.Thread(target=host.intake.run, args=(stop,), daemon=True)
    runner.start()

    def send(msg) -> float:
        env = msg.env
        out = Outgoing(from_addr=gm.address("alice"), to=(gm.address(repo_user),),
                       subject=mime.subject_for(env, msg.id, env.kind), envelope=E.encode(env))
        t0 = time.perf_counter()
        sender.send(out)
        return t0

    def sign(name, *a, **kw):
        kit.now = max(kit.now + 1000, int(time.time() * 1000))
        head = host.intake.head()
        return getattr(kit, name)(*a, log=False, seen=Seen(head.seq, head.hash), **kw)

    def wait(pred, timeout=30.0):
        deadline = time.monotonic() + timeout
        while not pred():
            assert time.monotonic() < deadline
            time.sleep(0.005)

    rows = []
    try:
        time.sleep(1.0)                                   # IDLE armed
        g = sign("genesis", {"app.py": "a = 1\n"}, author="@alice")
        send(g)
        wait(lambda: host.replayer.tip is not None)
        for i in range(ROUNDS):
            p = sign("push", "@bob", g, {"app.py": f"a = {i + 2}\n"})
            t0 = send(p)
            wait(lambda: any(e.data["push"] == p.id for e in alice.of("review.requested")))
            ev = next(e for e in alice.of("review.requested") if e.data["push"] == p.id)
            first: dict[str, float] = {}
            for name, gid, t in list(hops):
                if gid == p.id:
                    first.setdefault(name, t)
            rows.append({
                "send->received": first["received"] - t0,
                "received->logged": first["logged"] - first["received"],
                "logged->delta": first["replayed"] - first["logged"],
                "delta->journal": first["journaled"] - first["replayed"],
                "journal->receiver": alice.arrivals[ev.id] - first["journaled"],
                "total": alice.arrivals[ev.id] - t0,
            })
    finally:
        stop.set()
        runner.join(15)
        alice.close()
    with capsys.disabled():
        print(f"\nIMAP IDLE (GreenMail) latency over {ROUNDS} pushes, ms (median / max):")
        for key in rows[0]:
            vals = [r[key] * 1000 for r in rows]
            print(f"  {key:18s} {statistics.median(vals):9.1f} / {max(vals):9.1f}")
    assert max(r["total"] for r in rows) < 30.0          # §20.2 IDLE wake bound
