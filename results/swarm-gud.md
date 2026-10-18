# Scripted swarm, gud tool trace

Source: `python -m evals.swarm_vs_prs`, gud side of the token table. These are the tool calls billed for the same edits, not the 68 Primitive emails.

41 turns. Input tokens 107,588. Output tokens 2,773. Total 110,361.
Each input figure is the whole prompt on that turn: system, tool list, and history.
No model was called.

## 1. @sub-1 `read`

Input tokens 1,450. Output tokens 23.

**Arguments**

```json
{
  "paths": [
    "auth.py",
    "tests/test_api.py",
    "tests/test_auth.py"
  ]
}
```

**Result**

```text
auth.py
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

tests/test_api.py
"""Handlers, plus the token bucket they do not call yet."""

from __future__ import annotations

import api.handlers as handlers
import auth
import billing
import ratelimit


def test_login_whoami_and_logout(alice):
    denied = handlers.handle_login({"username": "alice", "password": "no"})
    assert denied["status"] == 401
    assert denied["body"]["error"] == "invalid credentials"
    ok = handlers.handle_login({"username": "alice", "password": "correct horse"})
    assert ok["status"] == 200
    token = ok["body"]["token"]
    who = handlers.handle_whoami(token)
    assert who["status"] == 200
    assert who["body"]["username"] == "alice"
    assert who["body"]["email"] == "alice@example.com"
    assert who["body"]["plan"] == "pro"
    assert handlers.handle_whoami(None)["status"] == 401
    assert handlers.handle_whoami("nope")["status"] == 401
    assert handlers.handle_logout(token)["status"] == 200
    assert handlers.handle_whoami(token)["status"] == 401
    assert handlers.handle_logout(token)["status"] == 401


def test_sessions_do_not_cross(alice, bob):
    alice_token = handlers.handle_login({"username": "alice", "password": "correct horse"})["body"]["token"]
    bob_token = handlers.handle_login({"username": "bob", "password": "battery staple"})["body"]["token"]
    assert handlers.handle_whoami(alice_token)["body"]["plan"] == "pro"
    assert handlers.handle_whoami(bob_token)["body"]["plan"] == "free"
    assert auth.check_session(alice_token).username == "alice"
    assert auth.check_session(bob_token).username == "bob"


def test_charge_and_usage(alice_token):
    period = billing.current_period()
    billing.meter_request("alice", period=period, n=4)
    missing = handlers.handle_charge(None, {"amount_cents": 1})
    assert missing["status"] == 401
    bad = handlers.handle_charge(alice_token, {"amount_cents": "5"})
    assert bad["status"] == 400
    assert handlers.handle_charge(alice_token, {"amount_cents": True})["status"] == 400
    charged = handlers.handle_charge(alice_token, {"amount_cents": 500})
    assert charged["status"] == 200
    invoice = charged["body"]["invoice"]
    assert invoice["base_cents"] == 500
    assert invoice["overage_cents"] == 0
    assert invoice["total_cents"] == 500
    usage = handlers.handle_usage(alice_token)
    assert usage["status"] == 200
    assert usage["body"]["requests"] == 4
    assert usage["body"]["included"] == 10_000
    assert usage["body"]["overage_cents"] == 0
    assert handlers.handle_usage(None)["status"] == 401


def test_token_bucket_refills_per_key():
    bucket = ratelimit.TokenBucket(rate=1, capacity=2)
    assert bucket.allow("alice", now=0)
    assert bucket.allow("alice", now=0)
    assert not bucket.allow("alice", now=0)
    assert bucket.remaining("alice", now=0) == 0
    assert bucket.allow("alice", now=1)
    assert bucket.allow("bob", now=0)
    assert bucket.remaining("bob", now=0) == 1


def test_module_bucket_configure_and_reset():
    ratelimit.configure(rate=1, capacity=1)
    assert ratelimit.allow("k", now=10)
    assert not ratelimit.allow("k", now=10)
    assert ratelimit.remaining("k", now=10) == 0
    ratelimit.reset()
    assert ratelimit.allow("k", now=10)
    assert ratelimit.remaining("other", now=10) == 1

tests/test_auth.py
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
```

## 2. @sub-1 `push`

Input tokens 3,808. Output tokens 438.

**Arguments**

