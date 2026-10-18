"""Building signed gud emails and sending them to the repo with the right CC list."""

from __future__ import annotations

from dataclasses import dataclass

from . import sig
from .memberfile import cc_list
from .patch import diff_trees
from .types import Email, Mailbox, Member, Tree
from .wire import ZERO, normalize, render_body, state_hash, tree_hash


@dataclass(frozen=True)
class Identity:
    handle: str
    address: str
    private_raw: bytes | None = None   # None for humans


def _signed(identity: Identity, text: str, trailers: dict[str, str], diff: str = "") -> str:
    trailers = {**trailers, "Gud-Author": identity.handle}
    if identity.private_raw is not None:
        trailers["Gud-Sig"] = sig.sign(identity.private_raw, trailers, diff)
    return render_body(text, trailers, diff)


def push_body(identity: Identity, *, parent: str | None, parent_state: str | None, parent_tree: Tree,
              new_tree: Tree, intent: str, task: str | None = None,
              consent: list[str] | None = None,
              extra_trailers: dict[str, str] | None = None) -> tuple[str, str, str]:
    """Returns (body, new state hash, new tree hash). Genesis passes parent=None."""
    diff = diff_trees(parent_tree, new_tree)
    consent = list(consent or [])
    base = parent_state or ZERO
    new_state = state_hash(base, identity.handle, consent, diff)
    new_hash = tree_hash({p: normalize(t) for p, t in new_tree.items()})
    trailers = {"Gud-Kind": "push", "Gud-Parent": parent or "none", "Gud-Base": base,
                "Gud-State": new_state, "Gud-Tree": new_hash}
    if task:
        trailers["Gud-Task"] = task
    if consent:
        trailers["Gud-Consent"] = ", ".join(consent)
    for key, value in (extra_trailers or {}).items():
        if value:
            trailers[key] = value
    return _signed(identity, intent, trailers, diff), new_state, new_hash


def object_body(identity: Identity, target: str, reason: str) -> str:
    return _signed(identity, reason, {"Gud-Kind": "object", "Gud-Target": target, "Gud-Reason": reason})


def approve_body(identity: Identity, target: str, text: str = "Approved.") -> str:
    return _signed(identity, text, {"Gud-Kind": "approve", "Gud-Target": target})


def test_body(identity: Identity, target: str, passed: bool, tree: str, log: str) -> str:
    return _signed(identity, log[-4000:], {"Gud-Kind": "test", "Gud-Target": target,
                                           "Gud-Result": "pass" if passed else "fail", "Gud-Tree": tree})


def task_body(identity: Identity, task_id: str, requester: str, description: str,
              bug: str | None = None) -> str:
    """``bug`` is the message id of the report this ticket came from."""
    trailers = {"Gud-Kind": "task", "Gud-Task": task_id, "Gud-Requester": requester}
    if bug:
        trailers["Gud-Bug"] = bug
    return _signed(identity, description, trailers)


def subtask_body(identity: Identity, task_id: str, parent_task: str, assignee: str, description: str,
                 after: list[str] | None = None) -> str:
    """``after`` lists subtask ids that must land before the assignee starts."""
    trailers = {"Gud-Kind": "subtask", "Gud-Task": task_id, "Gud-Parent-Task": parent_task,
                "Gud-Assignee": assignee}
    if after:
        trailers["Gud-After"] = ", ".join(after)
    return _signed(identity, description, trailers)


def task_done_body(identity: Identity, task_id: str, tip: str, summary: str) -> str:
    return _signed(identity, summary, {"Gud-Kind": "task-done", "Gud-Task": task_id, "Gud-Tip": tip})


def notify_body(identity: Identity, text: str, task: str | None = None, target: str | None = None,
                ask: str | None = None) -> str:
    trailers = {"Gud-Kind": "notify", "Gud-Author": identity.handle}
    if task:
        trailers["Gud-Task"] = task
    if target:
        trailers["Gud-Target"] = target
    if ask:
        trailers["Gud-Ask"] = ask
    return render_body(text, trailers)


def join_body(identity, role, address, public_key, sponsor=None, reason=""):
    """Signed ``join`` body. Implemented in :func:`gud.joins.join_body`."""
    from .joins import join_body as _join_body
    return _join_body(identity, role, address, public_key, sponsor=sponsor, reason=reason)


def send(mailbox: Mailbox, identity: Identity, members: dict[str, Member], subject: str, body: str,
         in_reply_to: str | None = None, extra_cc: list[str] | None = None,
         cc_handles: list[str] | None = None) -> Email:
    """To the repo. ``cc_handles`` names the members this mail is for; None CCs every agent member."""
    if cc_handles is None:
        cc = cc_list(members, identity.address)
    else:
        sender = identity.address.lower()
        cc = [members[h].address for h in cc_handles
              if h in members and members[h].role != "human" and members[h].address.lower() != sender]
    cc += list(extra_cc or [])
    return mailbox.send(from_addr=identity.address, to=mailbox.repo_address, cc=list(dict.fromkeys(cc)),
                        subject=subject, body=body, in_reply_to=in_reply_to)


def role_handles(members: dict[str, Member], role: str) -> list[str]:
    return sorted(h for h, m in members.items() if m.role == role)


def push_cc(members: dict[str, Member], owners: list[str]) -> list[str]:
    """Who a push is for: the tester runs it, the lead tracks it, the owners vote on it."""
    return role_handles(members, "tester") + role_handles(members, "lead") + list(owners)


def reply_cc(members: dict[str, Member], push_author: str | None) -> list[str]:
    """Who a test result or vote is for: the push's author and the lead."""
    return ([push_author] if push_author else []) + role_handles(members, "lead")
