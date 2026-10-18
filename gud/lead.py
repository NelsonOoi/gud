"""The lead agent. Split tasks, reassign stale subtasks, close finished tasks, vote as @lead."""

from __future__ import annotations

import json
import re
import time

from . import compose, notify
from .bugs import (
    bugs_address_for, derive_bugs, is_bug_report, lead_noted, requester_for, review_requested, ticket_text,
)
from .compose import Identity
from .inbox import Gate, Inbox
from .replay import replay
from .tasks import Task, derive, next_task_id
from .wire import parse_body

# Keyword order matters: "lockout" and "protected interface" also mention rate limits.
DEMO_SPLITS: dict[str, list[tuple[str, str, list[str]]]] = {
    "lockout": [
        (
            "Replace login with a lockout counter",
            "gud-conflict. Rewrite auth.login so five wrong passwords lock the account and later "
            "calls return LoginResult(False, None, \"locked\") until revoke_user_sessions clears it. "
            "This push replaces the rate-limit lines the current owner wrote; they will object. "
            "The revised push keeps the rate-limit check and adds the lockout counter.",
            ["auth.py", "tests/test_auth.py"],
        ),
    ],
    "protected interface": [
        (
            "Add RateLimitDecision",
            "Edit api/interfaces.py only. Add a frozen dataclass RateLimitDecision with allowed: bool, "
            "remaining: int, and retry_after: float. Add decide(self, key: str) -> RateLimitDecision "
            "to the RateLimiter protocol. The path is explicit, so the owner has to approve.",
            ["api/interfaces.py"],
        ),
    ],
    "rate limit": [
        (
            "Rate-limit login",
            "In auth.py login, call ratelimit.allow(f\"login:{username}\") before checking the password. "
            "When it returns false, return LoginResult(False, None, \"rate limited\") and do not start "
            "a session. Add a test that a second login inside an empty bucket is rejected.",
            ["auth.py", "tests/test_auth.py"],
        ),
        (
            "Bill overage in charge",
            "In billing.py charge, read usage_of for the period, set overage_cents from "
            "overage_cents(requests, plan), and set total_cents to amount_cents + overage_cents. "
            "Keep rejecting a missing account and a negative amount. A charge of 0 cents on the free "
            "plan after 103 requests in the period invoices overage_cents == 6.",
            ["billing.py", "tests/test_billing.py"],
        ),
        (
            "Map 429 and meter charges",
            "In api/handlers.py handle_login, map login reason \"rate limited\" to status 429. "
            "On a successful charge, call billing.meter_request for that user so the next invoice "
            "sees the call.",
            ["api/handlers.py", "tests/test_api.py"],
        ),
        (
            "Add penalize",
            "In ratelimit.py add penalize(key) on TokenBucket and the module wrapper that sets that "
            "key's tokens to zero. Leave allow and remaining as the tests describe them. Add a test "
            "that penalize makes the next allow fail.",
            ["ratelimit.py", "tests/test_penalize.py"],
        ),
    ],
}

_POOL_FIRST = ("@sub-1", "@sub-2", "@sub-3", "@sub-4")
_WAIT_APPROVE = {"window", "needs_approval", "needs_quorum", "testing"}


def _hay(task: Task) -> str:
    return f"{task.title}\n{task.description}".lower().replace("-", " ")


def _hint_files(description: str) -> list[str]:
    files = []
    for line in description.splitlines():
        if line.lower().startswith("files:"):
            files.extend(part.strip() for part in line.split(":", 1)[1].split(",") if part.strip())
    return files


def _describe(description: str, files: list[str], task: Task | None = None) -> str:
    """The subtask email body. It carries the parent task, so the assignee's inbox has the whole brief."""
    text = description.strip()
    if files and "files:" not in text.lower():
        text += "\n\nFiles: " + ", ".join(files)
    if task is not None and task.description and task.description.strip() not in text:
        text += f"\n\nPart of {task.id}: {task.title}\n{task.description.strip()}"
    return text