```json
{
  "intent": "Rate-limit login",
  "edits": [
    {
      "path": "auth.py",
      "old": "",
      "new": "import ratelimit\n"
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "    if not ratelimit.allow(f\"login:{username}\"):\n        return LoginResult(False, None, \"rate limited\")\n"
    },
    {
      "path": "tests/test_auth.py",
      "old": "",
      "new": "import ratelimit\n"
    },
    {
      "path": "tests/test_auth.py",
      "old": "",
      "new": "def test_second_login_inside_an_empty_bucket_is_rejected(alice):\n    ratelimit.configure(rate=0, capacity=1)\n    try:\n        first = auth.login(\"alice\", \"correct horse\")\n        assert first.ok and first.session is not None\n        second = auth.login(\"alice\", \"correct horse\")\n        assert not second.ok\n        assert second.session is None\n        assert second.reason == \"rate limited\"\n    finally:\n        ratelimit.configure(rate=10, capacity=10)\n"
    },
    {
      "path": "tests/test_api.py",
      "old": "    assert ratelimit.allow(\"k\", now=10)\n    assert not ratelimit.allow(\"k\", now=10)\n    assert ratelimit.remaining(\"k\", now=10) == 0\n    ratelimit.reset()\n    assert ratelimit.allow(\"k\", now=10)\n    assert ratelimit.remaining(\"other\", now=10) == 1\n",
      "new": "    try:\n        assert ratelimit.allow(\"k\", now=10)\n        assert not ratelimit.allow(\"k\", now=10)\n        assert ratelimit.remaining(\"k\", now=10) == 0\n        ratelimit.reset()\n        assert ratelimit.allow(\"k\", now=10)\n        assert ratelimit.remaining(\"other\", now=10) == 1\n    finally:\n        # login now spends tokens; put the process-wide bucket back for later tests.\n        ratelimit.configure(rate=10, capacity=10)\n"
    }
  ]
}
```

**Result**

```text
pushed Rate-limit login
```

## 3. @lead `reply`

Input tokens 1,450. Output tokens 17.

**Arguments**

```json
{
  "action": "approve",
  "target": "Rate-limit login"
}
```

**Result**

```text
approve
```

## 4. @sub-1 `wait`

Input tokens 4,255. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed Rate-limit login pytest pass 16 passed in 0.11s
```

## 5. @sub-2 `read`

Input tokens 1,450. Output tokens 19.

**Arguments**

```json
{
  "paths": [
    "billing.py",
    "tests/test_billing.py"
  ]
}
```

**Result**

```text
billing.py
"""Plans, request metering, and invoices.

`overage_cents` knows what extra requests should cost. `charge` does not add
that figure yet; it invoices only the amount the caller passes (see TASKS.md).
"""

from __future__ import annotations

import time

from api.interfaces import Invoice, Plan, Usage

PLANS: dict[str, Plan] = {
    "free": Plan("free", monthly_cents=0, included_requests=100, overage_cents=2),
    "pro": Plan("pro", monthly_cents=2000, included_requests=10_000, overage_cents=1),
}

_accounts: dict[str, str] = {}
_usage: dict[tuple[str, str], int] = {}
_invoices: dict[str, list[Invoice]] = {}


def reset() -> None:
    """Drop accounts, meters, and invoices."""
    _accounts.clear()
    _usage.clear()
    _invoices.clear()


def open_account(username: str, plan: str = "free") -> Plan:
    """Attach `username` to a plan. `register_user` calls this."""
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan}")
    _accounts[username] = plan
    return PLANS[plan]


def plan_for(username: str) -> Plan:
    """The user's plan, or `free` when they have no account row."""
    return PLANS[_accounts.get(username, "free")]


def current_period(when: float | None = None) -> str:
    """UTC `YYYY-MM` for `when` (default: now)."""
    stamp = time.gmtime(time.time() if when is None else when)
    return f"{stamp.tm_year:04d}-{stamp.tm_mon:02d}"


def meter_request(username: str, period: str | None = None, n: int = 1) -> Usage:
    """Add `n` requests to the meter and return the new total."""
    if n < 1:
        raise ValueError("n must be >= 1")
    period = period or current_period()
    key = (username, period)
    _usage[key] = _usage.get(key, 0) + n
    return usage_of(username, period)


def usage_of(username: str, period: str | None = None) -> Usage:
    period = period or current_period()
    return Usage(username, period, _usage.get((username, period), 0))


def overage_cents(requests: int, plan: Plan) -> int:
    """Cents owed for requests past `plan.included_requests`. Zero when inside the plan."""
    extra = max(0, requests - plan.included_requests)
    return extra * plan.overage_cents


def charge(user: str, amount_cents: int, period: str | None = None) -> Invoice:
    """Record an invoice for `amount_cents`. Overage is not included yet.

    Raises `KeyError` when `user` has no account, `ValueError` when the amount is negative.
    """
    if amount_cents < 0:
        raise ValueError("amount_cents must be >= 0")
    if user not in _accounts:
        raise KeyError(user)
    period = period or current_period()
    invoice = Invoice(
        username=user, period=period, base_cents=amount_cents, overage_cents=0, total_cents=amount_cents,
    )
    _invoices.setdefault(user, []).append(invoice)
    return invoice


