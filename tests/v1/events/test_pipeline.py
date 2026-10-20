"""End-to-end event pipeline on a FakeNetwork (PLAN Phase 1b, SPEC §11, §19).

member sends mail -> repo mailbox push intake -> dedupe -> host log -> incremental replay ->
journal -> agent webhook (a real local HTTP receiver) -> EventClient (verify, dedupe).
"""

from __future__ import annotations

import threading
import time

import pytest

from gud.core.tasks import fix_task_id
from gud.events.model import validate
from gud.transport import mime
from gud.transport.fake import Faults
from tests.v1.kit import REPO

from .harness import World

W, G = 60_000, 30_000
BUDGET_S = 5.0


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def _hops(w: World, gid: str, event_id: str, receiver, sent_at: float) -> dict[str, float]:
    first: dict[str, float] = {}
    for name, g, t in w.hops:
        if g == gid:
            first.setdefault(name, t)
    arrival = receiver.arrivals[event_id]
    return {
        "send->received": first["received"] - sent_at,
        "received->logged": first["logged"] - first["received"],
        "logged->delta": first["replayed"] - first["logged"],
        "delta->journal": first["journaled"] - first["replayed"],
        "journal->receiver": arrival - first["journaled"],
        "total": arrival - sent_at,
    }


def _wait(pred, timeout: float = BUDGET_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.002)
    return pred()


# ---------------------------------------------------------------- the four §19 routing cases

def test_review_requested_reaches_the_owner_within_budget(world, capsys):
    w = world
    alice, bob = w.receiver("@alice"), w.receiver("@bob")
    g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
    w.pump()
    stop = threading.Event()
    w.dispatcher.start(stop)                      # live delivery: woken by publish
    try:
        sent = time.perf_counter()
        p = w.push("@bob", g, {"app.py": "a = 2\n"})
        assert _wait(lambda: alice.of("review.requested"))
    finally:
        stop.set()
        w.dispatcher.wake()
    rr = alice.of("review.requested")[0]
    assert rr.data["owner"] == "@alice" and rr.data["push"] == p.id and rr.data["why"] == "line"
    validate(rr)
    assert not bob.of("review.requested")                       # only the owner is asked
    hops = _hops(w, p.id, rr.id, alice, sent)
    with capsys.disabled():
        print("\nfake-network latency (ms): " + ", ".join(f"{k} {v * 1000:.2f}" for k, v in hops.items()))
    assert hops["total"] < BUDGET_S
    assert all(v >= 0 for v in hops.values())


def test_objection_reaches_the_author(world):
    w = world
    alice, bob = w.receiver("@alice"), w.receiver("@bob")
    g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
    p = w.push("@bob", g, {"app.py": "a = 2\n"})
    o = w.object("@alice", p, "breaks the import")
    w.pump()
    got = bob.of("objected")
    assert [e.data["objection"] for e in got] == [o.id]
    assert got[0].data["reason"] == "breaks the import" and got[0].data["by"] == "@alice"
    assert not alice.of("objected")                             # never the causing member


def test_claim_overlap_reaches_both_holders(world):
    w = world
    alice, bob, carol = w.receiver("@alice"), w.receiver("@bob"), w.receiver("@carol")
    w.genesis({"src/a.py": "a\n", "web/x.css": "x\n"}, author="@nelson")
    t1 = w.task("@nelson", "auth")
    t2 = w.task("@nelson", "login")
    w.pump()
    assert [e.data["task"] for e in alice.of("task.opened")] == [t1.id, t2.id]    # open work: every agent
    c1 = w.claim("@alice", t1, ["src/**"])
    c2 = w.claim("@bob", t2, ["src/a.py"])
    w.claim("@carol", t2, ["web/**"])                            # loses: t2 already claimed
    w.pump()
    for r in (alice, bob):
        ov = r.of("claim.overlap")
        assert len(ov) == 1 and sorted(ov[0].data["claims"]) == sorted([c1.id, c2.id])
    assert not carol.of("claim.overlap") and carol.of("claim.lost")