def _items(raw) -> list[tuple[str, str, list[str], list[int]]]:
    """Planner output as (title, description, files, after); `after` is 1-based indices."""
    out = []
    for item in raw:
        title, description, files = item[0], item[1], list(item[2])
        after = [int(i) for i in (item[3] if len(item) > 3 else []) if str(i).isdigit()]
        out.append((title, description, files, after))
    return out


class ScriptedPlanner:
    """Split a task by matching title and description keywords against a dict of scripts."""

    def __init__(self, splits: dict[str, list[tuple]] | None = None):
        self.splits = DEMO_SPLITS if splits is None else splits

    def split(self, task: Task, tip_tree: dict) -> list[tuple]:
        """(title, description, files) items, with an optional 4th `after` list of 1-based indices."""
        hay = _hay(task)
        for keyword, items in self.splits.items():
            if keyword.lower().replace("-", " ") in hay:
                return [(item[0], item[1], list(item[2]), *item[3:]) for item in items]
        return [(task.title or task.id, task.description or task.title, [])]


class LLMPlanner:
    """Ask OpenRouter for JSON subtasks. A bad response becomes one subtask."""

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def split(self, task: Task, tip_tree: dict) -> list[tuple[str, str, list[str]]]:
        paths = sorted(tip_tree)[:200]
        prompt = (
            f"Task {task.id}: {task.title}\n{task.description}\n\n"
            "Repo paths:\n" + "\n".join(paths)
        )
        try:
            raw = _chat(self.api_key, self.model, [
                {"role": "system", "content": (
                    "Split a software task into subtasks for a coding swarm. "
                    "Reply with JSON only: {\"subtasks\": [{\"title\": str, \"description\": str, "
                    "\"files\": [str], \"after\": [int]}]}. Use 2 to 4 subtasks.\n"
                    "- Copy every concrete value the task gives (names, prices, addresses, numbers, "
                    "required elements) into the description of the subtask that needs it, word for word.\n"
                    "- Give each file to exactly one subtask. Put a shared file (a stylesheet, a nav "
                    "used by every page) in one subtask.\n"
                    "- `after` lists the 1-based numbers of subtasks that must land first, e.g. a "
                    "subtask that creates files others edit. Leave it empty when work can run in parallel."
                )},
                {"role": "user", "content": prompt},
            ])
            items = _parse_plan(raw)
            if items:
                return items
        except Exception as exc:
            print(f"lead planner fallback: {exc}", flush=True)
        return [(task.title or task.id, task.description or task.title, [])]