def list_invoices(user: str) -> list[Invoice]:
    """Invoices for `user`, oldest first. Empty when they have never been charged."""
    return list(_invoices.get(user, []))


def outstanding_cents(user: str) -> int:
    """Sum of `total_cents` across the user's invoices."""
    return sum(invoice.total_cents for invoice in _invoices.get(user, []))

tests/test_billing.py
"""Plans, metering, overage math, and invoices."""

from __future__ import annotations

import pytest

import billing


def test_open_account_and_unknown_plan():
    plan = billing.open_account("carol", "pro")
    assert plan.monthly_cents == 2000
    assert billing.plan_for("carol").included_requests == 10_000
    assert billing.plan_for("stranger").name == "free"
    with pytest.raises(ValueError):
        billing.open_account("carol", "enterprise")


def test_meter_is_per_period():
    billing.open_account("carol", "free")
    first = billing.meter_request("carol", period="2026-10", n=3)
    assert first.requests == 3
    assert billing.meter_request("carol", period="2026-10").requests == 4
    other = billing.usage_of("carol", period="2026-11")
    assert other.requests == 0
    assert billing.current_period(1_700_000_000) == "2023-11"


def test_overage_cents_inside_and_past_the_plan():
    free = billing.PLANS["free"]
    assert free.included_requests == 100
    assert billing.overage_cents(100, free) == 0
    assert billing.overage_cents(103, free) == 6
    assert billing.overage_cents(0, billing.PLANS["pro"]) == 0


def test_charge_records_the_amount_without_overage():
    billing.open_account("carol", "free")
    billing.meter_request("carol", period="2026-10", n=150)
    invoice = billing.charge("carol", 250, period="2026-10")
    assert invoice.base_cents == 250
    assert invoice.overage_cents == 0
    assert invoice.total_cents == 250
    assert billing.list_invoices("carol") == [invoice]
    assert billing.outstanding_cents("carol") == 250
    again = billing.charge("carol", 0, period="2026-10")
    assert billing.outstanding_cents("carol") == 250
    assert again.total_cents == 0


def test_charge_rejects_unknown_user_and_negative_amount():
    with pytest.raises(KeyError):
        billing.charge("ghost", 10, period="2026-10")
    billing.open_account("carol")
    with pytest.raises(ValueError):
        billing.charge("carol", -1)
    with pytest.raises(ValueError):
        billing.meter_request("carol", n=0)
```

## 6. @sub-2 `push`

Input tokens 2,873. Output tokens 314.

**Arguments**

```json
{
  "intent": "Bill overage in charge",
  "edits": [
    {
      "path": "billing.py",
      "old": "",
      "new": "    usage = usage_of(user, period)\n    plan = plan_for(user)\n    extra = overage_cents(usage.requests, plan)\n"
    },
    {
      "path": "billing.py",
      "old": "        username=user, period=period, base_cents=amount_cents, overage_cents=0, total_cents=amount_cents,\n",
      "new": "        username=user, period=period, base_cents=amount_cents, overage_cents=extra,\n        total_cents=amount_cents + extra,\n"
    },
    {
      "path": "tests/test_billing.py",
      "old": "    billing.meter_request(\"carol\", period=\"2026-10\", n=150)\n",
      "new": "    billing.meter_request(\"carol\", period=\"2026-10\", n=10)\n"
    },
    {
      "path": "tests/test_billing.py",
      "old": "",
      "new": "def test_charge_of_zero_on_the_free_plan_bills_overage():\n    billing.open_account(\"carol\", \"free\")\n    billing.meter_request(\"carol\", period=\"2026-10\", n=103)\n    invoice = billing.charge(\"carol\", 0, period=\"2026-10\")\n    assert invoice.base_cents == 0\n    assert invoice.overage_cents == 6\n    assert invoice.total_cents == 6\n"
    }
  ]
}
```

**Result**

```text
pushed Bill overage in charge
```

## 7. @lead `reply`

Input tokens 1,473. Output tokens 19.

**Arguments**

```json
{
  "action": "approve",
  "target": "Bill overage in charge"
}
```

**Result**

```text
approve
```

## 8. @sub-2 `wait`

Input tokens 3,198. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed Bill overage in charge pytest pass 16 passed in 0.11s
```

## 9. @sub-3 `read`

Input tokens 1,450. Output tokens 20.

**Arguments**

