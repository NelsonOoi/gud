"""A worker agent. Push assigned subtasks, rebase lost races, and answer as a line owner."""

from __future__ import annotations

import hashlib
import json
import re
import time

from . import compose
from .compose import Identity
from .inbox import Gate, Inbox
from .merge import merge_edits
from .patch import PatchError, apply_diff, diff_trees
from .replay import replay
from .rules import affected_owners
from .tasks import derive, open_subtasks_for
from .wire import normalize, parse_body

_WAIT = {"window", "testing", "needs_approval", "needs_quorum", "qualified"}
_RETRY = {"superseded", "stale", "conflict"}

_RATE_OLD = (
    'def login(username: str, password: str) -> LoginResult:\n'
    '    """Check the password and start a session. Unknown user and bad password share one reason."""\n'
    '    row = _users.get(username)\n'
)
_RATE_NEW = (
    'def login(username: str, password: str) -> LoginResult:\n'
    '    """Check the password and start a session. Unknown user and bad password share one reason."""\n'
    '    if not ratelimit.allow(f"login:{username}"):\n'
    '        return LoginResult(False, None, "rate limited")\n'
    '    row = _users.get(username)\n'
)

_BAD_LOGIN = '''def login(username: str, password: str) -> LoginResult:
    """Check the password and start a session. Unknown user and bad password share one reason."""
    # gud-bad-edit: lockout replaces the login flow
    if ratelimit.allow(f"login:{username}") is False:
        return LoginResult(False, None, "rate limited")
    if _failures.get(username, 0) >= 5:
        return LoginResult(False, None, "locked")
    row = _users.get(username)
    if row is None or not verify_password(password, row[1]):
        _failures[username] = _failures.get(username, 0) + 1
        return LoginResult(False, None, "invalid credentials")
    _failures.pop(username, None)
    return LoginResult(True, start_session(username), "")
'''

_COMBINED_LOGIN = '''def login(username: str, password: str) -> LoginResult:
    """Check the password and start a session. Unknown user and bad password share one reason."""
    if not ratelimit.allow(f"login:{username}"):
        return LoginResult(False, None, "rate limited")  # owner kept this check
    if _failures.get(username, 0) >= 5:
        return LoginResult(False, None, "locked")
    row = _users.get(username)
    if row is None or not verify_password(password, row[1]):
        _failures[username] = _failures.get(username, 0) + 1
        return LoginResult(False, None, "invalid credentials")
    _failures.pop(username, None)
    return LoginResult(True, start_session(username), "")
'''

_RATE_TEST = '''
def test_second_login_inside_an_empty_bucket_is_rejected(alice):
    ratelimit.configure(rate=0, capacity=1)
    try:
        first = auth.login("alice", "correct horse")
        assert first.ok and first.session is not None
        second = auth.login("alice", "correct horse")
        assert not second.ok
        assert second.session is None
        assert second.reason == "rate limited"
    finally:
        ratelimit.configure(rate=10, capacity=10)
'''

_LOCK_TEST = '''
def test_five_bad_passwords_lock_the_account(alice):
    ratelimit.configure(rate=0, capacity=20)
    try:
        for _ in range(5):
            denied = auth.login("alice", "nope")
            assert denied.reason == "invalid credentials"
        locked = auth.login("alice", "correct horse")
        assert not locked.ok and locked.session is None
        assert locked.reason == "locked"
        auth.revoke_user_sessions("alice")
        again = auth.login("alice", "correct horse")
        assert again.ok and again.session is not None
    finally:
        ratelimit.configure(rate=10, capacity=10)
'''

_PENALIZE_APPEND = '''
def _penalize(self, key: str, now: float | None = None) -> None:
    """Drain `key`. The next allow fails until the bucket refills."""
    now = time.monotonic() if now is None else now
    self._tokens[key] = 0.0
    self._updated[key] = now


if not hasattr(TokenBucket, "penalize"):
    TokenBucket.penalize = _penalize


def penalize(key: str, now: float | None = None) -> None:
    """Set the process-wide bucket's tokens for `key` to zero."""
    _default.penalize(key, now=now)
'''

