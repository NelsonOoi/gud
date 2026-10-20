import hashlib
from dataclasses import replace

import pytest

from gud.core import envelope as E
from gud.core.hostlog import (
    ForkProof, HostLog, LogError, decode_entry, encode_entry, entry_bytes, fork_proof, replay_time,
)
from gud.core.types import ZERO, LogEntry, Seen

HOST = bytes(range(32))
NEW_HOST = bytes(range(1, 33))
OTHER = bytes(range(2, 34))
REPO = "repo@example.org"
SIG = E.sign_bytes(HOST, b"x").encode()


def _log(n_mail=3) -> HostLog:
    log = HostLog.open(REPO, HOST, 1_000)
    for i in range(n_mail):
        log.append_mail(f"{i:064x}", 1_000 + 10 * i, HOST, 10_000)
    return log


def test_entry_format_and_hash():
    log = HostLog.open(REPO, HOST, 1_000)
    entry = log.head
    signed = (b"gud-log 1\nseq 0\nprev " + ZERO.encode() + b"\ntime 1000\ntype open\nrepo " + REPO.encode() +
              b"\nkey " + E.public_of(HOST).encode() + b"\n")
    assert entry_bytes(0, ZERO, 1000, "open", entry.fields) == signed
    assert entry.hash == hashlib.sha256(signed).hexdigest()
    assert encode_entry(entry) == signed + b"sig " + entry.sig.encode() + b"\n"
    assert E.verify_bytes(E.public_of(HOST), signed, entry.sig)
    assert decode_entry(encode_entry(entry)) == entry
    assert log.key == E.public_of(HOST) and log.repo == REPO


def test_append_mail_times_and_lookup():
    log = HostLog.open(REPO, HOST, 1_000)
    a = log.append_mail("a" * 64, 500, HOST, 5_000)      # provider time behind: clamped to prev + 1
    b = log.append_mail("b" * 64, 3_000, HOST, 5_000)
    c = log.append_mail("c" * 64, 3_000, HOST, 5_000)    # tie: strictly increasing
    assert (a.time, b.time, c.time) == (1_001, 3_000, 3_001)
    assert log.seq_of("b" * 64) == 2 and log.seq_of("d" * 64) is None
    assert list(log.mail_ids()) == [(1, "a" * 64, 1_001), (2, "b" * 64, 3_000), (3, "c" * 64, 3_001)]
    assert list(log.mail_ids(3)) == [(3, "c" * 64, 3_001)]
    with pytest.raises(LogError):
        log.append_mail("a" * 64, 6_000, HOST, 9_000)       # already logged
    with pytest.raises(LogError):
        log.append_mail("e" * 64, 9_000, HOST, 8_000)       # ahead of the host clock
    with pytest.raises(LogError):
        log.append_mail("e" * 64, 9_000, OTHER, 9_000)      # wrong key
    tick = log.append_tick(HOST, 9_500)
    assert tick.type == "tick" and tick.fields == ()
    with pytest.raises(LogError):
        log.append_tick(HOST, 9_500)


def test_members_verify_and_extend():
    log = _log()
    entries = log.entries()
    mirror = HostLog()
    assert mirror.extend(entries[:2]) == 2
    assert mirror.extend(entries) == len(entries) - 2      # overlap is checked and skipped
    assert mirror.head == log.head and len(mirror) == len(log)
    mirror.verify()
    texts = [encode_entry(e) for e in entries]
    assert HostLog([decode_entry(t) for t in texts]).head.hash == log.head.hash
    assert mirror.key_at(0) == E.public_of(HOST) and mirror.at(2) == entries[2]
    with pytest.raises(LogError):
        mirror.at(99)


def _resign(entry: LogEntry, private: bytes, **changes) -> LogEntry:
    entry = replace(entry, **changes)
    signed = entry_bytes(entry.seq, entry.prev, entry.time, entry.type, entry.fields)
    return replace(entry, sig=E.sign_bytes(private, signed), hash=hashlib.sha256(signed).hexdigest())


@pytest.mark.parametrize("tamper", [
    lambda e: replace(e, time=e.time + 1),                                   # content changed, stale hash
    lambda e: replace(e, fields=(("id", "f" * 64),)),
    lambda e: _resign(e, OTHER, fields=(("id", "f" * 64),)),                 # re-signed by the wrong key
    lambda e: replace(_resign(e, HOST, fields=(("id", "f" * 64),)), sig=e.sig),   # hash fixed, sig stale
    lambda e: _resign(e, HOST, prev="1" * 64),                               # broken chain
    lambda e: _resign(e, HOST, time=1_000),                                  # time not increasing
    lambda e: _resign(e, HOST, seq=e.seq + 1),                               # seq gap
    lambda e: _resign(e, HOST, type="handover", fields=(("next", "ed25519:x"),)),
    lambda e: _resign(e, HOST, type="open", fields=(("repo", REPO), ("key", E.public_of(HOST)))),
])
def test_tamper_detection(tamper):
    entries = _log().entries()
    bad = list(entries)
    bad[2] = tamper(entries[2])
    log = HostLog()
    with pytest.raises(LogError):
        log.extend(bad)
    assert len(log) == 2                                   # stops at the last good entry


def test_extend_rejects_a_conflicting_known_entry():
    log = _log()
    entries = log.entries()
    forked = _resign(entries[1], HOST, fields=(("id", "9" * 64),))
    with pytest.raises(LogError):
        HostLog(entries).extend([forked])


