"""The UI view API of the v1 host (`/api/v1/view/*`): read-only JSON views of the host's own replay,
an all-events stream for the dashboard, loopback-only demo actions, and the built UI at `/`.

Everything here is derived from the host's `Replayer` / `ChainReader`, its log, journals and
delivery registry. Nothing in it is chain state or protocol: members never read it, and two hosts
of one repo show the same views because they replay the same log (§18).

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/v1/view/state` | repo, replay time, log head, config and test policy at the tip, tip link, red state, counts, UI signers |
| GET | `/api/v1/view/chain?limit=&before=` | links newest first (author, intent, tree hash, carried-from, tip verdict, diffstat) |
| GET | `/api/v1/view/proposals?dead=` | each unlinked push's current candidate: status, consent plan, timers, objections, negotiation |
| GET | `/api/v1/view/tasks` | tasks and subtasks, claims with leases and overlaps, bug reports |
| GET | `/api/v1/view/escalations` | consent escalations with deadline and default outcome |
| GET | `/api/v1/view/members` | members at the tip (roles, short keys, sponsors, lines owned live/total, delivery), removed members, join requests |
| GET | `/api/v1/view/tree?at=` | files at a link (or a push's proposed tree) with live/expired ownership per owner |
| GET | `/api/v1/view/file?path=&at=` | one file's content (text, or a binary/symlink placeholder) |
| GET | `/api/v1/view/blame?path=&at=` | one owner per line with link, intent and live/expired ownership (§13.1) |
| GET | `/api/v1/view/diff/<id>` | a link's or proposal's change as render ops (patch/put/delete/rename/mode, binary placeholders) plus a unified diff |
| GET | `/api/v1/view/thread/<id>` | a message and everything answering it (for a push: its whole negotiation) |
| GET | `/api/v1/view/needs?member=` | what needs a member now (`gud.wc.review.needs`), default the UI's human |
| GET | `/api/v1/view/host?limit=` | host log head and tail, intake/dispatch stats, deliveries ("agents connected"), self-audit |
| GET | `/api/v1/view/events?after=&limit=` | the all-events feed (every published §19 event, in publish order) |
| GET | `/api/v1/view/stream?after=` (`Last-Event-ID`) | the same feed as SSE, plus `head` frames when the log grows |
| POST | `/api/v1/view/act/{task,vote,comment,admit}` | loopback only: sign as a configured member and log it |

Access is the mirror's (§21): loopback callers need no signature; anyone else signs as a member.
Ids in paths and queries may be full ids, hex prefixes of 4 or more, task aliases (`T3`) or `tip`.
"""

from __future__ import annotations

import base64
import difflib
import json
import mimetypes
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from gud.core import envelope as E
from gud.core.config import MEMBERS, ConfigError, Members, globs_overlap, glob_match, parse_members, render_members
from gud.core.objects import BlameMap, Tree
from gud.core.ops import ChangeOp, Delete, ModeOp, Patch, Put, Rename, diff_trees, is_text, make_patch, \
    ops_to_records, split_lines
from gud.core.replay import ChainReader
from gud.core.types import DEAD_STATUSES, LIVE_STATUSES, Chain, ClaimState, Email, Event, Handle, Hex, \
    LinkState, Member, ProposalState, Record, TaskState
from gud.events.dispatch import Journal
from gud.events.intake import Intake, received_now
from gud.events.model import EVENT_TYPES, event_dict, event_from_dict
from gud.ops.audit import audit
from gud.transport import mime
from gud.transport.base import Outgoing
from gud.wc.review import needs

__all__ = ["EventFeed", "load_signers", "add_view_routes", "add_static_routes", "default_ui_dist", "HostView"]

_TYPE_ORDER = {t: i for i, t in enumerate(EVENT_TYPES)}
TEXT_LIMIT = 2 * 1024 * 1024            # a file view returns at most this much text


# ---------------------------------------------------------------- the all-events feed