```json
{
  "paths": [
    "api/handlers.py",
    "tests/test_api.py"
  ]
}
```

**Result**

```text
api/handlers.py
"""HTTP-shaped handlers over auth and billing.

Each function returns ``{"status": int, "body": dict}``. They do not apply
rate limits or bill overage; those land as separate pushes (see TASKS.md).
"""

from __future__ import annotations

import auth
import billing
from api.interfaces import Invoice


def handle_login(payload: dict) -> dict:
    """Check username and password. 200 with a token, or 401."""
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    result = auth.login(username, password)
    if not result.ok or result.session is None:
        return _error(401, result.reason or "invalid credentials")
    return _ok({"token": result.session.token, "username": result.session.username})


def handle_whoami(token: str | None) -> dict:
    """The user behind `token`, or 401."""
    session = auth.check_session(token)
    if session is None:
        return _error(401, "no session")
    user = auth.get_user(session.username)
    if user is None:
        return _error(401, "no session")
    return _ok({"username": user.username, "email": user.email, "plan": user.plan})


def handle_logout(token: str | None) -> dict:
    """Drop `token`. 401 when it was not a live session."""
    if not token or auth.check_session(token) is None:
        return _error(401, "no session")
    auth.logout(token)
    return _ok({"logged_out": True})


def handle_charge(token: str | None, payload: dict) -> dict:
    """Invoice `amount_cents` to the session's user. Does not add plan overage."""
    session = auth.check_session(token)
    if session is None:
        return _error(401, "no session")
    amount = payload.get("amount_cents")
    # bool is an int, and it is not an amount.
    if type(amount) is not int:
        return _error(400, "amount_cents must be an integer")
    try:
        invoice = billing.charge(session.username, amount)
    except KeyError:
        return _error(404, "no billing account")
    except ValueError as exc:
        return _error(400, str(exc))
    return _ok({"invoice": invoice_view(invoice)})


def handle_usage(token: str | None) -> dict:
    """Metered requests for the session's user, plus what overage would cost."""
    session = auth.check_session(token)
    if session is None:
        return _error(401, "no session")
    usage = billing.usage_of(session.username)
    plan = billing.plan_for(session.username)
    return _ok({
        "username": usage.username,
        "period": usage.period,
        "requests": usage.requests,
        "included": plan.included_requests,
        "overage_cents": billing.overage_cents(usage.requests, plan),
    })


def invoice_view(invoice: Invoice) -> dict:
    """A JSON-ready invoice. Handlers and tests both use this."""
    return {
        "username": invoice.username,
        "period": invoice.period,
        "base_cents": invoice.base_cents,
        "overage_cents": invoice.overage_cents,
        "total_cents": invoice.total_cents,
    }


def _ok(body: dict) -> dict:
    return {"status": 200, "body": body}


def _error(status: int, reason: str) -> dict:
    return {"status": status, "body": {"error": reason}}

tests/test_api.py
"""Handlers, plus the token bucket they do not call yet."""

from __future__ import annotations

import api.handlers as handlers
import auth
import billing
import ratelimit


def test_login_whoami_and_logout(alice):
    denied = handlers.handle_login({"username": "alice", "password": "no"})
    assert denied["status"] == 401
    assert denied["body"]["error"] == "invalid credentials"
    ok = handlers.handle_login({"username": "alice", "password": "correct horse"})
    assert ok["status"] == 200
    token = ok["body"]["token"]
    who = handlers.handle_whoami(token)
    assert who["status"] == 200
    assert who["body"]["username"] == "alice"
    assert who["body"]["email"] == "alice@example.com"
    assert who["body"]["plan"] == "pro"
    assert handlers.handle_whoami(None)["status"] == 401
    assert handlers.handle_whoami("nope")["status"] == 401
    assert handlers.handle_logout(token)["status"] == 200
    assert handlers.handle_whoami(token)["status"] == 401
    assert handlers.handle_logout(token)["status"] == 401


def test_sessions_do_not_cross(alice, bob):
    alice_token = handlers.handle_login({"username": "alice", "password": "correct horse"})["body"]["token"]
    bob_token = handlers.handle_login({"username": "bob", "password": "battery staple"})["body"]["token"]
    assert handlers.handle_whoami(alice_token)["body"]["plan"] == "pro"
    assert handlers.handle_whoami(bob_token)["body"]["plan"] == "free"
    assert auth.check_session(alice_token).username == "alice"
    assert auth.check_session(bob_token).username == "bob"


def test_charge_and_usage(alice_token):
    period = billing.current_period()
    billing.meter_request("alice", period=period, n=4)
    missing = handlers.handle_charge(None, {"amount_cents": 1})
    assert missing["status"] == 401
    bad = handlers.handle_charge(alice_token, {"amount_cents": "5"})
    assert bad["status"] == 400
    assert handlers.handle_charge(alice_token, {"amount_cents": True})["status"] == 400
    charged = handlers.handle_charge(alice_token, {"amount_cents": 500})
    assert charged["status"] == 200
    invoice = charged["body"]["invoice"]
    assert invoice["base_cents"] == 500
    assert invoice["overage_cents"] == 0
    assert invoice["total_cents"] == 500
    usage = handlers.handle_usage(alice_token)
    assert usage["status"] == 200
    assert usage["body"]["requests"] == 4
    assert usage["body"]["included"] == 10_000
    assert usage["body"]["overage_cents"] == 0
    assert handlers.handle_usage(None)["status"] == 401


def test_token_bucket_refills_per_key():
    bucket = ratelimit.TokenBucket(rate=1, capacity=2)
    assert bucket.allow("alice", now=0)
    assert bucket.allow("alice", now=0)
    assert not bucket.allow("alice", now=0)
    assert bucket.remaining("alice", now=0) == 0
    assert bucket.allow("alice", now=1)
    assert bucket.allow("bob", now=0)
    assert bucket.remaining("bob", now=0) == 1


def test_module_bucket_configure_and_reset():
    ratelimit.configure(rate=1, capacity=1)
    assert ratelimit.allow("k", now=10)
    assert not ratelimit.allow("k", now=10)
    assert ratelimit.remaining("k", now=10) == 0
    ratelimit.reset()
    assert ratelimit.allow("k", now=10)
    assert ratelimit.remaining("other", now=10) == 1
```

