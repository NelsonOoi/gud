"""A member's view of a repo: read the host mirror, send through their own account.

``list`` and ``wait`` page ``GET {host}/api/mail`` with a member signature
(SPEC "Mirror access"). ``recipient`` is ignored on those reads: the mirror
*is* the repo mailbox, including received times and ``sender_verified``.
``send`` is forwarded unchanged to ``send_via`` (the member's Primitive
account, or a FileMailbox in fake mode).
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from .hostauth import sign_request
from .types import Email


class RemoteMailbox:
    def __init__(self, host_url: str, identity, send_via, repo_address: str, client=None):
        self.host_url = host_url.rstrip("/")
        self.identity = identity
        self.send_via = send_via
        self.repo_address = repo_address
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self.page_limit = 500
        self._by_id: dict[str, Email] = {}
        self._order: list[Email] = []

    def send(self, *, from_addr: str, to: str, cc: list[str] | None = None, subject: str,
             body: str, in_reply_to: str | None = None,
             references: list[str] | None = None) -> Email:
        return self.send_via.send(
            from_addr=from_addr, to=to, cc=cc, subject=subject, body=body,
            in_reply_to=in_reply_to, references=references,
        )

    def list(self, *, recipient: str | None = None, since: float | None = None) -> list[Email]:
        """Repo mail from the host mirror, oldest first.

        ``recipient`` is ignored. Pass ``since`` to keep only messages with
        ``received_at`` strictly greater than that timestamp; already-fetched
        messages stay cached and later calls request only the increment.
        """
        del recipient  # the mirror is the repo mailbox, not one person's inbox
        self._sync()
        if since is None:
            return list(self._order)
        return [email for email in self._order if email.received_at > since]

    def wait(self, *, recipient: str, since: float, timeout: float) -> list[Email]:
        """Block on ``GET /api/mail?wait=`` until mail newer than ``since`` arrives.

        ``recipient`` is ignored for the result (it is mirror mail). The host
        long-polls its cache, so this does not list Primitive.
        """
        del recipient
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            found = self.list(since=since)
            if found or time.monotonic() >= deadline:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            self._wait_page(since, min(30.0, remaining))

    def _sync(self) -> None:
        since = self._order[-1].received_at if self._order else None
        seen_cursors: list[float] = []
        while True:
            batch, nxt = self._page(since)
            added = False
            for email in batch:
                if email.message_id not in self._by_id:
                    self._by_id[email.message_id] = email
                    self._order.append(email)
                    added = True
            self._order.sort(key=lambda email: (email.received_at, email.message_id))
            if nxt is None:
                break
            if since is not None and nxt <= since and not added:
                break
            if any(cursor == nxt for cursor in seen_cursors):
                break
            seen_cursors.append(nxt)
            since = nxt

    def _mail_path(self, since: float | None, limit: int, wait: float | None = None) -> str:
        pairs: list[tuple[str, str]] = []
        if since is not None:
            pairs.append(("since", format(float(since), ".17g")))
        if wait is not None:
            text = format(float(wait), ".6f").rstrip("0").rstrip(".")
            pairs.append(("wait", text or "0"))
        pairs.append(("limit", str(int(limit))))
        return "/api/mail?" + urlencode(pairs)

    def _page(self, since: float | None) -> tuple[list[Email], float | None]:
        limit = self.page_limit if self.page_limit and self.page_limit > 0 else 500
        path = self._mail_path(since, limit)
        headers = sign_request(self.identity, "GET", path, time.time())
        response = self._client.get(self.host_url + path, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(
                f"GET {path} failed with {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        emails = [_email_from_json(row) for row in (payload.get("emails") or [])]
        nxt = payload.get("next_since")
        return emails, None if nxt is None else float(nxt)

    def _wait_page(self, since: float | None, wait: float) -> None:
        limit = self.page_limit if self.page_limit and self.page_limit > 0 else 500
        path = self._mail_path(since, limit, wait)
        headers = sign_request(self.identity, "GET", path, time.time())
        timeout = httpx.Timeout(float(wait) + 10.0, connect=10.0)
        try:
            response = self._client.get(self.host_url + path, headers=headers, timeout=timeout)
        except TypeError:
            response = self._client.get(self.host_url + path, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(
                f"GET {path} failed with {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        for email in [_email_from_json(row) for row in (payload.get("emails") or [])]:
            if email.message_id not in self._by_id:
                self._by_id[email.message_id] = email
                self._order.append(email)
        self._order.sort(key=lambda email: (email.received_at, email.message_id))


def _email_from_json(row: dict) -> Email:
    return Email(
        message_id=row["message_id"],
        from_addr=row["from_addr"],
        to=tuple(row.get("to") or ()),
        cc=tuple(row.get("cc") or ()),
        subject=row.get("subject") or "",
        body=row.get("body") or "",
        received_at=float(row["received_at"]),
        sender_verified=bool(row.get("sender_verified")),
        in_reply_to=row.get("in_reply_to"),
        references=tuple(row.get("references") or ()),
        provider_id=row.get("provider_id"),
    )
