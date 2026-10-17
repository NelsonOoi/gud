"""A repo is a mailbox plus a clock. Every read replays the mail delivered to the repo address."""

from __future__ import annotations

import time
from collections.abc import Callable

from .replay import Chain, replay
from .types import Email, LinkState, Mailbox, Tree
from .wire import parse_body


class Repo:
    def __init__(self, mailbox: Mailbox, window: float, clock: Callable[[], float] = time.time):
        self.mailbox = mailbox
        self.window = window
        self.clock = clock

    def refresh(self) -> Chain:
        emails = self.mailbox.list(recipient=self.mailbox.repo_address)
        return replay(emails, self.window, self.clock())

    def tip(self) -> LinkState:
        tip = self.refresh().tip
        if tip is None:
            raise LookupError("repo has no genesis link")
        return tip

    def tree(self, at: str | None = None) -> Tree:
        chain = self.refresh()
        link = chain.tip if at is None else chain.by_id[at]
        if link is None:
            raise LookupError("repo has no genesis link")
        return dict(link.tree)

    def blame(self, path: str, at: str | None = None) -> list[tuple[str, str, str]]:
        chain = self.refresh()
        link = chain.tip if at is None else chain.by_id[at]
        if link is None:
            raise LookupError("repo has no genesis link")
        lines = link.tree.get(path, "").splitlines()
        blamed = link.blame.get(path, [])
        out: list[tuple[str, str, str]] = []
        for index, text in enumerate(lines):
            if index < len(blamed):
                owner, link_id = blamed[index]
            else:
                owner, link_id = "", ""
            out.append((owner, link_id, text))
        return out

    def thread(self, message_id: str) -> list[Email]:
        """The email plus every direct reply (In-Reply-To or Gud-Target), oldest first."""
        out: list[Email] = []
        for email in self.refresh().emails:
            if email.message_id == message_id or email.in_reply_to == message_id:
                out.append(email)
                continue
            if parse_body(email.body).get("Gud-Target") == message_id:
                out.append(email)
        return out

    def link(self, id: str) -> LinkState:
        return self.refresh().by_id[id]
