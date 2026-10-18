"""Live view of a gud repo: one cached replay, the feed, and the human actions.

The HTTP API and the hosted MCP app both call this. Widget polls share the cache
so a room full of viewers does not replay the mailbox on every tick.
"""

from __future__ import annotations

import os
import re
import threading
import time

from . import compose, sig, tasks as tasklib
from .bugs import _ask_targets, _target, _verb, bugs_address_for, derive_bugs
from .chain import Repo
from .compose import Identity
from .config import domain_address
from .keys import decode_private_key, env_private
from .memberfile import by_address, sending_address
from .patch import diffstat
from .types import Email, Member
from .wire import parse_body, short

FEED_LIMIT = 50
OPEN_PROPOSALS = frozenset({"window", "needs_approval", "needs_quorum"})
PUSH_VERBS = ("approve", "object", "veto")
BUG_VERBS = ("approve", "reject")
_TASK_ID = re.compile(r"^T\d+$")
_SUB_ID = re.compile(r"^T\d+\.\d+$")


class ConsoleError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def all_members(chain) -> dict[str, Member]:
    out: dict[str, Member] = {}
    for link in chain.links:
        out.update(link.members)
    return out


def handle_of(chain, address: str) -> str:
    member = by_address(all_members(chain), address)
    return member.handle if member else address


def email_kind(email: Email) -> str:
    return parse_body(email.body).kind or "human"


def activity(chain, limit: int = 100) -> list[dict]:
    items = []
    for email in chain.emails:
        parsed = parse_body(email.body)
        kind = parsed.kind or "human"
        summary = (parsed.get("Gud-Reason") or parsed.text or "").strip().splitlines()
        items.append({
            "id": email.message_id, "at": email.received_at, "kind": kind,
            "from": parsed.get("Gud-Author") or handle_of(chain, email.from_addr),
            "subject": email.subject, "summary": (summary[0] if summary else "")[:200],
            "target": parsed.get("Gud-Target") or email.in_reply_to,
        })
    for link in chain.links[1:]:
        items.append({
            "id": f"link:{link.message_id}", "at": link.linked_at, "kind": "link", "from": link.author,
            "subject": link.subject, "summary": f"link #{link.seq} {short(link.state)}",
            "target": link.message_id,
        })
    items.sort(key=lambda item: item["at"], reverse=True)
    return items[:limit]


def text_fallback(data: dict) -> str:
    """Short text for hosts that do not render the widget."""
    lines = []
    tip = data.get("tip")
    if tip:
        lines.append(f"tip {tip['seq']} {tip['state']} {tip['author']} {tip.get('subject') or ''}".rstrip())
    else:
        lines.append("tip: none")
    tasks = data.get("tasks") or []
    open_tasks = [task for task in tasks if task.get("status") != "done"]
    lines.append(f"tasks: {len(open_tasks)} open / {len(tasks)}")
    for task in tasks:
        subs = task.get("subtasks") or []
        done = sum(1 for sub in subs if sub.get("status") in {"landed", "no-change"})
        lines.append(f"  {task['id']} {task.get('status')} {done}/{len(subs)} {task.get('title') or ''}".rstrip())
    lines.append("recent:")
    recent = data.get("events") or []
    if not recent:
        lines.append("  (none)")
    for event in recent[:6]:
        lines.append(f"  {event['kind']} {event.get('actor') or ''} {event.get('text') or ''}".rstrip())
    needs = data.get("needs_you") or []
    if not needs:
        lines.append("needs-you: none")
    else:
        lines.append("needs-you:")
        for item in needs:
            verbs = " ".join(item.get("verbs") or [])
            lines.append(f"  {item['id']} [{verbs}] {item.get('title') or ''}".rstrip())
    focus = data.get("focus")
    if focus and focus.get("kind") == "file":
        lines.append(f"file: {focus.get('path')} ({len(focus.get('lines') or [])} lines)")
    elif focus and focus.get("kind") == "thread":
        lines.append(f"thread: {focus.get('id')} ({len(focus.get('emails') or [])} emails)")
    return "\n".join(lines)


