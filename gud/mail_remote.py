"""A member's view of a repo: read the host mirror, send through their own account.

``list`` and ``wait`` page ``GET {host}/api/mail`` with a member signature
(SPEC "Mirror access"). ``recipient`` is ignored on those reads: the mirror
*is* the repo mailbox, including received times and ``sender_verified``.
``send`` is forwarded unchanged to ``send_via`` (the member's Primitive
account, or a FileMailbox in fake mode).
"""

from __future__ import annotations

import time

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
        self._wake_since: float | None = None

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
        """Poll the mirror about once a second until mail newer than ``since`` arrives.

        ``recipient`` is ignored for the result (it is mirror mail). When
        ``send_via`` implements ``wait``, that call is the sleep: new mail in
        the member's own inbox wakes us, then the mirror is listed again.
        """
        del recipient
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            found = self.list(since=since)
            if found or time.monotonic() >= deadline:
                return found
            remaining = deadline - time.monotonic()
            step = min(1.0, remaining)
            if step <= 0:
                return []
            if not self._wake(step):
                time.sleep(step)

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

    def _page(self, since: float | None) -> tuple[list[Email], float | None]:
        limit = self.page_limit if self.page_limit and self.page_limit > 0 else 500
        pairs: list[tuple[str, str]] = []
        if since is not None:
            pairs.append(("since", format(float(since), ".17g")))
        pairs.append(("limit", str(int(limit))))
        query = "&".join(f"{key}={value}" for key, value in pairs)
        path = "/api/mail?" + query
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

    def _wake(self, step: float) -> bool:
        waiter = getattr(self.send_via, "wait", None)
        if not callable(waiter):
            return False
        self._arm_wake()
        try:
            waiter(recipient=self.identity.address, since=self._wake_since or 0.0, timeout=step)
        except TypeError:
            return False
        except Exception:
            return True
        try:
            inbox = self.send_via.list(recipient=self.identity.address)
        except Exception:
            return True
        if inbox:
            self._wake_since = inbox[-1].received_at
        return True

    def _arm_wake(self) -> None:
        if self._wake_since is not None:
            return
        try:
            inbox = self.send_via.list(recipient=self.identity.address)
        except Exception:
            self._wake_since = 0.0
            return
        self._wake_since = inbox[-1].received_at if inbox else 0.0


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
