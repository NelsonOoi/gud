"""What needs a member now, derived from the chain (`gud review`, the MCP `inbox`, the agent).

One implementation for every member tool. Each item is a JSON-compatible dict with a `type`:

- `review`: a live push (its current candidate) where the member has a say and has neither
  approved nor objected. `why` lists codes in the §19.2 `review.requested` vocabulary: `line`
  (the member owns live lines it touches), `lazy` (declared lazy owner), `explicit:<path>`,
  `quorum`, `members` (a human approve is needed for `.gud/members`).
- `objection`: a blocking, pending or confirmed objection on the member's own live push.
- `escalation`: an open escalation the member decides.
- `accept`: a root task the member requested whose work is done and that has no criteria
  (requester acceptance, §16.6).
- `lease`: the member's active claim lapses within `lease_warn_ms`.
- `overlap`: one of the member's active claims overlaps another member's (§16.4).
"""

from __future__ import annotations

from typing import Any

from gud.core.config import Members, globs_overlap
from gud.core.types import LIVE_STATUSES, Chain, ClaimState, Handle, Hex, Ms

LEASE_WARN_MS = 5 * 60_000


def why_codes(prop: Any, me: Handle, member: Any) -> list[str]:
    why: list[str] = []
    if me in prop.askers:
        why.append("line" if me in prop.line_owners else "lazy")
    why += [f"explicit:{path}" for path, rule, owners in prop.declared if rule == "explicit" and me in owners]
    if prop.quorum_needed and member is not None and member.votes:
        why.append("quorum")
    if prop.members_rule and member is not None and member.role == "human":
        why.append("members")
    return why


def my_claims(chain: Chain, me: Handle) -> list[ClaimState]:
    return [c for c in chain.claims.values() if c.holder == me and c.status == "active"]


def needs(chain: Chain, me: Handle, *, members: Members | None, now: Ms, tip_paths: list[str] | None = None,
          lease_warn_ms: int = LEASE_WARN_MS) -> list[dict]:
    """Everything that needs `me` at `chain` (see the module docstring), in a stable order."""
    member = members.get(me) if members is not None else None
    items: list[dict] = []
    seen_push: set[Hex] = set()
    for cid in sorted(chain.proposals):
        prop = chain.proposals[cid]
        if prop.status not in LIVE_STATUSES or prop.push in seen_push:
            continue
        cur = chain.push_status(prop.push) or prop
        if cur.id != prop.id:
            continue                              # only the push's current candidate
        seen_push.add(prop.push)
        if prop.author == me:
            for obj in prop.objections:
                if obj.status in ("blocking", "pending_verdict", "confirmed"):
                    items.append({"type": "objection", "push": prop.push, "candidate": prop.id, "objection": obj.id,
                                  "by": obj.by, "reason": obj.reason, "status": obj.status,
                                  "evidence": obj.evidence.path if obj.evidence else None})
            continue
        if me in {h for h, _ in prop.approvals} or any(o.by == me for o in prop.objections):
            continue
        why = why_codes(prop, me, member)
        if why:
            items.append({"type": "review", "push": prop.push, "candidate": prop.id, "author": prop.author,
                          "status": prop.status, "why": why, "files": list(prop.files),
                          "closes_at": prop.window_closes_at})
    for eid in sorted(chain.escalations):
        esc = chain.escalations[eid]
        if esc.status == "open" and me in esc.deciders:
            items.append({"type": "escalation", "escalation": esc.id, "reason": esc.reason,
                          "pushes": list(esc.pushes), "deadline": esc.deadline})
    for tid in sorted(chain.tasks):
        task = chain.tasks[tid]
        if task.status == "work_done" and task.requester == me and task.parent is None \
                and not (task.accept_paths or task.accept_cmds):
            items.append({"type": "accept", "task": task.id, "alias": task.alias, "title": task.title})
    mine = my_claims(chain, me)
    for c in mine:
        if c.lapses_at - now < lease_warn_ms:
            items.append({"type": "lease", "claim": c.id, "task": c.task, "lapses_at": c.lapses_at})
    paths = tip_paths or []
    for c in sorted(chain.claims.values(), key=lambda c: c.id):
        if c.status != "active" or c.holder == me:
            continue
        for m in mine:
            if globs_overlap(list(m.files), list(c.files), paths):
                items.append({"type": "overlap", "claim": m.id, "other": c.id, "holder": c.holder,
                              "files": list(c.files)})
    return items


def needs_of(client: Any) -> list[dict]:
    """`needs` for a `RepoClient`'s member at its cached chain."""
    chain = client.chain()
    reader = client.reader()
    members = reader.members() if chain.tip is not None else None
    paths = list(reader.tree().paths()) if chain.tip is not None else []
    return needs(chain, client.handle, members=members, now=client.now_ms(), tip_paths=paths)


WHY_TEXT = {"line": "you own lines it changes", "lazy": "declared owner", "quorum": "quorum vote",
            "members": "a human approve for .gud/members"}


def why_text(code: str) -> str:
    if code.startswith("explicit:"):
        return f"explicit owner of {code.split(':', 1)[1]}"
    return WHY_TEXT.get(code, code)
