"""Test kit for v1 replay scenarios: members and keys, a signed host log, and signed v1 messages of
every kind, with a controllable clock.

    k = Kit()                                  # members, genesis files, host log
    g = k.genesis({"app.py": "a = 1\\n"})       # signed by @nelson, logged now
    p = k.push("@alice", g, {"app.py": "a = 2\\n"})
    k.tick(61_000)
    chain = k.replay()

Every message is signed when built and logged (given a received time in the host log) unless
`log=False`; `k.log_msg(msg, at=...)` logs it later. Received times only ever come from the log.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from gud.core import envelope as E
from gud.core.config import MEMBERS, OWNERS, TEST, CONFIG
from gud.core.hostlog import HostLog
from gud.core.objects import MemoryStore, Tree
from gud.core.ops import ChangeOp, apply_ops, diff_trees, invert_ops, ops_to_records
from gud.core.replay import Replayer, carried_state, replay
from gud.core.types import ZERO, Email, Envelope, LogEntry, Record, Seen

REPO = "repo@example.org"
DOMAIN = "example.org"
AUTHSERV = "mx.example.org"
T0 = 1_700_000_000_000

DEFAULT_MEMBERS = (
    ("@nelson", "human", None), ("@alice", "agent", "@nelson"), ("@bob", "agent", "@nelson"),
    ("@carol", "agent", "@nelson"), ("@t1", "tester", "@nelson"), ("@dana", "human", None),
)


def _key(handle: str) -> bytes:
    return hashlib.sha256(b"gud-test-key\0" + handle.encode()).digest()


@dataclass
class Msg:
    """One built message: its email, envelope and (for pushes) the sender-side tree."""
    email: Email
    env: Envelope | None
    tree: Tree | None = None
    logged_at: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.email.gud_id

    def carried(self, onto: "Msg | str") -> str:
        return carried_state(self.id, onto if isinstance(onto, str) else onto.id)


class Kit:
    def __init__(self, members: Sequence[tuple[str, str, str | None]] = DEFAULT_MEMBERS, *,
                 config: str = "", owners: str = "", test: str = "", authserv: bool = True,
                 t0: int = T0) -> None:
        self.now = t0
        self.store = MemoryStore()
        self.host_key = _key("@host")
        self.log = HostLog.open(REPO, self.host_key, self.now)
        self.members = list(members)
        self.config = config + (f"authserv-id {AUTHSERV}\n" if authserv and "authserv-id" not in config else "")
        self.owners = owners
        self.test_policy = test
        self.emails: list[Email] = []
        self.msgs: list[Msg] = []
        self.trees: dict[str, Tree] = {}
        self._n = 0

    # ------------------------------------------------------------ clock and log

    def tick(self, ms: int) -> None:
        self.now += ms

    def key(self, handle: str) -> bytes:
        return _key(handle)

    def pub(self, handle: str) -> str:
        return E.public_of(_key(handle))

    def address(self, handle: str) -> str:
        return f"{handle.lstrip('@')}@{DOMAIN}"

    def seen(self) -> Seen:
        head = self.log.head
        assert head is not None
        return Seen(head.seq, head.hash)

    def log_msg(self, msg: Msg, at: int | None = None) -> Msg:
        """Append the message's mail entry at received time `at` (default: now, then +1 ms)."""
        when = self.now if at is None else at
        entry = self.log.append_mail(msg.id, when, self.host_key, max(when, self.log.head.time + 1))
        msg.logged_at = entry.time
        if at is None:
            self.now = max(self.now, entry.time) + 1
        return msg

    def tick_entry(self, at: int | None = None) -> LogEntry:
        when = self.now if at is None else at
        entry = self.log.append_tick(self.host_key, max(when, self.log.head.time + 1))
        self.now = max(self.now, entry.time) + 1
        return entry

    def entries(self) -> list[LogEntry]:
        return self.log.entries()

    # ------------------------------------------------------------ files

    def members_file(self, members: Iterable[tuple[str, str, str | None]] | None = None) -> bytes:
        lines = ["# handle role address key sponsor"]
        for handle, role, sponsor in (members if members is not None else self.members):
            lines.append(f"{handle} {role} {self.address(handle)} {self.pub(handle)} {sponsor or '-'}")
        lines.append(f"@host host {REPO} {self.pub('@host')} -")
        return ("\n".join(lines) + "\n").encode()

    def gud_files(self) -> dict[str, bytes]:
        files = {MEMBERS: self.members_file()}
        if self.config:
            files[CONFIG] = self.config.encode()
        if self.owners:
            files[OWNERS] = self.owners.encode()
        if self.test_policy:
            files[TEST] = self.test_policy.encode()
        return files

    # ------------------------------------------------------------ building messages

    def _email(self, env: Envelope, *, subject: str = "", in_reply_to: str | None = None) -> Email:
        gid = E.gud_id(env)
        self._n += 1
        mid = f"<m{self._n}.{gid[:8]}@{DOMAIN}>"
        raw = (f"Message-ID: {mid}\r\nSubject: {subject} {E.subject_tag(gid)}\r\n\r\n").encode() + E.encode(env)
        return Email(gud_id=gid, raw_mime=raw, message_id=mid, from_addr=self.address(env.author or "@x"),
                     to=(REPO,), cc=(), subject=f"[gud] {env.kind} {subject} {E.subject_tag(gid)}",
                     in_reply_to=in_reply_to, references=(), auth_results=(), text="", envelope=env)

    def sign(self, kind: str, author: str, headers: Mapping[str, str], records: Sequence[Record] = (), *,
             sent: int | None = None, seen: Seen | None = None, key: bytes | None = None,
             log: bool = True, at: int | None = None, subject: str = "") -> Msg:
        sent_s = (self.now if sent is None else sent) // 1000
        env = E.build(kind, author=author, headers=dict(headers), records=records, sent=sent_s,
                      seen=self.seen() if seen is None and kind != "join" else seen)
        problem = E.validate_kind(env)
        assert problem is None, problem
        env = E.sign(env, key if key is not None else _key(author))
        msg = Msg(self._email(env, subject=subject), env)
        self.emails.append(msg.email)
        self.msgs.append(msg)
        if log:
            self.log_msg(msg, at)
        return msg

    def raw_envelope(self, env: Envelope, *, log: bool = True, at: int | None = None) -> Msg:
        """Send an already built (possibly unsigned or mis-signed) envelope as is."""
        msg = Msg(self._email(env), env)
        self.emails.append(msg.email)
        self.msgs.append(msg)
        if log:
            self.log_msg(msg, at)
        return msg

    def tree(self, state: "Msg | str") -> Tree:
        sid = state if isinstance(state, str) else state.id
        if sid in self.trees:
            return self.trees[sid]
        chain = self.replayer().reader()
        link = chain.chain.link(sid)
        if link is not None:
            return chain.tree(sid)
        tree = chain.candidate_tree(sid)
        assert tree is not None, f"unknown state {sid[:12]}"
        return tree

    @staticmethod
    def _bytes(value: Any) -> bytes:
        return value.encode() if isinstance(value, str) else value

    def edit_ops(self, base: Tree, edits: Mapping[str, Any]) -> tuple[Tree, tuple[ChangeOp, ...]]:
        """edits: path -> str/bytes content, None (delete), or (mode, content)."""
        new = base
        for path in sorted(edits):
            value = edits[path]
            if value is None:
                new = new.delete(path)
            elif isinstance(value, tuple):
                new = new.set(path, value[0], self._bytes(value[1]))
            else:
                mode = base.get(path).mode if path in base else "100644"
                new = new.set(path, mode, self._bytes(value))
        return new, diff_trees(base, new, inline_budget=1 << 30)

    def genesis(self, files: Mapping[str, Any], author: str = "@nelson", *, gud: bool = True, **kw) -> Msg:
        content = dict(self.gud_files()) if gud else {}
        content.update({p: self._bytes(v) for p, v in files.items()})
        new, ops = self.edit_ops(Tree.empty(self.store), content)
        return self._push_msg(author, ZERO, new, ops, intent="genesis", **kw)

    def push(self, author: str, parent: "Msg | str", edits: Mapping[str, Any] | None = None, *,
             ops: Sequence[ChangeOp] | None = None, task: str | None = None, revises: "Msg | str | None" = None,
             carry: bool = True, intent: str = "change", tree_hash: str | None = None, **kw) -> Msg:
        parent_id = parent if isinstance(parent, str) else parent.id
        base = self.tree(parent)
        if ops is None:
            new, ops = self.edit_ops(base, edits or {})
        else:
            try:
                new = apply_ops(base, ops, _StoreBlobs(self.store))
            except Exception:
                if tree_hash is None:
                    raise
                new = base                    # deliberately broken push

        headers = {}
        if task:
            headers["Gud-Task"] = task if isinstance(task, str) else task.id
        if revises is not None:
            headers["Gud-Revises"] = revises if isinstance(revises, str) else revises.id
        if not carry:
            headers["Gud-Carry"] = "none"
        return self._push_msg(author, parent_id, new, tuple(ops), intent=intent, headers=headers,
                              tree_hash=tree_hash, **kw)

    def _push_msg(self, author: str, parent: str, new: Tree, ops: Sequence[ChangeOp], *, intent: str,
                  headers: Mapping[str, str] | None = None, tree_hash: str | None = None, **kw) -> Msg:
        h = {"Gud-Parent": parent, "Gud-Tree": tree_hash or new.hash(), **(headers or {})}
        records = (Record("text", (), intent.encode()),) + ops_to_records(ops)
        msg = self.sign("push", author, h, records, **kw)
        msg.tree = new
        self.trees[msg.id] = new
        return msg

    def revert(self, author: str, parent: "Msg | str", link: "Msg | str", *, before: "Msg | str",
               task: str | None = None, **kw) -> Msg:
        """Revert of `link` (whose parent is `before`) onto `parent`."""
        onto = self.tree(parent)
        new, conflicts = invert_ops(self.tree(before), self.tree(link), onto)
        assert not conflicts, conflicts
        h = {"Gud-Parent": parent if isinstance(parent, str) else parent.id,
             "Gud-Revert": link if isinstance(link, str) else link.id, "Gud-Tree": new.hash()}
        if task:
            h["Gud-Task"] = task
        msg = self.sign("revert", author, h, (Record("text", (), b"revert"),), **kw)
        msg.tree = new
        self.trees[msg.id] = new
        return msg

    def _target(self, target: "Msg | str") -> str:
        return target if isinstance(target, str) else target.id

    def approve(self, who: str, target: "Msg | str", text: str = "", **kw) -> Msg:
        records = (Record("text", (), text.encode()),) if text else ()
        return self.sign("approve", who, {"Gud-Target": self._target(target)}, records, **kw)

    def hold(self, who: str, target: "Msg | str", **kw) -> Msg:
        return self.sign("hold", who, {"Gud-Target": self._target(target)}, **kw)

    def object(self, who: str, target: "Msg | str", reason: str = "no", *, evidence: tuple[str, bytes] | None = None,
               **kw) -> Msg:
        records = [Record("text", (), reason.encode())]
        if evidence is not None:
            path, data = evidence
            records.append(Record("evidence", (path, hashlib.sha256(data).hexdigest(), str(len(data))), data))
        return self.sign("object", who, {"Gud-Target": self._target(target)}, records, **kw)

    def veto(self, who: str, target: "Msg | str", reason: str = "veto: no", **kw) -> Msg:
        return self.object(who, target, reason if reason.lower().startswith("veto") else "veto " + reason, **kw)

    def comment(self, who: str, target: "Msg | str", text: str = "ok", *, task: str | None = None,
                escalate: str | None = None, verdict: str | None = None, **kw) -> Msg:
        h = {"Gud-Target": self._target(target)}
        if task:
            h["Gud-Task"] = task
        if escalate:
            h["Gud-Escalate"] = escalate
        if verdict:
            h["Gud-Verdict"] = verdict
        return self.sign("comment", who, h, (Record("text", (), text.encode()),), **kw)

    def test(self, scope: str, target: "Msg | str", tree: str, result: str = "pass", *, tester: str = "@t1",
             stack: Sequence[str] | None = None, on: str | None = None, base: str | None = None,
             link: str | None = None, **kw) -> Msg:
        h = {"Gud-Scope": scope, "Gud-Target": self._target(target), "Gud-Tree": tree, "Gud-Result": result}
        if stack is not None:
            h["Gud-Stack"] = E.render_list(list(stack))
        if on:
            h["Gud-On"] = on
        if base:
            h["Gud-Base-Result"] = base
        if link:
            h["Gud-Link"] = link
        return self.sign("test", tester, h, **kw)

    def test_push(self, cand: str, tree: Tree, result: str = "pass", **kw) -> Msg:
        return self.test("push", cand, tree.hash(), result, **kw)

    def test_tip(self, link: str, tree: Tree, result: str = "pass", **kw) -> Msg:
        return self.test("tip", link, tree.hash(), result, **kw)

    def task(self, who: str, title: str, *, accept: Sequence[str] = (), cmds: Sequence[str] = (),
             requester: str | None = None, bug: str | None = None, text: str = "", **kw) -> Msg:
        h = {"Gud-Title": title}
        if accept:
            h["Gud-Accept"] = E.render_list(list(accept))
        if requester:
            h["Gud-Requester"] = requester
        if bug:
            h["Gud-Bug"] = bug
        records = []
        if text:
            records.append(Record("text", (), text.encode()))
        if cmds:
            records.append(Record("accept", (), "\n".join(cmds).encode()))
        return self.sign("task", who, h, records, **kw)

    def subtask(self, who: str, parent: "Msg | str", title: str, *, files: Sequence[str] = (),
                after: Sequence[str] = (), accept: Sequence[str] = (), **kw) -> Msg:
        h = {"Gud-Parent-Task": self._target(parent), "Gud-Title": title}
        if files:
            h["Gud-Files"] = E.render_list(list(files))
        if after:
            h["Gud-After"] = E.render_list(list(after))
        if accept:
            h["Gud-Accept"] = E.render_list(list(accept))
        return self.sign("subtask", who, h, **kw)

    def claim(self, who: str, task: "Msg | str", files: Sequence[str] = ("**",), *, revises: "Msg | str | None" = None,
              release: bool = False, after: Sequence[str] = (), **kw) -> Msg:
        h = {"Gud-Task": self._target(task), "Gud-Files": E.render_list(list(files))}
        if revises is not None:
            h["Gud-Revises"] = self._target(revises)
        if release:
            h["Gud-Release"] = "yes"
        if after:
            h["Gud-After"] = E.render_list(list(after))
        return self.sign("claim", who, h, **kw)

    def no_change(self, who: str, task: "Msg | str", tip: "Msg | str", reason: str = "already done", **kw) -> Msg:
        h = {"Gud-Task": self._target(task), "Gud-Tip": self._target(tip)}
        return self.sign("no-change", who, h, (Record("text", (), reason.encode()),), **kw)

    def checkpoint(self, who: str, log: Seen, link: str, tree: str, snapshot: str, **kw) -> Msg:
        h = {"Gud-Log": log.render(), "Gud-Link": link, "Gud-Tree": tree, "Gud-Snapshot": snapshot}
        return self.sign("checkpoint", who, h, **kw)

    def blob(self, data: bytes, chunk_size: int, *, log: bool = True, only: Sequence[int] | None = None,
             at: int | None = None) -> list[Msg]:
        sha = hashlib.sha256(data).hexdigest()
        n = max(1, -(-len(data) // chunk_size))
        out = []
        for i in range(n):
            if only is not None and i not in only:
                continue
            env = E.build("blob", author=None, headers={
                "Gud-Blob": sha, "Gud-Size": str(len(data)), "Gud-Chunk": f"{i}/{n}",
                "Gud-Chunk-Size": str(chunk_size)}, records=(Record("data", (), data[i * chunk_size:(i + 1) * chunk_size]),))
            assert E.validate_kind(env) is None
            out.append(self.raw_envelope(env, log=log, at=at))
        return out

    def unsigned(self, who: str, text: str, *, reply_to: "Msg | None" = None, subject: str | None = None,
                 dmarc: bool = True, from_addr: str | None = None, log: bool = True, at: int | None = None,
                 message_id: str | None = None) -> Msg:
        """An unsigned human email, as received (with an Authentication-Results header)."""
        self._n += 1
        addr = from_addr or self.address(who)
        mid = message_id or f"<u{self._n}@{DOMAIN}>"
        if subject is None:
            subject = f"Re: {reply_to.email.subject}" if reply_to is not None else "hello"
        result = "pass" if dmarc else "fail"
        auth = f"{AUTHSERV}; dmarc={result} header.from={addr.rpartition('@')[2]}"
        raw = (f"Authentication-Results: {auth}\r\nFrom: {addr}\r\nMessage-ID: {mid}\r\nSubject: {subject}\r\n"
               + (f"In-Reply-To: {reply_to.email.message_id}\r\n" if reply_to else "") + f"\r\n{text}\r\n").encode()
        email = Email(gud_id=E.unsigned_id(raw), raw_mime=raw, message_id=mid, from_addr=addr, to=(REPO,), cc=(),
                      subject=subject, in_reply_to=reply_to.email.message_id if reply_to else None, references=(),
                      auth_results=(auth,), text=text, envelope=None)
        msg = Msg(email, None)
        self.emails.append(email)
        self.msgs.append(msg)
        if log:
            self.log_msg(msg, at)
        return msg

    # ------------------------------------------------------------ replay

    def replayer(self, now: int | None = None, **kw) -> Replayer:
        r = Replayer(self.store, **kw)
        r.feed(self.emails, self.entries(), self.now if now is None else now)
        return r

    def replay(self, now: int | None = None):
        return replay(self.emails, self.entries(), self.now if now is None else now, store=self.store)

    def run(self, now: int | None = None):
        """(chain, events) of a full replay up to `now` (default: the kit clock, with a tick logged)."""
        if now is None:
            self.tick_entry()
        r = Replayer(self.store)
        delta = r.feed(self.emails, self.entries(), self.now if now is None else now)
        return r.chain(), delta.events

    def advance(self, ms: int):
        """Move the clock, log a tick so replay time reaches it, and replay."""
        self.tick(ms)
        return self.run()


class _StoreBlobs:
    def __init__(self, store) -> None:
        self.store = store

    def blob(self, sha: str) -> bytes | None:
        return self.store.get(sha) if self.store.has(sha) else None


def events_of(events, type: str) -> list:
    return [e for e in events if e.type == type]


def status(chain, msg: "Msg | str") -> str:
    """'linked' if the push is on the chain, else its current proposal status."""
    pid = msg if isinstance(msg, str) else msg.id
    if any(link.push == pid for link in chain.links):
        return "linked"
    prop = chain.push_status(pid)
    return prop.status if prop is not None else "unknown"


def link_of(chain, msg: "Msg | str"):
    pid = msg if isinstance(msg, str) else msg.id
    for link in chain.links:
        if link.push == pid:
            return link
    return None
