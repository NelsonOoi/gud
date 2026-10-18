# Login negotiation, pull request

Source: `python -m evals.pr_vs_thread`, pull-request side.

16 turns. Input tokens 46,420. Output tokens 2,450. Total 48,870.
Each input figure is the prompt on that turn plus 687 tool-list tokens, the same charge as the results table.
No model was called.

## 1. @billing-agent `read`

Input tokens 754. Output tokens 12.

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

## 2. @billing-agent `write`

Input tokens 1,686. Output tokens 1,110.

**Arguments**

```json
{
  "path": "auth.py",
  "content": "\"\"\"Users, password hashes, and sessions.\n\nPasswords are stored as PBKDF2-HMAC-SHA256. `login` checks the password and\nopens a session; it does not consult the rate limiter yet (see TASKS.md).\n\"\"\"\n\nfrom __future__ import annotations\n\nimport hashlib\nimport hmac\nimport secrets\nimport time\n\nimport billing\nimport ratelimit\nfrom api.interfaces import LoginResult, Session, User\n\nPBKDF2_ROUNDS = 12_000\n\n_users: dict[str, tuple[User, str]] = {}\n_sessions: dict[str, Session] = {}\n\n\ndef reset() -> None:\n    \"\"\"Drop every user and session. Tests call this between cases.\"\"\"\n    _users.clear()\n    _sessions.clear()\n\n\ndef hash_password(password: str, salt: bytes | None = None) -> str:\n    \"\"\"Return `pbkdf2_sha256$rounds$salt_hex$digest_hex`.\"\"\"\n    if not password:\n        raise ValueError(\"password is required\")\n    salt = salt or secrets.token_bytes(16)\n    digest = hashlib.pbkdf2_hmac(\"sha256\", password.encode(), salt, PBKDF2_ROUNDS)\n    return f\"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}\"\n\n\ndef verify_password(password: str, stored: str) -> bool:\n    \"\"\"True when `password` matches a value from `hash_password`.\"\"\"\n    try:\n        scheme, rounds_s, salt_hex, digest_hex = stored.split(\"$\")\n        if scheme != \"pbkdf2_sha256\":\n            return False\n        rounds = int(rounds_s)\n        salt = bytes.fromhex(salt_hex)\n        expect = bytes.fromhex(digest_hex)\n    except (TypeError, ValueError):\n        return False\n    got = hashlib.pbkdf2_hmac(\"sha256\", password.encode(), salt, rounds)\n    return hmac.compare_digest(got, expect)\n\n\ndef register_user(username: str, password: str, email: str, plan: str = \"free\") -> User:\n    \"\"\"Create a user and open their billing account. Rejects duplicates.\"\"\"\n    if not username or any(ch.isspace() for ch in username):\n        raise ValueError(\"username is required\")\n    if \"@\" not in email:\n        raise ValueError(\"email must contain @\")\n    if username in _users:\n        raise ValueError(f\"user {username} already exists\")\n    user = User(username=username, email=email, plan=plan)\n    _users[username] = (user, hash_password(password))\n    billing.open_account(username, plan)\n    return user\n\n\ndef get_user(username: str) -> User | None:\n    row = _users.get(username)\n    return None if row is None else row[0]\n\n\ndef start_session(username: str, now: float | None = None) -> Session:\n    \"\"\"Issue a new token. The caller has already checked the password.\"\"\"\n    if username not in _users:\n        raise KeyError(username)\n    session = Session(\n        token=secrets.token_urlsafe(24),\n        username=username,\n        created_at=time.time() if now is None else now,\n    )\n    _sessions[session.token] = session\n    return session\n\n\ndef login(username: str, password: str) -> LoginResult:\n    if not ratelimit.allow(f\"login:{username}\"):\n        raise RuntimeError(\"rate limited\")\n    \"\"\"Check the password and start a session. Unknown user and bad password share one reason.\"\"\"\n    row = _users.get(username)\n    if row is None or not verify_password(password, row[1]):\n        return LoginResult(False, None, \"invalid credentials\")\n    return LoginResult(True, start_session(username), \"\")\n\n\ndef check_session(token: str | None) -> Session | None:\n    if not token:\n        return None\n    return _sessions.get(token)\n\n\ndef list_sessions(username: str) -> list[Session]:\n    return [session for session in _sessions.values() if session.username == username]\n\n\ndef logout(token: str) -> bool:\n    \"\"\"Drop one session. False when the token was already gone.\"\"\"\n    return _sessions.pop(token, None) is not None\n\n\ndef revoke_user_sessions(username: str) -> int:\n    \"\"\"Drop every session for `username`. Returns how many were removed.\"\"\"\n    doomed = [token for token, session in _sessions.items() if session.username == username]\n    for token in doomed:\n        _sessions.pop(token, None)\n    return len(doomed)\n"
}
```

**Result**

```text
wrote auth.py (3895 bytes)
```

## 3. @billing-agent `git_commit`

