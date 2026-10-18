"""Membership edits. Agents and ``@console`` propose; a human member approves."""

from __future__ import annotations

from . import compose
from .compose import Identity
from .memberfile import MEMBERS, format_members, members_of
from .types import Email, LinkState, Mailbox, Member, Tree
from .wire import parse_body

_HEADER = "# handle role address public_key sponsor"


def with_member(tree: Tree, member: Member) -> Tree:
    """Return a copy of ``tree`` whose ``.gud/members`` includes ``member``.

    Raises ``ValueError`` if the handle or address is already on the roster.
    The comment header of the existing file is kept.
    """
    current = members_of(tree)
    stored = _stored(member)
    if _taken(current, stored.handle):
        raise ValueError(f"member handle {stored.handle} is already on the roster")
    for existing in current.values():
        if existing.address.lower() == stored.address:
            raise ValueError(f"address {stored.address} is already used by {existing.handle}")
    updated = dict(current)
    updated[stored.handle] = stored
    return _write(tree, updated)


def without_member(tree: Tree, handle: str) -> Tree:
    """Return a copy of ``tree`` with ``handle`` removed. Raises if the handle is unknown."""
    current = members_of(tree)
    resolved = _resolve(current, handle)
    updated = {k: v for k, v in current.items() if k != resolved}
    return _write(tree, updated)


def propose_add(mailbox: Mailbox, identity: Identity, tip: LinkState, member: Member,
                reason: str = "", join_id: str | None = None) -> Email:
    """Push an add of ``member``. Current agent members are CC'd; the new address is not.

    ``join_id`` cites the join request this push admits (``Gud-Join``), inside the signature.
    """
    roster = tip.members or members_of(tip.tree)
    new_tree = with_member(tip.tree, member)
    stored = _stored(member)
    extra = {"Gud-Join": join_id} if join_id else None
    body, _, _ = compose.push_body(
        identity, parent=tip.message_id, parent_state=tip.state, parent_tree=tip.tree,
        new_tree=new_tree, intent=_add_intent(stored, reason), extra_trailers=extra,
    )
    return compose.send(mailbox, identity, roster, f"[gud] members: add {stored.handle}", body,
                        in_reply_to=tip.message_id)


def propose_remove(mailbox: Mailbox, identity: Identity, tip: LinkState, handle: str,
                   reason: str = "") -> Email:
    roster = tip.members or members_of(tip.tree)
    resolved = _resolve(roster or members_of(tip.tree), handle)
    new_tree = without_member(tip.tree, resolved)
    body, _, _ = compose.push_body(
        identity, parent=tip.message_id, parent_state=tip.state, parent_tree=tip.tree,
        new_tree=new_tree, intent=_remove_intent(resolved, reason),
    )
    return compose.send(mailbox, identity, roster, f"[gud] members: remove {resolved}", body,
                        in_reply_to=tip.message_id)


def welcome(mailbox: Mailbox, identity: Identity, member: Member, tip: LinkState,
            host_url: str | None = None) -> Email:
    """Tell ``member`` they landed. To their address, CC the repo, with a replay checkpoint."""
    body = compose.notify_body(identity, _checkpoint(member, tip, mailbox.repo_address, host_url),
                              target=tip.message_id)
    return mailbox.send(from_addr=identity.address, to=member.address, cc=[mailbox.repo_address],
                        subject=f"[gud] welcome {member.handle}", body=body,
                        in_reply_to=tip.message_id)


def membership_history(links: list[LinkState]) -> list[dict]:
    """Diff ``members_of(tree)`` along ``links``. One record per link that changes the roster."""
    out = []
    previous: dict[str, Member] = {}
    for link in links:
        current = members_of(link.tree)
        added = sorted(h for h in current if h not in previous)
        removed = sorted(h for h in previous if h not in current)
        changed = sorted(h for h in current if h in previous and _signature(current[h]) != _signature(previous[h]))
        if added or removed or changed:
            out.append({
                "link": link.message_id, "seq": link.seq, "author": link.author,
                "added": added, "removed": removed, "changed": changed, "at": link.linked_at,
            })
        previous = current
    return out


