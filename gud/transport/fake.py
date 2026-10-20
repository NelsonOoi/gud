"""Fake transports for tests and local runs.

`FakeMailbox` is an on-disk mail system shared by every address under one root directory. It is
safe across processes (an `fcntl` lock guards the index and the clock state), and its clock is
injectable. Layout:

    <root>/lock                     flock target
    <root>/state.json               {"last_ms": <last received_at>, "next": <next provider id>}
    <root>/boxes/<address>/index.jsonl   one line per delivery: {"n", "t", "id", "f"}
    <root>/boxes/<address>/<n>.eml       raw MIME as stored (with Authentication-Results)

It plays the receiving server: every delivery gets a strictly increasing `received_at` (one
clock for the whole root), and mail submitted through `send` gets an `Authentication-Results`
header from authserv-id `fake.gud.invalid` that reports `dmarc=pass` only when `From` is the
sending mailbox's own address (an account can't authenticate mail as someone else). `deliver`
injects raw bytes as an external MTA would hand them over, adding nothing.

`FakeNetwork` is an in-memory equivalent with a virtual clock and deterministic fault injection
(delay, jitter/reorder, duplicate, drop) for protocol tests. Nothing in it reads the wall clock.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from dataclasses import replace
from email.utils import formatdate, parseaddr
from pathlib import Path
from typing import Callable, Iterator, Mapping
from urllib.parse import quote

from gud.core.types import Ms

from . import auth, mime
from .base import (
    Capabilities, Outgoing, Received, Sent, Subscription, ThreadSubscription, TransportError,
    cfg_int, safe_call,
)

FAKE_AUTHSERV_ID = "fake.gud.invalid"
DEFAULT_MAX_MESSAGE_BYTES = 10 * 1024 * 1024

_conditions: dict[str, threading.Condition] = {}
_conditions_lock = threading.Lock()


def _condition(root: Path) -> threading.Condition:
    key = str(root)
    with _conditions_lock:
        cond = _conditions.get(key)
        if cond is None:
            cond = _conditions[key] = threading.Condition()
        return cond


def _box_name(address: str) -> str:
    return quote(address.strip().lower(), safe="@.+-_")


def _from_and_auth(raw: bytes, own_address: str, authserv_id: str) -> bytes:
    """Receiving-server behaviour for authenticated submission: prepend A-R for the From domain."""
    try:
        parsed = mime.parse(raw)
    except mime.MimeError:
        return raw
    sender = parsed.from_addr
    if "@" not in sender:
        return raw
    dmarc = "pass" if sender.lower() == own_address.lower() else "fail"
    return auth.prepend(raw, auth.synthesize(authserv_id, dmarc, sender.rsplit("@", 1)[1]))


def _recipients(message: Outgoing | bytes) -> tuple[str, ...]:
    if isinstance(message, Outgoing):
        addrs = (*message.to, *message.cc)
    else:
        addrs = mime.recipients(message)
    seen: dict[str, str] = {}
    for addr in addrs:
        bare = (parseaddr(addr)[1] or addr).strip()
        if bare and bare.lower() not in seen:
            seen[bare.lower()] = bare
    return tuple(seen.values())


class FakeMailbox:
    """Local multi-process mailbox. See the module docstring for the layout."""

    def __init__(self, root: Path, address: str, *, clock: Callable[[], float] = time.time,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                 authserv_id: str = FAKE_AUTHSERV_ID, poll_interval: float = 0.05) -> None:
        self.root = Path(root).resolve()
        self.address = address
        self.clock = clock
        self.max_message_bytes = max_message_bytes
        self.authserv_id = authserv_id
        self.poll_interval = poll_interval
        (self.root / "boxes").mkdir(parents=True, exist_ok=True)
        (self.root / "lock").touch(exist_ok=True)
        self._cond = _condition(self.root)
        self._closed = False

    # ------------------------------------------------------------ contract

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="fake", max_message_bytes=self.max_message_bytes, push="none",
            preserves_message_id=True, auth_results="header", authserv_id=self.authserv_id,
            send_quota_per_day=None, received_time_resolution_ms=1, raw_mime=True,
        )

    def send(self, message: Outgoing | bytes) -> Sent:
        if isinstance(message, Outgoing):
            raw = mime.build(message, date=formatdate(self.clock(), usegmt=True))
        elif isinstance(message, (bytes, bytearray)):
            raw = bytes(message)
        else:
            raise TypeError("send() takes an Outgoing or raw bytes")
        if len(raw) > self.max_message_bytes:
            raise TransportError(f"message is {len(raw)} bytes; the limit is {self.max_message_bytes}")
        rcpts = _recipients(message)
        if not rcpts:
            raise TransportError("message has no recipients")
        stored = _from_and_auth(raw, self.address, self.authserv_id)
        delivered = self.deliver(stored, rcpts)
        try:
            message_id = mime.parse(raw).message_id
        except mime.MimeError:
            message_id = ""
        first = delivered[0]
        return Sent(provider_id=first[0], message_id=message_id, accepted_at=first[1])

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        after = self._parse_cursor(since)
        box = self._box(self.address)
        out: list[Received] = []
        with self._locked(shared=True):
            rows = self._index_rows(box)
        for row in rows:
            if row["n"] <= after:
                continue
            raw = (box / row["f"]).read_bytes()
            out.append(Received(raw_mime=raw, received_at=row["t"], provider_id=row["id"], cursor=str(row["n"])))
            if len(out) >= limit:
                break
        return out, (out[-1].cursor if out else since)

    def wait(self, *, since: str | None, timeout: float) -> list[Received]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            items, _ = self.list(since=since)
            remaining = deadline - time.monotonic()
            if items or remaining <= 0:
                return items
            with self._cond:
                self._cond.wait(min(self.poll_interval, remaining))

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription:
        start = since if since is not None else self.head()

        def loop(stop: threading.Event) -> None:
            cursor = start
            while not stop.is_set():
                items, cursor = self.list(since=cursor)
                for item in items:
                    if stop.is_set():
                        return
                    safe_call(callback, item)
                if not items:
                    with self._cond:
                        self._cond.wait(self.poll_interval)

        return ThreadSubscription(loop, name=f"fake-subscribe-{self.address}")

    def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------ test hooks

    def head(self) -> str | None:
        """Cursor just after the newest message in this mailbox (None when empty)."""
        with self._locked(shared=True):
            rows = self._index_rows(self._box(self.address))
        return str(rows[-1]["n"]) if rows else None

    def deliver(self, raw: bytes, recipients: tuple[str, ...] | list[str] | None = None, *,
                received_at: Ms | None = None) -> list[tuple[str, Ms]]:
        """Store raw bytes as delivered (no header added), once per recipient. Returns
        `(provider_id, received_at)` per recipient. `received_at` overrides the clock."""
        rcpts = list(recipients) if recipients else [self.address]
        out: list[tuple[str, Ms]] = []
        with self._locked(shared=False):
            state = self._read_state()
            for rcpt in dict.fromkeys(r.strip().lower() for r in rcpts if r.strip()):
                now = int(self.clock() * 1000)
                when = received_at if received_at is not None else max(now, state["last_ms"] + 1)
                state["last_ms"] = max(state["last_ms"], when)
                pid = f"fake-{state['next']}"
                state["next"] += 1
                box = self._box(rcpt)
                box.mkdir(parents=True, exist_ok=True)
                rows = self._index_rows(box)
                n = rows[-1]["n"] + 1 if rows else 1
                name = f"{n}.eml"
                tmp = box / f".{name}.tmp"
                tmp.write_bytes(raw)
                os.replace(tmp, box / name)
                with open(box / "index.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"n": n, "t": when, "id": pid, "f": name}) + "\n")
                    fh.flush()
                out.append((pid, when))
            self._write_state(state)
        with self._cond:
            self._cond.notify_all()
        return out

    # ------------------------------------------------------------ internals

    @contextmanager
    def _locked(self, *, shared: bool) -> Iterator[None]:
        with open(self.root / "lock", "rb") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _box(self, address: str) -> Path:
        return self.root / "boxes" / _box_name(address)

    @staticmethod
    def _index_rows(box: Path) -> list[dict]:
        path = box / "index.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _read_state(self) -> dict:
        path = self.root / "state.json"
        if not path.exists():
            return {"last_ms": 0, "next": 1}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        tmp = self.root / ".state.json.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.root / "state.json")

    @staticmethod
    def _parse_cursor(cursor: str | None) -> int:
        if cursor is None or cursor == "":
            return 0
        if not cursor.isdigit():
            raise TransportError(f"invalid fake cursor {cursor!r}")
        return int(cursor)


def from_config(cfg: Mapping[str, str]) -> FakeMailbox:
    """cfg: `root` (directory), `address`; optional `max_message_bytes`, `authserv_id`."""
    root = cfg.get("root") or os.environ.get("GUD_HOME", "")
    if not root:
        raise TransportError("fake transport needs `root`")
    if not cfg.get("address"):
        raise TransportError("fake transport needs `address`")
    root_path = Path(root) if cfg.get("root") else Path(root) / "fake-mail"
    return FakeMailbox(
        root_path, cfg["address"],
        max_message_bytes=cfg_int(cfg, "max_message_bytes", DEFAULT_MAX_MESSAGE_BYTES) or DEFAULT_MAX_MESSAGE_BYTES,
        authserv_id=cfg.get("authserv_id") or FAKE_AUTHSERV_ID,
    )


# ---------------------------------------------------------------- in-memory network


@dataclass(frozen=True)
class Faults:
    """Deterministic fault injection for `FakeNetwork` (driven by `random.Random(seed)`)."""
    delay_ms: int = 0          # fixed transit delay per delivery
    jitter_ms: int = 0         # extra uniform delay in [0, jitter_ms]; makes later sends overtake earlier ones
    duplicate: float = 0.0     # probability a delivery arrives twice
    drop: float = 0.0          # probability a delivery never arrives
    seed: int = 0


@dataclass(order=True)
class _Pending:
    due: Ms
    seq: int
    rcpt: str = field(compare=False)
    raw: bytes = field(compare=False)


class FakeNetwork:
    """In-memory mail network with a virtual clock. `advance(ms)` moves time and delivers what
    is due; deliveries get strictly increasing `received_at` in delivery order. Subscribers are
    called synchronously from `send`/`advance`, so tests are fully deterministic."""

    def __init__(self, *, start_ms: Ms = 1_760_000_000_000, faults: Faults | None = None,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                 authserv_id: str = FAKE_AUTHSERV_ID) -> None:
        self._now = start_ms
        self.faults = faults or Faults()
        self._rng = random.Random(self.faults.seed)
        self.max_message_bytes = max_message_bytes
        self.authserv_id = authserv_id
        self._pending: list[_Pending] = []
        self._seq = 0
        self._last_ms = 0
        self._next_id = 1
        self._boxes: dict[str, list[Received]] = {}
        self._subs: dict[str, list[Callable[[Received], None]]] = {}
        self._lock = threading.RLock()
        self._msg_seq = 0          # network-wide, so Message-IDs never repeat across mailboxes

    @property
    def now_ms(self) -> Ms:
        return self._now

    def clock(self) -> float:
        return self._now / 1000.0

    def set_faults(self, faults: Faults) -> None:
        with self._lock:
            self.faults = faults
            self._rng = random.Random(faults.seed)

    def mailbox(self, address: str) -> "NetworkMailbox":
        return NetworkMailbox(self, address)

    @property
    def pending(self) -> int:
        return len(self._pending)

    def next_due(self) -> Ms | None:
        with self._lock:
            return min((p.due for p in self._pending), default=None)

    def advance(self, ms: int) -> int:
        return self.advance_to(self._now + max(0, int(ms)))

    def advance_to(self, when: Ms) -> int:
        """Move the clock to `when` (never backwards) and deliver everything due by then."""
        delivered = 0
        while True:
            with self._lock:
                due = sorted(p for p in self._pending if p.due <= when)
                if not due:
                    self._now = max(self._now, when)
                    return delivered
                item = due[0]
                self._pending.remove(item)
                self._now = max(self._now, item.due)
            self._deliver_now(item.rcpt, item.raw)
            delivered += 1

    def submit(self, raw: bytes, recipients: tuple[str, ...]) -> int:
        """Queue raw bytes (as stored) for each recipient, applying faults. Returns the number
        of copies that will be delivered."""
        copies_total = 0
        immediate: list[tuple[str, bytes]] = []
        with self._lock:
            for rcpt in dict.fromkeys(r.strip().lower() for r in recipients if r.strip()):
                if self.faults.drop and self._rng.random() < self.faults.drop:
                    continue
                copies = 2 if (self.faults.duplicate and self._rng.random() < self.faults.duplicate) else 1
                for copy in range(copies):
                    delay = self.faults.delay_ms + (self._rng.randint(0, self.faults.jitter_ms) if self.faults.jitter_ms else 0)
                    delay += copy
                    self._seq += 1
                    if delay <= 0:
                        immediate.append((rcpt, raw))
                    else:
                        self._pending.append(_Pending(self._now + delay, self._seq, rcpt, raw))
                    copies_total += 1
        for rcpt, data in immediate:
            self._deliver_now(rcpt, data)
        return copies_total

    def _deliver_now(self, rcpt: str, raw: bytes) -> Received:
        with self._lock:
            when = max(self._now, self._last_ms + 1)
            self._last_ms = when
            pid = f"net-{self._next_id}"
            self._next_id += 1
            box = self._boxes.setdefault(rcpt, [])
            item = Received(raw_mime=raw, received_at=when, provider_id=pid, cursor=str(len(box) + 1))
            box.append(item)
            subs = list(self._subs.get(rcpt, ()))
        for callback in subs:
            safe_call(callback, item)
        return item

    def _items(self, address: str) -> list[Received]:
        with self._lock:
            return list(self._boxes.get(address.lower(), ()))

    def _subscribe(self, address: str, callback: Callable[[Received], None]) -> None:
        with self._lock:
            self._subs.setdefault(address.lower(), []).append(callback)

    def _unsubscribe(self, address: str, callback: Callable[[Received], None]) -> None:
        with self._lock:
            subs = self._subs.get(address.lower(), [])
            if callback in subs:
                subs.remove(callback)


class _NetSubscription:
    def __init__(self, net: FakeNetwork, address: str, callback: Callable[[Received], None]) -> None:
        self._net, self._address, self._callback = net, address, callback

    def close(self) -> None:
        self._net._unsubscribe(self._address, self._callback)


class NetworkMailbox:
    """A `Mailbox` on a `FakeNetwork`. Message-IDs and Date headers are derived from a counter
    and the virtual clock, so the same script produces the same bytes."""

    def __init__(self, net: FakeNetwork, address: str) -> None:
        self.net = net
        self.address = address
        self._msg_counter = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="fake", max_message_bytes=self.net.max_message_bytes, push="none",
            preserves_message_id=True, auth_results="header", authserv_id=self.net.authserv_id,
            send_quota_per_day=None, received_time_resolution_ms=1, raw_mime=True,
        )

    def send(self, message: Outgoing | bytes) -> Sent:
        if isinstance(message, Outgoing):
            if message.message_id is None:
                with self.net._lock:
                    self.net._msg_seq += 1
                    self._msg_counter = self.net._msg_seq
                domain = self.address.rsplit("@", 1)[-1]
                message = replace(message, message_id=f"<net-{self.net.now_ms}-{self._msg_counter}@{domain}>")
            raw = mime.build(message, date=formatdate(self.net.clock(), usegmt=True))
        else:
            raw = bytes(message)
        if len(raw) > self.net.max_message_bytes:
            raise TransportError(f"message is {len(raw)} bytes; the limit is {self.net.max_message_bytes}")
        rcpts = _recipients(message)
        if not rcpts:
            raise TransportError("message has no recipients")
        accepted = self.net.now_ms
        self.net.submit(_from_and_auth(raw, self.address, self.net.authserv_id), rcpts)
        return Sent(provider_id=None, message_id=mime.parse(raw).message_id, accepted_at=accepted)

    def deliver(self, raw: bytes, recipients: tuple[str, ...] | None = None) -> int:
        """Inject raw bytes from an external MTA (nothing added), subject to the network's faults."""
        return self.net.submit(raw, tuple(recipients or (self.address,)))

    def head(self) -> str | None:
        items = self.net._items(self.address)
        return items[-1].cursor if items else None

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        after = FakeMailbox._parse_cursor(since)
        items = self.net._items(self.address)[after:after + max(0, limit)]
        return items, (items[-1].cursor if items else since)

    def wait(self, *, since: str | None, timeout: float) -> list[Received]:
        """Virtual time: if nothing is there, advance the network clock to the next due delivery
        (or by `timeout`) instead of sleeping."""
        deadline = self.net.now_ms + int(max(0.0, timeout) * 1000)
        while True:
            items, _ = self.list(since=since)
            if items or self.net.now_ms >= deadline:
                return items
            due = self.net.next_due()
            self.net.advance_to(min(due, deadline) if due is not None else deadline)

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription:
        if since is not None:
            for item in self.list(since=since, limit=1_000_000)[0]:
                safe_call(callback, item)
        self.net._subscribe(self.address, callback)
        return _NetSubscription(self.net, self.address, callback)

    def close(self) -> None:
        pass
