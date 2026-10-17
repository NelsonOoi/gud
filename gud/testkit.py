"""Shared fixtures: a fake mailbox with a controllable clock, member identities, and a genesis push.

Usage in tests:

    w = World.create(tmp_path)
    g = w.genesis({"app.py": "def f():\\n    return 1\\n"})
    p = w.push("@alice", parent=g, tree={...}, intent="change f")
    w.tick(61)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import compose, sig
from .compose import Identity
from .mail_fake import FileMailbox
from .memberfile import MEMBERS, PROTECTED, format_members
from .types import Email, Member, Tree
from .wire import normalize, parse_body

DOMAIN = "gud.local"
REPO = f"repo@{DOMAIN}"

DEFAULT_MEMBERS = [
    ("@lead", "lead"), ("@alice", "agent"), ("@bob", "agent"), ("@carol", "agent"),
    ("@tester", "tester"), ("@console", "console"), ("@nelson", "human"),
]


class Clock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        self.t += 0.001
        return self.t


@dataclass
class Pushed:
    email: Email
    state: str
    tree_hash: str
    tree: Tree

    @property
    def id(self) -> str:
        return self.email.message_id


@dataclass
class World:
    mailbox: FileMailbox
    clock: Clock
    ids: dict[str, Identity]
    members: dict[str, Member]
    protected: str = ""
    pushes: dict[str, Pushed] = field(default_factory=dict)

    @classmethod
    def create(cls, tmp: Path, roles: list[tuple[str, str]] | None = None, protected: str = "") -> "World":
        clock = Clock()
        mailbox = FileMailbox(Path(tmp) / "mailbox.jsonl", REPO, DOMAIN, clock=clock)
        ids, members = {}, {}
        for handle, role in roles or DEFAULT_MEMBERS:
            address = f"{handle.lstrip('@')}@{DOMAIN}"
            if role == "human":
                ids[handle] = Identity(handle, address, None)
                members[handle] = Member(handle, role, address, None, None)
            else:
                raw, pub = sig.generate()
                ids[handle] = Identity(handle, address, raw)
                members[handle] = Member(handle, role, address, pub, "@nelson")
        return cls(mailbox, clock, ids, members, protected)

    def tick(self, seconds: float) -> None:
        self.clock.t += seconds

    def gud_files(self) -> Tree:
        files = {MEMBERS: format_members(self.members)}
        if self.protected:
            files[PROTECTED] = normalize(self.protected)
        return files

    def genesis(self, files: Tree, author: str = "@lead") -> Pushed:
        tree = {**{p: normalize(t) for p, t in files.items()}, **self.gud_files()}
        body, state, th = compose.push_body(self.ids[author], parent=None, parent_state=None,
                                            parent_tree={}, new_tree=tree, intent="genesis")
        email = compose.send(self.mailbox, self.ids[author], self.members, "[gud] genesis", body)
        p = Pushed(email, state, th, tree)
        self.pushes[p.id] = p
        return p

    def push(self, author: str, *, parent: Pushed, tree: Tree, intent: str = "change",
             task: str | None = None, consent: list[str] | None = None, subject: str | None = None) -> Pushed:
        tree = {p: normalize(t) for p, t in tree.items()}
        body, state, th = compose.push_body(self.ids[author], parent=parent.id, parent_state=parent.state,
                                            parent_tree=parent.tree, new_tree=tree, intent=intent,
                                            task=task, consent=consent)
        email = compose.send(self.mailbox, self.ids[author], self.members,
                             subject or f"[gud] {intent}", body, in_reply_to=parent.id)
        p = Pushed(email, state, th, tree)
        self.pushes[p.id] = p
        return p

    def edit(self, author: str, parent: Pushed, path: str, new_text: str | None, **kw) -> Pushed:
        tree = dict(parent.tree)
        if new_text is None:
            tree.pop(path, None)
        else:
            tree[path] = new_text
        return self.push(author, parent=parent, tree=tree, **kw)

    def approve(self, who: str, target: Pushed) -> Email:
        return self._reply(who, target, compose.approve_body(self.ids[who], target.id))

    def object(self, who: str, target: Pushed, reason: str) -> Email:
        return self._reply(who, target, compose.object_body(self.ids[who], target.id, reason))

    def test(self, target: Pushed, passed: bool = True, who: str = "@tester") -> Email:
        return self._reply(who, target, compose.test_body(self.ids[who], target.id, passed,
                                                          target.tree_hash, "ok" if passed else "FAILED"))

    def human(self, who: str, target: Pushed, text: str) -> Email:
        ident = self.ids[who]
        return self.mailbox.send(from_addr=ident.address, to=REPO, subject=f"Re: {target.email.subject}",
                                 body=text + "\n", in_reply_to=target.id)

    def _reply(self, who: str, target: Pushed, body: str) -> Email:
        return compose.send(self.mailbox, self.ids[who], self.members,
                            f"Re: {target.email.subject}", body, in_reply_to=target.id)

    def repo_mail(self) -> list[Email]:
        return self.mailbox.list(recipient=REPO)


def parsed(email: Email):
    return parse_body(email.body)