@pytest.mark.parametrize("text", [
    b"gud-log 1\nseq 01\nprev " + ZERO.encode() + b"\ntime 1\ntype tick\nsig ed25519:x\n",
    b"gud-log 1\nseq 1\nprev " + ZERO.encode() + b"\ntime 1\ntype tick\nsig ed25519:x",
    b"gud-log 2\nseq 1\nprev " + ZERO.encode() + b"\ntime 1\ntype tick\nsig ed25519:x\n",
    b"gud-log 1\nprev " + ZERO.encode() + b"\nseq 1\ntime 1\ntype tick\nsig ed25519:x\n",
    b"gud-log 1\nseq 1\nprev " + b"A" * 64 + b"\ntime 1\ntype tick\nsig " + SIG + b"\n",
    b"gud-log 1\nseq 1\nprev " + ZERO.encode() + b"\ntime 1\ntype tick\nsig ed25519:x\n",
    b"gud-log 1\nseq 1\nprev " + ZERO.encode() + b"\ntime 1\ntype nap\nsig ed25519:x\n",
    b"gud-log 1\nseq 1\nprev " + ZERO.encode() + b"\ntime 1\ntype mail\nid xyz\nsig ed25519:x\n",
    b"gud-log 1\nseq 1\nprev " + ZERO.encode() + b"\ntime 1\ntype tick\nid " + b"a" * 64 + b"\nsig ed25519:x\n",
    b"gud-log 1\nseq 0\nprev " + ZERO.encode() + b"\ntime 1\ntype open\nrepo r\nkey k\nsig ed25519:x\n",
    b"gud-log 1\nseq 1\nprev " + ZERO.encode() + b"\ntime 1\ntype tick\nsig ed25519:x\n\n",
    b"\xff",
])
def test_decode_entry_rejects(text):
    with pytest.raises(LogError):
        decode_entry(text)


def test_entry_zero_must_be_open_and_from_only_on_continuation():
    log = _log(0)
    tick = _resign(log.head, HOST, type="tick", fields=())
    with pytest.raises(LogError):
        HostLog([tick])
    with pytest.raises(LogError):
        HostLog([_resign(log.head, HOST, fields=log.head.fields + (("from", "0:" + "a" * 64),))])


def test_fork_proof():
    log = _log(2)
    entries = log.entries()
    other_branch = HostLog(entries[:2])
    other_branch.append_mail("9" * 64, 2_000, HOST, 10_000)
    a, b = log.at(2), other_branch.at(2)
    key = log.key_at(2)
    proof = fork_proof(a, b, key)
    assert isinstance(proof, ForkProof)
    assert fork_proof(b, a, key) == proof
    data = proof.to_json()
    assert set(data) == {"a", "b"}
    assert {decode_entry(data["a"].encode()), decode_entry(data["b"].encode())} == {a, b}
    assert fork_proof(a, a, key) is None
    assert fork_proof(a, log.at(1), key) is None
    assert fork_proof(a, b, E.public_of(OTHER)) is None
    assert fork_proof(a, replace(b, time=b.time + 5), key) is None


def test_check_seen():
    log = _log()
    h1 = log.at(1).hash
    assert log.check_seen(Seen(1, h1), own_seq=3) == "ok"
    assert log.check_seen(Seen(1, "e" * 64), own_seq=3) == "mismatch"
    assert log.check_seen(Seen(3, h1), own_seq=3) == "future"
    assert log.check_seen(Seen(50, h1), own_seq=99) == "unknown"


def test_planned_handover():
    log = _log(1)
    new_pub = E.public_of(NEW_HOST)
    log.handover(HOST, new_pub, 5_000)
    with pytest.raises(LogError):
        log.append_mail("7" * 64, 5_100, NEW_HOST, 6_000)           # open must come first
    with pytest.raises(LogError):
        log.append_tick(HOST, 6_000)                                # old key is retired
    opened = log.continue_after_handover(NEW_HOST, 6_000)
    assert opened.field("from") == f"{opened.seq - 1}:{opened.prev}"
    log.append_mail("7" * 64, 6_100, NEW_HOST, 7_000)
    assert log.key == new_pub
    assert log.key_at(2) == E.public_of(HOST) and log.key_at(3) == new_pub
    member = HostLog(log.entries())
    assert member.head == log.head
    with pytest.raises(LogError):
        log.append_tick(HOST, 8_000)


def test_unplanned_handover_needs_trust():
    log = _log(1)
    rogue = HostLog(log.entries())
    rogue.__init__(log.entries(), trusted_keys=[E.public_of(NEW_HOST)])
    rogue.continue_after_handover(NEW_HOST, 5_000)
    rogue.append_mail("8" * 64, 5_100, NEW_HOST, 6_000)
    with pytest.raises(LogError):
        HostLog(rogue.entries())
    trusting = HostLog(rogue.entries(), trusted_keys=[E.public_of(NEW_HOST)])
    assert trusting.key == E.public_of(NEW_HOST)
    with pytest.raises(LogError):
        HostLog(log.entries()).continue_after_handover(NEW_HOST, 5_000)


def test_replay_time():
    log = _log()
    assert replay_time(log, 999_999, None) == log.head.time
    assert replay_time(log, 1_005, None) == 1_005
    assert replay_time(log, 999_999, 2) == log.at(2).time - 1
    assert replay_time(HostLog(), 42, None) == 42
