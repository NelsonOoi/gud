"""A local cache of mail Primitive delivers by webhook. Sends still go to Primitive.

Reading never calls the network. The delivered copy of a send arrives later
through ``ingest``, the same way a recipient's copy arrives today.
"""

from __future__ import annotations

from pathlib import Path

from .mail_fake import FileMailbox
from .types import Email


class HostedMailbox:
    def __init__(self, cache_path, repo_address: str, domain: str, send_via):
        self.cache = FileMailbox(Path(cache_path), repo_address, domain)
        self.path = self.cache.path
        self.repo_address = repo_address
        self.domain = domain
        self.send_via = send_via

    def send(self, *, from_addr: str, to: str, cc: list[str] | None = None, subject: str,
             body: str, in_reply_to: str | None = None,
             references: list[str] | None = None) -> Email:
        return self.send_via.send(
            from_addr=from_addr, to=to, cc=cc, subject=subject, body=body,
            in_reply_to=in_reply_to, references=references,
        )

    def list(self, *, recipient: str | None = None, since: float | None = None) -> list[Email]:
        return self.cache.list(recipient=recipient, since=since)

    def wait(self, *, recipient: str, since: float, timeout: float) -> list[Email]:
        """Watch the cache file. This does not call Primitive."""
        return self.cache.wait(recipient=recipient, since=since, timeout=timeout)

    def get(self, message_id: str) -> Email | None:
        return self.cache.get(message_id)

    def ingest(self, email: Email, recipients: list[str]) -> int:
        """Append one row per recipient. A retried delivery or the catch-up list writes nothing new.

        ``received_at`` stays Primitive's timestamp, so replay agrees on every machine.
        """
        return self.cache.append(email, recipients)