class Lead:
    """Watch the mailbox and advance every task. Every decision is re-derived from the mail."""

    def __init__(self, mailbox, identity: Identity, window: float, planner, clock=time.time,
                 stale_after: float = 120, pool: list[str] | None = None, reviewer=None,
                 gate: Gate | None = None, bugs_address: str | None = None, bug_reviewer=None):
        """`reviewer` (an LLMCoder) makes the lead review pushes to lines it owns instead of
        approving them; without one the lead signs off on its own lines (the scripted demo).

        `bug_reviewer` may accept or reject mail to `bugs_address`. Without one, a report
        waits for a member to reply. `bugs_address=""` turns intake off; None uses bugs@<domain>.
        """
        self.mailbox = mailbox
        self.identity = identity
        self.window = window
        self.planner = planner
        self.clock = clock
        self.stale_after = stale_after
        self.pool = pool
        self.reviewer = reviewer
        self.bug_reviewer = bug_reviewer
        self.bugs_address = (
            bugs_address_for(getattr(mailbox, "repo_address", "")) if bugs_address is None
            else bugs_address.strip().lower()
        )
        self.inbox = Inbox(mailbox, identity.address, mailbox.repo_address)
        self.gate = gate or Gate()
        self._starved: set[tuple[str, str]] = set()
        # What this lead already sent, so a step before that mail is delivered does not repeat it.
        self._split_done: set[str] = set()
        self._finished: set[str] = set()
        self._voted: set[str] = set()
        self._review_failed: dict[str, float] = {}
        self._bug_failed: dict[str, float] = {}
        self._bugs_seen: set[str] = set()
        self._bugs_waiting = False
        self._bugs_error: str | None = None
        self._reassigned: set[tuple[str, str]] = set()
        self._open_tasks: set[str] = set()

    def step(self) -> list:
        mail = self.inbox.read()
        bug_rows = self._bug_inbox()
        fresh_bugs = [
            email for email in bug_rows
            if email.message_id not in self._bugs_seen and is_bug_report(email, self.bugs_address)
        ]
        for email in mail:
            parsed = parse_body(email.body)
            if parsed.kind == "task" and parsed.get("Gud-Task"):
                self._open_tasks.add(parsed.get("Gud-Task"))
        busy = bool(self._open_tasks - self._finished) or self._bugs_waiting
        if not self.gate.due(bool(mail) or bool(fresh_bugs), busy):
            return []
        chain, now = self._chain()
        tip = chain.tip
        if tip is None:
            return []
        for email in bug_rows:
            self._bugs_seen.add(email.message_id)
        tasks = derive(chain)
        for task in tasks:
            if task.status != "done":
                self._open_tasks.add(task.id)
            else:
                self._open_tasks.discard(task.id)
        sent = []
        sent += self._triage(chain, tip, bug_rows)
        task_mail = [email for email in sent if parse_body(email.body).kind == "task"]
        if task_mail:
            chain.emails = [*chain.emails, *task_mail]
            for email in task_mail:
                chain.by_message[email.message_id] = email
            tasks = derive(chain)
        sent += self._split(chain, tasks, tip)
        sent += self._reassign(chain, tasks, tip, now)
        sent += self._finish(chain, tasks, tip)
        sent += self._approve(chain, tip)
        return sent

    def _bug_inbox(self) -> list:
        if not self.bugs_address:
            return []
        try:
            rows = self.mailbox.list(recipient=self.bugs_address)
        except Exception as exc:
            message = str(exc)
            if message != self._bugs_error:
                self._bugs_error = message
                print(f"lead: bugs mailbox: {message}", flush=True)
            return []
        self._bugs_error = None
        return rows

    def _triage(self, chain, tip, bug_rows) -> list:
        """Open a task for an accepted bug report, or tell the sender it was rejected."""
        if not self.bugs_address:
            self._bugs_waiting = False
            return []
        reports = derive_bugs(bug_rows, chain.emails, tip.members, self.bugs_address)
        sent: list = []
        known = list(chain.emails)
        waiting = False
        for report in reports:
            if report.status == "accepted":
                self._note_opened(report, report.task_id or "", known, sent)
                continue
            if report.status == "rejected":
                self._note_rejected(report, known, sent)
                continue
            if report.verdict == "approve":
                self._open_from_report(tip, report, known, sent)
                continue
            if self.bug_reviewer is None:
                self._ask_for_review(tip, report, known, sent)
                waiting = True
                continue
            failed_at = self._bug_failed.get(report.id)
            if failed_at is not None and time.monotonic() - failed_at < 20:
                waiting = True
                continue
            action, reason = self._ask_bug_reviewer(report, tip)
            if action not in {"approve", "reject"}:
                self._bug_failed[report.id] = time.monotonic()
                waiting = True
                print(f"lead: bug review abstain {report.id} {reason}", flush=True)
                continue
            self._bug_failed.pop(report.id, None)
            verdict = self._send_verdict(tip, report, action, reason)
            sent.append(verdict)
            known.append(verdict)
            if action == "approve":
                self._open_from_report(tip, report, known, sent, reason)
            else:
                report.reason = reason
                self._note_rejected(report, known, sent)
                print(f"lead: reject bug {report.id} {reason}", flush=True)
        self._bugs_waiting = waiting
        return sent

    def _ask_bug_reviewer(self, report, tip) -> tuple[str, str]:
        try:
            action, reason = self.bug_reviewer.review_bug(report.subject, report.text, sorted(tip.tree)[:80])
        except Exception as exc:
            print(f"lead: bug review failed: {exc}", flush=True)
            return "abstain", str(exc)[:200]
        action = (action or "").strip().lower()
        if action == "accept":
            action = "approve"
        if action == "object":
            action = "reject"
        reason = (reason or "").strip().split("\n", 1)[0][:300]
        if action not in {"approve", "reject"}:
            return "abstain", reason or "no verdict"
        return action, reason or ("Approved." if action == "approve" else "Rejected.")

    def _send_verdict(self, tip, report, action: str, reason: str):
        if action == "approve":
            body = compose.approve_body(self.identity, report.id, reason)
        else:
            body = compose.object_body(self.identity, report.id, reason)
        return compose.send(
            self.mailbox, self.identity, tip.members, f"Re: {report.subject}", body,
            in_reply_to=report.id, cc_handles=[],
        )

    def _open_from_report(self, tip, report, known: list, sent: list, reason: str = "") -> None:
        email = self._open_ticket(tip, report, known)
        sent.append(email)
        known.append(email)
        task_id = parse_body(email.body).get("Gud-Task") or ""
        self._note_opened(report, task_id, known, sent)
        print(f"lead: ticket {task_id} from bug {report.id} {report.subject}"
              f"{(' ' + reason) if reason else ''}", flush=True)

    def _open_ticket(self, tip, report, existing: list):
        task_id = next_task_id(existing)
        requester = requester_for(tip.members, report.from_addr, self.identity.handle)
        body = compose.task_body(
            self.identity, task_id, requester, ticket_text(report), bug=report.id,
        )
        return compose.send(
            self.mailbox, self.identity, tip.members, f"[gud] {task_id}: {report.subject}", body,
            in_reply_to=report.id, cc_handles=compose.role_handles(tip.members, "lead"),
        )

    def _note_opened(self, report, task_id: str, known: list, sent: list) -> None:
        if not task_id or lead_noted(known, report.id, self.identity.handle):
            return
        note = self._tell(report, f"Opened {task_id}: {report.subject}.")
        if note is not None:
            sent.append(note)
            known.append(note)

    def _note_rejected(self, report, known: list, sent: list) -> None:
        if lead_noted(known, report.id, self.identity.handle):
            return
        reason = (report.reason or "Rejected.").strip()
        if "no ticket" not in reason.lower():
            reason = f"{reason}\n\nNo ticket was opened."
        note = self._tell(report, reason)
        if note is not None:
            sent.append(note)
            known.append(note)

    def _ask_for_review(self, tip, report, known: list, sent: list) -> None:
        """Mail the sponsor once, so a person can reply approve or reject on the thread."""
        if review_requested(known, report.id, self.identity.handle):
            return
        handle = requester_for(tip.members, report.from_addr, self.identity.handle)
        human = tip.members.get(handle)
        if human is None or human.role != "human" or human.address.lower() == report.from_addr.lower():
            return
        text = f"{ticket_text(report)}\n\nReply approve or reject."
        body = compose.notify_body(self.identity, text, target=report.id, ask="review")
        email = self.mailbox.send(
            from_addr=self.identity.address, to=human.address, cc=[self.mailbox.repo_address],
            subject=f"Review: {report.subject}", body=body, in_reply_to=report.id,
        )
        sent.append(email)
        known.append(email)
        print(f"lead: review request {report.id} -> {human.handle}", flush=True)

    def _tell(self, report, text: str):
        to = (report.from_addr or "").strip()
        skip = {self.identity.address.lower(), self.mailbox.repo_address.lower(), self.bugs_address}
        if not to or to.lower() in skip or "@" not in to:
            return None
        body = compose.notify_body(self.identity, text, target=report.id)
        return self.mailbox.send(
            from_addr=self.identity.address, to=to, cc=[self.mailbox.repo_address],
            subject=f"Re: {report.subject}", body=body, in_reply_to=report.id,
        )

    def _chain(self):
        now = self.clock()
        emails = _listed(self.mailbox)
        return replay(emails, self.window, now), now

    def _pool(self, members: dict) -> list[str]:
        if self.pool is not None:
            return [h for h in self.pool if h in members]
        handles = [h for h in _POOL_FIRST if h in members and members[h].role == "agent"]
        for handle, member in sorted(members.items()):
            if member.role == "agent" and handle not in handles:
                handles.append(handle)
        return handles

    def _split(self, chain, tasks, tip) -> list:
        pool = self._pool(tip.members)
        busy = _busy(tasks)
        start = sum(len(task.subtasks) for task in tasks)
        sent = []
        for task in tasks:
            # Only an open task with no subtasks is split, once: the lead remembers what it sent
            # because on real mail its own subtasks show up in the repo seconds later.
            if task.status == "done" or task.subtasks or not task.message_id or task.id in self._split_done:
                continue
            self._split_done.add(task.id)
            items = _items(self.planner.split(task, tip.tree))
            ids: list[str] = []
            for index, (title, description, files, after) in enumerate(items):
                text = _describe(description, files, task)
                avoid = _owners_of(tip, files) if "gud-conflict" in description else set()
                handle, start = _pick(pool, busy | avoid, start)
                if handle is None:
                    if (task.id, title) not in self._starved:
                        self._starved.add((task.id, title))
                        print(f"lead: no free worker for {task.id} {title}", flush=True)
                    break
                sub_id = _next_sub_id(task, sent)
                deps = [ids[i - 1] for i in after if 1 <= i <= index and i - 1 < len(ids)]
                email = self._send_subtask(tip, task, title, text, handle, sub_id, deps)
                ids.append(sub_id)
                sent.append(email)
                busy.add(handle)
                note = f" after {', '.join(deps)}" if deps else ""
                print(f"lead: subtask {sub_id} -> {handle} {title}{note}", flush=True)
        return sent

    def _send_subtask(self, tip, task, title, description, assignee, sub_id, after=None):
        body = compose.subtask_body(self.identity, sub_id, task.id, assignee, description, after=after)
        return compose.send(
            self.mailbox, self.identity, tip.members, f"[gud] {sub_id}: {title}", body,
            in_reply_to=task.message_id, cc_handles=[assignee],
        )

    def _reassign(self, chain, tasks, tip, now: float) -> list:
        pool = self._pool(tip.members)
        busy = _busy(tasks)
        start = sum(len(task.subtasks) for task in tasks)
        sent = []
        for task in tasks:
            if task.status == "done":
                continue
            for sub in task.subtasks:
                if sub.status == "landed":
                    continue
                if _pushes_by(chain, sub.id, sub.assignee):
                    continue
                assigned = _assigned_at(chain, sub.id)
                ready = _ready_at(chain, sub)
                if assigned is None or ready is None or now - max(assigned, ready) < self.stale_after:
                    continue
                files = _hint_files(sub.description)
                avoid = {sub.assignee} | (_owners_of(tip, files) if "gud-conflict" in sub.description else set())
                handle, start = _pick(pool, busy | avoid, start)
                if handle is None or handle == sub.assignee or (sub.id, handle) in self._reassigned:
                    continue
                self._reassigned.add((sub.id, handle))
                email = self._send_subtask(tip, task, sub.title, sub.description, handle, sub.id, sub.after)
                sent.append(email)
                busy.add(handle)
                print(f"lead: reassign {sub.id} {sub.assignee} -> {handle}", flush=True)
        return sent

    def _finish(self, chain, tasks, tip) -> list:
        sent = []
        for task in tasks:
            ready = bool(task.subtasks) and all(sub.status == "landed" for sub in task.subtasks)
            if task.status != "done" and ready and task.id not in self._finished:
                self._finished.add(task.id)
                summary = _done_summary(task)
                body = compose.task_done_body(self.identity, task.id, tip.message_id, summary)
                email = compose.send(
                    self.mailbox, self.identity, tip.members, f"[gud] {task.id} done: {task.title}",
                    body, in_reply_to=task.message_id,
                    cc_handles=sorted({sub.assignee for sub in task.subtasks if sub.assignee}),
                )
                sent.append(email)
                print(f"lead: task-done {task.id} tip {tip.message_id}", flush=True)
                note = self._notify(task, tip, summary)
                if note is not None:
                    sent.append(note)
            elif task.status == "done" and task.notified_at is None and task.id not in self._finished:
                self._finished.add(task.id)
                note = self._notify(task, tip, _done_summary(task))
                if note is not None:
                    sent.append(note)
        return sent

    def _notify(self, task, tip, summary):
        human = _human(tip, task.requester) or _human_any(self, task.requester)
        if human is None or human.role != "human":
            print(f"lead: no human member for requester {task.requester}", flush=True)
            return None
        links = [sub.link_id for sub in task.subtasks if sub.link_id]
        email = notify.notify_requester(
            self.mailbox, self.identity, human, task.id, task.title, tip, summary, links,
        )
        print(f"lead: notify {human.handle} {task.id}", flush=True)
        return email

    def _approve(self, chain, tip) -> list:
        me = self.identity.handle
        sent = []
        for prop in chain.proposals.values():
            if prop.author == me or prop.status not in _WAIT_APPROVE:
                continue
            if prop.message_id in self._voted or _already(chain, me, prop.message_id):
                continue
            parent = chain.by_id.get(prop.parent)
            if parent is None or me not in parent.members or not parent.members[me].votes:
                continue
            if not _lead_should_approve(prop, me):
                continue
            target = chain.by_message.get(prop.message_id)
            if target is None:
                continue
            parsed = parse_body(target.body)
            if "gud-bad-edit" in parsed.diff or "gud-bad-edit" in parsed.text:
                continue
            action, reason = "approve", "Approved."
            quorum = prop.protected_rule == "quorum" or prop.status == "needs_quorum"
            if self.reviewer is not None and not quorum:
                failed_at = self._review_failed.get(prop.message_id)
                if failed_at is not None and time.monotonic() - failed_at < 20:
                    continue
                from .worker import review_context
                action, reason = self.reviewer.review(parsed.diff, review_context(chain, prop, parsed, me))
                if action not in {"approve", "object"}:
                    self._review_failed[prop.message_id] = time.monotonic()
                    continue
            if action == "object":
                body = compose.object_body(self.identity, prop.message_id, reason)
            else:
                body = compose.approve_body(self.identity, prop.message_id, reason)
            email = compose.send(
                self.mailbox, self.identity, parent.members, f"Re: {target.subject}", body,
                in_reply_to=prop.message_id, cc_handles=compose.reply_cc(parent.members, prop.author),
            )
            self._voted.add(prop.message_id)
            sent.append(email)
            print(f"lead: {action} {prop.message_id} {prop.subject} {reason if action == 'object' else ''}".rstrip(),
                  flush=True)
        return sent