def test_tip_red_reaches_agents_and_testers(world):
    w = world
    rs = {h: w.receiver(h) for h in ("@alice", "@bob", "@carol", "@t1")}
    g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
    w.test_tip(g.id, g.tree, "fail")
    w.pump()
    for h, r in rs.items():
        red = r.of("tip.red")
        assert len(red) == 1 and red[0].data["red_link"] == g.id, h
        assert r.of("fix.opened") or h == "@t1"                     # fix tasks go to agents
    chain = w.chain()
    assert chain.red.red and fix_task_id(g.id) in chain.tasks


def test_window_close_needs_a_tick_and_links(world):
    w = world
    bob = w.receiver("@bob")
    g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
    p = w.push("@bob", g, {"app.py": "a = 2\n"})
    w.pump()
    assert not bob.of("linked")
    wake = w.intake.next_wakeup
    assert wake is not None and wake > w.net.now_ms
    w.advance(W + G + 10)                # the clock passes the close; the host appends a tick
    w.pump()
    assert w.intake.stats["ticks"] >= 1
    assert [e.data["push"] for e in bob.of("linked")] == [p.id]
    assert w.chain().tip.push == p.id


# ---------------------------------------------------------------- at least once

def test_duplicate_deliveries_give_one_entry_and_one_event_per_member(tmp_path):
    w = World(tmp_path, faults=Faults(duplicate=1.0, seed=3))
    try:
        alice, bob = w.receiver("@alice"), w.receiver("@bob")
        g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
        p = w.push("@bob", g, {"app.py": "a = 2\n"})
        w.object("@alice", p, "no")
        w.net.advance(10)
        w.intake.catch_up()               # the safety-net poll sees every copy again
        w.pump()
        assert w.intake.stats["duplicates"] >= 3
        ids = [gid for _, gid, _ in w.intake.log.mail_ids()]
        assert len(ids) == len(set(ids)) == 3
        for r in (alice, bob):
            assert len({e.id for e in r.events}) == len(r.events)
        for h in w.dispatcher.journal.handles():
            items = w.dispatcher.journal.since(h, 0)
            assert len({i.event.id for i in items}) == len(items)
    finally:
        w.close()


def test_reordered_mail_is_logged_in_arrival_order(tmp_path):
    w = World(tmp_path, faults=Faults(jitter_ms=500, seed=11))
    try:
        alice = w.receiver("@alice")
        w.genesis({"src/a.py": "a\n"}, author="@nelson")
        w.net.advance(1000)
        tasks = [w.task("@nelson", f"t{i}") for i in range(6)]
        w.net.advance(1000)
        w.pump()
        logged = [gid for _, gid, _ in w.intake.log.mail_ids()][1:]
        assert sorted(logged) == sorted(t.id for t in tasks)
        assert [e.data["task"] for e in alice.of("task.opened")] == logged       # log order, once each
    finally:
        w.close()


def test_receiver_down_retries_with_backoff_then_resumes(world):
    w = world
    alice = w.receiver("@alice")
    alice.mode = "down"
    g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
    w.push("@bob", g, {"app.py": "a = 2\n"})
    w.push("@carol", g, {"app.py": "a = 3\n"})
    head = w.dispatcher.journal.head("@alice")
    assert head >= 3
    w.pump()
    assert alice.posts == 1 and not alice.events
    w.pump()                                      # inside the 1 s backoff: no attempt
    assert alice.posts == 1
    w.net.advance(1_000)
    w.pump()
    assert alice.posts == 2                       # retried, still down: next wait 2 s
    w.net.advance(1_000)
    w.pump()
    assert alice.posts == 2
    w.net.advance(1_000)
    alice.mode = "flaky"                          # processes, but the 500 makes the host re-send
    w.pump()
    assert alice.posts == 3 and len(alice.events) == 1 and w.dispatcher.acked("@alice") == 0
    alice.mode = "ok"
    w.net.advance(4_000)
    w.pump()
    assert w.dispatcher.acked("@alice") == head
    ids = [e.id for e in alice.events]
    assert len(ids) == len(set(ids)) == head      # every event once, in journal order
    assert [e.id for e in alice.events] == [i.event.id for i in w.dispatcher.journal.since("@alice", 0)]
    assert alice.client.duplicates == 1 and alice.client.cursor == head


