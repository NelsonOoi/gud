"""Leaderless tasks and claims (SPEC §15.5, §16): opening, claims with leases and file scope, overlap,
self-splitting, derived done, fix tasks.

`TaskBoard` is an incremental state machine fed in log order by `core.replay`. The engine verifies
signatures and roles before calling it; the board applies the §16 rules and returns §19 events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from .config import ConfigError, Glob, Members, RepoConfig, globs_overlap
from .envelope import make_event, sha256_hex
from .types import ClaimState, Event, Handle, Hex, LinkState, Ms, RedState, TaskState


class TaskError(ValueError):
    pass


# ---------------------------------------------------------------- inputs

@dataclass(frozen=True)
class TaskInput:
    id: Hex
    kind: Literal["task", "subtask"]
    author: Handle | None
    received: Ms
    title: str
    description: str
    requester: Handle | None
    parent: Hex | None
    accept_paths: tuple[str, ...]
    accept_cmds: tuple[str, ...]
    after: tuple[Hex, ...]
    files_hint: tuple[str, ...]
    bug: Hex | None


@dataclass(frozen=True)
class ClaimInput:
    id: Hex
    author: Handle
    received: Ms
    task: Hex
    files: tuple[str, ...]
    revises: Hex | None
    release: bool
    after: tuple[Hex, ...]
    done: bool = False               # Gud-Done: yes, the holder declares the item's work complete (§16.6)
    has_live_push: bool = False      # engine: the author has a live push with Gud-Task = task


@dataclass(frozen=True)
class Activity:
    by: Handle
    task: Hex
    at: Ms


@dataclass(frozen=True)
class NoChangeInput:
    id: Hex
    author: Handle
    received: Ms
    task: Hex
    tip: Hex
    reason: str
    paths_match: bool = True       # engine: Gud-Tip is a link and the claim's paths are unchanged since it
    has_live_push: bool = False    # engine: the author has a live push with Gud-Task = task


def fix_task_id(red_link: Hex) -> Hex:
    """§15.5."""
    return sha256_hex(("gud-fix/1\n" + red_link + "\n").encode("utf-8"))


def scope_matches(files: Sequence[str], path: str) -> bool:
    """True when `path` matches any glob in `files` (invalid globs match nothing)."""
    for pattern in files:
        try:
            if Glob(pattern).match(path):
                return True
        except ConfigError:
            continue
    return False


def aliases(tasks: Sequence[TaskState]) -> Mapping[Hex, str]:
    """§16.1: roots T1, T2, … by opening time; children T<n>.<m> by opening time under their parent."""
    by_parent: dict[Hex | None, list[TaskState]] = {}
    ids = {t.id for t in tasks}
    for t in tasks:
        parent = t.parent if t.parent in ids else None
        by_parent.setdefault(parent, []).append(t)
    out: dict[Hex, str] = {}

    def walk(parent: Hex | None, prefix: str) -> None:
        kids = sorted(by_parent.get(parent, []), key=lambda t: (t.opened_at, t.id))
        for n, t in enumerate(kids, 1):
            out[t.id] = f"{prefix}{n}" if parent is None else f"{prefix}.{n}"
            walk(t.id, out[t.id])

    walk(None, "T")
    return out


# ---------------------------------------------------------------- internal records

@dataclass
class _Task:
    id: Hex
    kind: str
    title: str
    description: str
    author: Handle | None
    requester: Handle | None
    parent: Hex | None
    opened_at: Ms
    alias: str
    accept_paths: list[str] = field(default_factory=list)
    accept_cmds: list[str] = field(default_factory=list)
    after: list[Hex] = field(default_factory=list)
    files_hint: list[str] = field(default_factory=list)
    bug: Hex | None = None
    children: list[Hex] = field(default_factory=list)
    claim: Hex | None = None
    work_done_at: Ms | None = None
    work_done_link: Hex | None = None
    done_at: Ms | None = None
    done_link: Hex | None = None
    links: list[list[Any]] = field(default_factory=list)          # [state, seq, linked_at] with Gud-Task = id
    no_change: dict[str, Any] | None = None                        # accepted no-change for this item
    accept_passes: list[list[Any]] = field(default_factory=list)  # [link, at] passes received while green
    requester_accepts: list[Ms] = field(default_factory=list)
    lapses: int = 0
    ready_for: Hex | None = None                                   # work-done link a task.ready was sent for
    cited_link: Hex | None = None
    cited_author: Handle | None = None
    offered_to: Handle | None = None
    offer_until: Ms | None = None
    red_link: Hex | None = None
    scope: list[str] = field(default_factory=list)
    declared: dict[str, Any] | None = None                         # done declaration {id, by, at}

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        if d["declared"] is None:
            del d["declared"]            # snapshots without a declaration keep their v1 bytes
        return d

    @classmethod
    def from_json(cls, d: Mapping) -> _Task:
        return cls(**dict(d))


@dataclass
class _Claim:
    id: Hex
    latest: Hex
    task: Hex
    holder: Handle
    files: list[str]
    after: list[Hex]
    won_at: Ms
    last_activity: Ms
    lapses_at: Ms
    status: str

    def state(self) -> ClaimState:
        return ClaimState(self.id, self.latest, self.task, self.holder, tuple(self.files), tuple(self.after),
                          self.won_at, self.last_activity, self.lapses_at, self.status)  # type: ignore[arg-type]

    def to_json(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, d: Mapping) -> _Claim:
        return cls(**dict(d))


_CLAIM_ROLES = frozenset({"agent", "human"})


class TaskBoard:
    """Incremental task/claim state machine. Fed in log order by core.replay."""

    def __init__(self, cfg_at: Callable[[Ms], RepoConfig], members_at: Callable[[Ms], Members]) -> None:
        self._cfg_at = cfg_at
        self._members_at = members_at
        self._tasks: dict[Hex, _Task] = {}
        self._claims: dict[Hex, _Claim] = {}
        self._roots = 0
        self._bugs: dict[Hex, Hex] = {}
        self._link_seq: dict[Hex, int] = {}
        self._tip: tuple[Hex, int] | None = None
        self._red = False
        self._overlaps: set[str] = set()
        self._changed_tasks: set[Hex] = set()
        self._changed_claims: set[Hex] = set()

    # ------------------------------------------------------------ helpers

    def _agents(self, at: Ms) -> list[Handle]:
        members = self._members_at(at)
        return [h for h, m in sorted(members.by_handle.items()) if m.role == "agent"]

    def _testers(self, at: Ms) -> list[Handle]:
        members = self._members_at(at)
        return [h for h, m in sorted(members.by_handle.items()) if m.role == "tester"]

    def _event(self, type: str, at: Ms, concerns: Iterable[Handle | None], data: Mapping[str, Any],
               causer: Handle | None = None) -> Event:
        who = {h for h in concerns if h and h != causer}
        return make_event(type, at, who, data)

    def _touch_task(self, tid: Hex) -> None:
        self._changed_tasks.add(tid)

    def _touch_claim(self, cid: Hex) -> None:
        self._changed_claims.add(cid)

    def take_changes(self) -> tuple[list[Hex], list[Hex]]:
        """Ids of tasks and claims changed since the last call (sorted); used by core.replay deltas."""
        tasks, claims = sorted(self._changed_tasks), sorted(self._changed_claims)
        self._changed_tasks.clear()
        self._changed_claims.clear()
        return tasks, claims

    def root_of(self, task: Hex) -> Hex:
        seen = set()
        cur = task
        while cur in self._tasks and self._tasks[cur].parent and cur not in seen:
            seen.add(cur)
            cur = self._tasks[cur].parent  # type: ignore[assignment]
        return cur

    def requester_of(self, task: Hex) -> Handle | None:
        root = self._tasks.get(self.root_of(task))
        return root.requester if root else None

    def has(self, task: Hex) -> bool:
        return task in self._tasks

    def _ancestors(self, task: Hex) -> list[Hex]:
        out = []
        cur: Hex | None = task
        while cur is not None and cur in self._tasks and cur not in out:
            out.append(cur)
            cur = self._tasks[cur].parent
        return out

    def _nested(self, a: Hex, b: Hex) -> bool:
        """One item is the other or one of its ancestors."""
        return a in self._ancestors(b) or b in self._ancestors(a)

    def _descendants(self, task: Hex) -> list[Hex]:
        out, stack = [], [task]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(reversed(self._tasks[cur].children) if cur in self._tasks else ())
        return out

    def _criteria(self, t: _Task) -> bool:
        return bool(t.accept_paths or t.accept_cmds)

    def _state(self, t: _Task) -> TaskState:
        if t.done_at is not None:
            status = "done"
        elif t.work_done_at is not None:
            status = "work_done"
        elif t.claim is not None:
            status = "claimed"
        else:
            status = "open"
        no_change: list[tuple[Hex, str]] = []
        for tid in self._descendants(t.id):
            nc = self._tasks[tid].no_change
            if nc is not None:
                no_change.append((tid, nc["reason"]))
        return TaskState(
            id=t.id, alias=t.alias, kind=t.kind, title=t.title, description=t.description,  # type: ignore[arg-type]
            author=t.author, requester=t.requester, parent=t.parent, opened_at=t.opened_at,
            accept_paths=tuple(t.accept_paths), accept_cmds=tuple(t.accept_cmds), after=tuple(t.after),
            files_hint=tuple(t.files_hint), bug=t.bug, status=status, claim=t.claim,  # type: ignore[arg-type]
            children=tuple(t.children), work_done_at=t.work_done_at, work_done_link=t.work_done_link,
            done_at=t.done_at, done_link=t.done_link, no_change=tuple(no_change),
            cited_link=t.cited_link, offered_to=t.offered_to, offer_until=t.offer_until,
            declared=t.declared["id"] if t.declared else None, declared_by=t.declared["by"] if t.declared else None,
        )

    def tasks(self) -> Mapping[Hex, TaskState]:
        return {tid: self._state(t) for tid, t in self._tasks.items()}

    def task(self, tid: Hex) -> TaskState | None:
        t = self._tasks.get(tid)
        return None if t is None else self._state(t)

    def claims(self) -> Mapping[Hex, ClaimState]:
        return {cid: c.state() for cid, c in self._claims.items()}

    def claim(self, cid: Hex) -> ClaimState | None:
        c = self._claims.get(cid)
        return None if c is None else c.state()

    def active_claim(self, task: Hex) -> ClaimState | None:
        t = self._tasks.get(task)
        if t is None or t.claim is None:
            return None
        return self._claims[t.claim].state()

    def _active(self) -> list[_Claim]:
        return [c for _, c in sorted(self._claims.items()) if c.status == "active"]

    def holders(self, task: Hex) -> list[Handle]:
        """Holders of active claims on `task` and its descendants."""
        out = set()
        for tid in self._descendants(task):
            t = self._tasks.get(tid)
            if t is not None and t.claim is not None:
                out.add(self._claims[t.claim].holder)
        return sorted(out)

    def claims_overlapping(self, paths: Sequence[str], tip_paths: Iterable[str]) -> tuple[ClaimState, ...]:
        """Active claims whose scope matches any of `paths`."""
        out = []
        for c in self._active():
            if any(scope_matches(c.files, p) for p in paths):
                out.append(c.state())
        return tuple(out)

    # ------------------------------------------------------------ opening

    def add_task(self, t: TaskInput) -> list[Event]:
        """§16.1/§16.5/§16.8. A subtask counts only while its author holds the active claim on its parent."""
        if t.id in self._tasks:
            return []
        if t.kind == "subtask":
            parent = self._tasks.get(t.parent or "")
            if parent is None or parent.claim is None:
                return []
            claim = self._claims[parent.claim]
            if claim.holder != t.author or claim.status != "active":
                return []
        if t.bug is not None:
            if t.bug in self._bugs:
                return []
            self._bugs[t.bug] = t.id
        if t.kind == "subtask":
            parent = self._tasks[t.parent]  # type: ignore[index]
            alias = f"{parent.alias}.{len(parent.children) + 1}"
            parent.children.append(t.id)
            self._touch_task(parent.id)
        else:
            self._roots += 1
            alias = f"T{self._roots}"
        task = _Task(
            id=t.id, kind=t.kind, title=t.title, description=t.description, author=t.author,
            requester=t.requester, parent=t.parent if t.kind == "subtask" else None, opened_at=t.received,
            alias=alias, accept_paths=list(t.accept_paths), accept_cmds=list(t.accept_cmds),
            after=list(t.after), files_hint=list(t.files_hint), bug=t.bug,
        )
        self._tasks[t.id] = task
        self._touch_task(t.id)
        criteria = {"paths": list(t.accept_paths), "cmds": list(t.accept_cmds)}
        if t.kind == "subtask":
            event = self._event("subtask.posted", t.received, self._agents(t.received), {
                "subtask": t.id, "alias": alias, "parent": t.parent, "title": t.title,
                "after": list(t.after), "files": list(t.files_hint), "criteria": criteria,
            }, causer=t.author)
        else:
            event = self._event("task.opened", t.received, self._agents(t.received), {
                "task": t.id, "alias": alias, "title": t.title, "requester": t.requester,
                "criteria": criteria, "bug": t.bug,
            }, causer=t.author)
        # A parent whose work was done gains an open child: its work is no longer done.
        events = [event]
        if t.kind == "subtask":
            events += self._refresh(t.received)
        return events

    # ------------------------------------------------------------ claims

    def _lease(self, at: Ms) -> Ms:
        return self._cfg_at(at).lease

    def _overlap_events(self, claim: _Claim, at: Ms, tip_paths: Sequence[str]) -> list[Event]:
        events = []
        for other in self._active():
            if other.id == claim.id or other.holder == claim.holder:
                continue
            if self._nested(claim.task, other.task):
                continue                 # a task's holder split it: claims on its own subtasks are the plan
            if not globs_overlap(claim.files, other.files, tip_paths):
                continue
            pair = sorted((claim.id, other.id))
            key = "\n".join(pair)
            if key in self._overlaps:
                continue
            self._overlaps.add(key)
            a, b = (claim, other) if pair[0] == claim.id else (other, claim)
            events.append(self._event("claim.overlap", at, (a.holder, b.holder), {
                "claims": pair, "holders": [a.holder, b.holder], "globs": [list(a.files), list(b.files)],
                "tasks": [a.task, b.task],
            }))
        return events

    def add_claim(self, c: ClaimInput, tip_paths: Iterable[str]) -> list[Event]:
        """§16.3: win, void (claim.lost), update or release; overlap on win and update."""
        paths = list(tip_paths)
        if c.revises is not None:
            for active in self._active():
                if active.holder == c.author and c.revises in (active.id, active.latest):
                    declared = self._declare(active, c)
                    if c.release:
                        active.status = "released"
                        active.latest = c.id
                        self._tasks[active.task].claim = None
                        self._touch_claim(active.id)
                        self._touch_task(active.task)
                        return self._refresh(c.received) if declared else []
                    active.files = list(c.files)
                    active.after = list(c.after)
                    active.latest = c.id
                    active.last_activity = c.received
                    active.lapses_at = c.received + self._lease(c.received)
                    self._touch_claim(active.id)
                    events = self._overlap_events(active, c.received, paths)
                    return events + (self._refresh(c.received) if declared else [])
            if c.release:
                return []
        task = self._tasks.get(c.task)
        if c.id in self._claims:
            return []
        if task is None:
            return []
        lost_to: Hex | None = None
        if task.done_at is not None:
            lost_to = task.claim
            reason = "done"
        elif task.claim is not None:
            lost_to = task.claim
            reason = "claimed"
        elif task.kind == "fix" and task.offer_until is not None and c.received < task.offer_until \
                and c.author != task.offered_to:
            reason = "offered"
        else:
            reason = ""
        if reason:
            self._claims[c.id] = _Claim(c.id, c.id, c.task, c.author, list(c.files), list(c.after),
                                        c.received, c.received, c.received, "void")
            self._touch_claim(c.id)
            return [self._event("claim.lost", c.received, (c.author,), {
                "claim": c.id, "task": c.task, "active_claim": lost_to, "why": reason,
            })]
        claim = _Claim(c.id, c.id, c.task, c.author, list(c.files), list(c.after), c.received, c.received,
                       c.received + self._lease(c.received), "active")
        self._claims[c.id] = claim
        task.claim = c.id
        self._touch_claim(c.id)
        self._touch_task(task.id)
        events = [self._event("claim.won", c.received, (c.author, self.requester_of(c.task)), {
            "claim": c.id, "task": c.task, "holder": c.author, "files": list(c.files),
        })]
        return events + self._overlap_events(claim, c.received, paths)

    def _declare(self, claim: _Claim, c: ClaimInput) -> bool:
        """§16.6 done declaration: a claim update (or release) from the holder with `Gud-Done: yes`,
        sent while the holder has no live push for the item, on an item without valid subtasks."""
        task = self._tasks[claim.task]
        if not c.done or c.has_live_push or task.children or task.kind == "fix" or task.done_at is not None:
            return False
        if task.declared is not None:
            return False
        task.declared = {"id": c.id, "by": c.author, "at": c.received}
        self._touch_task(task.id)
        return True

    def add_activity(self, a: Activity) -> None:
        """§16.3 lease renewal by a push or comment from the holder on the item or a descendant."""
        for tid in self._ancestors(a.task):
            t = self._tasks[tid]
            if t.claim is None:
                continue
            claim = self._claims[t.claim]
            if claim.holder == a.by and claim.status == "active" and a.at > claim.last_activity:
                claim.last_activity = a.at
                claim.lapses_at = a.at + self._lease(a.at)
                self._touch_claim(claim.id)

    # ------------------------------------------------------------ no-change

    def add_no_change(self, n: NoChangeInput) -> list[Event]:
        """§16.6 no-change acceptance (tree checks are done by the engine and passed in)."""
        task = self._tasks.get(n.task)
        if task is None or task.claim is None or task.done_at is not None:
            return []
        claim = self._claims[task.claim]
        if claim.holder != n.author or claim.status != "active" or n.received <= claim.won_at:
            return []
        if not n.paths_match or n.has_live_push or task.children:
            return []
        task.no_change = {"id": n.id, "reason": n.reason, "at": n.received, "files": list(claim.files),
                          "tip": n.tip}
        self._touch_task(task.id)
        return self._refresh(n.received)

    # ------------------------------------------------------------ links and tests

    def on_link(self, link: LinkState, touched: Sequence[str], tip_paths: Iterable[str]) -> list[Event]:
        """Work done for `Gud-Task`, no-change drops, scope.exceeded and claim removal at a new link."""
        at = link.linked_at
        self._link_seq[link.state] = link.seq
        self._tip = (link.state, link.seq)
        events: list[Event] = []
        if link.task is not None and link.task in self._tasks:
            task = self._tasks[link.task]
            task.links.append([link.state, link.seq, at])
            self._touch_task(task.id)
        for tid, t in list(self._tasks.items()):
            if t.no_change is None or self._tasks[self.root_of(tid)].done_at is not None:
                continue
            if any(scope_matches(t.no_change["files"], p) for p in touched):
                t.no_change = None
                for anc in self._ancestors(tid):
                    a = self._tasks[anc]
                    if a.parent is None and a.done_at is not None:
                        break
                    a.done_at = a.done_link = a.work_done_at = a.work_done_link = a.ready_for = None
                    self._touch_task(anc)
        for claim in self._active():
            if claim.holder == link.author:
                continue
            if link.task is not None and claim.task in self._ancestors(link.task):
                continue                 # work on a subtask of the claimed item is inside the plan
            inside = [p for p in touched if scope_matches(claim.files, p)]
            if inside:
                events.append(self._event("scope.exceeded", at, (link.author, claim.holder), {
                    "push": link.push, "claim": claim.id, "paths": inside, "link": link.state,
                }))
        members = self._members_at(at)
        for claim in self._active():
            m = members.get(claim.holder)
            if m is None or m.role not in _CLAIM_ROLES:
                claim.status = "removed"
                self._tasks[claim.task].claim = None
                self._touch_claim(claim.id)
                self._touch_task(claim.task)
        return events + self._refresh(at)

    def on_accept_test(self, task: Hex, link: Hex, result: Literal["pass", "fail"], at: Ms, red: bool) -> list[Event]:
        t = self._tasks.get(task)
        if t is None or result != "pass" or red or link not in self._link_seq:
            return []
        t.accept_passes.append([link, at])
        self._touch_task(task)
        return self._refresh(at)

    def on_requester_accept(self, task: Hex, by: Handle, at: Ms) -> list[Event]:
        t = self._tasks.get(task)
        if t is None or t.parent is not None or t.requester is None or by != t.requester:
            return []
        t.requester_accepts.append(at)
        self._touch_task(task)
        return self._refresh(at)

    # ------------------------------------------------------------ red tip

    def on_red(self, red: RedState, at: Ms, cited_author: Handle | None, touched: Sequence[str]) -> list[Event]:
        """§15.5: opens the fix task at red start, or re-cites it when bisect finds first_bad."""
        self._red = red.red
        if not red.red or red.red_link is None:
            return []
        tid = fix_task_id(red.red_link)
        cited = red.first_bad or red.red_link
        members = self._members_at(at)
        author_member = members.get(cited_author) if cited_author else None
        sponsor = author_member.sponsor if author_member else None
        events: list[Event] = []
        task = self._tasks.get(tid)
        if task is None:
            self._roots += 1
            task = _Task(
                id=tid, kind="fix", title=f"fix red tip at {red.red_link[:12]}", description="", author=None,
                requester=sponsor, parent=None, opened_at=at, alias=f"T{self._roots}",
                cited_link=cited, cited_author=cited_author, offered_to=cited_author,
                offer_until=at + self._cfg_at(at).fix_offer, red_link=red.red_link, scope=list(touched),
            )
            self._tasks[tid] = task
            self._touch_task(tid)
            events.append(self._event("fix.opened", at, self._agents(at), {
                "task": tid, "alias": task.alias, "cited_link": cited, "author": cited_author,
                "offered_to": cited_author, "offer_until": task.offer_until, "red_link": red.red_link,
                "files": list(touched),
            }))
        elif task.cited_link != cited and task.done_at is None:
            task.cited_link = cited
            task.cited_author = cited_author
            task.offered_to = cited_author
            task.requester = sponsor
            task.scope = list(touched)
            self._touch_task(tid)
        return events

    def on_green(self, at: Ms) -> list[Event]:
        """Red ended: open fix tasks are done, and tasks waiting on green may finish."""
        self._red = False
        events: list[Event] = []
        for tid, t in self._tasks.items():
            if t.kind == "fix" and t.done_at is None:
                t.work_done_at = at
                t.work_done_link = self._tip[0] if self._tip else None
                events += self._finish(t, at)
        return events + self._refresh(at)

    # ------------------------------------------------------------ time

    def advance(self, to: Ms) -> list[Event]:
        """Lease lapses up to `to` (in lapse-time order), then done transitions."""
        events: list[Event] = []
        while True:
            due = [c for c in self._active() if c.lapses_at <= to]
            if not due:
                break
            claim = min(due, key=lambda c: (c.lapses_at, c.id))
            claim.status = "lapsed"
            at = claim.lapses_at
            task = self._tasks[claim.task]
            task.claim = None
            task.lapses += 1
            self._touch_claim(claim.id)
            self._touch_task(task.id)
            events.append(self._event("claim.lapsed", at, self._agents(at) + [claim.holder], {
                "claim": claim.id, "task": claim.task, "holder": claim.holder, "lapsed_at": at,
            }))
            if task.lapses == self._cfg_at(at).lease_thrash:
                events.append(self.escalate(claim.task, "lease-thrash", at, None))
        return events

    def next_time(self) -> Ms | None:
        times = [c.lapses_at for c in self._active()]
        return min(times) if times else None

    def escalate(self, task: Hex, reason: str, at: Ms, by: Handle | None) -> Event:
        """§16.7 escalated event without a chain effect (lease thrash, budget, stuck)."""
        members = self._members_at(at)
        sponsors = set()
        for h in self.holders(task) + ([by] if by else []):
            m = members.get(h)
            if m is not None and m.sponsor:
                sponsors.add(m.sponsor)
        deciders = sorted({h for h in [self.requester_of(task)] if h} | sponsors)
        return self._event("escalated", at, deciders, {
            "escalation": None, "task": task, "reason": reason, "pushes": [], "positions": [],
            "deciders": deciders, "deadline": None, "default": None, "by": by,
        }, causer=by)

    # ------------------------------------------------------------ done (§16.6)

    def _finish(self, t: _Task, at: Ms) -> list[Event]:
        t.done_at = at
        t.done_link = self._tip[0] if self._tip else None
        self._touch_task(t.id)
        holders = self.holders(t.id)
        for tid in self._descendants(t.id):
            sub = self._tasks[tid]
            if sub.claim is not None and tid == t.id:
                claim = self._claims[sub.claim]
                claim.status = "done"
                sub.claim = None
                self._touch_claim(claim.id)
        state = self._state(t)
        # every agent follows the board: a done item can unblock work that is `after` it (§19.2)
        return [self._event("task.done", at, [t.requester] + holders + self._agents(at), {
            "task": t.id, "alias": t.alias, "link": t.done_link,
            "no_change": [[sid, reason] for sid, reason in state.no_change],
        })]

    def _work_done(self, t: _Task) -> bool:
        if t.kind == "fix":
            return False
        if t.children:
            return all(self._tasks[c].done_at is not None for c in t.children)
        if t.no_change is not None or t.declared is not None:
            return True
        # a subtask is small by construction: a link carrying its id is its work; a root task's work is
        # done only when its holder says so (claim update with Gud-Done, or no-change), §16.6
        return t.parent is not None and bool(t.links)

    def _refresh(self, at: Ms) -> list[Event]:
        """Re-derive work-done and done for every open item, children before parents."""
        events: list[Event] = []
        for tid in reversed(list(self._tasks)):
            t = self._tasks[tid]
            if t.done_at is not None or t.kind == "fix":
                continue
            if self._work_done(t):
                if t.work_done_at is None:
                    t.work_done_at = at
                    if not t.children and t.links and t.declared is None and t.parent is not None:
                        t.work_done_link = t.links[0][0]
                    else:
                        t.work_done_link = self._tip[0] if self._tip else None
                    self._touch_task(tid)
            elif t.work_done_at is not None:
                t.work_done_at = None
                t.work_done_link = None
                t.ready_for = None
                self._touch_task(tid)
                continue
            else:
                continue
            if self._red:
                continue
            if self._criteria(t):
                base = self._link_seq.get(t.work_done_link or "", -1)
                ok = any(self._link_seq.get(link, -1) >= base for link, _ in t.accept_passes)
                needs = "criteria"
            elif t.parent is None and t.requester is not None and self._is_human(t.requester, at):
                ok = any(a > (t.work_done_at or 0) for a in t.requester_accepts)
                needs = "accept"
            else:
                ok, needs = True, ""
            if ok:
                events += self._finish(t, at)
            elif t.ready_for != t.work_done_link:
                t.ready_for = t.work_done_link
                self._touch_task(tid)
                who = self._testers(at) if needs == "criteria" else [t.requester]
                events.append(self._event("task.ready", at, who, {
                    "task": tid, "alias": t.alias, "link": t.work_done_link, "needs": needs,
                }))
        return events

    def _is_human(self, handle: Handle, at: Ms) -> bool:
        m = self._members_at(at).get(handle)
        return m is not None and m.role == "human"

    # ------------------------------------------------------------ persistence

    def to_json(self) -> dict:
        return {
            "tasks": [t.to_json() for t in self._tasks.values()],
            "claims": [c.to_json() for c in self._claims.values()],
            "roots": self._roots,
            "bugs": [[k, v] for k, v in sorted(self._bugs.items())],
            "link_seq": [[k, v] for k, v in self._link_seq.items()],
            "tip": list(self._tip) if self._tip else None,
            "red": self._red,
            "overlaps": sorted(self._overlaps),
        }

    @classmethod
    def from_json(cls, data: dict, cfg_at, members_at) -> TaskBoard:
        board = cls(cfg_at, members_at)
        for d in data["tasks"]:
            t = _Task.from_json(d)
            board._tasks[t.id] = t
        for d in data["claims"]:
            c = _Claim.from_json(d)
            board._claims[c.id] = c
        board._roots = data["roots"]
        board._bugs = {k: v for k, v in data["bugs"]}
        board._link_seq = {k: v for k, v in data["link_seq"]}
        board._tip = tuple(data["tip"]) if data["tip"] else None  # type: ignore[assignment]
        board._red = data["red"]
        board._overlaps = set(data["overlaps"])
        return board
