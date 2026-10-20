"""Host-side intake (SPEC-v1 §11, PLAN Phase 1b): the single entry point for "an email arrived".

Every path ends in `on_received`: the adapter's push intake (`Mailbox.subscribe`: IMAP IDLE,
JMAP EventSource, Gmail Pub/Sub, a provider long-poll), provider webhooks (`on_webhook`, through
the adapter's `WebhookVerifier`), and catch-up polling (`catch_up`, on start, after reconnects
and as a slow safety net). One received message goes through, under one lock:

1. parse (`transport.mime.to_email`); messages replay ignores are dropped;
2. filter (§11.1): every v1 envelope message that decodes, and unsigned mail only when it passes
   §8.1 (2) and (3) at the tip; mail from non-members is rate limited per sender;
3. dedupe by gud id (webhooks and IMAP are at least once; the log never holds an id twice);
4. store the raw MIME (before the log entry, so a logged id always has its mail);
5. append a signed `mail` entry with the received time `max(min(provider time, now), prev + 1)`;
6. feed the `Replayer` with exactly that entry and email (an incremental step, §12);
7. publish the delta's events to the `Dispatcher` (journal + wake-up) and queue §19.4 human
   notifications.

Time-only changes (window closes, park times, lapses, escalation deadlines) need the log head to
pass them (§11.3). `tick()` appends a `tick` entry as soon as the clock reaches the last delta's
`next_wakeup`, after 5 s of silence while anything is open (a pending timer), and at least every
60 s, then advances replay. Missing raw mail (a lost file) stalls replay just before that entry;
intake re-reads it from the store, then from the mailbox, and reports what it cannot find.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from gud.core import envelope as E
from gud.core.config import Members, RepoRules
from gud.core.hostlog import HostLog, LogError, decode_entry, encode_entry
from gud.core.replay import Replayer, ReplayError, snapshot_at
from gud.core.types import ChainDelta, Email, Hex, LogEntry, Ms, Seen
from gud.transport import auth, mime
from gud.transport.base import Mailbox, Received, TransportError, WebhookVerifier

from .dispatch import Dispatcher
from .model import EVENT_TYPES

_TYPE_ORDER = {t: i for i, t in enumerate(EVENT_TYPES)}


class IntakeError(ValueError):
    """The stored host log, key or replay cache do not fit together."""


class WebhookRejected(PermissionError):
    """A provider webhook failed its signature check."""


def _merge(a: ChainDelta | None, b: ChainDelta) -> ChainDelta:
    if a is None:
        return b
    return replace(
        b,
        t_before=a.t_before,
        new_links=a.new_links + b.new_links,
        changed_proposals=_last_by_id(a.changed_proposals + b.changed_proposals),
        changed_tasks=_last_by_id(a.changed_tasks + b.changed_tasks),
        changed_claims=_last_by_id(a.changed_claims + b.changed_claims),
        changed_escalations=_last_by_id(a.changed_escalations + b.changed_escalations),
        red=b.red if b.red is not None else a.red,
        events=tuple(sorted(a.events + b.events, key=lambda e: (e.at, _TYPE_ORDER.get(e.type, 99), e.id))),
    )


def _last_by_id(items: tuple) -> tuple:
    out: dict = {}
    for item in items:
        out.pop(item.id, None)
        out[item.id] = item
    return tuple(out.values())


class Intake:
    """Host-side single entry point for 'an email arrived' (§11, Phase 1b)."""

    def __init__(self, *, repo: str, mailbox: Mailbox, replayer: Replayer, log_path: Path,
                 host_private: bytes, mail_store: Path, dispatcher: Dispatcher,
                 clock: Callable[[], float] = time.time, rate_limit_per_sender: int = 120,
                 state_path: Path | None = None, verifiers: Mapping[str, WebhookVerifier] | None = None,
                 tick_idle_ms: int = 5_000, tick_max_ms: int = 60_000, catch_up_interval_s: float = 30.0,
                 save_interval_s: float = 5.0,
                 on_hop: Callable[[str, Hex, float], None] | None = None) -> None:
        self.repo = repo
        self.mailbox = mailbox
        self.replayer = replayer
        self.log_path = Path(log_path)
        self.mail_store = Path(mail_store)
        self.dispatcher = dispatcher
        self.clock = clock
        self.rate_limit_per_sender = rate_limit_per_sender
        self.state_path = Path(state_path) if state_path is not None else self.log_path.with_name("intake.json")
        self.tick_idle_ms = tick_idle_ms
        self.tick_max_ms = tick_max_ms
        self.catch_up_interval_s = catch_up_interval_s
        self.save_interval_s = save_interval_s
        self.on_hop = on_hop
        self._key = host_private
        self._lock = threading.RLock()
        self._log_cond = threading.Condition(self._lock)
        self._catch_lock = threading.Lock()
        self._rate: dict[str, deque] = {}
        self._rules: tuple[Hex, RepoRules] | None = None
        self.last_error: str | None = None
        self.missing: tuple[Hex, ...] = ()
        self.next_wakeup: Ms | None = None
        self._dirty = False
        self.stats = {"received": 0, "logged": 0, "duplicates": 0, "ignored": 0, "rate_limited": 0,
                      "ticks": 0, "webhooks": 0}
        self.mail_store.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        self._cursor: str | None = state.get("cursor")
        self._notify_events: set[Hex] = set(state.get("notify_events") or [])
        # snapshot sha256 -> log seq, from logged `checkpoint` messages (served on request, §17)
        self._checkpoints: dict[Hex, int] = {k: int(v) for k, v in (state.get("checkpoints") or {}).items()}
        self.verifiers: dict[str, WebhookVerifier] = dict(verifiers or {})
        if not self.verifiers and callable(getattr(mailbox, "verify", None)) and callable(getattr(mailbox, "parse", None)):
            try:
                self.verifiers[mailbox.capabilities().name] = mailbox  # type: ignore[assignment]
            except Exception:
                pass
        self._log = self._load_log()
        self._store = replayer.reader().store
        if dispatcher.seen is None:
            dispatcher.seen = self.seen
        self._sync_replayer()

    # ------------------------------------------------------------ persistence

    def _read_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _write_state(self) -> None:
        data = {"cursor": self._cursor, "notify_events": sorted(self._notify_events),
                "checkpoints": dict(sorted(self._checkpoints.items()))}
        tmp = self.state_path.with_name(f".{self.state_path.name}.tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        os.replace(tmp, self.state_path)

    def _load_log(self) -> HostLog:
        entries: list[LogEntry] = []
        if self.log_path.exists():
            for line in self.log_path.read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(decode_entry(json.loads(line).encode("utf-8")))
                except (json.JSONDecodeError, LogError, AttributeError):
                    break             # a torn final line: that entry was never acknowledged
        if entries:
            try:
                log = HostLog(entries)
            except LogError as exc:
                raise IntakeError(f"stored host log does not verify: {exc}") from exc
            if log.repo != self.repo:
                raise IntakeError(f"stored host log is for {log.repo}, not {self.repo}")
            if log.key != E.public_of(self._key):
                raise IntakeError("the host key is not the log's current key")
            if len(entries) and self.log_path.read_text("utf-8").count("\n") != len(entries):
                self._rewrite_log(log)
            return log
        log = HostLog.open(self.repo, self._key, self._now())
        self._rewrite_log(log)
        return log

    def _rewrite_log(self, log: HostLog) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.log_path.with_name(f".{self.log_path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for entry in log.entries():
                fh.write(json.dumps(encode_entry(entry).decode("utf-8")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.log_path)

    def _persist_entry(self, entry: LogEntry) -> None:
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(encode_entry(entry).decode("utf-8")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _mail_path(self, gud_id: Hex) -> Path:
        return self.mail_store / gud_id[:2] / f"{gud_id}.eml"

    def _store_raw(self, gud_id: Hex, raw: bytes) -> None:
        path = self._mail_path(gud_id)
        if path.exists() or (self.mail_store / f"{gud_id}.eml").exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def save(self) -> None:
        """Persist the replay cache (when the Replayer has a state_dir) and the intake state."""
        with self._lock:
            self._write_state()
            try:
                self.replayer.save()
            except ReplayError:
                pass
            self._dirty = False

    # ------------------------------------------------------------ accessors

    def _now(self) -> Ms:
        return int(self.clock() * 1000)

    def _hop(self, name: str, gud_id: Hex) -> None:
        if self.on_hop is not None:
            self.on_hop(name, gud_id, time.perf_counter())

    @property
    def lock(self) -> threading.RLock:
        """The intake lock (held while a message is logged and replayed). Readers of the replayer's
        state from other threads (the UI view API) hold it so they never see a half-applied step."""
        return self._lock

    @property
    def log(self) -> HostLog:
        return self._log

    def head(self) -> LogEntry:
        with self._lock:
            head = self._log.head
            assert head is not None
            return head

    def seen(self) -> Seen:
        """The host's current log head, for the `Gud-Seen` of host-signed notify messages."""
        head = self.head()
        return Seen(head.seq, head.hash)

    def log_entries(self, since: int, limit: int) -> list[LogEntry]:
        with self._lock:
            return self._log.entries(max(0, since), max(0, limit))

    def mail_since(self, since: int, limit: int) -> tuple[list[tuple[int, Hex]], int | None]:
        """Mail entries with seq ≥ since: ([(seq, id)], next seq to ask for or None)."""
        with self._lock:
            out: list[tuple[int, Hex]] = []
            nxt = None
            for seq, gid, _ in self._log.mail_ids(max(0, since)):
                if len(out) >= limit:
                    nxt = seq
                    break
                out.append((seq, gid))
            return out, nxt

    def wait_log(self, since: int, timeout: float) -> bool:
        """Block until the log has an entry with seq ≥ since, or `timeout` seconds pass."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._log_cond:
            while len(self._log) <= since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._log_cond.wait(remaining)
            return True

    def raw(self, gud_id: Hex) -> bytes | None:
        if not E.is_hex64(gud_id):
            return None
        for path in (self._mail_path(gud_id), self.mail_store / f"{gud_id}.eml"):   # sharded, or flat (archive import)
            try:
                return path.read_bytes()
            except FileNotFoundError:
                continue
        return None

    def object(self, sha256: Hex) -> bytes | None:
        """Blob content (or any object) from the replay store, §21 `GET /api/v1/object`."""
        if not E.is_hex64(sha256):
            return None
        try:
            return self._store.get(sha256) if self._store.has(sha256) else None
        except (KeyError, OSError):
            return None

    def rules(self) -> RepoRules | None:
        """Rules at the tip (cached per rules root); None before genesis."""
        with self._lock:
            tip = self.replayer.tip
            if tip is None:
                return None
            if self._rules is None or self._rules[0] != tip.rules_root:
                self._rules = (tip.rules_root, RepoRules.decode(self._store.get(tip.rules_root)))
            return self._rules[1]

    def members(self) -> Members | None:
        rules = self.rules()
        return rules.members if rules is not None else None

    def host_keys(self) -> tuple[str, ...]:
        """Keys a webhook or mirror response may be signed with (§11.6)."""
        keys = [self._log.key]
        members = self.members()
        if members is not None:
            try:
                key = members.host().public_key
            except ValueError:
                key = None
            if key and key not in keys:
                keys.append(key)
        return tuple(keys)

    def log_has_event(self, event_id: Hex) -> bool:
        """Whether a `notify` with this `Gud-Event` is logged (§19.4 dedupe across dispatchers)."""
        with self._lock:
            return event_id in self._notify_events

    # ------------------------------------------------------------ replay

    def _feed(self, entries: Sequence[LogEntry], emails: Sequence[Email], gid: Hex | None = None) -> ChainDelta | None:
        now = max(self._now(), self._log.head.time if self._log.head else 0)
        delta: ChainDelta | None = None
        try:
            delta = self.replayer.feed(list(emails), list(entries), now)
        except ReplayError as exc:
            self.last_error = str(exc)
            delta = exc.delta
        rounds = 0
        while delta is not None and delta.missing_mail and rounds < 10_000:
            rounds += 1
            found = [e for e in (self._email_from_store(g) for g in delta.missing_mail[:500]) if e is not None]
            if not found:
                break
            delta = _merge(delta, self.replayer.feed(found, [], now))
        if gid is not None:
            self._hop("replayed", gid)
        if delta is not None:
            self.missing = delta.missing_mail
            self.next_wakeup = delta.next_wakeup
            if delta.events:
                self.dispatcher.publish(delta.events)
                self.dispatcher.notify_humans(delta.events, self.log_has_event)
        if gid is not None:
            self._hop("journaled", gid)
        self._dirty = True
        return delta

    def _email_from_store(self, gud_id: Hex) -> Email | None:
        raw = self.raw(gud_id)
        if raw is None:
            return None
        email = mime.to_email(raw)
        return email if email is not None and email.gud_id == gud_id else None

    def _sync_replayer(self) -> None:
        with self._lock:
            rlog = self.replayer.log()
            n = len(rlog)
            if n > len(self._log) or (n and rlog.at(n - 1).hash != self._log.at(n - 1).hash):
                raise IntakeError("the replay cache does not match the host log")
            self._feed(self._log.entries(n), [])
            if self.missing:
                self.repair()

    def repair(self) -> int:
        """Re-read missing raw mail from the mailbox (a full `list()` scan) and feed it. Returns
        how many messages were recovered; the rest stay in `missing`."""
        with self._lock:
            want = set(self.missing)
        if not want:
            return 0
        found = 0
        cursor = None
        try:
            while want:
                items, nxt = self.mailbox.list(since=cursor, limit=500)
                if not items:
                    break
                for item in items:
                    email = mime.to_email(item.raw_mime, received_at=item.received_at, provider_id=item.provider_id)
                    if email is not None and email.gud_id in want:
                        self._store_raw(email.gud_id, item.raw_mime)
                        want.discard(email.gud_id)
                        found += 1
                cursor = nxt
        except TransportError as exc:
            self.last_error = f"repair: {exc}"
        if found:
            with self._lock:
                self._feed([], [])
        return found

    # ------------------------------------------------------------ intake

    def _loggable(self, email: Email) -> bool:
        """§11.1: every decoding v1 envelope message; unsigned mail passing §8.1 (2), (3) at the tip."""
        if email.envelope is not None:
            return True
        rules = self.rules()
        if rules is None or not rules.config.authserv_id:
            return False
        member = rules.members.by_address(email.from_addr)
        if member is None or member.role != "human":
            return False
        return auth.sender_verified(email.auth_results, email.from_addr, rules.config.authserv_id)

    def _rate_ok(self, email: Email, now: Ms) -> bool:
        members = self.members()
        if members is not None and members.by_address(email.from_addr) is not None:
            return True
        if self.rate_limit_per_sender <= 0:
            return True
        key = (email.from_addr or "").lower()
        window = self._rate.setdefault(key, deque())
        while window and window[0] <= now - 60_000:
            window.popleft()
        if len(window) >= self.rate_limit_per_sender:
            return False
        window.append(now)
        return True

    def on_received(self, item: Received) -> ChainDelta | None:
        """Parse, filter, dedupe, log, replay and publish one received message. Returns the
        replay delta, or None when the message was not logged (ignored, duplicate, limited)."""
        email = mime.to_email(item.raw_mime, received_at=item.received_at, provider_id=item.provider_id)
        with self._lock:
            self.stats["received"] += 1
            if email is None:
                self.stats["ignored"] += 1
                return None
            gid = email.gud_id
            self._hop("received", gid)
            if self._log.seq_of(gid) is not None:
                self.stats["duplicates"] += 1
                return None
            if self._log.head is not None and self._log.head.type == "handover":
                self.stats["ignored"] += 1          # handed over (§11.6): the new host logs it
                return None
            now = self._now()
            head = self._log.head
            assert head is not None
            clock_ms = max(now, head.time + 1)              # the host clock never runs backwards
            when = max(min(int(item.received_at), clock_ms), head.time + 1)   # a provider time ahead is clamped
            if email.envelope is None and self.next_wakeup is not None and head.time < when - 1 \
                    and self.next_wakeup <= when - 1:
                # §8.1 is judged at "the tip at received time": the tip after every timer before
                # `when` and before anything decided at `when` (replay note 1). Bring replay there.
                tick = self._log.append_tick(self._key, when - 1)
                self._persist_entry(tick)
                self.stats["ticks"] += 1
                self._feed([tick], [])
            if not self._loggable(email):
                self.stats["ignored"] += 1
                return None
            if not self._rate_ok(email, now):
                self.stats["rate_limited"] += 1
                return None
            self._store_raw(gid, item.raw_mime)
            entry = self._log.append_mail(gid, when, self._key, clock_ms)
            self._persist_entry(entry)
            self.stats["logged"] += 1
            if email.envelope is not None and email.envelope.kind == "notify":
                event = email.envelope.get("Gud-Event")
                if event:
                    self._notify_events.add(event)
                    self._write_state()
            if email.envelope is not None and email.envelope.kind == "checkpoint":
                self._index_checkpoint(email.envelope)
            self._hop("logged", gid)
            self._log_cond.notify_all()
            return self._feed([entry], [email], gid)

    def on_webhook(self, provider: str, headers: Mapping[str, str], body: bytes) -> int:
        """Verify and parse a provider webhook, then `on_received` each message. Returns how many
        were newly logged. Raises KeyError for an unknown provider, WebhookRejected for a bad
        signature, TransportError for a body that does not parse."""
        verifier = self.verifiers.get(provider)
        if verifier is None:
            raise KeyError(provider)
        if not verifier.verify(headers, body, self._now()):
            raise WebhookRejected(f"{provider} webhook signature does not verify")
        self.stats["webhooks"] += 1
        accepted = 0
        for item in verifier.parse(headers, body):
            if self.on_received(item) is not None:
                accepted += 1
        return accepted

    def register_verifier(self, provider: str, verifier: WebhookVerifier) -> None:
        """Plug in another provider's webhook (`POST /api/v1/webhook/<provider>`)."""
        self.verifiers[provider] = verifier

    def catch_up(self) -> int:
        """`list()` from the stored cursor (on start and after reconnect). Returns how many
        messages were newly logged. Only list cursors are persisted (transport note 4)."""
        with self._catch_lock:
            cursor = self._cursor
            logged = 0
            while True:
                items, nxt = self.mailbox.list(since=cursor, limit=500)
                if not items:
                    break
                for item in items:
                    if self.on_received(item) is not None:
                        logged += 1
                    if item.cursor:
                        cursor = item.cursor
                with self._lock:
                    self._cursor = cursor
                    self._write_state()
                if len(items) < 500:
                    break
            return logged

    def _tick_due(self, now: Ms) -> bool:
        head = self._log.head
        assert head is not None
        if now <= head.time or head.type == "handover":
            return False
        if self.next_wakeup is not None and now >= self.next_wakeup:
            return True
        quiet = now - head.time
        if self.next_wakeup is not None and quiet >= self.tick_idle_ms:
            return True                                   # something is open (it has a timer)
        return quiet >= self.tick_max_ms

    def tick(self, *, force: bool = False) -> ChainDelta | None:
        """Append a `tick` entry when one is due (§11.1, and the next replay wake-up) and advance
        replay. Returns the delta, or None when no tick was due."""
        with self._lock:
            now = self._now()
            if not (self._tick_due(now) or (force and now > self._log.head.time and self._log.head.type != "handover")):
                return None
            entry = self._log.append_tick(self._key, now)
            self._persist_entry(entry)
            self.stats["ticks"] += 1
            self._log_cond.notify_all()
            return self._feed([entry], [])

    def handover(self, new_public: str, *, repo: str | None = None) -> LogEntry:
        """Planned handover (§11.6): append `handover` naming the new host key (and the repo's new
        address, integration note 4). After it this host logs nothing; export the archive
        (`gud.ops.archive.export_archive(intake, ...)`) for the new host."""
        with self._lock:
            head = self._log.head
            if head is None or head.type == "handover":
                raise IntakeError("the log already ends in a handover")
            entry = self._log.handover(self._key, new_public, max(self._now(), head.time + 1), repo=repo)
            self._persist_entry(entry)
            self._log_cond.notify_all()
            self._feed([entry], [])
            return entry

    def next_tick_in(self) -> float:
        """Seconds until a tick can next be due (for the run loop)."""
        with self._lock:
            head = self._log.head
            now = self._now()
            dues = [head.time + self.tick_max_ms]
            if self.next_wakeup is not None:
                dues += [self.next_wakeup, head.time + self.tick_idle_ms]
            due = max(min(dues), head.time + 1)
            return max(0.0, (due - now) / 1000.0)

    def run(self, stop: threading.Event) -> None:
        """subscribe + catch_up + tick loop + dispatcher thread, until `stop` is set."""
        def push(item: Received) -> None:
            try:
                self.on_received(item)
            except Exception as exc:  # the adapter loop must survive; catch-up covers the gap
                self.last_error = f"push intake: {exc}"

        sub = None
        try:
            sub = self.mailbox.subscribe(push)
        except TransportError as exc:
            self.last_error = f"subscribe: {exc}"
        dispatch = self.dispatcher.start(stop)
        last_catch = 0.0
        last_save = time.monotonic()
        try:
            while not stop.is_set():
                if time.monotonic() - last_catch >= self.catch_up_interval_s:
                    last_catch = time.monotonic()
                    try:
                        self.catch_up()
                    except TransportError as exc:
                        self.last_error = f"catch-up: {exc}"
                try:
                    self.tick()
                except LogError as exc:
                    self.last_error = f"tick: {exc}"
                if self._dirty and time.monotonic() - last_save >= self.save_interval_s:
                    last_save = time.monotonic()
                    self.save()
                stop.wait(min(0.5, max(0.005, self.next_tick_in())))
        finally:
            if sub is not None:
                sub.close()
            self.dispatcher.wake()
            dispatch.join(timeout=5)
            self.save()

    # ------------------------------------------------------------ snapshots

    def snapshot(self) -> tuple[Hex, bytes]:
        """The current replay snapshot (§17), stored by hash for `GET /api/v1/snapshot`."""
        with self._lock:
            snap = self.replayer.snapshot()
            data = snap.to_bytes()
            sha = E.sha256_hex(data)
            path = self.log_path.with_name("snapshots") / sha
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
            return sha, data

    def _index_checkpoint(self, env) -> None:
        snap, log = env.get("Gud-Snapshot"), env.get("Gud-Log")
        if not snap or not log or not E.is_hex64(snap) or snap in self._checkpoints:
            return
        try:
            self._checkpoints[snap] = E.parse_seen(log).seq
        except E.EnvelopeError:
            return
        self._write_state()

    def snapshot_bytes(self, sha256: Hex) -> bytes | None:
        """A stored snapshot, or the snapshot a logged `checkpoint` names (§17 clone from a
        checkpoint): computed by replaying entries <= its `Gud-Log` seq with now = that entry's
        time, kept only when it hashes to the name."""
        if not E.is_hex64(sha256):
            return None
        path = self.log_path.with_name("snapshots") / sha256
        try:
            return path.read_bytes()
        except FileNotFoundError:
            pass
        with self._lock:
            seq = self._checkpoints.get(sha256)
            if seq is None or seq >= len(self._log):
                return None
            entries = self._log.entries(0, seq + 1)
        try:
            data = snapshot_at(entries, self._email_from_store, seq, self._store, self._log.trusted_keys)
        except ReplayError:
            return None
        if E.sha256_hex(data) != sha256:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{sha256}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return data


def received_now(raw: bytes, clock: Callable[[], float] = time.time, provider_id: str = "inject") -> Received:
    """A `Received` for raw bytes handed to the host directly (loopback demo/admin injection)."""
    return Received(raw_mime=raw, received_at=int(clock() * 1000), provider_id=provider_id, cursor="")


__all__ = ["Intake", "IntakeError", "WebhookRejected", "received_now"]