_PENALIZE_TEST = '''"""penalize drains a key so the next allow fails."""

from __future__ import annotations

import ratelimit


def test_penalize_makes_the_next_allow_fail():
    bucket = ratelimit.TokenBucket(rate=0, capacity=2)
    assert bucket.allow("alice", now=0)
    bucket.penalize("alice", now=0)
    assert bucket.allow("alice", now=0) is False
    ratelimit.configure(rate=0, capacity=2)
    try:
        assert ratelimit.allow("k", now=0)
        ratelimit.penalize("k", now=0)
        assert ratelimit.allow("k", now=0) is False
    finally:
        ratelimit.configure(rate=10, capacity=10)
'''

_DECISION_CLASS = '''@dataclass(frozen=True)
class RateLimitDecision:
    """Whether a key may proceed, and how long a caller should wait if it may not."""

    allowed: bool
    remaining: int
    retry_after: float


'''

_CHARGE_OLD = (
    "    invoice = Invoice(\n"
    "        username=user, period=period, base_cents=amount_cents, overage_cents=0, total_cents=amount_cents,\n"
    "    )\n"
)
_CHARGE_NEW = (
    "    usage = usage_of(user, period)\n"
    "    plan = plan_for(user)\n"
    "    extra = overage_cents(usage.requests, plan)\n"
    "    invoice = Invoice(\n"
    "        username=user, period=period, base_cents=amount_cents, overage_cents=extra,\n"
    "        total_cents=amount_cents + extra,\n"
    "    )\n"
)

_OVERAGE_TEST = '''
def test_charge_of_zero_on_the_free_plan_bills_overage():
    billing.open_account("carol", "free")
    billing.meter_request("carol", period="2026-10", n=103)
    invoice = billing.charge("carol", 0, period="2026-10")
    assert invoice.base_cents == 0
    assert invoice.overage_cents == 6
    assert invoice.total_cents == 6
'''


def demo_edit(subtask, tip_tree, blame, feedback=None) -> dict:
    """Deterministic file edits for the three demo tasks. `feedback` selects the revised lockout."""
    title = (getattr(subtask, "title", "") or "").lower().replace("-", " ")
    if "lockout" in title:
        return _lockout_edits(tip_tree, revised=bool(feedback))
    if "ratelimitdecision" in title.replace(" ", "") or "protected interface" in title:
        return _interface_edits(tip_tree)
    if "penalize" in title:
        return _penalize_edits(tip_tree)
    if "overage" in title:
        return _billing_edits(tip_tree)
    if "429" in title or "meter" in title:
        return _handler_edits(tip_tree)
    if "login" in title:
        return _auth_edits(tip_tree)
    return {}


def demo_review(diff: str, context: str = "") -> tuple[str, str]:
    """Object only to the scripted conflicting login rewrite. Everything else is approved."""
    if "gud-bad-edit" in (diff or ""):
        return "object", "keep the rate-limit check; do not replace login"
    return "approve", "Approved."


class ScriptedCoder:
    """Demo edits, or callables injected by a test. `review` returns `(action, reason)`."""

    def __init__(self, edit_fn=None, review_fn=None):
        self._edit = edit_fn
        self._review = review_fn

    def edit(self, subtask, tip_tree, blame, feedback=None):
        fn = self._edit or demo_edit
        try:
            return fn(subtask, tip_tree, blame, feedback)
        except TypeError:
            return fn(subtask, tip_tree, blame)

    def review(self, diff, context=""):
        fn = self._review or demo_review
        try:
            decision, reason = fn(diff, context)
        except TypeError:
            decision, reason = fn(diff)
        action = (decision or "").strip().lower()
        if action not in {"approve", "object"}:
            action = "approve"
        if not reason:
            reason = "Approved." if action == "approve" else "objected"
        return action, reason