## 10. @sub-3 `push`

Input tokens 3,105. Output tokens 147.

**Arguments**

```json
{
  "intent": "Map 429 and meter charges",
  "edits": [
    {
      "path": "api/handlers.py",
      "old": "        return _error(401, result.reason or \"invalid credentials\")\n",
      "new": "        status = 429 if result.reason == \"rate limited\" else 401\n        return _error(status, result.reason or \"invalid credentials\")\n"
    },
    {
      "path": "api/handlers.py",
      "old": "",
      "new": "    billing.meter_request(session.username)\n"
    },
    {
      "path": "tests/test_api.py",
      "old": "    assert usage[\"body\"][\"requests\"] == 4\n",
      "new": "    assert usage[\"body\"][\"requests\"] == 5\n"
    }
  ]
}
```

**Result**

```text
pushed Map 429 and meter charges
```

## 11. @lead `reply`

Input tokens 1,499. Output tokens 20.

**Arguments**

```json
{
  "action": "approve",
  "target": "Map 429 and meter charges"
}
```

**Result**

```text
approve
```

## 12. @sub-3 `wait`

Input tokens 3,264. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed Map 429 and meter charges pytest pass 15 passed in 0.10s
```

## 13. @sub-4 `read`

Input tokens 1,450. Output tokens 22.

**Arguments**

```json
{
  "paths": [
    "ratelimit.py",
    "tests/test_penalize.py"
  ]
}
```

**Result**

```text
ratelimit.py
"""A token bucket keyed by user or route.

`allow` spends tokens; they refill at `rate` per second up to `capacity`.
Nothing in auth or billing calls this yet. The demo task wires it into `login`
and adds a way to drain a key after abuse (see TASKS.md).
"""

from __future__ import annotations

import time


class TokenBucket:
    """One bucket per key. Pass `now` in tests so refill does not depend on the clock."""

    def __init__(self, rate: float, capacity: float):
        if rate < 0 or capacity <= 0:
            raise ValueError("rate must be >= 0 and capacity must be > 0")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens: dict[str, float] = {}
        self._updated: dict[str, float] = {}

    def allow(self, key: str, now: float | None = None, cost: float = 1.0) -> bool:
        """Spend `cost` tokens for `key`. False when the bucket cannot afford it."""
        if cost <= 0:
            raise ValueError("cost must be > 0")
        now = time.monotonic() if now is None else now
        tokens = self._available(key, now)
        if tokens < cost:
            self._tokens[key] = tokens
            self._updated[key] = now
            return False
        self._tokens[key] = tokens - cost
        self._updated[key] = now
        return True

    def remaining(self, key: str, now: float | None = None) -> int:
        """Whole tokens available right now. Does not spend any."""
        now = time.monotonic() if now is None else now
        return int(self._available(key, now))

    def reset(self) -> None:
        """Forget every key. The rate and capacity stay."""
        self._tokens.clear()
        self._updated.clear()

    def _available(self, key: str, now: float) -> float:
        if key not in self._tokens:
            return self.capacity
        elapsed = max(0.0, now - self._updated.get(key, now))
        return min(self.capacity, self._tokens[key] + elapsed * self.rate)


