"""Event dispatch (SPEC-v1 §19.3, §19.4) and the signing used on the host's HTTP surface.

Host side:

- `Journal`: one durable, append-only JSONL file per member (`<root>/<handle>.jsonl`). Every
  event concerning a member gets the next journal seq (`cursor`, from 1). Appending an event id
  already in a member's journal is a no-op, so re-feeding replay after a restart, duplicate
  webhooks or a second dispatcher never produce a second item.
- `DeliveryRegistry`: each member's delivery preference (webhook URL, SSE, or email, the
  default), held by the host and set with a signed `POST /api/v1/delivery`.
- `Dispatcher`: `publish` journals; `pump` delivers. Webhooks are POSTed in journal order with
  the §19.3 headers, signed by the host key over `"gud-hook/1\\n" + time + "\\n" + body`. A
  member's delivered position (`acked`) only moves on a 2xx, so delivery is at least once and
  resumes from the last acknowledged journal seq after a receiver outage or a host restart.
  Failures back off 1, 2, 4, ... s (capped at 10 min) for 24 h, then fall back to email.
  Members on the email channel get one host-signed `notify` per batch (≤ 60 s). SSE members
  read their journal through `sse()`. §19.4 human notifications are `notify` messages To the
  humans and Cc the repo, sent once per event id (checked against the log first).

Member side:

- `EventClient`: verifies webhook signatures, dedupes by event id, persists the cursor, and
  streams SSE with `Last-Event-ID` resume.
- `Debouncer`: batches bursts per session thread for about a second before an agent acts.

Mirror request auth v1 (§21) lives here too (`sign_request` / `verify_request`), because both
the host app and the member-side SSE client need it; `gud.host` re-exports it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import httpx

from gud.core import envelope as E
from gud.core.config import Members
from gud.core.types import Event, Handle, Hex, Member, Record, Seen
from gud.transport import mime
from gud.transport.base import Outgoing, Sent

from .model import (
    HUMAN_NOTIFY_TYPES, Delivery, JournalItem, event_dict, event_from_dict, from_json, recipients, thread_key,
    to_json,
)

HOOK_HEADERS = ("X-Gud-Event-Id", "X-Gud-Cursor", "X-Gud-Time", "X-Gud-Sig")
HOOK_PREFIX = b"gud-hook/1\n"
MAX_SKEW_S = 300
AUTH_HANDLE, AUTH_TIME, AUTH_SIG = "X-Gud-Handle", "X-Gud-Time", "X-Gud-Sig"
EMPTY_SHA = E.sha256_hex(b"")


# ---------------------------------------------------------------- small helpers

def _header(headers: Mapping[str, str] | Any, name: str) -> str | None:
    if headers is None:
        return None
    want = name.lower()
    try:
        items = list(headers.items())
    except Exception:
        return None
    for key, value in items:
        if str(key).lower() == want and value is not None:
            return str(value)
    return None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default


def _ms(clock: Callable[[], float]) -> int:
    return int(clock() * 1000)


# ---------------------------------------------------------------- webhook signatures (§19.3)

def hook_bytes(time_token: str, body: bytes) -> bytes:
    """The bytes a webhook signature covers: b"gud-hook/1\\n" + time + b"\\n" + body."""
    return HOOK_PREFIX + time_token.encode("ascii") + b"\n" + body


def sign_hook(body: bytes, host_private: bytes, now: float) -> dict[str, str]:
    """`X-Gud-Time` (integer unix seconds) and `X-Gud-Sig` for one webhook body."""
    token = str(int(now))
    return {"X-Gud-Time": token, "X-Gud-Sig": E.sign_bytes(host_private, hook_bytes(token, body))}


def verify_hook(headers: Mapping[str, str], body: bytes, host_keys: Sequence[str], now: float) -> bool:
    """§19.3 receiver check: a host key (tip `host` member key or current log key, §11.6) signs
    the body and time, and |now − X-Gud-Time| ≤ 300 s. Never raises."""
    token = _header(headers, "X-Gud-Time")
    sig = _header(headers, "X-Gud-Sig")
    if not token or not sig or not token.isdigit():
        return False
    if abs(float(now) - int(token)) > MAX_SKEW_S:
        return False
    payload = hook_bytes(token, body)
    return any(E.verify_bytes(key, payload, sig) for key in host_keys if key)


# ---------------------------------------------------------------- mirror request auth v1 (§21)

def format_time(now: float) -> str:
    """Stable decimal form of a unix time (§21 `X-Gud-Time`): whole seconds
    have no fraction, otherwise up to 6 digits without trailing zeros."""
    text = format(float(now), ".6f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def request_bytes(method: str, path_with_query: str, time_token: str, body: bytes = b"") -> bytes:
    """`"{METHOD} {path_with_query} {time} {sha256(body)}"` (§21; the empty body hashes too)."""
    return f"{method.upper()} {path_with_query} {time_token} {E.sha256_hex(body or b'')}".encode("utf-8")


def sign_request(handle: Handle, private_raw: bytes, method: str, path_with_query: str,
                 body: bytes, now: float) -> dict[str, str]:
    """Headers a member attaches to one mirror request."""
    token = format_time(now)
    return {AUTH_HANDLE: handle, AUTH_TIME: token,
            AUTH_SIG: E.sign_bytes(private_raw, request_bytes(method, path_with_query, token, body))}


def verify_request(members: Members | Mapping[Handle, Member] | None, method: str, path_with_query: str,
                   body: bytes, headers: Mapping[str, str], now: float, max_skew: float = MAX_SKEW_S) -> Handle | None:
    """The member handle when `headers` carry a valid v1 signature by a member at the tip whose
    key verifies it within ±`max_skew` seconds, else None. Never raises."""
    handle = _header(headers, AUTH_HANDLE)
    token = _header(headers, AUTH_TIME)
    sig = _header(headers, AUTH_SIG)
    if not handle or not token or not sig or members is None:
        return None
    try:
        signed_at = float(token)
    except ValueError:
        return None
    if signed_at != signed_at or abs(float(now) - signed_at) > float(max_skew):
        return None
    member = members.get(handle)
    if member is None or not member.public_key:
        return None
    if not E.verify_bytes(member.public_key, request_bytes(method, path_with_query, token, body), sig):
        return None
    return member.handle


# ---------------------------------------------------------------- journal

class Journal:
    """Durable per-member event journal (GUD_HOME/host/journal/<handle>.jsonl)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: dict[Handle, list[JournalItem]] = {}
        self._ids: dict[Handle, set[Hex]] = {}

    def _file(self, handle: Handle) -> Path:
        return self.root / f"{handle}.jsonl"

    def _load(self, handle: Handle) -> list[JournalItem]:
        items = self._items.get(handle)
        if items is not None:
            return items
        items = []
        path = self._file(handle)
        if path.exists():
            for line in path.read_bytes().splitlines():
                try:
                    obj = json.loads(line)
                    event = event_from_dict(obj)
                    cursor = int(obj["cursor"])
                except (ValueError, KeyError, TypeError):
                    continue          # a torn last line after a crash: that item was never delivered
                if cursor == len(items) + 1:
                    items.append(JournalItem(cursor, event, handle))
        self._items[handle] = items
        self._ids[handle] = {item.event.id for item in items}
        return items

    def handles(self) -> tuple[Handle, ...]:
        with self._lock:
            names = {p.name[:-6] for p in self.root.glob("@*.jsonl")}
            return tuple(sorted(names | set(self._items)))

    def append(self, events: Sequence[Event]) -> list[JournalItem]:
        """One item per (event, concerned member); an id already in that member's journal is skipped."""
        out: list[JournalItem] = []
        with self._lock:
            lines: dict[Handle, list[bytes]] = {}
            for event in events:
                for handle in event.concerns:
                    if not E.is_handle(handle):
                        continue
                    items = self._load(handle)
                    ids = self._ids[handle]
                    if event.id in ids:
                        continue
                    item = JournalItem(len(items) + 1, event, handle)
                    items.append(item)
                    ids.add(event.id)
                    out.append(item)
                    obj = event_dict(event)
                    obj["cursor"] = item.cursor
                    lines.setdefault(handle, []).append(E.jcs(obj) + b"\n")
            for handle, chunk in lines.items():
                with open(self._file(handle), "ab") as fh:
                    fh.write(b"".join(chunk))
                    fh.flush()
                    os.fsync(fh.fileno())
        return out

    def since(self, handle: Handle, after: int, limit: int = 500) -> list[JournalItem]:
        with self._lock:
            items = self._load(handle)
            start = max(0, int(after))
            return items[start:start + max(0, limit)]

    def head(self, handle: Handle) -> int:
        with self._lock:
            return len(self._load(handle))

    def has(self, handle: Handle, event_id: Hex) -> bool:
        with self._lock:
            self._load(handle)
            return event_id in self._ids[handle]