def test_webhook_falls_back_to_email_after_24h(tmp_path):
    w = World(tmp_path, dispatcher_kw={"retry_for_s": 30.0})
    try:
        alice = w.receiver("@alice")
        alice.mode = "down"
        g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
        w.push("@bob", g, {"app.py": "a = 2\n"})
        for _ in range(8):
            w.pump()
            w.net.advance(10_000)
        w.pump()
        inbox, _ = w.net.mailbox(w.kit.address("@alice")).list()
        notes = [mime.to_email(i.raw_mime) for i in inbox]
        assert notes and all(n.envelope.kind == "notify" for n in notes)
        assert w.dispatcher.acked("@alice") == w.dispatcher.journal.head("@alice")
    finally:
        w.close()


def test_host_restart_resumes_journal_without_duplicates(tmp_path):
    for persist in (True, False):
        w = World(tmp_path / str(persist), persist_replay=persist)
        try:
            alice = w.receiver("@alice")
            alice.mode = "down"
            g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
            w.push("@bob", g, {"app.py": "a = 2\n"})
            w.pump()
            head = w.dispatcher.journal.head("@alice")
            w.restart(save=persist)               # without a saved cache the log is replayed again
            assert w.dispatcher.journal.head("@alice") == head          # re-derived events dedupe
            assert w.chain().tip.state == g.id and len(w.intake.log) == 3
            alice.mode = "ok"
            w.net.advance(5_000)
            w.pump()
            w.push("@carol", g, {"app.py": "a = 3\n"})
            w.pump()
            ids = [e.id for e in alice.events]
            assert len(ids) == len(set(ids)) == w.dispatcher.journal.head("@alice") > head
        finally:
            w.close()


def test_missing_raw_mail_is_recovered_from_the_mailbox(tmp_path):
    w = World(tmp_path, persist_replay=False)
    try:
        g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
        p = w.push("@bob", g, {"app.py": "a = 2\n"})
        w.intake._mail_path(p.id).unlink()       # the host lost one stored message
        w.restart(save=False)
        assert w.intake.missing == ()
        assert w.intake.raw(p.id) is not None
        assert w.chain().push_status(p.id) is not None
    finally:
        w.close()


def test_unrecoverable_missing_mail_stalls_replay_before_it(tmp_path):
    w = World(tmp_path, persist_replay=False)
    try:
        g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
        p = w.push("@bob", g, {"app.py": "a = 2\n"})
        w.intake._mail_path(p.id).unlink()
        w.repo_box.net._boxes[REPO.lower()].clear()          # and the provider lost it too
        w.restart(save=False)
        assert w.intake.missing == (p.id,)
        assert w.chain().push_status(p.id) is None and w.chain().tip.state == g.id
    finally:
        w.close()


# ---------------------------------------------------------------- email and human notifications

def test_email_members_get_batched_host_signed_notify(world):
    w = world
    g = w.genesis({"app.py": "a = 1\n"}, author="@alice")      # @alice has no webhook: email
    w.push("@bob", g, {"app.py": "a = 2\n"})
    w.pump()
    box = w.net.mailbox(w.kit.address("@alice"))
    assert box.list()[0] == []                                  # still batching
    w.net.advance(5_000)
    w.pump()
    items, _ = box.list()
    assert len(items) == 1
    note = mime.to_email(items[0].raw_mime)
    env = note.envelope
    first = w.dispatcher.journal.since("@alice", 0)[0].event
    assert env.kind == "notify" and env.get("Gud-Event") == first.id and env.author == "@host"
    assert REPO not in note.cc                                  # channel 3 is never Cc'd to the repo
    from gud.core import envelope as E
    assert E.verify(env, w.kit.pub("@host"))


