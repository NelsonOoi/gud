"""A worker agent. Push assigned subtasks, sign when nothing needs to change, rebase lost races, and answer as a line owner."""

from __future__ import annotations

import hashlib
import json
import re
import time

from . import compose
from .compose import Identity
from .inbox import Gate, Inbox, next_window_close
from .merge import merge_edits
from .patch import PatchError, apply_diff, diff_trees, diffstat
from .replay import replay
from .rules import affected_owners
from .tasks import derive, open_subtasks_for
from .wire import normalize, parse_body, test_log

_WAIT = {"window", "testing", "needs_approval", "needs_quorum", "qualified"}
_RETRY = {"superseded", "stale", "conflict"}
_REVIEWABLE = {"window", "testing", "needs_approval", "needs_quorum"}
# Seconds before an abstained review is asked again. A vote only counts before the window closes.
REVIEW_RETRY = 3.0
# Objected pushes by one author on one subtask before the author stops and the lead escalates.
MAX_OBJECTIONS = 2

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
    if not ratelimit.allow("login"):
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
    """Object only to a login that drops the per-user rate-limit key. Everything else is approved."""
    added = [line for line in (diff or "").splitlines() if line.startswith("+")]
    if "gud-bad-edit" in (diff or "") or any('ratelimit.allow("login")' in line for line in added):
        return "object", ('keep the rate-limit check per user, ratelimit.allow(f"login:{username}"); '
                          "one shared key lets one client rate-limit every user")
    return "approve", "Approved."


class NoChange:
    """The coder checked this tip and the subtask is already satisfied."""

    def __init__(self, reason: str):
        self.reason = " ".join((reason or "").split())[:300]


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
        if action == "abstain":
            return action, reason or "abstained"
        if action not in {"approve", "object"}:
            action = "approve"
        if not reason:
            reason = "Approved." if action == "approve" else "objected"
        return action, reason