class Console:
    def __init__(self, cfg, mailbox=None, clock=time.time, *, identity: Identity | None = None,
                 refresh_seconds: float | None = None):
        from .mailbox import open_mailbox
        self.cfg = cfg
        self.clock = clock
        self.mailbox = mailbox or open_mailbox(cfg)
        self.repo = Repo(self.mailbox, cfg.window_seconds, clock=clock)
        self._identity = identity
        if refresh_seconds is None:
            refresh_seconds = float(os.environ.get("GUD_FEED_REFRESH", "3"))
        self.refresh_seconds = float(refresh_seconds)
        self._lock = threading.Lock()
        self._cached = None
        self._bug_mail: list[Email] = []
        self._cached_at = 0.0

    def bugs_address(self) -> str:
        raw = (getattr(self.cfg, "bugs_address", "") or "").strip()
        return raw.lower() if raw else bugs_address_for(self.mailbox.repo_address)

    def human_handle(self) -> str:
        return _at(getattr(self.cfg, "human", "") or "@nelson")

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def chain(self, *, force: bool = False):
        chain, _bugs = self._snapshot(force=force)
        return chain

    def _snapshot(self, *, force: bool = False):
        with self._lock:
            now = time.monotonic()
            due = self._cached is None or now - self._cached_at >= self.refresh_seconds
            if force or due:
                self._cached = self.repo.refresh()
                self._bug_mail = _read_bugs(self.mailbox, self.bugs_address())
                self._cached_at = now
            return self._cached, list(self._bug_mail)

    def authorize(self, token: str | None, headers: dict | None = None) -> None:
        """Legacy shared-secret check. The feed signs with the caller's Ed25519 key instead."""
        expected = getattr(self.cfg, "mcp_token", None)
        if not expected:
            return
        headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        auth = headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if token == expected or bearer == expected or headers.get("x-gud-token") == expected:
            return
        raise ConsoleError("token required", 401)

    def state(self, since: str | None = None, focus: str | None = None) -> dict:
        chain, bug_mail = self._snapshot()
        tasks = tasklib.derive(chain)
        members = chain.tip.members if chain.tip else {}
        bugs = derive_bugs(bug_mail, chain.emails, members, self.bugs_address())
        events = feed_events(chain, bugs, bug_mail)
        cursor = _cursor(events)
        needs = needs_you(bugs, chain)
        tip = _tip(chain)
        if since:
            mark = _parse_cursor(since)
            newer = [event for event in events if _event_key(event) > mark]
            task_ids, bug_ids = _touched(newer, tasks)
            payload = {
                "delta": True,
                "since": since,
                "repo": self.mailbox.repo_address,
                "now": self.clock(),
                "tip": tip,
                "tasks": [task.to_json() for task in tasks if task.id in task_ids],
                "bugs": [bug.to_json() for bug in bugs if bug.id in bug_ids],
                "events": list(reversed(newer)),
                "needs_you": needs,
                "cursor": cursor,
            }
        else:
            payload = {
                "delta": False,
                "repo": self.mailbox.repo_address,
                "now": self.clock(),
                "tip": tip,
                "tasks": [task.to_json() for task in tasks],
                "bugs": [bug.to_json() for bug in bugs],
                "events": list(reversed(events[-FEED_LIMIT:])),
                "needs_you": needs,
                "cursor": cursor,
            }
        if focus:
            payload["focus"] = self._focus(chain, bug_mail, tasks, bugs, focus)
        return payload

    def task_rows(self, chain) -> list[dict]:
        return [task.to_json() for task in tasklib.derive(chain)]

    def bug_rows_json(self, chain) -> list[dict]:
        members = chain.tip.members if chain.tip else {}
        bugs = derive_bugs(_read_bugs(self.mailbox, self.bugs_address()), chain.emails, members, self.bugs_address())
        return [bug.to_json() for bug in bugs]

    def select_repo(self, repo: str | None) -> None:
        """Point this mailbox at ``repo`` for the next read or write. Blank keeps the current one."""
        address = (repo or "").strip()
        if not address:
            return
        current = (getattr(self.mailbox, "repo_address", "") or "").strip()
        if address.lower() == current.lower():
            return
        self.mailbox.repo_address = address
        self.invalidate()

    def identity_for_key(self, private_key: str | None, handle: str | None = None) -> Identity:
        """The signer for a feed write. ``handle`` wins; otherwise the roster public key."""
        raw = decode_private_key(private_key)
        if raw is None:
            raise ConsoleError("private_key must be a 32-byte Ed25519 key", 400)
        named = _named_handle(handle)
        chain = self.chain(force=True)
        members = chain.tip.members if chain.tip else {}
        if named is None:
            pub = sig.public_of(raw)
            match = next((member for member in members.values() if member.public_key == pub), None)
            if match is None:
                raise ConsoleError("pass handle with the private key", 400)
            return Identity(match.handle, match.address, raw)
        member = members.get(named)
        address = member.address if member is not None else domain_address(self.cfg.domain, named)
        return Identity(named, address, raw)

    def create_task(self, title: str, description: str = "", requester: str | None = None, *,
                    identity: Identity | None = None) -> str:
        title = (title or "").strip()
        if not title:
            raise ConsoleError("title is required", 400)
        ident = identity or self.console_identity()
        if identity is not None and not (requester or "").strip():
            requester = ident.handle
        else:
            requester = _at(requester or self.human_handle())
        chain = self.chain(force=True)
        tid = tasklib.next_task_id(chain.emails)
        text = compose.task_body(ident, tid, requester, (description or "").strip() or title)
        members = chain.tip.members if chain.tip else {}
        compose.send(
            self.mailbox, ident, members, f"[gud] {tid}: {title}", text,
            cc_handles=compose.role_handles(members, "lead") or None,
        )
        self.invalidate()
        return tid

    def decide(self, target: str, verdict: str, reason: str = "", as_handle: str | None = None) -> str:
        """One plain human reply. Replay already understands approve, object, veto, reject."""
        verdict = (verdict or "").strip().lower()
        chain, bug_mail = self._snapshot(force=True)
        if chain.tip is None:
            raise ConsoleError("no genesis yet", 409)
        who = _at(as_handle or self.human_handle())
        member = all_members(chain).get(who)
        if member is None or member.role != "human":
            raise ConsoleError(f"{who} is not a human member", 400)
        members = chain.tip.members
        bugs = derive_bugs(bug_mail, chain.emails, members, self.bugs_address())
        item = next((row for row in needs_you(bugs, chain) if row["id"] == target), None)
        if item is None:
            if any(bug.id == target for bug in bugs):
                raise ConsoleError("bug report is already reviewed", 409)
            if _known_message(chain, bug_mail, target):
                raise ConsoleError("not open for a decision", 409)
            raise ConsoleError("unknown target", 404)
        if verdict not in item["verbs"]:
            raise ConsoleError(f"verdict must be one of: {', '.join(item['verbs'])}", 400)
        note = (reason or "").strip()
        body = verdict if not note else f"{verdict}\n{note}"
        email = self.mailbox.send(
            from_addr=sending_address(member, self.cfg.domain), to=self.mailbox.repo_address,
            subject=f"Re: {item['title']}", body=body + "\n", in_reply_to=target,
        )
        self.invalidate()
        return email.message_id

    def signed_reply(self, target: str, verdict: str, reason: str, identity: Identity) -> str:
        """A feed decision signed with the caller's key. Humans still send a plain reply."""
        verdict = (verdict or "").strip().lower()
        chain, bug_mail = self._snapshot(force=True)
        if chain.tip is None:
            raise ConsoleError("no genesis yet", 409)
        member = all_members(chain).get(identity.handle)
        if member is not None and member.role == "human":
            return self.decide(target, verdict, reason, identity.handle)
        members = chain.tip.members
        bugs = derive_bugs(bug_mail, chain.emails, members, self.bugs_address())
        item = next((row for row in needs_you(bugs, chain) if row["id"] == target), None)
        if item is None:
            if any(bug.id == target for bug in bugs):
                raise ConsoleError("bug report is already reviewed", 409)
            if _known_message(chain, bug_mail, target):
                raise ConsoleError("not open for a decision", 409)
            raise ConsoleError("unknown target", 404)
        if verdict not in item["verbs"]:
            raise ConsoleError(f"verdict must be one of: {', '.join(item['verbs'])}", 400)
        if verdict == "veto":
            raise ConsoleError("veto is a human reply", 400)
        note = (reason or "").strip()
        if verdict == "approve":
            body = compose.approve_body(identity, target, note or "Approved.")
        else:
            body = compose.object_body(identity, target, note or "Rejected.")
        email = compose.send(
            self.mailbox, identity, members, f"Re: {item['title']}", body, in_reply_to=target,
        )
        self.invalidate()
        return email.message_id

    def human_reply(self, target: str, who: str, text: str) -> str:
        """Arbitrary human reply. Still fake-mailbox only; ``decide`` is the hosted path."""
        if self.cfg.mailbox != "fake":
            raise ConsoleError("human replies are only simulated with the fake mailbox", 400)
        chain = self.chain(force=True)
        who = _at(who or self.human_handle())
        member = all_members(chain).get(who)
        if member is None or member.role != "human":
            raise ConsoleError(f"{who} is not a human member", 400)
        email = next((item for item in chain.emails if item.message_id == target), None)
        if email is None:
            raise ConsoleError("unknown target", 404)
        sent = self.mailbox.send(
            from_addr=sending_address(member, self.cfg.domain), to=self.mailbox.repo_address,
            subject=f"Re: {email.subject}", body=text.strip() + "\n", in_reply_to=email.message_id,
        )
        self.invalidate()
        return sent.message_id

    def console_identity(self) -> Identity:
        if self._identity is not None:
            return self._identity
        handle = "@console"
        path = self.cfg.key_path(handle)
        if path.exists():
            raw = sig.load_private(path)
        else:
            raw = env_private(handle)
            if raw is None:
                raise ConsoleError(f"no local key for {handle}", 400)
        return Identity(handle, domain_address(self.cfg.domain, handle), raw)

    def _focus(self, chain, bug_mail, tasks, bugs, focus: str) -> dict:
        focus = (focus or "").strip()
        task = next((item for item in tasks if item.id == focus), None)
        if task is not None and _TASK_ID.match(focus):
            return _thread_focus(chain, bug_mail, focus, _task_thread(chain, bug_mail, task), _task_diff(chain, task))
        sub = _find_subtask(tasks, focus)
        if sub is not None and _SUB_ID.match(focus):
            parent, st = sub
            emails = _thread_emails(chain, bug_mail, st.message_id)
            diff = ""
            if st.link_id:
                link = chain.by_id.get(st.link_id)
                diff = link.diff if link else ""
            return _thread_focus(chain, bug_mail, focus, emails, diff, task_id=parent.id)
        tip = chain.tip
        path = _norm_path(focus)
        if tip is not None and path and path in tip.tree:
            return _file_focus(tip, path)
        bug = next((item for item in bugs if item.id == focus), None)
        if bug is not None:
            return _thread_focus(chain, bug_mail, focus, _thread_emails(chain, bug_mail, bug.id), "")
        if _known_message(chain, bug_mail, focus):
            diff = _diff_for(chain, focus)
            return _thread_focus(chain, bug_mail, focus, _thread_emails(chain, bug_mail, focus), diff)
        if path and ("/" in focus or "." in focus):
            return {"kind": "missing", "id": focus, "path": path, "text": "no such file"}
        return {"kind": "missing", "id": focus, "text": "nothing matches"}


