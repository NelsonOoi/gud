"""Reading and writing `.gud/members` and `.gud/protected`."""

from __future__ import annotations

import fnmatch

from .types import Member, Tree

MEMBERS = ".gud/members"
PROTECTED = ".gud/protected"
ALWAYS_QUORUM = (MEMBERS, PROTECTED)
RULE_RANK = {None: 0, "explicit": 1, "quorum": 2}


def parse_members(text: str) -> dict[str, Member]:
    out: dict[str, Member] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        parts += ["-"] * (5 - len(parts))
        handle, role, address, key, sponsor = parts[:5]
        out[handle] = Member(handle=handle, role=role, address=address.lower(),
                             public_key=None if key == "-" else key,
                             sponsor=None if sponsor == "-" else sponsor)
    return out


def format_members(members: dict[str, Member]) -> str:
    lines = ["# handle role address public_key sponsor"]
    for m in members.values():
        lines.append(f"{m.handle} {m.role} {m.address} {m.public_key or '-'} {m.sponsor or '-'}")
    return "\n".join(lines) + "\n"


def members_of(tree: Tree) -> dict[str, Member]:
    return parse_members(tree.get(MEMBERS, ""))


def by_address(members: dict[str, Member], address: str) -> Member | None:
    address = address.lower()
    return next((m for m in members.values() if m.address == address), None)


def parse_protected(text: str) -> list[tuple[str, str]]:
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            glob, rule = line.split()[:2]
            rules.append((glob, rule))
    return rules


def rule_for(tree: Tree, paths: list[str]) -> str | None:
    """Strictest protection rule over the touched paths."""
    rules = parse_protected(tree.get(PROTECTED, ""))
    best = None
    for p in paths:
        r = "quorum" if p in ALWAYS_QUORUM else None
        for glob, rule in rules:
            if fnmatch.fnmatch(p, glob) and RULE_RANK[rule] > RULE_RANK[r]:
                r = rule
        if RULE_RANK[r] > RULE_RANK[best]:
            best = r
    return best


def cc_list(members: dict[str, Member], sender_address: str) -> list[str]:
    return [m.address for m in members.values()
            if m.role != "human" and m.address != sender_address.lower()]
