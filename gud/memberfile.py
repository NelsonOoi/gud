"""Reading and writing `.gud/members` and `.gud/protected`."""

from __future__ import annotations

import fnmatch

from .types import Member, Tree

MEMBERS = ".gud/members"
PROTECTED = ".gud/protected"
ALWAYS_QUORUM = (PROTECTED,)
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
    """The member at ``address``.

    A human registered at an outside address (a gmail, for example) is also
    recognized at ``handle@<agent domain>``. That is the address the host can
    actually send from. An on-domain registration is only the exact address.
    """
    address = address.lower()
    found = next((m for m in members.values() if m.address == address), None)
    if found is not None:
        return found
    local, sep, host = address.partition("@")
    domain = _agent_domain(members)
    if not sep or not local or host != domain:
        return None
    member = members.get(f"@{local}")
    if member is None or member.role != "human":
        return None
    registered = member.address.lower()
    registered_host = registered.rsplit("@", 1)[-1] if "@" in registered else ""
    if registered_host == domain:
        return None
    return member


def sending_address(member: Member, domain: str) -> str:
    """The From address the host mailbox can use for ``member``.

    Primitive rejects any domain other than the account's verified sending
    domain. A human whose roster address is outside that domain is sent as
    ``handle@domain``, which ``by_address`` still attributes to them.
    """
    domain = (domain or "").strip().lower()
    registered = (member.address or "").strip().lower()
    host = registered.rsplit("@", 1)[-1] if "@" in registered else ""
    if not domain or host == domain:
        return registered
    return f"{member.handle.lstrip('@')}@{domain}"


def _agent_domain(members: dict[str, Member]) -> str | None:
    """The domain agent addresses share, or None when they do not agree."""
    domains: set[str] = set()
    for member in members.values():
        if member.role == "human" or "@" not in (member.address or ""):
            continue
        domains.add(member.address.lower().rsplit("@", 1)[-1])
    if len(domains) == 1:
        return next(iter(domains))
    return None


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
        # The member list is a human approval, even when a glob such as `.gud/*` would match it.
        if p == MEMBERS:
            continue
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
