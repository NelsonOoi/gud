"""The v1 replay engine (SPEC §11.3, §12–§18): incremental `Replayer` and the pure `replay()`.

Model
-----
Replay is a discrete-event simulation over the host log. The engine keeps a replay time `t`
(never beyond the log head, §11.3) and advances it through the finite set of times at which
anything can change: each log entry's time, and the timers derived from the current state
(window close + G, park times, expiry, evidence and escalation deadlines, lease lapses,
auto-revert, `window.closing` emissions, §16.6 quiescence). At each time τ, in this order:

1. lease lapses due at or before τ, then done by quiescence due at τ (`TaskBoard.advance`, §16.6);
2. the log entry at τ, if any (messages verified at "tip at received time" use the tip
   before any link decided at τ);
3. settle: evaluate every candidate of the tip at τ, open/decide escalations, and walk the
   race; repeat while a link happens at τ (a new link's candidates may qualify at once);
4. `window.closing` emissions due at τ.

A decision at τ uses only messages with log time ≤ τ, so feeding the same log in any batch
split, or restarting from the saved state, gives the same chain and the same events.

ChainDelta
----------
Every `feed`/`advance` returns a `ChainDelta`:

- `new_links`: links decided in this step, in seq order.
- `changed_proposals`: the current `ProposalState` of every candidate or push whose state
  changed (status, reason, window, votes, ...). A candidate that linked is reported once with
  status `qualified` and `qualified_at = linked_at`, then leaves `Chain.proposals`. Dead,
  orphan and stale pushes are reported by push id.
- `changed_tasks`, `changed_claims`, `changed_escalations`: current states of those that
  changed.
- `red`: the new `RedState` when the red/green state changed, else None.
- `events`: every §19.2 event derived in the step, sorted by (at, EVENT_TYPES order, id).
  Event data always carries the event-id key fields (SPEC §19.2).
- `next_wakeup`: the earliest time > t at which a time-only step can change anything (it can
  only be reached once the log head passes it).
- `missing_mail`: logged ids at or after the processing position whose raw mail is missing.
"""

from __future__ import annotations

import bisect
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from . import envelope as E
from .config import MEMBERS, ConfigError, Members, RepoConfig, RepoRules, TestPolicy, bare_address, glob_match, rules_of
from .envelope import EnvelopeError, jcs, make_event, sha256_hex
from .hostlog import HostLog, LogError, decode_entry, encode_entry, replay_time
from .objects import BlameMap, MemoryStore, ObjectError, Tree
from .ops import (
    ChangeOp, OpError, apply_ops, blob_refs, diff_trees, is_text, records_to_ops, split_lines, three_way,
    touched_paths, update_blame, genesis_blame, invert_ops,
)
from .rules import (
    Answer, ConsentInputs, ConsentPlan, EvidenceVerdict, OpenPush, ParentView, Vote, consent_plan,
    decide_escalation, detect_escalations, escalation_id, evaluate_consent, first_verdict, is_confirmed, live,
)
from .tasks import Activity, ClaimInput, NoChangeInput, TaskBoard, TaskInput, fix_task_id, scope_matches
from .types import (
    DEAD_STATUSES, EVENT_TYPES, VOTER_ROLES, ZERO, Chain, ChainDelta, Email, EscalationState,
    Event, Evidence, Handle, Hex, LinkState, LogEntry, Mode, Ms, ObjectionState, ObjectStoreLike,
    ProposalState, RedState, Seen,
)


class ReplayError(ValueError):
    """Replay stopped (for example at a log entry that fails verification). `.delta` holds the
    ChainDelta of the good prefix when raised by `Replayer.feed`."""

    def __init__(self, message: str, delta: ChainDelta | None = None) -> None:
        super().__init__(message)
        self.delta = delta


_PUSH_ROLES = frozenset({"agent", "human", "console", "tester"})
_TASK_ROLES = frozenset({"agent", "human", "console"})
_CLAIM_ROLES = frozenset({"agent", "human"})
_TYPE_ORDER = {t: i for i, t in enumerate(EVENT_TYPES)}
_PUNCT = ".,!?;:\"'`()[]{}*_"
_TAG_RE = re.compile(r"\[gud:([0-9a-f]{12})\]")
_BIG = 1 << 40
_STATE_VERSION = 1


# ---------------------------------------------------------------- pure building blocks

def carried_state(push: Hex, onto: Hex) -> Hex:
    """§14.4."""
    return sha256_hex(("gud-carry/1\n" + push + "\n" + onto + "\n").encode("utf-8"))


def test_required(policy: TestPolicy, files: Sequence[str]) -> bool:
    """§15.1: does a candidate touching `files` need a vouching pass under `policy`?"""
    if not policy.command:
        return False
    files = list(files)
    if files == [MEMBERS]:
        return False
    if policy.every_link:
        return True
    return any(glob_match(g, p) for g in policy.on_paths for p in files)


test_required.__test__ = False  # type: ignore[attr-defined]


def human_verb(text: str) -> tuple[Literal["approve", "accept", "object", "veto"], str] | None:
    """§8.3: the first word decides (case-insensitive, punctuation stripped)."""
    stripped = (text or "").lstrip()
    if not stripped:
        return None
    parts = stripped.split(None, 1)
    word = parts[0].strip(_PUNCT).lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if word in ("approve", "accept", "object", "veto"):
        return word, rest  # type: ignore[return-value]
    return None


def _norm_mid(value: str | None) -> str:
    return (value or "").strip()


def resolve_unsigned_target(email: Email, by_message_id: Mapping[str, Hex], ids: Sequence[Hex] | Mapping[str, Any],
                            notify_target: Mapping[Hex, Hex]) -> Hex | None:
    """§8.2: In-Reply-To, then References last to first, then the last [gud:<12>] subject tag.

    `ids` is the logged gud ids (a sequence), or a mapping from 12-hex prefixes to lists of ids."""
    target: Hex | None = None
    for mid in [email.in_reply_to] + list(reversed(email.references)):
        key = _norm_mid(mid)
        if key and key in by_message_id:
            target = by_message_id[key]
            break
    if target is None:
        tags = _TAG_RE.findall(email.subject or "")
        if tags:
            prefix = tags[-1]
            if isinstance(ids, Mapping):
                matches = list(ids.get(prefix, ()))
            else:
                matches = [i for i in ids if i.startswith(prefix)]
            if len(matches) == 1:
                target = matches[0]
    if target is not None and target in notify_target:
        target = notify_target[target]
    return target


def red_state(links: Sequence[LinkState], verdicts: Mapping[Hex, tuple[Literal["pass", "fail"], Ms]],
              at: Ms) -> RedState:
    """§15.4 from tip verdicts (link state -> (result, received)) visible at `at`."""
    seq_of = {link.state: link.seq for link in links}
    by_seq = {link.seq: link for link in links}
    items = sorted((t, seq_of[s], r) for s, (r, t) in verdicts.items() if s in seq_of and t <= at)
    known: dict[int, str] = {}
    red, red_link, red_since = False, None, None
    for t, seq, r in items:
        known[seq] = r
        top = max(known)
        now_red = known[top] == "fail"
        if now_red and not red:
            red, red_link, red_since = True, by_seq[top].state, t
        elif not now_red and red:
            red, red_link, red_since = False, None, None
    greens = [s for s, r in known.items() if r == "pass"]
    last_green = by_seq[max(greens)].state if greens else None
    first_bad = None
    if red and known:
        top = max(known)
        if greens:
            nxt = max(greens) + 1
            if known.get(nxt) == "fail" and nxt <= top:
                first_bad = by_seq[nxt].state
        elif known.get(0) == "fail":
            first_bad = by_seq[0].state
    return RedState(red, red_link, red_since, last_green, first_bad, fix_task_id(red_link) if red_link else None)


# ---------------------------------------------------------------- snapshot

@dataclass(frozen=True)
class Snapshot:
    """§17 snapshot; to_bytes() is RFC 8785 JSON."""
    version: int
    log_head: Seen
    t: Ms
    state: dict

    def to_bytes(self) -> bytes:
        return jcs({"version": self.version, "log_head": self.log_head.render(), "t": self.t, "state": self.state})

    @classmethod
    def from_bytes(cls, data: bytes) -> Snapshot:
        try:
            doc = json.loads(data.decode("utf-8"))
            head = E.parse_seen(doc["log_head"])
            return cls(int(doc["version"]), head, int(doc["t"]), doc["state"])
        except (ValueError, KeyError, TypeError, EnvelopeError) as exc:
            raise ReplayError(f"bad snapshot: {exc}") from exc

    def hash(self) -> Hex:
        return sha256_hex(self.to_bytes())


# ---------------------------------------------------------------- email (de)serialisation

