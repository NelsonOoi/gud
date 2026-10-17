"""Task state, derived from `task`, `subtask`, `task-done` and `notify` emails plus the chain.

A later `subtask` email with the same id overrides the assignee (and the title,
description, and message id). That is how the lead reassigns a stale subtask.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .types import Email
from .wire import parse_body

TASK_ID = re.compile(r"^T(\d+)$")


@dataclass
class Subtask:
    id: str
    assignee: str
    title: str
    description: str
    status: str = "open"           # open | proposed | landed
    link_id: str | None = None
    message_id: str = ""


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

    def to_json(self) -> dict:
        return asdict(self)


def _title(subject: str) -> str:
    s = re.sub(r"^(re:\s*)+", "", subject, flags=re.I).strip()
    s = re.sub(r"^\[gud\]\s*", "", s)
    return re.sub(r"^T\d+(\.\d+)?:?\s*", "", s).strip()


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
                              e.received_at, message_id=e.message_id)
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
            else:
                st = Subtask(tid, p.get("Gud-Assignee") or "", _title(e.subject), p.text,
                             message_id=e.message_id)
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
            if s.assignee == handle and s.status != "landed"]
