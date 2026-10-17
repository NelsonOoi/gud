"""Join requests, welcome mail, and the member's mirror audit.

A ``join`` email does not change the chain. It asks a member to send a
membership push that cites ``Gud-Join``. Status of that request:

- ``member``: the tip roster has this handle with this public key.
- ``proposed``: a push cites the join and is still open (in ``proposals`` with
  a non-terminal status such as ``needs_quorum``, ``needs_approval``,
  ``window``, ``testing``, or ``qualified``).
- ``rejected``: at least one citing push is terminal (``superseded``,
  ``objected``, ``vetoed``, ``bad_sig``, ``stale``, ``conflict``,
  ``tests_failed``) and none is still open. ``objected`` counts because replay
  treats it as never-qualifying.
- ``pending``: nothing cites it, or the citing push already linked and the
  handle is no longer a tip member with this key (removed later).

``push_id`` is the citing push that explains the status (the open one, else
the linked one, else the latest terminal one).
"""

from __future__ import annotations

from . import sig
from .members import welcome
from .types import Email
from .wire import parse_body, render_body

_CLOSED = frozenset({
    "superseded", "bad_sig", "stale", "conflict", "tests_failed", "vetoed", "objected",
})


def join_body(identity, role: str, address: str, public_key: str, sponsor: str | None = None,
              reason: str = "") -> str:
    """Signed ``join`` body. ``Gud-Sig`` verifies with ``public_key``, which must be this identity's key."""
    if not getattr(identity, "private_raw", None):
        raise ValueError("join requires a private key")
    if not role or not address or not public_key:
        raise ValueError("role, address, and public_key are required")
    if sig.public_of(identity.private_raw) != public_key:
        raise ValueError("Gud-Key must equal this identity's public key")
    handle = _canon_handle(identity.handle)
    trailers = {
        "Gud-Kind": "join",
        "Gud-Author": handle,
        "Gud-Role": role,
        "Gud-Address": address,
        "Gud-Key": public_key,
    }
    if sponsor and sponsor != "-":
        trailers["Gud-Sponsor"] = sponsor
    if reason:
        trailers["Gud-Reason"] = reason
    text = reason.strip() if reason and reason.strip() else f"{handle} asks to join as {role}."
    trailers["Gud-Sig"] = sig.sign(identity.private_raw, trailers, "")
    return render_body(text, trailers)


def join_requests(chain) -> list[dict]:
    """``JoinRequest`` dicts, newest first. See the module docstring for ``status``."""
    emails = list(getattr(chain, "emails", []) or [])
    parsed = [(email, parse_body(email.body)) for email in emails]
    pushes: dict[str, list[Email]] = {}
    for email, body in parsed:
        join_id = body.get("Gud-Join") if body.kind == "push" else None
        if join_id:
            pushes.setdefault(join_id, []).append(email)
    tip = _tip(chain)
    members = tip.members if tip is not None else {}
    links_by_id = _link_index(chain)
    proposals = getattr(chain, "proposals", {}) or {}
    rows = []
    for email, body in parsed:
        if body.kind != "join":
            continue
        rows.append(_row(email, body, pushes.get(email.message_id, []), members, links_by_id, proposals))
    rows.sort(key=lambda row: (row["received_at"], row["id"]), reverse=True)
    return rows


def welcome_new_members(chain, mailbox, identity, host_url, already_sent=None) -> list[Email]:
    """Welcome each member whose admitting push has linked and cites a join.

    A link counts as already welcomed when ``already_sent`` contains its id or
    the chain has a ``notify`` whose ``Gud-Target`` is that link id (the welcome
    itself). ``already_sent``, when it is a set, is updated in place. Calling
    this on every host refresh is safe.
    """
    sent_ids = set(already_sent or ())
    emails = list(getattr(chain, "emails", []) or [])
    by_id = {email.message_id: email for email in emails}
    for email in emails:
        body = parse_body(email.body)
        target = body.get("Gud-Target") if body.kind == "notify" else None
        if target:
            sent_ids.add(target)
    out: list[Email] = []
    for link in list(getattr(chain, "links", []) or []):
        if link.message_id in sent_ids:
            continue
        email = by_id.get(link.message_id)
        if email is None:
            continue
        body = parse_body(email.body)
        join_id = body.get("Gud-Join") if body.kind == "push" else None
        if not join_id:
            continue
        join = by_id.get(join_id)
        if join is None:
            continue
        parsed_join = parse_body(join.body)
        if parsed_join.kind != "join":
            continue
        handle = _canon_handle(parsed_join.get("Gud-Author"))
        member = link.members.get(handle) if handle else None
        if member is None or not member.address:
            continue
        key = parsed_join.get("Gud-Key")
        if key and member.public_key and key != member.public_key:
            continue
        note = welcome(mailbox, identity, member, link, host_url=host_url)
        sent_ids.add(link.message_id)
        out.append(note)
    if isinstance(already_sent, set):
        already_sent.update(sent_ids)
    return out