class LLMCoder:
    """OpenRouter coder. `edit` returns full file contents; `review` is approve or object."""

    def __init__(self, api_key: str, model: str, clock=time.monotonic):
        self.api_key = api_key
        self.model = model
        self.clock = clock
        self._failed: dict[str, tuple[int, float]] = {}

    def edit(self, subtask, tip_tree, blame, feedback=None):
        files = _relevant(subtask, tip_tree)
        prompt = _edit_prompt(subtask, files, blame, feedback)
        key = hashlib.sha256(prompt.encode()).hexdigest()
        count, until = self._failed.get(key, (0, 0.0))
        if self.clock() < until:
            return {}
        messages = [
            {"role": "system", "content": (
                "You edit a repository for one subtask. Reply with JSON only: "
                "{\"files\": {\"path\": \"full new file text\"}}. "
                "Use null to delete a file. Include only files you change. "
                "Paths are repo-relative. Keep every file valid UTF-8 source. "
                "Use the exact values the task gives (names, prices, addresses, numbers). "
                "Keep content other agents already wrote unless your subtask says to replace it."
            )},
            {"role": "user", "content": prompt},
        ]
        error = None
        # At temperature 0 a truncated reply repeats verbatim, so the retry changes the input.
        for attempt in range(2):
            try:
                if attempt:
                    messages.append({"role": "user", "content": (
                        f"Your last reply was not complete JSON ({error}). Reply again with the complete "
                        "JSON object only, and include only the files you must change."
                    )})
                raw = _chat(self.api_key, self.model, messages, temperature=0.3 if attempt else 0)
                edits = _parse_edits(raw, tip_tree)
                self._failed.pop(key, None)
                return edits
            except Exception as exc:
                error = str(exc)[:160]
        self._failed[key] = (count + 1, self.clock() + min(30.0 * 2 ** count, 300.0))
        print(f"coder {getattr(subtask, 'id', '')} edit failed: {error}", flush=True)
        return {}

    def review(self, diff, context=""):
        """`(approve|object, reason)`, or `("abstain", why)` when the model gave no usable answer."""
        try:
            raw = _chat(self.api_key, self.model, [
                {"role": "system", "content": (
                    "You own lines that a proposed diff changes, and you review it before it lands. "
                    "Reply with JSON only: {\"action\": \"approve\" or \"object\", \"reason\": one sentence}. "
                    "Object when the diff deletes or breaks work you or others did that should stay, "
                    "or when it contradicts the stated requirements (wrong values, missing parts). "
                    "Say exactly what to change in the reason. Approve otherwise."
                )},
                {"role": "user", "content": f"{context}\n\nDiff:\n{diff[:8000]}"},
            ])
            data = _load_json(raw)
            action = str(data.get("action") or "").strip().lower()
            reason = str(data.get("reason") or "").strip() or "No objection."
        except Exception as exc:
            print(f"coder review failed: {exc}", flush=True)
            return "abstain", str(exc)[:200]
        if action not in {"approve", "object"}:
            return "abstain", f"unusable action {action!r}"
        return action, reason.split("\n", 1)[0][:300]


