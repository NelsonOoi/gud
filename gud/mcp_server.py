"""MCP server for gud. The email chain is the repo; these tools are the whole workflow.

Run: ``python -m gud.mcp_server``

One process serves the whole session. It does not bind a handle or load a private
key. Each signing call passes ``handle`` and ``private_key``.

Tool bodies live on ``GudTools`` so tests can call them without a transport.
``build_server`` registers those methods on a FastMCP server (``MCPServer`` on
mcp 2.x, which renamed FastMCP) over stdio.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import tempfile
import time
from pathlib import Path

from gud import compose
from gud.analytics import instrument, tracked, unwrap
from gud.compose import Identity
from gud.patch import apply_diff, diff_trees
from gud.tasks import derive as derive_tasks
from gud.wire import normalize, parse_body, short, test_log

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp>=2 renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

TOOL_NAMES = (
    "status", "read", "ls", "blame", "push", "export", "wait", "rebase", "reply", "log", "thread", "tasks",
)

INSTRUCTIONS = """One gud server serves this whole session. It is not bound to an agent. On every call that signs mail (push, reply, rebase), pass handle (for example @tester) and private_key (base64 raw Ed25519 private key). Pass handle on status and wait too. The mailbox is the repo: there are no branches, no pull, and no merge.

Read the tip, push small old/new edits, then wait. Re-read if old text is not found. To run tests locally, export the tip plus your edits to a scratch directory; push is still the only way to change the repo. wait tells you whether the push landed, was objected to, or lost a race. If it is superseded or stale, rebase — do not pull. When status says a push needs you, reply approve or object. Protected paths need an explicit approval or a quorum before they land. Finish a subtask by pushing with its task id.
"""


def _canon_handle(handle: str) -> str | None:
    text = str(handle).strip()
    if text.startswith("@"):
        text = text[1:].strip()
    if not text or "@" in text or any(ch.isspace() for ch in text):
        return None
    return "@" + text


def _decode_private_key(private_key: str) -> tuple[bytes | None, str | None]:
    """Base64 raw Ed25519 seed. Not checked against the roster or against disk."""
    if not isinstance(private_key, str):
        return None, "error: private_key must be base64 raw Ed25519"
    text = "".join(private_key.split())
    if not text:
        return None, "error: private_key is required"
    pad = (-len(text)) % 4
    try:
        raw = base64.b64decode(text + ("=" * pad), validate=True)
    except Exception:
        return None, "error: private_key must be base64 raw Ed25519"
    if len(raw) != 32:
        return None, "error: private_key must be a 32-byte Ed25519 private key"
    return raw, None

_REBASE_STATUSES = {"superseded", "stale", "conflict"}
_TERMINAL = {"objected", "vetoed", "tests_failed", "stale", "bad_sig", "conflict", "superseded"}
_KEEP_WAITING = {"window", "testing", "needs_approval", "needs_quorum", "qualified"}


def _path_error(path: str) -> str | None:
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return "empty or invalid path"
    raw = path.strip()
    if raw.startswith("/") or raw.startswith("~"):
        return "path escapes the tree"
    for part in raw.replace("\\", "/").split("/"):
        if part == "..":
            return "path escapes the tree"
    if not _norm_path(raw):
        return "empty or invalid path"
    return None


def _norm_path(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts)


def _binary(content: str) -> bool:
    return "\x00" in content


def _validate_edits(edits):
    """Return ([(path, old|None, new|None)], error message). old None creates; new None deletes."""
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except json.JSONDecodeError:
            return None, "edits must be a list of {path, old, new}"
    if not isinstance(edits, list) or not edits:
        return None, "edits must be a non-empty list of {path, old, new}"
    cleaned = []
    for edit in edits:
        if not isinstance(edit, dict) or "path" not in edit:
            return None, "each edit needs path, then old/new, new, or delete"
        if "content" in edit:
            return None, f"{edit.get('path')}: send old/new edits, not whole files"
        err = _path_error(str(edit["path"]))
        if err:
            return None, f"{edit.get('path')}: {err}"
        path = _norm_path(str(edit["path"]))
        if edit.get("delete"):
            if edit.get("old") is not None or edit.get("new") is not None:
                return None, f"{path}: delete takes no old or new"
            cleaned.append((path, None, None))
            continue
        old, new = edit.get("old"), edit.get("new")
        if not isinstance(new, str):
            return None, f"{path}: new must be text (delete: true removes the file)"
        if old is not None and (not isinstance(old, str) or not old):
            return None, f"{path}: old must be non-empty text"
        if _binary(new) or old is not None and _binary(old):
            return None, f"{path}: binary content is not allowed"
        cleaned.append((path, old, new))
    return cleaned, None


def _apply_edits(tree: dict, cleaned, link_id: str) -> tuple[dict, str | None]:
    """Apply validated edits in order to a copy of tree. Returns (tree, error message)."""
    out = dict(tree)
    for path, old, new in cleaned:
        if new is None:
            if path not in out:
                return out, f"no such file {path} at {link_id}; read it again"
            del out[path]
        elif old is None:
            if path in out:
                return out, f"{path} already exists at {link_id}; send old/new to change it"
            out[path] = normalize(new)
        else:
            if path not in out:
                return out, f"no such file {path} at {link_id}; read it again"
            hits = out[path].count(old)
            if hits == 0:
                return out, f"old text not found in {path} at {link_id}; read it again"
            if hits > 1:
                return out, f"old text matches {hits} places in {path}; include more surrounding lines"
            out[path] = normalize(out[path].replace(old, new, 1))
    return out, None


def _ops(before: list[str], after: list[str]) -> list[tuple[int, int, list[str]]]:
    sm = difflib.SequenceMatcher(a=before, b=after)
    return [(i1, i2, after[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _overlap(left, right) -> bool:
    for a1, a2, _ in left:
        for b1, b2, _ in right:
            if a1 == a2 and b1 == b2:
                if a1 == b1:
                    return True
                continue
            if a1 == a2:
                if b1 <= a1 < b2:
                    return True
                continue
            if b1 == b2:
                if a1 <= b1 < a2:
                    return True
                continue
            if a1 < b2 and b1 < a2:
                return True
    return False


def _fmt_ops(ops) -> str:
    bits = []
    for a1, a2, _ in ops:
        if a1 == a2:
            bits.append(f"insert at line {a1 + 1}")
        elif a2 == a1 + 1:
            bits.append(f"line {a1 + 1}")
        else:
            bits.append(f"lines {a1 + 1}-{a2}")
    return ", ".join(bits) or "nothing"


def _apply_ops(base: list[str], ops) -> list[str]:
    out: list[str] = []
    i = 0
    for a1, a2, repl in sorted(ops, key=lambda op: (op[0], op[1])):
        out.extend(base[i:a1])
        out.extend(repl)
        i = max(i, a2)
    out.extend(base[i:])
    return out


def three_way(base: dict, mine: dict, tip: dict) -> tuple[dict, list[str]]:
    """Replay ``mine``'s edits (relative to ``base``) onto ``tip``.

    Files the pusher did not touch stay as they are on the tip. Disjoint line
    edits merge. The same lines changed on both sides produce a conflict
    description and that file is left untouched.
    """
    out = dict(tip)
    conflicts: list[str] = []
    for path in sorted(set(base) | set(mine) | set(tip)):
        b, m, t = base.get(path), mine.get(path), tip.get(path)
        if m == b:
            continue
        if t == b:
            if m is None:
                out.pop(path, None)
            else:
                out[path] = m
            continue
        if t == m:
            continue
        if b is None and m is not None and t is not None:
            conflicts.append(f"{path}: both added this file with different contents")
            continue
        if m is None:
            conflicts.append(f"{path}: you deleted it but the tip changed it")
            continue
        if b is None:
            out[path] = m
            continue
        b_lines, m_lines, t_lines = b.splitlines(keepends=True), m.splitlines(keepends=True), t.splitlines(keepends=True)
        mine_ops, tip_ops = _ops(b_lines, m_lines), _ops(b_lines, t_lines)
        if _overlap(mine_ops, tip_ops):
            conflicts.append(f"{path}: tip changed {_fmt_ops(tip_ops)}; your push changed {_fmt_ops(mine_ops)}")
            continue
        merged = _apply_ops(b_lines, mine_ops + tip_ops)
        out[path] = "".join(merged)
    return out, conflicts


def _objections(push_id: str, emails) -> list[tuple[str, str]]:
    found = []
    for email in emails:
        parsed = parse_body(email.body)
        if parsed.kind == "object" and parsed.get("Gud-Target") == push_id:
            found.append((parsed.get("Gud-Author") or email.from_addr, parsed.get("Gud-Reason") or parsed.text))
            continue
        if parsed.kind is None and email.in_reply_to == push_id:
            words = parsed.text.strip().split(None, 1)
            if words and words[0].lower() in {"object", "veto"}:
                found.append((email.from_addr, words[1] if len(words) > 1 else words[0]))
    return found


def _approvers(prop, emails) -> set[str]:
    found = set()
    for item in prop.approvals or []:
        if isinstance(item, str) and item.startswith("@"):
            found.add(item)
    for email in emails:
        parsed = parse_body(email.body)
        if parsed.kind == "approve" and parsed.get("Gud-Target") == prop.message_id and parsed.get("Gud-Author"):
            found.add(parsed.get("Gud-Author"))
    return found


def _title(email, parsed) -> str:
    text = (email.subject or parsed.text or "").strip()
    return " ".join(text.split())[:120]


class GudTools:
    """Tools for one gud mailbox.

    ``identity`` is optional. The MCP process leaves it unset and takes ``handle``
    and ``private_key`` on each signing call. Tests may still bind an identity;
    that identity is used only when a call omits the corresponding argument.
    """

    def __init__(self, mailbox, identity: Identity | None = None, window_seconds: float = 60, clock=None):
        self.mailbox = mailbox
        self.identity = identity
        self.window_seconds = float(window_seconds)
        self.clock = clock or time.time
        self._repo = None

    def repo(self):
        if self._repo is None:
            from gud.chain import Repo
            self._repo = Repo(self.mailbox, self.window_seconds, clock=self.clock)
        return self._repo

    def _now(self) -> float:
        clock = self.clock
        stamp = getattr(clock, "t", None)
        if isinstance(stamp, (int, float)):
            return float(stamp)
        return float(clock())

    def _emails(self):
        return self.mailbox.list(recipient=self.mailbox.repo_address)

    def _domain(self) -> str:
        domain = getattr(self.mailbox, "domain", None)
        if isinstance(domain, str) and domain.strip():
            return domain.strip()
        repo = getattr(self.mailbox, "repo_address", "") or ""
        if "@" in repo:
            return repo.split("@", 1)[1]
        return "gud.local"

    def _address_for(self, handle: str, chain=None) -> str:
        """Tip roster address for ``handle`` when the repo has a tip, else ``handle@domain``."""
        if chain is None:
            chain = self.repo().refresh()
        tip = getattr(chain, "tip", None)
        member = (getattr(tip, "members", None) or {}).get(handle) if tip is not None else None
        address = getattr(member, "address", None) if member is not None else None
        if address:
            return str(address)
        return f"{handle.lstrip('@')}@{self._domain()}"

    def _call_identity(self, handle: str | None, private_key: str | None, *, sign: bool, chain=None):
        """Identity for this call only. Arguments win over a bound test identity."""
        if handle is None or not str(handle).strip():
            if self.identity is None or not self.identity.handle:
                return None, "error: handle is required"
            named = self.identity.handle
        else:
            named = _canon_handle(handle)
            if not named:
                return None, "error: invalid handle"
        omitted = private_key is None or (isinstance(private_key, str) and not private_key.strip())
        if omitted:
            raw = None
            if sign:
                bound = self.identity
                if bound is None or bound.handle != named or not bound.private_raw:
                    return None, "error: private_key is required"
                raw = bound.private_raw
        elif not isinstance(private_key, str):
            return None, "error: private_key must be base64 raw Ed25519"
        else:
            raw, err = _decode_private_key(private_key)
            if err:
                return None, err
        return Identity(named, self._address_for(named, chain), raw), None

    def _inbox(self, ident: Identity):
        return self.mailbox.list(recipient=ident.address)

    def _max_received(self, ident: Identity) -> float:
        mail = self._inbox(ident)
        return mail[-1].received_at if mail else 0.0

    def _find(self, message_id: str, chain=None):
        if chain is not None:
            for email in chain.emails or []:
                if email.message_id == message_id:
                    return email
        for email in self._emails():
            if email.message_id == message_id:
                return email
        return None

    def _forecast(self, parent, diff: str, new_tree: dict, ident: Identity) -> tuple[list[str], str | None, str]:
        from gud import rules
        author = ident.handle
        owners = sorted(o for o in rules.affected_owners(parent, diff, author) if o != author)
        rule = rules.protection(parent, diff)
        from gud.members import touches_members
        if touches_members(diff) and rule is None and not owners:
            return owners, "human", "needs approval from a human member"
        if rule == "quorum":
            voters = list(rules.quorum_voters(parent, new_tree))
            needed = len(voters) // 2 + 1
            return owners, rule, f"needs quorum {needed}/{len(voters)}"
        if rule == "explicit":
            if owners:
                return owners, rule, "needs approval from " + ", ".join(owners)
            return owners, rule, "needs approval from any other voter"
        if not owners:
            return owners, rule, "no affected owners; lands when tests pass"
        who = ", ".join(owners)
        verb = "objects" if len(owners) == 1 else "object"
        return owners, rule, f"lands in {int(self.window_seconds)}s unless {who} {verb}"

    def _blocking(self, prop, emails) -> str:
        objections = _objections(prop.message_id, emails)
        if prop.status == "window":
            if prop.window_closes_at:
                remain = max(0, int(round(prop.window_closes_at - self._now())))
                return f"window closes in {remain}s"
            return prop.reason or "window open"
        if prop.status == "objected" or objections and prop.status == "window":
            if objections:
                who, reason = objections[-1]
                return f"objection by {who}: {reason}"
        if prop.status == "objected" and objections:
            who, reason = objections[-1]
            return f"objection by {who}: {reason}"
        if prop.status == "needs_approval" and prop.protected_rule == "human":
            return "needs approval from a human member"
        if prop.status == "needs_approval":
            have = _approvers(prop, emails)
            missing = [o for o in (prop.affected_owners or []) if o not in have]
            if not missing and not prop.affected_owners:
                return "needs approval from [any other voter]"
            return "needs approval from [" + ", ".join(missing or prop.affected_owners or []) + "]"
        if prop.status == "needs_quorum":
            have = prop.quorum_have if prop.quorum_have is not None else len(_approvers(prop, emails))
            need = prop.quorum_needed if prop.quorum_needed is not None else "?"
            return f"needs quorum {have}/{need}"
        if prop.status == "testing":
            return "testing"
        if prop.status == "vetoed":
            return prop.reason or "vetoed"
        return prop.reason or prop.status

    def _needs_me(self, prop, chain, emails, ident: Identity) -> bool:
        me = ident.handle
        if prop.author == me or prop.status not in {"window", "needs_approval", "needs_quorum", "testing"}:
            return False
        if me in _approvers(prop, emails):
            return False
        if me in {who for who, _ in _objections(prop.message_id, emails)}:
            return False
        if prop.protected_rule == "human":
            return False
        if me in (prop.affected_owners or []):
            return True
        parent = chain.by_id.get(prop.parent)
        if parent is None or me not in parent.members or not parent.members[me].votes:
            return False
        if prop.status == "needs_quorum" or prop.protected_rule == "quorum":
            return prop.status == "needs_quorum"
        if prop.status == "needs_approval" and not prop.affected_owners:
            return True
        return False

    def _why_me(self, prop, ident: Identity) -> str:
        me = ident.handle
        if me in (prop.affected_owners or []):
            if prop.protected_rule == "explicit":
                return "you own lines; approval required"
            return "you own lines in this push"
        if prop.protected_rule == "quorum" or prop.status == "needs_quorum":
            return "quorum vote"
        return "approval required"

    def _task_index(self, emails, chain):
        tasks: dict[str, dict] = {}
        order: list[str] = []
        for email in emails:
            parsed = parse_body(email.body)
            if parsed.kind == "task":
                tid = parsed.get("Gud-Task")
                if not tid or tid in tasks:
                    continue
                tasks[tid] = {
                    "id": tid, "title": _title(email, parsed), "description": parsed.text,
                    "requester": parsed.get("Gud-Requester") or "", "status": "open", "subtasks": [],
                }
                order.append(tid)
            elif parsed.kind == "subtask":
                tid = parsed.get("Gud-Task")
                parent = parsed.get("Gud-Parent-Task") or ""
                if parent and parent not in tasks:
                    tasks[parent] = {
                        "id": parent, "title": parent, "description": "", "requester": "",
                        "status": "open", "subtasks": [],
                    }
                    order.append(parent)
                sub = {
                    "id": tid or "", "assignee": parsed.get("Gud-Assignee") or "",
                    "title": _title(email, parsed), "email_id": email.message_id, "status": "open",
                }
                if parent:
                    tasks[parent]["subtasks"].append(sub)
            elif parsed.kind == "task-done":
                tid = parsed.get("Gud-Task")
                if tid in tasks:
                    tasks[tid]["status"] = "done"
        landed = set()
        by_id = {email.message_id: email for email in emails}
        for link in chain.links:
            if getattr(link, "task", None):
                landed.add(link.task)
            email = by_id.get(link.message_id)
            if email is None:
                continue
            task = parse_body(email.body).get("Gud-Task")
            if task:
                landed.add(task)
        for task in tasks.values():
            for sub in task["subtasks"]:
                if sub["id"] in landed:
                    sub["status"] = "landed"
            if task["status"] != "done" and task["subtasks"]:
                if any(sub["status"] == "landed" for sub in task["subtasks"]):
                    task["status"] = "in_progress"
        derived = {item.id: item for item in derive_tasks(chain)}
        for task in tasks.values():
            src = derived.get(task["id"])
            if src is None:
                continue
            by_id = {sub.id: sub for sub in src.subtasks}
            for sub in task["subtasks"]:
                match = by_id.get(sub["id"])
                if match is not None and match.status == "no-change":
                    sub["status"] = "no-change"
            if src.status == "done":
                task["status"] = "done"
        return tasks, order

    def _open_subtasks(self, chain, emails, ident: Identity) -> list[dict]:
        tasks, order = self._task_index(emails, chain)
        me = ident.handle
        out = []
        for tid in order:
            for sub in tasks[tid]["subtasks"]:
                if sub["assignee"] == me and sub["status"] not in {"landed", "no-change"}:
                    out.append(sub)
        return out

    def _send_push(self, *, intent: str, tree: dict, task: str | None, parent, ident: Identity) -> str:
        body, _state, _tree_hash = compose.push_body(
            ident, parent=parent.message_id, parent_state=parent.state,
            parent_tree=parent.tree, new_tree=tree, intent=intent, task=task,
        )
        subject = "[gud] " + " ".join(intent.split())[:80]
        email = compose.send(
            self.mailbox, ident, parent.members, subject, body, in_reply_to=parent.message_id,
        )
        diff = parse_body(body).diff
        try:
            owners, rule, nxt = self._forecast(parent, diff, tree, ident)
        except Exception as exc:
            return "\n".join([email.message_id, f"error: rules failed ({exc})"])
        consent = "lazy consensus" if rule is None and owners else (rule or "none")
        return "\n".join([
            email.message_id,
            "affected: " + (", ".join(owners) if owners else "none"),
            "protection: " + (rule or "none"),
            "consent: " + consent,
            "next: " + nxt,
        ])

    def _parent(self, base: str | None):
        chain = self.repo().refresh()
        if base:
            parent = chain.by_id.get(base)
            if parent is None:
                return chain, None, f"error: unknown base {base}"
            return chain, parent, None
        if chain.tip is None:
            return chain, None, "error: no tip"
        return chain, chain.tip, None

    @tracked("gud")
    def status(self, handle: str | None = None) -> str:
        """Show the tip, your pending pushes and what blocks them, pushes that need your reply, and open subtasks assigned to you.

        Pass handle (for example @alice) so "me" is that agent. There is no pull. A pending push names the blocker: window closes in Ns, objection by @someone, needs approval from [..], or testing.
        """
        chain = self.repo().refresh()
        ident, err = self._call_identity(handle, None, sign=False, chain=chain)
        if err:
            return err
        emails = self._emails()
        tip = chain.tip
        lines = []
        if tip is None:
            lines.append("tip: none")
        else:
            title = tip.subject or tip.intent or ""
            lines.append(f"tip {tip.seq} {short(tip.state)} {tip.author} {' '.join(title.split())}")
        me = ident.handle
        mine = [p for p in chain.proposals.values() if p.author == me]
        lines.append("mine:" if mine else "mine: none")
        for prop in sorted(mine, key=lambda p: p.received_at):
            lines.append(f"  {prop.message_id} {prop.status} — {self._blocking(prop, emails)}")
        need = [p for p in chain.proposals.values() if self._needs_me(p, chain, emails, ident)]
        lines.append("needs-me:" if need else "needs-me: none")
        for prop in sorted(need, key=lambda p: p.received_at):
            lines.append(f"  {prop.message_id} {prop.author} {prop.status} — {self._why_me(prop, ident)}")
        subs = self._open_subtasks(chain, emails, ident)
        lines.append("subtasks:" if subs else "subtasks: none")
        for sub in subs:
            lines.append(f"  {sub['id']} {sub['title']}")
        return "\n".join(lines)

    @tracked("gud")
    def read(self, path: str, at: str | None = None) -> str:
        """Read a file at the tip, or at a link id passed as at. Returns the full text."""
        err = _path_error(path)
        if err:
            return f"error: {err}"
        path = _norm_path(path)
        tree = self.repo().tree(at)
        if path not in tree:
            return f"error: no such file {path}"
        return tree[path]

    @tracked("gud")
    def ls(self, prefix: str = "") -> str:
        """List repo paths. prefix is a path prefix, not a glob. Includes .gud."""
        tree = self.repo().tree()
        paths = sorted(p for p in tree if p.startswith(prefix or ""))
        return "\n".join(paths) if paths else "(empty)"

    @tracked("gud")
    def blame(self, path: str) -> str:
        """Who owns each line of path at the tip. Compact ranges, one owner per line: 1-4 @alice."""
        err = _path_error(path)
        if err:
            return f"error: {err}"
        path = _norm_path(path)
        rows = list(self.repo().blame(path) or [])
        if not rows:
            return f"error: no such file {path}"
        ranges = []
        start = 1
        owner = rows[0][0]
        for i, row in enumerate(rows, start=1):
            if row[0] != owner:
                ranges.append((start, i - 1, owner))
                start, owner = i, row[0]
        ranges.append((start, len(rows), owner))
        lines = []
        for a, b, who in ranges:
            lines.append(f"{a} {who}" if a == b else f"{a}-{b} {who}")
        return "\n".join(lines)

    @tracked("gud")
    def push(self, intent: str, edits: list[dict], task: str | None = None, base: str | None = None,
             handle: str | None = None, private_key: str | None = None) -> str:
        """Publish edits on the tip (or on the link base). This is how you change the repo.

        Pass handle (for example @alice) and private_key (base64 raw Ed25519 private key). They sign this call only.
        Read the tip, push small old/new edits, then wait. Re-read if old text is not found.
        edits is a list, applied in order to the file as it is on the tip at push time:
          {"path": "a.py", "old": "exact text, once in the file", "new": "replacement"} replaces;
          {"path": "a.py", "new": "full text"} creates a file that does not exist;
          {"path": "a.py", "delete": true} removes it.
        Include enough surrounding lines in old to match exactly one place. Lines others landed since
        your read are kept. task sets Gud-Task (use it to finish a subtask). intent is the note other agents read.

        Returns the push id, affected owners, the protection rule, and what happens next
        (lands in Ns unless an owner objects, needs approval, or needs quorum). Then call wait.
        Refuses paths outside the tree and binary files.
        """
        cleaned, err = _validate_edits(edits)
        if err:
            return f"error: {err}"
        _chain, parent, err = self._parent(base)
        if err:
            return err
        tree, err = _apply_edits(parent.tree, cleaned, parent.message_id)
        if err:
            return f"error: {err}"
        if not diff_trees(parent.tree, tree).strip():
            return "error: no changes"
        ident, err = self._call_identity(handle, private_key, sign=True, chain=_chain)
        if err:
            return err
        return self._send_push(intent=intent, tree=tree, task=task, parent=parent, ident=ident)

    @tracked("gud")
    def export(self, dir: str | None = None, at: str | None = None, edits: list[dict] | None = None) -> str:
        """Write the tree at the tip (or at the link at) into a scratch directory to run tests locally.

        dir must be missing or empty; with no dir a fresh temp dir is made. edits, in the same format as push,
        are applied on top so you can test exactly what you are about to push. Nothing reads the directory
        back: change the repo with push. Returns the directory, the link id, the file count, and edits applied.
        """
        cleaned = []
        if edits:
            cleaned, err = _validate_edits(edits)
            if err:
                return f"error: {err}"
        _chain, link, err = self._parent(at)
        if err:
            return err
        tree, err = _apply_edits(link.tree, cleaned, link.message_id)
        if err:
            return f"error: {err}"
        for path in tree:
            err = _path_error(path)
            if err:
                return f"error: {path}: {err}"
        if dir:
            root = Path(dir).expanduser()
            if root.exists() and (not root.is_dir() or any(root.iterdir())):
                return f"error: {root} exists and is not an empty directory"
            root.mkdir(parents=True, exist_ok=True)
        else:
            root = Path(tempfile.mkdtemp(prefix="gud-"))
        for path, text in tree.items():
            target = root / _norm_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return "\n".join([
            str(root),
            f"at: {link.message_id}",
            f"files: {len(tree)}",
            f"edits: {len(cleaned)}",
        ])

    def _push_outcome(self, push_id: str, chain, emails) -> str | None:
        for link in chain.links:
            if link.message_id == push_id:
                return f"landed {push_id} seq {link.seq} {short(link.state)}"
        prop = chain.proposals.get(push_id)
        objections = _objections(push_id, emails)
        if objections and (prop is None or prop.status in _KEEP_WAITING or prop.status == "objected"):
            who, reason = objections[-1]
            return f"objected {push_id} — objection by {who}: {reason}"
        if prop is None:
            return None
        if prop.status in _TERMINAL or prop.test_result == "fail":
            status = "tests_failed" if prop.test_result == "fail" and prop.status not in _TERMINAL else prop.status
            line = f"{status} {push_id} — {self._blocking(prop, emails)}"
            log = test_log(emails, push_id) if status == "tests_failed" else ""
            return f"{line}\n{log}" if log else line
        return None

    @tracked("gud")
    def wait(self, push_id: str | None = None, timeout: float = 60, handle: str | None = None) -> str:
        """Block until your push lands, is rejected, gets an objection, or new mail needs your response. This replaces pull.

        Pass handle (for example @alice) so "me" is that agent. Pass the push id from push. On superseded or stale, call rebase. timeout is seconds.
        """
        ident, err = self._call_identity(handle, None, sign=False)
        if err:
            return err
        deadline = time.monotonic() + float(timeout)
        seen = {email.message_id for email in self._inbox(ident)}
        since = self._max_received(ident)
        while True:
            chain = self.repo().refresh()
            emails = self._emails()
            if push_id:
                outcome = self._push_outcome(push_id, chain, emails)
                if outcome:
                    return outcome
            for prop in chain.proposals.values():
                if prop.message_id in seen or prop.author == ident.handle:
                    continue
                if self._needs_me(prop, chain, emails, ident):
                    return f"needs-you {prop.message_id} {prop.author} {prop.status} — {self._why_me(prop, ident)}"
            for sub in self._open_subtasks(chain, emails, ident):
                if sub.get("email_id") not in seen:
                    return f"subtask {sub['id']} {sub['title']}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout\n" + self.status(ident.handle)
            new = self.mailbox.wait(
                recipient=ident.address, since=since, timeout=min(0.4, remaining),
            )
            if new:
                since = max(since, max(email.received_at for email in new))
                for email in new:
                    seen.discard(email.message_id)

    @tracked("gud")
    def rebase(self, push_id: str, handle: str | None = None, private_key: str | None = None) -> str:
        """If your push lost a race (superseded or stale), replay your file edits onto the current tip and push again.

        Pass handle and private_key (base64 raw Ed25519) to sign the new push as that agent.
        If the tip changed the same lines, returns a conflict description and does not push. Otherwise returns the new push id.
        """
        chain = self.repo().refresh()
        tip = chain.tip
        if tip is None:
            return "error: no tip"
        if any(link.message_id == push_id for link in chain.links):
            return f"error: {push_id} already landed"
        prop = chain.proposals.get(push_id)
        email = self._find(push_id, chain)
        if email is None:
            return f"error: unknown push {push_id}"
        parsed = parse_body(email.body)
        parent_id = prop.parent if prop is not None else parsed.get("Gud-Parent")
        parent = chain.by_id.get(parent_id) if parent_id else None
        if parent is None:
            return "error: parent link is gone"
        moved = parent.message_id != tip.message_id
        status = prop.status if prop is not None else "unknown"
        if not moved and status not in _REBASE_STATUSES:
            return f"error: not rebaseable ({status})"
        try:
            mine = apply_diff(dict(parent.tree), parsed.diff)
        except Exception as exc:
            return f"error: cannot apply original diff ({exc})"
        merged, conflicts = three_way(parent.tree, mine, tip.tree)
        if conflicts:
            return "conflict\n" + "\n".join(conflicts)
        task = (prop.task if prop is not None else None) or parsed.get("Gud-Task")
        intent = parsed.text or (prop.subject if prop is not None else "") or "rebase"
        ident, err = self._call_identity(handle, private_key, sign=True, chain=chain)
        if err:
            return err
        return self._send_push(intent=f"rebase: {intent}"[:200], tree=merged, task=task, parent=tip, ident=ident)

    @tracked("gud")
    def reply(self, target: str, action: str, reason: str = "", handle: str | None = None,
              private_key: str | None = None) -> str:
        """Respond to a push. action is approve or object. object needs a reason.

        Pass handle and private_key (base64 raw Ed25519). The reply is signed with that key as that handle, for this call only.
        """
        action = (action or "").strip().lower()
        chain = self.repo().refresh()
        prop = chain.proposals.get(target)
        link = chain.by_id.get(target)
        if prop is not None and prop.parent in chain.by_id:
            members = chain.by_id[prop.parent].members
        elif link is not None:
            members = link.members
        elif chain.tip is not None:
            members = chain.tip.members
        else:
            return "error: no members"
        if action not in {"approve", "object"}:
            return "error: action must be approve or object"
        if action == "object" and not reason.strip():
            return "error: object needs a reason"
        ident, err = self._call_identity(handle, private_key, sign=True, chain=chain)
        if err:
            return err
        if action == "approve":
            body = compose.approve_body(ident, target, reason or "Approved.")
        else:
            body = compose.object_body(ident, target, reason.strip())
        email = compose.send(
            self.mailbox, ident, members, f"Re: {action} {target}", body, in_reply_to=target,
        )
        return f"{action} {email.message_id} target={target}"

    @tracked("gud")
    def log(self, limit: int = 20) -> str:
        """Recent links, oldest of this window first. One line each: seq, short state, author, subject."""
        chain = self.repo().refresh()
        rows = list(chain.links)[-int(limit):]
        if not rows:
            return "(no links)"
        lines = []
        for link in rows:
            title = " ".join((link.subject or link.intent or "").split())
            lines.append(f"{link.seq} {short(link.state)} {link.author} {title}".rstrip())
        return "\n".join(lines)

    @tracked("gud")
    def thread(self, message_id: str) -> str:
        """The push and its replies, oldest first: kind, author, id, and the text."""
        try:
            emails = list(self.repo().thread(message_id) or [])
        except NotImplementedError:
            emails = self._thread_fallback(message_id)
        if not emails:
            return f"error: no thread for {message_id}"
        lines = []
        for email in emails:
            parsed = parse_body(email.body)
            kind = parsed.kind or "human"
            author = parsed.get("Gud-Author") or email.from_addr
            extra = ""
            if parsed.get("Gud-Reason"):
                extra = " reason=" + parsed.get("Gud-Reason")
            elif parsed.get("Gud-Result"):
                extra = " result=" + parsed.get("Gud-Result")
            lines.append(f"{kind} {author} {email.message_id}{extra}")
            text = " ".join(parsed.text.split())[:240]
            if text:
                lines.append(f"  {text}")
        return "\n".join(lines)

    def _thread_fallback(self, message_id: str):
        emails = self._emails()
        out = []
        for email in emails:
            parsed = parse_body(email.body)
            if email.message_id == message_id or email.in_reply_to == message_id or parsed.get("Gud-Target") == message_id:
                out.append(email)
        return out

    @tracked("gud")
    def tasks(self) -> str:
        """Tasks and subtasks from task, subtask, task-done, and no-change emails. A subtask is landed when a link carries its Gud-Task, or no-change when the assignee signed that nothing needs to change."""
        chain = self.repo().refresh()
        tasks, order = self._task_index(self._emails(), chain)
        if not order:
            return "(no tasks)"
        lines = []
        for tid in order:
            task = tasks[tid]
            lines.append(f"{task['id']} {task['status']} {task['requester']} {task['title']}".rstrip())
            for sub in task["subtasks"]:
                lines.append(f"  {sub['id']} {sub['assignee']} {sub['status']} {sub['title']}".rstrip())
        return "\n".join(lines)


def build_server(tools: GudTools):
    """FastMCP server whose tools are ``tools`` methods. Armature instruments registration when configured."""
    mcp = FastMCP("gud", instructions=INSTRUCTIONS)
    actor = tools.identity.handle if tools.identity is not None else None
    hooked = instrument(mcp, actor=actor)
    for name in TOOL_NAMES:
        method = getattr(tools, name)
        if hooked is not None:
            method = unwrap(method)
        mcp.tool(name=name, structured_output=False)(method)
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gud.mcp_server", description="gud MCP server (stdio)")
    parser.parse_args(argv)
    from gud.config import load
    cfg = load()
    try:
        from gud.mailbox import open_mailbox
    except ImportError as exc:
        raise SystemExit(f"gud.mailbox.open_mailbox is not available ({exc})") from exc
    # Not a member of the repo. Signing calls carry handle and private_key.
    tools = GudTools(open_mailbox(cfg), window_seconds=cfg.window_seconds)
    build_server(tools).run("stdio")


if __name__ == "__main__":
    main()
