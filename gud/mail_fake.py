"""A local mailbox stored as JSONL. Same interface as the Primitive mailbox (gud.types.Mailbox)."""

from __future__ import annotations

import fcntl
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .types import Email


class FileMailbox:
    """Every send is delivered once per recipient; `list` filters by recipient like Primitive's `recipient`."""

    def __init__(self, path: Path, repo_address: str, domain: str, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.repo_address = repo_address
        self.domain = domain
        self.clock = clock

    def send(self, *, from_addr: str, to: str, cc: list[str] | None = None, subject: str,
             body: str, in_reply_to: str | None = None, references: list[str] | None = None) -> Email:
        cc = [c for c in (cc or []) if c.lower() != to.lower()]
        email = Email(
            message_id=f"<{uuid.uuid4().hex}@{self.domain}>",
            from_addr=from_addr, to=(to,), cc=tuple(cc), subject=subject, body=body,
            received_at=self.clock(), sender_verified=True, in_reply_to=in_reply_to,
            references=tuple(references or ([in_reply_to] if in_reply_to else [])),
        )
        rows = [{"recipient": r.lower(), **_to_json(email)} for r in dict.fromkeys([to, *cc])]
        with open(self.path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            for row in rows:
                f.write(json.dumps(row) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
        return email

    def list(self, *, recipient: str | None = None, since: float | None = None) -> list[Email]:
        recipient = (recipient or self.repo_address).lower()
        out = []
        for line in self.path.read_text().splitlines():
            row = json.loads(line)
            if row["recipient"] == recipient and (since is None or row["received_at"] > since):
                out.append(_from_json(row))
        return sorted(out, key=lambda e: e.received_at)

    def wait(self, *, recipient: str, since: float, timeout: float) -> list[Email]:
        deadline = time.time() + timeout
        while True:
            new = self.list(recipient=recipient, since=since)
            if new or time.time() >= deadline:
                return new
            time.sleep(0.2)

    def get(self, message_id: str) -> Email | None:
        for e in self.list(recipient=self.repo_address):
            if e.message_id == message_id:
                return e
        return None


def _to_json(e: Email) -> dict:
    d = asdict(e)
    d["to"], d["cc"], d["references"] = list(e.to), list(e.cc), list(e.references)
    return d


def _from_json(row: dict) -> Email:
    row = {k: v for k, v in row.items() if k != "recipient"}
    return Email(**{**row, "to": tuple(row["to"]), "cc": tuple(row["cc"]),
                    "references": tuple(row.get("references") or ())})
