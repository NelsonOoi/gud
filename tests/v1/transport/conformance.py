"""Transport conformance checks (SPEC §20.2), written against the `Mailbox` contract only.

Each `check_*` function takes a `Harness` describing one transport and returns a dict of
measurements (latencies, sizes, what the provider changed). `test_conformance.py` runs every
check against every harness it can build: fake and fake_network always, imap_smtp against a
local GreenMail, primitive against a mock HTTP server, and primitive against the live account
when GUD_LIVE_PRIMITIVE=1.

Messages are matched by a per-check nonce in the subject, so checks share mailboxes safely.
"""

from __future__ import annotations

import hashlib
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from gud.core import envelope as E
from gud.core.types import ZERO, Envelope, Record, Seen
from gud.transport import auth, mime
from gud.transport.base import Mailbox, Outgoing, Received, TransportError, envelope_budget

PUSH_KINDS = {"webhook", "idle", "eventsource", "pubsub", "none"}


@dataclass
class Harness:
    name: str
    repo: Mailbox                                   # reads the repo address
    sender: Mailbox                                 # a member's own account; sends From sender.address
    human_address: str                              # a human member's address the sender does not own
    authserv_id: str | None                         # the authserv-id the repo would pin
    reopen: Callable[[], Mailbox]                   # a fresh repo mailbox (after a forced disconnect)
    wake_bound_s: float                             # §20.2(4): 5 s for API providers, 30 s for IDLE
    delivery_timeout_s: float = 30.0
    deliver_external: Callable[[bytes, str], None] | None = None   # (raw, dmarc verdict) as the receiving MTA
    disconnect: Callable[[], int] | None = None     # break live subscription connections
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    blob_bytes: int = 25 * 1024 * 1024
    expected_caps: dict = field(default_factory=dict)
    adds_auth_results: bool = True                  # the receiving side writes A-R (GreenMail does not)
    virtual: bool = False                           # time is a virtual clock (FakeNetwork): no threads
    wall: Callable[[], float] = time.time           # the clock received_at is compared with
    sent_count: int = 0
    max_sends: int | None = None
    results: dict = field(default_factory=dict)

    def send(self, message: Outgoing | bytes, via: Mailbox | None = None):
        if self.max_sends is not None and self.sent_count >= self.max_sends:
            raise AssertionError(f"{self.name}: send budget of {self.max_sends} emails exhausted")
        self.sent_count += 1
        return (via or self.sender).send(message)


# ---------------------------------------------------------------- helpers


def nonce() -> str:
    return uuid.uuid4().hex[:12]


def head(mb: Mailbox) -> str | None:
    fn = getattr(mb, "head", None)
    if fn is not None:
        return fn()
    cursor = None
    while True:
        items, cursor = mb.list(since=cursor)
        if not items:
            return cursor


def collect(h: Harness, cursor: str | None, want: int, tag: str, *, mb: Mailbox | None = None,
            timeout: float | None = None) -> tuple[list[Received], str | None]:
    """Messages after `cursor` whose subject contains `tag`, until `want` arrived (or timeout)."""
    mb = mb or h.repo
    deadline = h.monotonic() + (timeout if timeout is not None else h.delivery_timeout_s)
    found: list[Received] = []
    while True:
        items, cursor = mb.list(since=cursor)
        for item in items:
            try:
                if tag in mime.parse(item.raw_mime).subject:
                    found.append(item)
            except mime.MimeError:
                pass
        remaining = deadline - h.monotonic()
        if len(found) >= want or remaining <= 0:
            return found, cursor
        if not items:
            mb.wait(since=cursor, timeout=min(remaining, 5.0))


def outgoing(h: Harness, env: Envelope | None, tag: str, *, text: str | None = None, **kw) -> Outgoing:
    gid = E.gud_id(env) if env is not None else None
    if env is not None:
        subject = mime.subject_for(env, gid, f"conformance {tag}")
        body = mime.render_human_part(env, gid) if text is None else text
    else:
        subject, body = f"conformance {tag}", text or "hello"
    return Outgoing(from_addr=h.sender.address, to=(h.repo.address,), subject=subject, text=body,
                    envelope=E.encode(env) if env is not None else None, **kw)


def blob_env(data: bytes, index: int = 0, count: int = 1, chunk_size: int | None = None,
             total: bytes | None = None) -> Envelope:
    total = data if total is None else total
    return E.build("blob", author=None, headers={
        "Gud-Blob": E.sha256_hex(total), "Gud-Size": str(len(total)),
        "Gud-Chunk": f"{index}/{count}", "Gud-Chunk-Size": str(chunk_size or len(data) or 1),
    }, records=[Record("data", (), data)])


