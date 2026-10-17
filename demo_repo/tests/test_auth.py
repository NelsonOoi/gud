"""Auth store: hashing, registration, login, and sessions."""

from __future__ import annotations

import auth
import billing


def test_hash_password_roundtrip():
    salt = b"0123456789abcdef"
    stored = auth.hash_password("s3cret!", salt=salt)
    assert stored.startswith(f"pbkdf2_sha256${auth.PBKDF2_ROUNDS}$")
    assert auth.verify_password("s3cret!", stored)
    assert not auth.verify_password("other", stored)
    assert not auth.verify_password("s3cret!", "not-a-hash")


def test_register_and_login(alice):
    assert alice.username == "alice"
    assert alice.plan == "pro"
    assert billing.plan_for("alice").name == "pro"
    result = auth.login("alice", "correct horse")
    assert result.ok and result.session is not None
    assert result.reason == ""
    found = auth.check_session(result.session.token)
    assert found is not None and found.username == "alice"
    assert auth.get_user("alice") == alice


def test_login_rejects_unknown_and_wrong_password_alike(alice):
    bad = auth.login("alice", "nope")
    missing = auth.login("nobody", "correct horse")
    assert not bad.ok and bad.session is None
    assert bad.reason == "invalid credentials"
    assert missing.reason == "invalid credentials"
    assert auth.check_session("") is None
    assert auth.check_session(None) is None


def test_logout_and_revoke(alice):
    first = auth.login("alice", "correct horse")
    second = auth.login("alice", "correct horse")
    assert first.session and second.session
    assert len(auth.list_sessions("alice")) == 2
    assert auth.logout(first.session.token)
    assert auth.check_session(first.session.token) is None
    assert not auth.logout(first.session.token)
    assert auth.revoke_user_sessions("alice") == 1
    assert auth.list_sessions("alice") == []
    assert auth.check_session(second.session.token) is None


def test_duplicate_register_and_bad_email():
    auth.register_user("carol", "pw", email="carol@example.com")
    try:
        auth.register_user("carol", "pw", email="carol@example.com")
        raise AssertionError("duplicate register should fail")
    except ValueError as exc:
        assert "already exists" in str(exc)
    try:
        auth.register_user("dave", "pw", email="not-an-email")
        raise AssertionError("bad email should fail")
    except ValueError as exc:
        assert "@" in str(exc)