def test_human_notifications_go_to_the_human_cc_repo_once(world):
    w = world
    g = w.genesis({"src/a.py": "a\n"}, author="@nelson")
    t = w.task("@nelson", "feature")
    c = w.claim("@alice", t, ["src/**"])
    w.push("@alice", g, {"src/b.py": "b\n"}, task=t.id)
    w.advance(W + G + 5)                    # links; work done -> task.ready (accept) for the requester
    w.pump()
    ready = [e for h in ("@nelson",) for e in (i.event for i in w.dispatcher.journal.since(h, 0))
             if e.type == "task.ready"]
    assert ready and ready[0].data["needs"] == "accept"
    inbox, _ = w.net.mailbox(w.kit.address("@nelson")).list()
    notes = [mime.to_email(i.raw_mime) for i in inbox]
    human = [n for n in notes if n.envelope.get("Gud-Event") == ready[0].id and REPO in n.cc]
    assert len(human) == 1
    assert w.intake.log_has_event(ready[0].id)                  # its Cc copy is logged
    w.dispatcher.notify_humans(ready, w.intake.log_has_event)
    w.pump()
    inbox2, _ = w.net.mailbox(w.kit.address("@nelson")).list()
    again = [mime.to_email(i.raw_mime) for i in inbox2]
    assert len([n for n in again if n.envelope.get("Gud-Event") == ready[0].id and REPO in n.cc]) == 1
    # a reply to the notify resolves to the task (§8.2) and accepts it
    w.unsigned("@nelson", "accept", subject="Re: " + human[0].subject, in_reply_to=human[0].message_id)
    w.pump()
    assert w.chain().tasks[t.id].status == "done"
    assert c.id in w.chain().claims


def test_unsigned_mail_from_non_humans_is_not_logged(world):
    w = world
    w.genesis({"app.py": "a = 1\n"}, author="@alice")
    w.unsigned("@alice", "approve")                  # an agent's address: §8.1 (3) fails
    w.net.mailbox("stranger@else.org").send(
        mime.Outgoing(from_addr="stranger@else.org", to=(REPO,), subject="hi", text="hello"))
    assert w.intake.stats["ignored"] == 2 and len(w.intake.log) == 2


def test_rate_limit_applies_to_non_members_only(tmp_path):
    w = World(tmp_path, intake_kw={"rate_limit_per_sender": 2})
    try:
        w.genesis({"app.py": "a = 1\n"}, author="@alice")
        for i in range(4):
            w.task("@nelson", f"t{i}")                 # members are never limited
        assert w.intake.stats["rate_limited"] == 0
        from gud.core import envelope as E
        from gud.core.types import Record
        stranger = "x@else.org"
        for i in range(4):
            key = bytes([i + 1]) * 32
            env = E.sign(E.build("comment", author="@x", headers={"Gud-Target": "0" * 64},
                                 records=(Record("text", (), f"spam {i}".encode()),), sent=1, seen=w.seen()), key)
            w.net.mailbox(stranger).send(mime.Outgoing(from_addr=stranger, to=(REPO,), subject="s",
                                                       envelope=E.encode(env)))
        assert w.intake.stats["rate_limited"] == 2
    finally:
        w.close()


# ---------------------------------------------------------------- ticks and "tip at received time"

def test_unsigned_mail_is_judged_at_the_tip_at_its_received_time(world):
    # A removal of @dana links at close + G by a timer. Dana's mail arrives after that with no
    # tick in between: the host first ticks to just before it (replay note 1), so the removal
    # has linked and the mail fails §8.1 (3).
    w = world
    g = w.genesis({"app.py": "a = 1\n"}, author="@alice")
    members = [m for m in w.kit.members if m[0] != "@dana"]
    rm = w.push("@bob", g, {".gud/members": w.kit.members_file(members), "app.py": "a = 2\n"})
    w.approve("@nelson", rm)
    assert w.chain().tip.push == g.id                       # @alice's lazy window is open
    w.net.advance(W + G + 1_000)                            # no tick yet
    w.unsigned("@dana", "approve")
    assert w.chain().tip.push == rm.id
    assert w.intake.stats["ignored"] == 1 and w.intake.stats["logged"] == 3


def test_tick_cadence(world):
    w = world
    w.genesis({"app.py": "a = 1\n"}, author="@alice")
    assert w.intake.next_wakeup is None
    w.net.advance(59_000)
    assert w.intake.tick() is None                          # nothing open: at least every 60 s
    w.net.advance(1_010)
    assert w.intake.tick() is not None
    w.push("@bob", w.chain().tip.state, {"app.py": "a = 2\n"})
    w.net.advance(4_000)
    assert w.intake.tick() is None
    w.net.advance(1_010)
    assert w.intake.tick() is not None                      # something open: every 5 s of silence
    assert 0 < w.intake.next_tick_in() <= 5.0
