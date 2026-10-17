"""Shared types for the demo service.

Other modules depend on these dataclasses and protocols. The path is
`explicit`-protected (see `.gud/protected`): a push that rewrites it stays
pending until the required approval is on the thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class User:
    """An account. `plan` names an entry in `billing.PLANS`."""

    username: str
    email: str
    plan: str


@dataclass(frozen=True)
class Session:
    """A login session. `token` is the secret the client sends back."""

    token: str
    username: str
    created_at: float


@dataclass(frozen=True)
class LoginResult:
    """Outcome of `login`. `session` is set only when `ok` is true."""

    ok: bool
    session: Session | None
    reason: str


@dataclass(frozen=True)
class Plan:
    """What a user pays, and how many requests are included before overage."""

    name: str
    monthly_cents: int
    included_requests: int
    overage_cents: int


@dataclass(frozen=True)
class Usage:
    """Requests counted for one user in one `YYYY-MM` period."""

    username: str
    period: str
    requests: int


@dataclass(frozen=True)
class Invoice:
    """One charge. `overage_cents` is zero until billing adds it in `charge`."""

    username: str
    period: str
    base_cents: int
    overage_cents: int
    total_cents: int


class AuthBackend(Protocol):
    """What `api.handlers` needs from the auth module."""

    def login(self, username: str, password: str) -> LoginResult: ...

    def check_session(self, token: str) -> Session | None: ...

    def logout(self, token: str) -> bool: ...


class BillingBackend(Protocol):
    """What handlers need in order to record a charge and read usage."""

    def charge(self, user: str, amount_cents: int) -> Invoice: ...

    def usage_of(self, username: str) -> Usage: ...


class RateLimiter(Protocol):
    """Structural type of `ratelimit.TokenBucket`. Keys are per user or route."""

    def allow(self, key: str) -> bool: ...

    def remaining(self, key: str) -> int: ...
