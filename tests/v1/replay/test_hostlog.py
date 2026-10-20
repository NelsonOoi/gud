"""Host log as replay input (§9, §11): received times only from the log, verification, Gud-Seen, forks,
send-time grace."""

from dataclasses import replace

import pytest

from gud.core import envelope as E
from gud.core.hostlog import HostLog, entry_bytes, fork_proof
from gud.core.replay import Replayer, ReplayError, replay
from gud.core.types import Seen
from tests.v1.kit import Kit, events_of, link_of, status

W, G = 60_000, 30_000


def base():
    k = Kit()
    g = k.genesis({"app.py": "a = 1\n"})
    return k, g


def _resign(entry, host_key, **changes):
    e = replace(entry, **changes)
    signed = entry_bytes(e.seq, e.prev, e.time, e.type, e.fields)
    return replace(e, sig=E.sign_bytes(host_key, signed), hash=E.sha256_hex(signed))


def test_rewritten_received_time_breaks_verification():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    q = k.push("@alice", g, {"n": "1\n"})
    entries = k.entries()
    seq = k.log.seq_of(p.id)
    forged = list(entries)
    forged[seq] = _resign(entries[seq], k.host_key, time=entries[seq].time + 1)   # host retimes p
    with pytest.raises(ReplayError) as exc:
        replay(k.emails, forged, k.now, store=k.store)
    assert exc.value.delta is not None
    r = Replayer(k.store)
    with pytest.raises(ReplayError):
        r.feed(k.emails, forged, k.now)
    assert r.log().head.seq == seq           # stopped at the last good entry
    assert status(r.chain(), q) == "unknown"


def test_received_time_comes_from_the_log_not_the_email():
    k, g = base()
    p = k.push("@alice", g, {"n": "1\n"}, log=False)
    k.emails[-1] = replace(p.email, received_at=1)          # a client-side time is ignored
    k.log_msg(p, at=k.now + 5_000)
    chain, _ = k.run()
    assert link_of(chain, p).received_at == p.logged_at == link_of(chain, p).linked_at


def test_vote_delayed_within_grace_counts_and_beyond_does_not():
    for delay, counts in ((G, True), (G + 1, False)):
        k, g = base()
        p = k.push("@bob", g, {"app.py": "a = 2\n"})
        close = p.logged_at + W
        o = k.object("@nelson", p, "no", sent=close, log=False)
        k.log_msg(o, at=close + delay)
        chain, _ = k.advance(10)
        assert (status(chain, p) != "linked") is counts, delay


def test_future_gud_sent_vote_does_not_count():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    k.object("@nelson", p, "no", sent=k.now + 10 * 60_000)
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "linked"


def test_gud_seen_mismatch_voids_message_and_raises_host_fork():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    bad_seen = Seen(1, "f" * 64)
    o = k.object("@nelson", p, "no", seen=bad_seen)
    chain, events = k.run()
    fork = events_of(events, "host.fork")
    assert fork and fork[0].data["message"] == o.id and fork[0].data["cited"] == bad_seen.render()
    assert fork[0].data["log_hash"] == k.log.at(1).hash
    chain, _ = k.advance(W + G + 5)
    assert status(chain, p) == "linked"                    # the objection was void


def test_gud_seen_mismatch_on_push_is_bad_sig():
    k, g = base()
    p = k.push("@alice", g, {"n": "1\n"}, seen=Seen(0, "e" * 64))
    chain, events = k.run()
    assert status(chain, p) == "bad_sig"
    assert events_of(events, "host.fork")


def test_gud_seen_citing_a_later_entry_is_void():
    k, g = base()
    p = k.push("@alice", g, {"n": "1\n"}, log=False)
    p.email = p.email  # build is fine; cite an entry at or after its own seq
    k2_seen = Seen(len(k.log) + 5, "a" * 64)
    q = k.push("@alice", g, {"m": "1\n"}, seen=k2_seen)
    chain, events = k.run()
    assert status(chain, q) == "bad_sig"
    assert events_of(events, "host.fork")[0].data["log_hash"] is None


def test_two_histories_caught_by_gud_seen_and_fork_proof():
    k, g = base()
    p = k.push("@alice", g, {"n": "1\n"})
    real = k.log.at(k.log.seq_of(p.id))
    # The host shows @bob a different entry at the same seq (another order).
    other = HostLog(k.entries()[:real.seq])
    alt = other.append_mail("ab" * 32, real.time, k.host_key, real.time + 10)
    q = k.push("@bob", g, {"m": "1\n"}, seen=Seen(alt.seq, alt.hash))
    chain, events = k.run()
    fork = events_of(events, "host.fork")
    assert fork and fork[0].data["message"] == q.id and fork[0].data["log_hash"] == real.hash
    proof = fork_proof(real, alt, k.pub("@host"))
    assert proof is not None and set(proof.to_json()) == {"a", "b"}


def test_tick_entries_advance_replay_time():
    k, g = base()
    p = k.push("@bob", g, {"app.py": "a = 2\n"})
    r = Replayer(k.store)
    r.feed(k.emails, k.entries(), k.now + 10 * W)
    assert status(r.chain(), p) == "window"
    k.tick(W + G + 5)
    entry = k.tick_entry()
    delta = r.feed([], [entry], k.now + 10 * W)
    assert status(r.chain(), p) == "linked"
    assert [l.push for l in delta.new_links] == [p.id]
    assert delta.new_links[0].linked_at == p.logged_at + W + G