def _lead_should_approve(prop, me: str) -> bool:
    """@lead signs off on lines it owns and casts a quorum vote."""
    if prop.protected_rule == "quorum" or prop.status == "needs_quorum":
        return True
    if me in (prop.affected_owners or []):
        return True
    if prop.protected_rule == "explicit" and not prop.affected_owners:
        return True
    return False


def _busy(tasks: list[Task]) -> set[str]:
    busy = set()
    for task in tasks:
        if task.status == "done":
            continue
        for sub in task.subtasks:
            if sub.status != "landed" and sub.assignee:
                busy.add(sub.assignee)
    return busy


def _pick(pool: list[str], busy: set[str], start: int) -> tuple[str | None, int]:
    if not pool:
        return None, start
    for offset in range(len(pool)):
        idx = (start + offset) % len(pool)
        handle = pool[idx]
        if handle not in busy:
            return handle, idx + 1
    return None, start


def _next_sub_id(task: Task, pending: list) -> str:
    highest = 0
    for sub in task.subtasks:
        highest = max(highest, _suffix(task.id, sub.id))
    for email in pending:
        highest = max(highest, _suffix(task.id, parse_body(email.body).get("Gud-Task") or ""))
    return f"{task.id}.{highest + 1}"


def _suffix(parent: str, sub_id: str) -> int:
    prefix = parent + "."
    if not sub_id.startswith(prefix):
        return 0
    try:
        return int(sub_id[len(prefix):])
    except ValueError:
        return 0