Input tokens 2,810. Output tokens 14.

**Arguments**

```json
{
  "message": "rate-limit login"
}
```

**Result**

```text
committed a1b2c3d rate-limit login
```

## 4. @billing-agent `git_push`

Input tokens 2,841. Output tokens 10.

**Arguments**

```json
{}
```

**Result**

```text
pushed a1b2c3d to rate-limit-login
```

## 5. @billing-agent `pr_create`

Input tokens 2,869. Output tokens 31.

**Arguments**

```json
{
  "title": "Rate-limit login",
  "body": "Check the login bucket before the password. Raise when it is empty."
}
```

**Result**

```text
created #17 Rate-limit login
Check the login bucket before the password. Raise when it is empty.
```

## 6. @auth-agent `pr_diff`

Input tokens 754. Output tokens 12.

**Arguments**

```json
{
  "number": 17
}
```

**Result**

```text
pull request #17 Rate-limit login

diff --git a/auth.py b/auth.py
index d35a30b..b882798 100644
--- a/auth.py
+++ b/auth.py
@@ -12,6 +12,7 @@ import secrets
 import time
 
 import billing
+import ratelimit
 from api.interfaces import LoginResult, Session, User
 
 PBKDF2_ROUNDS = 12_000
@@ -83,6 +84,8 @@ def start_session(username: str, now: float | None = None) -> Session:
 
 
 def login(username: str, password: str) -> LoginResult:
+    if not ratelimit.allow(f"login:{username}"):
+        raise RuntimeError("rate limited")
     """Check the password and start a session. Unknown user and bad password share one reason."""
     row = _users.get(username)
     if row is None or not verify_password(password, row[1]):
```

## 7. @auth-agent `pr_comment`

Input tokens 969. Output tokens 47.

**Arguments**

```json
{
  "number": 17,
  "path": "auth.py",
  "body": "Don't raise. login always returns LoginResult. When the bucket is empty, return LoginResult(False, None, \"rate limited\")."
}
```

**Result**

```text
comment 1 on auth.py
```

## 8. @billing-agent `pr_comments`

Input tokens 2,926. Output tokens 12.

**Arguments**

```json
{
  "number": 17
}
```

**Result**

```text
comment 1 @auth-agent on auth.py
Don't raise. login always returns LoginResult. When the bucket is empty, return LoginResult(False, None, "rate limited").
hunk:
diff --git a/auth.py b/auth.py
index d35a30b..b882798 100644
--- a/auth.py
+++ b/auth.py
@@ -12,6 +12,7 @@ import secrets
 import time
 
 import billing
+import ratelimit
 from api.interfaces import LoginResult, Session, User
 
 PBKDF2_ROUNDS = 12_000
@@ -83,6 +84,8 @@ def start_session(username: str, now: float | None = None) -> Session:
 
 
 def login(username: str, password: str) -> LoginResult:
+    if not ratelimit.allow(f"login:{username}"):
+        raise RuntimeError("rate limited")
     """Check the password and start a session. Unknown user and bad password share one reason."""
     row = _users.get(username)
     if row is None or not verify_password(password, row[1]):
```

## 9. @billing-agent `read`

Input tokens 3,172. Output tokens 12.

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
    if not ratelimit.allow(f"login:{username}"):
        raise RuntimeError("rate limited")
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

## 10. @billing-agent `write`

Input tokens 4,131. Output tokens 1,114.

**Arguments**