class LLMCoder:
    """OpenRouter coder. `edit` returns full file contents, or `NoChange`; `review` is approve or object.

    `review_model` answers reviews only; it defaults to `model`.
    """

    def __init__(self, api_key: str, model: str, review_model: str | None = None, clock=time.monotonic):
        self.api_key = api_key
        self.model = model
        self.review_model = review_model or model
        self.clock = clock
        self._failed: dict[str, tuple[int, float]] = {}

    def edit(self, subtask, tip_tree, blame, feedback=None):
        files = _relevant(subtask, tip_tree)
        prompt = _edit_prompt(subtask, files, blame, feedback)
        key = hashlib.sha256(prompt.encode()).hexdigest()
        count, until = self._failed.get(key, (0, 0.0))
        if self.clock() < until:
            return None
        messages = [
            {"role": "system", "content": (
                "You edit a repository for one subtask. Reply with JSON only: "
                "{\"files\": {\"path\": \"full new file text\"}}. "
                "When the current files already satisfy the subtask, do not rewrite them. Reply "
                "{\"files\": {}, \"done\": \"one sentence saying why nothing needs to change\"}. "
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
        return None

    def review(self, diff, context=""):
        """`(approve|object, reason)`, or `("abstain", why)` when the model gave no usable answer."""
        try:
            raw = _chat(self.api_key, self.review_model, [
                {"role": "system", "content": (
                    "You own lines that a proposed push changes, and you vote on it before it lands. "
                    "What your vote does: if you stay silent, the push lands when its consent window closes "
                    "(unless the context says it needs explicit approval). If you object, the push is blocked, "
                    "and your reason is mailed to the author as the instruction for their revision. "
                    "Check the requirements one by one: the push's own subtask, and your own subtasks. "
                    "Compare the file before and after the push. "
                    "Lines that the push's own subtask asks to replace are fine to replace, even if you wrote them. "
                    "Object only when something that should stay is lost, or a stated requirement is broken "
                    "(wrong values, missing parts). "
                    "If you objected to an earlier push for this subtask and that reason is now addressed, approve. "
                    "Reply with JSON only: {\"action\": \"approve\" or \"object\", \"reason\": one sentence}. "
                    "When you object, say exactly what must stay or change."
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
        # Push id -> clock time of the last abstained review.
        self._review_failed: dict[str, float] = {}
        # Subtask id -> (tip, feedback) the coder already left unchanged. The next step signs no-change.
        self._no_change: dict[str, tuple[str, str]] = {}
        self._gave_up: set[str] = set()
        self._window_at: float | None = None

    def step(self) -> list:
        mail = self.inbox.read()
        for email in mail:
            parsed = parse_body(email.body)
            if parsed.kind == "subtask" and parsed.get("Gud-Assignee") == self.identity.handle:
                self._assigned.add(parsed.get("Gud-Task") or "")
        busy = bool(self._assigned or self._pending or self._review_failed)
        if not self.gate.due(bool(mail), busy, self._window_at):
            return []
        chain, _now = self._chain()
        self._window_at = next_window_close(chain)
        tip = chain.tip
        if tip is None:
            return []
        sent = []
        sent += self._review(chain, tip)
        tasks = derive(chain)
        self._forget_finished(tasks)
        done = {link.task for link in chain.links if link.task}
        done.update(sub.id for task in tasks for sub in task.subtasks if sub.status == "no-change")
        mine = [sub for sub in open_subtasks_for(tasks, self.identity.handle)
                if sub.id in self._assigned and _ready(sub, done)]
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
        """Drop assignments that landed, finished with the task, or went to someone else.

        A `no-change` stays assigned: a later edit to those files opens the subtask again,
        and this worker still has the mail. Unseen ones stay too: mail can lag.
        """
        by_id = {}
        finished = set()
        for task in tasks:
            if task.status == "done":
                finished.add(task.id)
            for sub in task.subtasks:
                by_id[sub.id] = sub
        for sub_id in list(self._assigned):
            sub = by_id.get(sub_id)
            if sub is None:
                continue
            parent = sub_id.split(".", 1)[0]
            if sub.status == "landed" or sub.assignee != self.identity.handle or parent in finished:
                self._assigned.discard(sub_id)
                self._pending.pop(sub_id, None)
                self._no_change.pop(sub_id, None)

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
            if len(objected_pushes(chain, self.identity.handle, sub.id)) >= MAX_OBJECTIONS:
                if sub.id not in self._gave_up:
                    self._gave_up.add(sub.id)
                    print(f"{self.identity.handle} {sub.id}: objected {MAX_OBJECTIONS} times; "
                          "stopped revising, the lead escalates", flush=True)
                return None
            return self._from_coder(tip, sub, feedback=_objection_feedback(chain, latest, sub),
                                    objected=_pushed_tree(chain, latest))
        if status == "tests_failed":
            return self._from_coder(tip, sub, feedback="tests failed:\n" + test_log(chain.emails, latest.message_id))
        if status in _RETRY:
            edits = self._rebase_edits(chain, latest, tip, sub)
            return self._push(tip, edits, task=sub.id, intent=_intent(sub, "rebased onto the new tip"),
                              subject=f"[gud] {sub.id}: {sub.title}")
        return None

    def _from_coder(self, tip, sub, feedback, objected=None):
        attempt = (tip.message_id, feedback or "")
        if self._no_change.get(sub.id) == attempt:
            # Second time this tip came back unchanged. Sign it; do not ask the coder again.
            self._no_change.pop(sub.id, None)
            return self._declare(tip, sub, "no change needed")
        try:
            result = self.coder.edit(sub, tip.tree, tip.blame, feedback=feedback)
            if _repeats(tip.tree, result, objected):
                print(f"{self.identity.handle} {sub.id}: revision repeats the objected push; asking again",
                      flush=True)
                result = self.coder.edit(sub, tip.tree, tip.blame, feedback=(
                    feedback + "\n\nYour last revision was identical to the objected push. Change the "
                    "files so the objection is met while your subtask's requirements still hold."))
        except TypeError:
            result = self.coder.edit(sub, tip.tree, tip.blame)
        except Exception as exc:
            print(f"{self.identity.handle} edit {sub.id} failed: {exc}", flush=True)
            return None
        if isinstance(result, NoChange):
            self._no_change.pop(sub.id, None)
            return self._declare(tip, sub, result.reason)
        if result is None:
            return None
        edits = result if isinstance(result, dict) else {}
        note = feedback.split("\n", 1)[0][:160] if feedback else sub.title
        email = self._push(tip, edits, task=sub.id, intent=_intent(sub, note),
                           subject=f"[gud] {sub.id}: {sub.title}")
        if email is None:
            self._no_change[sub.id] = attempt
            print(f"{self.identity.handle} {sub.id}: coder changed nothing; will sign no-change if this tip holds",
                  flush=True)
        else:
            self._no_change.pop(sub.id, None)
        return email

    def _declare(self, tip, sub, reason: str):
        body = compose.no_change_body(self.identity, sub.id, tip.message_id, reason)
        email = compose.send(
            self.mailbox, self.identity, tip.members, f"[gud] {sub.id}: no change", body,
            in_reply_to=sub.message_id, cc_handles=compose.role_handles(tip.members, "lead"),
        )
        print(f"no-change {self.identity.handle} {sub.id} {tip.message_id} {reason}", flush=True)
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
            result = self.coder.edit(subtask, tip.tree, tip.blame, feedback=feedback)
        except TypeError:
            result = self.coder.edit(subtask, tip.tree, tip.blame)
        return result if isinstance(result, dict) else {}

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
        for push_id in list(self._review_failed):
            prop = chain.proposals.get(push_id)
            if prop is None or prop.status not in _REVIEWABLE:
                del self._review_failed[push_id]
        for prop in list(chain.proposals.values()):
            if prop.author == me or me not in (prop.affected_owners or []):
                continue
            if prop.status not in _REVIEWABLE:
                continue
            if prop.message_id in self._voted or _replied(chain, me, prop.message_id):
                continue
            if window_closed(prop, self.clock()):
                self._late(prop.message_id)
                continue
            failed_at = self._review_failed.get(prop.message_id)
            if failed_at is not None and self.clock() - failed_at < REVIEW_RETRY:
                continue
            email = chain.by_message.get(prop.message_id)
            if email is None:
                continue
            parsed = parse_body(email.body)
            context = review_context(chain, prop, parsed, me, now=self.clock())
            try:
                decision, reason = self.coder.review(parsed.diff, context)
            except Exception as exc:
                print(f"{me} review {prop.message_id} failed: {exc}", flush=True)
                self._review_failed[prop.message_id] = self.clock()
                continue
            action = (decision or "").strip().lower()
            if action not in {"approve", "object"}:
                # No vote: an outage must not approve. Lazy consensus still applies.
                self._review_failed[prop.message_id] = self.clock()
                continue
            if window_closed(prop, self.clock()):
                self._late(prop.message_id)
                continue
            self._review_failed.pop(prop.message_id, None)
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

    def _late(self, push_id: str) -> None:
        """The window closed before this owner voted; a vote now would not count."""
        self._voted.add(push_id)
        self._review_failed.pop(push_id, None)
        print(f"late review {self.identity.handle} {push_id}", flush=True)

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


def _objections(chain, push_id: str) -> list[tuple[str, str]]:
    """`(who, reason)` for each objection to `push_id`, oldest first."""
    prop = chain.proposals.get(push_id)
    if prop is not None and prop.objections:
        return [(item.get("by") or "owner", (item.get("reason") or "").strip()) for item in prop.objections]
    found = []
    for email in chain.emails:
        parsed = parse_body(email.body)
        if parsed.kind == "object" and parsed.get("Gud-Target") == push_id:
            found.append((parsed.get("Gud-Author") or "owner", (parsed.get("Gud-Reason") or parsed.text).strip()))
    return found or [("owner", "")]


def _objection_feedback(chain, email, sub) -> str:
    """What the author needs to revise an objected push. The first line becomes the revision's note."""
    found = _objections(chain, email.message_id)
    who = ", ".join(dict.fromkeys(by for by, _reason in found))
    lines = [f"objected by {by}: {reason or 'no reason given'}" for by, reason in found]
    lines += [
        f"Your push changed lines owned by {who}, so the objection blocked it.",
        f"Push a revision that still meets every requirement of {sub.id} and also meets the objection. "
        f"It lands only with approval from {who}, or if {who} sends no objection before the consent "
        "window closes.",
        "Your earlier diff:\n" + parse_body(email.body).diff[:3000],
    ]
    return "\n".join(lines)


def objected_pushes(chain, author: str, task_id: str) -> list:
    """`author`'s objected pushes for `task_id`, oldest first."""
    props = [prop for prop in chain.proposals.values()
             if prop.author == author and prop.task == task_id and prop.status == "objected"]
    return sorted(props, key=lambda prop: (prop.received_at, prop.message_id))


def _pushed_tree(chain, email) -> dict | None:
    """The tree `email`'s push would have produced on its parent."""
    parsed = parse_body(email.body)
    parent = chain.by_id.get(parsed.get("Gud-Parent") or "")
    if parent is None:
        return None
    try:
        return apply_diff(dict(parent.tree), parsed.diff)
    except PatchError:
        return None


def _repeats(tree: dict, edits, objected: dict | None) -> bool:
    """True when `edits` on `tree` rebuild exactly the `objected` push's tree."""
    if not isinstance(edits, dict) or not edits or objected is None:
        return False
    after = dict(tree)
    for path, content in edits.items():
        if content is None:
            after.pop(path, None)
        else:
            after[path] = content
    return after == objected


def window_closed(prop, now: float) -> bool:
    """Lazy consensus only: past the window close, a vote no longer changes the outcome."""
    return prop.window_closes_at is not None and now >= prop.window_closes_at


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


def _ready(sub, done: set[str]) -> bool:
    """A subtask with `Gud-After` waits until every listed subtask has landed or signed no-change."""
    return all(dep in done for dep in getattr(sub, "after", None) or [])


def review_context(chain, prop, parsed, handle: str, now: float | None = None) -> str:
    """What a voter needs to judge a push: what the vote does, the requirements on both sides,
    earlier objections on the subtask, and each changed file before and after."""
    role = "an owner of lines this push changes" if handle in (prop.affected_owners or []) else "a voter on this push"
    lines = [f"You are {handle}, {role}. The author is {prop.author}.", _vote_rule(prop, now)]
    subs = {sub.id: sub for task in derive(chain) for sub in task.subtasks}
    if prop.task and prop.task in subs:
        sub = subs[prop.task]
        lines.append(f"The push is for {sub.id} {sub.title}. Its requirements:\n{sub.description[:3000]}")
    if parsed.text:
        lines.append(f"The author's note:\n{parsed.text[:600]}")
    mine = [sub for sub in subs.values() if sub.assignee == handle and sub.id != prop.task][-5:]
    if mine:
        lines.append("Your own subtasks. The lines you wrote for them must keep meeting these requirements:\n"
                     + "\n\n".join(f"- {s.id} {s.title} ({s.status})\n{s.description[:1500]}" for s in mine))
    if chain.links and chain.links[0].author == handle:
        lines.append("You wrote the starting files (genesis). A subtask may ask to replace those lines.")
    earlier = [
        (item, other) for other in chain.proposals.values()
        if prop.task and other.task == prop.task and other.message_id != prop.message_id
        for item in other.objections
    ]
    if earlier:
        earlier.sort(key=lambda pair: (pair[0].get("at") or 0, pair[0].get("id") or ""))
        lines.append("Earlier objections on this subtask:\n" + "\n".join(
            f"- {item.get('by')} objected to {other.author}'s push {other.message_id}: {item.get('reason') or ''}"
            for item, other in earlier
        ))
    parent = chain.by_id.get(prop.parent)
    if parent is not None:
        try:
            after = apply_diff(parent.tree, parsed.diff)
        except PatchError:
            after = None
        for path in diffstat(parsed.diff)["files"][:4]:
            if path in parent.tree:
                lines.append(f"----- {path} before this push -----\n{parent.tree[path][:4000]}")
            else:
                lines.append(f"----- {path} is new in this push -----")
            if after is None:
                continue
            if path in after:
                lines.append(f"----- {path} after this push -----\n{after[path][:4000]}")
            else:
                lines.append(f"----- {path} is deleted by this push -----")
    return "\n\n".join(lines)


def _vote_rule(prop, now: float | None) -> str:
    if prop.protected_rule == "human":
        return "This push changes the member list. It lands only when a human member replies approve."
    if prop.protected_rule == "quorum":
        return "This push changes quorum-protected paths. It lands only when a quorum of voters approves."
    if prop.protected_rule:
        return ("This push changes protected paths. It lands only when every owner approves; "
                "silence keeps it waiting, and an objection does not count.")
    if prop.window_closes_at is None:
        return "No one else owns the lines this push changes."
    left = "" if now is None else f", in about {max(0, prop.window_closes_at - now):.0f} seconds"
    return (f"If you stay silent, this push lands when its consent window closes{left}. "
            "An objection from you before then blocks it, and your reason is mailed to the author "
            "as the instruction for the revision. A vote after the window closes does not count.")


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
        parts.append("Feedback from the thread:\n" + str(feedback)[:4000])
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


def _parse_edits(raw: str, tree: dict):
    """File edits, or `NoChange` when the reply says the tip already satisfies the subtask."""
    data = _load_json(raw)
    done = ""
    if isinstance(data, dict) and "files" in data and isinstance(data.get("files"), dict):
        done = str(data.get("done") or "").strip()
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
    edits = _changed(tree, edits) if edits else {}
    if done and not edits:
        reason = NoChange(done).reason
        if reason:
            return NoChange(reason)
    return edits


def _load_json(raw: str):
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start < 0:
        return json.loads(text)
    # Models sometimes append stray text after the object; at temperature 0 a retry repeats it.
    return json.JSONDecoder().raw_decode(text, start)[0]


def _chat(api_key: str, model: str, messages: list[dict], temperature: float = 0) -> str:
    import httpx
    response = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=120,
    )
    if response.is_error:
        detail = " ".join((response.text or "").split())[:240]
        raise RuntimeError(
            f"OpenRouter {response.status_code} model {model}: {detail or response.reason_phrase}"
        )
    return response.json()["choices"][0]["message"]["content"] or ""