def needs_you(bugs, chain) -> list[dict]:
    rows = []
    for bug in bugs:
        if bug.status == "pending" and not bug.verdict:
            rows.append({
                "id": bug.id, "title": bug.subject, "kind": "bug",
                "verbs": list(BUG_VERBS), "status": bug.status,
            })
    proposals = sorted(chain.proposals.values(), key=lambda item: (item.received_at, item.message_id))
    for prop in proposals:
        if prop.status not in OPEN_PROPOSALS:
            continue
        rows.append({
            "id": prop.message_id, "title": _title(prop.subject) or prop.subject, "kind": "push",
            "verbs": list(PUSH_VERBS), "status": prop.status,
        })
    return rows


def feed_events(chain, bugs, bug_mail) -> list[dict]:
    """Oldest first. The snapshot serves the newest slice of this list."""
    from .replay import _human_verb

    events = []
    pool = _dedupe([*bug_mail, *chain.emails])
    for email in chain.emails:
        parsed = parse_body(email.body)
        kind = parsed.kind
        actor = parsed.get("Gud-Author") or handle_of(chain, email.from_addr)
        task = parsed.get("Gud-Task") or None
        target = parsed.get("Gud-Target") or email.in_reply_to
        text = _clip(parsed.text or email.subject)
        base = (email.message_id, email.received_at, actor, task, target)
        if kind == "task":
            events.append(_event(*base, "task", _title(email.subject) or text))
        elif kind == "subtask":
            events.append(_event(*base, "subtask", _title(email.subject) or text))
        elif kind == "push":
            events.append(_event(*base, "push", _title(email.subject) or text))
        elif kind == "test":
            result = (parsed.get("Gud-Result") or "").lower()
            events.append(_event(*base, "test-pass" if result == "pass" else "test-fail", result or text))
        elif kind == "approve":
            events.append(_event(*base, "approve", text or "approve"))
        elif kind == "object":
            events.append(_event(*base, "object", _clip(parsed.get("Gud-Reason") or text)))
        elif kind == "no-change":
            events.append(_event(
                email.message_id, email.received_at, actor, task, parsed.get("Gud-Tip") or target,
                "no-change", _clip(parsed.get("Gud-Reason") or text or "no change"),
            ))
        elif kind == "task-done":
            events.append(_event(*base, "task-done", text or "done"))
        elif kind is None:
            verb = _human_verb(parsed.text or "")
            if verb:
                word, rest = verb
                events.append(_event(*base, word, _clip(rest or word)))
    for link in chain.links:
        stat = diffstat(link.diff) if link.diff else {"added": 0, "removed": 0}
        events.append(_event(
            f"link:{link.message_id}", link.linked_at, link.author, link.task, link.message_id,
            "link", f"+{stat['added']} -{stat['removed']} {_title(link.subject) or _clip(link.intent)}",
            added=stat["added"], removed=stat["removed"],
        ))
    child = {}
    for prev, nxt in zip(chain.links, chain.links[1:]):
        child[prev.message_id] = nxt
    for prop in chain.proposals.values():
        if prop.status != "superseded":
            continue
        winner = child.get(prop.parent)
        events.append(_event(
            f"superseded:{prop.message_id}",
            winner.linked_at if winner else prop.received_at,
            prop.author, prop.task, prop.message_id, "superseded",
            _title(prop.subject) or _clip(prop.reason or "superseded"),
        ))
    for bug in bugs:
        actor = handle_of(chain, bug.from_addr)
        events.append(_event(bug.id, bug.received_at, actor, bug.task_id, bug.id, "bug", bug.subject, bug=bug.id))
        if bug.status == "pending" and not bug.verdict:
            continue
        when = _bug_review_time(pool, bug.id) or bug.received_at
        if bug.status == "accepted" or bug.verdict == "approve":
            events.append(_event(
                f"bug-accepted:{bug.id}", when, bug.review_by or actor, bug.task_id, bug.id,
                "bug-accepted", bug.subject, bug=bug.id,
            ))
        elif bug.status == "rejected" or bug.verdict == "reject":
            events.append(_event(
                f"bug-rejected:{bug.id}", when, bug.review_by or actor, None, bug.id,
                "bug-rejected", _clip(bug.reason or bug.subject), bug=bug.id,
            ))
    events.sort(key=_event_key)
    return events


