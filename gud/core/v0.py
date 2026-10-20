"""Legacy reader (SPEC §23): v0 mailboxes (no host log, v0 trailers) replayed by the unchanged v0
`gud.replay.replay`, presented through the v1 chain view, read-only.

This is the only v1 module allowed to import v0 code. v0 rules (including the lead exception)
apply only here; nothing in v1 replay reads v0 trailers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from gud import replay as _v0_replay
from gud import wire as _v0_wire

from .config import Members, Owners, RepoConfig, RepoRules, TestPolicy
from .objects import BlameMap, BlameRun, ObjectError, Tree, valid_path
from .types import (
    ZERO, Chain, Email, LinkState, Member, ObjectStoreLike, ProposalState, RedState, Seen,
)

if TYPE_CHECKING:  # pragma: no cover
    import gud.types


def is_v1_message(email: Email) -> bool:
    """A v1 envelope message: a gud.env envelope carrying Gud-Version: 1."""
    return email.envelope is not None and email.envelope.get("Gud-Version") == "1"


def is_v0_mailbox(emails: Sequence["gud.types.Email"]) -> bool:
    """True when the (v0) emails carry v0 Gud-* trailers. The caller knows there is no host log (§2.3)."""
    for email in emails:
        body = getattr(email, "body", None)
        if isinstance(body, str) and _v0_wire.parse_body(body).kind is not None:
            return True
    return False


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


_V1_STATUS = {
    "window", "objected", "escalated", "needs_approval", "needs_quorum", "testing", "pending_blobs", "qualified",
    "parked", "orphan", "carried", "vetoed", "tests_failed", "declined", "expired", "superseded", "stale",
    "bad_sig", "malformed", "conflict", "bad_config",
}


@dataclass(frozen=True)
class V0Chain:
    chain: "gud.replay.Chain"                 # unchanged v0 result
    links: tuple[LinkState, ...]              # adapted: tree_root/blame_root into `store`
    tip_state: str | None
    now: float = 0.0

    def view(self) -> Chain:
        """The v1 `Chain` view (read-only): links and proposals; no tasks, claims or log."""
        proposals: dict[str, ProposalState] = {}
        for key in sorted(self.chain.proposals):
            p = self.chain.proposals[key]
            proposals[key] = ProposalState(
                id=key, push=key, named_parent=p.parent or ZERO, parent=p.parent or None, author=p.author,
                kind="push", received_at=_ms(p.received_at),
                status=p.status if p.status in _V1_STATUS else "stale", reason=p.reason,  # type: ignore[arg-type]
                task=p.task, revises=None, negotiation=key, files=(), tree_hash=None, window_open=None,
                window_closes_at=_ms(p.window_closes_at) if p.window_closes_at is not None else None, park_at=None,
                line_owners=tuple(sorted(p.affected_owners)), askers=tuple(sorted(p.affected_owners)), declared=(),
                quorum_needed=p.quorum_needed, quorum_have=p.quorum_have, members_rule=False,
                approvals=tuple((h, 0) for h in sorted(p.approvals)), objections=(), held_by=None,
                test_needed=p.test_result is None, test_result=p.test_result if p.test_result in ("pass", "fail") else None,  # type: ignore[arg-type]
                pending_blobs=(), escalation=None, qualified_at=None,
            )
        return Chain(t=_ms(self.now), log_head=Seen(-1, ZERO), links=self.links, proposals=proposals, tasks={},
                     claims={}, escalations={}, red=RedState(False, None, None, None, None, None))


def _rules(members: dict[str, Any]) -> RepoRules:
    by_handle = {h: Member(h, m.role, m.address, m.public_key, m.sponsor) for h, m in sorted(members.items())}
    return RepoRules(Members(by_handle), Owners(()), TestPolicy(), RepoConfig())


def read_v0(emails: Sequence["gud.types.Email"], window_seconds: float, now: float,
            store: ObjectStoreLike) -> V0Chain:
    """Replay a v0 mailbox with the unchanged v0 reader and adapt its links (trees and blame stored in `store`)."""
    chain = _v0_replay.replay(list(emails), window_seconds, now)
    seq_of = {link.message_id: link.seq for link in chain.links}
    links: list[LinkState] = []
    prev_tree: Tree | None = None
    prev_state: str | None = None
    for link in chain.links:
        files = {p: ("100644", text.encode("utf-8")) for p, text in sorted(link.tree.items()) if valid_path(p)}
        try:
            tree = Tree.from_files(store, files)
        except ObjectError:
            tree = Tree.empty(store)
        blame = BlameMap.empty(store)
        for path in sorted(link.blame):
            if path not in files:
                continue
            runs = [BlameRun(owner, seq_of.get(mid, link.seq), 1) for owner, mid in link.blame[path]]
            blame = blame.set(path, runs)
        changed = tuple(tree.changed_paths(prev_tree)) if prev_tree is not None else tuple(tree.paths())
        rules_root = store.put(_rules(link.members).encode())
        links.append(LinkState(
            state=link.state, seq=link.seq, push=link.state, kind="push", parent=prev_state,
            named_parent=prev_state, author=link.author, tree_root=tree.root, tree_hash=tree.hash(),
            blame_root=blame.root, rules_root=rules_root, received_at=_ms(link.received_at),
            linked_at=_ms(link.linked_at), task=link.task, intent=link.intent,
            files=tuple(sorted(changed, key=lambda p: p.encode("utf-8"))),
        ))
        prev_tree, prev_state = tree, link.state
    return V0Chain(chain, tuple(links), links[-1].state if links else None, now)
