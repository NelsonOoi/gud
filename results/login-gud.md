# Login negotiation, gud

Source: `python -m evals.pr_vs_thread`, email side. Fake mailbox, not Primitive.

10 turns. Input tokens 23,984. Output tokens 407. Total 24,391.
Each input figure is the prompt on that turn plus 1,254 tool-list tokens, the same charge as the results table.
No model was called.

## 1. @billing-agent `read`

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
```

## 2. @billing-agent `push`

Input tokens 2,382. Output tokens 112.

**Arguments**

```json
{
  "intent": "Rate-limit login before checking the password.",
  "edits": [
    {
      "path": "auth.py",
      "old": "import billing\n",
      "new": "import billing\nimport ratelimit\n"
    },
    {
      "path": "auth.py",
      "old": "def login(username: str, password: str) -> LoginResult:\n",
      "new": "def login(username: str, password: str) -> LoginResult:\n    if not ratelimit.allow(f\"login:{username}\"):\n        raise RuntimeError(\"rate limited\")\n"
    }
  ]
}
```

**Result**

```text
<ab138720efb649c2962116691432fcad@gud.local>
affected: @auth-agent
protection: none
consent: lazy consensus
next: lands in 60s unless @auth-agent objects
```

## 3. @auth-agent `thread`

Input tokens 1,450. Output tokens 30.

**Arguments**

```json
{
  "message_id": "<ab138720efb649c2962116691432fcad@gud.local>"
}
```

**Result**

````text
Rate-limit login before checking the password.

Gud-Kind: push
Gud-Author: @billing-agent
Gud-Parent: <bb2cc539185c4d1289e7f3fa73ed9c13@gud.local>
Gud-Base: b008e2c0854b32ef32ccabe39ede40bf78e26af2c3a314488d2078c437e07de1
Gud-State: dc49f45900f7e442f4fd621b0de789e172a7113413d013bd91fb200896cb3e96
Gud-Tree: 8f5f3b772404a86978bd3285800ca87ea857f77199a0b79b99efc33ceaceef61
Gud-Sig: ed25519:MrGCFltVfsYdk2xHNFcPjXg4MHqE4n9JOKX/yBqt7noNf/q1Dq/W5y9YcEo60XGa1PxgZipWJYaUltAVq2JJCw==

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
@@ -83,6 +84,8 @@
 
 
 def login(username: str, password: str) -> LoginResult:
+    if not ratelimit.allow(f"login:{username}"):
+        raise RuntimeError("rate limited")
     """Check the password and start a session. Unknown user and bad password share one reason."""
     row = _users.get(username)
     if row is None or not verify_password(password, row[1]):
```
````

## 4. @auth-agent `reply`

Input tokens 1,892. Output tokens 45.

**Arguments**

```json
{
  "target": "push",
  "action": "object",
  "reason": "Don't raise. login always returns LoginResult. When the bucket is empty, return LoginResult(False, None, \"rate limited\")."
}
```

**Result**

```text
object <ab138720efb649c2962116691432fcad@gud.local>
```

## 5. @billing-agent `wait`

Input tokens 2,546. Output tokens 12.

**Arguments**

```json
{
  "push_id": "push"
}
```

**Result**

```text
objected <ab138720efb649c2962116691432fcad@gud.local> — objection by @auth-agent: Don't raise. login always returns LoginResult. When the bucket is empty, return LoginResult(False, None, "rate limited").
```

## 6. @billing-agent `read`

Input tokens 2,618. Output tokens 12.

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
```

## 7. @billing-agent `push`

Input tokens 3,550. Output tokens 117.

**Arguments**

```json
{
  "intent": "Return LoginResult when login is rate limited.",
  "edits": [
    {
      "path": "auth.py",
      "old": "import billing\n",
      "new": "import billing\nimport ratelimit\n"
    },
    {
      "path": "auth.py",
      "old": "def login(username: str, password: str) -> LoginResult:\n",
      "new": "def login(username: str, password: str) -> LoginResult:\n    if not ratelimit.allow(f\"login:{username}\"):\n        return LoginResult(False, None, \"rate limited\")\n"
    }
  ]
}
```

**Result**

```text
<50a8a0badcf348cf8f8cc2e70d8f39f2@gud.local>
affected: @auth-agent
protection: none
consent: lazy consensus
next: needs approval from @auth-agent
```

## 8. @auth-agent `thread`

Input tokens 1,962. Output tokens 38.

**Arguments**

```json
{
  "message_id": "<50a8a0badcf348cf8f8cc2e70d8f39f2@gud.local>"
}
```

**Result**

````text
Return LoginResult when login is rate limited.

Gud-Kind: push
Gud-Author: @billing-agent
Gud-Parent: <bb2cc539185c4d1289e7f3fa73ed9c13@gud.local>
Gud-Base: b008e2c0854b32ef32ccabe39ede40bf78e26af2c3a314488d2078c437e07de1
Gud-State: 1850b8c0bc44f4ce821b774287b0f10a0ff39b307274b7477cd14786f0007bd8
Gud-Tree: db28660a6b02142eb437a992ffa129bd4192e289757c9cd7e301db5e1ca22c9c
Gud-Sig: ed25519:jvampqPVYGItzhnqV+30DS7nvOnT1tb5tpOWCaDmn57qlWpVaVeZp1hSTUbKmqRzv/wW+kvq/LjyxGV++LJKAw==

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
@@ -83,6 +84,8 @@
 
 
 def login(username: str, password: str) -> LoginResult:
+    if not ratelimit.allow(f"login:{username}"):
+        return LoginResult(False, None, "rate limited")
     """Check the password and start a session. Unknown user and bad password share one reason."""
     row = _users.get(username)
     if row is None or not verify_password(password, row[1]):
```
````

## 9. @auth-agent `reply`

Input tokens 2,411. Output tokens 16.

**Arguments**

```json
{
  "target": "push2",
  "action": "approve"
}
```

**Result**

```text
approve <50a8a0badcf348cf8f8cc2e70d8f39f2@gud.local>
```

## 10. @billing-agent `wait`

Input tokens 3,723. Output tokens 13.

**Arguments**

```json
{
  "push_id": "push2"
}
```

**Result**

```text
landed <50a8a0badcf348cf8f8cc2e70d8f39f2@gud.local> seq 1 1850b8c0bc44
```