def signed_comment(text: str) -> Envelope:
    private, _public = E.generate_key()
    env = E.build("comment", author="@conformance", headers={"Gud-Target": "ab" * 32},
                  records=[Record("text", (), text.encode("utf-8"))], sent=int(time.time()),
                  seen=Seen(0, ZERO))
    return E.sign(env, private)


def hostile_bytes(seed: int, size: int = 120_000) -> bytes:
    """NUL, CR, lone LF, CRLF, '.' lines, 'From ' lines, 998+ byte lines, non-UTF-8."""
    rng = random.Random(seed)
    parts = [
        b"\x00\x00NUL\x00", b"\r", b"lone-cr\rmid", b"\n", b"lone-lf\n", b"crlf\r\n",
        b".\r\n", b"\r\n.\r\n", b"..dots\n", b"From the start\n", b"x" * 5000, b"\n",
        b"y" * 998 + b"\r\n", b"\xff\xfe\xc3\x28\xa0\xa1", "ünïcödé ☃ 漢字".encode("utf-8"),
        " sep".encode("utf-8"), b"\r\n" * 3, b"=3D=\r\n=", b"--boundary-ish--\r\n",
    ]
    out = b"".join(parts)
    out += bytes(rng.getrandbits(8) for _ in range(size - len(out))) if size > len(out) else b""
    return out


# ---------------------------------------------------------------- checks


def check_capabilities(h: Harness) -> dict:
    caps = h.repo.capabilities()
    send_caps = h.sender.capabilities()
    assert caps.raw_mime is True, "a repo transport must return raw MIME"
    assert caps.push in PUSH_KINDS
    assert caps.auth_results in {"header", "synthesized", "none"}
    assert caps.max_message_bytes >= 1024 * 1024
    assert caps.received_time_resolution_ms >= 1
    if caps.auth_results == "synthesized":
        assert caps.authserv_id, "a synthesizing adapter documents its fixed authserv-id"
    for key, value in h.expected_caps.items():
        assert getattr(caps, key) == value, f"{key}: {getattr(caps, key)!r} != documented {value!r}"
    budget = envelope_budget(send_caps.max_message_bytes, caps.max_message_bytes)
    return {"capabilities": caps.__dict__, "envelope_budget": budget}


def check_roundtrip(h: Harness) -> dict:
    """§20.2(1): envelopes of hostile bytes and non-ASCII text arrive with identical gud ids."""
    tag = nonce()
    envs = [
        blob_env(hostile_bytes(1)),
        signed_comment("ünïcödé ☃ 漢字 — text with\r\nCRLF and\rCR and a trailing space \n" + "z" * 3000),
        blob_env(b"\r\n.\r\n" * 2000 + b"\n.\n" * 2000 + b"\x00" * 64),
    ]
    cursor = head(h.repo)
    sent = []
    for env in envs:
        h.send(outgoing(h, env, tag))
        sent.append(E.encode(env))
    got, _ = collect(h, cursor, len(envs), tag)
    assert len(got) >= len(envs), f"only {len(got)}/{len(envs)} round-trip messages arrived"
    want = {E.gud_id(env): data for env, data in zip(envs, sent)}
    seen = {}
    for item in got:
        email = mime.to_email(item.raw_mime, received_at=item.received_at, provider_id=item.provider_id)
        assert email is not None and email.envelope is not None, "envelope message did not decode"
        parsed = mime.parse(item.raw_mime)
        assert parsed.envelope_bytes == want[email.gud_id], "envelope bytes changed in transit"
        assert E.subject_tag(email.gud_id) in parsed.subject, "subject tag lost"
        assert email.gud_id in parsed.text or parsed.text == "", "human part lost the Gud-Id line"
        seen[email.gud_id] = item
    assert set(seen) == set(want), "gud ids differ after the round trip"
    return {"messages": len(envs), "bytes": sum(len(s) for s in sent)}


def check_dedupe(h: Harness) -> dict:
    """§20.2(2): the same message delivered twice yields one gud id; an intake logs it once."""
    tag = nonce()
    env = signed_comment(f"dedupe {tag}")
    msg = outgoing(h, env, tag)
    cursor = head(h.repo)
    h.send(msg)
    h.send(msg)
    got, _ = collect(h, cursor, 2, tag)
    assert got, "no copy arrived"
    ids = [mime.to_email(item.raw_mime).gud_id for item in got]
    assert set(ids) == {E.gud_id(env)}
    logged = list(dict.fromkeys(ids))  # what an intake keyed by gud id appends
    assert len(logged) == 1
    return {"copies_delivered": len(got), "logged": len(logged),
            "provider_ids_distinct": len({i.provider_id for i in got}) == len(got)}