_default = TokenBucket(rate=10, capacity=10)


def configure(rate: float, capacity: float) -> TokenBucket:
    """Replace the process-wide bucket. Returns it."""
    global _default
    _default = TokenBucket(rate, capacity)
    return _default


def reset() -> None:
    """Empty the process-wide bucket."""
    _default.reset()


def allow(key: str, now: float | None = None, cost: float = 1.0) -> bool:
    return _default.allow(key, now=now, cost=cost)


def remaining(key: str, now: float | None = None) -> int:
    return _default.remaining(key, now=now)
```

## 14. @sub-4 `push`

Input tokens 2,112. Output tokens 393.

**Arguments**

```json
{
  "intent": "Add penalize",
  "edits": [
    {
      "path": "ratelimit.py",
      "old": "",
      "new": "\ndef _penalize(self, key: str, now: float | None = None) -> None:\n    \"\"\"Drain `key`. The next allow fails until the bucket refills.\"\"\"\n    now = time.monotonic() if now is None else now\n    self._tokens[key] = 0.0\n    self._updated[key] = now\n\n\nif not hasattr(TokenBucket, \"penalize\"):\n    TokenBucket.penalize = _penalize\n\n\ndef penalize(key: str, now: float | None = None) -> None:\n    \"\"\"Set the process-wide bucket's tokens for `key` to zero.\"\"\"\n    _default.penalize(key, now=now)\n"
    },
    {
      "path": "tests/test_penalize.py",
      "old": "",
      "new": "\"\"\"penalize drains a key so the next allow fails.\"\"\"\n\nfrom __future__ import annotations\n\nimport ratelimit\n\n\ndef test_penalize_makes_the_next_allow_fail():\n    bucket = ratelimit.TokenBucket(rate=0, capacity=2)\n    assert bucket.allow(\"alice\", now=0)\n    bucket.penalize(\"alice\", now=0)\n    assert bucket.allow(\"alice\", now=0) is False\n    ratelimit.configure(rate=0, capacity=2)\n    try:\n        assert ratelimit.allow(\"k\", now=0)\n        ratelimit.penalize(\"k\", now=0)\n        assert ratelimit.allow(\"k\", now=0) is False\n    finally:\n        ratelimit.configure(rate=10, capacity=10)\n"
    }
  ]
}
```

**Result**

```text
pushed Add penalize
```

## 15. @lead `reply`

Input tokens 1,526. Output tokens 17.

**Arguments**

```json
{
  "action": "approve",
  "target": "Add penalize"
}
```

**Result**

```text
approve
```

## 16. @sub-4 `wait`

Input tokens 2,514. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed Add penalize pytest pass 16 passed in 0.11s
```

## 17. @auth-agent `read`

Input tokens 1,450. Output tokens 12.

**Arguments**

```json
{
  "path": "auth.py"
}
```

**Result**

```text
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
import ratelimit
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
    if not ratelimit.allow(f"login:{username}"):
        return LoginResult(False, None, "rate limited")
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
```

## 18. @auth-agent `push`

Input tokens 2,413. Output tokens 234.

**Arguments**

```json
{
  "intent": "lockout",
  "edits": [
    {
      "path": "auth.py",
      "old": "",
      "new": "_failures: dict[str, int] = {}\n"
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "    _failures.clear()\n"
    },
    {
      "path": "auth.py",
      "old": "    if not ratelimit.allow(f\"login:{username}\"):\n",
      "new": "    # gud-bad-edit: lockout replaces the login flow\n    if not ratelimit.allow(\"login\"):\n"
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "    if _failures.get(username, 0) >= 5:\n        return LoginResult(False, None, \"locked\")\n"
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "        _failures[username] = _failures.get(username, 0) + 1\n"
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "    _failures.pop(username, None)\n"
    },
    {
      "path": "auth.py",
      "old": "\n",
      "new": ""
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "    _failures.pop(username, None)\n"
    }
  ]
}
```

**Result**

```text
pushed lockout
```

## 19. @sub-1 `thread`

Input tokens 4,285. Output tokens 12.

**Arguments**

```json
{
  "id": "lockout"
}
```

**Result**