class EventFeed:
    """Every event the host publishes, once each, in publish order, numbered from 1 (the UI's
    stream; per-member journals only hold what concerns a member). Persisted as JSONL when given
    a path. On start, events already in member journals but not in the feed are backfilled in
    `(at, §19.2 type order, id)` order, so a host whose replay cache predates the feed still shows
    its history."""

    def __init__(self, path: Path | None, journal: Journal | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._items: list[Event] = []
        self._ids: set[Hex] = set()
        if self.path is not None and self.path.exists():
            for line in self.path.read_bytes().splitlines():
                try:
                    event = event_from_dict(json.loads(line))
                except (ValueError, KeyError, TypeError):
                    continue
                if event.id not in self._ids:
                    self._items.append(event)
                    self._ids.add(event.id)
        if journal is not None:
            missing: dict[Hex, Event] = {}
            for handle in journal.handles():
                after = 0
                while True:
                    page = journal.since(handle, after, 1000)
                    for item in page:
                        if item.event.id not in self._ids:
                            missing.setdefault(item.event.id, item.event)
                    if len(page) < 1000:
                        break
                    after = page[-1].cursor
            self._append(sorted(missing.values(), key=lambda e: (e.at, _TYPE_ORDER.get(e.type, 99), e.id)))

    def _append(self, events: Sequence[Event]) -> int:
        new = [e for e in events if e.id not in self._ids]
        if not new:
            return 0
        for e in new:
            self._items.append(e)
            self._ids.add(e.id)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as fh:
                fh.write(b"".join(E.jcs(event_dict(e)) + b"\n" for e in new))
        return len(new)

    def add(self, events: Sequence[Event]) -> int:
        with self._cond:
            n = self._append(list(events))
            if n:
                self._cond.notify_all()
            return n

    def head(self) -> int:
        with self._lock:
            return len(self._items)

    def since(self, after: int, limit: int = 500) -> list[tuple[int, Event]]:
        with self._lock:
            start = max(0, int(after))
            return [(start + i + 1, e) for i, e in enumerate(self._items[start:start + max(0, limit)])]

    def wait(self, after: int, timeout: float) -> bool:
        with self._cond:
            if len(self._items) > after:
                return True
            self._cond.wait(max(0.0, timeout))
            return len(self._items) > after


# ---------------------------------------------------------------- UI signers

def load_signers(homes: Iterable[Path | str]) -> dict[Handle, bytes]:
    """Private keys the UI may sign with: every `members/<handle>/key` (`ed25519:<b64 seed>`)
    under each member GUD_HOME given. The UI only uses those that are members at the tip."""
    out: dict[Handle, bytes] = {}
    for home in homes:
        root = Path(home).expanduser() / "members"
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/key")):
            handle = path.parent.name
            if not E.is_handle(handle) or handle in out:
                continue
            try:
                value = path.read_text("ascii").strip()
                raw = base64.b64decode(value.removeprefix("ed25519:"), validate=True)
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if len(raw) == 32:
                out[handle] = raw
    return out


def default_ui_dist() -> Path | None:
    """`$GUD_UI_DIST`, else `ui/dist` next to the `gud` package (a source checkout), if built."""
    import os

    env = os.environ.get("GUD_UI_DIST")
    candidates = [Path(env)] if env else []
    candidates.append(Path(__file__).resolve().parent.parent / "ui" / "dist")
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return None


# ---------------------------------------------------------------- helpers

def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _key_short(key: str | None) -> str | None:
    if not key:
        return None
    return key.removeprefix("ed25519:")[:12]


def _line_counts(old: bytes | None, new: bytes | None) -> tuple[int, int]:
    a = split_lines(old) if old else []
    b = split_lines(new) if new else []
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


class _StoreBlobs:
    def __init__(self, store) -> None:
        self.store = store

    def blob(self, sha: Hex) -> bytes | None:
        try:
            return self.store.get(sha) if self.store.has(sha) else None
        except (KeyError, OSError):
            return None


# ---------------------------------------------------------------- the views

class HostView:
    """Builds the view JSON from the host's replay. Every method takes the intake lock while it
    reads replay state (the replayer is not thread-safe); trees and blame come from the
    content-addressed store and are cached by their immutable ids."""

    def __init__(self, intake: Intake, *, repo: str, dispatcher=None, registry=None, feed: EventFeed | None = None,
                 bugs=None, signers: Mapping[Handle, bytes] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self.intake = intake
        self.repo = repo
        self.dispatcher = dispatcher
        self.registry = registry
        self.feed = feed
        self.bugs = bugs
        self.signers = dict(signers or {})
        self.clock = clock or intake.clock
        self._diffstat: dict[Hex, dict] = {}
        self._owners: dict[tuple[Hex, int, int], dict] = {}
        self._audit: tuple[int, dict] | None = None
        self._bugs_cache: tuple[float, list] | None = None

    # ------------------------------------------------------------ basics

    @property
    def replayer(self):
        return self.intake.replayer

    @property
    def store(self):
        store = getattr(self, "_store_cache", None)
        if store is None:
            store = self._store_cache = self.intake.replayer.reader().store
        return store

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _chain_reader(self) -> tuple[Chain, ChainReader]:
        chain = self.replayer.chain()
        return chain, ChainReader(chain, self.store, index=self.replayer)

    def resolve(self, ref: str | None, chain: Chain, *, kinds: Sequence[str] = ()) -> Hex:
        """A full id, a hex prefix (≥ 4) of a link, push, candidate, task, claim, escalation or
        logged message, a task alias (`T3`, `T3.1`) or `tip`. Raises HTTPException(404/400)."""
        ref = (ref or "").strip().strip("<>")
        if ref.startswith("[gud:") and ref.endswith("]"):
            ref = ref[5:-1]
        ref = ref.removeprefix("gud:")
        if not ref:
            raise HTTPException(400, "an id is required")
        if ref == "tip":
            if chain.tip is None:
                raise HTTPException(404, "no tip yet")
            return chain.tip.state
        for t in chain.tasks.values():
            if t.alias.lower() == ref.lower():
                return t.id
        low = ref.lower()
        if E.is_hex64(low):
            return low
        if len(low) < 4 or any(c not in "0123456789abcdef" for c in low):
            raise HTTPException(400, f"{ref!r} is not an id, a hex prefix of 4 or more, an alias or tip")
        pool: set[Hex] = set()
        for link in chain.links:
            pool.update((link.state, link.push))
        for key, prop in chain.proposals.items():
            pool.update((key, prop.push))
        pool.update(chain.tasks)
        pool.update(chain.claims)
        pool.update(chain.escalations)
        hits = {h for h in pool if h.startswith(low)}
        if not hits:
            hits = {gid for _, gid, _ in self.intake.log.mail_ids() if gid.startswith(low)}
        if len(hits) == 1:
            return hits.pop()
        if not hits:
            raise HTTPException(404, f"no id starts with {ref}")
        raise HTTPException(400, f"{ref} is ambiguous ({len(hits)} ids)")

    @staticmethod
    def _link_of(chain: Chain, gid: Hex) -> LinkState | None:
        link = chain.link(gid)
        if link is not None:
            return link
        for link in chain.links:
            if link.push == gid:
                return link
        return None

    @staticmethod
    def _alias(chain: Chain, task: Hex | None) -> str | None:
        if not task:
            return None
        t = chain.tasks.get(task)
        return t.alias if t is not None else None

    # ------------------------------------------------------------ state

    def state(self) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            tip = chain.tip
            rules = reader.rules() if tip is not None else None
            verdicts = reader.tip_verdicts()
            head = self.intake.head()
            red = self._red_json(chain, reader)
            live_props = [p for p in self._current_candidates(chain) if p.status in LIVE_STATUSES]
            tip_json = self._link_json(chain, reader, tip, verdicts) if tip is not None else None
        cfg = rules.config if rules is not None else None
        test = rules.test if rules is not None else None
        caps = self.intake.mailbox.capabilities()
        members = rules.members if rules is not None else None
        signers = []
        for handle in sorted(self.signers):
            m = members.get(handle) if members is not None else None
            if m is not None and m.role != "host":
                signers.append({"handle": handle, "role": m.role})
        human = next((s["handle"] for s in signers if s["role"] == "human"), None)
        console = next((s["handle"] for s in signers if s["role"] == "console"), None)
        return {
            "repo": self.repo,
            "bugs_address": getattr(self.bugs, "address", None),
            "host_key": self.intake.log.key,
            "transport": caps.name,
            "now": self.now_ms(),
            "t": chain.t,
            "log_head": {"seq": head.seq, "hash": head.hash, "time": head.time, "type": head.type},
            "processed": {"seq": chain.log_head.seq, "hash": chain.log_head.entry_hash},
            "config": None if cfg is None else {
                "window": cfg.window, "grace": cfg.grace, "skew": cfg.skew, "own_links": cfg.own_links,
                "own_time": cfg.own_time, "lease": cfg.lease, "escalate": cfg.escalate, "expire": cfg.expire,
                "carry": cfg.carry, "fix_offer": cfg.fix_offer, "lease_thrash": cfg.lease_thrash,
                "checkpoint": cfg.checkpoint, "authserv_id": cfg.authserv_id,
            },
            "test_policy": None if test is None else {
                "command": test.command, "image": test.image, "timeout": test.timeout, "on_paths": list(test.on_paths),
                "on_tip_links": test.on_tip_links, "on_tip_time": test.on_tip_time, "every_link": test.every_link,
                "stack_max": test.stack_max, "auto_revert": test.auto_revert,
            },
            "tip": tip_json,
            "red": red,
            "counts": {
                "links": len(chain.links),
                "proposals_live": len(live_props),
                "tasks_open": sum(1 for t in chain.tasks.values() if t.status != "done"),
                "claims_active": sum(1 for c in chain.claims.values() if c.status == "active"),
                "escalations_open": sum(1 for e in chain.escalations.values() if e.status == "open"),
            },
            "ui": {"signers": signers, "human": human, "console": console,
                   "actions": bool(signers)},
        }

    # ------------------------------------------------------------ links

    def _diffstat_of(self, chain: Chain, link: LinkState) -> dict:
        hit = self._diffstat.get(link.state)
        if hit is not None:
            return hit
        added = removed = 0
        binary: list[str] = []
        if link.parent is not None:
            parent = chain.link(link.parent)
            if parent is not None:
                old, new = Tree(self.store, parent.tree_root), Tree(self.store, link.tree_root)
                for path in old.changed_paths(new):
                    a, b = old.get(path), new.get(path)
                    da = old.read(path) if a is not None else None
                    db = new.read(path) if b is not None else None
                    if (da is not None and (not is_text(da) or a.mode == "120000")) or \
                            (db is not None and (not is_text(db) or b.mode == "120000")):
                        binary.append(path)
                        continue
                    x, y = _line_counts(da, db)
                    added += x
                    removed += y
        else:
            new = Tree(self.store, link.tree_root)
            for path, entry in new.items():
                data = new.read(path)
                if is_text(data) and entry.mode != "120000":
                    added += len(split_lines(data))
                else:
                    binary.append(path)
        out = {"added": added, "removed": removed, "files": list(link.files), "binary": binary}
        self._diffstat[link.state] = out
        return out

    def _link_json(self, chain: Chain, reader: ChainReader, link: LinkState, verdicts: Mapping[Hex, tuple]) -> dict:
        red = chain.red
        role = None
        if red.first_bad == link.state:
            role = "first_bad"
        elif red.red and red.red_link == link.state:
            role = "red_link"
        elif red.last_green == link.state:
            role = "last_green"
        verdict = verdicts.get(link.state)
        carried_seq = None
        if link.carried_from:
            src = chain.link(link.carried_from)
            carried_seq = src.seq if src is not None else None
        return {
            "seq": link.seq, "state": link.state, "push": link.push, "kind": link.kind, "author": link.author,
            "intent": link.intent, "title": _first_line(link.intent) or f"link {link.state[:12]}",
            "tree_hash": link.tree_hash, "parent": link.parent, "named_parent": link.named_parent,
            "carried_from": link.carried_from, "carried_from_seq": carried_seq, "revert_of": link.revert_of,
            "received_at": link.received_at, "linked_at": link.linked_at, "task": link.task,
            "task_alias": self._alias(chain, link.task), "files": list(link.files),
            "verdict": verdict[0] if verdict else None, "verdict_at": verdict[1] if verdict else None,
            "red_role": role, "diffstat": self._diffstat_of(chain, link),
        }

    def chain(self, limit: int = 200, before: int | None = None) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            verdicts = reader.tip_verdicts()
            links = [l for l in reversed(chain.links) if before is None or l.seq < before][:max(1, limit)]
            rows = [self._link_json(chain, reader, l, verdicts) for l in links]
        return {"links": rows, "total": len(chain.links),
                "next_before": rows[-1]["seq"] if rows and rows[-1]["seq"] > 0 else None}

    # ------------------------------------------------------------ red tip

    def _red_json(self, chain: Chain, reader: ChainReader) -> dict:
        red = chain.red

        def seq(state: Hex | None) -> int | None:
            link = chain.link(state) if state else None
            return link.seq if link is not None else None

        fb = chain.link(red.first_bad) if red.first_bad else None
        fix = chain.tasks.get(red.fix_task) if red.fix_task else None
        claim = chain.claims.get(fix.claim) if fix is not None and fix.claim else None
        return {
            "red": red.red, "red_link": red.red_link, "red_link_seq": seq(red.red_link), "red_since": red.red_since,
            "last_green": red.last_green, "last_green_seq": seq(red.last_green),
            "first_bad": red.first_bad, "first_bad_seq": fb.seq if fb else None,
            "first_bad_author": fb.author if fb else None, "first_bad_title": _first_line(fb.intent) if fb else None,
            "fix_task": None if fix is None else {
                "id": fix.id, "alias": fix.alias, "status": fix.status, "title": fix.title,
                "offered_to": fix.offered_to, "offer_until": fix.offer_until, "cited_link": fix.cited_link,
                "holder": claim.holder if claim is not None else None,
            },
        }

    # ------------------------------------------------------------ proposals

    @staticmethod
    def _current_candidates(chain: Chain) -> list[ProposalState]:
        """Each push's current candidate (§14.4), unlinked pushes only."""
        linked = {l.push for l in chain.links}
        best: dict[Hex, tuple[int, str, ProposalState]] = {}
        seqs = {l.state: l.seq for l in chain.links}
        for key in sorted(chain.proposals):
            prop = chain.proposals[key]
            if prop.push in linked:
                continue
            rank = seqs.get(prop.parent, -1) if prop.parent else -1
            cur = best.get(prop.push)
            if cur is None or (rank, prop.id) > (cur[0], cur[1]):
                best[prop.push] = (rank, prop.id, prop)
        return [v[2] for v in best.values()]

    def _proposal_json(self, chain: Chain, reader: ChainReader, prop: ProposalState, by_neg: Mapping[Hex, list],
                       race: Mapping[Hex, int], blocker: Hex | None, now: int) -> dict:
        parent = chain.link(prop.parent) if prop.parent else None
        named = chain.link(prop.named_parent) if prop.named_parent else None
        cfg = reader.rules(parent.state).config if parent is not None else (
            reader.rules().config if chain.tip is not None else None)
        email = reader.email(prop.push)
        env = email.envelope if email is not None else None
        intent = env.text if env is not None else ""
        voted = {h for h, _ in prop.approvals} | {o.by for o in prop.objections}
        objections = []
        for o in prop.objections:
            objections.append({
                "id": o.id, "by": o.by, "reason": o.reason, "received": o.received,
                "kind": "evidence" if o.evidence is not None else ("veto" if o.reason.lower().startswith("veto") else "reason"),
                "evidence": None if o.evidence is None else {"path": o.evidence.path, "sha256": o.evidence.sha256,
                                                             "size": o.evidence.size},
                "status": o.status, "answered_by": o.answered_by, "episode": o.episode,
                "blocking": o.status in ("blocking", "pending_verdict", "confirmed"),
            })
        grace_until = prop.window_closes_at + cfg.grace if (cfg is not None and prop.window_closes_at) else None
        revisions = []
        for other in sorted(by_neg.get(prop.negotiation, ()), key=lambda p: (p.received_at, p.push)):
            revisions.append({"push": other.push, "candidate": other.id, "status": other.status,
                              "received_at": other.received_at, "revises": other.revises,
                              "current": other.push == prop.push})
        links_in_neg = [l for l in chain.links if l.push in {r["push"] for r in revisions}]
        return {
            "id": prop.id, "push": prop.push, "kind": prop.kind, "author": prop.author,
            "title": _first_line(intent) or f"{prop.kind} {prop.push[:12]}", "intent": intent,
            "received_at": prop.received_at, "status": prop.status, "reason": prop.reason,
            "live": prop.status in LIVE_STATUSES, "dead": prop.status in DEAD_STATUSES,
            "parent": prop.parent, "parent_seq": parent.seq if parent is not None else None,
            "named_parent": prop.named_parent, "named_parent_seq": named.seq if named is not None else None,
            "carried": bool(prop.parent and prop.parent != prop.named_parent),
            "task": prop.task, "task_alias": self._alias(chain, prop.task), "revises": prop.revises,
            "negotiation": prop.negotiation, "files": list(prop.files), "tree_hash": prop.tree_hash,
            "timers": {
                "window_open": prop.window_open, "window_closes_at": prop.window_closes_at, "grace_until": grace_until,
                "park_at": prop.park_at, "expires_at": prop.received_at + cfg.expire if cfg is not None else None,
                "window": cfg.window if cfg is not None else None, "grace": cfg.grace if cfg is not None else None,
                "held": prop.held_by is not None, "held_by": prop.held_by, "qualified_at": prop.qualified_at,
            },
            "plan": {
                "line_owners": list(prop.line_owners), "askers": list(prop.askers),
                "declared": [{"path": p, "rule": r, "owners": list(o)} for p, r, o in prop.declared],
                "quorum_needed": prop.quorum_needed, "quorum_have": prop.quorum_have,
                "members_rule": prop.members_rule,
                "waiting_on": [h for h in prop.askers if h not in voted],
            },
            "approvals": [{"by": h, "at": at} for h, at in prop.approvals],
            "objections": objections,
            "rounds": max((o.episode for o in prop.objections), default=0),
            "revisions": revisions,
            "linked_revisions": [{"push": l.push, "seq": l.seq, "state": l.state} for l in links_in_neg],
            "test": {"needed": prop.test_needed, "result": prop.test_result},
            "pending_blobs": list(prop.pending_blobs), "escalation": prop.escalation,
            "race": race.get(prop.id), "blocks_tip": blocker == prop.id,
            "join": env.get("Gud-Join") if env is not None else None,
        }

    def proposals(self, dead: int = 50) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            now = self.now_ms()
            current = self._current_candidates(chain)
            by_neg: dict[Hex, list[ProposalState]] = {}
            for p in current:
                by_neg.setdefault(p.negotiation, []).append(p)
            tip = chain.tip.state if chain.tip is not None else None
            on_tip = sorted((p for p in current if p.parent == tip and p.status in LIVE_STATUSES),
                            key=lambda p: (p.received_at, p.id))
            race = {p.id: i for i, p in enumerate(on_tip)}
            blocker = next((p.id for p in on_tip if p.status not in ("parked", "qualified")), None)
            if blocker is not None and len(on_tip) < 2:
                blocker = None
            live = [p for p in current if p.status in LIVE_STATUSES]
            gone = sorted((p for p in current if p.status not in LIVE_STATUSES),
                          key=lambda p: (-p.received_at, p.id))[:max(0, dead)]
            rows = [self._proposal_json(chain, reader, p, by_neg, race, blocker, now)
                    for p in sorted(live, key=lambda p: (p.received_at, p.id)) + gone]
        return {"proposals": rows, "live": len(live), "dead_shown": len(gone)}

    # ------------------------------------------------------------ tasks and claims

    def _claim_json(self, chain: Chain, c: ClaimState, overlaps: Mapping[Hex, list]) -> dict:
        return {"id": c.id, "latest": c.latest, "task": c.task, "task_alias": self._alias(chain, c.task),
                "holder": c.holder, "files": list(c.files), "after": list(c.after), "won_at": c.won_at,
                "last_activity": c.last_activity, "lapses_at": c.lapses_at, "status": c.status,
                "overlaps": overlaps.get(c.id, [])}

    def tasks(self) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            tip_paths = list(reader.tree().paths()) if chain.tip is not None else []
            active = sorted((c for c in chain.claims.values() if c.status == "active"), key=lambda c: (c.won_at, c.id))
            overlaps: dict[Hex, list] = {}
            pairs = []
            for i, a in enumerate(active):
                for b in active[i + 1:]:
                    if a.holder == b.holder or not globs_overlap(list(a.files), list(b.files), tip_paths):
                        continue
                    shared = sorted(set(a.files) & set(b.files))
                    paths = [p for p in tip_paths if any(glob_match(g, p) for g in a.files if _ok_glob(g))
                             and any(glob_match(g, p) for g in b.files if _ok_glob(g))][:50]
                    pairs.append({"claims": [a.id, b.id], "holders": [a.holder, b.holder],
                                  "tasks": [a.task, b.task], "globs": shared, "paths": paths})
                    overlaps.setdefault(a.id, []).append(b.id)
                    overlaps.setdefault(b.id, []).append(a.id)
            links_by_task: dict[Hex, list] = {}
            for l in chain.links:
                if l.task:
                    links_by_task.setdefault(l.task, []).append({"seq": l.seq, "state": l.state, "author": l.author})
            claims_by_task: dict[Hex, list] = {}
            for c in sorted(chain.claims.values(), key=lambda c: (c.won_at, c.id)):
                claims_by_task.setdefault(c.task, []).append(self._claim_json(chain, c, overlaps))
            rows = []
            for t in sorted(chain.tasks.values(), key=lambda t: (t.opened_at, t.id)):
                rows.append(self._task_json(chain, t, claims_by_task, links_by_task))
            claims = [self._claim_json(chain, c, overlaps)
                      for c in sorted(chain.claims.values(), key=lambda c: (c.won_at, c.id))]
        return {"tasks": rows, "claims": claims, "overlaps": pairs, "bugs": self._bugs(chain)}

    def _task_json(self, chain: Chain, t: TaskState, claims_by_task, links_by_task) -> dict:
        cited = chain.link(t.cited_link) if t.cited_link else None
        active = chain.claims.get(t.claim) if t.claim else None
        return {
            "id": t.id, "alias": t.alias, "kind": t.kind, "title": t.title, "description": t.description,
            "author": t.author, "requester": t.requester, "parent": t.parent,
            "parent_alias": self._alias(chain, t.parent), "children": list(t.children), "opened_at": t.opened_at,
            "status": t.status, "accept_paths": list(t.accept_paths), "accept_cmds": list(t.accept_cmds),
            "has_criteria": bool(t.accept_paths or t.accept_cmds), "after": list(t.after),
            "files_hint": list(t.files_hint), "bug": t.bug,
            "claim": t.claim, "holder": active.holder if active is not None else None,
            "work_done_at": t.work_done_at, "work_done_link": t.work_done_link, "done_at": t.done_at,
            "done_link": t.done_link, "no_change": [{"subtask": s, "reason": r} for s, r in t.no_change],
            "cited_link": t.cited_link, "cited_seq": cited.seq if cited is not None else None,
            "cited_author": cited.author if cited is not None else None,
            "offered_to": t.offered_to, "offer_until": t.offer_until,
            "waiting": ("acceptance" if t.status == "work_done" and not (t.accept_paths or t.accept_cmds)
                        else "criteria" if t.status == "work_done" else None),
            "claims": claims_by_task.get(t.id, []), "links": links_by_task.get(t.id, []),
        }

    def _bugs(self, chain: Chain) -> list[dict]:
        if self.bugs is None:
            return []
        cached = self._bugs_cache
        if cached is None or time.monotonic() - cached[0] > 10:
            try:
                items, _ = self.bugs.list(since=None, limit=1000)
            except Exception:
                items = []
            rows = []
            for item in items:
                parsed = mime.parse(item.raw_mime)
                rows.append({"id": E.unsigned_id(item.raw_mime), "from": parsed.from_addr, "subject": parsed.subject,
                             "text": parsed.text, "received_at": item.received_at})
            self._bugs_cache = cached = (time.monotonic(), rows)
        by_bug = {t.bug: t for t in chain.tasks.values() if t.bug}
        out = []
        for row in cached[1]:
            task = by_bug.get(row["id"])
            out.append({**row, "task": task.id if task else None, "task_alias": task.alias if task else None})
        return out

    # ------------------------------------------------------------ escalations

    def escalations(self) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            current = self._current_candidates(chain)
            by_neg: dict[Hex, list[ProposalState]] = {}
            for p in current:
                by_neg.setdefault(p.negotiation, []).append(p)
            rows = []
            for esc in sorted(chain.escalations.values(), key=lambda e: (e.opened_at, e.id)):
                pushes = []
                received: dict[Hex, int] = {}
                for root in esc.pushes:
                    props = by_neg.get(root, [])
                    head = max(props, key=lambda p: (p.received_at, p.push)) if props else None
                    root_prop = chain.push_status(root)
                    received[root] = root_prop.received_at if root_prop is not None else (
                        head.received_at if head is not None else 0)
                    confirmed = any(o.status == "confirmed" for p in props for o in p.objections)
                    email = reader.email(head.push if head is not None else root)
                    env = email.envelope if email is not None else None
                    link = self._link_of(chain, root)
                    pushes.append({
                        "root": root, "push": head.push if head is not None else root,
                        "author": head.author if head is not None else (env.author if env else None),
                        "title": _first_line(env.text if env else "") or root[:12],
                        "status": head.status if head is not None else ("linked" if link else None),
                        "confirmed_evidence": confirmed,
                    })
                if esc.reason == "mutual":
                    order = sorted(esc.pushes, key=lambda r: (received.get(r, 0), r))
                    default = {"chosen": order[0] if order else None, "declined": order[1:],
                               "why": "the earlier-received push is chosen (§13.7 mutual default)"}
                else:
                    root = esc.pushes[0] if esc.pushes else None
                    bad = any(p["confirmed_evidence"] for p in pushes)
                    default = {"chosen": None if bad else root, "declined": [root] if bad and root else [],
                               "why": ("declined: a confirmed evidence objection is not cleared" if bad
                                       else "the push is chosen (§13.7 rounds default)")}
                rows.append({
                    "id": esc.id, "reason": esc.reason, "pushes": pushes, "opened_at": esc.opened_at,
                    "deadline": esc.deadline, "deciders": list(esc.deciders), "status": esc.status,
                    "chosen": esc.chosen, "declined": list(esc.declined), "decided_by": esc.decided_by,
                    "decided_at": esc.decided_at, "default": default,
                })
        return {"escalations": rows}

    # ------------------------------------------------------------ members

    def _owner_counts(self, link: LinkState, cfg, eval_time: int, chain: Chain) -> dict:
        """Per path at `link`: {owner: [lines, live lines]} from blame runs (no file reads)."""
        key = (link.blame_root, link.seq, eval_time // 60_000)
        hit = self._owners.get(key)
        if hit is not None:
            return hit
        blame = BlameMap(self.store, link.blame_root)
        out: dict[str, dict[str, list[int]]] = {}
        for path in blame.paths():
            per: dict[str, list[int]] = {}
            for run in blame.get(path):
                live = self._live(run.seq, link, cfg, eval_time, chain)
                cell = per.setdefault(run.owner, [0, 0])
                cell[0] += run.length
                if live:
                    cell[1] += run.length
            out[path] = per
        if len(self._owners) > 64:
            self._owners.clear()
        self._owners[key] = out
        return out

    @staticmethod
    def _live(seq: int, at: LinkState, cfg, eval_time: int, chain: Chain) -> bool:
        """§13.1 fading at link `at` for evaluation time `eval_time` (the replay time)."""
        if at.seq - seq < cfg.own_links:
            return True
        written = chain.links[seq].linked_at if 0 <= seq < len(chain.links) else 0
        return eval_time - written < cfg.own_time

    def members(self) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            tip = chain.tip
            if tip is None:
                return {"members": [], "removed": [], "joins": self._joins(chain, reader, None)}
            rules = reader.rules()
            owners = self._owner_counts(tip, rules.config, chain.t, chain)
            totals: dict[str, list[int]] = {}
            for per in owners.values():
                for h, (n, live) in per.items():
                    cell = totals.setdefault(h, [0, 0])
                    cell[0] += n
                    cell[1] += live
            first_seen: dict[Handle, int] = {}
            removed: dict[Handle, dict] = {}
            prev: Members | None = None
            for link in chain.links:
                if link.seq != 0 and MEMBERS not in link.files:
                    continue
                ms = reader.members(link.state)
                for h in ms.by_handle:
                    first_seen.setdefault(h, link.seq)
                    removed.pop(h, None)
                if prev is not None:
                    for h, m in prev.by_handle.items():
                        if ms.get(h) is None:
                            removed[h] = {"handle": h, "role": m.role, "address": m.address, "removed_seq": link.seq,
                                          "removed_link": link.state}
                prev = ms
            rows = []
            for h in sorted(rules.members.by_handle):
                m = rules.members.by_handle[h]
                d = self.registry.get(h) if self.registry is not None else None
                rows.append({
                    "handle": h, "role": m.role, "address": m.address, "key": m.public_key,
                    "key_short": _key_short(m.public_key), "sponsor": m.sponsor, "votes": m.votes,
                    "lines": totals.get(h, [0, 0])[0], "lines_live": totals.get(h, [0, 0])[1],
                    "added_seq": first_seen.get(h),
                    "delivery": self._delivery_json(h, d),
                })
            gone = []
            for h, row in sorted(removed.items()):
                row["lines"] = totals.get(h, [0, 0])[0]
                gone.append(row)
            joins = self._joins(chain, reader, rules.members)
        return {"members": rows, "removed": gone, "joins": joins}

    def _delivery_json(self, handle: Handle, d) -> dict | None:
        if d is None:
            return None
        head = self.dispatcher.journal.head(handle) if self.dispatcher is not None else None
        acked = self.dispatcher.acked(handle) if self.dispatcher is not None else None
        registered = self.registry is not None and handle in {x.handle for x in self.registry.all()}
        return {"channel": d.channel, "url": d.url, "registered": registered, "journal_head": head, "acked": acked}

    def _joins(self, chain: Chain, reader: ChainReader, members: Members | None) -> list[dict]:
        proposed: dict[Hex, ProposalState] = {}
        for p in self._current_candidates(chain):
            email = reader.email(p.push)
            env = email.envelope if email is not None else None
            jid = env.get("Gud-Join") if env is not None else None
            if jid and p.status in LIVE_STATUSES:
                proposed[jid] = p
        out = []
        for jid in reader.joins():
            email = reader.email(jid)
            env = email.envelope if email is not None else None
            if env is None:
                continue
            handle = env.author or ""
            member = members.get(handle) if members is not None else None
            status = "pending"
            if member is not None and member.public_key == env.get("Gud-Key"):
                status = "member"
            elif jid in proposed:
                status = "proposed"
            seq = self.intake.log.seq_of(jid)
            out.append({
                "id": jid, "handle": handle, "role": env.get("Gud-Role"), "address": env.get("Gud-Address"),
                "key": env.get("Gud-Key"), "key_short": _key_short(env.get("Gud-Key")), "sponsor": env.get("Gud-Sponsor"),
                "reason": env.text, "received_at": self.intake.log.at(seq).time if seq is not None else None,
                "status": status, "push": proposed[jid].push if jid in proposed else None,
            })
        return out

    # ------------------------------------------------------------ trees, files, blame

    def _tree_at(self, chain: Chain, reader: ChainReader, at: str | None) -> tuple[Tree, dict, LinkState | None]:
        if chain.tip is None:
            raise HTTPException(404, "no genesis yet")
        if not at or at == "tip":
            tip = chain.tip
            return reader.tree(), {"kind": "link", "state": tip.state, "seq": tip.seq}, tip
        gid = self.resolve(at, chain)
        link = self._link_of(chain, gid)
        if link is not None:
            return reader.tree(link.state), {"kind": "link", "state": link.state, "seq": link.seq}, link
        prop = chain.proposals.get(gid) or chain.push_status(gid)
        if prop is not None:
            tree = reader.candidate_tree(prop.id) or self._native_tree(chain, reader, prop.push)
            if tree is not None:
                return tree, {"kind": "proposal", "state": prop.id, "push": prop.push, "status": prop.status}, None
        raise HTTPException(404, f"{at} is not a link or an in-flight push with a tree")

    def _native_tree(self, chain: Chain, reader: ChainReader, push: Hex) -> Tree | None:
        email = reader.email(push)
        parent = email.envelope.get("Gud-Parent") if email is not None and email.envelope else None
        if not parent or chain.link(parent) is None:
            return None
        from gud.core.ops import OpError, apply_ops
        try:
            return apply_ops(reader.tree(parent), reader.ops_of(push), _StoreBlobs(self.store))
        except (OpError, KeyError, ValueError):
            return None

    def tree(self, at: str | None) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            tree, where, link = self._tree_at(chain, reader, at)
            cfg = reader.rules(link.state).config if link is not None else reader.rules().config
            owners = self._owner_counts(link, cfg, chain.t, chain) if link is not None else {}
            members = reader.members(link.state) if link is not None else reader.members()
        files = []
        for path, entry in tree.items():
            per = owners.get(path, {})
            text = entry.mode != "120000" and entry.size <= TEXT_LIMIT and is_text(tree.read(path))
            files.append({
                "path": path, "mode": entry.mode, "size": entry.size, "sha256": entry.blob, "binary": not text,
                "lines": sum(n for n, _ in per.values()) if text else None,
                "owners": {h: n for h, (n, _) in sorted(per.items())},
                "live_owners": {h: live for h, (_, live) in sorted(per.items()) if live},
                "former": sorted(h for h in per if members.get(h) is None),
            })
        return {"at": where, "files": files}

    def file(self, path: str, at: str | None) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            tree, where, _ = self._tree_at(chain, reader, at)
        entry = tree.get(path)
        if entry is None:
            raise HTTPException(404, f"{path} is not in that tree")
        data = tree.read(path)
        out: dict[str, Any] = {"path": path, "at": where, "mode": entry.mode, "size": entry.size,
                               "sha256": entry.blob}
        if entry.mode == "120000":
            out.update(kind="symlink", target=data.decode("utf-8", "replace"))
        elif is_text(data):
            text = data.decode("utf-8")
            out.update(kind="text", text=text[:TEXT_LIMIT], truncated=len(text) > TEXT_LIMIT,
                       lines=len(split_lines(data)))
        else:
            out.update(kind="binary", placeholder=f"binary file, {entry.size} bytes, sha256 {entry.blob[:12]}")
        return out

    def blame(self, path: str, at: str | None) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            _tree, where, link = self._tree_at(chain, reader, at)
            if link is None:
                raise HTTPException(400, "blame is per link; a proposal has no blame yet")
            rows = reader.blame(path, link.state)
            cfg = reader.rules(link.state).config
            members = reader.members(link.state)
            tip_members = reader.members()
            t = chain.t
            links = chain.links
        if not rows:
            raise HTTPException(404, f"{path} is not in the tree at #{link.seq}")
        entry = _tree.get(path)
        binary = entry is None or entry.mode == "120000" or not is_text(_tree.read(path))
        lines = []
        intents: dict[int, str] = {}
        for i, (owner, seq, data) in enumerate(rows, start=1):
            src = links[seq] if 0 <= seq < len(links) else None
            if seq not in intents:
                intents[seq] = _first_line(src.intent) if src is not None else ""
            live = self._live(seq, link, cfg, t, chain)
            m = members.get(owner)
            lines.append({
                "n": i, "text": "" if binary else data.decode("utf-8", "replace").rstrip("\n"),
                "owner": owner, "seq": seq, "link": src.state if src is not None else None,
                "intent": intents[seq], "live": live,
                "voter": bool(m is not None and m.votes),
                "member": tip_members.get(owner) is not None,
                "expires_at": (None if src is None or live is False else
                               (src.linked_at + cfg.own_time) if link.seq - seq >= cfg.own_links else None),
                "links_left": max(0, cfg.own_links - (link.seq - seq)),
            })
        if binary:
            lines[0]["text"] = f"(binary file, {entry.size if entry else 0} bytes)"
        return {"path": path, "at": where, "binary": binary, "own_links": cfg.own_links, "own_time": cfg.own_time,
                "t": t, "lines": lines}

    # ------------------------------------------------------------ diffs

    def diff(self, ref: str) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            gid = self.resolve(ref, chain)
            link = self._link_of(chain, gid)
            ops: tuple[ChangeOp, ...] = ()
            if link is not None:
                before = reader.tree(link.parent) if link.parent else Tree.empty(self.store)
                after = reader.tree(link.state)
                ops = reader.ops_of(link.push)
                head = {"kind": "link", "state": link.state, "push": link.push, "seq": link.seq,
                        "parent": link.parent, "carried_from": link.carried_from, "author": link.author}
            else:
                prop = chain.proposals.get(gid) or chain.push_status(gid)
                if prop is None:
                    raise HTTPException(404, f"{ref} is not a link or a push")
                ops = reader.ops_of(prop.push)
                after = reader.candidate_tree(prop.id)
                base_state = prop.parent if after is not None else prop.named_parent
                if after is None:
                    after = self._native_tree(chain, reader, prop.push)
                before = reader.tree(base_state) if base_state and chain.link(base_state) else None
                head = {"kind": "proposal", "candidate": prop.id, "push": prop.push, "status": prop.status,
                        "parent": base_state, "named_parent": prop.named_parent, "author": prop.author}
        signed = [self._op_json(op) for op in ops]
        if before is None or after is None:
            return {**head, "available": False, "why": "blobs pending or the parent is not a link",
                    "ops": signed, "files": [], "unified": ""}
        files, unified = self._render(before, after)
        return {**head, "available": True, "ops": signed, "files": files, "unified": unified}

    @staticmethod
    def _op_json(op: ChangeOp) -> dict:
        if isinstance(op, Patch):
            return {"op": "patch", "path": op.path}
        if isinstance(op, Put):
            return {"op": "put", "path": op.path, "mode": op.mode, "sha256": op.sha256, "size": op.size,
                    "inline": op.data is not None}
        if isinstance(op, Delete):
            return {"op": "delete", "path": op.path}
        if isinstance(op, Rename):
            return {"op": "rename", "src": op.src, "dst": op.dst}
        if isinstance(op, ModeOp):
            return {"op": "mode", "path": op.path, "mode": op.mode}
        return {"op": type(op).__name__}

    def _render(self, before: Tree, after: Tree) -> tuple[list[dict], str]:
        """The change before → after per path (`add`, `modify`, `delete`, `rename`, `mode`), with
        unified hunks for text; binaries and symlinks get a placeholder instead of content."""
        ops = diff_trees(before, after, inline_budget=1 << 62)
        files: list[dict] = []
        out: list[str] = []
        shown: set[str] = set()
        for op in ops:
            if isinstance(op, Rename) and before.get(op.src) == after.get(op.dst):
                files.append({"op": "rename", "path": op.dst, "src": op.src, "binary": False, "added": 0,
                              "removed": 0, "hunks": ""})
                out += [f"diff --git a/{op.src} b/{op.dst}", "similarity index 100%", f"rename from {op.src}",
                        f"rename to {op.dst}"]
                shown.update((op.src, op.dst))
        for path in before.changed_paths(after):
            if path in shown:
                continue
            a, b = before.get(path), after.get(path)
            if a is None:
                kind = "add"
            elif b is None:
                kind = "delete"
            elif a.blob == b.blob:
                kind = "mode"
            else:
                kind = "modify"
            old = before.read(path) if a is not None else None
            new = after.read(path) if b is not None else None
            link = (a is not None and a.mode == "120000") or (b is not None and b.mode == "120000")
            text = not link and (old is None or is_text(old)) and (new is None or is_text(new))
            row: dict[str, Any] = {"op": kind, "path": path, "binary": not text,
                                   "old_mode": a.mode if a else None, "new_mode": b.mode if b else None,
                                   "old_size": a.size if a else None, "new_size": b.size if b else None,
                                   "old_sha256": a.blob if a else None, "new_sha256": b.blob if b else None,
                                   "added": 0, "removed": 0, "hunks": ""}
            out.append(f"diff --git a/{path} b/{path}")
            if a is None:
                out.append(f"new file mode {b.mode}")
            elif b is None:
                out.append(f"deleted file mode {a.mode}")
            elif a.mode != b.mode:
                out += [f"old mode {a.mode}", f"new mode {b.mode}"]
            if kind == "mode":
                files.append(row)
                continue
            if not text:
                if link:
                    row["placeholder"] = "symlink " + " → ".join(
                        x.decode("utf-8", "replace") for x in (old, new) if x is not None)
                else:
                    row["placeholder"] = (f"binary file: {a.size if a else 0} → {b.size if b else 0} bytes"
                                          f" (sha256 {(b.blob if b else a.blob)[:12]})")
                out.append(f"Binary files {'a/' + path if a else '/dev/null'} and "
                           f"{'b/' + path if b else '/dev/null'} differ")
                files.append(row)
                continue
            hunks = make_patch(old or b"", new or b"").decode("utf-8", "replace")
            row["hunks"] = hunks
            row["added"], row["removed"] = _line_counts(old, new)
            out.append(f"--- {'a/' + path if a else '/dev/null'}")
            out.append(f"+++ {'b/' + path if b else '/dev/null'}")
            out += hunks.splitlines()
            files.append(row)
        return files, "\n".join(out) + ("\n" if out else "")

    # ------------------------------------------------------------ threads

    def _message_json(self, email: Email) -> dict:
        env = email.envelope
        seq = self.intake.log.seq_of(email.gud_id)
        out: dict[str, Any] = {
            "id": email.gud_id, "seq": seq, "received_at": self.intake.log.at(seq).time if seq is not None else None,
            "kind": env.kind if env else "unsigned", "author": env.author if env else None,
            "from": email.from_addr, "subject": email.subject, "signed": env is not None,
            "text": env.text if env else email.text, "message_id": email.message_id,
            "headers": {}, "evidence": None,
        }
        if env is not None:
            for name in ("Gud-Target", "Gud-Task", "Gud-Parent", "Gud-Revises", "Gud-Result", "Gud-Scope",
                         "Gud-On", "Gud-Base-Result", "Gud-Escalate", "Gud-Verdict", "Gud-Files", "Gud-Title",
                         "Gud-Parent-Task", "Gud-Release", "Gud-Join", "Gud-Tree", "Gud-Sent", "Gud-Seen"):
                value = env.get(name)
                if value:
                    out["headers"][name[4:].lower()] = value
            ev = env.records_named("evidence")
            if ev:
                out["evidence"] = {"path": ev[0].args[0], "sha256": ev[0].args[1], "size": int(ev[0].args[2])}
        return out

    def thread(self, ref: str) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            gid = self.resolve(ref, chain)
            link = self._link_of(chain, gid)
            head: dict[str, Any] = {"id": gid}
            roots: list[Hex] = []
            if link is not None:
                head.update(type="link", seq=link.seq, state=link.state, push=link.push, author=link.author,
                            title=_first_line(link.intent))
                roots = [link.push]
            elif gid in chain.tasks:
                t = chain.tasks[gid]
                head.update(type="task", alias=t.alias, title=t.title, status=t.status)
                roots = [gid] + [c.latest for c in chain.claims.values() if c.task == gid] + \
                    [c.id for c in chain.claims.values() if c.task == gid] + list(t.children)
            elif gid in chain.claims:
                c = chain.claims[gid]
                head.update(type="claim", holder=c.holder, task=c.task, status=c.status)
                roots = [gid, c.latest]
            elif gid in chain.escalations:
                esc = chain.escalations[gid]
                head.update(type="escalation", reason=esc.reason, status=esc.status)
                roots = list(esc.pushes)
            else:
                prop = chain.proposals.get(gid) or chain.push_status(gid)
                if prop is not None:
                    head.update(type="proposal", push=prop.push, status=prop.status, author=prop.author)
                    neg = [p.push for p in chain.proposals.values() if p.negotiation == prop.negotiation]
                    roots = sorted(set([prop.push, prop.negotiation] + neg))      # the root may have linked
                else:
                    head.update(type="message")
                    roots = [gid]
            if head.get("type") in ("link", "escalation"):
                # the whole negotiation of each push (§13.6): follow Gud-Revises back to its root,
                # then every push still known under that root, linked revisions included
                chained: set[Hex] = set(roots)
                for push in list(roots):
                    cur, steps = push, 0
                    while steps < 64:
                        email = reader.email(cur)
                        env = email.envelope if email is not None else None
                        prev = env.get("Gud-Revises") if env is not None else None
                        if not prev or prev in chained:
                            break
                        chained.add(prev)
                        cur, steps = prev, steps + 1
                negs = {p.negotiation for p in chain.proposals.values() if p.push in chained} | chained
                roots = sorted(chained | {p.push for p in chain.proposals.values() if p.negotiation in negs})
            seen: dict[Hex, Email] = {}
            for root in roots:
                for email in reader.thread(root):
                    seen.setdefault(email.gud_id, email)
                if root not in seen:
                    email = reader.email(root)
                    if email is not None:
                        seen[root] = email
            msgs = sorted((self._message_json(e) for e in seen.values()),
                          key=lambda m: (m["seq"] if m["seq"] is not None else 1 << 60, m["id"]))
        if not msgs:
            raise HTTPException(404, f"no message {ref} in the log")
        return {"head": head, "messages": msgs}

    # ------------------------------------------------------------ needs

    def needs(self, member: str | None) -> dict:
        with self.intake.lock:
            chain, reader = self._chain_reader()
            if chain.tip is None:
                return {"member": member, "items": []}
            members = reader.members()
            who = member or next((h for h in sorted(self.signers) if (m := members.get(h)) and m.role == "human"),
                                 None)
            if not who:
                return {"member": None, "items": []}
            paths = list(reader.tree().paths())
            items = needs(chain, who, members=members, now=chain.t, tip_paths=paths)
        return {"member": who, "items": items}

    # ------------------------------------------------------------ host log and audit

    def host(self, limit: int = 50) -> dict:
        log = self.intake.log
        with self.intake.lock:
            chain, reader = self._chain_reader()
            n = len(log)
            entries = log.entries(max(0, n - max(1, limit)), max(1, limit))
            counts: dict[str, int] = {}
            for e in log.entries():
                counts[e.type] = counts.get(e.type, 0) + 1
            tail = []
            for e in reversed(entries):
                row: dict[str, Any] = {"seq": e.seq, "type": e.type, "time": e.time, "hash": e.hash}
                if e.type == "mail":
                    gid = e.field("id") or ""
                    row["id"] = gid
                    email = reader.email(gid)
                    if email is None:
                        raw = self.intake.raw(gid)
                        email = mime.to_email(raw) if raw is not None else None
                    if email is not None:
                        env = email.envelope
                        row.update(kind=env.kind if env else "unsigned",
                                   author=(env.author if env else email.from_addr),
                                   summary=_first_line(env.text if env else email.text)[:160])
                    else:
                        row.update(kind=None, missing=True)
                elif e.type in ("open", "handover"):
                    row["fields"] = dict(e.fields)
                tail.append(row)
            processed = chain.log_head.seq
            missing = list(self.intake.missing)
            last_error = self.replayer.last_error or self.intake.last_error
            next_wakeup = self.intake.next_wakeup
        mail_count = counts.get("mail", 0)
        if self._audit is None or self._audit[0] != mail_count:
            try:
                report = audit(self.intake, retained=[], config=self._config()).to_json()
            except Exception as exc:  # pragma: no cover - defensive
                report = {"ok": False, "exit_status": 2, "error": str(exc)}
            report["checked_at"] = self.now_ms()
            report["mail_entries"] = mail_count
            self._audit = (mail_count, report)
        report = self._audit[1]
        deliveries = []
        if self.registry is not None:
            for d in self.registry.all():
                deliveries.append({"handle": d.handle, "channel": d.channel, "url": d.url,
                                   "journal_head": self.dispatcher.journal.head(d.handle) if self.dispatcher else None,
                                   "acked": self.dispatcher.acked(d.handle) if self.dispatcher else None})
        head = log.head
        return {
            "repo": self.repo, "key": log.key, "repos": list(log.repos),
            "head": {"seq": head.seq, "hash": head.hash, "time": head.time, "type": head.type} if head else None,
            "processed_seq": processed, "entries": n, "counts": counts, "tail": tail,
            "handed_over": bool(head is not None and head.type == "handover"),
            "missing_mail": missing, "last_error": last_error, "next_wakeup": next_wakeup,
            "intake": dict(self.intake.stats),
            "dispatch": dict(self.dispatcher.stats) if self.dispatcher is not None else {},
            "deliveries": deliveries,
            "feed_head": self.feed.head() if self.feed is not None else None,
            "audit": {
                "ok": report.get("ok"), "exit_status": report.get("exit_status"), "log_ok": report.get("log_ok"),
                "log_error": report.get("log_error"), "checked_at": report.get("checked_at"),
                "mail_entries": report.get("mail_entries"),
                "seen_mismatches": len(report.get("seen_mismatches") or []),
                "fork_proofs": len(report.get("fork_proofs") or []),
                "late_votes": len(report.get("late_votes") or []),
                "missing_mail": len(report.get("missing_mail") or []),
                "altered": len(report.get("altered") or []),
                "details": {k: report.get(k) for k in ("seen_mismatches", "late_votes", "missing_mail", "altered")},
            },
        }

    def _config(self):
        rules = self.intake.rules()
        if rules is None:
            from gud.core.config import RepoConfig
            return RepoConfig()
        return rules.config

    # ------------------------------------------------------------ events

    def events(self, after: int, limit: int) -> dict:
        if self.feed is None:
            return {"events": [], "head": 0}
        rows = [{"n": n, **event_dict(e)} for n, e in self.feed.since(after, limit)]
        return {"events": rows, "head": self.feed.head()}

    def stream(self, after: int, *, follow: bool = True, keepalive_s: float = 15.0,
               max_s: float | None = None) -> Iterator[bytes]:
        cursor = max(0, after)
        deadline = None if max_s is None else time.monotonic() + max_s
        last_log = len(self.intake.log)
        last_keep = time.monotonic()
        yield b": gud ui\n\n"
        while True:
            items = self.feed.since(cursor, 200) if self.feed is not None else []
            for n, event in items:
                cursor = n
                yield (f"id: {n}\nevent: {event.type}\ndata: ".encode() + E.jcs(event_dict(event, self.repo))
                       + b"\n\n")
            n_log = len(self.intake.log)
            if n_log != last_log:
                last_log = n_log
                head = self.intake.head()
                yield (b"event: head\ndata: " + E.jcs({"seq": head.seq, "time": head.time, "type": head.type})
                       + b"\n\n")
            if items:
                continue
            if not follow:
                return
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return
            if now - last_keep >= keepalive_s:
                last_keep = now
                yield b": keepalive\n\n"
            wait = 1.0 if deadline is None else max(0.0, min(1.0, deadline - now))
            if self.feed is not None:
                self.feed.wait(cursor, wait)
            else:
                time.sleep(wait)

    # ------------------------------------------------------------ actions (loopback only)

    def _signer(self, want: str | None, roles: Sequence[str]) -> tuple[Handle, bytes, Member]:
        members = self.intake.members()
        if members is None:
            raise HTTPException(409, "no genesis yet")
        pool = [want] if want else sorted(self.signers, key=lambda h: (roles.index(m.role) if (m := members.get(h))
                                                                       and m.role in roles else 99, h))
        for h in pool:
            if h not in self.signers:
                continue
            m = members.get(h)
            if m is not None and m.role in roles and m.public_key == E.public_of(self.signers[h]):
                return h, self.signers[h], m
        if want:
            raise HTTPException(403, f"{want} cannot do this from the UI (no key here, not a member, a key that "
                                     f"does not match the members file, or a role not in {', '.join(roles)})")
        raise HTTPException(409, f"no UI signer with role {' or '.join(roles)}: start `gud host serve` with "
                                 f"--ui-home pointing at that member's GUD_HOME")

    def _send(self, kind: str, handle: Handle, key: bytes, member: Member, headers: Mapping[str, str],
              records: Sequence[Record], summary: str) -> dict:
        env = E.build(kind, author=handle, headers=dict(headers), records=tuple(records),
                      sent=E.now_sent(self.clock), seen=self.intake.seen())
        problem = E.validate_kind(env)
        if problem:
            raise HTTPException(400, f"{kind}: {problem}")
        env = E.sign(env, key)
        gid = E.gud_id(env)
        raw = mime.build(Outgoing(from_addr=member.address, to=(self.repo,), subject=mime.subject_for(env, gid, summary),
                                  text=mime.render_human_part(env, gid), envelope=E.encode(env)))
        delta = self.intake.on_received(received_now(raw, self.intake.clock, provider_id="ui"))
        return {"id": gid, "kind": kind, "as": handle, "logged": self.intake.log.seq_of(gid) is not None,
                "events": [e.type for e in delta.events] if delta is not None else []}

    def act_task(self, body: Mapping[str, Any]) -> dict:
        title = str(body.get("title") or "").strip()
        if not title or "\n" in title:
            raise HTTPException(400, "a one-line title is required")
        handle, key, member = self._signer(body.get("as"), ("human", "console", "agent"))
        headers = {"Gud-Title": title}
        requester = body.get("requester")
        if requester:
            headers["Gud-Requester"] = str(requester)
        accept = [str(x).strip() for x in (body.get("accept") or []) if str(x).strip()]
        if accept:
            headers["Gud-Accept"] = E.render_list(accept)
        if body.get("bug"):
            headers["Gud-Bug"] = str(body["bug"])
        records = []
        if str(body.get("description") or "").strip():
            records.append(Record("text", (), str(body["description"]).encode("utf-8")))
        cmds = [str(x) for x in (body.get("accept_cmds") or []) if str(x).strip()]
        if cmds:
            records.append(Record("accept", (), "\n".join(cmds).encode("utf-8")))
        return self._send("task", handle, key, member, headers, records, title)

    def act_vote(self, body: Mapping[str, Any]) -> dict:
        kind = str(body.get("kind") or "")
        if kind not in ("approve", "object", "veto", "hold"):
            raise HTTPException(400, "kind is approve, object, veto or hold")
        with self.intake.lock:
            chain = self.replayer.chain()
            target = self.resolve(body.get("target"), chain)
            is_push = chain.push_status(target) is not None or target in chain.proposals or \
                self._link_of(chain, target) is not None
            is_task = target in chain.tasks
        if not is_push and not (kind == "approve" and is_task):
            raise HTTPException(400, f"{kind} targets a push" + (" (approve also a task)" if kind != "approve" else
                                                                 " or a task"))
        roles = ("human",) if kind == "veto" else ("human", "agent")
        handle, key, member = self._signer(body.get("as"), roles)
        text = str(body.get("text") or "").strip()
        if kind == "veto":
            text = text if text.lower().startswith("veto") else ("veto " + text).strip()
        if kind == "object" and not text:
            raise HTTPException(400, "an objection needs a reason")
        if kind == "object" and text.lower().split(" ", 1)[0].strip(".,!?;:") == "veto" and member.role != "human":
            raise HTTPException(400, "only a human can veto")
        wire = "object" if kind == "veto" else kind
        records = [Record("text", (), text.encode("utf-8"))] if text else []
        return self._send(wire, handle, key, member, {"Gud-Target": target}, records, f"{kind} {target[:12]}")

    def act_comment(self, body: Mapping[str, Any]) -> dict:
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "a comment needs text")
        with self.intake.lock:
            chain = self.replayer.chain()
            target = self.resolve(body.get("target"), chain)
            task = self.resolve(body["task"], chain) if body.get("task") else None
        handle, key, member = self._signer(body.get("as"), ("human", "console", "agent"))
        headers = {"Gud-Target": target}
        if task:
            headers["Gud-Task"] = task
        if body.get("verdict") == "reject":
            headers["Gud-Verdict"] = "reject"
        return self._send("comment", handle, key, member, headers, [Record("text", (), text.encode("utf-8"))],
                          f"comment on {target[:12]}")

    def act_admit(self, body: Mapping[str, Any]) -> dict:
        handle, key, member = self._signer(body.get("as"), ("human", "console", "agent"))
        with self.intake.lock:
            chain, reader = self._chain_reader()
            if chain.tip is None:
                raise HTTPException(409, "no genesis yet")
            jid = self.resolve(body.get("join"), chain)
            email = reader.email(jid)
            env = email.envelope if email is not None else None
            if env is None or env.kind != "join" or jid not in reader.joins():
                raise HTTPException(404, f"{jid[:12]} is not a logged join request with a valid self-signature")
            tip = chain.tip
            tree = reader.tree()
            members = reader.members()
        by = dict(members.by_handle)
        new = Member(env.author or "", env.get("Gud-Role") or "agent", env.get("Gud-Address") or "",  # type: ignore[arg-type]
                     env.get("Gud-Key"), env.get("Gud-Sponsor"))
        by[new.handle] = new
        try:
            data = render_members(Members(by))
            parse_members(data)
        except ConfigError as exc:
            raise HTTPException(400, f"cannot admit: {exc}") from exc
        after = tree.set(MEMBERS, "100644", data)
        ops = diff_trees(tree, after, inline_budget=1 << 30)
        if not ops:
            raise HTTPException(409, f"{new.handle} is already a member with that line")
        intent = str(body.get("text") or f"admit {new.handle} ({new.role})")
        records = [Record("text", (), intent.encode("utf-8"))] + list(ops_to_records(ops))
        headers = {"Gud-Parent": tip.state, "Gud-Tree": after.hash(), "Gud-Join": jid}
        out = self._send("push", handle, key, member, headers, records, intent)
        out["member"] = {"handle": new.handle, "role": new.role, "address": new.address, "sponsor": new.sponsor}
        return out


def _ok_glob(pattern: str) -> bool:
    try:
        glob_match(pattern, "x")
        return True
    except ConfigError:
        return False


# ---------------------------------------------------------------- routes

def add_view_routes(app: FastAPI, view: HostView, *, caller: Callable, is_loopback: Callable[[Request], bool],
                    sse_keepalive_s: float = 15.0) -> None:
    """Mount `/api/v1/view/*` on the host app. `caller(request, body)` is the mirror's §21 check."""

    def int_q(request: Request, name: str, default: int, lo: int = 0, hi: int | None = None) -> int:
        raw = request.query_params.get(name)
        if raw in (None, ""):
            return default
        try:
            value = max(lo, int(raw))
        except ValueError as exc:
            raise HTTPException(400, f"{name} must be an integer") from exc
        return min(hi, value) if hi is not None else value

    async def auth(request: Request, body: bytes = b"") -> None:
        await caller(request, body)

    def sync(fn: Callable[[], dict]):
        import anyio
        return anyio.to_thread.run_sync(fn)

    @app.get("/api/v1/view/state")
    async def v_state(request: Request):
        await auth(request)
        return await sync(view.state)

    @app.get("/api/v1/view/chain")
    async def v_chain(request: Request):
        await auth(request)
        limit = int_q(request, "limit", 200, 1, 2000)
        before = request.query_params.get("before")
        b = int(before) if before and before.lstrip("-").isdigit() else None
        return await sync(lambda: view.chain(limit, b))

    @app.get("/api/v1/view/proposals")
    async def v_proposals(request: Request):
        await auth(request)
        dead = int_q(request, "dead", 50, 0, 1000)
        return await sync(lambda: view.proposals(dead))

    @app.get("/api/v1/view/tasks")
    async def v_tasks(request: Request):
        await auth(request)
        return await sync(view.tasks)

    @app.get("/api/v1/view/escalations")
    async def v_escalations(request: Request):
        await auth(request)
        return await sync(view.escalations)

    @app.get("/api/v1/view/members")
    async def v_members(request: Request):
        await auth(request)
        return await sync(view.members)

    @app.get("/api/v1/view/tree")
    async def v_tree(request: Request):
        await auth(request)
        at = request.query_params.get("at")
        return await sync(lambda: view.tree(at))

    @app.get("/api/v1/view/file")
    async def v_file(request: Request):
        await auth(request)
        path = request.query_params.get("path") or ""
        if not path:
            raise HTTPException(400, "path is required")
        at = request.query_params.get("at")
        return await sync(lambda: view.file(path, at))

    @app.get("/api/v1/view/blame")
    async def v_blame(request: Request):
        await auth(request)
        path = request.query_params.get("path") or ""
        if not path:
            raise HTTPException(400, "path is required")
        at = request.query_params.get("at")
        return await sync(lambda: view.blame(path, at))

    @app.get("/api/v1/view/diff/{ref}")
    async def v_diff(ref: str, request: Request):
        await auth(request)
        return await sync(lambda: view.diff(ref))

    @app.get("/api/v1/view/thread/{ref}")
    async def v_thread(ref: str, request: Request):
        await auth(request)
        return await sync(lambda: view.thread(ref))

    @app.get("/api/v1/view/needs")
    async def v_needs(request: Request):
        await auth(request)
        member = request.query_params.get("member") or None
        if member is not None and not E.is_handle(member):
            raise HTTPException(400, "member must be a handle")
        return await sync(lambda: view.needs(member))

    @app.get("/api/v1/view/host")
    async def v_host(request: Request):
        await auth(request)
        limit = int_q(request, "limit", 50, 1, 1000)
        return await sync(lambda: view.host(limit))

    @app.get("/api/v1/view/events")
    async def v_events(request: Request):
        await auth(request)
        after = int_q(request, "after", 0)
        limit = int_q(request, "limit", 500, 1, 5000)
        return view.events(after, limit)

    @app.get("/api/v1/view/stream")
    async def v_stream(request: Request):
        await auth(request)
        after = int_q(request, "after", 0)
        last = request.headers.get("Last-Event-ID")
        if last and last.strip().isdigit():
            after = max(after, int(last.strip()))
        follow = request.query_params.get("follow", "1") not in ("0", "false", "no")
        raw = request.query_params.get("max")
        try:
            max_s = max(0.0, min(86_400.0, float(raw))) if raw else None
        except ValueError as exc:
            raise HTTPException(400, "max must be a number") from exc
        gen = view.stream(after, follow=follow, keepalive_s=sse_keepalive_s, max_s=max_s)
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def loopback_only(request: Request) -> None:
        if not is_loopback(request):
            raise HTTPException(403, "UI actions are loopback-only")

    async def body_json(request: Request) -> dict:
        try:
            obj = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "body must be JSON") from exc
        if not isinstance(obj, dict):
            raise HTTPException(400, "body must be a JSON object")
        return obj

    for name in ("task", "vote", "comment", "admit"):
        def make(name: str):
            async def act(request: Request):
                loopback_only(request)
                obj = await body_json(request)
                fn = getattr(view, f"act_{name}")
                return await sync(lambda: fn(obj))
            act.__name__ = f"v_act_{name}"
            return act
        app.post(f"/api/v1/view/act/{name}")(make(name))


def add_static_routes(app: FastAPI, dist: Path | None) -> None:
    """Serve the built UI (`ui/dist`) at `/`: files by path, `index.html` for everything else that
    is not under `/api/`. Without a build, `/` says how to make one."""

    root = Path(dist).resolve() if dist is not None else None

    def index() -> Any:
        if root is None or not (root / "index.html").is_file():
            return HTMLResponse("<!doctype html><title>gud host</title><p>gud host v1 is running. The web UI is not "
                                "built: run <code>npm ci &amp;&amp; npm run build</code> in <code>ui/</code> "
                                "(or set <code>GUD_UI_DIST</code>).</p>", status_code=200)
        return FileResponse(root / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def ui_root():
        return index()

    @app.get("/{path:path}", include_in_schema=False)
    def ui_file(path: str):
        if path.startswith("api/") or path == "api":
            raise HTTPException(404, "not found")
        if root is not None and path:
            target = (root / path).resolve()
            if target.is_file() and (target == root or root in target.parents):
                kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                cache = "public, max-age=31536000, immutable" if "/assets/" in f"/{path}" else "no-cache"
                return FileResponse(target, media_type=kind, headers={"Cache-Control": cache})
        if "." in path.rsplit("/", 1)[-1]:
            raise HTTPException(404, "not found")
        return index()