def check_ordering(h: Harness, gap_s: float = 1.1) -> dict:
    """§20.2(3): delivery times are returned and non-decreasing for mail sent ≥ 1 s apart."""
    tag = nonce()
    cursor = head(h.repo)
    order = []
    for i in range(3):
        env = signed_comment(f"ordering {tag} #{i}")
        order.append(E.gud_id(env))
        h.send(outgoing(h, env, tag))
        if i < 2:
            h.sleep(gap_s)
    got, _ = collect(h, cursor, 3, tag)
    assert len(got) >= 3
    times = [item.received_at for item in got]
    ids = [mime.to_email(item.raw_mime).gud_id for item in got]
    assert all(isinstance(t, int) and t > 1_500_000_000_000 for t in times), "received_at must be ms"
    assert times == sorted(times), f"received_at not non-decreasing: {times}"
    assert [i for i in dict.fromkeys(ids)] == order, "list order differs from send order"
    return {"received_at": times, "gaps_ms": [b - a for a, b in zip(times, times[1:])]}


def check_wait_latency(h: Harness) -> dict:
    """§20.2(4): `wait` returns within the bound after delivery."""
    tag = nonce()
    cursor = head(h.repo)
    env = signed_comment(f"wait {tag}")
    sent_at: list[float] = []

    def send_one() -> None:
        h.send(outgoing(h, env, tag))
        sent_at.append(h.monotonic())

    thread = None
    if h.virtual:
        send_one()  # delivery is scheduled on the virtual clock; wait() advances it
    else:
        def later() -> None:
            h.sleep(1.0)
            send_one()
        thread = threading.Thread(target=later, daemon=True)
        thread.start()
    started = h.monotonic()
    got: list[Received] = []
    deadline = started + h.wake_bound_s + h.delivery_timeout_s
    while not got and h.monotonic() < deadline:
        items = h.repo.wait(since=cursor, timeout=max(0.1, deadline - h.monotonic()))
        for item in items:
            cursor = item.cursor
            if tag in mime.parse(item.raw_mime).subject:
                got.append(item)
    returned, returned_wall = h.monotonic(), h.wall()
    if thread is not None:
        thread.join(timeout=60)
    assert got, "wait never returned the message"
    from_send = returned - sent_at[0]
    from_delivery = returned_wall - got[0].received_at / 1000.0
    assert from_delivery <= h.wake_bound_s, f"wait returned {from_delivery:.2f}s after delivery (bound {h.wake_bound_s}s)"
    return {"wait_latency_from_delivery_s": round(from_delivery, 3), "wait_latency_from_send_s": round(from_send, 3)}


def check_subscribe_latency(h: Harness) -> dict:
    """§20.2(4): the push intake fires within the bound after delivery."""
    tag = nonce()
    env = signed_comment(f"subscribe {tag}")
    hits: list[tuple[float, Received, float]] = []
    event = threading.Event()

    def on_mail(item: Received) -> None:
        if tag in mime.parse(item.raw_mime).subject:
            hits.append((h.monotonic(), item, h.wall()))
            event.set()

    sub = h.repo.subscribe(on_mail)
    try:
        h.sleep(1.0)  # let the subscription connect
        h.send(outgoing(h, env, tag))
        sent = h.monotonic()
        _wait_event(h, event, h.wake_bound_s + h.delivery_timeout_s)
    finally:
        sub.close()
    assert hits, "subscribe never fired"
    from_send = hits[0][0] - sent
    from_delivery = hits[0][2] - hits[0][1].received_at / 1000.0
    assert from_delivery <= h.wake_bound_s, f"subscribe fired {from_delivery:.2f}s after delivery (bound {h.wake_bound_s}s)"
    assert mime.to_email(hits[0][1].raw_mime).gud_id == E.gud_id(env)
    return {"subscribe_latency_from_delivery_s": round(from_delivery, 3),
            "subscribe_latency_from_send_s": round(from_send, 3), "callbacks": len(hits)}


