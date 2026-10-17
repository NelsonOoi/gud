"""Task-done notices. ``EmailNotifier`` mails them; ``Notifier`` is the seam for a later iMessage backend.

Inkbox is intentionally deferred.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from . import compose
from .compose import Identity
from .types import Email, LinkState, Mailbox, Member
from .wire import short


@runtime_checkable
class Notifier(Protocol):
    def notify_requester(self, human: Member, task_id: str, title: str, tip: LinkState,
                         summary: str, links: list[str]) -> Email: ...


class EmailNotifier:
    def __init__(self, mailbox: Mailbox, identity: Identity):
        self.mailbox = mailbox
        self.identity = identity

    def notify_requester(self, human: Member, task_id: str, title: str, tip: LinkState,
                         summary: str, links: list[str]) -> Email:
        return notify_requester(self.mailbox, self.identity, human, task_id, title, tip, summary, links)


def notify_requester(mailbox: Mailbox, identity: Identity, human: Member, task_id: str, title: str,
                     tip: LinkState, summary: str, links: list[str]) -> Email:
    """Mail the human requester. To their address, CC the repo."""
    body = compose.notify_body(identity, _text(summary, tip, links), task=task_id, target=tip.message_id)
    return mailbox.send(
        from_addr=identity.address, to=human.address, cc=[mailbox.repo_address],
        subject=f"[gud] {task_id} done: {title}", body=body, in_reply_to=tip.message_id,
    )


def _text(summary: str, tip: LinkState, links: list[str]) -> str:
    lines = [summary.strip(), "", f"Tip {short(tip.state)} {tip.message_id} (seq {tip.seq})."]
    if links:
        lines += ["", "Links:", *[f"- {link}" for link in links]]
    return "\n".join(lines)