def _event(id, at, actor, task, target, kind, text, bug=None, added=None, removed=None) -> dict:
    row = {
        "id": id, "kind": kind, "time": at, "actor": actor or "",
        "task": task, "target": target, "text": _clip(text),
    }
    if bug:
        row["bug"] = bug
    if added is not None:
        row["added"] = added
        row["removed"] = removed or 0
    return row


def _tip(chain) -> dict | None:
    tip = chain.tip
    if tip is None:
        return None
    return {
        "seq": tip.seq, "id": tip.message_id, "author": tip.author,
        "subject": tip.subject, "state": short(tip.state),
    }


def _cursor(events: list[dict]) -> str:
    if not events:
        return "0.000000|"
    last = events[-1]
    return f"{last['time']:.6f}|{last['id']}"


def _parse_cursor(value: str) -> tuple:
    time_s, _, eid = (value or "").partition("|")
    try:
        stamp = round(float(time_s), 6)
    except ValueError:
        stamp = 0.0
    return (stamp, eid)


def _event_key(event: dict) -> tuple:
    return (round(float(event["time"]), 6), event["id"])


def _touched(events, tasks) -> tuple[set[str], set[str]]:
    parents = {}
    for task in tasks:
        parents[task.id] = task.id
        for sub in task.subtasks:
            parents[sub.id] = task.id
    task_ids: set[str] = set()
    bug_ids: set[str] = set()
    for event in events:
        bug = event.get("bug")
        if bug:
            bug_ids.add(bug)
        tid = event.get("task")
        if not tid:
            continue
        task_ids.add(parents.get(tid) or (tid.split(".", 1)[0] if "." in tid else tid))
    return task_ids, bug_ids


