"""The v1 Mailbox contract (SPEC §20.1, INTERFACES.md "gud/transport/base.py").

Every provider adapter implements `Mailbox`. Adapters with webhook intake also implement
`WebhookVerifier`. This module depends only on `gud.core.types`.

Cursor contract (all adapters):

- A cursor is an opaque string. `None` means "the start of the mailbox".
- `list(since=c)` returns messages delivered after `c`, oldest first, at most `limit` of them,
  and the cursor to pass next time (the cursor of the last returned message, or `c` when
  nothing was returned). Each `Received.cursor` is the position just after that message, so a
  caller may stop part-way through a batch and resume from any item.
- A cursor survives closing and reopening the mailbox (it is resumable across processes).
- Delivery is at least once: after a reconnect or a provider-side reset (for example an IMAP
  UIDVALIDITY change) a message may be returned again. Consumers dedupe by gud id.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Mapping, Protocol, runtime_checkable

from gud.core.types import Ms

PushKind = Literal["webhook", "idle", "eventsource", "pubsub", "none"]
AuthResultsKind = Literal["header", "synthesized", "none"]

# §6: an envelope never exceeds 7 MiB, and a message keeps 64 KiB for headers and the text part.
ENVELOPE_CAP = 7_340_032
MIME_OVERHEAD = 65_536


class TransportError(RuntimeError):
    """A provider or connection failure. Adapters raise this (or a subclass), never raw I/O errors."""


class QuotaExceeded(TransportError):
    """The provider refused a send because a quota or rate limit is exhausted."""


@dataclass(frozen=True)
class Capabilities:
    name: str                                        # "imap_smtp", "jmap", "primitive", ...
    max_message_bytes: int
    push: PushKind
    preserves_message_id: bool
    auth_results: AuthResultsKind
    authserv_id: str | None                          # fixed id when synthesized (or the provider's, when known)
    send_quota_per_day: int | None
    received_time_resolution_ms: int
    raw_mime: bool                                   # must be True to host a repo


@dataclass(frozen=True)
class Outgoing:
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    subject: str = ""
    text: str = ""                                   # human part
    envelope: bytes | None = None                    # gud.env attachment bytes
    message_id: str | None = None                    # adapter generates if None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class Received:
    raw_mime: bytes                                  # as received, incl. Authentication-Results
    received_at: Ms                                  # receiving server's delivery time
    provider_id: str
    cursor: str                                      # resumable position after this message


@dataclass(frozen=True)
class Sent:
    provider_id: str | None
    message_id: str                                  # as the provider will deliver it (rewritten if known)
    accepted_at: Ms


class Subscription(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class Mailbox(Protocol):
    address: str                                     # the address this mailbox reads

    def capabilities(self) -> Capabilities: ...

    def send(self, message: Outgoing | bytes) -> Sent: ...

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]: ...

    def wait(self, *, since: str | None, timeout: float) -> list[Received]: ...

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription: ...
    # Push intake. Without `since` it delivers mail that arrives after the call; with `since` it
    # first catches up from that cursor. Reconnects internally and catches up with list();
    # the callback runs on a background thread and may be called twice for one message.

    def close(self) -> None: ...


class WebhookVerifier(Protocol):                     # adapters with webhook intake implement this
    def verify(self, headers: Mapping[str, str], body: bytes, now: Ms) -> bool: ...

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[Received]: ...


def envelope_budget(max_message_bytes: int, host_max_message_bytes: int | None = None) -> int:
    """§6 envelope budget, with M the smaller of the two limits.

    §6 writes B = min(7 340 032, floor((M - 65 536) * 3 / 4)), but the gud.env part is base64
    in 76-character lines each ending in CRLF, so 57 envelope bytes take 78 bytes of MIME, not
    76. Using 3/4 lets a full-budget message exceed M whenever the formula (not the 7 MiB cap)
    binds. This uses 57/78, which is never larger than the §6 value, so a sender using it still
    obeys the §6 MUST (SPEC §6)."""
    m = max_message_bytes if host_max_message_bytes is None else min(max_message_bytes, host_max_message_bytes)
    return max(0, min(ENVELOPE_CAP, (m - MIME_OVERHEAD) * 57 // 78))


def now_ms(clock: Callable[[], float] = time.time) -> Ms:
    return int(clock() * 1000)


class ThreadSubscription:
    """A `Subscription` backed by one daemon thread running `target(stop_event)`.

    Adapters use it for their push loops (IDLE, long-poll, file watch). `close()` sets the
    event and joins the thread (bounded), so a closed subscription calls back no more."""

    def __init__(self, target: Callable[[threading.Event], None], *, name: str = "gud-subscription") -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(target,), name=name, daemon=True)
        self._thread.start()

    def _run(self, target: Callable[[threading.Event], None]) -> None:
        try:
            target(self._stop)
        except Exception:  # a dead loop must not kill the process; catch-up covers the gap
            pass

    @property
    def stopped(self) -> threading.Event:
        return self._stop

    def close(self) -> None:
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=10)


def safe_call(callback: Callable[[Received], None], item: Received) -> None:
    """Run a subscriber callback; an exception in it never stops the intake loop."""
    try:
        callback(item)
    except Exception:
        pass


# ---------------------------------------------------------------- string config helpers
def cfg_bool(cfg: Mapping[str, str], key: str, default: bool) -> bool:
    value = cfg.get(key)
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def cfg_int(cfg: Mapping[str, str], key: str, default: int | None) -> int | None:
    value = cfg.get(key)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise TransportError(f"config {key}={value!r} is not an integer") from exc


def cfg_list(cfg: Mapping[str, str], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = cfg.get(key)
    if value is None or str(value).strip() == "":
        return default
    return tuple(item.strip() for item in str(value).split(",") if item.strip())
