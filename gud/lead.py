"""The lead agent. Split tasks, reassign stale subtasks, close finished tasks, vote as @lead."""

from __future__ import annotations

import json
import re
import time

from . import compose, notify
from .compose import Identity
from .replay import replay
from .tasks import Task, derive
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


def _describe(description: str, files: list[str]) -> str:
    text = description.strip()
    if files and "files:" not in text.lower():
        text += "\n\nFiles: " + ", ".join(files)
    return text


class ScriptedPlanner:
    """Split a task by matching title and description keywords against a dict of scripts."""

    def __init__(self, splits: dict[str, list[tuple[str, str, list[str]]]] | None = None):
        self.splits = DEMO_SPLITS if splits is None else splits

    def split(self, task: Task, tip_tree: dict) -> list[tuple[str, str, list[str]]]:
        hay = _hay(task)
        for keyword, items in self.splits.items():
            if keyword.lower().replace("-", " ") in hay:
                return [(title, desc, list(files)) for title, desc, files in items]
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
                    "\"files\": [str]}]}. Use 2 to 4 subtasks that touch different files."
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
                 stale_after: float = 120, pool: list[str] | None = None):
        self.mailbox = mailbox
        self.identity = identity
        self.window = window
        self.planner = planner
        self.clock = clock
        self.stale_after = stale_after
        self.pool = pool
        self._starved: set[tuple[str, str]] = set()

    def step(self) -> list:
        chain, now = self._chain()
        tip = chain.tip
        if tip is None:
            return []
        tasks = derive(chain)
        sent = []
        sent += self._split(chain, tasks, tip)
        sent += self._reassign(chain, tasks, tip, now)
        sent += self._finish(chain, tasks, tip)
        sent += self._approve(chain, tip)
        return sent

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
            # Only an open task with no subtasks is split. A second step sees the mail and does nothing.
            if task.status == "done" or task.subtasks or not task.message_id:
                continue
            for title, description, files in self.planner.split(task, tip.tree):
                text = _describe(description, files)
                avoid = _owners_of(tip, files) if "gud-conflict" in description else set()
                handle, start = _pick(pool, busy | avoid, start)
                if handle is None:
                    if (task.id, title) not in self._starved:
                        self._starved.add((task.id, title))
                        print(f"lead: no free worker for {task.id} {title}", flush=True)
                    break
                email = self._send_subtask(tip, task, title, text, handle, _next_sub_id(task, sent))
                sent.append(email)
                busy.add(handle)
                print(f"lead: subtask {parse_body(email.body).get('Gud-Task')} -> {handle} {title}",
                      flush=True)
        return sent

    def _send_subtask(self, tip, task, title, description, assignee, sub_id):
        body = compose.subtask_body(self.identity, sub_id, task.id, assignee, description)
        return compose.send(
            self.mailbox, self.identity, tip.members, f"[gud] {sub_id}: {title}", body,
            in_reply_to=task.message_id,
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
                if assigned is None or now - assigned < self.stale_after:
                    continue
                files = _hint_files(sub.description)
                avoid = {sub.assignee} | (_owners_of(tip, files) if "gud-conflict" in sub.description else set())
                handle, start = _pick(pool, busy | avoid, start)
                if handle is None or handle == sub.assignee:
                    continue
                email = self._send_subtask(tip, task, sub.title, sub.description, handle, sub.id)
                sent.append(email)
                busy.add(handle)
                print(f"lead: reassign {sub.id} {sub.assignee} -> {handle}", flush=True)
        return sent

    def _finish(self, chain, tasks, tip) -> list:
        sent = []
        for task in tasks:
            ready = bool(task.subtasks) and all(sub.status == "landed" for sub in task.subtasks)
            if task.status != "done" and ready:
                summary = _done_summary(task)
                body = compose.task_done_body(self.identity, task.id, tip.message_id, summary)
                email = compose.send(
                    self.mailbox, self.identity, tip.members, f"[gud] {task.id} done: {task.title}",
                    body, in_reply_to=task.message_id,
                )
                sent.append(email)
                print(f"lead: task-done {task.id} tip {tip.message_id}", flush=True)
                note = self._notify(task, tip, summary)
                if note is not None:
                    sent.append(note)
            elif task.status == "done" and task.notified_at is None:
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
            if _already(chain, me, prop.message_id):
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
            body = compose.approve_body(self.identity, prop.message_id)
            email = compose.send(
                self.mailbox, self.identity, parent.members, f"Re: {target.subject}", body,
                in_reply_to=prop.message_id,
            )
            sent.append(email)
            print(f"lead: approve {prop.message_id} {prop.subject}", flush=True)
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
        items.append((title, description, files))
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
