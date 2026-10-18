"""An agent's own inbox for one repo, and the gate that decides when to replay the repo.

Agents are driven by their inbox: an assignment, a push that touches their lines, or a
reply to their own push arrives as mail addressed to them. The repo mailbox is read only
when an agent has to agree with everyone else: which tip to build on, and whether a push
linked. See SPEC "Agents".
"""

from __future__ import annotations

import time

from .types import Email, Mailbox
from .wire import parse_body


def _own_source(mailbox: Mailbox):
    """The member's own account. A RemoteMailbox lists the host mirror; its inbox is ``send_via``."""
    return getattr(mailbox, "send_via", None) or mailbox


class Inbox:
    def __init__(self, mailbox: Mailbox, address: str, repo_address: str):
        self.mailbox = mailbox
        self.address = address.lower()
        self.repo_address = repo_address.lower()
        self._since: float | None = None
        self._high = 0.0
        self._seen: set[str] = set()

    def read(self) -> list[Email]:
        """Mail to this agent about this repo that it has not read yet, oldest first."""
        rows = _own_source(self.mailbox).list(recipient=self.address, since=self._since)
        fresh = []
        for email in rows:
            if email.message_id in self._seen or not self._for_repo(email):
                continue
            self._seen.add(email.message_id)
            fresh.append(email)
        if rows:
            self._high = max(self._high, max(email.received_at for email in rows))
            # Overlap by a second: two mails can share a received time, and `since` is exclusive.
            self._since = self._high - 1.0
        return fresh

    def wait(self, timeout: float) -> None:
        """Sleep until mail newer than anything read reaches this agent, or `timeout` passes."""
        _own_source(self.mailbox).wait(recipient=self.address, since=self._high, timeout=timeout)

    def _for_repo(self, email: Email) -> bool:
        addrs = {a.lower() for a in (*email.to, *email.cc)}
        return self.repo_address in addrs


def kinds(emails: list[Email]) -> list[str]:
    return [parse_body(email.body).kind or "" for email in emails]


class Gate:
    """Replay the repo when the inbox brought something relevant, or on a timer.

    ``busy_every`` applies while the agent has open work (a pending push, an assignment
    waiting on another subtask, an untested push); ``idle_every`` otherwise. Zero means
    every step replays, which is what tests that drive ``step`` by hand expect.
    """

    def __init__(self, busy_every: float = 0.0, idle_every: float = 0.0):
        self.busy_every = busy_every
        self.idle_every = idle_every
        self._last: float | None = None

    def due(self, relevant: bool, busy: bool) -> bool:
        now = time.monotonic()
        every = self.busy_every if busy else self.idle_every
        if relevant or self._last is None or now - self._last >= every:
            self._last = now
            return True
        return False
