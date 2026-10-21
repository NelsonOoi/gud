"""gud.ops.audit: honest host, forked host, retimed / withheld / missing / altered mail."""

from __future__ import annotations

from dataclasses import replace

import pytest

from gud.core import envelope as E
from gud.core.hostlog import HostLog, decode_entry, entry_bytes
from gud.core.types import Record
from gud.ops.audit import audit

from .harness import HOST_KEY, View, World, address

GENESIS = {"app.py": b"a = 1\n"}
ALICE = address("@alice")


def comment(client, target, text="note", **kw):
    return client.sign_and_send("comment", {"Gud-Target": target}, (Record("text", (), text.encode()),),
                                summary="comment", **kw)


@pytest.fixture
def world() -> World:
    w = World(genesis=GENESIS)
    p = w["@alice"].push({"app.py": b"a = 2\n"})
    w.step()
    w.push = p
    return w


def inbox(world, handle="@alice"):
    return world[handle].mailbox.list(limit=10_000)[0]


def test_honest_host_passes(world):
    bob, nelson = world["@bob"], world["@nelson"]
    nelson.approve(world.push, cc=[ALICE])
    world.step()
    comment(bob, world.push, "looks fine", cc=[ALICE])
    world.step(100_000)
    copies = inbox(world)
    assert len(copies) == 2
    report = audit(world["@alice"], inbox=copies)
    assert report.ok and report.exit_status == 0, report
    assert report.log_ok and not report.late_votes
    E.jcs(report.to_json())                                            # JSON-able evidence file
    for handle in world.clients:
        assert audit(world[handle]).exit_status == 0


def test_forked_host_is_proven(world):
    alice, bob, host = world["@alice"], world["@bob"], world.host
    # the host forks its log: bob is served a different entry at the next seq
    other = HostLog(host.log.entries())
    other.append_tick(HOST_KEY, world.net.now_ms + 7)
    bob.mirror = View(other, host.store)
    world.step()
    assert alice.replayer().log().at(len(other) - 1).hash != other.head.hash
    # bob signs citing what he was shown; the host logs it into alice's history anyway
    forked = comment(bob, world.push, "seen from the fork")
    world.step()

    report = audit(alice)
    assert report.fork_proofs == ()
    assert [(m, s.render()) for m, s, _ in report.seen_mismatches] == \
        [(forked, f"{other.head.seq}:{other.head.hash}")]
    assert report.exit_status == 1

    # bob hands over the entries he retained: two host-signed entries for one seq
    evidence = audit(alice, retained=bob.replayer().log().entries())
    assert evidence.exit_status == 2 and len(evidence.fork_proofs) == 1
    proof = evidence.fork_proofs[0].to_json()
    a, b = decode_entry(proof["a"].encode()), decode_entry(proof["b"].encode())
    assert a.seq == b.seq and a.hash != b.hash
    for entry in (a, b):
        signed = entry_bytes(entry.seq, entry.prev, entry.time, entry.type, entry.fields)
        assert E.verify_bytes(E.public_of(HOST_KEY), signed, entry.sig)
    # bob's own audit, given alice's entries, proves the same fork
    assert audit(bob, retained=alice.replayer().log().entries()).fork_proofs[0].to_json() == proof


def test_retimed_message_and_late_vote(world):
    nelson = world["@nelson"]
    vote = nelson.approve(world.push, cc=[ALICE])
    world.step(retime_ms=600_000)                                       # host claims it arrived 10 min later
    report = audit(world["@alice"], inbox=inbox(world))
    assert [m for m, _, _ in report.retimed] == [vote]
    own, logged = report.retimed[0][1:]
    assert logged - own > 590_000                                       # copies arrive ms apart
    assert [m for m, _ in report.late_votes] == [vote]
    assert report.exit_status == 1


def test_withheld_message(world):
    held = comment(world["@bob"], world.push, "the host drops this", cc=[ALICE])
    world.step(skip=[held])
    world.step(200_000)                                                 # the log moved on well past skew
    report = audit(world["@alice"], inbox=inbox(world))
    assert report.withheld == (held,) and report.exit_status == 1


def test_missing_and_altered_mail(world):
    host = world.host
    ids = [gid for _, gid, _ in host.log.mail_ids()]
    store = dict(host.store)
    del store[ids[0]]
    store[ids[1]] = host.store[ids[0]]
    report = audit(View(host.log, store))
    assert report.missing_mail == (ids[0],)
    assert [m for m, _ in report.altered] == [ids[1]]
    assert report.exit_status == 1


def test_tampered_or_truncated_log_fails(world):
    host = world.host
    entries = host.log.entries()

    class Tampered(View):
        def log_entries(self, since, limit):
            out = list(entries)
            out[2] = replace(out[2], time=out[2].time + 1)
            return out[since:since + limit]

    report = audit(Tampered(host.log, host.store))
    assert not report.log_ok and report.exit_status == 2

    truncated = audit(View(HostLog(entries[:2]), host.store), retained=entries)
    assert not truncated.log_ok and "verified entry" in truncated.log_error
    assert truncated.exit_status == 2