class Worker:
    """One assignee. `step` is safe to call in a loop; it never sends the same reply twice.

    Work arrives in the worker's own inbox: its assignments, pushes that touch lines it
    owns, and replies to its pushes. The repo is replayed (through `gate`) to pick the tip
    to build on and to see whether its pushes linked.
    """

    def __init__(self, mailbox, identity: Identity, window: float, coder, clock=time.time,
                 gate: Gate | None = None, pending_ttl: float = 90.0):
        self.mailbox = mailbox
        self.identity = identity
        self.window = window
        self.coder = coder
        self.clock = clock
        self.inbox = Inbox(mailbox, identity.address, mailbox.repo_address)
        self.gate = gate or Gate()
        self.pending_ttl = pending_ttl
        self._assigned: set[str] = set()
        # Pushes sent but not yet seen in the repo mailbox: subtask id -> (message id, monotonic time).
        self._pending: dict[str, tuple[str, float]] = {}
        self._voted: set[str] = set()
        self._review_failed: dict[str, float] = {}
        # Subtask id -> (tip, feedback) on which the coder produced no change.
        self._no_change: dict[str, tuple[str, str]] = {}

    def step(self) -> list:
        mail = self.inbox.read()
        for email in mail:
            parsed = parse_body(email.body)
            if parsed.kind == "subtask" and parsed.get("Gud-Assignee") == self.identity.handle:
                self._assigned.add(parsed.get("Gud-Task") or "")
        busy = bool(self._assigned or self._pending)
        if not self.gate.due(bool(mail), busy):
            return []
        chain, _now = self._chain()
        tip = chain.tip
        if tip is None:
            return []
        sent = []
        sent += self._review(chain, tip)
        tasks = derive(chain)
        self._forget_finished(tasks)
        landed = {link.task for link in chain.links if link.task}
        mine = [sub for sub in open_subtasks_for(tasks, self.identity.handle)
                if sub.id in self._assigned and _ready(sub, landed)]
        open_ids = {sub.id for sub in mine}
        pushed = False
        for sub in mine:
            email = self._advance(chain, tip, sub)
            if email is not None:
                self._pending[sub.id] = (email.message_id, time.monotonic())
                sent.append(email)
                pushed = True
                break
        if not pushed:
            sent += self._rebase_orphans(chain, tip, open_ids)
        return sent

    def _forget_finished(self, tasks) -> None:
        """Drop assignments that landed or went to someone else. Unseen ones stay: mail can lag."""
        by_id = {sub.id: sub for task in tasks for sub in task.subtasks}
        for sub_id in list(self._assigned):
            sub = by_id.get(sub_id)
            if sub is not None and (sub.status == "landed" or sub.assignee != self.identity.handle):
                self._assigned.discard(sub_id)
                self._pending.pop(sub_id, None)

    def _chain(self):
        now = self.clock()
        return replay(_listed(self.mailbox), self.window, now), now

    def _advance(self, chain, tip, sub):
        pending = self._pending.get(sub.id)
        if pending is not None:
            message_id, sent_at = pending
            if message_id in chain.by_message:
                del self._pending[sub.id]
            elif time.monotonic() - sent_at < self.pending_ttl:
                return None
            else:
                del self._pending[sub.id]
        latest = _latest_push(chain, self.identity.handle, sub.id)
        if latest is None:
            return self._from_coder(tip, sub, feedback=None)
        status = _status(chain, latest)
        if status == "link" or status in _WAIT:
            return None
        if status == "objected":
            return self._from_coder(tip, sub, feedback="objected: " + _objection(chain, latest.message_id))
        if status == "tests_failed":
            return self._from_coder(tip, sub, feedback="tests failed:\n" + _test_log(chain, latest.message_id))
        if status in _RETRY:
            edits = self._rebase_edits(chain, latest, tip, sub)
            return self._push(tip, edits, task=sub.id, intent=_intent(sub, "rebased onto the new tip"),
                              subject=f"[gud] {sub.id}: {sub.title}")
        return None

    def _from_coder(self, tip, sub, feedback):
        attempt = (tip.message_id, feedback or "")
        if self._no_change.get(sub.id) == attempt:
            return None
        try:
            edits = self.coder.edit(sub, tip.tree, tip.blame, feedback=feedback) or {}
        except TypeError:
            edits = self.coder.edit(sub, tip.tree, tip.blame) or {}
        except Exception as exc:
            print(f"{self.identity.handle} edit {sub.id} failed: {exc}", flush=True)
            return None
        note = feedback.split("\n", 1)[0][:160] if feedback else sub.title
        email = self._push(tip, edits, task=sub.id, intent=_intent(sub, note),
                           subject=f"[gud] {sub.id}: {sub.title}")
        if email is None:
            self._no_change[sub.id] = attempt
            print(f"{self.identity.handle} {sub.id}: coder changed nothing; waiting for a new tip", flush=True)
        else:
            self._no_change.pop(sub.id, None)
        return email

    def _rebase_edits(self, chain, email, tip, subtask):
        """Re-apply a lost race on the new tip: a clean 3-way merge first, the coder only on conflict."""
        recovered = _recover(chain, email)
        if recovered is None:
            if subtask is None:
                return {}
            return self._coder_edit(subtask, tip, feedback=None)
        parent_files, edits = recovered
        changed = any(tip.tree.get(path) != old for path, old in parent_files.items())
        if not changed:
            return edits
        merged = merge_edits(parent_files, edits, tip.tree)
        if merged is not None:
            return merged
        if subtask is None:
            # A stale snapshot of a finished push would overwrite work that landed on top.
            return {}
        feedback = ("rebase: your earlier push was superseded and conflicts with the new tip. "
                    "Re-apply your change on the current files.\nYour earlier diff:\n"
                    + parse_body(email.body).diff[:3000])
        return self._coder_edit(subtask, tip, feedback=feedback)

    def _coder_edit(self, subtask, tip, feedback):
        try:
            return self.coder.edit(subtask, tip.tree, tip.blame, feedback=feedback) or {}
        except TypeError:
            return self.coder.edit(subtask, tip.tree, tip.blame) or {}

    def _rebase_orphans(self, chain, tip, open_ids: set[str]) -> list:
        """Re-push a race I lost that was not one of my open subtasks (the demo's step 5)."""
        me = self.identity.handle
        landed = {link.task for link in chain.links if link.task}
        groups: dict[str, list] = {}
        for prop in chain.proposals.values():
            if prop.author != me:
                continue
            if prop.task and (prop.task in open_ids or prop.task in landed):
                continue
            groups.setdefault(prop.task or "", []).append(prop)
        sent = []
        for props in groups.values():
            latest = max(props, key=lambda prop: prop.received_at)
            if latest.status not in _RETRY:
                continue
            key = "rebase:" + latest.message_id
            if key in self._pending:
                if time.monotonic() - self._pending[key][1] < self.pending_ttl:
                    continue
            email = chain.by_message.get(latest.message_id)
            if email is None:
                continue
            edits = self._rebase_edits(chain, email, tip, None)
            pushed = self._push(
                tip, edits, task=latest.task, intent=f"rebase: {latest.subject}",
                subject=("[gud] rebase " + latest.subject)[:140],
            )
            if pushed is not None:
                self._pending[key] = (pushed.message_id, time.monotonic())
                sent.append(pushed)
        return sent

    def _review(self, chain, tip) -> list:
        me = self.identity.handle
        sent = []
        for prop in list(chain.proposals.values()):
            if prop.author == me or me not in (prop.affected_owners or []):
                continue
            if prop.status not in {"window", "testing", "needs_approval", "needs_quorum"}:
                continue
            if prop.message_id in self._voted or _replied(chain, me, prop.message_id):
                continue
            failed_at = self._review_failed.get(prop.message_id)
            if failed_at is not None and time.monotonic() - failed_at < 20:
                continue
            email = chain.by_message.get(prop.message_id)
            if email is None:
                continue
            parsed = parse_body(email.body)
            context = review_context(chain, prop, parsed, me)
            try:
                decision, reason = self.coder.review(parsed.diff, context)
            except Exception as exc:
                print(f"{me} review {prop.message_id} failed: {exc}", flush=True)
                self._review_failed[prop.message_id] = time.monotonic()
                continue
            action = (decision or "").strip().lower()
            if action not in {"approve", "object"}:
                # No vote: an outage must not approve. Lazy consensus still applies.
                self._review_failed[prop.message_id] = time.monotonic()
                continue
            parent = chain.by_id.get(prop.parent)
            members = parent.members if parent is not None else tip.members
            if action == "object":
                body = compose.object_body(self.identity, prop.message_id, reason or "objected")
            else:
                body = compose.approve_body(self.identity, prop.message_id, reason or "Approved.")
            sent.append(compose.send(
                self.mailbox, self.identity, members, f"Re: {email.subject}", body,
                in_reply_to=prop.message_id, cc_handles=compose.reply_cc(members, prop.author),
            ))
            self._voted.add(prop.message_id)
            print(f"{action} {me} {prop.message_id} {reason}", flush=True)
        return sent

    def _push(self, tip, edits, *, task, intent, subject):
        if not edits:
            return None
        tree = dict(tip.tree)
        for path, content in edits.items():
            if not _safe_path(path):
                print(f"{self.identity.handle} skip unsafe path {path}", flush=True)
                continue
            if content is None:
                tree.pop(path, None)
            else:
                tree[path] = content
        diff = diff_trees(tip.tree, tree)
        if not diff.strip():
            return None
        body, _state, _tree_hash = compose.push_body(
            self.identity, parent=tip.message_id, parent_state=tip.state, parent_tree=tip.tree,
            new_tree=tree, intent=intent, task=task,
        )
        owners = affected_owners(tip, diff, self.identity.handle)
        email = compose.send(
            self.mailbox, self.identity, tip.members, subject, body, in_reply_to=tip.message_id,
            cc_handles=compose.push_cc(tip.members, owners),
        )
        print(f"push {self.identity.handle} {task or '-'} {email.message_id}", flush=True)
        return email


