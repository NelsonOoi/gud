"""Affected owners, protection, and quorum voters for one push against its parent link."""

from __future__ import annotations

from .blame import enclosing_owners, removed_owners
from .memberfile import members_of, rule_for
from .patch import diffstat
from .types import LinkState, Tree


def affected_owners(parent: LinkState, diff: str, author: str) -> list[str]:
    """Voters who own lines this diff removes, rewrites, or inserts inside. Excludes `author`."""
    owners = removed_owners(parent.tree, parent.blame, diff) | enclosing_owners(parent.tree, parent.blame, diff)
    owners.discard(author)
    voters = {handle for handle, member in parent.members.items() if member.votes}
    return sorted(owner for owner in owners if owner in voters)


def protection(parent: LinkState, diff: str) -> str | None:
    """Strictest `.gud/protected` rule over paths the diff touches."""
    return rule_for(parent.tree, diffstat(diff)["files"])


def quorum_voters(parent: LinkState, new_tree: Tree) -> list[str]:
    """Parent voters whose `.gud/members` line this push does not add, remove, or change."""
    before = members_of(parent.tree)
    after = members_of(new_tree)
    kept = [
        handle for handle, member in parent.members.items()
        if member.votes and before.get(handle) == after.get(handle)
    ]
    return sorted(kept)
