"""Chain events (SPEC §19): the typed results of a replay step, their JSON form and routing.

Replay (`gud.core.replay`) derives every §19.2 event, with its id, decision time and the members
it concerns. Concerns follow §19.2 with no lead anywhere: open work (`task.opened`,
`subtask.posted`, `claim.lapsed`, `fix.opened`) goes to every agent so anyone can claim it;
work in progress goes to the **claim holders** (`push.received` to holders of claims on the
push's task and of claims whose scope it touches, `objected` to the holder of the push's task
claim, `claim.overlap` and `scope.exceeded` to both holders); the tip is followed by every agent
(`linked`, `task.done`, `tip.red`, `tip.green`), so an agent with no claim still sees work land. This module adds what the host and members need on top: the §19.1 wire body, schema
checks, routing keys for warm agent sessions, and delivery records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from gud.core.envelope import EVENT_KEYS, EnvelopeError, event_id, is_handle, is_hex64, jcs, make_event  # re-exported
from gud.core.types import EVENT_TYPES, Event, Handle, Hex, Member  # re-exported

__all__ = [
    "EVENT_TYPES", "Event", "EVENT_KEYS", "event_id", "make_event", "DATA_FIELDS", "HUMAN_NOTIFY_TYPES",
    "to_json", "from_json", "event_dict", "event_from_dict", "validate", "thread_key", "THREAD_KEY",
    "recipients", "Delivery", "JournalItem",
]

# §19.2 "Data" column, as field names in the event data (SPEC §19.2: each id-key part is a
# data field). Replay may carry more fields than these.
DATA_FIELDS: Mapping[str, tuple[str, ...]] = {
    "push.received": ("push", "parent", "author", "task", "files", "forecast", "window_closes_at", "needs_test"),
    "review.requested": ("push", "candidate", "owner", "why", "closes_at"),
    "objected": ("push", "objection", "by", "reason", "evidence"),
    "commented": ("target", "comment", "by", "text"),
    "approved": ("push", "by", "approve"),
    "held": ("push", "by", "closes_at", "hold"),
    "test.passed": ("scope", "target", "tree", "tester", "log", "test"),
    "test.failed": ("scope", "target", "tree", "tester", "log", "test"),
    "window.closing": ("candidate", "closes_at"),
    "superseded": ("push", "by_link"),
    "push.rejected": ("push", "status", "reason"),
    "carried": ("push", "candidate", "onto", "files"),
    "linked": ("link", "push", "seq", "author", "task", "files", "carried_from"),
    "task.opened": ("task", "alias", "title", "requester", "criteria"),
    "subtask.posted": ("subtask", "alias", "parent", "title", "after", "files"),
    "claim.won": ("claim", "task", "holder", "files"),
    "claim.lost": ("claim", "task", "active_claim"),
    "claim.lapsed": ("claim", "task", "holder", "lapsed_at"),
    "claim.overlap": ("claims", "holders", "globs"),
    "scope.exceeded": ("push", "claim", "paths"),
    "task.ready": ("task", "needs", "link"),
    "task.done": ("task", "link", "no_change"),
    "escalated": ("escalation", "reason", "pushes", "positions", "deciders", "deadline", "default"),
    "escalation.resolved": ("escalation", "chosen", "declined", "by"),
    "tip.red": ("red_link", "last_green"),
    "tip.bisected": ("red_link", "first_bad", "author", "fix_task"),
    "tip.green": ("link", "red_link"),
    "fix.opened": ("task", "alias", "cited_link", "author", "offered_to", "offer_until"),
    "host.fork": ("message", "cited", "log_hash", "proof"),
}

# §19.4: human notifications go To the human and Cc the repo (logged, deduped by Gud-Event).
HUMAN_NOTIFY_TYPES: frozenset[str] = frozenset({"task.ready", "task.done", "escalated"})


def event_dict(event: Event, repo: str | None = None) -> dict[str, Any]:
    """The §19.1 object (with `repo` when given)."""
    out: dict[str, Any] = {"id": event.id, "type": event.type, "at": event.at,
                           "concerns": list(event.concerns), "data": _plain(event.data)}
    if repo is not None:
        out["repo"] = repo
    return out


def event_from_dict(obj: Mapping[str, Any]) -> Event:
    """Inverse of `event_dict`. Raises ValueError on a malformed object."""
    try:
        eid, etype, at = obj["id"], obj["type"], obj["at"]
        concerns, data = obj.get("concerns", []), obj.get("data", {})
    except (KeyError, TypeError) as exc:
        raise ValueError(f"event object lacks {exc}") from exc
    if not is_hex64(eid) or etype not in EVENT_KEYS or not isinstance(at, int) or isinstance(at, bool):
        raise ValueError("event id, type or at is malformed")
    if not isinstance(concerns, list) or not all(isinstance(h, str) for h in concerns) or not isinstance(data, dict):
        raise ValueError("event concerns or data is malformed")
    return Event(id=eid, type=etype, at=at, concerns=tuple(concerns), data=data)


def to_json(event: Event, repo: str) -> bytes:
    """§19.1 body: RFC 8785 canonical JSON of {id, type, at, repo, concerns, data}."""
    return jcs(event_dict(event, repo))


def from_json(body: bytes) -> tuple[str, Event]:
    """(repo, event) from a §19.1 body. Raises ValueError."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("event body is not JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("repo"), str):
        raise ValueError("event body must be an object with a repo")
    return obj["repo"], event_from_dict(obj)


