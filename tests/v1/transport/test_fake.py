"""FakeMailbox (on-disk, multi-process) and FakeNetwork (virtual time, faults)."""

from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from gud.transport import mime, registry
from gud.transport.base import Mailbox, Outgoing, TransportError
from gud.transport.fake import FAKE_AUTHSERV_ID, Faults, FakeMailbox, FakeNetwork


def _msg(i: int, frm: str = "alice@gud.test", to: tuple[str, ...] = ("repo@gud.test",)) -> Outgoing:
    return Outgoing(from_addr=frm, to=to, subject=f"m{i}", text=f"body {i}\n", envelope=None)


def _sender_proc(root: str, who: str, n: int) -> None:
    box = FakeMailbox(Path(root), f"{who}@gud.test")
    for i in range(n):
        box.send(_msg(i, frm=f"{who}@gud.test"))


def test_protocol_and_clock_injection(tmp_path: Path) -> None:
    now = [1_000.0]
    repo = FakeMailbox(tmp_path, "repo@gud.test", clock=lambda: now[0])
    alice = FakeMailbox(tmp_path, "alice@gud.test", clock=lambda: now[0])
    assert isinstance(repo, Mailbox)
    alice.send(_msg(0))
    alice.send(_msg(1))          # same clock reading: still strictly increasing
    now[0] = 999.0               # clock going backwards never reorders deliveries
    alice.send(_msg(2))
    items, cursor = repo.list()
    assert [i.received_at for i in items] == [1_000_000, 1_000_001, 1_000_002]
    assert cursor == "3" and repo.list(since=cursor) == ([], cursor)
    assert [i.cursor for i in repo.list(since="1")[0]] == ["2", "3"]
    first, cur = repo.list(limit=1)
    assert len(first) == 1 and cur == "1"
    with pytest.raises(TransportError):
        repo.list(since="INBOX:1:2")


def test_send_fans_out_and_authenticates_own_address(tmp_path: Path) -> None:
    alice = FakeMailbox(tmp_path, "alice@gud.test")
    sent = alice.send(_msg(0, to=("repo@gud.test", "bob@gud.test")))
    alice.send(_msg(1, frm="human@example.org"))
    repo_items, _ = FakeMailbox(tmp_path, "repo@gud.test").list()
    bob_items, _ = FakeMailbox(tmp_path, "bob@gud.test").list()
    assert len(repo_items) == 2 and len(bob_items) == 1
    own, forged = (mime.parse(i.raw_mime) for i in repo_items)
    assert own.auth_results == (f"{FAKE_AUTHSERV_ID}; dmarc=pass header.from=gud.test",)
    assert forged.auth_results == (f"{FAKE_AUTHSERV_ID}; dmarc=fail header.from=example.org",)
    assert own.message_id == sent.message_id


def test_raw_bytes_sent_unchanged_below_receiving_header(tmp_path: Path) -> None:
    alice = FakeMailbox(tmp_path, "alice@gud.test")
    raw = b"From: alice@gud.test\nTo: repo@gud.test\nSubject: bare LF\n\nline\r\nmixed\n"
    alice.send(raw)
    item = FakeMailbox(tmp_path, "repo@gud.test").list()[0][0]
    assert item.raw_mime.endswith(raw) and item.raw_mime.startswith(b"Authentication-Results: ")


def test_size_limit(tmp_path: Path) -> None:
    alice = FakeMailbox(tmp_path, "alice@gud.test", max_message_bytes=2 * 1024 * 1024)
    with pytest.raises(TransportError):
        alice.send(Outgoing(from_addr="alice@gud.test", to=("repo@gud.test",), envelope=b"\x00" * 2 * 1024 * 1024))


def test_multi_process_senders(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_sender_proc, args=(str(tmp_path), f"s{k}", 15)) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    items, _ = FakeMailbox(tmp_path, "repo@gud.test").list()
    assert len(items) == 60
    times = [i.received_at for i in items]
    assert times == sorted(times) and len(set(times)) == 60
    assert len({i.provider_id for i in items}) == 60
    assert [i.cursor for i in items] == [str(n) for n in range(1, 61)]