def pending_membership(chain) -> list:
    """Proposals whose diff touches ``.gud/members``. Quorum fields stay on the proposal."""
    emails = {e.message_id: e for e in chain.emails}
    matched = []
    for prop in chain.proposals.values():
        email = emails.get(prop.message_id)
        if email is None:
            continue
        if touches_members(parse_body(email.body).diff):
            matched.append(prop)
    matched.sort(key=lambda p: p.received_at)
    return matched


def _add_intent(member: Member, reason: str) -> str:
    sponsor = member.sponsor or "none"
    text = (f"Add {member.handle} ({member.role}) <{member.address}>, "
            f"sponsored by {sponsor}.")
    if reason.strip():
        text += " " + reason.strip()
    return text


def _remove_intent(handle: str, reason: str) -> str:
    text = f"Remove {handle} from the repo."
    if reason.strip():
        text += " " + reason.strip()
    return text


def _checkpoint(member: Member, tip: LinkState, repo: str, host_url: str | None = None) -> str:
    lines = [f"Welcome {member.handle}. You are now a {member.role} on {repo}."]
    if host_url:
        lines.append(f"Host: {host_url}")
    lines += [
        "",
        "Checkpoint (verify by replaying the repo mailbox from genesis):",
        f"  tip: {tip.message_id}",
        f"  seq: {tip.seq}",
        f"  state: {tip.state}",
        f"  tree: {tip.tree_hash}",
        "",
        "Members:",
    ]
    for person in tip.members.values():
        lines.append(f"  {person.handle} {person.role} {person.address}")
    lines += [
        "",
        "Replay every email delivered to the repo address from the genesis push",
        "and confirm the tip state hash and tree hash above before you push.",
    ]
    return "\n".join(lines)


def _stored(member: Member) -> Member:
    return Member(_canon(member.handle), member.role, member.address.lower(),
                  member.public_key, member.sponsor)


def _canon(handle: str) -> str:
    handle = handle.strip()
    return handle if handle.startswith("@") else f"@{handle}"


def _taken(members: dict[str, Member], handle: str) -> bool:
    try:
        _resolve(members, handle)
    except ValueError:
        return False
    return True


def _resolve(members: dict[str, Member], handle: str) -> str:
    if handle in members:
        return handle
    canon = _canon(handle)
    if canon in members:
        return canon
    bare = handle.lstrip("@")
    for existing in members:
        if existing.lstrip("@") == bare:
            return existing
    raise ValueError(f"unknown member {handle}")


def _signature(member: Member) -> tuple:
    return (member.role, member.address.lower(), member.public_key, member.sponsor)


def _header_lines(existing: str) -> list[str]:
    header = []
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped == "":
            header.append(line)
            continue
        break
    while header and not header[-1].strip():
        header.pop()
    return header


def _write(tree: Tree, members: dict[str, Member]) -> Tree:
    existing = tree.get(MEMBERS, "")
    header = _header_lines(existing)
    member_lines = [ln for ln in format_members(members).splitlines() if not ln.startswith("#")]
    if not header or header == [_HEADER]:
        text = format_members(members)
    else:
        text = "\n".join([*header, "", *member_lines]) + "\n"
    out = dict(tree)
    out[MEMBERS] = text
    return out


def touches_members(diff: str) -> bool:
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if any(_is_members_path(part) for part in parts[2:4]):
                return True
        elif line.startswith(("--- ", "+++ ")):
            token = line[4:].split("\t", 1)[0].strip()
            if token != "/dev/null" and _is_members_path(token):
                return True
    return False


def only_members(diff: str) -> bool:
    """True when every file the diff changes is ``.gud/members``."""
    paths = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            paths += line.split()[2:4]
        elif line.startswith(("--- ", "+++ ")):
            token = line[4:].split("\t", 1)[0].strip()
            if token != "/dev/null":
                paths.append(token)
    return bool(paths) and all(_is_members_path(path) for path in paths)


def _is_members_path(token: str) -> bool:
    token = token.strip()
    for prefix in ("a/", "b/"):
        if token.startswith(prefix):
            token = token[2:]
            break
    return token == MEMBERS or token.endswith("/" + MEMBERS)