# ---------------------------------------------------------------- delivery preferences

class DeliveryRegistry:
    """Per-member delivery preference (§19.3), persisted as one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        raw = _read_json(self.path, {})
        self._by: dict[Handle, Delivery] = {}
        for handle, d in (raw.items() if isinstance(raw, dict) else ()):
            if isinstance(d, dict) and d.get("channel") in ("webhook", "sse", "email"):
                self._by[handle] = Delivery(handle, d["channel"], d.get("url"))

    def set(self, delivery: Delivery) -> None:
        if delivery.channel not in ("webhook", "sse", "email"):
            raise ValueError(f"unknown channel {delivery.channel!r}")
        if delivery.channel == "webhook" and not (delivery.url or "").startswith(("http://", "https://")):
            raise ValueError("a webhook delivery needs an http(s) url")
        with self._lock:
            self._by[delivery.handle] = delivery
            data = {h: {"channel": d.channel, "url": d.url} for h, d in sorted(self._by.items())}
            _atomic_write(self.path, json.dumps(data, indent=1).encode("utf-8"))

    def get(self, handle: Handle) -> Delivery:
        with self._lock:
            return self._by.get(handle) or Delivery(handle, "email")

    def all(self) -> tuple[Delivery, ...]:
        with self._lock:
            return tuple(self._by[h] for h in sorted(self._by))


# ---------------------------------------------------------------- notify messages

_TASK_TARGET = frozenset({"task.opened", "task.ready", "task.done", "fix.opened", "claim.won", "claim.lost",
                          "claim.lapsed"})


def _target_headers(event: Event) -> dict[str, str]:
    """What a human reply to the notify should resolve to (§8.2): the task for task events (so
    `accept` is requester acceptance), the push for push events. An escalation over several
    pushes names none, so a bare `approve` reply cannot pick one by accident."""
    d = event.data

    def hexval(key: str) -> str | None:
        value = d.get(key)
        return value if isinstance(value, str) and E.is_hex64(value) else None

    if event.type in _TASK_TARGET:
        task = hexval("task")
        return {"Gud-Target": task, "Gud-Task": task} if task else {}
    if event.type == "subtask.posted":
        sub = hexval("subtask")
        return {"Gud-Target": sub} if sub else {}
    if event.type in ("escalated", "escalation.resolved"):
        pushes = [p for p in (d.get("pushes") or []) if isinstance(p, str) and E.is_hex64(p)]
        return {"Gud-Target": pushes[0]} if len(pushes) == 1 else {}
    headers: dict[str, str] = {}
    for key in ("push", "target", "candidate", "red_link", "link", "claim", "message"):
        value = hexval(key)
        if value:
            headers["Gud-Target"] = value
            break
    task = hexval("task")
    if task:
        headers["Gud-Task"] = task
    return headers


def notify_message(events: Sequence[Event], *, members: Members, host_private: bytes, seen: Seen,
                   to: Sequence[str], cc: Sequence[str] = (), sent: int) -> Outgoing:
    """A host-signed `notify` (§7, §19.3 (3)): `Gud-Event` = the first event, the rest listed in
    the text. `Gud-Target`/`Gud-Task` let replies resolve their target through it (§8.2)."""
    if not events:
        raise ValueError("notify needs at least one event")
    host = members.host()
    first = events[0]
    lines = [f"{e.type} {e.id} at {e.at}" for e in events]
    text = "gud events:\n" + "\n".join(lines) + "\n"
    headers = {"Gud-Event": first.id, "Gud-Event-Type": first.type, **_target_headers(first)}
    env = E.build("notify", author=host.handle, headers=headers, records=(Record("text", (), text.encode()),),
                  sent=sent, seen=seen)
    if E.validate_kind(env) is not None:
        headers = {"Gud-Event": first.id, "Gud-Event-Type": first.type}
        env = E.build("notify", author=host.handle, headers=headers,
                      records=(Record("text", (), text.encode()),), sent=sent, seen=seen)
    env = E.sign(env, host_private)
    gid = E.gud_id(env)
    summary = f"{first.type}" + (f" (+{len(events) - 1} more)" if len(events) > 1 else "")
    return Outgoing(from_addr=host.address, to=tuple(to), cc=tuple(cc), subject=mime.subject_for(env, gid, summary),
                    text=mime.render_human_part(env, gid), envelope=E.encode(env))


# ---------------------------------------------------------------- dispatcher

@dataclass
class _Retry:
    attempt: int = 0
    next_at: int = 0
    since: int = 0


class Dispatcher:
    """Host-side delivery of journaled events to members (§19.3, §19.4)."""

    def __init__(self, journal: Journal, registry: DeliveryRegistry, *, repo: str, host_private: bytes,
                 send: Callable[[Outgoing], Sent], members: Callable[[], Members | None],
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.time,
                 seen: Callable[[], Seen] | None = None, state_path: Path | None = None,
                 email_batch_s: float = 30.0, retry_cap_s: float = 600.0, retry_for_s: float = 86_400.0,
                 hook_timeout_s: float = 5.0, max_per_pass: int = 64,
                 on_delivered: Callable[[Handle, JournalItem, str], None] | None = None) -> None:
        self.journal = journal
        self.registry = registry
        self.repo = repo
        self._key = host_private
        self._send = send
        self._members = members
        self._http = http or httpx.Client(timeout=hook_timeout_s)
        self.clock = clock
        self.seen = seen
        self.email_batch_ms = int(email_batch_s * 1000)
        self.retry_cap_ms = int(retry_cap_s * 1000)
        self.retry_for_ms = int(retry_for_s * 1000)
        self.hook_timeout_s = hook_timeout_s
        # every agent gets every link (§19.2): bound the webhook posts per member per pass, so one member's
        # backlog never delays the others; `run` comes straight back for the rest
        self.max_per_pass = max(1, int(max_per_pass))
        self._backlog = False
        self.on_delivered = on_delivered
        self.state_path = Path(state_path) if state_path is not None else journal.root / "dispatch.json"
        self._cond = threading.Condition()
        self._wake = threading.Event()
        self._pump_lock = threading.Lock()
        self._lock = threading.Lock()
        self._log_has_event: Callable[[Hex], bool] = lambda _id: False
        state = _read_json(self.state_path, {})
        self._acked: dict[Handle, int] = {h: int(c) for h, c in (state.get("acked") or {}).items()}
        self._retry: dict[Handle, _Retry] = {h: _Retry(**r) for h, r in (state.get("retry") or {}).items()}
        self._email_since: dict[Handle, int] = {h: int(t) for h, t in (state.get("email_since") or {}).items()}
        self._notified: list[Hex] = list(state.get("notified") or [])
        self._notified_set = set(self._notified)
        self._human_queue: list[Event] = []
        for obj in state.get("human_queue") or []:
            try:
                self._human_queue.append(event_from_dict(obj))
            except ValueError:
                pass
        self.stats = {"webhook": 0, "email": 0, "notify": 0, "failures": 0}
        # called with every published batch (before journaling); the UI's all-events feed
        self.observers: list[Callable[[Sequence[Event]], None]] = []

    # ------------------------------------------------------------ state

    def _save(self) -> None:
        with self._lock:
            state = {
                "acked": dict(sorted(self._acked.items())),
                "retry": {h: r.__dict__ for h, r in sorted(self._retry.items())},
                "email_since": dict(sorted(self._email_since.items())),
                "notified": self._notified[-10_000:],
                "human_queue": [event_dict(e) for e in self._human_queue],
            }
        _atomic_write(self.state_path, json.dumps(state).encode("utf-8"))

    def acked(self, handle: Handle) -> int:
        """The journal seq up to which `handle` has acknowledged delivery."""
        return self._acked.get(handle, 0)

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------ intake side

    def publish(self, events: Sequence[Event]) -> list[JournalItem]:
        """Journal, then schedule delivery (wakes `run`). Returns the new journal items."""
        for observer in list(self.observers) if events else ():
            try:
                observer(events)
            except Exception:
                pass
        items = self.journal.append(events) if events else []
        if items:
            with self._cond:
                self._cond.notify_all()
            self._wake.set()
        return items

    def notify_humans(self, events: Sequence[Event], log_has_event: Callable[[Hex], bool]) -> None:
        """§19.4: queue `task.ready` accept requests, `task.done` and `escalated` for the human
        members they concern. `pump` sends each once, To the humans and Cc the repo, unless the
        log already has a `notify` for that event."""
        self._log_has_event = log_has_event
        queued = False
        with self._lock:
            known = {e.id for e in self._human_queue}
            for event in events:
                if event.type not in HUMAN_NOTIFY_TYPES:
                    continue
                if event.type == "task.ready" and event.data.get("needs") != "accept":
                    continue
                if event.id in self._notified_set or event.id in known:
                    continue
                self._human_queue.append(event)
                known.add(event.id)
                queued = True
        if queued:
            self._wake.set()

    # ------------------------------------------------------------ delivery

    def _members_now(self) -> Members | None:
        try:
            return self._members()
        except Exception:
            return None

    def pump(self) -> int:
        """One delivery pass. Returns the number of events delivered (webhook items, events in
        sent email batches and human notifications)."""
        with self._pump_lock:
            members = self._members_now()
            if members is None:
                return 0
            now = _ms(self.clock)
            sent = 0
            self._backlog = False
            handles = set(self.journal.handles()) | {d.handle for d in self.registry.all()}
            for handle in sorted(handles):
                sent += self._pump_member(handle, members, now)
            sent += self._pump_humans(members)
            self._save()
            return sent

    def _pump_member(self, handle: Handle, members: Members, now: int) -> int:
        member = members.get(handle)
        if member is None or member.role == "host":
            return 0                      # removed members get nothing; the journal keeps their items
        acked = self._acked.get(handle, 0)
        pending = self.journal.since(handle, acked)
        if not pending:
            self._email_since.pop(handle, None)
            return 0
        delivery = self.registry.get(handle)
        if delivery.channel == "sse":
            return 0
        if delivery.channel == "webhook" and delivery.url:
            retry = self._retry.get(handle)
            if retry is not None and now < retry.next_at:
                return 0
            done = 0
            if len(pending) > self.max_per_pass:
                self._backlog = True
            for item in pending[:self.max_per_pass]:
                if self._post(delivery.url, item):
                    self._acked[handle] = item.cursor
                    self._retry.pop(handle, None)
                    done += 1
                    self.stats["webhook"] += 1
                    if self.on_delivered is not None:
                        self.on_delivered(handle, item, "webhook")
                    continue
                self.stats["failures"] += 1
                retry = self._retry.get(handle) or _Retry(since=now)
                retry.attempt += 1
                retry.next_at = now + int(min(self.retry_cap_ms, 1000 * 2 ** min(retry.attempt - 1, 30)))
                self._retry[handle] = retry
                if now - retry.since >= self.retry_for_ms:
                    self._retry.pop(handle, None)
                    done += self._email(member, self.journal.since(handle, self._acked.get(handle, 0), 10_000))
                break
            return done
        since = self._email_since.setdefault(handle, now)
        if now - since < self.email_batch_ms:
            return 0
        return self._email(member, self.journal.since(handle, acked, 10_000))

    def _post(self, url: str, item: JournalItem) -> bool:
        body = to_json(item.event, self.repo)
        headers = {"Content-Type": "application/json", "X-Gud-Event-Id": item.event.id,
                   "X-Gud-Cursor": str(item.cursor), **sign_hook(body, self._key, self.clock())}
        try:
            resp = self._http.post(url, content=body, headers=headers, timeout=self.hook_timeout_s)
        except (httpx.HTTPError, OSError):
            return False
        return 200 <= resp.status_code < 300

    def _email(self, member: Member, items: Sequence[JournalItem]) -> int:
        if not items:
            return 0
        events = [it.event for it in items
                  if not (member.role == "human" and it.event.type in HUMAN_NOTIFY_TYPES)]
        if events:
            try:
                self._send(self._notify(events, to=(member.address,)))
            except Exception:
                self.stats["failures"] += 1
                return 0
        self._acked[member.handle] = items[-1].cursor
        self._email_since.pop(member.handle, None)
        self.stats["email"] += len(events)
        if self.on_delivered is not None:
            for it in items:
                self.on_delivered(member.handle, it, "email")
        return len(events)

    def _notify(self, events: Sequence[Event], *, to: Sequence[str], cc: Sequence[str] = ()) -> Outgoing:
        members = self._members_now()
        if members is None or self.seen is None:
            raise RuntimeError("notify needs the tip members and the log head")
        return notify_message(events, members=members, host_private=self._key, seen=self.seen(), to=to, cc=cc,
                              sent=int(self.clock()))

    def _pump_humans(self, members: Members) -> int:
        with self._lock:
            queue = list(self._human_queue)
        sent = 0
        keep: list[Event] = []
        for event in queue:
            if event.id in self._notified_set or self._log_has_event(event.id):
                self._mark_notified(event.id)
                continue
            humans = [m for m in recipients(event, members) if m.role == "human"]
            if not humans:
                continue
            try:
                self._send(self._notify([event], to=tuple(m.address for m in humans), cc=(self.repo,)))
            except Exception:
                self.stats["failures"] += 1
                keep.append(event)
                continue
            self._mark_notified(event.id)
            self.stats["notify"] += 1
            sent += 1
        with self._lock:
            done = {e.id for e in queue} - {e.id for e in keep}
            self._human_queue = [e for e in self._human_queue if e.id not in done]
        return sent

    def _mark_notified(self, event_id: Hex) -> None:
        with self._lock:
            if event_id not in self._notified_set:
                self._notified_set.add(event_id)
                self._notified.append(event_id)

    def _next_due_s(self) -> float:
        if self._backlog:
            return 0.0
        now = _ms(self.clock)
        dues = [r.next_at for r in self._retry.values()]
        dues += [t + self.email_batch_ms for t in self._email_since.values()]
        if not dues:
            return 1.0
        return max(0.01, min(1.0, (min(dues) - now) / 1000.0))

    def run(self, stop: threading.Event) -> None:
        """Deliver until `stop`: woken at once by `publish`, else at the next retry or batch."""
        while not stop.is_set():
            due = self._next_due_s()
            if due > 0:
                self._wake.wait(due)
            self._wake.clear()
            if stop.is_set():
                break
            try:
                self.pump()
            except Exception:
                time.sleep(0.05)

    def start(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(target=self.run, args=(stop,), name="gud-dispatch", daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------ SSE

    def sse(self, handle: Handle, after: int, *, follow: bool = True, keepalive_s: float = 15.0,
            max_s: float | None = None) -> Iterator[bytes]:
        """SSE frames (`id: <journal seq>`, `event: <type>`, `data: <event JSON>`) for `handle`'s
        journal after `after`; with `follow`, waits for new items, sending keepalive comments,
        until `max_s` seconds have passed (None = forever)."""
        cursor = max(0, int(after))
        deadline = None if max_s is None else time.monotonic() + max_s
        yield b": gud events\n\n"
        while True:
            items = self.journal.since(handle, cursor)
            for item in items:
                cursor = item.cursor
                yield (f"id: {item.cursor}\nevent: {item.event.type}\ndata: ".encode()
                       + to_json(item.event, self.repo) + b"\n\n")
            if items:
                continue
            if not follow:
                return
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            wait = keepalive_s if remaining is None else min(keepalive_s, remaining)
            with self._cond:
                if self.journal.head(handle) <= cursor:
                    self._cond.wait(wait)
            if self.journal.head(handle) <= cursor and (deadline is None or time.monotonic() < deadline):
                yield b": keepalive\n\n"


# ---------------------------------------------------------------- member side

class EventClient:
    """Member side: receive events by webhook or SSE, dedupe by event id, persist the cursor.

    `state_path` holds `{"cursor": <last journal seq>, "seen": [event ids]}`. `host_keys` returns
    the keys a webhook may be signed with (the tip `host` member key and the current log key)."""

    def __init__(self, state_path: Path, host_keys: Callable[[], Sequence[str]], *, repo: str | None = None,
                 max_seen: int = 100_000) -> None:
        self.state_path = Path(state_path)
        self.host_keys = host_keys
        self.repo = repo
        self.max_seen = max_seen
        self._lock = threading.Lock()
        state = _read_json(self.state_path, {})
        self._cursor = int(state.get("cursor") or 0)
        self._seen_list: list[Hex] = list(state.get("seen") or [])
        self._seen = set(self._seen_list)
        self.rejected = 0
        self.duplicates = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    def seen(self, event_id: Hex) -> bool:
        with self._lock:
            return event_id in self._seen

    def _save(self) -> None:
        if len(self._seen_list) > self.max_seen:
            self._seen_list = self._seen_list[-self.max_seen:]
            self._seen = set(self._seen_list)
        _atomic_write(self.state_path, json.dumps({"cursor": self._cursor, "seen": self._seen_list}).encode())

    def accept(self, event: Event, cursor: int | None) -> bool:
        """Record one event; False when its id was already seen (the cursor still advances)."""
        with self._lock:
            if cursor is not None and cursor > self._cursor:
                self._cursor = cursor
            if event.id in self._seen:
                self.duplicates += 1
                self._save()
                return False
            self._seen.add(event.id)
            self._seen_list.append(event.id)
            self._save()
            return True

    def accept_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> Event | None:
        """Verify (§19.3), parse and dedupe one webhook. None if the signature or body is bad, or
        the event was already seen; a receiver answers 2xx in both of the latter cases."""
        if not verify_hook(headers, body, list(self.host_keys()), now):
            self.rejected += 1
            return None
        try:
            repo, event = from_json(body)
        except ValueError:
            self.rejected += 1
            return None
        if (self.repo is not None and repo != self.repo) or _header(headers, "X-Gud-Event-Id") not in (None, event.id):
            self.rejected += 1
            return None
        cursor = _header(headers, "X-Gud-Cursor")
        return event if self.accept(event, int(cursor) if cursor and cursor.isdigit() else None) else None

    def stream(self, mirror: Any, handle: Handle, *, follow: bool = True, timeout: float | None = None,
               clock: Callable[[], float] = time.time) -> Iterator[Event]:
        """SSE from the host mirror with resume: `Last-Event-ID` = the stored cursor. `mirror` is
        a `RepoClient`-like object with `host_url`, `private_raw` and optionally `http`."""
        base = str(getattr(mirror, "host_url")).rstrip("/")
        private = getattr(mirror, "private_raw")
        http = getattr(mirror, "http", None) or httpx.Client(timeout=None)
        query = f"member={handle}&after={self._cursor}" + ("" if follow else "&follow=0")
        if timeout is not None:
            query += f"&max={int(timeout)}"
        path = f"/api/v1/events?{query}"
        headers = sign_request(handle, private, "GET", path, b"", clock())
        headers["Last-Event-ID"] = str(self._cursor)
        with http.stream("GET", base + path, headers=headers) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"SSE refused: HTTP {resp.status_code}")
            for cursor, data in parse_sse(resp.iter_lines()):
                try:
                    _, event = from_json(data.encode("utf-8"))
                except ValueError:
                    continue
                if self.accept(event, cursor):
                    yield event


def parse_sse(lines: Iterable[str]) -> Iterator[tuple[int | None, str]]:
    """(id, data) per SSE message from decoded lines; comments and unknown fields are skipped."""
    cur_id: int | None = None
    data: list[str] = []
    for line in lines:
        line = line.rstrip("\r")
        if line == "":
            if data:
                yield cur_id, "\n".join(data)
            cur_id, data = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "id":
            cur_id = int(value) if value.isdigit() else None
        elif name == "data":
            data.append(value)
    if data:
        yield cur_id, "\n".join(data)


@dataclass
class _Batch:
    first: float
    last: float
    events: list[Event] = field(default_factory=list)


class Debouncer:
    """Agent-side burst batching (§19.4 SHOULD ~1 s): events are grouped by session thread key
    and flushed once the thread has been quiet for `delay_s`, or `max_delay_s` after its first
    event, whichever comes first. Call `poll()` from the agent loop (or `flush_all()`)."""

    def __init__(self, flush: Callable[[str, list[Event]], None], *, delay_s: float = 1.0,
                 max_delay_s: float = 5.0, key: Callable[[Event], str] = thread_key,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._flush = flush
        self.delay_s = delay_s
        self.max_delay_s = max_delay_s
        self._key = key
        self.clock = clock
        self._lock = threading.Lock()
        self._batches: dict[str, _Batch] = {}

    def add(self, events: Iterable[Event]) -> None:
        now = self.clock()
        with self._lock:
            for event in events:
                key = self._key(event)
                batch = self._batches.get(key)
                if batch is None:
                    batch = self._batches[key] = _Batch(now, now)
                batch.last = now
                if all(e.id != event.id for e in batch.events):
                    batch.events.append(event)

    def pending(self) -> int:
        with self._lock:
            return sum(len(b.events) for b in self._batches.values())

    def next_due(self) -> float | None:
        with self._lock:
            dues = [min(b.last + self.delay_s, b.first + self.max_delay_s) for b in self._batches.values()]
        return min(dues) if dues else None

    def poll(self, now: float | None = None) -> int:
        """Flush every due batch; returns how many were flushed."""
        now = self.clock() if now is None else now
        with self._lock:
            due = [k for k, b in sorted(self._batches.items())
                   if now >= b.last + self.delay_s or now >= b.first + self.max_delay_s]
            batches = [(k, self._batches.pop(k).events) for k in due]
        for key, events in batches:
            self._flush(key, events)
        return len(batches)

    def flush_all(self) -> int:
        with self._lock:
            batches = [(k, b.events) for k, b in sorted(self._batches.items())]
            self._batches.clear()
        for key, events in batches:
            self._flush(key, events)
        return len(batches)