def _email_json(email: Email, store: ObjectStoreLike) -> bytes:
    doc = {
        "gud_id": email.gud_id, "raw": store.put(email.raw_mime), "message_id": email.message_id,
        "from_addr": email.from_addr, "to": list(email.to), "cc": list(email.cc), "subject": email.subject,
        "in_reply_to": email.in_reply_to, "references": list(email.references),
        "auth_results": list(email.auth_results), "text": email.text,
        "env": store.put(E.encode(email.envelope)) if email.envelope is not None else None,
        "provider_id": email.provider_id,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _email_from_json(data: bytes, store: ObjectStoreLike) -> Email:
    doc = json.loads(data.decode("utf-8"))
    env = E.decode(store.get(doc["env"])) if doc["env"] else None
    return Email(
        gud_id=doc["gud_id"], raw_mime=store.get(doc["raw"]), message_id=doc["message_id"],
        from_addr=doc["from_addr"], to=tuple(doc["to"]), cc=tuple(doc["cc"]), subject=doc["subject"],
        in_reply_to=doc["in_reply_to"], references=tuple(doc["references"]),
        auth_results=tuple(doc["auth_results"]), text=doc["text"], envelope=env,
        provider_id=doc["provider_id"],
    )


def _email_ok(email: Email) -> bool:
    """§11.4: the message's gud id must be what the log names."""
    try:
        if email.envelope is not None:
            return E.gud_id(email.envelope) == email.gud_id
        return E.unsigned_id(email.raw_mime) == email.gud_id
    except (EnvelopeError, ValueError, TypeError):
        return False


def _link_json(link: LinkState) -> dict:
    return dict(link.__dict__) | {"files": list(link.files)}


def _link_from(d: Mapping) -> LinkState:
    return LinkState(**(dict(d) | {"files": tuple(d["files"])}))


def _domain(address: str) -> str:
    return bare_address(address).rpartition("@")[2]


class _Blobs:
    """BlobSource over the engine's completed blobs, as visible at time `at`."""

    def __init__(self, engine: Replayer, at: Ms) -> None:
        self.engine, self.at = engine, at

    def blob(self, sha: Hex) -> bytes | None:
        b = self.engine._s["blobs"].get(sha)
        if b is None or b["done"] is None or b["done"] > self.at:
            return None
        return self.engine._store.get(sha)


# ---------------------------------------------------------------- the replayer

class Replayer:
    """Persisted incremental replay. One per (repo, member). Not thread-safe; callers serialise."""

    def __init__(self, store: ObjectStoreLike, *, state_dir: Path | None = None,
                 trusted_host_keys: Sequence[str] = ()) -> None:
        self._store = store
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._trusted = tuple(trusted_host_keys)
        self._log = HostLog(trusted_keys=self._trusted)
        self._mail_keys: dict[Hex, Hex] = {}       # gud id -> store key of the email json (local)
        self._mail: dict[Hex, Email] = {}          # held / not yet processed mail
        self._s: dict[str, Any] = self._fresh_state()
        self._links: list[LinkState] = []
        self._link_index: dict[Hex, int] = {}
        self._link_times: list[Ms] = []
        self._board = TaskBoard(self._cfg_at, self._members_at)
        self._last_error: str | None = None
        self._init_caches()

    # ------------------------------------------------------------ state

    @staticmethod
    def _fresh_state() -> dict[str, Any]:
        return {
            "t": None, "next": 0, "pushes": {}, "negotiations": {}, "cands": [], "cand_info": {},
            "orphans": {}, "votes": {}, "comments": {}, "push_tests": {}, "stack_tests": {}, "ev_tests": {},
            "verdicts": {}, "red": None, "escalations": {}, "blobs": {}, "last_touch": {}, "seen": {},
            "msgid": {}, "notify": {}, "checkpoints": [], "closing_sent": [], "thread": {}, "joins": [],
            "authors": {}, "objections": {},
        }

    def _init_caches(self) -> None:
        self._rules_cache: dict[Hex, RepoRules] = {}
        self._verify_cache: dict[tuple[Hex, str], bool] = {}
        self._ops_cache: dict[Hex, tuple[ChangeOp, ...]] = {}
        self._env_cache: dict[Hex, E.Envelope] = {}
        self._evals: dict[Hex, dict] = {}
        self._last_props: dict[Hex, ProposalState] = {}
        self._prefix: dict[str, list[Hex]] = {}
        for gid in self._s["seen"]:
            self._prefix.setdefault(gid[:12], []).append(gid)
        self._closing = set(self._s["closing_sent"])
        self._events: list[Event] = []
        self._touched_props: set[Hex] = set()
        self._changed_esc: set[Hex] = set()
        self._red_changed = False
        self._new_links: list[LinkState] = []
        self._linked_cands: dict[Hex, ProposalState] = {}
        self._checkpoint_head: Seen | None = getattr(self, "_checkpoint_head", None)

    def _engine_json(self) -> dict:
        s = dict(self._s)
        s["closing_sent"] = sorted(self._closing)
        s["links"] = [_link_json(link) for link in self._links]
        s["board"] = self._board.to_json()
        s["version"] = _STATE_VERSION
        return s

    def _load_engine(self, s: dict) -> None:
        s = dict(s)
        links = [_link_from(d) for d in s.pop("links")]
        board = s.pop("board")
        s.pop("version", None)
        s.pop("tip", None)
        self._s = s
        self._links = links
        self._link_index = {link.state: link.seq for link in links}
        self._link_times = [link.linked_at for link in links]
        self._board = TaskBoard.from_json(board, self._cfg_at, self._members_at)
        self._init_caches()
        self._recompute_props()
        self._last_props = dict(self._current_props())

    # ------------------------------------------------------------ constructors / persistence

    @classmethod
    def load(cls, state_dir: Path, store: ObjectStoreLike) -> Replayer:
        state_dir = Path(state_dir)
        doc = json.loads((state_dir / "state.json").read_text("utf-8"))
        r = cls(store, state_dir=state_dir, trusted_host_keys=doc["local"]["trusted"])
        entries = [decode_entry(text.encode("utf-8")) for text in doc["local"]["log"]]
        r._log = HostLog(entries, trusted_keys=r._trusted)
        r._mail_keys = dict(doc["local"]["mail"])
        r._load_engine(doc["engine"])
        head = doc["local"].get("checkpoint_head")
        r._checkpoint_head = E.parse_seen(head) if head else None
        for gid in doc["local"]["held"]:
            key = r._mail_keys.get(gid)
            if key:
                r._mail[gid] = _email_from_json(store.get(key), store)
        return r

    @classmethod
    def from_checkpoint(cls, snapshot: bytes, store: ObjectStoreLike, *, state_dir: Path | None = None,
                        trusted_host_keys: Sequence[str] = ()) -> Replayer:
        """Resume from a §17 snapshot. Feed the full log (entries are small and never truncated);
        entries at or before the snapshot's head are checked against it and not re-processed, and
        their mail is not needed. Tree and blame nodes named by the snapshot must be in `store`."""
        snap = Snapshot.from_bytes(snapshot)
        if snap.version != 1:
            raise ReplayError(f"unsupported snapshot version {snap.version}")
        r = cls(store, state_dir=state_dir, trusted_host_keys=trusted_host_keys)
        r._load_engine(snap.state)
        r._checkpoint_head = snap.log_head
        for link in r._links:
            if not store.has(link.tree_root) and link.tree_root != Tree.empty(store).root:
                raise ReplayError(f"store lacks tree {link.tree_root[:12]} of link {link.seq}")
        return r

    def save(self) -> None:
        """Atomic write of state_dir/state.json (engine state, log, mail index)."""
        if self._state_dir is None:
            raise ReplayError("no state_dir")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        for gid, email in self._mail.items():
            if gid not in self._mail_keys:
                self._mail_keys[gid] = self._store.put(_email_json(email, self._store))
        doc = {
            "engine": self._engine_json(),
            "local": {
                "log": [encode_entry(e).decode("utf-8") for e in self._log.entries()],
                "mail": sorted(self._mail_keys.items()),
                "held": sorted(self._mail),
                "trusted": list(self._trusted),
                "checkpoint_head": self._checkpoint_head.render() if self._checkpoint_head else None,
            },
        }
        data = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
        path = self._state_dir / "state.json"
        tmp = path.with_name(f".state.{os.getpid()}.tmp")
        tmp.write_text(data, "utf-8")
        os.replace(tmp, path)
        (self._state_dir / "head").write_text(self._head().render() + "\n", "utf-8")

    def snapshot(self) -> Snapshot:
        """§17: the state after the last processed log entry, at the current replay time."""
        state = self._engine_json()
        state["tip"] = self._tip_section()
        return Snapshot(1, self._processed_head(), self._s["t"] if self._s["t"] is not None else 0, state)

    def _tip_section(self) -> dict:
        tip = self.tip
        if tip is None:
            return {"entries": [], "blame": [], "rules": None}
        tree = self._tree(tip.tree_root)
        blame = BlameMap.load(self._store, tip.blame_root)
        view = self._parent_view(tip)
        t = self._s["t"] or tip.linked_at
        runs = []
        for path in blame.paths():
            out = []
            for run in blame.get(path):
                owner = run.owner if _live(run.seq, view, t) else "-"
                out.append([owner, run.seq, run.length])
            runs.append([path, out])
        return {
            "entries": [[p, e.mode, e.blob, e.size] for p, e in tree.items()],
            "blame": runs,
            "rules": self._store.get(tip.rules_root).decode("utf-8"),
        }

    # ------------------------------------------------------------ accessors

    def log(self) -> HostLog:
        return self._log

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def tip(self) -> LinkState | None:
        return self._links[-1] if self._links else None

    def _head(self) -> Seen:
        head = self._log.head
        return Seen(head.seq, head.hash) if head else Seen(-1, ZERO)

    def _processed_head(self) -> Seen:
        n = self._s["next"]
        if n == 0:
            return Seen(-1, ZERO)
        if n - 1 >= len(self._log):
            # resumed from a checkpoint and not yet fed the log up to its head: the snapshot's
            # head is the processed head (the entries before it are checked when they arrive)
            if self._checkpoint_head is not None:
                return self._checkpoint_head
            raise ReplayError(f"replay state is at log entry {n - 1} but the log has {len(self._log)} entries")
        entry = self._log.at(n - 1)
        return Seen(entry.seq, entry.hash)

    def by_message_id(self, message_id: str) -> Hex | None:
        """The gud id of the first processed message with this Message-ID (for human references)."""
        return self._s["msgid"].get(_norm_mid(message_id))

    def wanted_mail(self) -> tuple[Hex, ...]:
        out = []
        for seq, gid, _ in self._log.mail_ids(self._s["next"]):
            if gid not in self._mail and gid not in self._s["seen"]:
                out.append(gid)
        return tuple(dict.fromkeys(out))

    def chain(self) -> Chain:
        return Chain(
            t=self._s["t"] if self._s["t"] is not None else 0,
            log_head=self._processed_head(),
            links=tuple(self._links),
            proposals=self._current_props(),
            tasks=self._board.tasks(),
            claims=self._board.claims(),
            escalations={k: self._esc_state(k) for k in self._s["escalations"]},
            red=self._red(),
        )

    def reader(self) -> ChainReader:
        return ChainReader(self.chain(), self._store, index=self)

    # ------------------------------------------------------------ feeding

    def feed(self, emails: Iterable[Email], log_entries: Sequence[LogEntry], now: Ms) -> ChainDelta:
        """Verify+append log entries, index emails by gud id (unlogged mail is held), then advance."""
        for email in emails:
            gid = email.gud_id
            if gid in self._mail or gid in self._s["seen"] or not _email_ok(email):
                continue
            self._mail[gid] = email
        error = None
        try:
            self._log.extend(list(log_entries))
        except LogError as exc:
            error = str(exc)
            self._last_error = error
        delta = self.advance(now)
        if error is not None:
            raise ReplayError(f"host log verification failed: {error}", delta)
        return delta

    def advance(self, now: Ms) -> ChainDelta:
        """Time-only step (window closes, lapses, deadlines) up to replay_time(now)."""
        t_before = self._s["t"] if self._s["t"] is not None else 0
        n_before = len(self._links)
        self._begin_step()
        checkpoint_head = self._checkpoint_head
        if checkpoint_head is not None and len(self._log) > checkpoint_head.seq >= 0:
            if self._log.at(checkpoint_head.seq).hash != checkpoint_head.entry_hash:
                raise ReplayError("log does not match the checkpoint head")
            self._checkpoint_head = None
        missing = self._first_missing()
        t = replay_time(self._log, now, missing)
        if self._s["t"] is not None:
            t = max(t, self._s["t"])
        if len(self._log):
            self._run(t)
        return self._end_step(t_before, n_before)

    def _first_missing(self) -> int | None:
        for entry in self._log.entries(self._s["next"]):
            if entry.type == "mail":
                gid = entry.fields[0][1]
                if gid not in self._mail and gid not in self._s["seen"]:
                    return entry.seq
        return None

    # ------------------------------------------------------------ step bookkeeping

    def _begin_step(self) -> None:
        self._events = []
        self._touched_props = set()
        self._changed_esc = set()
        self._red_changed = False
        self._new_links = []

    def _end_step(self, t_before: Ms, n_before: int) -> ChainDelta:
        props = self._current_props()
        changed: list[ProposalState] = []
        keys = set(self._touched_props) | set(props) | set(self._last_props)
        for key in sorted(keys):
            cur = props.get(key)
            if cur is None:
                self._last_props.pop(key, None)
                continue
            if self._last_props.get(key) != cur:
                changed.append(cur)
                self._last_props[key] = cur
        changed += [self._linked_cands[k] for k in sorted(self._linked_cands)]
        self._linked_cands = {}
        live_ids = {c["id"] for c in self._s["cands"]}
        for key in [k for k in self._evals if k not in live_ids]:
            del self._evals[key]
        task_ids, claim_ids = self._board.take_changes()
        tasks = self._board.tasks() if task_ids else {}
        events = sorted(self._events, key=lambda e: (e.at, _TYPE_ORDER.get(e.type, 99), e.id))
        return ChainDelta(
            t_before=t_before,
            t_after=self._s["t"] if self._s["t"] is not None else 0,
            log_head=self._processed_head(),
            new_links=tuple(self._new_links),
            changed_proposals=tuple(changed),
            changed_tasks=tuple(tasks[i] for i in task_ids if i in tasks),
            changed_claims=tuple(c for c in (self._board.claim(i) for i in claim_ids) if c is not None),
            changed_escalations=tuple(self._esc_state(k) for k in sorted(self._changed_esc)),
            red=self._red() if self._red_changed else None,
            events=tuple(events),
            next_wakeup=self._next_timer(),
            missing_mail=self.wanted_mail(),
        )

    def _emit(self, type: str, at: Ms, concerns: Iterable[Handle | None], data: Mapping[str, Any],
              causer: Handle | None = None) -> None:
        who = {h for h in concerns if h and h != causer}
        self._events.append(make_event(type, at, who, data))

    def _emit_all(self, events: Iterable[Event]) -> None:
        self._events.extend(events)

    # ------------------------------------------------------------ the clock

    def _run(self, t: Ms) -> None:
        while True:
            entry_time = None
            n = self._s["next"]
            if n < len(self._log):
                entry = self._log.at(n)
                if entry.type != "mail" or entry.fields[0][1] in self._mail or entry.fields[0][1] in self._s["seen"]:
                    entry_time = entry.time
            timer = self._next_timer()
            options = [x for x in (entry_time, timer) if x is not None and x <= t]
            if not options:
                break
            tau = min(options)
            self._s["t"] = tau
            self._emit_all(self._board.advance(tau))
            if entry_time == tau:
                self._process_entry(self._log.at(n), tau)
                self._s["next"] = n + 1
            self._settle(tau)
        if self._s["t"] is None or self._s["t"] < t:
            self._s["t"] = t

    def _next_timer(self) -> Ms | None:
        now = self._s["t"]
        if now is None:
            return None
        times: list[Ms] = []
        for c in self._s["cands"]:
            ev = self._evals.get(c["id"])
            if ev is None:
                continue
            for key in ("next", "park", "expire", "closing"):
                value = ev.get(key)
                if value is not None and value > now:
                    times.append(value)
        for esc in self._s["escalations"].values():
            if esc["status"] == "open" and esc["deadline"] > now:
                times.append(esc["deadline"])
        lapse = self._board.next_time(now)                 # lease lapses and §16.6 quiescence
        if lapse is not None and lapse > now:
            times.append(lapse)
        return min(times) if times else None

    # ------------------------------------------------------------ links and rules lookups

    def _tree(self, root: Hex) -> Tree:
        return Tree(self._store, root)

    def _rules_of_root(self, root: Hex) -> RepoRules:
        rules = self._rules_cache.get(root)
        if rules is None:
            rules = RepoRules.decode(self._store.get(root))
            if len(self._rules_cache) > 4096:
                self._rules_cache.clear()
            self._rules_cache[root] = rules
        return rules

    def _rules(self, link: LinkState) -> RepoRules:
        return self._rules_of_root(link.rules_root)

    def _link(self, state: Hex | None) -> LinkState | None:
        if state is None:
            return None
        seq = self._link_index.get(state)
        return None if seq is None else self._links[seq]

    def _tip_at(self, at: Ms) -> LinkState | None:
        i = bisect.bisect_right(self._link_times, at)
        return self._links[i - 1] if i else None

    def _cfg_at(self, at: Ms) -> RepoConfig:
        tip = self._tip_at(at)
        return self._rules(tip).config if tip else RepoConfig()

    def _members_at(self, at: Ms) -> Members:
        tip = self._tip_at(at)
        return self._rules(tip).members if tip else Members({})

    def _members_now(self) -> Members:
        return self._rules(self.tip).members if self.tip else Members({})

    def _parent_view(self, link: LinkState) -> ParentView:
        return ParentView(link.seq, link.linked_at, self._tree(link.tree_root),
                          BlameMap(self._store, link.blame_root), self._rules(link),
                          lambda seq: self._links[seq].linked_at)

    def _role_handles(self, role: str) -> list[Handle]:
        members = self._members_now()
        return [h for h, m in sorted(members.by_handle.items()) if m.role == role]

    # ------------------------------------------------------------ envelopes and signatures

    def _env(self, key: Hex) -> E.Envelope:
        env = self._env_cache.get(key)
        if env is None:
            env = E.decode(self._store.get(key))
            if len(self._env_cache) > 4096:
                self._env_cache.clear()
            self._env_cache[key] = env
        return env

    def _verified(self, gid: Hex, env_key: Hex, public_key: str | None) -> bool:
        if not public_key:
            return False
        ck = (gid, public_key)
        ok = self._verify_cache.get(ck)
        if ok is None:
            ok = E.verify(self._env(env_key), public_key)
            if len(self._verify_cache) > 200_000:
                self._verify_cache.clear()
            self._verify_cache[ck] = ok
        return ok

    def _signer(self, gid: Hex, env_key: Hex, author: Handle | None, members: Members,
                roles: frozenset[str]) -> Handle | None:
        """The verified author if they are a member with an allowed role, else None (§9)."""
        if not author:
            return None
        m = members.get(author)
        if m is None or m.role not in roles:
            return None
        return author if self._verified(gid, env_key, m.public_key) else None

    def _unsigned_human(self, msg: Mapping, rules: RepoRules) -> Handle | None:
        """§8.1 (2)+(3) at the relevant state: DMARC pass under the pinned authserv-id, From a human."""
        authserv = rules.config.authserv_id
        if not authserv:
            return None
        if not E.dmarc_pass(msg["auth"], authserv, _domain(msg["from"])):
            return None
        m = rules.members.by_address(msg["from"])
        return m.handle if m is not None and m.role == "human" else None

    # ------------------------------------------------------------ log entries

    def _process_entry(self, entry: LogEntry, tau: Ms) -> None:
        if entry.type != "mail":
            return
        gid = entry.fields[0][1]
        if gid in self._s["seen"]:
            return
        email = self._mail.pop(gid, None)
        if email is None:
            return
        self._s["seen"][gid] = entry.seq
        self._prefix.setdefault(gid[:12], []).append(gid)
        self._mail_keys.setdefault(gid, self._store.put(_email_json(email, self._store)))
        mid = _norm_mid(email.message_id)
        if email.envelope is None:
            if mid and mid in self._s["msgid"]:
                return   # §4: two unsigned messages with one Message-ID count once
            if mid:
                self._s["msgid"][mid] = gid
            self._unsigned(email, entry.seq, tau)
            return
        if mid and mid not in self._s["msgid"]:
            self._s["msgid"][mid] = gid
        env = email.envelope
        kind = env.kind
        problem = E.validate_kind(env)
        env_key = self._store.put(E.encode(env))
        self._env_cache[env_key] = env
        if kind == "blob":
            if problem is None:
                self._blob(env, tau)
            return
        if kind in ("push", "revert"):
            self._push(gid, env, env_key, entry.seq, tau, problem)
            return
        if problem is not None:
            return
        if kind != "join" and not self._seen_ok(gid, env, entry.seq, tau):
            return
        handler = {
            "object": self._vote, "approve": self._vote, "hold": self._vote, "comment": self._comment,
            "test": self._test, "claim": self._claim, "task": self._task, "subtask": self._task,
            "no-change": self._no_change, "checkpoint": self._checkpoint, "notify": self._notify,
            "join": self._join,
        }.get(kind or "")
        if handler is not None:
            handler(gid, env, env_key, tau)

    def _seen_ok(self, gid: Hex, env: E.Envelope, seq: int, tau: Ms) -> bool:
        """§9 Gud-Seen check; a mismatch voids the message and raises host.fork."""
        try:
            seen = E.parse_seen(env.get("Gud-Seen") or "")
        except EnvelopeError:
            return False
        verdict = self._log.check_seen(seen, seq)
        if verdict == "ok":
            return True
        log_hash = self._log.at(seen.seq).hash if seen.seq < len(self._log) else None
        members = self._members_now()
        everyone = set(members.by_handle) | {m.sponsor for m in members.by_handle.values() if m.sponsor}
        self._emit("host.fork", tau, everyone, {
            "message": gid, "cited": seen.render(), "log_hash": log_hash, "proof": None, "why": verdict,
        })
        return False

    # ------------------------------------------------------------ blobs (§6)

    def _blob(self, env: E.Envelope, tau: Ms) -> None:
        sha = env.get("Gud-Blob") or ""
        size = int(env.get("Gud-Size") or 0)
        chunk_size = int(env.get("Gud-Chunk-Size") or 0)
        i, _, n = (env.get("Gud-Chunk") or "").partition("/")
        index, count = int(i), int(n)
        b = self._s["blobs"].get(sha)
        if b is None:
            b = self._s["blobs"][sha] = {"size": size, "chunk": chunk_size, "n": count, "chunks": {}, "done": None}
        if (b["size"], b["chunk"], b["n"]) != (size, chunk_size, count) or b["done"] is not None:
            return
        key = str(index)
        if key in b["chunks"]:
            return
        b["chunks"][key] = self._store.put(env.records_named("data")[0].data)
        if len(b["chunks"]) == count:
            content = b"".join(self._store.get(b["chunks"][str(k)]) for k in range(count))
            if sha256_hex(content) == sha and len(content) == size:
                self._store.put(content)
                b["done"] = tau

    def _blobs_done(self, shas: Sequence[Hex], at: Ms) -> Ms | None:
        """Completion time of all blobs, or None if any is incomplete at `at`."""
        latest = 0
        for sha in shas:
            b = self._s["blobs"].get(sha)
            if b is None or b["done"] is None or b["done"] > at:
                return None
            latest = max(latest, b["done"])
        return latest

    # ------------------------------------------------------------ pushes (§12)

    def _push(self, gid: Hex, env: E.Envelope, env_key: Hex, seq: int, tau: Ms, problem: str | None) -> None:
        s = self._s
        if gid in s["pushes"]:
            return
        kind = env.kind
        author = env.author or ""
        p: dict[str, Any] = {
            "id": gid, "seq": seq, "at": tau, "author": author, "kind": kind, "parent": env.get("Gud-Parent") or ZERO,
            "task": env.get("Gud-Task"), "revises": None, "carry": env.get("Gud-Carry") != "none",
            "sent": env.sent_ms, "intent": env.text.split("\n", 1)[0] if env.text else "", "env": env_key,
            "files": [], "blobs": [], "revert": env.get("Gud-Revert"), "tree": env.get("Gud-Tree"),
            "status": "new", "reason": "", "dead_at": None, "native": None, "native_hash": None,
            "native_rules": None, "cands": [], "linked": None, "negotiation": gid, "counting": [],
            "blobs_at": None, "checked": False,
        }
        s["pushes"][gid] = p
        self._touched_props.add(gid)
        if problem is None:
            try:
                ops = records_to_ops(env.records) if kind == "push" else ()
            except OpError as exc:
                problem = str(exc)
            else:
                self._ops_cache[gid] = ops
                p["files"] = list(touched_paths(ops))
                p["blobs"] = list(blob_refs(ops))
        if problem is not None:
            self._kill(p, "malformed", problem, tau)
            return
        if not self._seen_ok(gid, env, seq, tau):
            self._kill(p, "bad_sig", "Gud-Seen does not match the host log", tau)
            return
        revised = s["pushes"].get(env.get("Gud-Revises") or "")
        if revised is not None and revised["author"] == author and revised["id"] != gid:
            p["revises"] = revised["id"]
            p["negotiation"] = revised["negotiation"]
        s["negotiations"].setdefault(p["negotiation"], []).append(gid)
        if kind == "revert":
            base = self._link(p["revert"])
            p["files"] = list(base.files) if base is not None else []
        tip = self.tip
        if p["task"] and tip is not None:
            m = self._members_now().get(author)
            if m is not None and self._verified(gid, env_key, m.public_key):
                self._emit_all(self._board.add_activity(Activity(author, p["task"], tau), push=gid))
        if p["parent"] == ZERO:
            if tip is None:
                self._try_genesis(p, env, tau)
            else:
                self._kill(p, "stale", "genesis already exists", tau)
            return
        if tip is None:
            self._orphan_or_stale(p, tau)
            return
        named = self._link(p["parent"])
        if named is None:
            self._orphan_or_stale(p, tau)
            if p["status"] == "orphan":
                self._received_event(p, None, tau)
            return
        self._enter(p, tau, fresh=True)

    def _received_event(self, p: Mapping, cand: Mapping | None, tau: Ms) -> None:
        plan = ConsentPlan.from_json(cand["plan"]) if cand and cand.get("plan") else None
        tip = self.tip
        needs_test = False
        if tip is not None:
            needs_test = test_required(self._rules(tip).test, p["files"])
        rules = sorted({r for _, r, _ in plan.declared} | ({"lazy"} if plan and plan.askers else set())) if plan else []
        concerns = set(self._role_handles("tester")) if needs_test else set()
        if p["task"]:
            concerns.update(self._board.holders(p["task"]))
        concerns.update(c.holder for c in self._board.claims_overlapping(p["files"], ()))
        self._emit("push.received", tau, concerns, {
            "push": p["id"], "parent": p["parent"], "author": p["author"], "task": p["task"], "kind": p["kind"],
            "files": list(p["files"]), "needs_test": needs_test,
            "forecast": {
                "line_owners": list(plan.line_owners) if plan else [],
                "declared": [[a, b, list(c)] for a, b, c in plan.declared] if plan else [],
                "rules": rules, "quorum_needed": plan.quorum_needed if plan else None,
            },
            "window_closes_at": cand["E"] + self._rules(tip).config.window if plan and plan.askers and tip else None,
        }, causer=p["author"])

    def _try_genesis(self, p: dict, env: E.Envelope, tau: Ms) -> None:
        """§12.2."""
        if p["kind"] != "push":
            self._kill(p, "malformed", "genesis must be a push", tau)
            return
        empty = Tree.empty(self._store)
        try:
            tree = apply_ops(empty, self._ops(p), _Blobs(self, tau))
        except OpError as exc:
            if p["blobs"] and self._blobs_done(p["blobs"], tau) is None:
                self._kill(p, "stale", "genesis blobs are not complete", tau)
            else:
                self._kill(p, "conflict", str(exc), tau)
            return
        if tree.hash() != p["tree"]:
            self._kill(p, "conflict", "tree hash differs from Gud-Tree", tau)
            return
        try:
            rules = rules_of(tree)
            host = rules.members.host()
        except ConfigError as exc:
            self._kill(p, "bad_config", str(exc), tau)
            return
        if host.public_key != self._log.key_at(p["seq"]):
            self._kill(p, "bad_sig", "host key differs from the log key", tau)
            return
        if self._signer(p["id"], p["env"], p["author"], rules.members, _PUSH_ROLES) is None:
            self._kill(p, "bad_sig", "signature does not verify", tau)
            return
        rules_root = self._store.put(rules.encode())
        link = LinkState(
            state=p["id"], seq=0, push=p["id"], kind="push", parent=None, named_parent=None, author=p["author"],
            tree_root=tree.root, tree_hash=tree.hash(), blame_root=genesis_blame(tree, p["author"]).root,
            rules_root=rules_root, received_at=tau, linked_at=tau, task=p["task"], intent=p["intent"],
            files=tuple(sorted(tree.paths(), key=lambda x: x.encode("utf-8"))),
        )
        p["status"], p["linked"] = "linked", p["id"]
        self._append_link(link, p, tau)

    def _ops(self, p: Mapping) -> tuple[ChangeOp, ...]:
        ops = self._ops_cache.get(p["id"])
        if ops is None:
            ops = records_to_ops(self._env(p["env"]).records) if p["kind"] == "push" else ()
            if len(self._ops_cache) > 4096:
                self._ops_cache.clear()
            self._ops_cache[p["id"]] = ops
        return ops

    def _kill(self, p: dict, status: str, reason: str, tau: Ms) -> None:
        if p["status"] in DEAD_STATUSES or p["status"] == "linked":
            return
        p["status"], p["reason"], p["dead_at"] = status, reason, tau
        self._touched_props.add(p["id"])
        for key in p["cands"]:
            self._touched_props.add(key)
        self._s["cands"] = [c for c in self._s["cands"] if c["push"] != p["id"]]
        if status != "superseded":
            self._emit("push.rejected", tau, (p["author"],), {
                "push": p["id"], "status": status, "reason": reason,
            })
        if p.get("task"):
            self._emit_all(self._board.on_push_end(p["id"], p["task"], tau))
        self._check_orphans(tau)

    def _orphan_or_stale(self, p: dict, tau: Ms) -> None:
        if self._possible(p["parent"]):
            p["status"] = "orphan"
            p["reason"] = f"parent {p['parent'][:12]} is not a link yet"
            self._s["orphans"].setdefault(p["parent"], []).append(p["id"])
        else:
            self._kill(p, "stale", "parent is not on the chain", tau)

    def _possible(self, state: Hex, depth: int = 0) -> bool:
        """Could a link with this state still appear (§12.4 orphan vs stale)?"""
        if depth > 64:
            return False
        if any(c["id"] == state for c in self._s["cands"]):
            return True
        q = self._s["pushes"].get(state)
        if q is not None and q["status"] == "orphan":
            return self._possible(q["parent"], depth + 1)
        return False

    def _check_orphans(self, tau: Ms) -> None:
        for parent in sorted(self._s["orphans"]):
            if parent in self._link_index or self._possible(parent):
                continue
            for pid in self._s["orphans"].pop(parent, []):
                p = self._s["pushes"][pid]
                if p["status"] == "orphan":
                    p["status"] = "new"
                    self._kill(p, "stale", "parent can no longer link", tau)

    # ------------------------------------------------------------ candidates

    def _intrinsic(self, p: dict, tau: Ms) -> bool:
        """§12.3 step 1 on the named parent (once). False if the push died or must wait for blobs."""
        if p["checked"]:
            return True
        named = self._link(p["parent"])
        if named is None:
            return False
        rules = self._rules(named)
        if self._signer(p["id"], p["env"], p["author"], rules.members, _PUSH_ROLES) is None:
            self._kill(p, "bad_sig", "author is not a member with a push role, or the signature fails", tau)
            return False
        done = self._blobs_done(p["blobs"], tau)
        if done is None:
            return False
        base = self._tree(named.tree_root)
        if p["kind"] == "push":
            try:
                tree = apply_ops(base, self._ops(p), _Blobs(self, tau))
            except OpError as exc:
                self._kill(p, "conflict", str(exc), tau)
                return False
        else:
            target = self._link(p["revert"])
            if target is None or target.parent is None or named.seq < target.seq:
                self._kill(p, "conflict", "Gud-Revert must name a non-genesis link at or before the parent", tau)
                return False
            before = self._tree(self._links[target.seq - 1].tree_root)
            tree, conflicts = invert_ops(before, self._tree(target.tree_root), base)
            if conflicts:
                self._kill(p, "conflict", "revert conflicts on " + ", ".join(conflicts), tau)
                return False
        if tree.hash() != p["tree"]:
            self._kill(p, "conflict", "tree hash differs from Gud-Tree", tau)
            return False
        try:
            rules_root = self._store.put(rules_of(tree).encode())
        except ConfigError as exc:
            self._kill(p, "bad_config", str(exc), tau)
            return False
        p["native"], p["native_hash"], p["native_rules"] = tree.root, tree.hash(), rules_root
        p["blobs_at"] = done
        p["checked"] = True
        return True

    def _enter(self, p: dict, tau: Ms, *, fresh: bool) -> None:
        """Put a push whose named parent is a link onto the tip: native or carried straight on."""
        tip = self.tip
        assert tip is not None
        named = self._link(p["parent"])
        assert named is not None
        rules = self._rules(named)
        if self._signer(p["id"], p["env"], p["author"], rules.members, _PUSH_ROLES) is None:
            self._kill(p, "bad_sig", "author is not a member with a push role, or the signature fails", tau)
            return
        p["status"] = "open"
        if named.state == tip.state:
            cand = self._make_cand(p, tip, carried=False, window_open=p["at"], prev=None, tau=tau)
        else:
            cand = self._carry(p, tip, window_open=p["at"], prev=None, tau=tau)
        if fresh and p["status"] not in DEAD_STATUSES:
            self._received_event(p, cand, tau)

    def _make_cand(self, p: dict, parent: LinkState, *, carried: bool, window_open: Ms,
                   prev: Mapping | None, tau: Ms, tree_root: Hex | None = None) -> dict | None:
        """Create the candidate of `p` on `parent` (the tip)."""
        members = self._rules(parent).members
        # Verify against the key at the candidate's parent, not only the named one, so a key
        # rotated (or a member removed and re-added) after the named parent cannot carry a push.
        if self._signer(p["id"], p["env"], p["author"], members, _PUSH_ROLES) is None:
            self._kill(p, "bad_sig", "author is not a member of the parent with a push role, "
                       "or the signature fails with their key there", tau)
            return None
        cid = carried_state(p["id"], parent.state) if carried else p["id"]
        cand: dict[str, Any] = {
            "id": cid, "push": p["id"], "parent": parent.state, "carried": carried, "E": window_open,
            "root": None, "hash": None, "rules": None, "plan": None, "created": tau,
            "prev_askers": list(prev["plan"]["askers"]) if prev and prev.get("plan") else None,
            "prev_E": prev["E"] if prev else None,
        }
        if not carried and self._intrinsic(p, tau):
            cand["root"], cand["hash"], cand["rules"] = p["native"], p["native_hash"], p["native_rules"]
        elif carried and tree_root is not None:
            cand["root"] = tree_root
        if p["status"] in DEAD_STATUSES:
            return None
        if cand["root"] is not None and carried:
            tree = self._tree(cand["root"])
            cand["hash"] = tree.hash()
            try:
                cand["rules"] = self._store.put(rules_of(tree).encode())
            except ConfigError:
                self._supersede(p, parent, tau, "the carried tree has an unparseable config file")
                return None
        if cand["root"] is not None:
            self._plan(cand, p, parent)
        if cid not in p["cands"]:
            p["cands"].append(cid)
        self._s["cand_info"][cid] = {"push": p["id"], "parent": parent.state, "root": cand["root"],
                                     "hash": cand["hash"]}
        self._s["cands"].append(cand)
        self._s["cands"].sort(key=lambda c: (self._s["pushes"][c["push"]]["seq"], c["push"]))
        self._touched_props.add(cid)
        if carried:
            self._emit("carried", tau, (p["author"],), {
                "push": p["id"], "candidate": cid, "onto": parent.state, "files": list(p["files"]),
            })
        self._review_events(cand, p, prev, tau)
        return cand

    def _plan(self, cand: dict, p: Mapping, parent: LinkState) -> None:
        """Consent plan on the parent; for carried candidates also fixes the window open (§14.3)."""
        view = self._parent_view(parent)
        after = self._tree(cand["root"])
        ops = self._cand_ops(p, cand, view.tree, after)
        plan = consent_plan(view, view.tree, after, ops, p["author"], cand["E"])
        if cand["carried"]:
            prev = cand["prev_askers"]
            if prev is None or not set(plan.askers) <= set(prev):
                new_e = max(p["at"], parent.linked_at)
                if new_e != cand["E"]:
                    cand["E"] = new_e
                    plan = consent_plan(view, view.tree, after, ops, p["author"], new_e)
        cand["plan"] = plan.to_json()
        self._s["cand_info"][cand["id"]] = {"push": p["id"], "parent": parent.state, "root": cand["root"],
                                            "hash": cand["hash"]}

    def _cand_ops(self, p: Mapping, cand: Mapping, before: Tree, after: Tree) -> tuple[ChangeOp, ...]:
        """Operations taking the parent tree to the candidate tree (for touched lines and blame)."""
        if p["kind"] == "push" and (not cand["carried"] or self._rules(self.tip).config.carry == "file"):
            return self._ops(p)
        return diff_trees(before, after, inline_budget=_BIG)

    def _carry(self, p: dict, onto: LinkState, *, window_open: Ms, prev: Mapping | None, tau: Ms) -> dict | None:
        """§14.1–14.3: carry live push `p` onto the new last link, or supersede it."""
        if not p["carry"]:
            self._supersede(p, onto, tau, "Gud-Carry: none")
            return None
        named = self._link(p["parent"])
        if named is None:
            self._supersede(p, onto, tau, "named parent is not a link")
            return None
        mode = self._rules(onto).config.carry
        touched = self._s["last_touch"]
        overlap = [f for f in p["files"] if touched.get(f, -1) > named.seq]
        ready = self._intrinsic(p, tau)
        if p["status"] in DEAD_STATUSES:
            return None
        if not ready:
            if overlap:
                self._supersede(p, onto, tau, "a later link touched " + ", ".join(overlap))
                return None
            return self._make_cand(p, onto, carried=True, window_open=window_open, prev=prev, tau=tau)
        root = self._derived_tree(p, named, onto, mode, overlap)
        if root is None:
            self._supersede(p, onto, tau, ("a later link touched " + ", ".join(overlap)) if overlap
                            else "the change does not apply to the new tip")
            return None
        return self._make_cand(p, onto, carried=True, window_open=window_open, prev=prev, tau=tau, tree_root=root)

    def _derived_tree(self, p: Mapping, named: LinkState, onto: LinkState, mode: str,
                      overlap: Sequence[str]) -> Hex | None:
        """§14.2 derived tree root, or None when not carryable."""
        native = self._tree(p["native"])
        tree = self._tree(onto.tree_root)
        if mode == "file" or not overlap:
            if overlap:
                return None
            try:
                for path in p["files"]:
                    entry = native.get(path)
                    if entry is None:
                        if path in tree:
                            tree = tree.delete(path)
                    else:
                        tree = tree.set_entry(path, entry)
            except (ObjectError, KeyError):
                return None
            return tree.root
        merged = three_way(self._tree(named.tree_root), native, tree, paths=p["files"])
        if merged.conflicts:
            return None
        return merged.tree.root

    def _supersede(self, p: dict, by: LinkState, tau: Ms, reason: str) -> None:
        if p["status"] in DEAD_STATUSES or p["status"] == "linked":
            return
        self._emit("superseded", tau, (p["author"],), {
            "push": p["id"], "by_link": by.state, "reason": reason,
        })
        self._kill(p, "superseded", f"superseded by {by.state[:12]}: {reason}", tau)

    def _review_events(self, cand: Mapping, p: Mapping, prev: Mapping | None, tau: Ms) -> None:
        if not cand.get("plan"):
            return
        plan = ConsentPlan.from_json(cand["plan"])
        old = set(prev["plan"]["askers"]) if prev and prev.get("plan") else set()
        why: dict[Handle, str] = {}
        for h in plan.line_owners:
            why[h] = "line"
        for h in plan.lazy_declared:
            why[h] = "lazy"
        for _, owners in plan.explicit:
            for h in owners:
                why[h] = "explicit"
        if plan.quorum_voters is not None:
            for h in plan.quorum_voters:
                why.setdefault(h, "quorum")
        if plan.members_rule:
            for h in plan.humans:
                why.setdefault(h, "members")
        close = cand["E"] + self._rules(self.tip).config.window
        for owner in sorted(why):
            if owner == p["author"] or owner in old:
                continue
            self._emit("review.requested", tau, (owner,), {
                "push": p["id"], "candidate": cand["id"], "owner": owner, "why": why[owner], "closes_at": close,
            })

    # ------------------------------------------------------------ linking (§12.4)

    def _append_link(self, link: LinkState, p: dict, tau: Ms) -> None:
        self._links.append(link)
        self._link_index[link.state] = link.seq
        self._link_times.append(link.linked_at)
        self._new_links.append(link)
        for path in link.files:
            self._s["last_touch"][path] = link.seq
        self._emit("linked", tau, self._linked_concerns(link), {
            "link": link.state, "push": link.push, "seq": link.seq, "author": link.author, "task": link.task,
            "files": list(link.files), "carried_from": link.carried_from, "kind": link.kind,
            "tree_hash": link.tree_hash,
        })
        tip_paths = list(self._tree(link.tree_root).paths()) if self._board.claims_overlapping(link.files, ()) else []
        self._emit_all(self._board.on_link(link, link.files, tip_paths))
        for pid in self._s["orphans"].pop(link.state, []):
            q = self._s["pushes"][pid]
            if q["status"] == "orphan":
                q["status"] = "new"
                self._enter(q, tau, fresh=False)
        self._check_orphans(tau)

    def _linked_concerns(self, link: LinkState) -> set[Handle]:
        # every agent: an agent with no claim and no live push still follows the tip (§19.2)
        out = {link.author} | set(self._role_handles("agent"))
        out.update(c.holder for c in self._board.claims_overlapping(link.files, ()))
        files = set(link.files)
        for c in self._s["cands"]:
            q = self._s["pushes"][c["push"]]
            if q["id"] != link.push and files & set(q["files"]):
                out.add(q["author"])
        return out

    def _link_cand(self, cand: dict, ev: Mapping, tau: Ms) -> None:
        p = self._s["pushes"][cand["push"]]
        parent = self.tip
        assert parent is not None
        before = self._tree(parent.tree_root)
        after = self._tree(cand["root"])
        ops = self._cand_ops(p, cand, before, after)
        seq = parent.seq + 1
        blame = update_blame(before, BlameMap(self._store, parent.blame_root), after, ops, p["author"], seq)
        link = LinkState(
            state=cand["id"], seq=seq, push=p["id"], kind=p["kind"], parent=parent.state,
            named_parent=p["parent"], author=p["author"], tree_root=cand["root"], tree_hash=cand["hash"],
            blame_root=blame.root, rules_root=cand["rules"], received_at=p["at"], linked_at=tau, task=p["task"],
            intent=p["intent"], files=tuple(p["files"]),
            carried_from=p["parent"] if cand["carried"] else None, revert_of=p["revert"],
        )
        prop = self._prop_for(cand, ev)
        self._linked_cands[cand["id"]] = replace(prop, status="qualified", qualified_at=tau,
                                                 reason=f"linked at seq {seq}")
        p["status"], p["linked"] = "linked", cand["id"]
        self._touched_props.add(p["id"])
        others = [c for c in self._s["cands"] if c is not cand]
        self._s["cands"] = []
        self._append_link(link, p, tau)
        for c in others:
            q = self._s["pushes"][c["push"]]
            if q["status"] in DEAD_STATUSES or q["status"] == "linked":
                continue
            self._touched_props.add(c["id"])
            self._carry(q, link, window_open=c["E"], prev=c, tau=tau)
        self._check_orphans(tau)

    # ------------------------------------------------------------ settle (§12.4)

    def _settle(self, tau: Ms) -> None:
        for _ in range(10_000):
            self._evaluate_all(tau)
            if self._update_escalations(tau):
                self._evaluate_all(tau)
            if not self._walk(tau):
                break
        self._closing_events(tau)

    def _evaluate_all(self, tau: Ms) -> None:
        for cand in list(self._s["cands"]):
            p = self._s["pushes"][cand["push"]]
            if p["status"] in DEAD_STATUSES:
                continue
            ev = self._evaluate(cand, p, tau)
            if ev["status"] in DEAD_STATUSES:
                if ev["status"] == "superseded":
                    continue
                self._kill(p, ev["status"], ev["reason"], tau)
                continue
            old = self._evals.get(cand["id"])
            self._evals[cand["id"]] = ev
            if old != ev:
                self._touched_props.add(cand["id"])

    def _walk(self, tau: Ms) -> bool:
        order = sorted(self._s["cands"], key=lambda c: (0 if self._evals.get(c["id"], {}).get("auto") else 1,
                                                       self._s["pushes"][c["push"]]["seq"]))
        for cand in order:
            ev = self._evals.get(cand["id"])
            if ev is None:
                continue
            if ev["status"] == "qualified":
                self._link_cand(cand, ev, tau)
                return True
            if ev["park"] is not None and tau >= ev["park"]:
                continue
            return False
        return False

    def _evaluate(self, cand: dict, p: dict, tau: Ms) -> dict:
        """§12.3 checks 2–9 for one candidate at τ."""
        parent = self.tip
        assert parent is not None
        rules = self._rules(parent)
        cfg = rules.config
        ev: dict[str, Any] = {"status": "window", "reason": "", "q": None, "park": None, "next": None,
                              "expire": p["at"] + cfg.expire, "closing": None, "close": None, "auto": False,
                              "test": None, "test_needed": False, "consent": None}
        if tau >= p["at"] + cfg.expire:
            return ev | {"status": "expired", "reason": "push expired"}
        votes = self._votes_for(p, parent, tau)
        for v in votes:
            if v.kind == "veto" and v.received <= tau:
                return ev | {"status": "vetoed", "reason": f"vetoed by {v.by}"}
        esc = self._escalation_for(p["negotiation"])
        if esc is not None and esc.status == "resolved" and p["negotiation"] in esc.declined \
                and esc.decided_at is not None and esc.decided_at <= tau:
            return ev | {"status": "declined", "reason": f"declined by escalation ({esc.decided_by})"}
        E_ = cand["E"]
        if cand["root"] is None:
            if self._blobs_done(p["blobs"], tau) is None:
                return ev | {"status": "pending_blobs", "reason": "waiting for blobs",
                             "park": E_ + cfg.window + cfg.grace, "close": E_ + cfg.window}
            if not self._complete_blobs(cand, p, tau):
                return ev | {"status": "superseded"}
        auto = self._auto_revert(cand, p, parent, tau)
        if auto is True:
            return ev | {"status": "qualified", "reason": "auto-revert", "q": tau, "auto": True}
        if isinstance(auto, int):
            ev["next"] = auto
        inputs = self._consent_inputs(cand, p, parent, votes, tau)
        consent = evaluate_consent(inputs, tau)
        self._note_counting(p, consent)
        close = consent.close
        ev["close"] = close
        ev["consent"] = consent
        plan = inputs.plan
        if plan.askers:
            ev["closing"] = max(close - min(10_000, cfg.window // 4), cand["created"])
        park = close + cfg.grace
        need = test_required(rules.test, p["files"])
        ev["test_needed"] = need
        vouch: Ms | None = None
        if need:
            first = self._first_push_test(cand, p, tau)
            if first is not None:
                if first["result"] != "pass" or first["tree"] != cand["hash"]:
                    return ev | {"status": "tests_failed", "reason": f"push test {first['id'][:12]} failed",
                                 "test": "fail"}
                vouch = first["at"]
            killed, stack_vouch = self._stack_verdict(cand, p, parent, tau)
            if killed:
                return ev | {"status": "tests_failed", "reason": "single-push stack test failed", "test": "fail"}
            if stack_vouch is not None and (vouch is None or stack_vouch < vouch):
                vouch = stack_vouch
            if vouch is None:
                park += rules.test.timeout
                status = "testing"
                return ev | {"status": status, "reason": "waiting for a vouching test pass", "park": park,
                             "next": _min(ev["next"], consent.next_change)}
            ev["test"] = "pass"
        if consent.holds_at is None:
            return ev | {"status": consent.status, "reason": consent.reason, "park": park,
                         "next": _min(ev["next"], consent.next_change)}
        q = max(consent.holds_at, vouch or 0, p["blobs_at"] or 0)
        return ev | {"status": "qualified", "reason": "qualified", "q": q, "park": None}

    def _first_push_test(self, cand: Mapping, p: Mapping, tau: Ms) -> dict | None:
        """§15.1: the first push-scope result for the candidate. A result naming the candidate id decides
        (any tree); for a carried candidate, a result naming the push id counts when its tree is the
        candidate's tree (§4, §14.4)."""
        exact = [t for t in self._s["push_tests"].get(cand["id"], ()) if t["at"] <= tau]
        if exact:
            return exact[0]
        if cand["id"] != p["id"]:
            for t in self._s["push_tests"].get(p["id"], ()):
                if t["at"] <= tau and t["tree"] == cand["hash"]:
                    return t
        return None

    def _complete_blobs(self, cand: dict, p: dict, tau: Ms) -> bool:
        """Blobs just completed: run the deferred intrinsic check and derive the candidate tree."""
        if not self._intrinsic(p, tau):
            return False
        parent = self.tip
        assert parent is not None
        if not cand["carried"]:
            cand["root"], cand["hash"], cand["rules"] = p["native"], p["native_hash"], p["native_rules"]
        else:
            named = self._link(p["parent"])
            assert named is not None
            overlap = [f for f in p["files"] if self._s["last_touch"].get(f, -1) > named.seq]
            root = self._derived_tree(p, named, parent, self._rules(parent).config.carry, overlap)
            if root is None:
                self._supersede(p, parent, tau, "the change does not apply to the new tip")
                return False
            tree = self._tree(root)
            try:
                rules_root = self._store.put(rules_of(tree).encode())
            except ConfigError:
                self._supersede(p, parent, tau, "the carried tree has an unparseable config file")
                return False
            cand["root"], cand["hash"], cand["rules"] = root, tree.hash(), rules_root
        self._plan(cand, p, parent)
        self._review_events(cand, p, {"plan": {"askers": cand["prev_askers"] or []}}, tau)
        return True

    def _auto_revert(self, cand: Mapping, p: Mapping, parent: LinkState, tau: Ms) -> bool | int | None:
        """§15.6: True if this revert qualifies now under auto-revert, an int timer if it will, else None."""
        if p["kind"] != "revert":
            return None
        policy = self._rules(parent).test
        red = self._red()
        if policy.auto_revert is None or not red.red or red.first_bad is None or p["revert"] != red.first_bad:
            return None
        due = (red.red_since or 0) + policy.auto_revert
        if tau >= due:
            return True
        return due

    # ------------------------------------------------------------ votes and consent inputs

    def _vote_records(self, p: Mapping) -> list[dict]:
        out = []
        for target in [p["id"]] + [c for c in p["cands"] if c != p["id"]]:
            out.extend(self._s["votes"].get(target, ()))
        return out

    def _vote_for(self, rec: Mapping, parent: LinkState) -> Vote | None:
        """Verify one vote record against members at the candidate's parent (§9, deviation 17)."""
        rules = self._rules(parent)
        evidence = None
        if rec.get("evidence"):
            ed = rec["evidence"]
            evidence = Evidence(ed["path"], ed["sha"], ed["size"], None)
        if rec["signed"]:
            m = rules.members.get(rec["by"])
            if m is None or m.role not in VOTER_ROLES:
                return None
            if not self._verified(rec["id"], rec["env"], m.public_key):
                return None
            kind = rec["kind"]
            if kind == "object" and m.role == "human" and _starts_with_veto(rec["reason"]):
                kind = "veto"
            return Vote(rec["id"], kind, rec["by"], rec["at"], rec["sent"], evidence, rec["reason"])
        handle = self._unsigned_human(rec, rules)
        if handle is None:
            return None
        return Vote(rec["id"], rec["kind"], handle, rec["at"], None, None, rec["reason"])

    def _votes_for(self, p: Mapping, parent: LinkState, tau: Ms) -> list[Vote]:
        out = []
        for rec in self._vote_records(p):
            if rec["at"] > tau:
                continue
            vote = self._vote_for(rec, parent)
            if vote is not None:
                out.append(vote)
        out.sort(key=lambda v: (v.received, v.id))
        return out

    def _answers_for(self, p: Mapping, objection_ids: Iterable[Hex]) -> list[Answer]:
        out = []
        author = p["author"]
        for target in [p["id"]] + list(p["cands"]):
            for cm in self._s["comments"].get(target, ()):
                if cm["by"] == author:
                    out.append(Answer(cm["id"], cm["at"], None))
        for oid in objection_ids:
            for cm in self._s["comments"].get(oid, ()):
                if cm["by"] == author:
                    out.append(Answer(cm["id"], cm["at"], oid))
        for qid in self._s["negotiations"].get(p["negotiation"], ()):
            q = self._s["pushes"][qid]
            if qid != p["id"] and q["revises"] is not None:
                out.append(Answer(qid, q["at"], None))
        return out

    def _verdicts_for(self, objection_ids: Iterable[Hex]) -> list[EvidenceVerdict]:
        out = []
        for oid in objection_ids:
            for t in self._s["ev_tests"].get(oid, ()):
                out.append(EvidenceVerdict(oid, t["on"], t["on_push"], t["base"], t["result"], t["at"], t["contains"]))
        return out

    def _inherited(self, p: Mapping, parent: LinkState, tau: Ms) -> list[ObjectionState]:
        """Confirmed, uncleared evidence objections on earlier pushes of the negotiation (§13.5)."""
        out = []
        members = self._rules(parent).members
        for qid in self._s["negotiations"].get(p["negotiation"], ()):
            q = self._s["pushes"][qid]
            if qid == p["id"] or q["seq"] > p["seq"]:
                continue
            for rec in self._vote_records(q):
                if rec["kind"] != "object" or not rec.get("evidence") or rec["at"] > tau:
                    continue
                if not rec["signed"] or members.get(rec["by"]) is None:
                    continue
                verdicts = self._verdicts_for([rec["id"]])
                first = first_verdict(verdicts, rec["id"], qid, tau)
                if first is None or not is_confirmed(first):
                    continue
                cleared = False
                for rid in self._s["negotiations"][p["negotiation"]]:
                    if rid in (p["id"], qid) or self._s["pushes"][rid]["seq"] > p["seq"]:
                        continue   # only a revision received before P clears it for P
                    fr = first_verdict(verdicts, rec["id"], rid, tau)
                    if fr is not None and fr.result == "pass" and fr.contains_file:
                        cleared = True
                if cleared:
                    continue
                ed = rec["evidence"]
                out.append(ObjectionState(rec["id"], rec["by"], rec["reason"], rec["at"],
                                          Evidence(ed["path"], ed["sha"], ed["size"], None), "confirmed", None, 0))
        return out

    def _consent_inputs(self, cand: Mapping, p: Mapping, parent: LinkState, votes: Sequence[Vote],
                        tau: Ms) -> ConsentInputs:
        plan = ConsentPlan.from_json(cand["plan"])
        objection_ids = [v.id for v in votes if v.kind == "object"]
        inherited = self._inherited(p, parent, tau)
        return ConsentInputs(
            plan=plan, author=p["author"], window_open=cand["E"], votes=votes,
            answers=self._answers_for(p, objection_ids),
            verdicts=self._verdicts_for(objection_ids + [o.id for o in inherited]),
            negotiation_objections=inherited, escalation=self._escalation_for(p["negotiation"]),
            cfg=self._rules(parent).config, push=p["id"], negotiation=p["negotiation"],
        )

    def _note_counting(self, p: dict, consent) -> None:
        """Remember which of the push's own objections ever counted (for episodes, §13.6)."""
        own = None
        for o in consent.objections:
            if o.status == "void" or o.id in p["counting"]:
                continue
            own = own if own is not None else self._own_objections(p)
            if o.id in own:
                p["counting"].append(o.id)

    def _own_objections(self, p: Mapping) -> set[Hex]:
        return {rec["id"] for rec in self._vote_records(p) if rec["kind"] == "object"}

    def _stack_verdict(self, cand: Mapping, p: Mapping, parent: LinkState, tau: Ms) -> tuple[bool, Ms | None]:
        """(killed by a single-push stack fail, earliest vouching stack pass) for a candidate (§15.3)."""
        killed = False
        vouch: Ms | None = None
        for t in self._s["stack_tests"].get(parent.state, ()):
            if t["at"] <= tau and t["pushes"] == [p["id"]] and t["result"] == "fail":
                killed = True
        # Walk back from the parent: links X+1..Y must be exactly P1..P(i-1).
        chain_pushes: list[Hex] = []
        link = parent
        for _ in range(64):
            for t in self._s["stack_tests"].get(link.state, ()):
                if t["at"] > tau or t["result"] != "pass":
                    continue
                i = len(chain_pushes)
                if t["pushes"][:i] == list(reversed(chain_pushes)) and i < len(t["pushes"]) and \
                        t["pushes"][i] == p["id"] and t["hashes"][i] == cand["hash"]:
                    if vouch is None or t["at"] < vouch:
                        vouch = t["at"]
            if link.parent is None:
                break
            chain_pushes.append(link.push)
            link = self._links[link.seq - 1]
        return killed, vouch

    # ------------------------------------------------------------ escalations (§13.6, §13.7)

    def _escalation_for(self, root: Hex) -> EscalationState | None:
        best = None
        for key, esc in self._s["escalations"].items():
            if root in esc["pushes"]:
                if best is None or (esc["opened_at"], key) > (best["opened_at"], best["id"]):
                    best = esc
        return None if best is None else self._esc_state(best["id"])

    def _esc_state(self, key: Hex) -> EscalationState:
        e = self._s["escalations"][key]
        return EscalationState(e["id"], e["reason"], tuple(e["pushes"]), e["opened_at"], e["deadline"],
                               tuple(e["deciders"]), e["status"], e["chosen"], tuple(e["declined"]),
                               e["decided_by"], e["decided_at"])

    def _episodes(self, root: Hex, parent: LinkState, tau: Ms) -> dict[Handle, int]:
        """§13.6 episode count per objector in negotiation `root`, at τ."""
        members = self._rules(parent).members
        pushes = [self._s["pushes"][q] for q in self._s["negotiations"].get(root, ())]
        objs = []
        for q in pushes:
            for rec in self._vote_records(q):
                if rec["kind"] != "object" or rec["at"] > tau or rec["id"] not in q["counting"]:
                    continue
                by = rec["by"] if rec["signed"] else (self._unsigned_human(rec, self._rules(parent)) or "")
                if not by or members.get(by) is None:
                    continue
                objs.append((rec["at"], rec["id"], by, q, rec))
        objs.sort(key=lambda x: (x[0], x[1]))
        counts: dict[Handle, int] = {}
        answered: dict[Handle, Ms | None] = {}
        for at, oid, by, q, rec in objs:
            if by not in counts:
                counts[by], answered[by] = 1, None
            elif answered[by] is not None and at > answered[by]:  # type: ignore[operator]
                counts[by] += 1
                answered[by] = None
            ans = [a.at for a in self._answers_for(q, [oid]) if a.at > at and a.at <= tau
                   and (a.answers is None or a.answers == oid)]
            if ans and answered[by] is None:
                answered[by] = min(ans)
            if rec.get("evidence"):
                verdicts = self._verdicts_for([oid])
                first = first_verdict(verdicts, oid, q["id"], tau)
                if first is not None and is_confirmed(first):
                    for r in pushes:
                        if r["seq"] <= q["seq"]:
                            continue
                        fr = first_verdict(verdicts, oid, r["id"], tau)
                        if fr is not None and fr.result == "fail":
                            counts[by] += 1
        return counts

    def _update_escalations(self, tau: Ms) -> bool:
        parent = self.tip
        if parent is None:
            return False
        changed = False
        opens = []
        for cand in self._s["cands"]:
            p = self._s["pushes"][cand["push"]]
            ev = self._evals.get(cand["id"])
            consent = ev.get("consent") if ev else None
            objections = consent.objections if consent is not None else ()
            opens.append(OpenPush(p["id"], p["negotiation"], p["author"], p["at"], p["task"], tuple(objections),
                                  self._episodes(p["negotiation"], parent, tau)))
        for reason, roots in detect_escalations(opens, tau):
            eid = escalation_id(reason, roots)
            if eid in self._s["escalations"]:
                continue
            if any(e["status"] == "open" and set(roots) & set(e["pushes"]) for e in self._s["escalations"].values()):
                continue
            self._open_escalation(eid, reason, roots, opens, tau)
            changed = True
        for key in sorted(self._s["escalations"]):
            e = self._s["escalations"][key]
            if e["status"] != "open":
                continue
            esc = self._esc_state(key)
            decided = decide_escalation(esc, self._decider_votes(esc, parent, tau),
                                        {r: self._evidence_uncleared(r, parent, tau) for r in esc.pushes},
                                        {r: self._s["pushes"][r]["at"] for r in esc.pushes if r in self._s["pushes"]},
                                        tau)
            if decided.status == "resolved":
                e.update(status="resolved", chosen=decided.chosen, declined=list(decided.declined),
                         decided_by=decided.decided_by, decided_at=decided.decided_at)
                self._changed_esc.add(key)
                changed = True
                self._emit("escalation.resolved", decided.decided_at or tau,
                           set(e["deciders"]) | set(e["authors"]) | set(e["objectors"]), {
                               "escalation": key, "chosen": decided.chosen, "declined": list(decided.declined),
                               "by": decided.decided_by, "reason": e["reason"],
                           })
        return changed

    def _open_escalation(self, eid: Hex, reason: str, roots: Sequence[Hex], opens: Sequence[OpenPush],
                         tau: Ms) -> None:
        members = self._members_now()
        deciders: set[Handle] = set()
        authors: set[Handle] = set()
        objectors: set[Handle] = set()
        positions = []
        for o in opens:
            if o.negotiation not in roots:
                continue
            authors.add(o.author)
            if o.task:
                req = self._board.requester_of(o.task)
                if req:
                    deciders.add(req)
            m = members.get(o.author)
            if m is not None and m.sponsor:
                deciders.add(m.sponsor)
            for ob in o.objections:
                if ob.status == "void":
                    continue
                objectors.add(ob.by)
                positions.append({"push": o.push, "objection": ob.id, "by": ob.by, "reason": ob.reason,
                                  "status": ob.status, "answered_by": ob.answered_by})
        if not deciders:
            deciders = {h for h in members.humans() if members.get(h) and members.get(h).votes}  # type: ignore[union-attr]
        cfg = self._rules(self.tip).config  # type: ignore[arg-type]
        deadline = tau + cfg.escalate
        rec = {"id": eid, "reason": reason, "pushes": sorted(roots), "opened_at": tau, "deadline": deadline,
               "deciders": sorted(deciders), "status": "open", "chosen": None, "declined": [], "decided_by": None,
               "decided_at": None, "authors": sorted(authors), "objectors": sorted(objectors)}
        self._s["escalations"][eid] = rec
        self._changed_esc.add(eid)
        if reason == "mutual":
            first = min(roots, key=lambda r: (self._s["pushes"][r]["at"], r))
            default = {"chosen": first}
        else:
            default = {"chosen": None if self._evidence_uncleared(roots[0], self.tip, tau) else roots[0]}  # type: ignore[arg-type]
        self._emit("escalated", tau, deciders | authors | objectors, {
            "escalation": eid, "reason": reason, "pushes": sorted(roots), "positions": positions,
            "deciders": sorted(deciders), "deadline": deadline, "default": default,
        })

    def _decider_votes(self, esc: EscalationState, parent: LinkState, tau: Ms) -> list[Vote]:
        out = []
        members = self._rules(parent).members
        for root in esc.pushes:
            for qid in self._s["negotiations"].get(root, ()):
                q = self._s["pushes"][qid]
                for rec in self._vote_records(q):
                    if rec["at"] > tau:
                        continue
                    if rec["signed"]:
                        m = members.get(rec["by"])
                        if m is None or not self._verified(rec["id"], rec["env"], m.public_key):
                            continue
                        by = rec["by"]
                        kind = rec["kind"]
                        if kind == "object" and m.role == "human" and _starts_with_veto(rec["reason"]):
                            kind = "veto"
                    else:
                        by = self._unsigned_human(rec, self._rules(parent)) or ""
                        kind = rec["kind"]
                        if not by:
                            continue
                    if kind == "hold":
                        continue
                    out.append(Vote(rec["id"], kind, by, rec["at"], rec["sent"], None, rec["reason"], target=root))
        return out

    def _evidence_uncleared(self, root: Hex, parent: LinkState | None, tau: Ms) -> bool:
        if parent is None:
            return False
        pushes = self._s["negotiations"].get(root, ())
        for qid in pushes:
            q = self._s["pushes"][qid]
            for rec in self._vote_records(q):
                if rec["kind"] != "object" or not rec.get("evidence") or rec["at"] > tau:
                    continue
                verdicts = self._verdicts_for([rec["id"]])
                first = first_verdict(verdicts, rec["id"], qid, tau)
                if first is None or not is_confirmed(first):
                    continue
                cleared = any((fr := first_verdict(verdicts, rec["id"], rid, tau)) is not None and
                              fr.result == "pass" and fr.contains_file for rid in pushes if rid != qid)
                if not cleared:
                    return True
        return False

    # ------------------------------------------------------------ window.closing

    def _closing_events(self, tau: Ms) -> None:
        for cand in self._s["cands"]:
            ev = self._evals.get(cand["id"])
            if not ev or ev.get("closing") is None or ev.get("close") is None:
                continue
            key = f"{cand['id']}:{ev['close']}"
            if key in self._closing or ev["closing"] > tau or ev["close"] <= tau:
                continue
            self._closing.add(key)
            p = self._s["pushes"][cand["push"]]
            plan = ConsentPlan.from_json(cand["plan"])
            consent = ev.get("consent")
            voted = {h for h, _ in consent.approvals} if consent else set()
            voted |= {o.by for o in consent.objections} if consent else set()
            self._emit("window.closing", tau, {p["author"]} | (set(plan.askers) - voted), {
                "candidate": cand["id"], "closes_at": ev["close"], "push": p["id"],
            })

    # ------------------------------------------------------------ other kinds

    def _vote(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        kind = env.kind
        target = env.get("Gud-Target") or ""
        author = env.author or ""
        if kind == "approve" and self._board.has(target):
            members = self._members_now()
            m = members.get(author)
            if m is not None and m.role == "human" and self._verified(gid, env_key, m.public_key):
                self._emit_all(self._board.on_requester_accept(target, author, tau))
            return
        evidence = None
        records = env.records_named("evidence")
        if kind == "object" and records:
            rec = records[0]
            path, sha, size = rec.args
            evidence = {"path": path, "sha": sha, "size": int(size), "inline": self._store.put(rec.data) if rec.data else None}
        reason = env.text
        vote = {"id": gid, "kind": kind, "by": author, "at": tau, "sent": env.sent_ms, "reason": reason,
                "evidence": evidence, "env": env_key, "signed": True, "target": target}
        self._s["votes"].setdefault(target, []).append(vote)
        if kind == "object":
            self._s["objections"][gid] = target
        self._vote_event(vote, target, tau)

    def _push_of(self, target: Hex) -> dict | None:
        p = self._s["pushes"].get(target)
        if p is None and target in self._s["cand_info"]:
            p = self._s["pushes"].get(self._s["cand_info"][target]["push"])
        return p

    def _vote_event(self, vote: Mapping, target: Hex, tau: Ms) -> None:
        p = self._push_of(target)
        if p is None:
            return
        self._touched_props.add(p["id"])
        for c in p["cands"]:
            self._touched_props.add(c)
        tip = self.tip
        if tip is None:
            return
        v = self._vote_for(vote, tip)
        if v is None:
            return
        self._thread_add(target, v.by)
        if v.kind in ("object", "veto"):
            concerns = {p["author"]}
            if p["task"]:
                concerns.update(self._board.holders(p["task"]))
            if vote.get("evidence"):
                concerns.update(self._role_handles("tester"))
            ed = vote.get("evidence")
            self._emit("objected", tau, concerns, {
                "push": p["id"], "objection": vote["id"], "by": v.by, "reason": v.reason,
                "evidence": {"path": ed["path"], "sha": ed["sha"]} if ed else None, "veto": v.kind == "veto",
                "target": target,
            }, causer=v.by)
        elif v.kind == "approve":
            self._emit("approved", tau, {p["author"]}, {"approve": vote["id"], "push": p["id"], "by": v.by,
                                                       "target": target}, causer=v.by)
        elif v.kind == "hold":
            cand = next((c for c in self._s["cands"] if c["push"] == p["id"]), None)
            cfg = self._rules(tip).config
            close = cand["E"] + 2 * cfg.window if cand else None
            self._emit("held", tau, {p["author"]}, {"hold": vote["id"], "push": p["id"], "by": v.by,
                                                   "closes_at": close, "target": target}, causer=v.by)

    def _thread_add(self, target: Hex, by: Handle) -> None:
        authors = self._s["thread"].setdefault(target, [])
        if by not in authors:
            authors.append(by)

    def _author_of(self, gid: Hex) -> Handle | None:
        p = self._push_of(gid)
        if p is not None:
            return p["author"]
        t = self._board.task(gid)
        if t is not None:
            return t.author
        c = self._board.claim(gid)
        if c is not None:
            return c.holder
        return self._s["authors"].get(gid)

    def _comment(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        author = env.author or ""
        members = self._members_now()
        m = members.get(author)
        if m is None or m.role == "host" or not self._verified(gid, env_key, m.public_key):
            return
        target = env.get("Gud-Target") or ""
        task = env.get("Gud-Task")
        self._s["comments"].setdefault(target, []).append({"id": gid, "by": author, "at": tau, "task": task})
        self._s["authors"][gid] = author
        if task:
            self._emit_all(self._board.add_activity(Activity(author, task, tau)))
        p = self._push_of(target)
        if p is not None:
            self._touched_props.add(p["id"])
        concerns = set(self._s["thread"].get(target, ())) | {self._author_of(target)}
        self._thread_add(target, author)
        self._emit("commented", tau, concerns, {
            "comment": gid, "target": target, "by": author, "text": env.text, "task": task,
            "escalate": env.get("Gud-Escalate"), "verdict": env.get("Gud-Verdict"),
        }, causer=author)
        if env.get("Gud-Escalate") and task:
            self._events.append(self._board.escalate(task, env.get("Gud-Escalate") or "", tau, author))

    def _test(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        tip = self.tip
        if tip is None:
            return
        tester = self._signer(gid, env_key, env.author, self._members_now(), frozenset({"tester"}))
        if tester is None:
            return
        scope = env.get("Gud-Scope")
        target = env.get("Gud-Target") or ""
        tree = env.get("Gud-Tree") or ""
        result = env.get("Gud-Result") or ""
        concerns: set[Handle | None] = set()
        if scope == "push":
            results = self._s["push_tests"].setdefault(target, [])
            if not any(t["tree"] == tree for t in results):        # first result per (target, tree)
                results.append({"id": gid, "result": result, "tree": tree, "at": tau})
            p = self._push_of(target)
            if p is not None:
                concerns.add(p["author"])
                self._touched_props.add(target)
        elif scope == "stack":
            base = self._link(target)
            pushes = list(env.get_list("Gud-Stack"))
            if base is None:
                return
            final, n, hashes = self._stack(base, pushes)
            if n != len(pushes) or final.hash() != tree:
                return
            self._s["stack_tests"].setdefault(base.state, []).append(
                {"id": gid, "pushes": pushes, "result": result, "at": tau, "hashes": hashes})
            for pid in pushes:
                q = self._s["pushes"].get(pid)
                if q is not None:
                    concerns.add(q["author"])
        elif scope == "tip":
            link = self._link(target)
            if link is None or link.tree_hash != tree or target in self._s["verdicts"]:
                return
            self._s["verdicts"][target] = [result, tau]
            concerns.add(link.author)
            self._update_red(tau)
        elif scope == "accept":
            link = self._link(env.get("Gud-Link"))
            if link is None or link.tree_hash != tree:
                return
            red = self._red().red
            self._emit_all(self._board.on_accept_test(target, link.state, result, tau, red))  # type: ignore[arg-type]
            concerns.update(self._board.holders(target))
            concerns.add(self._board.requester_of(target))
        elif scope == "evidence":
            if not self._evidence_test(gid, env, target, tree, result, tau, concerns):
                return
        self._emit("test.passed" if result == "pass" else "test.failed", tau, concerns, {
            "test": gid, "scope": scope, "target": target, "tree": tree, "tester": tester, "result": result,
            "log": env.text[-4096:], "stack": list(env.get_list("Gud-Stack")) or None,
        }, causer=tester)

    def _evidence_test(self, gid: Hex, env: E.Envelope, target: Hex, tree: str, result: str, tau: Ms,
                       concerns: set) -> bool:
        objection = None
        for rec in self._s["votes"].get(self._s["objections"].get(target, ""), ()):
            if rec["id"] == target:
                objection = rec
        on = env.get("Gud-On") or ""
        info = self._s["cand_info"].get(on)
        if objection is None or not objection.get("evidence") or info is None or info["root"] is None:
            return False
        ed = objection["evidence"]
        content = self._store.get(ed["inline"]) if ed.get("inline") else _Blobs(self, tau).blob(ed["sha"])
        if content is None or sha256_hex(content) != ed["sha"] or len(content) != ed["size"]:
            return False
        cand_tree = self._tree(info["root"])
        contains = False
        entry = cand_tree.get(ed["path"])
        if entry is not None and entry.blob == ed["sha"]:
            contains = True
        try:
            with_file = cand_tree.set(ed["path"], "100644", content) if not contains else cand_tree
        except ObjectError:
            return False
        if with_file.hash() != tree:
            return False
        objected = self._push_of(objection["target"])
        base = env.get("Gud-Base-Result")
        if objected is not None and info["push"] == objected["id"] and base is None:
            return False
        self._s["ev_tests"].setdefault(target, []).append({
            "id": gid, "on": on, "on_push": info["push"], "base": base, "result": result, "at": tau,
            "contains": contains,
        })
        if objected is not None:
            concerns.add(objected["author"])
            self._touched_props.add(objected["id"])
        concerns.add(objection["by"])
        q = self._s["pushes"].get(info["push"])
        if q is not None:
            self._touched_props.add(q["id"])
            concerns.add(q["author"])
        return True

    def _claim(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        holder = self._signer(gid, env_key, env.author, self._members_now(), _CLAIM_ROLES)
        if holder is None:
            return
        tip = self.tip
        tip_paths = list(self._tree(tip.tree_root).paths()) if tip else []
        task = env.get("Gud-Task") or ""
        done = env.get("Gud-Done") == "yes"
        live = done and any(q["author"] == holder and q["task"] == task and q["status"] not in DEAD_STATUSES
                            and q["status"] != "linked" for q in self._s["pushes"].values())
        self._emit_all(self._board.add_claim(ClaimInput(
            id=gid, author=holder, received=tau, task=task,
            files=tuple(env.get_list("Gud-Files")), revises=env.get("Gud-Revises"),
            release=env.get("Gud-Release") == "yes", after=tuple(env.get_list("Gud-After")),
            done=done, has_live_push=live,
        ), tip_paths))

    def _task(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        members = self._members_now()
        author = self._signer(gid, env_key, env.author, members, _TASK_ROLES)
        if author is None:
            return
        bug = env.get("Gud-Bug")
        if bug and not members.get(author).votes:  # type: ignore[union-attr]
            return
        requester = env.get("Gud-Requester")
        rm = members.get(requester) if requester else None
        if rm is None or rm.role != "human":
            am = members.get(author)
            if am is not None and am.role == "human":
                requester = author
            elif am is not None and am.sponsor and members.get(am.sponsor) is not None:
                requester = am.sponsor
            else:
                requester = None
        accept = env.records_named("accept")
        cmds = tuple(line for line in (accept[0].data.decode("utf-8").split("\n") if accept else []) if line.strip())
        kind = env.kind
        self._emit_all(self._board.add_task(TaskInput(
            id=gid, kind="subtask" if kind == "subtask" else "task", author=author, received=tau,
            title=env.get("Gud-Title") or "", description=env.text, requester=requester,
            parent=env.get("Gud-Parent-Task"), accept_paths=tuple(env.get_list("Gud-Accept")), accept_cmds=cmds,
            after=tuple(env.get_list("Gud-After")), files_hint=tuple(env.get_list("Gud-Files")),
            bug=bug if kind == "task" else None,
        )))

    def _no_change(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        author = self._signer(gid, env_key, env.author, self._members_now(), _CLAIM_ROLES)
        if author is None:
            return
        task = env.get("Gud-Task") or ""
        at_link = self._link(env.get("Gud-Tip"))
        claim = self._board.active_claim(task)
        paths_match = False
        if at_link is not None and claim is not None and self.tip is not None:
            a, b = self._tree(at_link.tree_root), self._tree(self.tip.tree_root)
            if "**" in claim.files:
                paths_match = a.hash() == b.hash()
            else:
                paths_match = not any(scope_matches(claim.files, path) for path in a.changed_paths(b))
        live = any(q["author"] == author and q["task"] == task and q["status"] not in DEAD_STATUSES
                   and q["status"] != "linked" for q in self._s["pushes"].values())
        self._emit_all(self._board.add_no_change(NoChangeInput(
            id=gid, author=author, received=tau, task=task, tip=env.get("Gud-Tip") or "", reason=env.text,
            paths_match=paths_match, has_live_push=live,
        )))

    def _checkpoint(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        link = self._link(env.get("Gud-Link"))
        if link is None:
            return
        by = self._signer(gid, env_key, env.author, self._rules(link).members, VOTER_ROLES)
        if by is None:
            return
        self._s["checkpoints"].append({"id": gid, "by": by, "log": env.get("Gud-Log"), "link": link.state,
                                       "tree": env.get("Gud-Tree"), "snapshot": env.get("Gud-Snapshot"), "at": tau})

    def valid_checkpoints(self) -> list[dict]:
        """§17: (log, link, snapshot) groups signed by more than half of the voters at the link."""
        groups: dict[tuple, set[Handle]] = {}
        for c in self._s["checkpoints"]:
            groups.setdefault((c["log"], c["link"], c["snapshot"]), set()).add(c["by"])
        out = []
        for (log, link, snap), signers in sorted(groups.items()):
            voters = self._rules(self._link(link)).members.voters()  # type: ignore[arg-type]
            if len(signers & set(voters)) * 2 > len(voters):
                out.append({"log": log, "link": link, "snapshot": snap, "signers": sorted(signers)})
        return out

    def _notify(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        if self._signer(gid, env_key, env.author, self._members_now(),
                        frozenset({"agent", "human", "tester", "console", "host"})) is None:
            return
        target = env.get("Gud-Target") or env.get("Gud-Task")
        if target:
            self._s["notify"][gid] = target

    def _join(self, gid: Hex, env: E.Envelope, env_key: Hex, tau: Ms) -> None:
        if E.verify(env, env.get("Gud-Key")):
            self._s["joins"].append(gid)

    def _unsigned(self, email: Email, seq: int, tau: Ms) -> None:
        """§8: unsigned human mail: votes on pushes, task acceptance, or a new task."""
        if not email.text:
            return
        target = resolve_unsigned_target(email, self._s["msgid"], self._prefix, self._s["notify"])
        verb = human_verb(email.text)
        msg = {"from": email.from_addr, "auth": list(email.auth_results)}
        tip = self.tip
        if target is None:
            if email.in_reply_to or tip is None:
                return
            handle = self._unsigned_human(msg, self._rules(tip))
            if handle is None:
                return
            title = _clean_subject(email.subject)
            self._emit_all(self._board.add_task(TaskInput(
                id=email.gud_id, kind="task", author=handle, received=tau, title=title, description=email.text,
                requester=handle, parent=None, accept_paths=(), accept_cmds=(), after=(), files_hint=(), bug=None,
            )))
            return
        if self._board.has(target):
            if verb is not None and verb[0] in ("accept", "approve") and tip is not None:
                handle = self._unsigned_human(msg, self._rules(tip))
                if handle is not None:
                    self._emit_all(self._board.on_requester_accept(target, handle, tau))
            return
        p = self._push_of(target)
        if p is not None and verb is not None and verb[0] in ("approve", "object", "veto"):
            vote = {"id": email.gud_id, "kind": verb[0], "by": "", "at": tau, "sent": None, "reason": verb[1],
                    "evidence": None, "env": None, "signed": False, "target": target, **msg}
            self._s["votes"].setdefault(target, []).append(vote)
            self._vote_event(vote, target, tau)

    # ------------------------------------------------------------ red tip (§15.4)

    def _red(self) -> RedState:
        r = self._s["red"]
        if r is None:
            return RedState(False, None, None, None, None, None)
        return RedState(r["red"], r["red_link"], r["red_since"], r["last_green"], r["first_bad"], r["fix_task"])

    def _update_red(self, tau: Ms) -> None:
        verdicts = {k: (v[0], v[1]) for k, v in self._s["verdicts"].items()}
        new = red_state(self._links, verdicts, tau)  # type: ignore[arg-type]
        old = self._red()
        if new == old:
            return
        self._s["red"] = dict(new.__dict__)
        self._red_changed = True
        agents_testers = set(self._role_handles("agent")) | set(self._role_handles("tester"))
        if new.red and not old.red:
            self._emit("tip.red", tau, agents_testers, {"red_link": new.red_link, "last_green": new.last_green})
        if new.red and new.first_bad is not None and (not old.red or old.first_bad != new.first_bad):
            bad = self._link(new.first_bad)
            self._emit("tip.bisected", tau, agents_testers | ({bad.author} if bad else set()), {
                "red_link": new.red_link, "first_bad": new.first_bad, "author": bad.author if bad else None,
                "fix_task": new.fix_task,
            })
        if old.red and not new.red:
            top = max((self._link_index[s] for s in self._s["verdicts"] if s in self._link_index), default=0)
            self._emit("tip.green", tau, agents_testers, {"link": self._links[top].state, "red_link": old.red_link})
            self._emit_all(self._board.on_green(tau))
        if new.red:
            cited = self._link(new.first_bad or new.red_link)
            self._emit_all(self._board.on_red(new, tau, cited.author if cited else None,
                                              list(cited.files) if cited else []))

    # ------------------------------------------------------------ stacks (§15.3)

    def _stack(self, base: LinkState, pushes: Sequence[Hex]) -> tuple[Tree, int, list[Hex]]:
        """Apply pushes in sequence on `base` by file-mode rules; (tree, how many applied, hash after each)."""
        tree = self._tree(base.tree_root)
        touched: set[str] = set()
        hashes: list[Hex] = []
        for pid in pushes:
            p = self._s["pushes"].get(pid)
            if p is None or not p["checked"]:
                break
            named = self._link(p["parent"])
            if named is None or named.seq > base.seq:
                break
            between = any(self._touched_between(f, named.seq, base.seq) for f in p["files"])
            if between or touched & set(p["files"]):
                break
            native = self._tree(p["native"])
            try:
                for path in p["files"]:
                    entry = native.get(path)
                    if entry is None:
                        if path in tree:
                            tree = tree.delete(path)
                    else:
                        tree = tree.set_entry(path, entry)
                rules_of(tree)
            except (ObjectError, KeyError, ConfigError):
                break
            touched.update(p["files"])
            hashes.append(tree.hash())
        return tree, len(hashes), hashes

    def _touched_between(self, path: str, after_seq: int, upto_seq: int) -> bool:
        for seq in range(after_seq + 1, upto_seq + 1):
            if path in self._links[seq].files:
                return True
        return False

    # ------------------------------------------------------------ proposals view

    def _prop_for(self, cand: Mapping, ev: Mapping | None) -> ProposalState:
        p = self._s["pushes"][cand["push"]]
        plan = ConsentPlan.from_json(cand["plan"]) if cand.get("plan") else None
        consent = ev.get("consent") if ev else None
        status = ev["status"] if ev else "window"
        reason = ev["reason"] if ev else ""
        park = ev.get("park") if ev else None
        if ev and status not in ("qualified",) and park is not None and self._s["t"] is not None \
                and self._s["t"] >= park:
            status, reason = "parked", f"parked ({ev['status']}): {reason}"
        episodes = self._episodes(p["negotiation"], self.tip, self._s["t"]) if consent and consent.objections \
            and self.tip else {}
        objections = tuple(replace(o, episode=episodes.get(o.by, 0)) for o in consent.objections) if consent else ()
        esc = self._escalation_for(p["negotiation"])
        return ProposalState(
            id=cand["id"], push=p["id"], named_parent=p["parent"], parent=cand["parent"], author=p["author"],
            kind=p["kind"], received_at=p["at"], status=status, reason=reason, task=p["task"],  # type: ignore[arg-type]
            revises=p["revises"], negotiation=p["negotiation"], files=tuple(p["files"]), tree_hash=cand["hash"],
            window_open=cand["E"], window_closes_at=ev.get("close") if ev else None, park_at=park,
            line_owners=plan.line_owners if plan else (), askers=plan.askers if plan else (),
            declared=plan.declared if plan else (), quorum_needed=plan.quorum_needed if plan else None,
            quorum_have=consent.quorum_have if consent else None, members_rule=plan.members_rule if plan else False,
            approvals=consent.approvals if consent else (), objections=objections,
            held_by=consent.held_by if consent else None, test_needed=bool(ev and ev.get("test_needed")),
            test_result=ev.get("test") if ev else None,  # type: ignore[arg-type]
            pending_blobs=tuple(sha for sha in p["blobs"] if self._blobs_done([sha], self._s["t"] or 0) is None),
            escalation=esc.id if esc else None, qualified_at=ev.get("q") if ev else None,
        )

    def _push_prop(self, p: Mapping) -> ProposalState:
        return ProposalState(
            id=p["id"], push=p["id"], named_parent=p["parent"], parent=None, author=p["author"],
            kind=p["kind"] if p["kind"] in ("push", "revert") else "push", received_at=p["at"],  # type: ignore[arg-type]
            status=p["status"] if p["status"] != "new" else "orphan", reason=p["reason"], task=p["task"],  # type: ignore[arg-type]
            revises=p["revises"], negotiation=p["negotiation"], files=tuple(p["files"]), tree_hash=p["native_hash"],
            window_open=None, window_closes_at=None, park_at=None, line_owners=(), askers=(), declared=(),
            quorum_needed=None, quorum_have=None, members_rule=False, approvals=(), objections=(), held_by=None,
            test_needed=False, test_result=None, pending_blobs=(), escalation=None, qualified_at=None,
        )

    def _current_props(self) -> dict[Hex, ProposalState]:
        out: dict[Hex, ProposalState] = {}
        for cand in self._s["cands"]:
            out[cand["id"]] = self._prop_for(cand, self._evals.get(cand["id"]))
        for pid, p in self._s["pushes"].items():
            if p["status"] in DEAD_STATUSES or p["status"] == "orphan":
                out[pid] = self._push_prop(p)
        return out

    def _recompute_props(self) -> None:
        t = self._s["t"]
        if t is None or self.tip is None:
            return
        for cand in self._s["cands"]:
            p = self._s["pushes"][cand["push"]]
            self._evals[cand["id"]] = self._evaluate(cand, p, t)


def _min(a: Ms | None, b: Ms | None) -> Ms | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _live(seq: int, view: ParentView, at: Ms) -> bool:
    return live(seq, view, at)


def _starts_with_veto(text: str) -> bool:
    verb = human_verb(text or "")
    return verb is not None and verb[0] == "veto"


def _clean_subject(subject: str) -> str:
    s = (subject or "").strip()
    while True:
        low = s.lower()
        if low.startswith("re:") or low.startswith("fwd:"):
            s = s.split(":", 1)[1].strip()
        elif low.startswith("[gud]"):
            s = s[5:].strip()
        else:
            break
    return s


# ---------------------------------------------------------------- reader

class ChainReader:
    """Accessors that need the object store. `index` (a Replayer) enables email, thread, candidate
    and ops lookups; without it those return None or empty results."""

    def __init__(self, chain: Chain, store: ObjectStoreLike, index: Replayer | None = None) -> None:
        self.chain = chain
        self.store = store
        self._index = index

    def _link(self, at: Hex | None) -> LinkState:
        link = self.chain.tip if at is None else self.chain.link(at)
        if link is None:
            raise ReplayError(f"no link {at}")
        return link

    def tree(self, at: Hex | None = None) -> Tree:
        return Tree(self.store, self._link(at).tree_root)

    def files(self, at: Hex | None = None) -> dict[str, tuple[Mode, bytes]]:
        return self.tree(at).to_files()

    def blame(self, path: str, at: Hex | None = None) -> list[tuple[Handle, int, bytes]]:
        link = self._link(at)
        tree = Tree(self.store, link.tree_root)
        entry = tree.get(path)
        if entry is None:
            return []
        data = tree.read(path)
        owners = list(BlameMap(self.store, link.blame_root).lines(path))
        if entry.mode != "120000" and is_text(data):
            return [(o, s, line) for (o, s), line in zip(owners, split_lines(data))]
        return [(owners[0][0], owners[0][1], data)] if owners else []

    def rules(self, at: Hex | None = None) -> RepoRules:
        return RepoRules.decode(self.store.get(self._link(at).rules_root))

    def members(self, at: Hex | None = None) -> Members:
        return self.rules(at).members

    def candidate_tree(self, candidate: Hex) -> Tree | None:
        if self._index is None:
            return None
        info = self._index._s["cand_info"].get(candidate)
        if info is None or info["root"] is None:
            return None
        return Tree(self.store, info["root"])

    def ops_of(self, push: Hex) -> tuple[ChangeOp, ...]:
        if self._index is None:
            return ()
        p = self._index._s["pushes"].get(push)
        return self._index._ops(p) if p is not None else ()

    def email(self, gud_id: Hex) -> Email | None:
        if self._index is None:
            return None
        if gud_id in self._index._mail:
            return self._index._mail[gud_id]
        key = self._index._mail_keys.get(gud_id)
        return _email_from_json(self.store.get(key), self.store) if key else None

    def thread(self, gud_id: Hex) -> list[Email]:
        """The message and every message targeting it (transitively), in log order."""
        if self._index is None:
            return []
        s = self._index._s
        targets: dict[Hex, list[Hex]] = {}
        for target, recs in list(s["votes"].items()) + list(s["comments"].items()):
            for rec in recs:
                targets.setdefault(target, []).append(rec["id"])
        for cid, info in s["cand_info"].items():
            if cid != info["push"]:
                targets.setdefault(info["push"], []).append(cid)
        found, stack = {gud_id}, [gud_id]
        while stack:
            cur = stack.pop()
            for child in targets.get(cur, ()):
                if child not in found:
                    found.add(child)
                    stack.append(child)
        ordered = sorted((s["seen"][g], g) for g in found if g in s["seen"])
        return [e for e in (self.email(g) for _, g in ordered) if e is not None]

    def tip_verdicts(self) -> dict[Hex, tuple[str, Ms]]:
        """Read-only: the counting §15.4 tip verdict per link state, `(result, received)` (needs `index`)."""
        if self._index is None:
            return {}
        return {k: (v[0], v[1]) for k, v in self._index._s["verdicts"].items()}

    def joins(self) -> tuple[Hex, ...]:
        """Read-only: ids of logged `join` requests whose self-signature verifies, in log order (needs `index`)."""
        if self._index is None:
            return ()
        return tuple(self._index._s["joins"])

    def forecast(self, parent: Hex, ops: Sequence[ChangeOp], author: Handle, at: Ms) -> ConsentPlan:
        """Owners and rules a change would need on `parent` if received at `at` (for agents before pushing)."""
        link = self._link(parent)
        base = Tree(self.store, link.tree_root)

        class _Store:
            def blob(_, sha: Hex) -> bytes | None:
                return self.store.get(sha) if self.store.has(sha) else None

        after = apply_ops(base, ops, _Store())
        view = ParentView(link.seq, link.linked_at, base, BlameMap(self.store, link.blame_root), self.rules(parent),
                          lambda seq: self.chain.links[seq].linked_at)
        return consent_plan(view, base, after, ops, author, at)


def carryable(parent_chain: Sequence[LinkState], push_ops: Sequence[ChangeOp], push_files: Sequence[str],
              mode: Literal["file", "line"], reader: ChainReader, named_parent: Hex, onto: Hex) -> Tree | None:
    """§14.1–14.2: the derived tree of a push (ops against `named_parent`) carried onto `onto`, or None."""
    by_state = {link.state: link for link in parent_chain}
    x, y = by_state.get(named_parent), by_state.get(onto)
    if x is None or y is None or y.seq < x.seq:
        return None
    files = set(push_files)
    overlap = any(files & set(link.files) for link in parent_chain if x.seq < link.seq <= y.seq)

    class _Store:
        def blob(_, sha: Hex) -> bytes | None:
            return reader.store.get(sha) if reader.store.has(sha) else None

    try:
        if mode == "file":
            if overlap:
                return None
            tree = apply_ops(reader.tree(onto), push_ops, _Store())
        else:
            native = apply_ops(reader.tree(named_parent), push_ops, _Store())
            merged = three_way(reader.tree(named_parent), native, reader.tree(onto), paths=push_files)
            if merged.conflicts:
                return None
            tree = merged.tree
        rules_of(tree)
    except (OpError, ConfigError, ObjectError):
        return None
    return tree


def stack_tree(reader: ChainReader, base: Hex, pushes: Sequence[Hex]) -> tuple[Tree, int]:
    """§15.3: (tree after applying as many pushes as carry by file-mode rules, how many applied)."""
    if reader._index is None:
        raise ReplayError("stack_tree needs a reader from Replayer.reader()")
    link = reader._index._link(base)
    if link is None:
        raise ReplayError(f"no link {base}")
    tree, n, _ = reader._index._stack(link, pushes)
    return tree, n


# ---------------------------------------------------------------- pure entry points

def _replayer(store: ObjectStoreLike | None, checkpoint: bytes | None, trusted: Sequence[str]) -> Replayer:
    store = store if store is not None else MemoryStore()
    if checkpoint is not None:
        return Replayer.from_checkpoint(checkpoint, store, trusted_host_keys=trusted)
    return Replayer(store, trusted_host_keys=trusted)


def replay(emails: Iterable[Email], log_entries: Sequence[LogEntry], now: Ms, *,
           store: ObjectStoreLike | None = None, checkpoint: bytes | None = None,
           trusted_host_keys: Sequence[str] = ()) -> Chain:
    """Reference full replay (§12–§16); a pure function of its arguments. Raises ReplayError when the
    host log fails verification."""
    r = _replayer(store, checkpoint, trusted_host_keys)
    r.feed(emails, log_entries, now)
    return r.chain()


def replay_events(emails: Iterable[Email], log_entries: Sequence[LogEntry], now: Ms, **kw) -> tuple[Event, ...]:
    r = _replayer(kw.get("store"), kw.get("checkpoint"), kw.get("trusted_host_keys", ()))
    return r.feed(emails, log_entries, now).events


def snapshot_at(entries: Sequence[LogEntry], mail: Callable[[Hex], Email | None], seq: int,
                store: ObjectStoreLike, trusted_host_keys: Sequence[str] = ()) -> bytes:
    """§17 snapshot bytes for log position `seq`: replay entries <= seq with now = time(seq).
    `mail(id)` returns each logged message. Raises ReplayError when an entry or its mail is missing."""
    upto = [e for e in entries if e.seq <= seq]
    if not upto or upto[-1].seq != seq:
        raise ReplayError(f"no log entry {seq}")
    emails = []
    for e in upto:
        if e.type == "mail":
            email = mail(e.fields[0][1])
            if email is None:
                raise ReplayError(f"mail {e.fields[0][1][:12]} of entry {e.seq} is not available")
            emails.append(email)
    r = Replayer(store, trusted_host_keys=trusted_host_keys)
    r.feed(emails, upto, upto[-1].time)
    return r.snapshot().to_bytes()