def _owners_of(tip, files: list[str]) -> set[str]:
    owners = set()
    for path in files:
        for owner, _link in tip.blame.get(path, []):
            if owner:
                owners.add(owner)
    return owners


def _assigned_at(chain, sub_id: str) -> float | None:
    found = None
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.kind == "subtask" and parsed.get("Gud-Task") == sub_id:
            found = email.received_at
    return found


def _ready_at(chain, sub) -> float | None:
    """When every `Gud-After` subtask had landed: 0 with none, None while one is still open."""
    if not sub.after:
        return 0.0
    landed = {link.task: link.linked_at or link.received_at for link in chain.links if link.task}
    if any(dep not in landed for dep in sub.after):
        return None
    return max(landed[dep] for dep in sub.after)


def _pushes_by(chain, sub_id: str, author: str) -> list:
    found = []
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.kind == "push" and parsed.get("Gud-Task") == sub_id and parsed.get("Gud-Author") == author:
            found.append(email)
    return found


def _already(chain, author: str, target: str) -> bool:
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.get("Gud-Author") == author and parsed.get("Gud-Target") == target and parsed.kind in {
            "approve", "object",
        }:
            return True
    return False


def _human(tip, handle: str):
    if not handle:
        return None
    handle = handle if handle.startswith("@") else f"@{handle}"
    return tip.members.get(handle)


