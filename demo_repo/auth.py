"""Users, password hashes, and sessions.

Passwords are stored as PBKDF2-HMAC-SHA256. `login` checks the password and
opens a session; it does not consult the rate limiter yet (see TASKS.md).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import billing
from api.interfaces import LoginResult, Session, User

PBKDF2_ROUNDS = 12_000

_users: dict[str, tuple[User, str]] = {}
_sessions: dict[str, Session] = {}


def reset() -> None:
    """Drop every user and session. Tests call this between cases."""
    _users.clear()
    _sessions.clear()


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Return `pbkdf2_sha256$rounds$salt_hex$digest_hex`."""
    if not password:
        raise ValueError("password is required")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """True when `password` matches a value from `hash_password`."""
    try:
        scheme, rounds_s, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        expect = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return hmac.compare_digest(got, expect)


def register_user(username: str, password: str, email: str, plan: str = "free") -> User:
    """Create a user and open their billing account. Rejects duplicates."""
    if not username or any(ch.isspace() for ch in username):
        raise ValueError("username is required")
    if "@" not in email:
        raise ValueError("email must contain @")
    if username in _users:
        raise ValueError(f"user {username} already exists")
    user = User(username=username, email=email, plan=plan)
    _users[username] = (user, hash_password(password))
    billing.open_account(username, plan)
    return user


def get_user(username: str) -> User | None:
    row = _users.get(username)
    return None if row is None else row[0]


def start_session(username: str, now: float | None = None) -> Session:
    """Issue a new token. The caller has already checked the password."""
    if username not in _users:
        raise KeyError(username)
    session = Session(
        token=secrets.token_urlsafe(24),
        username=username,
        created_at=time.time() if now is None else now,
    )
    _sessions[session.token] = session
    return session


def login(username: str, password: str) -> LoginResult:
    """Check the password and start a session. Unknown user and bad password share one reason."""
    row = _users.get(username)
    if row is None or not verify_password(password, row[1]):
        return LoginResult(False, None, "invalid credentials")
    return LoginResult(True, start_session(username), "")


def check_session(token: str | None) -> Session | None:
    if not token:
        return None
    return _sessions.get(token)


def list_sessions(username: str) -> list[Session]:
    return [session for session in _sessions.values() if session.username == username]


def logout(token: str) -> bool:
    """Drop one session. False when the token was already gone."""
    return _sessions.pop(token, None) is not None


def revoke_user_sessions(username: str) -> int:
    """Drop every session for `username`. Returns how many were removed."""
    doomed = [token for token, session in _sessions.items() if session.username == username]
    for token in doomed:
        _sessions.pop(token, None)
    return len(doomed)
