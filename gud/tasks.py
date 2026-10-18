"""Task state, derived from `task`, `subtask`, `task-done`, `no-change` and `notify` emails plus the chain.

A later `subtask` email with the same id overrides the assignee (and the title,
description, and message id). That is how the lead reassigns a stale subtask.
A `no-change` from the current assignee completes a subtask that needs nothing,
until a later link changes the files that subtask names.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .sig import verify
from .types import Email
from .wire import parse_body

# A push that can still link. no-change waits it out so task-done does not race the edit.
_IN_FLIGHT = frozenset({"window", "testing", "needs_approval", "needs_quorum", "qualified"})

TASK_ID = re.compile(r"^T(\d+)$")


@dataclass
class Subtask:
    id: str
    assignee: str
    title: str
    description: str
    status: str = "open"           # open | proposed | landed | no-change
    link_id: str | None = None
    message_id: str = ""
    after: list[str] = field(default_factory=list)   # Gud-After: subtasks that land first
    reason: str = ""               # one line, when status is no-change
    assigned_at: float = 0.0
    closed_at: float | None = None # when the accepted no-change arrived


@dataclass
class Task:
    id: str
    title: str
    description: str
    requester: str
    created_at: float
    status: str = "open"           # open | in_progress | done
    subtasks: list[Subtask] = field(default_factory=list)
    done_link: str | None = None
    notified_at: float | None = None
    message_id: str = ""
    bug: str | None = None          # message id of the bug report, when the ticket came from one
    source: str | None = None       # message id of the plain human email the lead turned into this task

    def to_json(self) -> dict:
        return asdict(self)


def _title(subject: str) -> str:
    s = re.sub(r"^(re:\s*)+", "", subject, flags=re.I).strip()
    s = re.sub(r"^\[gud\]\s*", "", s)
    return re.sub(r"^T\d+(\.\d+)?:?\s*", "", s).strip()


def _after(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def derive(chain) -> list[Task]:
    """Tasks oldest first. `chain` is a gud.replay.Chain."""
    tasks: dict[str, Task] = {}
    subs: dict[str, Subtask] = {}
    landed = {l.task: l.message_id for l in chain.links if l.task}
    proposed = {p.task for p in chain.proposals.values() if p.task}
    for e in chain.emails:
        p = parse_body(e.body)
        kind, tid = p.kind, p.get("Gud-Task")
        if not tid:
            continue
        if kind == "task" and tid not in tasks:
            tasks[tid] = Task(tid, _title(e.subject), p.text, p.get("Gud-Requester") or "",
                              e.received_at, message_id=e.message_id, bug=p.get("Gud-Bug") or None,
                              source=p.get("Gud-Source") or None)
        elif kind == "subtask":
            parent = tasks.get(p.get("Gud-Parent-Task") or "")
            if parent is None or not tid:
                continue
            if tid in subs:
                # Latest assignment wins. The subtask stays one row on the parent task.
                st = subs[tid]
                assignee = p.get("Gud-Assignee")
                if assignee:
                    st.assignee = assignee
                title = _title(e.subject)
                if title:
                    st.title = title
                if p.text:
                    st.description = p.text
                st.message_id = e.message_id
                st.after = _after(p.get("Gud-After"))
                st.assigned_at = e.received_at
            else:
                st = Subtask(tid, p.get("Gud-Assignee") or "", _title(e.subject), p.text,
                             message_id=e.message_id, after=_after(p.get("Gud-After")),
                             assigned_at=e.received_at)
                subs[tid] = st
                parent.subtasks.append(st)
        elif kind == "task-done" and tid in tasks:
            tasks[tid].done_link = p.get("Gud-Tip")
            tasks[tid].status = "done"
        elif kind == "notify" and tid in tasks and tasks[tid].notified_at is None:
            tasks[tid].notified_at = e.received_at
    for st in subs.values():
        if st.id in landed:
            st.status, st.link_id = "landed", landed[st.id]
            continue
        accepted = _no_change_reason(chain, st)
        if accepted is not None:
            st.status = "no-change"
            st.reason, st.closed_at = accepted
        elif st.id in proposed:
            st.status = "proposed"
    for t in tasks.values():
        if t.status != "done" and (t.subtasks or t.id in landed):
            t.status = "in_progress"
    return list(tasks.values())


def next_task_id(emails: list[Email]) -> str:
    n = 0
    for e in emails:
        p = parse_body(e.body)
        m = TASK_ID.match(p.get("Gud-Task") or "") if p.kind == "task" else None
        if m:
            n = max(n, int(m.group(1)))
    return f"T{n + 1}"


def open_subtasks_for(tasks: list[Task], handle: str) -> list[Subtask]:
    return [s for t in tasks if t.status != "done" for s in t.subtasks
            if s.assignee == handle and s.status not in {"landed", "no-change"}]


def _safe_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    return ".." not in path.split("/")


def _mentions(path: str, text: str) -> bool:
    if len(path) < 2:
        return False
    pattern = r"(?<![A-Za-z0-9_./-])" + re.escape(path) + r"(?![A-Za-z0-9_./-])"
    return re.search(pattern, text) is not None


def named_paths(title: str, description: str, tree: dict) -> list[str]:
    """Paths a subtask is about: its `Files:` line, plus any cited-tree path named in the text.

    Empty means the subtask named nothing, so the whole tree is relevant.
    """
    text = f"{title}\n{description}"
    chosen: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        path = path.strip().strip("`\"'")
        if not _safe_path(path) or path in seen:
            return
        seen.add(path)
        chosen.append(path)

    for line in text.splitlines():
        if line.lower().startswith("files:"):
            for part in line.split(":", 1)[1].split(","):
                add(part)
    for path in sorted(tree):
        if _mentions(path, text):
            add(path)
    return chosen


def _still_matches(cited, tip, sub: Subtask) -> bool:
    """True when nothing the subtask names changed between the cited link and the tip."""
    named = named_paths(sub.title, sub.description, cited.tree)
    if not named:
        return cited.tree_hash == tip.tree_hash
    return all(cited.tree.get(path) == tip.tree.get(path) for path in named)


def _signing_member(chain, author: str, cited):
    """The key that signed the check: the cited link's member, else the tip's."""
    groups = [cited.members]
    if chain.tip is not None:
        groups.append(chain.tip.members)
    for members in groups:
        member = members.get(author)
        if member is not None and member.public_key and member.role != "human":
            return member
    return None


def _signed_by(email, parsed, member) -> bool:
    if member is None or not email.sender_verified:
        return False
    if email.from_addr.lower() != member.address.lower():
        return False
    return verify(member.public_key, parsed.trailers, parsed.diff)


def _in_flight(chain, sub: Subtask) -> bool:
    for prop in chain.proposals.values():
        if prop.task == sub.id and prop.author == sub.assignee and prop.status in _IN_FLIGHT:
            return True
    return False


def _no_change_reason(chain, sub: Subtask) -> tuple[str, float] | None:
    """`(reason, received_at)` of the latest `no-change` that still holds, or None.

    Accepted only from the current assignee, signed, citing a link, received at or
    after the current assignment, with the named files still equal to that link.
    """
    if chain.tip is None or _in_flight(chain, sub):
        return None
    best = None
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.kind != "no-change" or parsed.get("Gud-Task") != sub.id:
            continue
        if parsed.get("Gud-Author") != sub.assignee or email.received_at < sub.assigned_at:
            continue
        reason = " ".join((parsed.get("Gud-Reason") or parsed.text or "").split())[:300]
        if not reason:
            continue
        cited = chain.by_id.get(parsed.get("Gud-Tip") or "")
        if cited is None or not _signed_by(email, parsed, _signing_member(chain, sub.assignee, cited)):
            continue
        if not _still_matches(cited, chain.tip, sub):
            continue
        row = (email.received_at, email.message_id, reason)
        if best is None or row[:2] > best[:2]:
            best = row
    if best is None:
        return None
    return best[2], best[0]
