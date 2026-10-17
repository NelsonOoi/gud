"""Shared data types and transport interfaces. See SPEC.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Tree = dict[str, str]
# Per file, one entry per line: (owner handle, message id of the link that wrote it).
Blame = dict[str, list[tuple[str, str]]]

Kind = Literal["push", "object", "approve", "test", "task", "subtask", "task-done", "notify"]
Role = Literal["agent", "lead", "tester", "console", "human"]
VOTER_ROLES: frozenset[str] = frozenset({"agent", "lead", "human"})


@dataclass(frozen=True)
class Email:
    """One email as delivered to a mailbox. `received_at` is the mailbox's clock and decides ordering."""
    message_id: str
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    received_at: float
    sender_verified: bool
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    provider_id: str | None = None


@dataclass(frozen=True)
class Parsed:
    """An email body split into free text, trailers, and diff (gud.wire.parse_body)."""
    text: str
    trailers: dict[str, str]
    diff: str

    @property
    def kind(self) -> str | None:
        return self.trailers.get("Gud-Kind")

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.trailers.get(name, default)


@dataclass(frozen=True)
class Member:
    handle: str
    role: str
    address: str
    public_key: str | None
    sponsor: str | None = None

    @property
    def votes(self) -> bool:
        return self.role in VOTER_ROLES


@dataclass
class LinkState:
    """Everything known after applying a link."""
    message_id: str
    seq: int
    author: str
    state: str
    tree_hash: str
    tree: Tree
    blame: Blame
    members: dict[str, Member]
    received_at: float
    subject: str = ""
    task: str | None = None
    linked_at: float = 0.0      # qualify time; genesis uses received_at
    intent: str = ""
    diff: str = ""


ProposalStatus = Literal[
    "window", "objected", "vetoed", "testing", "tests_failed", "needs_approval",
    "needs_quorum", "stale", "bad_sig", "conflict", "superseded", "qualified",
]


@dataclass
class ProposalState:
    """A push whose parent is on the chain but which is not (yet) a link."""
    message_id: str
    parent: str
    author: str
    subject: str
    received_at: float
    status: str
    reason: str = ""
    affected_owners: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    objections: list[dict] = field(default_factory=list)
    window_closes_at: float | None = None
    protected_rule: str | None = None
    quorum_needed: int | None = None
    quorum_have: int | None = None
    test_result: str | None = None
    task: str | None = None


@dataclass
class RunResult:
    passed: bool
    log: str
    tree_hash: str
    seconds: float
    runner: str


class Mailbox(Protocol):
    repo_address: str

    def send(self, *, from_addr: str, to: str, cc: list[str] | None = None, subject: str,
             body: str, in_reply_to: str | None = None,
             references: list[str] | None = None) -> Email: ...

    def list(self, *, recipient: str | None = None, since: float | None = None) -> list[Email]: ...

    def wait(self, *, recipient: str, since: float, timeout: float) -> list[Email]: ...


class Runner(Protocol):
    def run(self, tree: Tree) -> RunResult: ...