```text
diff --git a/auth.py b/auth.py
index 66eb074..763689f 100644
--- a/auth.py
+++ b/auth.py
@@ -19,12 +19,14 @@ PBKDF2_ROUNDS = 12_000
 
 _users: dict[str, tuple[User, str]] = {}
 _sessions: dict[str, Session] = {}
+_failures: dict[str, int] = {}
 
 
 def reset() -> None:
     """Drop every user and session. Tests call this between cases."""
     _users.clear()
     _sessions.clear()
+    _failures.clear()
 
 
 def hash_password(password: str, salt: bytes | None = None) -> str:
@@ -86,13 +88,16 @@ def start_session(username: str, now: float | None = None) -> Session:
 def login(username: str, password: str) -> LoginResult:
     """Check the password and start a session. Unknown user and bad password share one reason."""
     if not ratelimit.allow(f"login:{username}"):
-        return LoginResult(False, None, "rate limited")
+        return LoginResult(False, None, "rate limited")  # owner kept this check
+    if _failures.get(username, 0) >= 5:
+        return LoginResult(False, None, "locked")
     row = _users.get(username)
     if row is None or not verify_password(password, row[1]):
+        _failures[username] = _failures.get(username, 0) + 1
         return LoginResult(False, None, "invalid credentials")
+    _failures.pop(username, None)
     return LoginResult(True, start_session(username), "")
 
-
 def check_session(token: str | None) -> Session | None:
     if not token:
         return None
@@ -110,6 +115,7 @@ def logout(token: str) -> bool:
 
 def revoke_user_sessions(username: str) -> int:
     """Drop every session for `username`. Returns how many were removed."""
+    _failures.pop(username, None)
     doomed = [token for token, session in _sessions.items() if session.username == username]
     for token in doomed:
         _sessions.pop(token, None)
diff --git a/tests/test_auth.py b/tests/test_auth.py
index ae1c538..5fb337c 100644
--- a/tests/test_auth.py
+++ b/tests/test_auth.py
@@ -74,3 +74,17 @@ def test_second_login_inside_an_empty_bucket_is_reject
```

## 20. @sub-1 `reply`

Input tokens 4,843. Output tokens 43.

**Arguments**

```json
{
  "action": "object",
  "reason": "keep the rate-limit check per user, ratelimit.allow(f\"login:{username}\"); one shared key lets one client rate-limit every user"
}
```

**Result**

```text
object
```

## 21. @auth-agent `wait`

Input tokens 2,655. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
objected: keep the rate-limit check per user, ratelimit.allow(f"login:{username}"); one shared key lets one client rate-limit every user
```

## 22. @auth-agent `read`

Input tokens 2,701. Output tokens 12.

**Arguments**

```json
{
  "path": "auth.py"
}
```

**Result**

```text
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
import ratelimit
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
    if not ratelimit.allow(f"login:{username}"):
        return LoginResult(False, None, "rate limited")
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
```

## 23. @auth-agent `push`

Input tokens 3,665. Output tokens 383.

**Arguments**

```json
{
  "intent": "lockout revised",
  "edits": [
    {
      "path": "auth.py",
      "old": "",
      "new": "_failures: dict[str, int] = {}\n"
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "    _failures.clear()\n"
    },
    {
      "path": "auth.py",
      "old": "        return LoginResult(False, None, \"rate limited\")\n",
      "new": "        return LoginResult(False, None, \"rate limited\")  # owner kept this check\n    if _failures.get(username, 0) >= 5:\n        return LoginResult(False, None, \"locked\")\n"
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "        _failures[username] = _failures.get(username, 0) + 1\n"
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "    _failures.pop(username, None)\n"
    },
    {
      "path": "auth.py",
      "old": "\n",
      "new": ""
    },
    {
      "path": "auth.py",
      "old": "",
      "new": "    _failures.pop(username, None)\n"
    },
    {
      "path": "tests/test_auth.py",
      "old": "",
      "new": "def test_five_bad_passwords_lock_the_account(alice):\n    ratelimit.configure(rate=0, capacity=20)\n    try:\n        for _ in range(5):\n            denied = auth.login(\"alice\", \"nope\")\n            assert denied.reason == \"invalid credentials\"\n        locked = auth.login(\"alice\", \"correct horse\")\n        assert not locked.ok and locked.session is None\n        assert locked.reason == \"locked\"\n        auth.revoke_user_sessions(\"alice\")\n        again = auth.login(\"alice\", \"correct horse\")\n        assert again.ok and again.session is not None\n    finally:\n        ratelimit.configure(rate=10, capacity=10)\n"
    }
  ]
}
```

**Result**

```text
pushed revision
```

## 24. @sub-1 `reply`

Input tokens 4,893. Output tokens 11.

**Arguments**

```json
{
  "action": "approve"
}
```

**Result**

```text
approve
```

## 25. @auth-agent `wait`

Input tokens 4,055. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed revision pytest pass 19 passed in 0.13s
```

