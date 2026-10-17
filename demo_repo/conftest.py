"""Shared fixtures. Every test starts with empty auth, billing, and rate-limit stores."""

from __future__ import annotations

import pytest

import auth
import billing
import ratelimit


@pytest.fixture(autouse=True)
def _empty_stores():
    auth.reset()
    billing.reset()
    ratelimit.reset()
    yield
    auth.reset()
    billing.reset()
    ratelimit.reset()


@pytest.fixture
def alice():
    """A user on the pro plan, registered and billed, with no session yet."""
    return auth.register_user("alice", "correct horse", email="alice@example.com", plan="pro")


@pytest.fixture
def bob():
    """A second user on the free plan, so tests can show sessions do not cross."""
    return auth.register_user("bob", "battery staple", email="bob@example.com", plan="free")


@pytest.fixture
def alice_token(alice):
    """A live session token for `alice`."""
    result = auth.login("alice", "correct horse")
    assert result.ok and result.session is not None
    return result.session.token


def login_token(username: str, password: str) -> str:
    """Log `username` in and return the token. Fails the test if login does not."""
    result = auth.login(username, password)
    assert result.ok and result.session is not None
    return result.session.token