def _intent(sub, note: str) -> str:
    text = f"{sub.id} {sub.title}".strip()
    if note and note != sub.title:
        text += "\n\n" + note
    owners = "Please approve if you own the lines this push rewrites."
    return text + "\n\n" + owners


def _latest_push(chain, author: str, task_id: str):
    found = None
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.kind == "push" and parsed.get("Gud-Author") == author and parsed.get("Gud-Task") == task_id:
            found = email
    return found


def _status(chain, email) -> str:
    if email.message_id in chain.by_id:
        return "link"
    prop = chain.proposals.get(email.message_id)
    return prop.status if prop is not None else "unknown"


def _objection(chain, push_id: str) -> str:
    prop = chain.proposals.get(push_id)
    if prop is not None and prop.objections:
        last = prop.objections[-1]
        who = last.get("by") or "owner"
        return f"{who}: {last.get('reason') or ''}".strip()
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.kind == "object" and parsed.get("Gud-Target") == push_id:
            return parsed.get("Gud-Reason") or parsed.text
    return "the owner objected"


def _test_log(chain, push_id: str) -> str:
    log = ""
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.kind == "test" and parsed.get("Gud-Target") == push_id:
            log = parsed.text or ""
    return log[-2000:]


def _replied(chain, author: str, target: str) -> bool:
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.get("Gud-Author") == author and parsed.get("Gud-Target") == target and parsed.kind in {
            "approve", "object",
        }:
            return True
    return False