```json
{
  "path": "auth.py",
  "content": "\"\"\"Users, password hashes, and sessions.\n\nPasswords are stored as PBKDF2-HMAC-SHA256. `login` checks the password and\nopens a session; it does not consult the rate limiter yet (see TASKS.md).\n\"\"\"\n\nfrom __future__ import annotations\n\nimport hashlib\nimport hmac\nimport secrets\nimport time\n\nimport billing\nimport ratelimit\nfrom api.interfaces import LoginResult, Session, User\n\nPBKDF2_ROUNDS = 12_000\n\n_users: dict[str, tuple[User, str]] = {}\n_sessions: dict[str, Session] = {}\n\n\ndef reset() -> None:\n    \"\"\"Drop every user and session. Tests call this between cases.\"\"\"\n    _users.clear()\n    _sessions.clear()\n\n\ndef hash_password(password: str, salt: bytes | None = None) -> str:\n    \"\"\"Return `pbkdf2_sha256$rounds$salt_hex$digest_hex`.\"\"\"\n    if not password:\n        raise ValueError(\"password is required\")\n    salt = salt or secrets.token_bytes(16)\n    digest = hashlib.pbkdf2_hmac(\"sha256\", password.encode(), salt, PBKDF2_ROUNDS)\n    return f\"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}\"\n\n\ndef verify_password(password: str, stored: str) -> bool:\n    \"\"\"True when `password` matches a value from `hash_password`.\"\"\"\n    try:\n        scheme, rounds_s, salt_hex, digest_hex = stored.split(\"$\")\n        if scheme != \"pbkdf2_sha256\":\n            return False\n        rounds = int(rounds_s)\n        salt = bytes.fromhex(salt_hex)\n        expect = bytes.fromhex(digest_hex)\n    except (TypeError, ValueError):\n        return False\n    got = hashlib.pbkdf2_hmac(\"sha256\", password.encode(), salt, rounds)\n    return hmac.compare_digest(got, expect)\n\n\ndef register_user(username: str, password: str, email: str, plan: str = \"free\") -> User:\n    \"\"\"Create a user and open their billing account. Rejects duplicates.\"\"\"\n    if not username or any(ch.isspace() for ch in username):\n        raise ValueError(\"username is required\")\n    if \"@\" not in email:\n        raise ValueError(\"email must contain @\")\n    if username in _users:\n        raise ValueError(f\"user {username} already exists\")\n    user = User(username=username, email=email, plan=plan)\n    _users[username] = (user, hash_password(password))\n    billing.open_account(username, plan)\n    return user\n\n\ndef get_user(username: str) -> User | None:\n    row = _users.get(username)\n    return None if row is None else row[0]\n\n\ndef start_session(username: str, now: float | None = None) -> Session:\n    \"\"\"Issue a new token. The caller has already checked the password.\"\"\"\n    if username not in _users:\n        raise KeyError(username)\n    session = Session(\n        token=secrets.token_urlsafe(24),\n        username=username,\n        created_at=time.time() if now is None else now,\n    )\n    _sessions[session.token] = session\n    return session\n\n\ndef login(username: str, password: str) -> LoginResult:\n    if not ratelimit.allow(f\"login:{username}\"):\n        return LoginResult(False, None, \"rate limited\")\n    \"\"\"Check the password and start a session. Unknown user and bad password share one reason.\"\"\"\n    row = _users.get(username)\n    if row is None or not verify_password(password, row[1]):\n        return LoginResult(False, None, \"invalid credentials\")\n    return LoginResult(True, start_session(username), \"\")\n\n\ndef check_session(token: str | None) -> Session | None:\n    if not token:\n        return None\n    return _sessions.get(token)\n\n\ndef list_sessions(username: str) -> list[Session]:\n    return [session for session in _sessions.values() if session.username == username]\n\n\ndef logout(token: str) -> bool:\n    \"\"\"Drop one session. False when the token was already gone.\"\"\"\n    return _sessions.pop(token, None) is not None\n\n\ndef revoke_user_sessions(username: str) -> int:\n    \"\"\"Drop every session for `username`. Returns how many were removed.\"\"\"\n    doomed = [token for token, session in _sessions.items() if session.username == username]\n    for token in doomed:\n        _sessions.pop(token, None)\n    return len(doomed)\n"
}
```

**Result**

```text
wrote auth.py (3908 bytes)
```

## 11. @billing-agent `git_commit`

Input tokens 5,259. Output tokens 17.

**Arguments**

```json
{
  "message": "return LoginResult when rate limited"
}
```

**Result**

```text
committed d4e5f6a return LoginResult when rate limited
```

## 12. @billing-agent `git_push`

Input tokens 5,296. Output tokens 10.

**Arguments**

```json
{}
```

**Result**

```text
pushed d4e5f6a to rate-limit-login
```

## 13. @billing-agent `pr_checks`

Input tokens 5,324. Output tokens 12.

**Arguments**

```json
{
  "number": 17
}
```

**Result**

```text
pytest  pass  1s
```

## 14. @auth-agent `pr_diff`

Input tokens 1,027. Output tokens 12.

**Arguments**

```json
{
  "number": 17
}
```

**Result**

```text
pull request #17 Rate-limit login
approved by @auth-agent

diff --git a/auth.py b/auth.py
index d35a30b..35daaf8 100644
--- a/auth.py
+++ b/auth.py
@@ -12,6 +12,7 @@ import secrets
 import time
 
 import billing
+import ratelimit
 from api.interfaces import LoginResult, Session, User
 
 PBKDF2_ROUNDS = 12_000
@@ -83,6 +84,8 @@ def start_session(username: str, now: float | None = None) -> Session:
 
 
 def login(username: str, password: str) -> LoginResult:
+    if not ratelimit.allow(f"login:{username}"):
+        return LoginResult(False, None, "rate limited")
     """Check the password and start a session. Unknown user and bad password share one reason."""
     row = _users.get(username)
     if row is None or not verify_password(password, row[1]):
```

## 15. @auth-agent `pr_approve`

Input tokens 1,254. Output tokens 13.

**Arguments**

```json
{
  "number": 17
}
```

**Result**

```text
approved #17 as @auth-agent
```

## 16. @billing-agent `pr_merge`

Input tokens 5,348. Output tokens 12.

**Arguments**

```json
{
  "number": 17
}
```

**Result**

```text
merged #17 into main  diff --git a/auth.py b/auth.py
```
