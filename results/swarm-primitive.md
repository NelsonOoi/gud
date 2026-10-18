# Primitive swarm mailbox

Repo `repo-swarm-20261018@lean-ridge.primitive.email`. 68 emails in the order Primitive listed them.
This is the scripted `scripts/demo.py` run, not the tool transcript used for the token table.

## 1. push — [gud] genesis

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`, `console@lean-ridge.primitive.email`
- Message id: `<primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>`
- Received at: 1792331988.799
- Sender verified: True

````text
genesis from demo_repo

Gud-Kind: push
Gud-Author: @lead
Gud-Parent: none
Gud-Base: 0000000000000000000000000000000000000000000000000000000000000000
Gud-State: 3fbf6bc3826deb5a537ea62c3cecdee03ccd8ad348f9c4c5c379a308e9f57078
Gud-Tree: 31df16defd6362ed442e3843f1bb28556cafce311c3a7c58b937dea373cc7d7d
Gud-Sig: ed25519:vuClpvQZ1wi2WAnKd9zxzjzxgbnU/UqrMCkn2nNFNdeGodZ6MOI/dwWl+fX4X5y+GUxolY/qF9fLzNBCfpmGDA==

```diff
diff --git a/.gud/members b/.gud/members
--- /dev/null
+++ b/.gud/members
@@ -0,0 +1,13 @@
+# handle role address public_key sponsor
+@lead lead lead@lean-ridge.primitive.email ed25519:VgFRNwtuNzPytcVoZYBzqGHaYhfzbLyXqYWTfkayo9Y= @nelson
+@auth-agent agent auth-agent@lean-ridge.primitive.email ed25519:h1aem6NHttf18aXGfDhzsSE5DpINO50J9yHNBrwa058= @nelson
+@billing-agent agent billing-agent@lean-ridge.primitive.email ed25519:nrK3tbntjZKzoQAr7dB5QHBG8/ddNJlWom89qLpkCvU= @nelson
+@api-agent agent api-agent@lean-ridge.primitive.email ed25519:C5HnfSEDUCiymyUOeAIyca6BHRcghNnGNRVtBJsO81w= @nelson
+@sub-1 agent sub-1@lean-ridge.primitive.email ed25519:ol7ZUYZUfQlN2x3h+E7z44IiaPNA45/qWVlsmy4aig0= @nelson
+@sub-2 agent sub-2@lean-ridge.primitive.email ed25519:5KsbU7J0lXgQ/fb1egGmIo0OhC4b8yuX/5Jrg+T0akU= @nelson
+@sub-3 agent sub-3@lean-ridge.primitive.email ed25519:BD89PETGvi/0PxtuhtrVXfhkFs9WK12LUd9UrBuIch8= @nelson
+@sub-4 agent sub-4@lean-ridge.primitive.email ed25519:h8JtB70w81tzoskb7fMj8e6i2xf15HMVoHHSpyRuOMI= @nelson
+@tester tester tester@lean-ridge.primitive.email ed25519:OzHw3pTuMsPWy/e4AEyN2HjvY5uU9v5ogvmAI/0wsao= @nelson
+@qa qa qa@lean-ridge.primitive.email ed25519:cX39Nts8elJNoDuD0dg5P3S4dA/93KxZLxSE5N26Oqg= @nelson
+@console console console@lean-ridge.primitive.email ed25519:InqKr6+04CzAvKZ4CP1dLT2JHK87ouBrPAqyfNnLs7w= @nelson
+@nelson human nelson@lean-ridge.primitive.email - -
diff --git a/.gud/protected b/.gud/protected
--- /dev/null
+++ b/.gud/protected
@@ -0,0 +1,2 @@
+api/interfaces.py explicit
+.gud/* quorum
diff --git a/TASKS.md b/TASKS.md
--- /dev/null
+++ b/TASKS.md
@@ -0,0 +1,80 @@
+# Demo tasks
+
+The service under this directory is the starting tree. `pytest -q` is green.
+A push lands only after a tester reply says these tests pass on the resulting
+tree, and only after consent (see `SPEC.md`). `.gud/protected` is:
+
+```
+api/interfaces.py explicit
+.gud/* quorum
+```
+
+Do not edit `.gud/*` in these tasks. Membership changes need a quorum; these
+tasks do not.
+
+Agents push against the current tip. Two pushes that share a parent cannot
+both become the next link: the earliest one that qualifies lands, and the
+other has to be replayed on top of it. That is the swarm.
+
+## Task 1 — Add per-user rate limiting to login and bill overage requests
+
+Split across files so the diffs do not touch the same lines. They still share
+a parent until each one is rebased onto whoever landed first.
+
+| Subtask | Agent | File | What to change |
+| --- | --- | --- | --- |
+| T1.1 | `@auth-agent` | `auth.py` `login` | Call `ratelimit.allow(f"login:{username}")` before checking the password. When it returns false, return `LoginResult(False, None, "rate limited")` and do not start a session. |
+| T1.2 | `@billing-agent` | `billing.py` `charge` | Read `usage_of` for the period, set `overage_cents` from `overage_cents(requests, plan)`, and set `total_cents` to `amount_cents + overage_cents`. Keep rejecting a missing account and a negative amount. |
+| T1.3 | `@api-agent` | `api/handlers.py` `handle_login`, `handle_charge` | Map login reason `"rate limited"` to status 429. On a successful charge, call `billing.meter_request` for that user so the next invoice sees the call. |
+| T1.4 | `@sub-2` | `ratelimit.py` | Add `penalize(key)` on `TokenBucket` (and the module wrapper) that sets that key's tokens to zero. Leave `allow` / `remaining` behavior as the tests describe it. |
+
+`@sub-1`, `@sub-3`, and `@sub-4` stay idle on this task unless the lead
+splits T1.3 into login versus charge.
+
+Tests to add with the code: a second login inside an empty bucket is rejected;
+`charge` of 0 cents on the free plan after 103 requests in the period invoices
+`overage_cents == 6`; `penalize` makes the next `allow` fail.
+
+## Task 2 — Lockout and rate limit collide in `login`
+
+This one is supposed to object. `@auth-agent` and `@sub-1` both edit
+`auth.login` in `auth.py`.
+
+- `@auth-agent` adds the rate-limit check from T1.1 (if it has not landed).
+- `@sub-1` adds a failure counter: five `login` calls with a wrong password
+  for the same username lock the account, and later calls return
+  `LoginResult(False, None, "locked")` until `revoke_user_sessions` (or a new
+  helper) clears it.
+
+Whoever lands second rewrites lines the first agent now owns. The owner
+objects on the thread, for example: "keep the rate-limit check; do not replace
+`login`". `@sub-1` (or `@auth-agent`, if they were the one objected to) sends
+a new push of `login` that does both: rate limit first, then the lockout
+counter, then the password check. The owner approves that push. The objected
+push never becomes the next link.
+
+Show the objection on the original push and the revised push landing after
+approval, with blame for `login` ending on the agent who sent the combined
+version.
+
+## Task 3 — Record the rate-limit decision on the protected interface
+
+`@api-agent` edits `api/interfaces.py` only:
+
+- Add a frozen dataclass `RateLimitDecision` with `allowed: bool`,
+  `remaining: int`, and `retry_after: float`.
+- Add `decide(self, key: str) -> RateLimitDecision` to the `RateLimiter`
+  protocol.
+
+That path is `explicit`. Genesis is authored by `@lead`, so `@lead` owns the
+protocol lines this push rewrites and has to approve. If the core treats the
+change as having no affected owner (a brand-new dataclass at the bottom of the
+file, no rewritten line), one approval from any other voter is still required.
+Either way, have `@lead` send a signed `approve`, or have `@nelson` reply
+`approve` on the thread. Do not wait on the lazy-consensus window: explicit
+consent does not open one.
+
+`ratelimit.TokenBucket` does not have to grow `decide` in this task. A later
+push can implement it. This task is the consent beat: the push stays pending,
+with no link, until that approval arrives, and then the tester still has to
+pass the tree.
diff --git a/api/handlers.py b/api/handlers.py
--- /dev/null
+++ b/api/handlers.py
@@ -0,0 +1,93 @@
+"""HTTP-shaped handlers over auth and billing.
+
+Each function returns ``{"status": int, "body": dict}``. They do not apply
+rate limits or bill overage; those land as separate pushes (see TASKS.md).
+"""
+
+from __future__ import annotations
+
+import auth
+import billing
+from api.interfaces import Invoice
+
+
+def handle_login(payload: dict) -> dict:
+    """Check username and password. 200 with a token, or 401."""
+    username = str(payload.get("username") or "")
+    password = str(payload.get("password") or "")
+    result = auth.login(username, password)
+    if not result.ok or result.session is None:
+        return _error(401, result.reason or "invalid credentials")
+    return _ok({"token": result.session.token, "username": result.session.username})
+
+
+def handle_whoami(token: str | None) -> dict:
+    """The user behind `token`, or 401."""
+    session = auth.check_session(token)
+    if session is None:
+        return _error(401, "no session")
+    user = auth.get_user(session.username)
+    if user is None:
+        return _error(401, "no session")
+    return _ok({"username": user.username, "email": user.email, "plan": user.plan})
+
+
+def handle_logout(token: str | None) -> dict:
+    """Drop `token`. 401 when it was not a live session."""
+    if not token or auth.check_session(token) is None:
+        return _error(401, "no session")
+    auth.logout(token)
+    return _ok({"logged_out": True})
+
+
+def handle_charge(token: str | None, payload: dict) -> dict:
+    """Invoice `amount_cents` to the session's user. Does not add plan overage."""
+    session = auth.check_session(token)
+    if session is None:
+        return _error(401, "no session")
+    amount = payload.get("amount_cents")
+    # bool is an int, and it is not an amount.
+    if type(amount) is not int:
+        return _error(400, "amount_cents must be an integer")
+    try:
+        invoice = billing.charge(session.username, amount)
+    except KeyError:
+        return _error(404, "no billing account")
+    except ValueError as exc:
+        return _error(400, str(exc))
+    return _ok({"invoice": invoice_view(invoice)})
+
+
+def handle_usage(token: str | None) -> dict:
+    """Metered requests for the session's user, plus what overage would cost."""
+    session = auth.check_session(token)
+    if session is None:
+        return _error(401, "no session")
+    usage = billing.usage_of(session.username)
+    plan = billing.plan_for(session.username)
+    return _ok({
+        "username": usage.username,
+        "period": usage.period,
+        "requests": usage.requests,
+        "included": plan.included_requests,
+        "overage_cents": billing.overage_cents(usage.requests, plan),
+    })
+
+
+def invoice_view(invoice: Invoice) -> dict:
+    """A JSON-ready invoice. Handlers and tests both use this."""
+    return {
+        "username": invoice.username,
+        "period": invoice.period,
+        "base_cents": invoice.base_cents,
+        "overage_cents": invoice.overage_cents,
+        "total_cents": invoice.total_cents,
+    }
+
+
+def _ok(body: dict) -> dict:
+    return {"status": 200, "body": body}
+
+
+def _error(status: int, reason: str) -> dict:
+    return {"status": status, "body": {"error": reason}}
diff --git a/api/interfaces.py b/api/interfaces.py
--- /dev/null
+++ b/api/interfaces.py
@@ -0,0 +1,94 @@
+"""Shared types for the demo service.
+
+Other modules depend on these dataclasses and protocols. The path is
+`explicit`-protected (see `.gud/protected`): a push that rewrites it stays
+pending until the required approval is on the thread.
+"""
+
+from __future__ import annotations
+
+from dataclasses import dataclass
+from typing import Protocol
+
+
+@dataclass(frozen=True)
+class User:
+    """An account. `plan` names an entry in `billing.PLANS`."""
+
+    username: str
+    email: str
+    plan: str
+
+
+@dataclass(frozen=True)
+class Session:
+    """A login session. `token` is the secret the client sends back."""
+
+    token: str
+    username: str
+    created_at: float
+
+
+@dataclass(frozen=True)
+class LoginResult:
+    """Outcome of `login`. `session` is set only when `ok` is true."""
+
+    ok: bool
+    session: Session | None
+    reason: str
+
+
+@dataclass(frozen=True)
+class Plan:
+    """What a user pays, and how many requests are included before overage."""
+
+    name: str
+    monthly_cents: int
+    included_requests: int
+    overage_cents: int
+
+
+@dataclass(frozen=True)
+class Usage:
+    """Requests counted for one user in one `YYYY-MM` period."""
+
+    username: str
+    period: str
+    requests: int
+
+
+@dataclass(frozen=True)
+class Invoice:
+    """One charge. `overage_cents` is zero until billing adds it in `charge`."""
+
+    username: str
+    period: str
+    base_cents: int
+    overage_cents: int
+    total_cents: int
+
+
+class AuthBackend(Protocol):
+    """What `api.handlers` needs from the auth module."""
+
+    def login(self, username: str, password: str) -> LoginResult: ...
+
+    def check_session(self, token: str) -> Session | None: ...
+
+    def logout(self, token: str) -> bool: ...
+
+
+class BillingBackend(Protocol):
+    """What handlers need in order to record a charge and read usage."""
+
+    def charge(self, user: str, amount_cents: int) -> Invoice: ...
+
+    def usage_of(self, username: str) -> Usage: ...
+
+
+class RateLimiter(Protocol):
+    """Structural type of `ratelimit.TokenBucket`. Keys are per user or route."""
+
+    def allow(self, key: str) -> bool: ...
+
+    def remaining(self, key: str) -> int: ...
diff --git a/auth.py b/auth.py
--- /dev/null
+++ b/auth.py
@@ -0,0 +1,113 @@
+"""Users, password hashes, and sessions.
+
+Passwords are stored as PBKDF2-HMAC-SHA256. `login` checks the password and
+opens a session; it does not consult the rate limiter yet (see TASKS.md).
+"""
+
+from __future__ import annotations
+
+import hashlib
+import hmac
+import secrets
+import time
+
+import billing
+from api.interfaces import LoginResult, Session, User
+
+PBKDF2_ROUNDS = 12_000
+
+_users: dict[str, tuple[User, str]] = {}
+_sessions: dict[str, Session] = {}
+
+
+def reset() -> None:
+    """Drop every user and session. Tests call this between cases."""
+    _users.clear()
+    _sessions.clear()
+
+
+def hash_password(password: str, salt: bytes | None = None) -> str:
+    """Return `pbkdf2_sha256$rounds$salt_hex$digest_hex`."""
+    if not password:
+        raise ValueError("password is required")
+    salt = salt or secrets.token_bytes(16)
+    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
+    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"
+
+
+def verify_password(password: str, stored: str) -> bool:
+    """True when `password` matches a value from `hash_password`."""
+    try:
+        scheme, rounds_s, salt_hex, digest_hex = stored.split("$")
+        if scheme != "pbkdf2_sha256":
+            return False
+        rounds = int(rounds_s)
+        salt = bytes.fromhex(salt_hex)
+        expect = bytes.fromhex(digest_hex)
+    except (TypeError, ValueError):
+        return False
+    got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
+    return hmac.compare_digest(got, expect)
+
+
+def register_user(username: str, password: str, email: str, plan: str = "free") -> User:
+    """Create a user and open their billing account. Rejects duplicates."""
+    if not username or any(ch.isspace() for ch in username):
+        raise ValueError("username is required")
+    if "@" not in email:
+        raise ValueError("email must contain @")
+    if username in _users:
+        raise ValueError(f"user {username} already exists")
+    user = User(username=username, email=email, plan=plan)
+    _users[username] = (user, hash_password(password))
+    billing.open_account(username, plan)
+    return user
+
+
+def get_user(username: str) -> User | None:
+    row = _users.get(username)
+    return None if row is None else row[0]
+
+
+def start_session(username: str, now: float | None = None) -> Session:
+    """Issue a new token. The caller has already checked the password."""
+    if username not in _users:
+        raise KeyError(username)
+    session = Session(
+        token=secrets.token_urlsafe(24),
+        username=username,
+        created_at=time.time() if now is None else now,
+    )
+    _sessions[session.token] = session
+    return session
+
+
+def login(username: str, password: str) -> LoginResult:
+    """Check the password and start a session. Unknown user and bad password share one reason."""
+    row = _users.get(username)
+    if row is None or not verify_password(password, row[1]):
+        return LoginResult(False, None, "invalid credentials")
+    return LoginResult(True, start_session(username), "")
+
+
+def check_session(token: str | None) -> Session | None:
+    if not token:
+        return None
+    return _sessions.get(token)
+
+
+def list_sessions(username: str) -> list[Session]:
+    return [session for session in _sessions.values() if session.username == username]
+
+
+def logout(token: str) -> bool:
+    """Drop one session. False when the token was already gone."""
+    return _sessions.pop(token, None) is not None
+
+
+def revoke_user_sessions(username: str) -> int:
+    """Drop every session for `username`. Returns how many were removed."""
+    doomed = [token for token, session in _sessions.items() if session.username == username]
+    for token in doomed:
+        _sessions.pop(token, None)
+    return len(doomed)
diff --git a/billing.py b/billing.py
--- /dev/null
+++ b/billing.py
@@ -0,0 +1,94 @@
+"""Plans, request metering, and invoices.
+
+`overage_cents` knows what extra requests should cost. `charge` does not add
+that figure yet; it invoices only the amount the caller passes (see TASKS.md).
+"""
+
+from __future__ import annotations
+
+import time
+
+from api.interfaces import Invoice, Plan, Usage
+
+PLANS: dict[str, Plan] = {
+    "free": Plan("free", monthly_cents=0, included_requests=100, overage_cents=2),
+    "pro": Plan("pro", monthly_cents=2000, included_requests=10_000, overage_cents=1),
+}
+
+_accounts: dict[str, str] = {}
+_usage: dict[tuple[str, str], int] = {}
+_invoices: dict[str, list[Invoice]] = {}
+
+
+def reset() -> None:
+    """Drop accounts, meters, and invoices."""
+    _accounts.clear()
+    _usage.clear()
+    _invoices.clear()
+
+
+def open_account(username: str, plan: str = "free") -> Plan:
+    """Attach `username` to a plan. `register_user` calls this."""
+    if plan not in PLANS:
+        raise ValueError(f"unknown plan {plan}")
+    _accounts[username] = plan
+    return PLANS[plan]
+
+
+def plan_for(username: str) -> Plan:
+    """The user's plan, or `free` when they have no account row."""
+    return PLANS[_accounts.get(username, "free")]
+
+
+def current_period(when: float | None = None) -> str:
+    """UTC `YYYY-MM` for `when` (default: now)."""
+    stamp = time.gmtime(time.time() if when is None else when)
+    return f"{stamp.tm_year:04d}-{stamp.tm_mon:02d}"
+
+
+def meter_request(username: str, period: str | None = None, n: int = 1) -> Usage:
+    """Add `n` requests to the meter and return the new total."""
+    if n < 1:
+        raise ValueError("n must be >= 1")
+    period = period or current_period()
+    key = (username, period)
+    _usage[key] = _usage.get(key, 0) + n
+    return usage_of(username, period)
+
+
+def usage_of(username: str, period: str | None = None) -> Usage:
+    period = period or current_period()
+    return Usage(username, period, _usage.get((username, period), 0))
+
+
+def overage_cents(requests: int, plan: Plan) -> int:
+    """Cents owed for requests past `plan.included_requests`. Zero when inside the plan."""
+    extra = max(0, requests - plan.included_requests)
+    return extra * plan.overage_cents
+
+
+def charge(user: str, amount_cents: int, period: str | None = None) -> Invoice:
+    """Record an invoice for `amount_cents`. Overage is not included yet.
+
+    Raises `KeyError` when `user` has no account, `ValueError` when the amount is negative.
+    """
+    if amount_cents < 0:
+        raise ValueError("amount_cents must be >= 0")
+    if user not in _accounts:
+        raise KeyError(user)
+    period = period or current_period()
+    invoice = Invoice(
+        username=user, period=period, base_cents=amount_cents, overage_cents=0, total_cents=amount_cents,
+    )
+    _invoices.setdefault(user, []).append(invoice)
+    return invoice
+
+
+def list_invoices(user: str) -> list[Invoice]:
+    """Invoices for `user`, oldest first. Empty when they have never been charged."""
+    return list(_invoices.get(user, []))
+
+
+def outstanding_cents(user: str) -> int:
+    """Sum of `total_cents` across the user's invoices."""
+    return sum(invoice.total_cents for invoice in _invoices.get(user, []))
diff --git a/conftest.py b/conftest.py
--- /dev/null
+++ b/conftest.py
@@ -0,0 +1,47 @@
+"""Shared fixtures. Every test starts with empty auth, billing, and rate-limit stores."""
+
+from __future__ import annotations
+
+import pytest
+
+import auth
+import billing
+import ratelimit
+
+
+@pytest.fixture(autouse=True)
+def _empty_stores():
+    auth.reset()
+    billing.reset()
+    ratelimit.reset()
+    yield
+    auth.reset()
+    billing.reset()
+    ratelimit.reset()
+
+
+@pytest.fixture
+def alice():
+    """A user on the pro plan, registered and billed, with no session yet."""
+    return auth.register_user("alice", "correct horse", email="alice@example.com", plan="pro")
+
+
+@pytest.fixture
+def bob():
+    """A second user on the free plan, so tests can show sessions do not cross."""
+    return auth.register_user("bob", "battery staple", email="bob@example.com", plan="free")
+
+
+@pytest.fixture
+def alice_token(alice):
+    """A live session token for `alice`."""
+    result = auth.login("alice", "correct horse")
+    assert result.ok and result.session is not None
+    return result.session.token
+
+
+def login_token(username: str, password: str) -> str:
+    """Log `username` in and return the token. Fails the test if login does not."""
+    result = auth.login(username, password)
+    assert result.ok and result.session is not None
+    return result.session.token
diff --git a/pytest.ini b/pytest.ini
--- /dev/null
+++ b/pytest.ini
@@ -0,0 +1,3 @@
+[pytest]
+testpaths = tests
+pythonpath = .
diff --git a/ratelimit.py b/ratelimit.py
--- /dev/null
+++ b/ratelimit.py
@@ -0,0 +1,75 @@
+"""A token bucket keyed by user or route.
+
+`allow` spends tokens; they refill at `rate` per second up to `capacity`.
+Nothing in auth or billing calls this yet. The demo task wires it into `login`
+and adds a way to drain a key after abuse (see TASKS.md).
+"""
+
+from __future__ import annotations
+
+import time
+
+
+class TokenBucket:
+    """One bucket per key. Pass `now` in tests so refill does not depend on the clock."""
+
+    def __init__(self, rate: float, capacity: float):
+        if rate < 0 or capacity <= 0:
+            raise ValueError("rate must be >= 0 and capacity must be > 0")
+        self.rate = float(rate)
+        self.capacity = float(capacity)
+        self._tokens: dict[str, float] = {}
+        self._updated: dict[str, float] = {}
+
+    def allow(self, key: str, now: float | None = None, cost: float = 1.0) -> bool:
+        """Spend `cost` tokens for `key`. False when the bucket cannot afford it."""
+        if cost <= 0:
+            raise ValueError("cost must be > 0")
+        now = time.monotonic() if now is None else now
+        tokens = self._available(key, now)
+        if tokens < cost:
+            self._tokens[key] = tokens
+            self._updated[key] = now
+            return False
+        self._tokens[key] = tokens - cost
+        self._updated[key] = now
+        return True
+
+    def remaining(self, key: str, now: float | None = None) -> int:
+        """Whole tokens available right now. Does not spend any."""
+        now = time.monotonic() if now is None else now
+        return int(self._available(key, now))
+
+    def reset(self) -> None:
+        """Forget every key. The rate and capacity stay."""
+        self._tokens.clear()
+        self._updated.clear()
+
+    def _available(self, key: str, now: float) -> float:
+        if key not in self._tokens:
+            return self.capacity
+        elapsed = max(0.0, now - self._updated.get(key, now))
+        return min(self.capacity, self._tokens[key] + elapsed * self.rate)
+
+
+_default = TokenBucket(rate=10, capacity=10)
+
+
+def configure(rate: float, capacity: float) -> TokenBucket:
+    """Replace the process-wide bucket. Returns it."""
+    global _default
+    _default = TokenBucket(rate, capacity)
+    return _default
+
+
+def reset() -> None:
+    """Empty the process-wide bucket."""
+    _default.reset()
+
+
+def allow(key: str, now: float | None = None, cost: float = 1.0) -> bool:
+    return _default.allow(key, now=now, cost=cost)
+
+
+def remaining(key: str, now: float | None = None) -> int:
+    return _default.remaining(key, now=now)
diff --git a/tests/test_api.py b/tests/test_api.py
--- /dev/null
+++ b/tests/test_api.py
@@ -0,0 +1,79 @@
+"""Handlers, plus the token bucket they do not call yet."""
+
+from __future__ import annotations
+
+import api.handlers as handlers
+import auth
+import billing
+import ratelimit
+
+
+def test_login_whoami_and_logout(alice):
+    denied = handlers.handle_login({"username": "alice", "password": "no"})
+    assert denied["status"] == 401
+    assert denied["body"]["error"] == "invalid credentials"
+    ok = handlers.handle_login({"username": "alice", "password": "correct horse"})
+    assert ok["status"] == 200
+    token = ok["body"]["token"]
+    who = handlers.handle_whoami(token)
+    assert who["status"] == 200
+    assert who["body"]["username"] == "alice"
+    assert who["body"]["email"] == "alice@example.com"
+    assert who["body"]["plan"] == "pro"
+    assert handlers.handle_whoami(None)["status"] == 401
+    assert handlers.handle_whoami("nope")["status"] == 401
+    assert handlers.handle_logout(token)["status"] == 200
+    assert handlers.handle_whoami(token)["status"] == 401
+    assert handlers.handle_logout(token)["status"] == 401
+
+
+def test_sessions_do_not_cross(alice, bob):
+    alice_token = handlers.handle_login({"username": "alice", "password": "correct horse"})["body"]["token"]
+    bob_token = handlers.handle_login({"username": "bob", "password": "battery staple"})["body"]["token"]
+    assert handlers.handle_whoami(alice_token)["body"]["plan"] == "pro"
+    assert handlers.handle_whoami(bob_token)["body"]["plan"] == "free"
+    assert auth.check_session(alice_token).username == "alice"
+    assert auth.check_session(bob_token).username == "bob"
+
+
+def test_charge_and_usage(alice_token):
+    period = billing.current_period()
+    billing.meter_request("alice", period=period, n=4)
+    missing = handlers.handle_charge(None, {"amount_cents": 1})
+    assert missing["status"] == 401
+    bad = handlers.handle_charge(alice_token, {"amount_cents": "5"})
+    assert bad["status"] == 400
+    assert handlers.handle_charge(alice_token, {"amount_cents": True})["status"] == 400
+    charged = handlers.handle_charge(alice_token, {"amount_cents": 500})
+    assert charged["status"] == 200
+    invoice = charged["body"]["invoice"]
+    assert invoice["base_cents"] == 500
+    assert invoice["overage_cents"] == 0
+    assert invoice["total_cents"] == 500
+    usage = handlers.handle_usage(alice_token)
+    assert usage["status"] == 200
+    assert usage["body"]["requests"] == 4
+    assert usage["body"]["included"] == 10_000
+    assert usage["body"]["overage_cents"] == 0
+    assert handlers.handle_usage(None)["status"] == 401
+
+
+def test_token_bucket_refills_per_key():
+    bucket = ratelimit.TokenBucket(rate=1, capacity=2)
+    assert bucket.allow("alice", now=0)
+    assert bucket.allow("alice", now=0)
+    assert not bucket.allow("alice", now=0)
+    assert bucket.remaining("alice", now=0) == 0
+    assert bucket.allow("alice", now=1)
+    assert bucket.allow("bob", now=0)
+    assert bucket.remaining("bob", now=0) == 1
+
+
+def test_module_bucket_configure_and_reset():
+    ratelimit.configure(rate=1, capacity=1)
+    assert ratelimit.allow("k", now=10)
+    assert not ratelimit.allow("k", now=10)
+    assert ratelimit.remaining("k", now=10) == 0
+    ratelimit.reset()
+    assert ratelimit.allow("k", now=10)
+    assert ratelimit.remaining("other", now=10) == 1
diff --git a/tests/test_auth.py b/tests/test_auth.py
--- /dev/null
+++ b/tests/test_auth.py
@@ -0,0 +1,64 @@
+"""Auth store: hashing, registration, login, and sessions."""
+
+from __future__ import annotations
+
+import auth
+import billing
+
+
+def test_hash_password_roundtrip():
+    salt = b"0123456789abcdef"
+    stored = auth.hash_password("s3cret!", salt=salt)
+    assert stored.startswith(f"pbkdf2_sha256${auth.PBKDF2_ROUNDS}$")
+    assert auth.verify_password("s3cret!", stored)
+    assert not auth.verify_password("other", stored)
+    assert not auth.verify_password("s3cret!", "not-a-hash")
+
+
+def test_register_and_login(alice):
+    assert alice.username == "alice"
+    assert alice.plan == "pro"
+    assert billing.plan_for("alice").name == "pro"
+    result = auth.login("alice", "correct horse")
+    assert result.ok and result.session is not None
+    assert result.reason == ""
+    found = auth.check_session(result.session.token)
+    assert found is not None and found.username == "alice"
+    assert auth.get_user("alice") == alice
+
+
+def test_login_rejects_unknown_and_wrong_password_alike(alice):
+    bad = auth.login("alice", "nope")
+    missing = auth.login("nobody", "correct horse")
+    assert not bad.ok and bad.session is None
+    assert bad.reason == "invalid credentials"
+    assert missing.reason == "invalid credentials"
+    assert auth.check_session("") is None
+    assert auth.check_session(None) is None
+
+
+def test_logout_and_revoke(alice):
+    first = auth.login("alice", "correct horse")
+    second = auth.login("alice", "correct horse")
+    assert first.session and second.session
+    assert len(auth.list_sessions("alice")) == 2
+    assert auth.logout(first.session.token)
+    assert auth.check_session(first.session.token) is None
+    assert not auth.logout(first.session.token)
+    assert auth.revoke_user_sessions("alice") == 1
+    assert auth.list_sessions("alice") == []
+    assert auth.check_session(second.session.token) is None
+
+
+def test_duplicate_register_and_bad_email():
+    auth.register_user("carol", "pw", email="carol@example.com")
+    try:
+        auth.register_user("carol", "pw", email="carol@example.com")
+        raise AssertionError("duplicate register should fail")
+    except ValueError as exc:
+        assert "already exists" in str(exc)
+    try:
+        auth.register_user("dave", "pw", email="not-an-email")
+        raise AssertionError("bad email should fail")
+    except ValueError as exc:
+        assert "@" in str(exc)
diff --git a/tests/test_billing.py b/tests/test_billing.py
--- /dev/null
+++ b/tests/test_billing.py
@@ -0,0 +1,58 @@
+"""Plans, metering, overage math, and invoices."""
+
+from __future__ import annotations
+
+import pytest
+
+import billing
+
+
+def test_open_account_and_unknown_plan():
+    plan = billing.open_account("carol", "pro")
+    assert plan.monthly_cents == 2000
+    assert billing.plan_for("carol").included_requests == 10_000
+    assert billing.plan_for("stranger").name == "free"
+    with pytest.raises(ValueError):
+        billing.open_account("carol", "enterprise")
+
+
+def test_meter_is_per_period():
+    billing.open_account("carol", "free")
+    first = billing.meter_request("carol", period="2026-10", n=3)
+    assert first.requests == 3
+    assert billing.meter_request("carol", period="2026-10").requests == 4
+    other = billing.usage_of("carol", period="2026-11")
+    assert other.requests == 0
+    assert billing.current_period(1_700_000_000) == "2023-11"
+
+
+def test_overage_cents_inside_and_past_the_plan():
+    free = billing.PLANS["free"]
+    assert free.included_requests == 100
+    assert billing.overage_cents(100, free) == 0
+    assert billing.overage_cents(103, free) == 6
+    assert billing.overage_cents(0, billing.PLANS["pro"]) == 0
+
+
+def test_charge_records_the_amount_without_overage():
+    billing.open_account("carol", "free")
+    billing.meter_request("carol", period="2026-10", n=150)
+    invoice = billing.charge("carol", 250, period="2026-10")
+    assert invoice.base_cents == 250
+    assert invoice.overage_cents == 0
+    assert invoice.total_cents == 250
+    assert billing.list_invoices("carol") == [invoice]
+    assert billing.outstanding_cents("carol") == 250
+    again = billing.charge("carol", 0, period="2026-10")
+    assert billing.outstanding_cents("carol") == 250
+    assert again.total_cents == 0
+
+
+def test_charge_rejects_unknown_user_and_negative_amount():
+    with pytest.raises(KeyError):
+        billing.charge("ghost", 10, period="2026-10")
+    billing.open_account("carol")
+    with pytest.raises(ValueError):
+        billing.charge("carol", -1)
+    with pytest.raises(ValueError):
+        billing.meter_request("carol", n=0)
```
````

## 2. task — [gud] T1: Add per-user rate limiting to login and bill overage requests

- From: `console@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `lead@lean-ridge.primitive.email`, `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`
- Message id: `<primitive-direct-7be4d963-3ead-4500-a5cc-1fb061710874@lean-ridge.primitive.email>`
- Received at: 1792332012.526
- Sender verified: True

```text
Add per-user rate limiting to login, and bill overage requests. Split across auth.py, billing.py, api/handlers.py, and ratelimit.py.

Gud-Kind: task
Gud-Author: @console
Gud-Task: T1
Gud-Requester: @nelson
Gud-Sig: ed25519:ABQ+FCHEMzOltda7qakuOpp6zHUhnL1iz8BiYzAzXBvhVvCrHxyX6wBJrzqTBWQGbtUXB7LoKdDJ05UrGjMJAw==
```

## 3. notify — Review: probe bug 174238

- From: `lead@lean-ridge.primitive.email`
- To: `nelson@lean-ridge.primitive.email`
- Cc: `repo-swarm-20261018@lean-ridge.primitive.email`
- Message id: `<primitive-direct-9b3809df-eaea-4d7d-ac3d-1dd0c18c3ab6@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-9587ddbf-8494-4153-b03e-3855c5245304@blue-ivory.primitive.email>`
- Received at: 1792332020.271
- Sender verified: True

```text
probe

Reported by reporter@blue-ivory.primitive.email (<primitive-direct-9587ddbf-8494-4153-b03e-3855c5245304@blue-ivory.primitive.email>).

Reply approve or reject.

Gud-Kind: notify
Gud-Author: @lead
Gud-Ask: review
Gud-Target: <primitive-direct-9587ddbf-8494-4153-b03e-3855c5245304@blue-ivory.primitive.email>
```

## 4. notify — Review: Pricing page has no comparison table

- From: `lead@lean-ridge.primitive.email`
- To: `nelson@lean-ridge.primitive.email`
- Cc: `repo-swarm-20261018@lean-ridge.primitive.email`
- Message id: `<primitive-direct-4b190dfe-0d3a-47db-8b27-3f2e94b5bf08@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-58f1d559-e12e-4f65-afff-9baf080135b6@blue-ivory.primitive.email>`
- Received at: 1792332020.567
- Sender verified: True

```text
The pricing page shows cards. The spec asked for a comparison table with Free, Pro /mo, Team /user/mo.

Reported by reporter@blue-ivory.primitive.email (<primitive-direct-58f1d559-e12e-4f65-afff-9baf080135b6@blue-ivory.primitive.email>).

Reply approve or reject.

Gud-Kind: notify
Gud-Author: @lead
Gud-Ask: review
Gud-Target: <primitive-direct-58f1d559-e12e-4f65-afff-9baf080135b6@blue-ivory.primitive.email>
```

## 5. subtask — [gud] T1.1: Rate-limit login

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-1@lean-ridge.primitive.email`
- Message id: `<primitive-direct-e75adeb0-3b4e-41bf-b25b-f7fb6efc8c58@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-7be4d963-3ead-4500-a5cc-1fb061710874@lean-ridge.primitive.email>`
- Received at: 1792332020.605
- Sender verified: True

```text
In auth.py login, call ratelimit.allow(f"login:{username}") before checking the password. When it returns false, return LoginResult(False, None, "rate limited") and do not start a session. Add a test that a second login inside an empty bucket is rejected. Login now spends tokens from the process-wide bucket, and a test that calls ratelimit.configure replaces that bucket for every later test, so in conftest.py's autouse fixture call ratelimit.configure(rate=10, capacity=10) before each test.

Files: auth.py, tests/test_auth.py, conftest.py

Part of T1: Add per-user rate limiting to login and bill overage requests
Add per-user rate limiting to login, and bill overage requests. Split across auth.py, billing.py, api/handlers.py, and ratelimit.py.

Gud-Kind: subtask
Gud-Author: @lead
Gud-Task: T1.1
Gud-Parent-Task: T1
Gud-Assignee: @sub-1
Gud-Sig: ed25519:cmGB8G2bcOQ4Ozc+UuAAdiOeRBQeqB+vFOs2RA3Oy3G9g4vvePKkdDjoKaWOxUoNTeJLQYcIZoMztvReSKUWAQ==
```

## 6. subtask — [gud] T1.2: Bill overage in charge

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-2@lean-ridge.primitive.email`
- Message id: `<primitive-direct-d74d6791-8662-4a10-886b-160a3cf8ee37@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-7be4d963-3ead-4500-a5cc-1fb061710874@lean-ridge.primitive.email>`
- Received at: 1792332020.887
- Sender verified: True

```text
In billing.py charge, read usage_of for the period, set overage_cents from overage_cents(requests, plan), and set total_cents to amount_cents + overage_cents. Keep rejecting a missing account and a negative amount. A charge of 0 cents on the free plan after 103 requests in the period invoices overage_cents == 6. test_charge_records_the_amount_without_overage meters 150 requests and expects no overage; make it meter 10.

Files: billing.py, tests/test_billing.py

Part of T1: Add per-user rate limiting to login and bill overage requests
Add per-user rate limiting to login, and bill overage requests. Split across auth.py, billing.py, api/handlers.py, and ratelimit.py.

Gud-Kind: subtask
Gud-Author: @lead
Gud-Task: T1.2
Gud-Parent-Task: T1
Gud-Assignee: @sub-2
Gud-Sig: ed25519:Ll2tsaV4MojTZYDI8vm9483qnAhsptmk9W9Eyy3Vele/xQpA9IiCQuGTR+/VLRamyuLE2Yrh+FdWkiqCbHNWCA==
```

## 7. subtask — [gud] T1.3: Map 429 and meter charges

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-3@lean-ridge.primitive.email`
- Message id: `<primitive-direct-8123f15f-8e5e-45b8-954a-c27cd3f52811@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-7be4d963-3ead-4500-a5cc-1fb061710874@lean-ridge.primitive.email>`
- Received at: 1792332020.905
- Sender verified: True

```text
In api/handlers.py handle_login, map login reason "rate limited" to status 429. On a successful charge, call billing.meter_request for that user so the next invoice sees the call.

Files: api/handlers.py, tests/test_api.py

Part of T1: Add per-user rate limiting to login and bill overage requests
Add per-user rate limiting to login, and bill overage requests. Split across auth.py, billing.py, api/handlers.py, and ratelimit.py.

Gud-Kind: subtask
Gud-Author: @lead
Gud-Task: T1.3
Gud-Parent-Task: T1
Gud-Assignee: @sub-3
Gud-Sig: ed25519:3RAgZGGw8lXmEV2O3EDhumIDehpixgmWGyGfDw2bWwH3wBTjsog3xgf606zrDNq7AXCG/frBvrp9sJnCGMQeDg==
```

## 8. subtask — [gud] T1.4: Add penalize

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-4@lean-ridge.primitive.email`
- Message id: `<primitive-direct-62d0b4b9-7c14-43d2-96ee-75bb1addba4d@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-7be4d963-3ead-4500-a5cc-1fb061710874@lean-ridge.primitive.email>`
- Received at: 1792332022.167
- Sender verified: True

```text
In ratelimit.py add penalize(key) on TokenBucket and the module wrapper that sets that key's tokens to zero. Leave allow and remaining as the tests describe them. Add a test that penalize makes the next allow fail. Only add lines to ratelimit.py; do not change or delete any existing line, blank lines included.

Files: ratelimit.py, tests/test_penalize.py

Part of T1: Add per-user rate limiting to login and bill overage requests
Add per-user rate limiting to login, and bill overage requests. Split across auth.py, billing.py, api/handlers.py, and ratelimit.py.

Gud-Kind: subtask
Gud-Author: @lead
Gud-Task: T1.4
Gud-Parent-Task: T1
Gud-Assignee: @sub-4
Gud-Sig: ed25519:SWzZ32918IqpI+8E0XakAlBdNRYqZKvG1sdzYLufh2RYDSrgvxdnROjGta9OXUyGjWSEdwYk/wZiHAEmuF/+BA==
```

## 9. push — [gud] T1.1: Rate-limit login

- From: `sub-1@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>`
- Received at: 1792332022.293
- Sender verified: True

````text
T1.1 Rate-limit login

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-1
Gud-Parent: <primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>
Gud-Base: 3fbf6bc3826deb5a537ea62c3cecdee03ccd8ad348f9c4c5c379a308e9f57078
Gud-State: aed9196fc6c0196b8093de672ce8f12aa33db08bb2d238c3f73b54a94af41aef
Gud-Tree: 97a71845dc84a8e60a00b2baf71c3d712121944957ef746eda97de03269f9547
Gud-Task: T1.1
Gud-Sig: ed25519:pV68w6JNF7TOplRGBIu8xDS0ZAUhGz42a6XSLwEmhg2phOh2tOB/D9pUDTAmX1QaKHOMjAusOLNNJT2Ngn9tAQ==

```diff
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -12,6 +12,7 @@
 import time
 
 import billing
+import ratelimit
 from api.interfaces import LoginResult, Session, User
 
 PBKDF2_ROUNDS = 12_000
@@ -84,6 +85,8 @@
 
 def login(username: str, password: str) -> LoginResult:
     """Check the password and start a session. Unknown user and bad password share one reason."""
+    if not ratelimit.allow(f"login:{username}"):
+        return LoginResult(False, None, "rate limited")
     row = _users.get(username)
     if row is None or not verify_password(password, row[1]):
         return LoginResult(False, None, "invalid credentials")
diff --git a/tests/test_api.py b/tests/test_api.py
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -71,9 +71,13 @@
 
 def test_module_bucket_configure_and_reset():
     ratelimit.configure(rate=1, capacity=1)
-    assert ratelimit.allow("k", now=10)
-    assert not ratelimit.allow("k", now=10)
-    assert ratelimit.remaining("k", now=10) == 0
-    ratelimit.reset()
-    assert ratelimit.allow("k", now=10)
-    assert ratelimit.remaining("other", now=10) == 1
+    try:
+        assert ratelimit.allow("k", now=10)
+        assert not ratelimit.allow("k", now=10)
+        assert ratelimit.remaining("k", now=10) == 0
+        ratelimit.reset()
+        assert ratelimit.allow("k", now=10)
+        assert ratelimit.remaining("other", now=10) == 1
+    finally:
+        # login now spends tokens; put the process-wide bucket back for later tests.
+        ratelimit.configure(rate=10, capacity=10)
diff --git a/tests/test_auth.py b/tests/test_auth.py
--- a/tests/test_auth.py
+++ b/tests/test_auth.py
@@ -4,6 +4,7 @@
 
 import auth
 import billing
+import ratelimit
 
 
 def test_hash_password_roundtrip():
@@ -62,3 +63,14 @@
         raise AssertionError("bad email should fail")
     except ValueError as exc:
         assert "@" in str(exc)
+def test_second_login_inside_an_empty_bucket_is_rejected(alice):
+    ratelimit.configure(rate=0, capacity=1)
+    try:
+        first = auth.login("alice", "correct horse")
+        assert first.ok and first.session is not None
+        second = auth.login("alice", "correct horse")
+        assert not second.ok
+        assert second.session is None
+        assert second.reason == "rate limited"
+    finally:
+        ratelimit.configure(rate=10, capacity=10)
```
````

## 10. push — [gud] T1.4: Add penalize

- From: `sub-4@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-648341df-a1cd-4668-91d6-91e4c40f3fff@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>`
- Received at: 1792332022.424
- Sender verified: True

````text
T1.4 Add penalize

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-4
Gud-Parent: <primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>
Gud-Base: 3fbf6bc3826deb5a537ea62c3cecdee03ccd8ad348f9c4c5c379a308e9f57078
Gud-State: 451537ed2dd9dfb218f06d63fb51b76db4bfa5f4773158837416f3c7695fa7ec
Gud-Tree: fb842ae5363d31610b7dc3589ae6af72de6dfd0629b9b2c45b1a7e12b2e99c8e
Gud-Task: T1.4
Gud-Sig: ed25519:EnrXmEDFKtBGWleL65O8xR9Sr64f9GZgyGOFVpPl1Mv/gQJGq8SkXY/JBrpgycbSW3gNcokb1VzUW1IcRmlRAg==

```diff
diff --git a/ratelimit.py b/ratelimit.py
--- a/ratelimit.py
+++ b/ratelimit.py
@@ -73,3 +73,18 @@
 
 def remaining(key: str, now: float | None = None) -> int:
     return _default.remaining(key, now=now)
+
+def _penalize(self, key: str, now: float | None = None) -> None:
+    """Drain `key`. The next allow fails until the bucket refills."""
+    now = time.monotonic() if now is None else now
+    self._tokens[key] = 0.0
+    self._updated[key] = now
+
+
+if not hasattr(TokenBucket, "penalize"):
+    TokenBucket.penalize = _penalize
+
+
+def penalize(key: str, now: float | None = None) -> None:
+    """Set the process-wide bucket's tokens for `key` to zero."""
+    _default.penalize(key, now=now)
diff --git a/tests/test_penalize.py b/tests/test_penalize.py
--- /dev/null
+++ b/tests/test_penalize.py
@@ -0,0 +1,19 @@
+"""penalize drains a key so the next allow fails."""
+
+from __future__ import annotations
+
+import ratelimit
+
+
+def test_penalize_makes_the_next_allow_fail():
+    bucket = ratelimit.TokenBucket(rate=0, capacity=2)
+    assert bucket.allow("alice", now=0)
+    bucket.penalize("alice", now=0)
+    assert bucket.allow("alice", now=0) is False
+    ratelimit.configure(rate=0, capacity=2)
+    try:
+        assert ratelimit.allow("k", now=0)
+        ratelimit.penalize("k", now=0)
+        assert ratelimit.allow("k", now=0) is False
+    finally:
+        ratelimit.configure(rate=10, capacity=10)
```
````

## 11. test — Re: [gud] T1.1: Rate-limit login

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-1@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-70ccd245-9658-49ca-aafc-f4b8dbacae31@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>`
- Received at: 1792332023.421
- Sender verified: True

```text
................                                                         [100%]
16 passed in 0.11s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 97a71845dc84a8e60a00b2baf71c3d712121944957ef746eda97de03269f9547
Gud-Target: <primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:l0iIBZvCw7wBOUYNs+f5t723DBKjSVscKDeZQOhr5PlUbkMGE4mVoY/7jPMooAYGutlBdkIBXGxofIBtz4GzDQ==
```

## 12. push — [gud] T1.2: Bill overage in charge

- From: `sub-2@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-b7a273ae-9f14-4ff6-a0ab-26ecd8d6d669@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>`
- Received at: 1792332023.455
- Sender verified: True

````text
T1.2 Bill overage in charge

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-2
Gud-Parent: <primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>
Gud-Base: 3fbf6bc3826deb5a537ea62c3cecdee03ccd8ad348f9c4c5c379a308e9f57078
Gud-State: 5fa7da15c111a249fa26b7081b5f58aafea054bd1a8fb98d891a9975f5de3b21
Gud-Tree: 656b1216b46a5e2e280e103b28ac8b1a0f7b4c845b55e005d2c68c4c86bea06f
Gud-Task: T1.2
Gud-Sig: ed25519:qpGQqLqrEmSPku5bq3WKK5YHFwcwT28jEZck75P8pagkeoKne4P8kc/l+U8aoueO4TauGWKMif9PJIn5dV6SAg==

```diff
diff --git a/billing.py b/billing.py
--- a/billing.py
+++ b/billing.py
@@ -77,8 +77,12 @@
     if user not in _accounts:
         raise KeyError(user)
     period = period or current_period()
+    usage = usage_of(user, period)
+    plan = plan_for(user)
+    extra = overage_cents(usage.requests, plan)
     invoice = Invoice(
-        username=user, period=period, base_cents=amount_cents, overage_cents=0, total_cents=amount_cents,
+        username=user, period=period, base_cents=amount_cents, overage_cents=extra,
+        total_cents=amount_cents + extra,
     )
     _invoices.setdefault(user, []).append(invoice)
     return invoice
diff --git a/tests/test_billing.py b/tests/test_billing.py
--- a/tests/test_billing.py
+++ b/tests/test_billing.py
@@ -36,7 +36,7 @@
 
 def test_charge_records_the_amount_without_overage():
     billing.open_account("carol", "free")
-    billing.meter_request("carol", period="2026-10", n=150)
+    billing.meter_request("carol", period="2026-10", n=10)
     invoice = billing.charge("carol", 250, period="2026-10")
     assert invoice.base_cents == 250
     assert invoice.overage_cents == 0
@@ -56,3 +56,10 @@
         billing.charge("carol", -1)
     with pytest.raises(ValueError):
         billing.meter_request("carol", n=0)
+def test_charge_of_zero_on_the_free_plan_bills_overage():
+    billing.open_account("carol", "free")
+    billing.meter_request("carol", period="2026-10", n=103)
+    invoice = billing.charge("carol", 0, period="2026-10")
+    assert invoice.base_cents == 0
+    assert invoice.overage_cents == 6
+    assert invoice.total_cents == 6
```
````

## 13. push — [gud] T1.3: Map 429 and meter charges

- From: `sub-3@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-c4c76b88-da9c-438c-897d-37c2917a2174@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>`
- Received at: 1792332023.528
- Sender verified: True

````text
T1.3 Map 429 and meter charges

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-3
Gud-Parent: <primitive-direct-311a48c8-24a5-449d-991d-0b87b095419a@lean-ridge.primitive.email>
Gud-Base: 3fbf6bc3826deb5a537ea62c3cecdee03ccd8ad348f9c4c5c379a308e9f57078
Gud-State: 4447f1815b62b0418b7fc321f9f91e1e1f0b96fde946d377e72815f3eef814d1
Gud-Tree: 7a5207a43e3a635762173b2320bf215c8a8a08e82cb2426d12faf3f86195d243
Gud-Task: T1.3
Gud-Sig: ed25519:8SEFjWcV0Rlel8hnyv3U2Rsk2nDbdKlBipD+7rDCn1ZWGdM9WMsjXdALPHKeJnX9dRPmrWfOLVVpyJiIjgaTDg==

```diff
diff --git a/api/handlers.py b/api/handlers.py
--- a/api/handlers.py
+++ b/api/handlers.py
@@ -17,7 +17,8 @@
     password = str(payload.get("password") or "")
     result = auth.login(username, password)
     if not result.ok or result.session is None:
-        return _error(401, result.reason or "invalid credentials")
+        status = 429 if result.reason == "rate limited" else 401
+        return _error(status, result.reason or "invalid credentials")
     return _ok({"token": result.session.token, "username": result.session.username})
 
 
@@ -55,6 +56,7 @@
         return _error(404, "no billing account")
     except ValueError as exc:
         return _error(400, str(exc))
+    billing.meter_request(session.username)
     return _ok({"invoice": invoice_view(invoice)})
 
 
diff --git a/tests/test_api.py b/tests/test_api.py
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -52,7 +52,7 @@
     assert invoice["total_cents"] == 500
     usage = handlers.handle_usage(alice_token)
     assert usage["status"] == 200
-    assert usage["body"]["requests"] == 4
+    assert usage["body"]["requests"] == 5
     assert usage["body"]["included"] == 10_000
     assert usage["body"]["overage_cents"] == 0
     assert handlers.handle_usage(None)["status"] == 401
```
````

## 14. approve — Re: [gud] T1.1: Rate-limit login

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-1@lean-ridge.primitive.email`
- Message id: `<primitive-direct-a669082a-b4f9-4247-9fba-9f7c27f7ead6@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>`
- Received at: 1792332024.062
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @lead
Gud-Target: <primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>
Gud-Sig: ed25519:2p7uvEZz7ztMtPHP+8B5/G6QiaukV9Ypf8CfgNVkH1DFdXx0VHms9GgOzKbnm4cmaHVHJ9BMqxCMM8AAZSESBA==
```

## 15. approve — Re: [gud] T1.2: Bill overage in charge

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-2@lean-ridge.primitive.email`
- Message id: `<primitive-direct-687592f8-ea08-4e0a-bc9e-3745227bfcbb@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-b7a273ae-9f14-4ff6-a0ab-26ecd8d6d669@lean-ridge.primitive.email>`
- Received at: 1792332024.261
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @lead
Gud-Target: <primitive-direct-b7a273ae-9f14-4ff6-a0ab-26ecd8d6d669@lean-ridge.primitive.email>
Gud-Sig: ed25519:B6HWJnKx185naIkR5gnzQNP6WnUxhrCHRS0I623LchPnXZX72AwT21Z1hMWCixgZtVfMjmPyrP3Ir1/adH+dBQ==
```

## 16. approve — Re: [gud] T1.3: Map 429 and meter charges

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-3@lean-ridge.primitive.email`
- Message id: `<primitive-direct-803cfe79-01a8-45d2-b1c6-a6d231355dc8@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-c4c76b88-da9c-438c-897d-37c2917a2174@lean-ridge.primitive.email>`
- Received at: 1792332024.589
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @lead
Gud-Target: <primitive-direct-c4c76b88-da9c-438c-897d-37c2917a2174@lean-ridge.primitive.email>
Gud-Sig: ed25519:XF2ZxuTpk4jdC4ppqLgi/LDyWeFXoRNqCM0nSS7z/BAm3pNE5nUCNgJN4CvilTlu7mcP9RI1SQ6yf7NQrgVRAA==
```

## 17. test — Re: [gud] T1.4: Add penalize

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-4@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-c42d15dc-1e87-4b7e-bb9e-b336e7b0175b@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-648341df-a1cd-4668-91d6-91e4c40f3fff@lean-ridge.primitive.email>`
- Received at: 1792332025.434
- Sender verified: True

```text
................                                                         [100%]
16 passed in 0.19s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: fb842ae5363d31610b7dc3589ae6af72de6dfd0629b9b2c45b1a7e12b2e99c8e
Gud-Target: <primitive-direct-648341df-a1cd-4668-91d6-91e4c40f3fff@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:NBubdsLh9FmM/O9lgzBIRRZVu7+wQgrIGDpDTM4YfY70K9HDKlRjG+RuaLZJedX0iFf2VJN6fEqwMaJzZZv7Dw==
```

## 18. test — Re: [gud] T1.2: Bill overage in charge

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-2@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-4f904474-c53e-4a2d-b766-2dc1b737cd4d@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-b7a273ae-9f14-4ff6-a0ab-26ecd8d6d669@lean-ridge.primitive.email>`
- Received at: 1792332025.56
- Sender verified: True

```text
................                                                         [100%]
16 passed in 0.19s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 656b1216b46a5e2e280e103b28ac8b1a0f7b4c845b55e005d2c68c4c86bea06f
Gud-Target: <primitive-direct-b7a273ae-9f14-4ff6-a0ab-26ecd8d6d669@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:wF73ruhe1YcCbuMWlkmTZuUXOVkqnygo0rK5qibNIuhzMrbgcA14PP+cFrGt+0Ojs0mDjnv5mDh+vSIadUwABw==
```

## 19. push — [gud] T1.2: Bill overage in charge

- From: `sub-2@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>`
- Received at: 1792332026.167
- Sender verified: True

````text
T1.2 Bill overage in charge

rebased onto the new tip

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-2
Gud-Parent: <primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>
Gud-Base: aed9196fc6c0196b8093de672ce8f12aa33db08bb2d238c3f73b54a94af41aef
Gud-State: 7f689ef56768a6e1889616011541d3a7bf105fef576e67e1bee21e738a01d0d0
Gud-Tree: d6db0d0704420b40cb40cfa116dc4da4593849fb71bf729e4ea1e5fb02778163
Gud-Task: T1.2
Gud-Sig: ed25519:GK8JfzvLH65a+IQRr3ZjPV4pKmP2VtZ6BDsHDagWtT4N/0awCnZWeFtjA/DxkJDUAeXKGZPcIq3xk9wmMy7AAQ==

```diff
diff --git a/billing.py b/billing.py
--- a/billing.py
+++ b/billing.py
@@ -77,8 +77,12 @@
     if user not in _accounts:
         raise KeyError(user)
     period = period or current_period()
+    usage = usage_of(user, period)
+    plan = plan_for(user)
+    extra = overage_cents(usage.requests, plan)
     invoice = Invoice(
-        username=user, period=period, base_cents=amount_cents, overage_cents=0, total_cents=amount_cents,
+        username=user, period=period, base_cents=amount_cents, overage_cents=extra,
+        total_cents=amount_cents + extra,
     )
     _invoices.setdefault(user, []).append(invoice)
     return invoice
diff --git a/tests/test_billing.py b/tests/test_billing.py
--- a/tests/test_billing.py
+++ b/tests/test_billing.py
@@ -36,7 +36,7 @@
 
 def test_charge_records_the_amount_without_overage():
     billing.open_account("carol", "free")
-    billing.meter_request("carol", period="2026-10", n=150)
+    billing.meter_request("carol", period="2026-10", n=10)
     invoice = billing.charge("carol", 250, period="2026-10")
     assert invoice.base_cents == 250
     assert invoice.overage_cents == 0
@@ -56,3 +56,10 @@
         billing.charge("carol", -1)
     with pytest.raises(ValueError):
         billing.meter_request("carol", n=0)
+def test_charge_of_zero_on_the_free_plan_bills_overage():
+    billing.open_account("carol", "free")
+    billing.meter_request("carol", period="2026-10", n=103)
+    invoice = billing.charge("carol", 0, period="2026-10")
+    assert invoice.base_cents == 0
+    assert invoice.overage_cents == 6
+    assert invoice.total_cents == 6
```
````

## 20. test — Re: [gud] T1.3: Map 429 and meter charges

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-3@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-273089f2-3569-459f-b012-f79571f69c6e@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-c4c76b88-da9c-438c-897d-37c2917a2174@lean-ridge.primitive.email>`
- Received at: 1792332026.212
- Sender verified: True

```text
...............                                                          [100%]
15 passed in 0.19s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 7a5207a43e3a635762173b2320bf215c8a8a08e82cb2426d12faf3f86195d243
Gud-Target: <primitive-direct-c4c76b88-da9c-438c-897d-37c2917a2174@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:ZucgHxX5syskhQJtSQiCK2P88Qc7ejuvWcaMfbjxwxtPhTdtQa0i0wc3W9Wrg3kxu651ZcIrHqtTmh80mBvVDA==
```

## 21. approve — Re: [gud] T1.2: Bill overage in charge

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-2@lean-ridge.primitive.email`
- Message id: `<primitive-direct-00c0fc6c-5f5b-4e37-9119-a41ebd8be70c@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>`
- Received at: 1792332027.316
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @lead
Gud-Target: <primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>
Gud-Sig: ed25519:bGvNCHkfsGBukay0byP1Il2Ne1S8ua6c9Fxn9/xUjxLZTJbpeNXkgKecbc7t8NikfnM/FZKS1Dh+Z3fd5UikCg==
```

## 22. push — [gud] T1.4: Add penalize

- From: `sub-4@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-f49fb14c-e8d7-4878-84fc-3e5605f6f92c@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>`
- Received at: 1792332027.625
- Sender verified: True

````text
T1.4 Add penalize

rebased onto the new tip

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-4
Gud-Parent: <primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>
Gud-Base: aed9196fc6c0196b8093de672ce8f12aa33db08bb2d238c3f73b54a94af41aef
Gud-State: a577c64c1567dbe2c2b2ff4a0883fb37563c10ee3e4dfd044f472e459fc2b3a0
Gud-Tree: 50d05edfaef6c3b95345f91c9520fcaf5201324e06d213c4f5702ee625d44629
Gud-Task: T1.4
Gud-Sig: ed25519:KZxkpkiiESQhKJo3F4xCLZ4K/lnGMtycav06Vhzm7KIwhY+oF7bTh4CMkfx6AwfnEx0U3pGuIuDoLpR5O8GFDQ==

```diff
diff --git a/ratelimit.py b/ratelimit.py
--- a/ratelimit.py
+++ b/ratelimit.py
@@ -73,3 +73,18 @@
 
 def remaining(key: str, now: float | None = None) -> int:
     return _default.remaining(key, now=now)
+
+def _penalize(self, key: str, now: float | None = None) -> None:
+    """Drain `key`. The next allow fails until the bucket refills."""
+    now = time.monotonic() if now is None else now
+    self._tokens[key] = 0.0
+    self._updated[key] = now
+
+
+if not hasattr(TokenBucket, "penalize"):
+    TokenBucket.penalize = _penalize
+
+
+def penalize(key: str, now: float | None = None) -> None:
+    """Set the process-wide bucket's tokens for `key` to zero."""
+    _default.penalize(key, now=now)
diff --git a/tests/test_penalize.py b/tests/test_penalize.py
--- /dev/null
+++ b/tests/test_penalize.py
@@ -0,0 +1,19 @@
+"""penalize drains a key so the next allow fails."""
+
+from __future__ import annotations
+
+import ratelimit
+
+
+def test_penalize_makes_the_next_allow_fail():
+    bucket = ratelimit.TokenBucket(rate=0, capacity=2)
+    assert bucket.allow("alice", now=0)
+    bucket.penalize("alice", now=0)
+    assert bucket.allow("alice", now=0) is False
+    ratelimit.configure(rate=0, capacity=2)
+    try:
+        assert ratelimit.allow("k", now=0)
+        ratelimit.penalize("k", now=0)
+        assert ratelimit.allow("k", now=0) is False
+    finally:
+        ratelimit.configure(rate=10, capacity=10)
```
````

## 23. test — Re: [gud] T1.2: Bill overage in charge

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-2@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-10b40e64-d4aa-449d-a2e9-b6214077fb50@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>`
- Received at: 1792332028.804
- Sender verified: True

```text
.................                                                        [100%]
17 passed in 0.11s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: d6db0d0704420b40cb40cfa116dc4da4593849fb71bf729e4ea1e5fb02778163
Gud-Target: <primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:l+kUXHaqoSnVn35mc7jAFA0rOSiRCgH3CjrQE/hhxwi5FD0nBto+hVR6jW+vJDzASm5Zi5n8vOrAu5t8mxjfAw==
```

## 24. test — Re: [gud] T1.4: Add penalize

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-4@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-d3296f3c-b760-43db-bf4e-fd720ccb3ec0@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-f49fb14c-e8d7-4878-84fc-3e5605f6f92c@lean-ridge.primitive.email>`
- Received at: 1792332030.155
- Sender verified: True

```text
.................                                                        [100%]
17 passed in 0.12s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 50d05edfaef6c3b95345f91c9520fcaf5201324e06d213c4f5702ee625d44629
Gud-Target: <primitive-direct-f49fb14c-e8d7-4878-84fc-3e5605f6f92c@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:Gom/AEdJmuYy9CKjM62dbac4/GMXCuNodFJ1g2crupLOlIaprVH3TX9g6ETYCoaXkmlTb2P5Wp/MtSTm0/6DDw==
```

## 25. push — [gud] T1.4: Add penalize

- From: `sub-4@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>`
- Received at: 1792332030.726
- Sender verified: True

````text
T1.4 Add penalize

rebased onto the new tip

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-4
Gud-Parent: <primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>
Gud-Base: 7f689ef56768a6e1889616011541d3a7bf105fef576e67e1bee21e738a01d0d0
Gud-State: 157f6eb66ed3c7b3f9204351402588ce37a679a14bb5378fb8fa5d5fde0da9d6
Gud-Tree: d4d76725ae31729e31feb5632362e03b2870f8168e0a61d3dc3d314f52db2936
Gud-Task: T1.4
Gud-Sig: ed25519:pVTi2frLr9I8u74pyy/Bzdzvl0pJ9+nVbr0u1vTGk89IBblPl1bW0208qcoc+dckZC+mCcG6GSOXp5BLPBK9Bg==

```diff
diff --git a/ratelimit.py b/ratelimit.py
--- a/ratelimit.py
+++ b/ratelimit.py
@@ -73,3 +73,18 @@
 
 def remaining(key: str, now: float | None = None) -> int:
     return _default.remaining(key, now=now)
+
+def _penalize(self, key: str, now: float | None = None) -> None:
+    """Drain `key`. The next allow fails until the bucket refills."""
+    now = time.monotonic() if now is None else now
+    self._tokens[key] = 0.0
+    self._updated[key] = now
+
+
+if not hasattr(TokenBucket, "penalize"):
+    TokenBucket.penalize = _penalize
+
+
+def penalize(key: str, now: float | None = None) -> None:
+    """Set the process-wide bucket's tokens for `key` to zero."""
+    _default.penalize(key, now=now)
diff --git a/tests/test_penalize.py b/tests/test_penalize.py
--- /dev/null
+++ b/tests/test_penalize.py
@@ -0,0 +1,19 @@
+"""penalize drains a key so the next allow fails."""
+
+from __future__ import annotations
+
+import ratelimit
+
+
+def test_penalize_makes_the_next_allow_fail():
+    bucket = ratelimit.TokenBucket(rate=0, capacity=2)
+    assert bucket.allow("alice", now=0)
+    bucket.penalize("alice", now=0)
+    assert bucket.allow("alice", now=0) is False
+    ratelimit.configure(rate=0, capacity=2)
+    try:
+        assert ratelimit.allow("k", now=0)
+        ratelimit.penalize("k", now=0)
+        assert ratelimit.allow("k", now=0) is False
+    finally:
+        ratelimit.configure(rate=10, capacity=10)
```
````

## 26. push — [gud] T1.3: Map 429 and meter charges

- From: `sub-3@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-f128ac68-dda2-49d0-9834-6e9a272aaece@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>`
- Received at: 1792332031.133
- Sender verified: True

````text
T1.3 Map 429 and meter charges

rebased onto the new tip

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-3
Gud-Parent: <primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>
Gud-Base: 7f689ef56768a6e1889616011541d3a7bf105fef576e67e1bee21e738a01d0d0
Gud-State: bac95f8e623e63e81d0f7c3519946c978016a99f810d2d6b1c615c5bc9cd40fa
Gud-Tree: 6344991df58a91879a66097abbedf990defb95f593c431c9c7d735e69d68d9cc
Gud-Task: T1.3
Gud-Sig: ed25519:KpcPg9p0HXFYx5hPGKS5yQkI7ScyGjj5JEATcdoYJrl+yqwbS8XAHMk2jM+f8QFFZwuakXSYxbAAd1lFYcvKCw==

```diff
diff --git a/api/handlers.py b/api/handlers.py
--- a/api/handlers.py
+++ b/api/handlers.py
@@ -17,7 +17,8 @@
     password = str(payload.get("password") or "")
     result = auth.login(username, password)
     if not result.ok or result.session is None:
-        return _error(401, result.reason or "invalid credentials")
+        status = 429 if result.reason == "rate limited" else 401
+        return _error(status, result.reason or "invalid credentials")
     return _ok({"token": result.session.token, "username": result.session.username})
 
 
@@ -55,6 +56,7 @@
         return _error(404, "no billing account")
     except ValueError as exc:
         return _error(400, str(exc))
+    billing.meter_request(session.username)
     return _ok({"invoice": invoice_view(invoice)})
 
 
diff --git a/tests/test_api.py b/tests/test_api.py
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -52,7 +52,7 @@
     assert invoice["total_cents"] == 500
     usage = handlers.handle_usage(alice_token)
     assert usage["status"] == 200
-    assert usage["body"]["requests"] == 4
+    assert usage["body"]["requests"] == 5
     assert usage["body"]["included"] == 10_000
     assert usage["body"]["overage_cents"] == 0
     assert handlers.handle_usage(None)["status"] == 401
```
````

## 27. test — Re: [gud] T1.4: Add penalize

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-4@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-acbf4885-8159-4865-8db1-bbea506d3cda@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>`
- Received at: 1792332032.442
- Sender verified: True

```text
..................                                                       [100%]
18 passed in 0.12s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: d4d76725ae31729e31feb5632362e03b2870f8168e0a61d3dc3d314f52db2936
Gud-Target: <primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:qpQgaIX7aaRSwqVkPBneX/5JdU8qpfAwnZk4uKakrUtnUrfPZHh4gjgpWHOuSH91wE3uIExPi4ExMTRdyroEDg==
```

## 28. task — [gud] T2: Add a login lockout

- From: `console@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `lead@lean-ridge.primitive.email`, `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`
- Message id: `<primitive-direct-12dc1f1d-2dd7-4b35-b6cf-3462775dac36@lean-ridge.primitive.email>`
- Received at: 1792332033.856
- Sender verified: True

```text
Five wrong passwords for one username lock the account until revoke_user_sessions clears it.

Gud-Kind: task
Gud-Author: @console
Gud-Task: T2
Gud-Requester: @nelson
Gud-Sig: ed25519:mB/XYfIv1qUnIdPZoa69oUqpw1/zIIp1J9Abgf9qZwpItyTL52nkvQZllHsZ9Vy57E3fXaImxJ4jukkul6l/CA==
```

## 29. subtask — [gud] T2.1: Add a lockout counter to login

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-34d24637-cc1f-4297-bb84-2d4a9051d42a@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-12dc1f1d-2dd7-4b35-b6cf-3462775dac36@lean-ridge.primitive.email>`
- Received at: 1792332034.643
- Sender verified: True

```text
gud-conflict. Five wrong passwords for one username lock the account: later login calls return LoginResult(False, None, "locked") until revoke_user_sessions clears the count. Keep the counter in a module-level _failures dict and clear it in reset. Add a test that five bad passwords lock alice and revoke_user_sessions unlocks her.

Target auth.login:

def login(username: str, password: str) -> LoginResult:
    if not ratelimit.allow("login"):
        return LoginResult(False, None, "rate limited")
    if _failures.get(username, 0) >= 5:
        return LoginResult(False, None, "locked")
    row = _users.get(username)
    if row is None or not verify_password(password, row[1]):
        _failures[username] = _failures.get(username, 0) + 1
        return LoginResult(False, None, "invalid credentials")
    _failures.pop(username, None)
    return LoginResult(True, start_session(username), "")

Files: auth.py, tests/test_auth.py

Part of T2: Add a login lockout
Five wrong passwords for one username lock the account until revoke_user_sessions clears it.

Gud-Kind: subtask
Gud-Author: @lead
Gud-Task: T2.1
Gud-Parent-Task: T2
Gud-Assignee: @auth-agent
Gud-Sig: ed25519:tDKcsHeRZJbFC/EItX5bceyxmNoGH66aufmrbM8hesQVnw/PoeemH4eZiVSwUeUmlbkUIVYyShbNpI5tqSYWBw==
```

## 30. push — [gud] T2.1: Add a lockout counter to login

- From: `auth-agent@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`
- Message id: `<primitive-direct-ecc243f7-d6b1-4ff3-a098-369c0a8e9869@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>`
- Received at: 1792332035.463
- Sender verified: True

````text
T2.1 Add a lockout counter to login

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @auth-agent
Gud-Parent: <primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>
Gud-Base: 157f6eb66ed3c7b3f9204351402588ce37a679a14bb5378fb8fa5d5fde0da9d6
Gud-State: d0126804b64719fb6717bdcead9e51db3f08a9eff1a3f17ac9574a710869d0fc
Gud-Tree: 596f0a53686f571154661ca783a8591743f7742afcab4ed995996cd4e795ebce
Gud-Task: T2.1
Gud-Sig: ed25519:C+0Zcq66g4CFBl/n5iyCykH8m9AsJ3Y2JEY1jrOPwHHqPvE34DIh+LsGEGXHA9F4uNVuaVlrtv2/BT1odyLaBA==

```diff
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -19,12 +19,14 @@
 
 _users: dict[str, tuple[User, str]] = {}
 _sessions: dict[str, Session] = {}
+_failures: dict[str, int] = {}
 
 
 def reset() -> None:
     """Drop every user and session. Tests call this between cases."""
     _users.clear()
     _sessions.clear()
+    _failures.clear()
 
 
 def hash_password(password: str, salt: bytes | None = None) -> str:
@@ -85,13 +87,17 @@
 
 def login(username: str, password: str) -> LoginResult:
     """Check the password and start a session. Unknown user and bad password share one reason."""
-    if not ratelimit.allow(f"login:{username}"):
+    # gud-bad-edit: lockout replaces the login flow
+    if not ratelimit.allow("login"):
         return LoginResult(False, None, "rate limited")
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
@@ -110,6 +116,7 @@
 
 def revoke_user_sessions(username: str) -> int:
     """Drop every session for `username`. Returns how many were removed."""
+    _failures.pop(username, None)
     doomed = [token for token, session in _sessions.items() if session.username == username]
     for token in doomed:
         _sessions.pop(token, None)
```
````

## 31. object — Re: [gud] T2.1: Add a lockout counter to login

- From: `sub-1@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-36977f43-eb5f-4fa3-9a0a-2e56c0bf6a9f@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-ecc243f7-d6b1-4ff3-a098-369c0a8e9869@lean-ridge.primitive.email>`
- Received at: 1792332035.832
- Sender verified: True

```text
keep the rate-limit check per user, ratelimit.allow(f"login:{username}"); one shared key lets one client rate-limit every user

Gud-Kind: object
Gud-Author: @sub-1
Gud-Target: <primitive-direct-ecc243f7-d6b1-4ff3-a098-369c0a8e9869@lean-ridge.primitive.email>
Gud-Reason: keep the rate-limit check per user, ratelimit.allow(f"login:{username}"); one shared key lets one client rate-limit every user
Gud-Sig: ed25519:Pj8tYfSB6xMu+gB3BNQO9wr8vMCGM7GxUltDwvoUDSvaBepAjjuwuvlLg/lPmAzw56MMd60F+6NMx8+EFPqMCQ==
```

## 32. push — [gud] T1.3: Map 429 and meter charges

- From: `sub-3@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>`
- Received at: 1792332036.531
- Sender verified: True

````text
T1.3 Map 429 and meter charges

rebased onto the new tip

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @sub-3
Gud-Parent: <primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>
Gud-Base: 157f6eb66ed3c7b3f9204351402588ce37a679a14bb5378fb8fa5d5fde0da9d6
Gud-State: 8082273bccebdcd9553a20ad34a59ae3d3d9726130529792c488d76a0dbeb07a
Gud-Tree: 8fb316aa67f252307d82a7e7baa15fe3226a1ab977243ec21010ae614a4b810b
Gud-Task: T1.3
Gud-Sig: ed25519:ox4FqP+FTEz0NWYqlXeF698FZqW5CuZvgDqiAI1FPNviLUoZ0vP1omPHxWXl80q9llyT/CYUpKP554+qHm/wBA==

```diff
diff --git a/api/handlers.py b/api/handlers.py
--- a/api/handlers.py
+++ b/api/handlers.py
@@ -17,7 +17,8 @@
     password = str(payload.get("password") or "")
     result = auth.login(username, password)
     if not result.ok or result.session is None:
-        return _error(401, result.reason or "invalid credentials")
+        status = 429 if result.reason == "rate limited" else 401
+        return _error(status, result.reason or "invalid credentials")
     return _ok({"token": result.session.token, "username": result.session.username})
 
 
@@ -55,6 +56,7 @@
         return _error(404, "no billing account")
     except ValueError as exc:
         return _error(400, str(exc))
+    billing.meter_request(session.username)
     return _ok({"invoice": invoice_view(invoice)})
 
 
diff --git a/tests/test_api.py b/tests/test_api.py
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -52,7 +52,7 @@
     assert invoice["total_cents"] == 500
     usage = handlers.handle_usage(alice_token)
     assert usage["status"] == 200
-    assert usage["body"]["requests"] == 4
+    assert usage["body"]["requests"] == 5
     assert usage["body"]["included"] == 10_000
     assert usage["body"]["overage_cents"] == 0
     assert handlers.handle_usage(None)["status"] == 401
```
````

## 33. test — Re: [gud] T2.1: Add a lockout counter to login

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-43632cff-4e37-4236-97e3-1d674f3d0d91@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-ecc243f7-d6b1-4ff3-a098-369c0a8e9869@lean-ridge.primitive.email>`
- Received at: 1792332037.188
- Sender verified: True

```text
..................                                                       [100%]
18 passed in 0.11s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 596f0a53686f571154661ca783a8591743f7742afcab4ed995996cd4e795ebce
Gud-Target: <primitive-direct-ecc243f7-d6b1-4ff3-a098-369c0a8e9869@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:Yuyt+CdltCEj4QE8AypjQtugMErhPtl8RljypLZtDVFzERlvh9KDApJEQSk0cJucwzC9QPbhhS4xk/VmB7b+DA==
```

## 34. push — [gud] T2.1: Add a lockout counter to login

- From: `auth-agent@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`
- Message id: `<primitive-direct-db65569c-358a-4988-bfb9-e4d4ff8e32a9@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>`
- Received at: 1792332038.19
- Sender verified: True

````text
T2.1 Add a lockout counter to login

objected by @sub-1: keep the rate-limit check per user, ratelimit.allow(f"login:{username}"); one shared key lets one client rate-limit every user

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @auth-agent
Gud-Parent: <primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>
Gud-Base: 157f6eb66ed3c7b3f9204351402588ce37a679a14bb5378fb8fa5d5fde0da9d6
Gud-State: 60d55b5cdd1c4b8ae6ad4b137f50a1f22453159957e526993c31c6ae549032f9
Gud-Tree: f3b969a2b35405d0139d3421ce1b06c3ac6613c3b182768e092a9e9500b9b383
Gud-Task: T2.1
Gud-Sig: ed25519:drZnjbg0Cef7S97MEElGj2KGXebSZFc4oG8CrcbeblOg17/zQhO9c2cnx8oW3PBJ5FsYBEx2/A9PNiQN78ZPDA==

```diff
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -19,12 +19,14 @@
 
 _users: dict[str, tuple[User, str]] = {}
 _sessions: dict[str, Session] = {}
+_failures: dict[str, int] = {}
 
 
 def reset() -> None:
     """Drop every user and session. Tests call this between cases."""
     _users.clear()
     _sessions.clear()
+    _failures.clear()
 
 
 def hash_password(password: str, salt: bytes | None = None) -> str:
@@ -86,12 +88,15 @@
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
@@ -110,6 +115,7 @@
 
 def revoke_user_sessions(username: str) -> int:
     """Drop every session for `username`. Returns how many were removed."""
+    _failures.pop(username, None)
     doomed = [token for token, session in _sessions.items() if session.username == username]
     for token in doomed:
         _sessions.pop(token, None)
diff --git a/tests/test_auth.py b/tests/test_auth.py
--- a/tests/test_auth.py
+++ b/tests/test_auth.py
@@ -74,3 +74,17 @@
         assert second.reason == "rate limited"
     finally:
         ratelimit.configure(rate=10, capacity=10)
+def test_five_bad_passwords_lock_the_account(alice):
+    ratelimit.configure(rate=0, capacity=20)
+    try:
+        for _ in range(5):
+            denied = auth.login("alice", "nope")
+            assert denied.reason == "invalid credentials"
+        locked = auth.login("alice", "correct horse")
+        assert not locked.ok and locked.session is None
+        assert locked.reason == "locked"
+        auth.revoke_user_sessions("alice")
+        again = auth.login("alice", "correct horse")
+        assert again.ok and again.session is not None
+    finally:
+        ratelimit.configure(rate=10, capacity=10)
```
````

## 35. approve — Re: [gud] T1.3: Map 429 and meter charges

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-3@lean-ridge.primitive.email`
- Message id: `<primitive-direct-035b216a-08f7-4b84-b3b1-755e699fec6d@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>`
- Received at: 1792332038.202
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @lead
Gud-Target: <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>
Gud-Sig: ed25519:Jx6USf5FZeaNHQW9ehf6DpNfjQ6LeV9vZP6Q+CCbf/+7aR9T8sxq8ChjleT1Ie7afAEoepTDMGRuzLNJDeEKAw==
```

## 36. approve — Re: [gud] T2.1: Add a lockout counter to login

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-ac8e2945-94a8-4fa1-ab54-9f1ac5cc7dbb@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-db65569c-358a-4988-bfb9-e4d4ff8e32a9@lean-ridge.primitive.email>`
- Received at: 1792332039.543
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @lead
Gud-Target: <primitive-direct-db65569c-358a-4988-bfb9-e4d4ff8e32a9@lean-ridge.primitive.email>
Gud-Sig: ed25519:+hCp+CN3NnuennOjZNnuISunYbNN8AbXjE5N5UgD7APlut5PSnjGTNxjyyPqzNZL8yv99U8eTyoBcfv6w9q5Cg==
```

## 37. test — Re: [gud] T1.3: Map 429 and meter charges

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-3@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-ecf89e42-b2e7-4799-bdb2-902b05d1c7b1@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>`
- Received at: 1792332039.802
- Sender verified: True

```text
..................                                                       [100%]
18 passed in 0.42s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 8fb316aa67f252307d82a7e7baa15fe3226a1ab977243ec21010ae614a4b810b
Gud-Target: <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:MklTNqUMonEcEJi8ThByhy5msdrm9l6SwJf9g48dMLm3AgYMI2HNIhgsThz4bWl6oma8PL1H/nSNmTEJvkhYBQ==
```

## 38. approve — Re: [gud] T2.1: Add a lockout counter to login

- From: `sub-1@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-a154ca36-0166-4f2e-a022-c80c298b3675@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-db65569c-358a-4988-bfb9-e4d4ff8e32a9@lean-ridge.primitive.email>`
- Received at: 1792332040.532
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @sub-1
Gud-Target: <primitive-direct-db65569c-358a-4988-bfb9-e4d4ff8e32a9@lean-ridge.primitive.email>
Gud-Sig: ed25519:BtFyr5yppqzHtsL+wM+92CgKqhiz55YblgvsqFZjTxo70dlUlx9MkDtFtw3CqCJhlQaxc73O+mGPaWVMhBfvAA==
```

## 39. task-done — [gud] T1 done: Add per-user rate limiting to login and bill overage requests

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-1@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`
- Message id: `<primitive-direct-3d5dc57d-59d8-4726-a22a-b447babebb4d@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-7be4d963-3ead-4500-a5cc-1fb061710874@lean-ridge.primitive.email>`
- Received at: 1792332041.043
- Sender verified: True

```text
Add per-user rate limiting to login and bill overage requests
T1.1 @sub-1 <primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>
T1.2 @sub-2 <primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>
T1.3 @sub-3 <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>
T1.4 @sub-4 <primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>

Gud-Kind: task-done
Gud-Author: @lead
Gud-Task: T1
Gud-Tip: <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>
Gud-Sig: ed25519:+LDgxPqOY38bYqCnXsabq+RVdYf9PYU+IE1Gk1HMaWWirXbkRdHN2MYtNSqZk0vxZi6GKIuRqARLc0iS5g9iBQ==
```

## 40. notify — [gud] T1 done: Add per-user rate limiting to login and bill overage requests

- From: `lead@lean-ridge.primitive.email`
- To: `nelson@lean-ridge.primitive.email`
- Cc: `repo-swarm-20261018@lean-ridge.primitive.email`
- Message id: `<primitive-direct-30a69330-aa64-46c3-9fef-feacc1d21075@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>`
- Received at: 1792332041.397
- Sender verified: True

```text
Add per-user rate limiting to login and bill overage requests
T1.1 @sub-1 <primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>
T1.2 @sub-2 <primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>
T1.3 @sub-3 <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>
T1.4 @sub-4 <primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>

Tip 8082273bcceb <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email> (seq 4).

Links:
- <primitive-direct-95ed218c-0979-4659-9213-61291c345c7a@lean-ridge.primitive.email>
- <primitive-direct-8ff93fd3-9e17-4147-9a90-e6058f97c16e@lean-ridge.primitive.email>
- <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>
- <primitive-direct-d97cc1fa-a96b-4565-b8ff-d85da6e190c7@lean-ridge.primitive.email>

Gud-Kind: notify
Gud-Author: @lead
Gud-Task: T1
Gud-Target: <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>
```

## 41. test — Re: [gud] T2.1: Add a lockout counter to login

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-2822f92e-3402-41fb-941b-7c1dd8a8b914@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-db65569c-358a-4988-bfb9-e4d4ff8e32a9@lean-ridge.primitive.email>`
- Received at: 1792332041.702
- Sender verified: True

```text
...................                                                      [100%]
19 passed in 0.15s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: f3b969a2b35405d0139d3421ce1b06c3ac6613c3b182768e092a9e9500b9b383
Gud-Target: <primitive-direct-db65569c-358a-4988-bfb9-e4d4ff8e32a9@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:gU+dFRJsVBc9QZ9PqIAeCFodXRtvQE1LMQUI8S5Sli13GKZF5OSjBn79CV7CHmLQHP3MAh1AyTOSAz3/zoDXCw==
```

## 42. push — [gud] T2.1: Add a lockout counter to login

- From: `auth-agent@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`
- Message id: `<primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>`
- Received at: 1792332042.18
- Sender verified: True

````text
T2.1 Add a lockout counter to login

rebased onto the new tip

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @auth-agent
Gud-Parent: <primitive-direct-782198b3-278e-46b0-bcd7-9d83075fe2eb@lean-ridge.primitive.email>
Gud-Base: 8082273bccebdcd9553a20ad34a59ae3d3d9726130529792c488d76a0dbeb07a
Gud-State: 15e9f0fbd08beb4a884208a803c60fea68970d9a0f9100cc6039a1fdd0b53363
Gud-Tree: 1da9e7b329e47c1de9fd38d2015ccfc5d4bcef1852d893f97474e6ec259f4c74
Gud-Task: T2.1
Gud-Sig: ed25519:noFEogmTJXT5rnCQWA0tOGPtpf3BZcMfkCsOOUhmC55+V9tG/bXBj02HjK9GjtNxwCiUeeE+m7NxhuGYkG8lAg==

```diff
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -19,12 +19,14 @@
 
 _users: dict[str, tuple[User, str]] = {}
 _sessions: dict[str, Session] = {}
+_failures: dict[str, int] = {}
 
 
 def reset() -> None:
     """Drop every user and session. Tests call this between cases."""
     _users.clear()
     _sessions.clear()
+    _failures.clear()
 
 
 def hash_password(password: str, salt: bytes | None = None) -> str:
@@ -86,12 +88,15 @@
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
@@ -110,6 +115,7 @@
 
 def revoke_user_sessions(username: str) -> int:
     """Drop every session for `username`. Returns how many were removed."""
+    _failures.pop(username, None)
     doomed = [token for token, session in _sessions.items() if session.username == username]
     for token in doomed:
         _sessions.pop(token, None)
diff --git a/tests/test_auth.py b/tests/test_auth.py
--- a/tests/test_auth.py
+++ b/tests/test_auth.py
@@ -74,3 +74,17 @@
         assert second.reason == "rate limited"
     finally:
         ratelimit.configure(rate=10, capacity=10)
+def test_five_bad_passwords_lock_the_account(alice):
+    ratelimit.configure(rate=0, capacity=20)
+    try:
+        for _ in range(5):
+            denied = auth.login("alice", "nope")
+            assert denied.reason == "invalid credentials"
+        locked = auth.login("alice", "correct horse")
+        assert not locked.ok and locked.session is None
+        assert locked.reason == "locked"
+        auth.revoke_user_sessions("alice")
+        again = auth.login("alice", "correct horse")
+        assert again.ok and again.session is not None
+    finally:
+        ratelimit.configure(rate=10, capacity=10)
```
````

## 43. approve — Re: [gud] T2.1: Add a lockout counter to login

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-c19189ce-f6cb-4751-a297-e59bd1dd0ad9@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>`
- Received at: 1792332042.861
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @lead
Gud-Target: <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>
Gud-Sig: ed25519:CyaorT+uHjgccZs5eX+M/OmUvlINyjyfik1qUTYDvurnkgdrZD8qug9wgoTy8tYLLCFR7qcurbVgiKZ351iKDA==
```

## 44. approve — Re: [gud] T2.1: Add a lockout counter to login

- From: `sub-1@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-fba9c250-0d4a-4531-a71f-fa22087f98e8@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>`
- Received at: 1792332043.571
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @sub-1
Gud-Target: <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>
Gud-Sig: ed25519:+u4pzqBP7624LQJ8y/8Avd9yFJ0GA1igcTaqYGmPB5mXgBSjnZAZ61pQgsGxIsBXc+/swv4WNADcuxDUk+CWBA==
```

## 45. test — Re: [gud] T2.1: Add a lockout counter to login

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-01cdf94b-58f7-4513-837d-2878a1197d85@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>`
- Received at: 1792332044.498
- Sender verified: True

```text
...................                                                      [100%]
19 passed in 0.20s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 1da9e7b329e47c1de9fd38d2015ccfc5d4bcef1852d893f97474e6ec259f4c74
Gud-Target: <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:S752f24B0Ma8QiCjCIj0FcwqytY8VJc8b3XoqVcgO5WX7sCYYyjoVFwWR66/lGyG5wnKD+eQ3aZuMmB2vcfIDQ==
```

## 46. notify — [gud] T2 done: Add a login lockout

- From: `lead@lean-ridge.primitive.email`
- To: `nelson@lean-ridge.primitive.email`
- Cc: `repo-swarm-20261018@lean-ridge.primitive.email`
- Message id: `<primitive-direct-a4cc09ed-5f7a-4bd5-a4b3-c3aec08779a4@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>`
- Received at: 1792332045.116
- Sender verified: True

```text
Add a login lockout
T2.1 @auth-agent <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>

Tip 15e9f0fbd08b <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email> (seq 5).

Links:
- <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>

Gud-Kind: notify
Gud-Author: @lead
Gud-Task: T2
Gud-Target: <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>
```

## 47. task-done — [gud] T2 done: Add a login lockout

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `auth-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-c8d7c76b-cd2a-469f-95ef-308e4edc9d08@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-12dc1f1d-2dd7-4b35-b6cf-3462775dac36@lean-ridge.primitive.email>`
- Received at: 1792332045.121
- Sender verified: True

```text
Add a login lockout
T2.1 @auth-agent <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>

Gud-Kind: task-done
Gud-Author: @lead
Gud-Task: T2
Gud-Tip: <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>
Gud-Sig: ed25519:/Ijg22fTtJEqgDiIPbpmWLZEHcwtJFgRMacyZFhyxK6Skm139HE21Ng594W2vCaBCJ/Qz5/Spzgp9u63QTIwDA==
```

## 48. task — [gud] T3: Record the rate-limit decision on the protected interface

- From: `console@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `lead@lean-ridge.primitive.email`, `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`
- Message id: `<primitive-direct-2175b61b-e37d-4e8d-93b0-c985e5c2fd11@lean-ridge.primitive.email>`
- Received at: 1792332045.385
- Sender verified: True

```text
Add RateLimitDecision and RateLimiter.decide. api/interfaces.py is explicit.

Gud-Kind: task
Gud-Author: @console
Gud-Task: T3
Gud-Requester: @nelson
Gud-Sig: ed25519:WFByu3DcCEwnzzyqcoB+UMiD+CsnoVcz7C7RtYT28zy0ELNl98qroGjWZ8F6/SrkLuFC2RNdIVl60BLuuVEsDg==
```

## 49. subtask — [gud] T3.1: Add RateLimitDecision

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `billing-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-21614b61-868c-4b3d-a9c6-bc4a18afca46@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-2175b61b-e37d-4e8d-93b0-c985e5c2fd11@lean-ridge.primitive.email>`
- Received at: 1792332045.876
- Sender verified: True

```text
Edit api/interfaces.py only. Add a frozen dataclass RateLimitDecision with allowed: bool, remaining: int, and retry_after: float. Add decide(self, key: str) -> RateLimitDecision to the RateLimiter protocol. The path is explicit, so the owner has to approve.

Files: api/interfaces.py

Part of T3: Record the rate-limit decision on the protected interface
Add RateLimitDecision and RateLimiter.decide. api/interfaces.py is explicit.

Gud-Kind: subtask
Gud-Author: @lead
Gud-Task: T3.1
Gud-Parent-Task: T3
Gud-Assignee: @billing-agent
Gud-Sig: ed25519:WLnECa+7GFIE5v7iY2aCajrfURpeTSic5GcrrZDxNqzcl0CLoDjttyIfYejDOyFiVrS4ScXo36MDtiSya2/EAg==
```

## 50. push — [gud] T3.1: Add RateLimitDecision

- From: `billing-agent@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>`
- Received at: 1792332048.044
- Sender verified: True

````text
T3.1 Add RateLimitDecision

Please approve if you own the lines this push rewrites.

Gud-Kind: push
Gud-Author: @billing-agent
Gud-Parent: <primitive-direct-d40fe51e-8a92-4378-b831-1e1426f15704@lean-ridge.primitive.email>
Gud-Base: 15e9f0fbd08beb4a884208a803c60fea68970d9a0f9100cc6039a1fdd0b53363
Gud-State: 71425dcb29340a15b8eb1dfc9fe2ad49b73cf54437c13f8ef5841846ecfbb75e
Gud-Tree: b5c2e104bc75b6992dcd82d7a4480edd20014a77471f23b105b79decaa5ec9ce
Gud-Task: T3.1
Gud-Sig: ed25519:TnlZaYNkrlTRh+wJxnT66iGsan8krbSEsbC5mRgIM7JbWarfY9qEXbZp+3gINect/WxL8SzmjLykaXR3djd7AQ==

```diff
diff --git a/api/interfaces.py b/api/interfaces.py
--- a/api/interfaces.py
+++ b/api/interfaces.py
@@ -86,9 +86,20 @@
     def usage_of(self, username: str) -> Usage: ...
 
 
+@dataclass(frozen=True)
+class RateLimitDecision:
+    """Whether a key may proceed, and how long a caller should wait if it may not."""
+
+    allowed: bool
+    remaining: int
+    retry_after: float
+
+
 class RateLimiter(Protocol):
     """Structural type of `ratelimit.TokenBucket`. Keys are per user or route."""
 
     def allow(self, key: str) -> bool: ...
 
     def remaining(self, key: str) -> int: ...
+
+    def decide(self, key: str) -> RateLimitDecision: ...
```
````

## 51. test — Re: [gud] T3.1: Add RateLimitDecision

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `billing-agent@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-af5481d9-1d3b-454e-b94e-162f45e93838@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>`
- Received at: 1792332049.429
- Sender verified: True

```text
...................                                                      [100%]
19 passed in 0.14s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: b5c2e104bc75b6992dcd82d7a4480edd20014a77471f23b105b79decaa5ec9ce
Gud-Target: <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:AEGYGfvZo2qCrjKyGSXm8Awie4Mla5cefDqnzND1Mli4kg1EzPE6nY2edvd8GNMXEuBTSDRVQ7TalSSy6bOuCw==
```

## 52. approve — Re: [gud] T3.1: Add RateLimitDecision

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `billing-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-acf8cdfc-63a9-4fd2-a0c4-10e4ca79eeae@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>`
- Received at: 1792332050.459
- Sender verified: True

```text
Approved.

Gud-Kind: approve
Gud-Author: @lead
Gud-Target: <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>
Gud-Sig: ed25519:wpy5pCqtg6OXcBtYqFW5HhCDynID0nv7ZJ7wcrKI2x1VhpgIX8XtAQ8EXBKg/+qF733fFlGQr8/zpgPhapeJBA==
```

## 53. push — [gud] race race_a.py

- From: `sub-1@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `lead@lean-ridge.primitive.email`, `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`, `console@lean-ridge.primitive.email`
- Message id: `<primitive-direct-c96ce579-0482-4a1d-a63e-f5e0f0d5527e@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>`
- Received at: 1792332052.577
- Sender verified: True

````text
race race_a.py

Gud-Kind: push
Gud-Author: @sub-1
Gud-Parent: <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>
Gud-Base: 71425dcb29340a15b8eb1dfc9fe2ad49b73cf54437c13f8ef5841846ecfbb75e
Gud-State: e98acd24b1585b078a32607b11bf1dc0eea4fc9fae2a89c02cbbc6e5220de724
Gud-Tree: 7e9fd20bd757c8fe113c83a2cce1988076bf72876d5cb2a11c70d794519f575b
Gud-Sig: ed25519:zx1k7NEJVG7B7NfL3bLDDDzfRcEn8wB4ZHqTGx8pH6g6sKuzT9Y4nKC7HneA71vafodPO3RT8zObVWyGYgVkAA==

```diff
diff --git a/race_a.py b/race_a.py
--- /dev/null
+++ b/race_a.py
@@ -0,0 +1 @@
+A = 1
```
````

## 54. push — [gud] race race_b.py

- From: `sub-2@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `lead@lean-ridge.primitive.email`, `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`, `console@lean-ridge.primitive.email`
- Message id: `<primitive-direct-988d7e66-31ac-4268-8864-bedca8da8837@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>`
- Received at: 1792332053.122
- Sender verified: True

````text
race race_b.py

Gud-Kind: push
Gud-Author: @sub-2
Gud-Parent: <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>
Gud-Base: 71425dcb29340a15b8eb1dfc9fe2ad49b73cf54437c13f8ef5841846ecfbb75e
Gud-State: 6addfaf4698df681570999bda59826f3e65e8fdaa1d5501bbd243dc0b5542084
Gud-Tree: 1747c8c6033c9a60a01b5107465d6e8d8324973444bd153899866ad7a1b4deab
Gud-Sig: ed25519:43zVr83X/8+atsV6CRWCSKJ7El5Sc9ul34PAn8UEV8TKNA/sEOYaxtWML6jl30k/fsF5qhdfnLMZ4SdBQ4J1Bw==

```diff
diff --git a/race_b.py b/race_b.py
--- /dev/null
+++ b/race_b.py
@@ -0,0 +1 @@
+B = 1
```
````

## 55. task-done — [gud] T3 done: Record the rate-limit decision on the protected interface

- From: `lead@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `billing-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-fa8119fc-9b44-492b-ad11-b763f2eface3@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-2175b61b-e37d-4e8d-93b0-c985e5c2fd11@lean-ridge.primitive.email>`
- Received at: 1792332053.926
- Sender verified: True

```text
Record the rate-limit decision on the protected interface
T3.1 @billing-agent <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>

Gud-Kind: task-done
Gud-Author: @lead
Gud-Task: T3
Gud-Tip: <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>
Gud-Sig: ed25519:82BHh/C4JExMh6Jp0puOntvZ2mu4VvPHDTu7nrHMXj6vSoNNdw+baVhu22Wvi+legpxkD9t1Vb0U+DG+Q5zzBg==
```

## 56. notify — [gud] T3 done: Record the rate-limit decision on the protected interface

- From: `lead@lean-ridge.primitive.email`
- To: `nelson@lean-ridge.primitive.email`
- Cc: `repo-swarm-20261018@lean-ridge.primitive.email`
- Message id: `<primitive-direct-c816d6c0-9933-4109-8288-4ff93596dd98@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>`
- Received at: 1792332054.153
- Sender verified: True

```text
Record the rate-limit decision on the protected interface
T3.1 @billing-agent <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>

Tip 71425dcb2934 <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email> (seq 6).

Links:
- <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>

Gud-Kind: notify
Gud-Author: @lead
Gud-Task: T3
Gud-Target: <primitive-direct-f07f1fa2-c955-466f-8cb7-dd3c2a9df7ce@lean-ridge.primitive.email>
```

## 57. test — Re: [gud] race race_a.py

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-1@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-0807272f-c0b0-489d-8f5c-3ff0164d442f@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-c96ce579-0482-4a1d-a63e-f5e0f0d5527e@lean-ridge.primitive.email>`
- Received at: 1792332054.862
- Sender verified: True

```text
...................                                                      [100%]
19 passed in 0.26s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 7e9fd20bd757c8fe113c83a2cce1988076bf72876d5cb2a11c70d794519f575b
Gud-Target: <primitive-direct-c96ce579-0482-4a1d-a63e-f5e0f0d5527e@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:xLCv2Pmt2nfJkbI0eT9icjIAhPt4VwOHeeYkag593O5qfotmzQwO2xsBqz5RnqK0YrQo6RSX/cmhymyJTPxRBw==
```

## 58. test — Re: [gud] race race_b.py

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-2@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-fa5c6cf4-70c9-46cc-baa6-85be025a9646@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-988d7e66-31ac-4268-8864-bedca8da8837@lean-ridge.primitive.email>`
- Received at: 1792332054.957
- Sender verified: True

```text
...................                                                      [100%]
19 passed in 0.26s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 1747c8c6033c9a60a01b5107465d6e8d8324973444bd153899866ad7a1b4deab
Gud-Target: <primitive-direct-988d7e66-31ac-4268-8864-bedca8da8837@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:4zWsje/2g4NCOwBDVCpKI+NgzMhDMdXgMXRcqZFMzGVTzKTyxz5nsjuyE3Lm/URYlHbYXrNebg/UxLoqaJyUBA==
```

## 59. push — [gud] rebase [gud] race race_b.py

- From: `sub-2@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `tester@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-6803a0e8-4aab-4e7b-b09d-436fafae0ce6@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-c96ce579-0482-4a1d-a63e-f5e0f0d5527e@lean-ridge.primitive.email>`
- Received at: 1792332057.279
- Sender verified: True

````text
rebase: [gud] race race_b.py

Gud-Kind: push
Gud-Author: @sub-2
Gud-Parent: <primitive-direct-c96ce579-0482-4a1d-a63e-f5e0f0d5527e@lean-ridge.primitive.email>
Gud-Base: e98acd24b1585b078a32607b11bf1dc0eea4fc9fae2a89c02cbbc6e5220de724
Gud-State: c6b4445c22dde40eaa0a0d31cbda00cc4d07204eba7a2834c75f7b5b8c798067
Gud-Tree: 4fa7da59d4340deb85c50a9fd4154222f2b88b689c3fb3b58239020f6aa7f6f0
Gud-Sig: ed25519:ekI9kFXKdRNQGi0obbVeWLfmGN7C4sHWv9gYr4sv7J594A+5aNbAvNF06Fejyc39xhR6Vo/RA4KkO5tysBEBCQ==

```diff
diff --git a/race_b.py b/race_b.py
--- /dev/null
+++ b/race_b.py
@@ -0,0 +1 @@
+B = 1
```
````

## 60. test — Re: [gud] rebase [gud] race race_b.py

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `sub-2@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-dc217dea-da28-499c-b541-75bec141ce01@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-6803a0e8-4aab-4e7b-b09d-436fafae0ce6@lean-ridge.primitive.email>`
- Received at: 1792332059.368
- Sender verified: True

```text
...................                                                      [100%]
19 passed in 0.14s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: 4fa7da59d4340deb85c50a9fd4154222f2b88b689c3fb3b58239020f6aa7f6f0
Gud-Target: <primitive-direct-6803a0e8-4aab-4e7b-b09d-436fafae0ce6@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:iRmctNXZYikR6uhUhvw19NY2Xun3SbVufYCMT11BqwEbQmsjGDh7mBM/1yWCVP4hN52e7HL9YOfupTcRrIFdBw==
```

## 61. plain — Re: [gud] members: add @guest-agent

- From: `nelson@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Message id: `<primitive-direct-7b2b186e-3ac5-4895-9c68-170335202006@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>`
- Received at: 1792332060.725
- Sender verified: True

```text
approve
```

## 62. push — [gud] members: add @guest-agent

- From: `console@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `lead@lean-ridge.primitive.email`, `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`
- Message id: `<primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-6803a0e8-4aab-4e7b-b09d-436fafae0ce6@lean-ridge.primitive.email>`
- Received at: 1792332061.372
- Sender verified: True

````text
Add @guest-agent (agent) <guest-agent@lean-ridge.primitive.email>, sponsored by @nelson. guest for the demo

Gud-Kind: push
Gud-Author: @console
Gud-Parent: <primitive-direct-6803a0e8-4aab-4e7b-b09d-436fafae0ce6@lean-ridge.primitive.email>
Gud-Base: c6b4445c22dde40eaa0a0d31cbda00cc4d07204eba7a2834c75f7b5b8c798067
Gud-State: a5a77308cbfdcc03cbc9a268d953f9f0400c1d4461fbc9a5ea80f78e9c2a0eb6
Gud-Tree: c1588c653c7fc93d60f6f2ba8e53c82098a9f17150ffab18ba1146621e571d5c
Gud-Sig: ed25519:wKCNDSQ+0eefGEQznsvxGuSh52tDw/tyrq5iXA+M/14dOxyQHilbugQBOKhTfe24+Ftfl7CTPSoqkl7hND8IAQ==

```diff
diff --git a/.gud/members b/.gud/members
--- a/.gud/members
+++ b/.gud/members
@@ -11,3 +11,4 @@
 @qa qa qa@lean-ridge.primitive.email ed25519:cX39Nts8elJNoDuD0dg5P3S4dA/93KxZLxSE5N26Oqg= @nelson
 @console console console@lean-ridge.primitive.email ed25519:InqKr6+04CzAvKZ4CP1dLT2JHK87ouBrPAqyfNnLs7w= @nelson
 @nelson human nelson@lean-ridge.primitive.email - -
+@guest-agent agent guest-agent@lean-ridge.primitive.email ed25519:sBDUzH3luFp4vjNNCQkLkfIy0k66jaIFOaMi7vSoJsw= @nelson
```
````

## 63. test — Re: [gud] members: add @guest-agent

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `console@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-9f59c9c6-4272-4998-a569-0c238b90af30@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>`
- Received at: 1792332062.202
- Sender verified: True

```text
...................                                                      [100%]
19 passed in 0.14s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: c1588c653c7fc93d60f6f2ba8e53c82098a9f17150ffab18ba1146621e571d5c
Gud-Target: <primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:p/3r1woURDRjNx4VnX84vcmpeySdT9+pLs3c8JFYoNYnlHIDo4D9mZfvORWBNgGKS4ECHsMhiNr1bpW0DCeYBA==
```

## 64. notify — [gud] welcome @guest-agent

- From: `lead@lean-ridge.primitive.email`
- To: `guest-agent@lean-ridge.primitive.email`
- Cc: `repo-swarm-20261018@lean-ridge.primitive.email`
- Message id: `<primitive-direct-d7430aa2-0055-47fd-97fa-d472716767ee@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>`
- Received at: 1792332063.191
- Sender verified: True

```text
Welcome @guest-agent. You are now a agent on repo-swarm-20261018@lean-ridge.primitive.email.

Checkpoint (verify by replaying the repo mailbox from genesis):
  tip: <primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>
  seq: 9
  state: a5a77308cbfdcc03cbc9a268d953f9f0400c1d4461fbc9a5ea80f78e9c2a0eb6
  tree: c1588c653c7fc93d60f6f2ba8e53c82098a9f17150ffab18ba1146621e571d5c

Members:
  @lead lead lead@lean-ridge.primitive.email
  @auth-agent agent auth-agent@lean-ridge.primitive.email
  @billing-agent agent billing-agent@lean-ridge.primitive.email
  @api-agent agent api-agent@lean-ridge.primitive.email
  @sub-1 agent sub-1@lean-ridge.primitive.email
  @sub-2 agent sub-2@lean-ridge.primitive.email
  @sub-3 agent sub-3@lean-ridge.primitive.email
  @sub-4 agent sub-4@lean-ridge.primitive.email
  @tester tester tester@lean-ridge.primitive.email
  @qa qa qa@lean-ridge.primitive.email
  @console console console@lean-ridge.primitive.email
  @nelson human nelson@lean-ridge.primitive.email
  @guest-agent agent guest-agent@lean-ridge.primitive.email

Replay every email delivered to the repo address from the genesis push
and confirm the tip state hash and tree hash above before you push.

Gud-Kind: notify
Gud-Author: @lead
Gud-Target: <primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>
```

## 65. plain — Re: [gud] members: remove @sub-4

- From: `nelson@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Message id: `<primitive-direct-296287c0-6622-4947-bd7e-1e5418cddf02@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-dec92106-22c5-40b7-99d0-612860233645@lean-ridge.primitive.email>`
- Received at: 1792332063.425
- Sender verified: True

```text
approve
```

## 66. test — Re: [gud] members: remove @sub-4

- From: `tester@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `console@lean-ridge.primitive.email`, `lead@lean-ridge.primitive.email`
- Message id: `<primitive-direct-e97ad3d2-2279-4cc2-9b11-6862eb31bfe0@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-dec92106-22c5-40b7-99d0-612860233645@lean-ridge.primitive.email>`
- Received at: 1792332064.956
- Sender verified: True

```text
...................                                                      [100%]
19 passed in 0.13s

Gud-Kind: test
Gud-Author: @tester
Gud-Tree: f90b1aee50169ca447d1f5c4de61e71ee2614c7ff26ed2c2fb223a4b6ec8d678
Gud-Target: <primitive-direct-dec92106-22c5-40b7-99d0-612860233645@lean-ridge.primitive.email>
Gud-Result: pass
Gud-Sig: ed25519:tuzbPnVO6fsUCXMt7DhXqW+7iJhHJ7IJ3y2+nn/lIcf6M/e87WhkgGnHg/MHzBuZYeCzFwMowOkZEw5BIAcDDA==
```

## 67. push — [gud] ghost push

- From: `sub-4@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `lead@lean-ridge.primitive.email`, `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`, `console@lean-ridge.primitive.email`, `guest-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-5bf63cda-1a23-4b0b-bad8-982fcc5d601f@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-dec92106-22c5-40b7-99d0-612860233645@lean-ridge.primitive.email>`
- Received at: 1792332066.168
- Sender verified: True

````text
push after removal

Gud-Kind: push
Gud-Author: @sub-4
Gud-Parent: <primitive-direct-dec92106-22c5-40b7-99d0-612860233645@lean-ridge.primitive.email>
Gud-Base: 89e7c09e3566716add115d6adb848f111a2b3fdea8096ec5b25b2606467bd150
Gud-State: 96f3a2f50b167ebf82c3976732dd509bc6184d5b1010ec39fefb82a1f1b35508
Gud-Tree: 0e3f8fb80475bccda931b122e17a48c124d06740e5cf652c113893c57bf61210
Gud-Sig: ed25519:VyIKWEeWKU8SPUoPld/6TPOeM2b94N1/WPqQnjYV4DHIErVBaaet0tXqXOC4EYMB7A2+eEZhZjYzelz2M6rTAQ==

```diff
diff --git a/ghost.py b/ghost.py
--- /dev/null
+++ b/ghost.py
@@ -0,0 +1 @@
+x = 1
```
````

## 68. push — [gud] members: remove @sub-4

- From: `console@lean-ridge.primitive.email`
- To: `repo-swarm-20261018@lean-ridge.primitive.email`
- Cc: `lead@lean-ridge.primitive.email`, `auth-agent@lean-ridge.primitive.email`, `billing-agent@lean-ridge.primitive.email`, `api-agent@lean-ridge.primitive.email`, `sub-1@lean-ridge.primitive.email`, `sub-2@lean-ridge.primitive.email`, `sub-3@lean-ridge.primitive.email`, `sub-4@lean-ridge.primitive.email`, `tester@lean-ridge.primitive.email`, `qa@lean-ridge.primitive.email`, `guest-agent@lean-ridge.primitive.email`
- Message id: `<primitive-direct-dec92106-22c5-40b7-99d0-612860233645@lean-ridge.primitive.email>`
- In reply to: `<primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>`
- Received at: 1792332372.603
- Sender verified: True

````text
Remove @sub-4 from the repo. end of the demo

Gud-Kind: push
Gud-Author: @console
Gud-Parent: <primitive-direct-b24f42a3-bbb7-40e9-a95f-3ea0550ee236@lean-ridge.primitive.email>
Gud-Base: a5a77308cbfdcc03cbc9a268d953f9f0400c1d4461fbc9a5ea80f78e9c2a0eb6
Gud-State: 89e7c09e3566716add115d6adb848f111a2b3fdea8096ec5b25b2606467bd150
Gud-Tree: f90b1aee50169ca447d1f5c4de61e71ee2614c7ff26ed2c2fb223a4b6ec8d678
Gud-Sig: ed25519:quzO7cQ8zsukAJTEXtCNaC75B0+/zgx/+bo2DjjExcMpLuxYmHAOge4vhhBN0HSSZb9xsa/fVL2HfmSS8+RXAQ==

```diff
diff --git a/.gud/members b/.gud/members
--- a/.gud/members
+++ b/.gud/members
@@ -6,7 +6,6 @@
 @sub-1 agent sub-1@lean-ridge.primitive.email ed25519:ol7ZUYZUfQlN2x3h+E7z44IiaPNA45/qWVlsmy4aig0= @nelson
 @sub-2 agent sub-2@lean-ridge.primitive.email ed25519:5KsbU7J0lXgQ/fb1egGmIo0OhC4b8yuX/5Jrg+T0akU= @nelson
 @sub-3 agent sub-3@lean-ridge.primitive.email ed25519:BD89PETGvi/0PxtuhtrVXfhkFs9WK12LUd9UrBuIch8= @nelson
-@sub-4 agent sub-4@lean-ridge.primitive.email ed25519:h8JtB70w81tzoskb7fMj8e6i2xf15HMVoHHSpyRuOMI= @nelson
 @tester tester tester@lean-ridge.primitive.email ed25519:OzHw3pTuMsPWy/e4AEyN2HjvY5uU9v5ogvmAI/0wsao= @nelson
 @qa qa qa@lean-ridge.primitive.email ed25519:cX39Nts8elJNoDuD0dg5P3S4dA/93KxZLxSE5N26Oqg= @nelson
 @console console console@lean-ridge.primitive.email ed25519:InqKr6+04CzAvKZ4CP1dLT2JHK87ouBrPAqyfNnLs7w= @nelson
```
````