def _recover(chain, email):
    parsed = parse_body(email.body)
    parent = chain.by_id.get(parsed.get("Gud-Parent") or "")
    if parent is None:
        return None
    try:
        new_tree = apply_diff(dict(parent.tree), parsed.diff)
    except PatchError:
        return None
    parent_files = {}
    edits = {}
    for path in set(parent.tree) | set(new_tree):
        if parent.tree.get(path) != new_tree.get(path):
            parent_files[path] = parent.tree.get(path)
            edits[path] = new_tree.get(path)
    return parent_files, edits


def _ready(sub, landed: set[str]) -> bool:
    """A subtask with `Gud-After` waits until every listed subtask has landed."""
    return all(dep in landed for dep in getattr(sub, "after", None) or [])


def review_context(chain, prop, parsed, handle: str) -> str:
    """What an owner needs to judge a push: its subtask, the owner's own work, the files before."""
    lines = [f"You are {handle}, an owner of lines this push changes."]
    subs = {sub.id: (task, sub) for task in derive(chain) for sub in task.subtasks}
    if prop.task and prop.task in subs:
        _task, sub = subs[prop.task]
        lines.append(f"The push is for {sub.id} {sub.title}. Its requirements:\n{sub.description[:3000]}")
    if parsed.text:
        lines.append(f"The author's note:\n{parsed.text[:600]}")
    mine = [sub for _task, sub in subs.values() if sub.assignee == handle]
    if mine:
        lines.append("Your own subtasks:\n" + "\n".join(f"- {s.id} {s.title} ({s.status})" for s in mine))
    parent = chain.by_id.get(prop.parent)
    if parent is not None:
        from .patch import diffstat
        for path in diffstat(parsed.diff)["files"][:4]:
            if path in parent.tree:
                lines.append(f"----- {path} before this push -----\n{parent.tree[path][:4000]}")
    return "\n\n".join(lines)


def _listed(mailbox):
    last = None
    for _ in range(5):
        try:
            return mailbox.list(recipient=mailbox.repo_address)
        except json.JSONDecodeError as exc:
            last = exc
            time.sleep(0.02)
    raise last


_BUCKET_OLD = '''def test_module_bucket_configure_and_reset():
    ratelimit.configure(rate=1, capacity=1)
    assert ratelimit.allow("k", now=10)
    assert not ratelimit.allow("k", now=10)
    assert ratelimit.remaining("k", now=10) == 0
    ratelimit.reset()
    assert ratelimit.allow("k", now=10)
    assert ratelimit.remaining("other", now=10) == 1
'''

_BUCKET_NEW = '''def test_module_bucket_configure_and_reset():
    ratelimit.configure(rate=1, capacity=1)
    try:
        assert ratelimit.allow("k", now=10)
        assert not ratelimit.allow("k", now=10)
        assert ratelimit.remaining("k", now=10) == 0
        ratelimit.reset()
        assert ratelimit.allow("k", now=10)
        assert ratelimit.remaining("other", now=10) == 1
    finally:
        # login now spends tokens; put the process-wide bucket back for later tests.
        ratelimit.configure(rate=10, capacity=10)
'''


def _auth_edits(tree: dict) -> dict:
    src = _ensure_import_line(tree.get("auth.py", ""), "import billing\n", "import billing\nimport ratelimit\n")
    if 'return LoginResult(False, None, "rate limited")' not in src and _RATE_OLD in src:
        src = src.replace(_RATE_OLD, _RATE_NEW, 1)
    tests = _ensure_test_import(tree.get("tests/test_auth.py", ""))
    tests = _append_marker(tests, "def test_second_login_inside_an_empty_bucket_is_rejected", _RATE_TEST)
    api_tests = tree.get("tests/test_api.py", "")
    if _BUCKET_OLD in api_tests:
        api_tests = api_tests.replace(_BUCKET_OLD, _BUCKET_NEW, 1)
    return _changed(tree, {"auth.py": src, "tests/test_auth.py": tests, "tests/test_api.py": api_tests})


