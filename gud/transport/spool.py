"""A webhook-fed local store of received mail, for providers whose inbound path is a push.

SES (SNS -> HTTPS) and Postmark (inbound webhook) deliver mail by calling us; neither has an
API that lists a mailbox the way IMAP, JMAP or the Gmail API do. Their adapters follow the v0
`HostedMailbox` pattern: the webhook handler (and a periodic catch-up from the provider's own
store, S3 or Postmark's message search) writes into a `Spool`, and `list`/`wait`/`subscribe`
read the spool. The spool is the adapter's mailbox; it is safe across processes, so the webhook
server and the intake may run in different processes.

Layout (one directory per mailbox):

    <root>/lock            flock target
    <root>/index.jsonl     one line per message: {"n", "t", "id", "f", "src"}
    <root>/<n>.eml         raw MIME exactly as the adapter stored it (incl. synthesized A-R)

- `n` is a sequence number starting at 1; the cursor is `str(n)` (None = the start).
- `id` is the provider id; `ingest` stores one id once (a retried webhook, or the catch-up
  finding a message the webhook already delivered, writes nothing). The first copy wins, so the
  stored bytes, and with them the unsigned gud id (§3.3), never change.
- `t` is the delivery time in unix ms as the adapter determined it (see each adapter).
- List order is intake order. A provider that pushes out of order can give a later row an
  earlier `t`; the host clamps log times (§11.1), so this only affects the measured ordering.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from gud.core.types import Ms

from .base import Received, TransportError

_conditions: dict[str, threading.Condition] = {}
_conditions_lock = threading.Lock()


def _condition(root: Path) -> threading.Condition:
    with _conditions_lock:
        cond = _conditions.get(str(root))
        if cond is None:
            cond = _conditions[str(root)] = threading.Condition()
        return cond


def parse_cursor(cursor: str | None) -> int:
    if cursor is None or cursor == "":
        return 0
    if not cursor.isdigit():
        raise TransportError(f"invalid spool cursor {cursor!r}")
    return int(cursor)


class Spool:
    def __init__(self, root: Path | str, *, poll_interval: float = 0.05) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "lock").touch(exist_ok=True)
        self.poll_interval = poll_interval
        self._cond = _condition(self.root)

    @contextmanager
    def _locked(self, *, shared: bool) -> Iterator[None]:
        with open(self.root / "lock", "rb") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _rows(self) -> list[dict]:
        path = self.root / "index.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn last line from a crash; the writer appends whole lines
        return rows

    def has(self, provider_id: str) -> bool:
        with self._locked(shared=True):
            return any(row["id"] == provider_id for row in self._rows())

    def ids(self) -> set[str]:
        with self._locked(shared=True):
            return {row["id"] for row in self._rows()}

    def ingest(self, raw: bytes, received_at: Ms, provider_id: str, *, source: str = "") -> Received | None:
        """Store one message. Returns its `Received`, or None when the id is already stored."""
        if not provider_id:
            raise TransportError("a spooled message needs a provider id")
        with self._locked(shared=False):
            rows = self._rows()
            if any(row["id"] == provider_id for row in rows):
                return None
            n = rows[-1]["n"] + 1 if rows else 1
            name = f"{n}.eml"
            tmp = self.root / f".{name}.tmp"
            tmp.write_bytes(raw)
            os.replace(tmp, self.root / name)
            with open(self.root / "index.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"n": n, "t": int(received_at), "id": provider_id, "f": name,
                                     "src": source}) + "\n")
                fh.flush()
        with self._cond:
            self._cond.notify_all()
        return Received(raw_mime=raw, received_at=int(received_at), provider_id=provider_id, cursor=str(n))

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        after = parse_cursor(since)
        with self._locked(shared=True):
            rows = [row for row in self._rows() if row["n"] > after][:max(1, int(limit))]
        out = [Received(raw_mime=(self.root / row["f"]).read_bytes(), received_at=row["t"],
                        provider_id=row["id"], cursor=str(row["n"])) for row in rows]
        return out, (out[-1].cursor if out else since)

    def head(self) -> str | None:
        with self._locked(shared=True):
            rows = self._rows()
        return str(rows[-1]["n"]) if rows else None

    def wait(self, *, since: str | None, timeout: float,
             on_idle: Callable[[], None] | None = None, idle_every: float = 0.0) -> list[Received]:
        """Block until the spool has rows after `since` or `timeout` passes. Another process may
        write the spool, so this re-reads the index every `poll_interval`. `on_idle` (the
        adapter's catch-up) runs every `idle_every` seconds while nothing arrives."""
        deadline = time.monotonic() + max(0.0, timeout)
        next_idle = time.monotonic() + idle_every if on_idle and idle_every > 0 else None
        while True:
            items, _ = self.list(since=since)
            remaining = deadline - time.monotonic()
            if items or remaining <= 0:
                return items
            if next_idle is not None and time.monotonic() >= next_idle:
                try:
                    on_idle()
                except TransportError:
                    pass
                next_idle = time.monotonic() + idle_every
                continue
            with self._cond:
                self._cond.wait(min(self.poll_interval, remaining))