def _bug_review_time(emails, bug_id: str) -> float | None:
    asks = _ask_targets(emails)
    found = None
    for email in emails:
        if _target(email, asks) != bug_id or _verb(email) is None:
            continue
        row = (email.received_at, email.message_id)
        if found is None or row < found:
            found = row
    return None if found is None else found[0]


def _known_message(chain, bug_mail, message_id: str) -> bool:
    if message_id in chain.by_id or message_id in chain.proposals:
        return True
    for email in chain.emails:
        if email.message_id == message_id:
            return True
    for email in bug_mail:
        if email.message_id == message_id:
            return True
    return False


def _read_bugs(mailbox, address: str) -> list[Email]:
    try:
        return mailbox.list(recipient=address)
    except Exception:
        return []


def _dedupe(emails: list[Email]) -> list[Email]:
    out = []
    seen = set()
    for email in emails:
        if email.message_id in seen:
            continue
        seen.add(email.message_id)
        out.append(email)
    return out


def _thread_emails(chain, bug_mail, message_id: str) -> list[Email]:
    rows = []
    seen = set()
    for email in _dedupe([*chain.emails, *bug_mail]):
        if email.message_id in seen:
            continue
        parsed = parse_body(email.body)
        if email.message_id == message_id or email.in_reply_to == message_id or parsed.get("Gud-Target") == message_id:
            seen.add(email.message_id)
            rows.append(email)
    rows.sort(key=lambda email: (email.received_at, email.message_id))
    return rows


