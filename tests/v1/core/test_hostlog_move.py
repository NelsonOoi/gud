"""Host migration to a new repo address (SPEC §11.6): a `handover` may name the repo's
new address; the continuing `open` must then name it."""

from __future__ import annotations

import pytest

from gud.core import envelope as E
from gud.core.hostlog import HostLog, LogError, decode_entry, encode_entry

OLD, NEW = "repo@old.example", "repo@new.example"
T = 1_760_000_000_000


def _log() -> tuple[HostLog, bytes]:
    key = b"\x01" * 32
    log = HostLog.open(OLD, key, T)
    log.append_tick(key, T + 1)
    return log, key


def test_handover_to_a_new_address():
    log, old = _log()
    new = b"\x02" * 32
    h = log.handover(old, E.public_of(new), T + 2, repo=NEW)
    assert h.fields == (("next", E.public_of(new)), ("repo", NEW))
    assert decode_entry(encode_entry(h)) == h
    assert log.next_repo() == NEW and log.repo == OLD
    o = log.continue_after_handover(new, T + 3)
    assert o.field("repo") == NEW and log.repo == NEW and log.repos == (OLD, NEW)
    log.append_tick(new, T + 4)
    again = HostLog(log.entries())                   # a member verifies the whole chain
    assert again.repo == NEW and again.repos == (OLD, NEW) and again.key == E.public_of(new)


def test_open_must_name_the_handover_address():
    log, old = _log()
    new = b"\x02" * 32
    log.handover(old, E.public_of(new), T + 2, repo=NEW)
    entries = log.entries()
    # a forged continuation keeping the old address (or naming a third one) does not verify
    for repo in (OLD, "repo@other.example"):
        forged = HostLog(entries)
        head = forged.head
        from gud.core.hostlog import entry_bytes
        from gud.core.types import LogEntry
        fields = (("repo", repo), ("key", E.public_of(new)), ("from", f"{head.seq}:{head.hash}"))
        signed = entry_bytes(head.seq + 1, head.hash, T + 3, "open", fields)
        bad = LogEntry(head.seq + 1, head.hash, T + 3, "open", fields, E.sign_bytes(new, signed), E.sha256_hex(signed))
        with pytest.raises(LogError, match="different repo than its handover"):
            HostLog(entries + [bad])


def test_without_a_handover_address_the_repo_stays():
    log, old = _log()
    new = b"\x02" * 32
    h = log.handover(old, E.public_of(new), T + 2)
    assert h.fields == (("next", E.public_of(new)),)
    assert log.continue_after_handover(new, T + 3).field("repo") == OLD
    # an unplanned (trusted-key) open cannot move the repo either
    log2, _ = _log()
    with pytest.raises(LogError):
        trusted = HostLog(log2.entries(), trusted_keys=(E.public_of(new),))
        from gud.core.hostlog import entry_bytes
        from gud.core.types import LogEntry
        head = trusted.head
        fields = (("repo", NEW), ("key", E.public_of(new)), ("from", f"{head.seq}:{head.hash}"))
        signed = entry_bytes(head.seq + 1, head.hash, T + 3, "open", fields)
        trusted.extend([LogEntry(head.seq + 1, head.hash, T + 3, "open", fields, E.sign_bytes(new, signed),
                                 E.sha256_hex(signed))])


def test_handover_repo_field_is_checked():
    log, old = _log()
    h = log.handover(old, E.public_of(b"\x02" * 32), T + 2, repo=NEW)
    text = encode_entry(h).replace(b"repo repo@new.example", b"repo not-an-address")
    with pytest.raises(LogError):
        decode_entry(text)