def audit(mirror: list[Email], own: list[Email], repo_address: str, skew: float) -> dict:
    """Compare the host mirror with one member's inbox.

    Only own-inbox messages that carry a ``Gud-Kind`` and list ``repo_address``
    in To or Cc are checked. Match is by ``message_id``. A message is missing
    when the mirror has no copy, altered when the body bytes differ, and retimed
    when ``abs(mirror.received_at - own.received_at) > skew``. A message can be
    both altered and retimed. Returns ``{missing, altered, retimed}``, each a
    list of ``{message_id, subject, received_at}`` (retimed also has
    ``mirror_received_at``), in inbox order.
    """
    repo = (repo_address or "").strip().lower()
    by_id: dict[str, Email] = {}
    for email in mirror:
        by_id.setdefault(email.message_id, email)
    missing: list[dict] = []
    altered: list[dict] = []
    retimed: list[dict] = []
    seen: set[str] = set()
    for email in own:
        if email.message_id in seen:
            continue
        seen.add(email.message_id)
        if not _counts(email, repo):
            continue
        other = by_id.get(email.message_id)
        if other is None:
            missing.append(_hit(email))
            continue
        if other.body != email.body:
            altered.append(_hit(email))
        if abs(float(other.received_at) - float(email.received_at)) > float(skew):
            retimed.append(_hit(email, mirror_received_at=other.received_at))
    return {"missing": missing, "altered": altered, "retimed": retimed}


def _row(email: Email, body, citing: list[Email], members, links_by_id, proposals) -> dict:
    trailers = body.trailers
    address = (trailers.get("Gud-Address") or "").strip()
    key = trailers.get("Gud-Key") or ""
    sponsor = trailers.get("Gud-Sponsor")
    if sponsor in (None, "", "-"):
        sponsor = None
    reason = trailers.get("Gud-Reason")
    if reason is None:
        reason = body.text or ""
    valid = bool(
        address
        and email.sender_verified
        and email.from_addr.strip().lower() == address.lower()
        and sig.verify(key, trailers, body.diff)
    )
    status, push_id = _status(
        _canon_handle(trailers.get("Gud-Author")), key, citing, members, links_by_id, proposals,
    )
    return {
        "id": email.message_id,
        "handle": _canon_handle(trailers.get("Gud-Author")),
        "role": trailers.get("Gud-Role") or "",
        "address": address.lower(),
        "public_key": key,
        "sponsor": sponsor,
        "reason": reason,
        "received_at": email.received_at,
        "valid": valid,
        "status": status,
        "push_id": push_id,
    }


def _status(handle: str, key: str, citing: list[Email], members, links_by_id, proposals):
    pending: list[Email] = []
    rejected: list[Email] = []
    linked: list[Email] = []
    for email in citing:
        if email.message_id in links_by_id:
            linked.append(email)
            continue
        prop = proposals.get(email.message_id)
        state = prop.status if prop is not None else "stale"
        if state in _CLOSED:
            rejected.append(email)
        else:
            pending.append(email)
    if _is_member(members, handle, key):
        chosen = _latest(linked) or _latest(pending) or _latest(citing)
        return "member", chosen.message_id if chosen else None
    if pending:
        return "proposed", _latest(pending).message_id
    if rejected:
        return "rejected", _latest(rejected).message_id
    chosen = _latest(linked)
    return "pending", chosen.message_id if chosen else None


def _is_member(members, handle: str, key: str) -> bool:
    member = members.get(handle) if handle else None
    if member is None or not key:
        return False
    return member.public_key == key


def _latest(emails: list[Email]) -> Email | None:
    if not emails:
        return None
    return max(emails, key=lambda email: (email.received_at, email.message_id))


def _tip(chain):
    tip = getattr(chain, "tip", None)
    if tip is not None:
        return tip
    links = getattr(chain, "links", None) or []
    return links[-1] if links else None


def _link_index(chain) -> dict:
    by_id = getattr(chain, "by_id", None)
    if by_id:
        return by_id
    return {link.message_id: link for link in (getattr(chain, "links", None) or [])}


def _counts(email: Email, repo: str) -> bool:
    if not repo:
        return False
    addressed = {*(addr.lower() for addr in email.to), *(addr.lower() for addr in email.cc)}
    if repo not in addressed:
        return False
    return parse_body(email.body).kind is not None


def _hit(email: Email, mirror_received_at: float | None = None) -> dict:
    row = {"message_id": email.message_id, "subject": email.subject, "received_at": email.received_at}
    if mirror_received_at is not None:
        row["mirror_received_at"] = mirror_received_at
    return row


def _canon_handle(handle: str | None) -> str:
    handle = (handle or "").strip()
    if not handle:
        return ""
    return handle if handle.startswith("@") else f"@{handle}"