def _human_any(self, handle: str):
    handle = handle if str(handle).startswith("@") else f"@{handle}"
    chain, _now = self._chain()
    for link in reversed(chain.links):
        member = link.members.get(handle)
        if member is not None:
            return member
    return None


def _done_summary(task: Task) -> str:
    lines = [task.title.strip() or task.id]
    for sub in task.subtasks:
        lines.append(f"{sub.id} {sub.assignee} {sub.link_id or ''}".rstrip())
    return "\n".join(lines)


def _listed(mailbox):
    import json as _json
    last = None
    for _ in range(5):
        try:
            return mailbox.list(recipient=mailbox.repo_address)
        except _json.JSONDecodeError as exc:
            last = exc
            time.sleep(0.02)
    raise last


def _chat(api_key: str, model: str, messages: list[dict]) -> str:
    import httpx
    response = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "temperature": 0},
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"] or ""


def _parse_plan(raw: str) -> list[tuple[str, str, list[str]]]:
    data = _load_json(raw)
    if isinstance(data, dict):
        data = data.get("subtasks") or data.get("items") or []
    if not isinstance(data, list):
        return []
    items = []
    for row in data:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        description = str(row.get("description") or title).strip()
        files = []
        for path in row.get("files") or []:
            if isinstance(path, str) and _safe_path(path.strip()):
                files.append(path.strip())
        after = [int(i) for i in row.get("after") or [] if isinstance(i, int) or str(i).isdigit()]
        items.append((title, description, files, after))
    return items


def _load_json(raw: str):
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


def _safe_path(path: str) -> bool:
    if not path or path.startswith(("/", "\\")) or ".." in path.split("/"):
        return False
    return True