def _billing_edits(tree: dict) -> dict:
    src = tree.get("billing.py", "")
    if _CHARGE_OLD in src:
        src = src.replace(_CHARGE_OLD, _CHARGE_NEW, 1)
    tests = tree.get("tests/test_billing.py", "")
    if 'n=150' in tests and "test_charge_records_the_amount_without_overage" in tests:
        tests = tests.replace(
            'billing.meter_request("carol", period="2026-10", n=150)',
            'billing.meter_request("carol", period="2026-10", n=10)',
            1,
        )
    tests = _append_marker(tests, "def test_charge_of_zero_on_the_free_plan_bills_overage", _OVERAGE_TEST)
    return _changed(tree, {"billing.py": src, "tests/test_billing.py": tests})


def _handler_edits(tree: dict) -> dict:
    src = tree.get("api/handlers.py", "")
    old = (
        "    if not result.ok or result.session is None:\n"
        '        return _error(401, result.reason or "invalid credentials")\n'
    )
    new = (
        "    if not result.ok or result.session is None:\n"
        '        status = 429 if result.reason == "rate limited" else 401\n'
        "        return _error(status, result.reason or \"invalid credentials\")\n"
    )
    if old in src:
        src = src.replace(old, new, 1)
    meter = "    billing.meter_request(session.username)\n    return _ok({\"invoice\": invoice_view(invoice)})\n"
    plain = "    return _ok({\"invoice\": invoice_view(invoice)})\n"
    if "billing.meter_request(session.username)" not in src and plain in src:
        src = src.replace(plain, meter, 1)
    tests = tree.get("tests/test_api.py", "")
    tests = tests.replace(
        'assert usage["body"]["requests"] == 4',
        'assert usage["body"]["requests"] == 5',
        1,
    )
    return _changed(tree, {"api/handlers.py": src, "tests/test_api.py": tests})


def _penalize_edits(tree: dict) -> dict:
    src = tree.get("ratelimit.py", "")
    if "def penalize(" not in src:
        src = src.rstrip("\n") + "\n" + _PENALIZE_APPEND
    updates = {"ratelimit.py": src}
    if "tests/test_penalize.py" not in tree:
        updates["tests/test_penalize.py"] = _PENALIZE_TEST
    return _changed(tree, updates)


def _interface_edits(tree: dict) -> dict:
    src = tree.get("api/interfaces.py", "")
    if "class RateLimitDecision" not in src:
        src = src.replace("class RateLimiter(Protocol):\n", _DECISION_CLASS + "class RateLimiter(Protocol):\n", 1)
    if "def decide(" not in src:
        src = src.replace(
            "    def remaining(self, key: str) -> int: ...\n",
            "    def remaining(self, key: str) -> int: ...\n\n"
            "    def decide(self, key: str) -> RateLimitDecision: ...\n",
            1,
        )
    return _changed(tree, {"api/interfaces.py": src})


def _lockout_edits(tree: dict, revised: bool) -> dict:
    src = _ensure_import_line(tree.get("auth.py", ""), "import billing\n", "import billing\nimport ratelimit\n")
    if revised or _combined_landed(src):
        src = _ensure_failures(src)
        src = _replace_function(src, "login", _COMBINED_LOGIN)
        tests = _ensure_test_import(tree.get("tests/test_auth.py", ""))
        tests = _append_marker(tests, "def test_five_bad_passwords_lock_the_account", _LOCK_TEST)
        return _changed(tree, {"auth.py": src, "tests/test_auth.py": tests})
    if "gud-bad-edit" in src:
        return {}
    src = _ensure_failures(src)
    src = _replace_function(src, "login", _BAD_LOGIN)
    return _changed(tree, {"auth.py": src})


def _combined_landed(src: str) -> bool:
    return "owner kept this check" in src and "gud-bad-edit" not in src and "_failures" in src