def _task_thread(chain, bug_mail, task) -> list[Email]:
    ids = [task.message_id] + [sub.message_id for sub in task.subtasks if sub.message_id]
    rows = []
    seen = set()
    for mid in ids:
        for email in _thread_emails(chain, bug_mail, mid):
            if email.message_id in seen:
                continue
            seen.add(email.message_id)
            rows.append(email)
    rows.sort(key=lambda email: (email.received_at, email.message_id))
    return rows


def _task_diff(chain, task) -> str:
    ids = {task.id, *(sub.id for sub in task.subtasks)}
    parts = []
    for link in chain.links:
        if link.task in ids and link.diff:
            parts.append(link.diff)
    return "\n".join(parts)


def _diff_for(chain, message_id: str) -> str:
    link = chain.by_id.get(message_id)
    if link is not None and link.diff:
        return link.diff
    email = next((item for item in chain.emails if item.message_id == message_id), None)
    if email is None:
        return ""
    return parse_body(email.body).diff or ""


def _thread_focus(chain, bug_mail, focus, emails, diff, task_id=None) -> dict:
    return {
        "kind": "thread",
        "id": focus,
        "task": task_id,
        "diff": diff or "",
        "emails": [_email_view(chain, email) for email in emails],
    }


def _email_view(chain, email: Email) -> dict:
    parsed = parse_body(email.body)
    return {
        "id": email.message_id,
        "from": parsed.get("Gud-Author") or handle_of(chain, email.from_addr),
        "subject": email.subject,
        "body": email.body,
        "received_at": email.received_at,
        "in_reply_to": email.in_reply_to,
        "kind": parsed.kind,
    }


def _file_focus(tip, path: str) -> dict:
    lines = tip.tree.get(path, "").splitlines()
    blamed = tip.blame.get(path, [])
    rows = []
    for index, text in enumerate(lines):
        if index < len(blamed):
            owner, link_id = blamed[index]
        else:
            owner, link_id = "", ""
        rows.append({"n": index + 1, "text": text, "owner": owner, "link_id": link_id})
    return {"kind": "file", "id": path, "path": path, "at": tip.message_id, "lines": rows}


def _find_subtask(tasks, focus: str):
    for task in tasks:
        for sub in task.subtasks:
            if sub.id == focus:
                return task, sub
    return None


def _norm_path(path: str) -> str:
    parts = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return ""
        parts.append(part)
    return "/".join(parts)


def _title(subject: str) -> str:
    text = re.sub(r"^(re:\s*)+", "", subject or "", flags=re.I).strip()
    text = re.sub(r"^\[gud\]\s*", "", text, flags=re.I)
    return re.sub(r"^T\d+(\.\d+)?:?\s*", "", text).strip()


def _clip(text: str, limit: int = 160) -> str:
    line = " ".join((text or "").split())
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def _named_handle(handle: str | None) -> str | None:
    text = (handle or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        text = text[1:].strip()
    if not text or "@" in text or any(ch.isspace() for ch in text):
        raise ConsoleError("invalid handle", 400)
    return f"@{text}"


def _at(handle: str) -> str:
    handle = (handle or "").strip()
    if not handle:
        return "@nelson"
    return handle if handle.startswith("@") else f"@{handle}"