## 26. @billing-agent `push`

Input tokens 1,450. Output tokens 118.

**Arguments**

```json
{
  "intent": "RateLimitDecision",
  "edits": [
    {
      "path": "api/interfaces.py",
      "old": "",
      "new": "@dataclass(frozen=True)\nclass RateLimitDecision:\n    \"\"\"Whether a key may proceed, and how long a caller should wait if it may not.\"\"\"\n\n    allowed: bool\n    remaining: int\n    retry_after: float\n\n\n"
    },
    {
      "path": "api/interfaces.py",
      "old": "",
      "new": "\n    def decide(self, key: str) -> RateLimitDecision: ...\n"
    }
  ]
}
```

**Result**

```text
pushed interface
```

## 27. @lead `reply`

Input tokens 1,550. Output tokens 17.

**Arguments**

```json
{
  "action": "approve",
  "target": "api/interfaces.py"
}
```

**Result**

```text
approve
```

## 28. @billing-agent `wait`

Input tokens 1,575. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed interface pytest pass 19 passed in 0.13s
```

## 29. @sub-1 `push`

Input tokens 4,911. Output tokens 35.

**Arguments**

```json
{
  "intent": "race_a.py",
  "edits": [
    {
      "path": "race_a.py",
      "old": "",
      "new": "A = 1\n"
    }
  ]
}
```

**Result**

```text
pushed race_a
```

## 30. @sub-2 `push`

Input tokens 3,230. Output tokens 35.

**Arguments**

```json
{
  "intent": "race_b.py",
  "edits": [
    {
      "path": "race_b.py",
      "old": "",
      "new": "B = 1\n"
    }
  ]
}
```

**Result**

```text
pushed race_b
```

## 31. @sub-1 `wait`

Input tokens 4,955. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed race_a
```

## 32. @sub-2 `wait`

Input tokens 3,274. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
superseded
```

## 33. @sub-2 `push`

Input tokens 3,291. Output tokens 35.

**Arguments**

```json
{
  "intent": "race_b.py",
  "edits": [
    {
      "path": "race_b.py",
      "old": "",
      "new": "B = 1\n"
    }
  ]
}
```

**Result**

```text
re-pushed race_b
```

## 34. @sub-2 `wait`

Input tokens 3,336. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed race_b
```

## 35. @lead `push`

Input tokens 1,574. Output tokens 51.

**Arguments**

```json
{
  "intent": "add @guest-agent",
  "edits": [
    {
      "path": ".gud/members",
      "old": "",
      "new": "@guest-agent agent guest-agent@gud.local ed25519:guest @nelson\n"
    }
  ]
}
```

**Result**

```text
proposed add
```

## 36. @nelson `reply`

Input tokens 1,450. Output tokens 15.

**Arguments**

```json
{
  "action": "approve",
  "target": "members"
}
```

**Result**

```text
approve
```

## 37. @lead `wait`

Input tokens 1,633. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed add pytest pass 19 passed in 0.15s
```

## 38. @lead `push`

Input tokens 1,661. Output tokens 177.

**Arguments**

```json
{
  "intent": "remove @sub-4",
  "edits": [
    {
      "path": ".gud/members",
      "old": "",
      "new": "\n"
    },
    {
      "path": ".gud/members",
      "old": "",
      "new": "\n"
    },
    {
      "path": ".gud/members",
      "old": "",
      "new": "\n"
    },
    {
      "path": ".gud/members",
      "old": "",
      "new": "\n"
    },
    {
      "path": ".gud/members",
      "old": "@sub-4 agent sub-4@gud.local ed25519:s4 @nelson\n",
      "new": "\n"
    },
    {
      "path": ".gud/members",
      "old": "",
      "new": "\n"
    },
    {
      "path": ".gud/members",
      "old": "",
      "new": "\n"
    },
    {
      "path": ".gud/members",
      "old": "",
      "new": "\n"
    },
    {
      "path": ".gud/members",
      "old": "",
      "new": "\n"
    }
  ]
}
```

**Result**

```text
proposed remove
```

## 39. @nelson `reply`

Input tokens 1,471. Output tokens 15.

**Arguments**

```json
{
  "action": "approve",
  "target": "members"
}
```

**Result**

```text
approve
```

## 40. @lead `wait`

Input tokens 1,846. Output tokens 9.

**Arguments**

```json
{}
```

**Result**

```text
landed remove
```

## 41. @sub-4 `push`

Input tokens 2,544. Output tokens 11.

**Arguments**

```json
{
  "intent": "ghost"
}
```

**Result**

```text
rejected: author is not a member
```