def _ensure_failures(src: str) -> str:
    if "_failures:" not in src:
        src = src.replace(
            "_sessions: dict[str, Session] = {}\n",
            "_sessions: dict[str, Session] = {}\n_failures: dict[str, int] = {}\n",
            1,
        )
    if "_failures.clear()" not in src:
        src = src.replace("    _sessions.clear()\n", "    _sessions.clear()\n    _failures.clear()\n", 1)
    needle = 'def revoke_user_sessions(username: str) -> int:\n    """Drop every session for `username`. Returns how many were removed."""\n'
    if "_failures.pop(username, None)" not in src and needle in src:
        src = src.replace(needle, needle + "    _failures.pop(username, None)\n", 1)
    return src


def _replace_function(src: str, name: str, new_block: str) -> str:
    lines = src.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if line.startswith(f"def {name}("):
            start = index
            break
    if start is None:
        return src
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith(("def ", "class ")):
            end = index
            break
    block = new_block if new_block.endswith("\n") else new_block + "\n"
    if end < len(lines) and not block.endswith("\n\n"):
        block += "\n"
    return "".join(lines[:start]) + block + "".join(lines[end:])


def _ensure_import_line(src: str, anchor: str, replacement: str) -> str:
    if "import ratelimit" in src:
        return src
    if anchor in src:
        return src.replace(anchor, replacement, 1)
    return "import ratelimit\n" + src


def _ensure_test_import(src: str) -> str:
    if "import ratelimit" in src:
        return src
    if "import billing\n" in src:
        return src.replace("import billing\n", "import billing\nimport ratelimit\n", 1)
    return "import ratelimit\n" + src


def _append_marker(src: str, marker: str, block: str) -> str:
    if marker in src:
        return src
    return src.rstrip("\n") + "\n" + block.strip("\n") + "\n"


def _changed(tree: dict, updates: dict) -> dict:
    out = {}
    for path, content in updates.items():
        if content is None:
            if path in tree:
                out[path] = None
            continue
        new = normalize(content)
        if tree.get(path) != new:
            out[path] = new
    return out


def _safe_path(path: str) -> bool:
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
        return False
    return ".." not in path.split("/")


def _relevant(subtask, tree: dict) -> dict[str, str]:
    text = f"{getattr(subtask, 'title', '')}\n{getattr(subtask, 'description', '')}"
    hinted = []
    for line in text.splitlines():
        if line.lower().startswith("files:"):
            hinted.extend(part.strip() for part in line.split(":", 1)[1].split(",") if part.strip())
    chosen = [path for path in hinted if path in tree and _safe_path(path)]
    if not chosen:
        for path in sorted(tree):
            if path in text and _safe_path(path):
                chosen.append(path)
    files = {}
    for path in chosen[:8]:
        files[path] = tree[path][:12000]
    return files


def _edit_prompt(subtask, files: dict, blame, feedback) -> str:
    parts = [
        f"Subtask {getattr(subtask, 'id', '')}: {getattr(subtask, 'title', '')}",
        getattr(subtask, "description", "") or "",
    ]
    if feedback:
        parts.append("Feedback from the thread:\n" + str(feedback)[:2000])
    for path, content in files.items():
        parts.append(f"----- {path} -----\n{content}")
        parts.append("blame: " + _blame_ranges(blame, path))
    parts.append("Return JSON with the full new contents of every file you change.")
    return "\n\n".join(parts)


def _blame_ranges(blame, path: str) -> str:
    rows = (blame or {}).get(path) or []
    if not rows:
        return "(none)"
    ranges = []
    start = 1
    owner = rows[0][0]
    for index, (who, _link) in enumerate(rows, start=1):
        if who != owner:
            ranges.append(f"{start}-{index - 1} {owner}")
            start, owner = index, who
    ranges.append(f"{start}-{len(rows)} {owner}")
    return ", ".join(ranges[:40])


def _parse_edits(raw: str, tree: dict) -> dict:
    data = _load_json(raw)
    if isinstance(data, dict) and "files" in data and isinstance(data["files"], dict):
        data = data["files"]
    if not isinstance(data, dict):
        return {}
    edits = {}
    for path, content in data.items():
        if not _safe_path(str(path)):
            continue
        if content is None:
            edits[str(path)] = None
        elif isinstance(content, str):
            edits[str(path)] = content
    return _changed(tree, edits) if edits else {}


def _load_json(raw: str):
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


def _chat(api_key: str, model: str, messages: list[dict], temperature: float = 0) -> str:
    import httpx
    response = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"] or ""