def validate(event: Event) -> None:
    """The event matches §19: known type, the §19.2 data fields present, JSON-compatible data
    (integers only), sorted handle concerns, and an id that recomputes from its key. Raises
    ValueError."""
    if event.type not in DATA_FIELDS:
        raise ValueError(f"unknown event type {event.type!r}")
    missing = [f for f in DATA_FIELDS[event.type] if f not in event.data]
    if missing:
        raise ValueError(f"{event.type}: data lacks {', '.join(missing)}")
    try:
        jcs(_plain(event.data))
    except ValueError as exc:
        raise ValueError(f"{event.type}: data is not canonical JSON: {exc}") from exc
    if list(event.concerns) != sorted(set(event.concerns)) or not all(is_handle(h) for h in event.concerns):
        raise ValueError(f"{event.type}: concerns must be sorted, distinct handles")
    try:
        expect = make_event(event.type, event.at, event.concerns, event.data).id
    except EnvelopeError as exc:
        raise ValueError(str(exc)) from exc
    if expect != event.id:
        raise ValueError(f"{event.type}: id does not match its key")


_PUSH_THREAD = frozenset({
    "push.received", "review.requested", "objected", "approved", "held", "window.closing", "superseded",
    "push.rejected", "carried", "linked", "scope.exceeded",
})
_TASK_THREAD = frozenset({
    "task.opened", "task.ready", "task.done", "claim.won", "claim.lost", "claim.lapsed", "fix.opened",
})


def thread_key(event: Event, roots: Mapping[Hex, Hex] | None = None) -> str:
    """Session routing key (PLAN Phase 1b "negotiation sessions"): `push:<negotiation root>` for
    events on a push thread, `task:<id>` for task and claim events, `thread:<target>` for
    comments, else `event:<id>`. `roots` maps push ids to their negotiation roots (from
    `ProposalState.negotiation`); without it the push id itself is the key."""
    d = event.data
    roots = roots or {}
    if event.type in _PUSH_THREAD and isinstance(d.get("push"), str):
        return "push:" + roots.get(d["push"], d["push"])
    if event.type in ("test.passed", "test.failed") and d.get("scope") in ("push", "evidence"):
        target = d.get("target")
        if isinstance(target, str):
            return "push:" + roots.get(target, target)
    if event.type in _TASK_THREAD and isinstance(d.get("task"), str):
        return "task:" + d["task"]
    if event.type == "subtask.posted" and isinstance(d.get("parent"), str):
        return "task:" + d["parent"]
    if event.type == "commented" and isinstance(d.get("target"), str):
        target = d["target"]
        return ("push:" + roots[target]) if target in roots else "thread:" + target
    if event.type in ("escalated", "escalation.resolved") and isinstance(d.get("escalation"), str):
        return "escalation:" + d["escalation"]
    return "event:" + event.id


THREAD_KEY: Callable[[Event], str] = thread_key


def recipients(event: Event, members: Any) -> tuple[Member, ...]:
    """The current members (`gud.core.config.Members` at the tip) an event is delivered to: its
    concerns that are still members, never the host itself. Removed members get nothing."""
    out = []
    for handle in event.concerns:
        member = members.get(handle) if members is not None else None
        if member is not None and member.role != "host":
            out.append(member)
    return tuple(out)


@dataclass(frozen=True)
class Delivery:
    handle: Handle
    channel: Literal["webhook", "sse", "email"]
    url: str | None = None


@dataclass(frozen=True)
class JournalItem:
    cursor: int                                      # per-member monotonic journal seq
    event: Event
    handle: Handle = ""                              # the member whose journal holds it


def _plain(value: Any) -> Any:
    """Tuples to lists, recursively (JSON shape)."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value
