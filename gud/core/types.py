"""Plain v1 data types shared by every module. See docs/INTERFACES.md and SPEC.md."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import unquote

Hex = str
Ms = int
Handle = str
Mode = Literal["100644", "100755", "120000"]
Role = Literal["agent", "human", "tester", "console", "host"]

ZERO: Hex = "0" * 64
MODES: frozenset[str] = frozenset({"100644", "100755", "120000"})
REGULAR_MODES: frozenset[str] = frozenset({"100644", "100755"})
ROLES: frozenset[str] = frozenset({"agent", "human", "tester", "console", "host"})
VOTER_ROLES: frozenset[str] = frozenset({"agent", "human"})
KINDS: frozenset[str] = frozenset({
    "push", "revert", "object", "approve", "hold", "comment", "test", "claim", "task", "subtask",
    "no-change", "checkpoint", "join", "notify", "blob",
})
SIGNED_KINDS: frozenset[str] = KINDS - {"blob"}


def _split_list(value: str | None) -> tuple[str, ...]:
    """SPEC §0 list parsing: split on `,`, trim ASCII spaces, drop empties, percent-decode."""
    if not value:
        return ()
    items = []
    for raw in value.split(","):
        item = raw.strip(" ")
        if item:
            items.append(unquote(item, errors="replace"))
    return tuple(items)


@dataclass(frozen=True)
class Record:
    name: str
    args: tuple[str, ...]
    data: bytes


@dataclass(frozen=True)
class Envelope:
    headers: tuple[tuple[str, str], ...]
    records: tuple[Record, ...]
    sig: str | None = None

    def get(self, name: str) -> str | None:
        for key, value in self.headers:
            if key == name:
                return value
        return None

    def get_list(self, name: str) -> tuple[str, ...]:
        return _split_list(self.get(name))

    @property
    def kind(self) -> str | None:
        return self.get("Gud-Kind")

    @property
    def author(self) -> Handle | None:
        return self.get("Gud-Author")

    @property
    def sent_ms(self) -> Ms | None:
        value = self.get("Gud-Sent")
        if value is None or not value.isascii() or not value.isdigit():
            return None
        return int(value) * 1000

    @property
    def text(self) -> str:
        for record in self.records:
            if record.name == "text":
                return record.data.decode("utf-8", errors="replace")
        return ""

    def records_named(self, name: str) -> tuple[Record, ...]:
        return tuple(record for record in self.records if record.name == name)


@dataclass(frozen=True)
class Seen:
    seq: int
    entry_hash: Hex

    def render(self) -> str:
        return f"{self.seq}:{self.entry_hash}"


@dataclass(frozen=True)
class Email:
    """One message at the repo address, as the host stored it."""
    gud_id: Hex
    raw_mime: bytes
    message_id: str
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    in_reply_to: str | None
    references: tuple[str, ...]
    auth_results: tuple[str, ...]
    text: str
    envelope: Envelope | None
    received_at: Ms | None = None
    provider_id: str | None = None

    @property
    def signed(self) -> bool:
        return self.envelope is not None and self.envelope.kind in SIGNED_KINDS


@dataclass(frozen=True)
class Member:
    handle: Handle
    role: Role
    address: str
    public_key: str | None
    sponsor: Handle | None = None

    @property
    def votes(self) -> bool:
        return self.role in VOTER_ROLES


@dataclass(frozen=True)
class LogEntry:
    seq: int
    prev: Hex
    time: Ms
    type: Literal["open", "mail", "tick", "handover"]
    fields: tuple[tuple[str, str], ...]
    sig: str
    hash: Hex

    def field(self, name: str) -> str | None:
        for key, value in self.fields:
            if key == name:
                return value
        return None


@dataclass(frozen=True)
class Evidence:
    path: str
    sha256: Hex
    size: int
    inline: bytes | None


@dataclass(frozen=True)
class LinkState:
    """One link. Holds roots into the object store, never a tree dict."""
    state: Hex
    seq: int
    push: Hex
    kind: Literal["push", "revert"]
    parent: Hex | None
    named_parent: Hex | None
    author: Handle
    tree_root: Hex
    tree_hash: Hex
    blame_root: Hex
    rules_root: Hex
    received_at: Ms
    linked_at: Ms
    task: Hex | None
    intent: str
    files: tuple[str, ...]
    carried_from: Hex | None = None
    revert_of: Hex | None = None


ProposalStatus = Literal[
    "window", "objected", "escalated", "needs_approval", "needs_quorum", "testing",
    "pending_blobs", "qualified", "parked", "orphan", "carried",
    "vetoed", "tests_failed", "declined", "expired", "superseded", "stale",
    "bad_sig", "malformed", "conflict", "bad_config",
]
LIVE_STATUSES: frozenset[str] = frozenset({
    "window", "objected", "escalated", "needs_approval", "needs_quorum", "testing",
    "pending_blobs", "qualified", "parked", "orphan", "carried",
})
DEAD_STATUSES: frozenset[str] = frozenset({
    "vetoed", "tests_failed", "declined", "expired", "superseded", "stale",
    "bad_sig", "malformed", "conflict", "bad_config",
})


@dataclass(frozen=True)
class ObjectionState:
    id: Hex
    by: Handle
    reason: str
    received: Ms
    evidence: Evidence | None
    status: Literal["blocking", "advisory", "pending_verdict", "confirmed", "refuted", "cleared", "void"]
    answered_by: Hex | None
    episode: int


@dataclass(frozen=True)
class ProposalState:
    """One candidate (§12.3) or, for dead/orphan pushes, the push itself."""
    id: Hex
    push: Hex
    named_parent: Hex
    parent: Hex | None
    author: Handle
    kind: Literal["push", "revert"]
    received_at: Ms
    status: ProposalStatus
    reason: str
    task: Hex | None
    revises: Hex | None
    negotiation: Hex
    files: tuple[str, ...]
    tree_hash: Hex | None
    window_open: Ms | None
    window_closes_at: Ms | None
    park_at: Ms | None
    line_owners: tuple[Handle, ...]
    askers: tuple[Handle, ...]
    declared: tuple[tuple[str, str, tuple[Handle, ...]], ...]
    quorum_needed: int | None
    quorum_have: int | None
    members_rule: bool
    approvals: tuple[tuple[Handle, Ms], ...]
    objections: tuple[ObjectionState, ...]
    held_by: Handle | None
    test_needed: bool
    test_result: Literal["pass", "fail"] | None
    pending_blobs: tuple[Hex, ...]
    escalation: Hex | None
    qualified_at: Ms | None


@dataclass(frozen=True)
class TaskState:
    id: Hex
    alias: str
    kind: Literal["task", "subtask", "fix"]
    title: str
    description: str
    author: Handle | None
    requester: Handle | None
    parent: Hex | None
    opened_at: Ms
    accept_paths: tuple[str, ...]
    accept_cmds: tuple[str, ...]
    after: tuple[Hex, ...]
    files_hint: tuple[str, ...]
    bug: Hex | None
    status: Literal["open", "claimed", "work_done", "done"]
    claim: Hex | None
    children: tuple[Hex, ...]
    work_done_at: Ms | None
    work_done_link: Hex | None
    done_at: Ms | None
    done_link: Hex | None
    no_change: tuple[tuple[Hex, str], ...]
    cited_link: Hex | None = None
    offered_to: Handle | None = None
    offer_until: Ms | None = None
    declared: Hex | None = None          # the holder's done declaration (claim with Gud-Done), §16.6
    declared_by: Handle | None = None
    # how work became done: "subtasks", "link", "declared", "no_change", "quiet" or "green" (fix tasks), §16.6
    work_done_by: str | None = None
    # done by quiescence (§16.6): latest activity on the item and the time the rule would hold, while it applies
    active_at: Ms | None = None
    quiet_until: Ms | None = None


@dataclass(frozen=True)
class ClaimState:
    id: Hex
    latest: Hex
    task: Hex
    holder: Handle
    files: tuple[str, ...]
    after: tuple[Hex, ...]
    won_at: Ms
    last_activity: Ms
    lapses_at: Ms
    status: Literal["active", "lapsed", "released", "done", "removed", "void"]


@dataclass(frozen=True)
class EscalationState:
    id: Hex
    reason: Literal["mutual", "rounds"]
    pushes: tuple[Hex, ...]
    opened_at: Ms
    deadline: Ms
    deciders: tuple[Handle, ...]
    status: Literal["open", "resolved"]
    chosen: Hex | None
    declined: tuple[Hex, ...]
    decided_by: Handle | Literal["default"] | None
    decided_at: Ms | None


@dataclass(frozen=True)
class RedState:
    red: bool
    red_link: Hex | None
    red_since: Ms | None
    last_green: Hex | None
    first_bad: Hex | None
    fix_task: Hex | None


EVENT_TYPES: tuple[str, ...] = (
    "push.received", "review.requested", "objected", "commented", "approved", "held",
    "test.passed", "test.failed", "window.closing", "superseded", "push.rejected", "carried",
    "linked", "task.opened", "subtask.posted", "claim.won", "claim.lost", "claim.lapsed",
    "claim.overlap", "scope.exceeded", "task.ready", "task.done", "escalated",
    "escalation.resolved", "tip.red", "tip.bisected", "tip.green", "fix.opened", "host.fork",
)


@dataclass(frozen=True)
class Event:
    id: Hex
    type: str
    at: Ms
    concerns: tuple[Handle, ...]
    data: Mapping[str, Any]


@dataclass(frozen=True)
class Chain:
    """Read-only view of replay state at time t. Trees and blame via core.replay.ChainReader."""
    t: Ms
    log_head: Seen
    links: tuple[LinkState, ...]
    proposals: Mapping[Hex, ProposalState]
    tasks: Mapping[Hex, TaskState]
    claims: Mapping[Hex, ClaimState]
    escalations: Mapping[Hex, EscalationState]
    red: RedState

    @property
    def tip(self) -> LinkState | None:
        return self.links[-1] if self.links else None

    def link(self, state: Hex) -> LinkState | None:
        for link in self.links:
            if link.state == state:
                return link
        return None

    def push_status(self, push_id: Hex) -> ProposalState | None:
        """The push's current candidate: the one on the most recent parent link, else the push itself."""
        seqs = {link.state: link.seq for link in self.links}
        best: tuple[int, str] | None = None
        found: ProposalState | None = None
        for key in sorted(self.proposals):
            prop = self.proposals[key]
            if prop.push != push_id:
                continue
            rank = (seqs.get(prop.parent, -1) if prop.parent else -1, prop.id)
            if best is None or rank > best:
                best, found = rank, prop
        return found


@dataclass(frozen=True)
class ChainDelta:
    """What one Replayer step changed."""
    t_before: Ms
    t_after: Ms
    log_head: Seen
    new_links: tuple[LinkState, ...]
    changed_proposals: tuple[ProposalState, ...]
    changed_tasks: tuple[TaskState, ...]
    changed_claims: tuple[ClaimState, ...]
    changed_escalations: tuple[EscalationState, ...]
    red: RedState | None
    events: tuple[Event, ...]
    next_wakeup: Ms | None
    missing_mail: tuple[Hex, ...]


class ObjectStoreLike(Protocol):
    def get(self, key: Hex) -> bytes: ...
    def put(self, data: bytes) -> Hex: ...
    def has(self, key: Hex) -> bool: ...