def test_wait_and_subscribe_cross_thread(tmp_path: Path) -> None:
    repo = FakeMailbox(tmp_path, "repo@gud.test")
    alice = FakeMailbox(tmp_path, "alice@gud.test")
    got = []
    sub = repo.subscribe(got.append)
    threading.Timer(0.3, lambda: alice.send(_msg(0))).start()
    t0 = time.monotonic()
    items = repo.wait(since=None, timeout=5)
    assert items and time.monotonic() - t0 < 2
    deadline = time.monotonic() + 2
    while not got and time.monotonic() < deadline:
        time.sleep(0.01)
    sub.close()
    assert len(got) == 1
    alice.send(_msg(1))
    time.sleep(0.2)
    assert len(got) == 1, "a closed subscription calls back no more"
    assert repo.wait(since=repo.head(), timeout=0.2) == []


def test_subscribe_with_since_catches_up(tmp_path: Path) -> None:
    repo = FakeMailbox(tmp_path, "repo@gud.test")
    alice = FakeMailbox(tmp_path, "alice@gud.test")
    for i in range(3):
        alice.send(_msg(i))
    got = []
    sub = repo.subscribe(got.append, since="1")
    deadline = time.monotonic() + 2
    while len(got) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    sub.close()
    assert [g.cursor for g in got] == ["2", "3"]


def test_registry_builds_fake(tmp_path: Path) -> None:
    box = registry.open_mailbox("fake", {"root": str(tmp_path), "address": "repo@gud.test"})
    assert isinstance(box, FakeMailbox) and box.capabilities().name == "fake"


# ---------------------------------------------------------------- FakeNetwork


def test_network_delay_and_virtual_wait() -> None:
    net = FakeNetwork(faults=Faults(delay_ms=500))
    repo, alice = net.mailbox("repo@gud.test"), net.mailbox("alice@gud.test")
    start = net.now_ms
    alice.send(_msg(0))
    assert repo.list() == ([], None)
    items = repo.wait(since=None, timeout=10)
    assert len(items) == 1 and items[0].received_at == start + 500 and net.now_ms == start + 500
    assert repo.wait(since=items[0].cursor, timeout=2) == [] and net.now_ms == start + 2500


def test_network_faults_are_deterministic() -> None:
    def run(seed: int) -> list[tuple[str, int]]:
        net = FakeNetwork(faults=Faults(jitter_ms=3000, duplicate=0.3, drop=0.2, seed=seed))
        repo, alice = net.mailbox("repo@gud.test"), net.mailbox("alice@gud.test")
        for i in range(40):
            alice.send(_msg(i))
            net.advance(100)
        net.advance(10_000)
        return [(mime.parse(i.raw_mime).subject, i.received_at) for i in repo.list()[0]]

    a, b = run(7), run(7)
    assert a == b and a != run(8)
    subjects = [s for s, _ in a]
    assert len(subjects) != 40                                      # drops and duplicates happened
    assert len(set(subjects)) < 40                                  # some dropped
    assert len(subjects) > len(set(subjects))                       # some duplicated
    assert subjects != sorted(subjects, key=lambda s: int(s[1:]))   # jitter reordered
    times = [t for _, t in a]
    assert times == sorted(times) and len(set(times)) == len(times)


def test_network_subscribe_is_synchronous_and_bytes_deterministic() -> None:
    def run() -> list[bytes]:
        net = FakeNetwork()
        repo, alice = net.mailbox("repo@gud.test"), net.mailbox("alice@gud.test")
        seen = []
        sub = repo.subscribe(lambda r: seen.append(r.raw_mime))
        alice.send(_msg(0))
        assert len(seen) == 1
        sub.close()
        alice.send(_msg(1))
        assert len(seen) == 1
        return seen

    assert run() == run()


def test_network_message_ids_are_unique_across_mailboxes() -> None:
    # Regression: each NetworkMailbox numbered its own Message-IDs, so two senders on one domain
    # (or a second mailbox object for one address) sending in the same millisecond produced the
    # same id, and §4 ("two unsigned messages with one Message-ID count once") dropped one.
    net = FakeNetwork()

    def send(addr: str) -> str:
        out = Outgoing(from_addr=addr, to=("r@x.org",), subject="s", text="t")
        return net.mailbox(addr).send(out).message_id

    assert len({send("a@x.org"), send("b@x.org"), send("a@x.org")}) == 3