def check_subscribe_reconnect(h: Harness) -> dict:
    """Push intake survives a dropped connection and catches up on reconnect."""
    if h.disconnect is None:
        return {"skipped": "no disconnect hook"}
    tag = nonce()
    hits: list[Received] = []
    event = threading.Event()

    def on_mail(item: Received) -> None:
        if tag in mime.parse(item.raw_mime).subject:
            hits.append(item)
            event.set()

    sub = h.repo.subscribe(on_mail)
    try:
        h.sleep(1.0)
        dropped = h.disconnect()
        h.send(outgoing(h, signed_comment(f"reconnect {tag}"), tag))
        sent = h.monotonic()
        _wait_event(h, event, 2 * h.wake_bound_s + h.delivery_timeout_s)
    finally:
        sub.close()
    assert hits, "subscription did not recover after the disconnect"
    return {"dropped_connections": dropped, "recovery_latency_s": round(h.monotonic() - sent, 3)}


def _wait_event(h: Harness, event: threading.Event, timeout: float) -> None:
    deadline = h.monotonic() + timeout
    while not event.is_set() and h.monotonic() < deadline:
        if h.virtual:
            h.sleep(0.05)
        else:
            event.wait(0.05)


def check_large_blob(h: Harness) -> dict:
    """§20.2(5): a 25 MB file round-trips through the blob path within max_message_bytes."""
    tag = nonce()
    m_send, m_repo = h.sender.capabilities().max_message_bytes, h.repo.capabilities().max_message_bytes
    m = min(m_send, m_repo)
    budget = envelope_budget(m_send, m_repo)
    content = os.urandom(h.blob_bytes)
    probe = E.encode(blob_env(b"", 0, 999_999, 99_999_999, content))
    chunk = budget - len(probe) - 32
    assert chunk > 0
    count = -(-len(content) // chunk)
    envs = [blob_env(content[i * chunk:(i + 1) * chunk], i, count, chunk, content) for i in range(count)]
    sizes = []
    cursor = head(h.repo)
    started = h.monotonic()
    for env in envs:
        data = E.encode(env)
        assert len(data) <= budget, f"envelope {len(data)} > budget {budget}"
        assert E.validate_kind(env) is None, E.validate_kind(env)
        msg = outgoing(h, env, tag, text=f"blob chunk {tag}")
        built = mime.build(msg)
        assert len(built) <= m, f"MIME {len(built)} > max_message_bytes {m}"
        sizes.append(len(built))
        h.send(msg)
    got, _ = collect(h, cursor, count, tag, timeout=max(h.delivery_timeout_s, 180.0))
    chunks: dict[int, bytes] = {}
    for item in got:
        email = mime.to_email(item.raw_mime)
        assert email is not None and email.envelope is not None
        idx = int(email.envelope.get("Gud-Chunk").split("/")[0])
        chunks[idx] = email.envelope.records_named("data")[0].data
    assert sorted(chunks) == list(range(count)), f"chunks missing: have {sorted(chunks)} of {count}"
    joined = b"".join(chunks[i] for i in range(count))
    assert hashlib.sha256(joined).hexdigest() == hashlib.sha256(content).hexdigest()
    return {"file_bytes": len(content), "chunks": count, "envelope_budget": budget,
            "largest_mime_bytes": max(sizes), "max_message_bytes": m,
            "largest_received_bytes": max(len(i.raw_mime) for i in got),
            "seconds": round(h.monotonic() - started, 1)}


def check_spoofed_human(h: Harness) -> dict:
    """§20.2(6): an unsigned reply with a forged From of a human member fails §8.1(2)."""
    tag = nonce()
    out: dict = {}
    cursor = head(h.repo)
    human_domain = h.human_address.rsplit("@", 1)[1]
    forged_inner = auth.synthesize(h.authserv_id or "mx.invalid", "pass", human_domain)

    def subject(label: str) -> str:
        return f"Re: conformance {tag} {label}"

    def unsigned(label: str, extra: bytes = b"") -> bytes:
        msg = Outgoing(from_addr=h.human_address, to=(h.repo.address,), subject=subject(label), text="approve\n")
        return extra + mime.build(msg)

    want = 0
    # 1. Through the submission path: the sender's account claims the human's From.
    try:
        h.send(Outgoing(from_addr=h.human_address, to=(h.repo.address,), subject=subject("submit"), text="approve\n"))
        out["submission"] = "accepted"
        want += 1
    except TransportError as exc:
        out["submission"] = f"refused: {str(exc)[:100]}"
    # 2. Display-name spoof through the sender's own account.
    h.send(Outgoing(from_addr=f'"{h.human_address}" <{h.sender.address}>', to=(h.repo.address,),
                    subject=subject("display"), text="approve\n"))
    want += 1
    # 3. Forged mail from outside carrying its own forged A-R (the receiving MTA says fail), and
    #    genuine human mail (the MTA says pass) as the positive control.
    if h.deliver_external is not None:
        h.deliver_external(unsigned("forged", b"Authentication-Results: " + forged_inner.encode() + b"\r\n"), "fail")
        h.deliver_external(unsigned("genuine"), "pass")
        want += 2
    got, _ = collect(h, cursor, want, tag)
    assert len(got) >= want, f"{len(got)}/{want} spoof-test messages arrived"
    verified_as_human: set[str] = set()
    labels: dict = {}
    for item in got:
        parsed = mime.parse(item.raw_mime)
        email = mime.to_email(item.raw_mime)
        assert email is not None and email.envelope is None and email.text.startswith("approve")
        label = parsed.subject.rsplit(" ", 1)[-1]
        is_human = parsed.from_addr.lower() == h.human_address.lower()
        labels[label] = {"from_is_human": is_human, "auth_results": list(parsed.auth_results)}
        if label == "display":
            assert not is_human, "display name leaked into the From address"
        if is_human and auth.sender_verified(parsed.auth_results, parsed.from_addr, h.authserv_id):
            verified_as_human.add(label)
    expected = {"genuine"} if h.deliver_external is not None else set()
    assert verified_as_human == expected, f"verified as the human: {verified_as_human}, want {expected}"
    out["verified_as_human"] = sorted(verified_as_human)
    out["messages"] = labels
    return out


def check_raw_retained(h: Harness) -> dict:
    """Raw MIME is returned as received: stable bytes (stable unsigned id), receiving headers kept."""
    tag = nonce()
    cursor = head(h.repo)
    msg = Outgoing(from_addr=h.sender.address, to=(h.repo.address,), subject=f"conformance {tag} raw",
                   text="Approve.\n\nA human-looking reply with ünïcode and a long line " + "w" * 1200 + "\n",
                   message_id=f"<conf-{tag}@{h.sender.address.rsplit('@', 1)[1]}>")
    sent = h.send(msg)
    got, after = collect(h, cursor, 1, tag)
    assert got
    item = got[0]
    again, _ = h.repo.list(since=cursor)
    match = [i for i in again if i.provider_id == item.provider_id]
    assert match and match[0].raw_mime == item.raw_mime, "raw bytes differ between two reads"
    email = mime.to_email(item.raw_mime)
    assert email is not None and email.envelope is None
    assert email.gud_id == hashlib.sha256(item.raw_mime).hexdigest()
    assert email.text.startswith("Approve."), "text part not recovered"
    assert "w" * 1200 in email.text.replace("\n", ""), "long line damaged"
    parsed = mime.parse(item.raw_mime)
    if h.adds_auth_results:
        assert parsed.auth_results, "the receiving side's Authentication-Results is missing"
    return {"message_id_sent": msg.message_id, "message_id_received": parsed.message_id,
            "message_id_preserved": parsed.message_id == msg.message_id,
            "sent_reported_message_id": sent.message_id, "auth_results": list(parsed.auth_results),
            "raw_bytes": len(item.raw_mime)}


def check_resume(h: Harness) -> dict:
    """§20.2(7): after a forced disconnect, list(cursor) returns every message missed, once."""
    tag = nonce()
    old = h.reopen()
    cursor = head(old)
    old.close()  # the reader goes away while mail keeps arriving
    ids = []
    for i in range(2):
        env = signed_comment(f"resume {tag} {i}")
        ids.append(E.gud_id(env))
        h.send(outgoing(h, env, tag))
    fresh = h.reopen()
    try:
        got, end = collect(h, cursor, 2, tag, mb=fresh)
        got_ids = [mime.to_email(i.raw_mime).gud_id for i in got]
        assert got_ids == ids, f"resume returned {got_ids}, want {ids} once each"
        again, _ = fresh.list(since=end)
        assert not [i for i in again if tag in mime.parse(i.raw_mime).subject], "resume repeated messages"
    finally:
        fresh.close()
    return {"missed": len(ids), "returned": len(got)}


ALL_CHECKS = [
    check_capabilities, check_roundtrip, check_dedupe, check_ordering, check_wait_latency,
    check_subscribe_latency, check_subscribe_reconnect, check_large_blob, check_spoofed_human,
    check_raw_retained, check_resume,
]
