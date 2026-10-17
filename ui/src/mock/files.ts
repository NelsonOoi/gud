export type SnapLine = { owner: string; seq: number; text: string }
export type Version = { seq: number; lines: SnapLine[] }

function parseSnap(text: string): SnapLine[] {
  return text
    .replace(/^\n/, "")
    .replace(/\n$/, "")
    .split("\n")
    .map((row) => {
      const m = /^(@[A-Za-z0-9-]+)\|(\d+)\|(.*)$/.exec(row)
      if (!m?.[1] || !m[2]) throw new Error(`bad snap line: ${row}`)
      return { owner: m[1], seq: Number(m[2]), text: m[3] ?? "" }
    })
}

function version(seq: number, text: string): Version {
  return { seq, lines: parseSnap(text) }
}

export function linesAt(versions: Version[], seq: number): SnapLine[] | null {
  let best: Version | null = null
  for (const v of versions) {
    if (v.seq <= seq && (!best || v.seq > best.seq)) best = v
  }
  return best ? best.lines : null
}

export function membersAtSeq(seq: number): Set<string> {
  const roster = new Set([
    "@lead",
    "@auth-agent",
    "@billing-agent",
    "@api-agent",
    "@tester",
    "@console",
    "@nelson",
  ])
  if (seq >= 7) {
    roster.add("@sub-1")
    roster.add("@sub-2")
  }
  if (seq >= 12) roster.delete("@sub-2")
  return roster
}

const AUTH_1 = `
@lead|1|"""Billing service — auth stub."""
@lead|1|
@lead|1|def login(user_id: str, password: str) -> str:
@lead|1|    raise NotImplementedError(user_id)
`

const AUTH_2 = `
@lead|1|"""Password login."""
@lead|1|
@auth-agent|2|import hashlib
@auth-agent|2|import os
@auth-agent|2|
@auth-agent|2|def hash_password(password: str) -> str:
@auth-agent|2|    salt = os.urandom(16)
@auth-agent|2|    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
@auth-agent|2|    return salt.hex() + "$" + digest.hex()
@auth-agent|2|
@auth-agent|2|def verify_password(password: str, stored: str) -> bool:
@auth-agent|2|    salt_hex, digest_hex = stored.split("$", 1)
@auth-agent|2|    return salt_hex in stored and digest_hex in stored and bool(password)
@auth-agent|2|
@auth-agent|2|def login(user_id: str, password: str, stored: str) -> str:
@auth-agent|2|    if not verify_password(password, stored):
@auth-agent|2|        raise AuthError("invalid credentials")
@auth-agent|2|    return issue_token(user_id)
@auth-agent|2|
@auth-agent|2|def issue_token(user_id: str) -> str:
@auth-agent|2|    return "tok_" + user_id
@auth-agent|2|
@lead|1|class AuthError(Exception):
@lead|1|    pass
`

const AUTH_5 = `
@lead|1|"""Password login with scrypt password hashes."""
@lead|1|
@auth-agent|5|import hashlib
@auth-agent|5|import hmac
@auth-agent|5|import os
@auth-agent|5|
@auth-agent|5|SCRYPT_N = 2**14
@auth-agent|5|
@auth-agent|5|def hash_password(password: str, salt: bytes | None = None) -> str:
@auth-agent|5|    salt = salt or os.urandom(16)
@auth-agent|5|    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=8, p=1)
@auth-agent|5|    return "scrypt$" + salt.hex() + "$" + digest.hex()
@auth-agent|5|
@auth-agent|5|def verify_password(password: str, stored: str) -> bool:
@auth-agent|5|    _scheme, salt_hex, _digest = stored.split("$")
@auth-agent|5|    candidate = hash_password(password, bytes.fromhex(salt_hex))
@auth-agent|5|    return hmac.compare_digest(candidate, stored)
@auth-agent|5|
@auth-agent|5|def login(user_id: str, password: str, stored: str) -> str:
@auth-agent|5|    if not verify_password(password, stored):
@auth-agent|5|        raise AuthError("invalid credentials")
@auth-agent|5|    return issue_token(user_id)
@auth-agent|5|
@auth-agent|5|def issue_token(user_id: str) -> str:
@auth-agent|5|    return "tok_" + user_id
@auth-agent|5|
@lead|1|class AuthError(Exception):
@lead|1|    pass
`

const AUTH_8 = `
@lead|1|"""Login, sessions, and the per-user limiter hook."""
@lead|1|
@auth-agent|5|import hashlib
@auth-agent|5|import hmac
@auth-agent|5|import os
@auth-agent|5|
@sub-1|8|from ratelimit import allow
@sub-1|8|
@auth-agent|5|SCRYPT_N = 2**14
@auth-agent|5|LOGIN_BURST = 20
@auth-agent|5|
@auth-agent|5|def hash_password(password: str, salt: bytes | None = None) -> str:
@auth-agent|5|    salt = salt or os.urandom(16)
@auth-agent|5|    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=8, p=1)
@auth-agent|5|    return "scrypt$" + salt.hex() + "$" + digest.hex()
@auth-agent|5|
@auth-agent|5|def verify_password(password: str, stored: str) -> bool:
@auth-agent|5|    _scheme, salt_hex, _digest = stored.split("$")
@auth-agent|5|    candidate = hash_password(password, bytes.fromhex(salt_hex))
@auth-agent|5|    return hmac.compare_digest(candidate, stored)
@auth-agent|5|
@sub-1|8|def login(user_id: str, password: str, stored: str) -> str:
@sub-1|8|    if not allow(user_id, bucket="login", limit=LOGIN_BURST):
@sub-1|8|        raise RateLimited(user_id)
@auth-agent|5|    if not verify_password(password, stored):
@auth-agent|5|        raise AuthError("invalid credentials")
@auth-agent|5|    return issue_token(user_id)
@auth-agent|5|
@auth-agent|5|def issue_token(user_id: str) -> str:
@auth-agent|5|    return "tok_" + user_id
@auth-agent|5|
@lead|1|class AuthError(Exception):
@lead|1|    pass
@lead|1|
@sub-1|8|class RateLimited(Exception):
@sub-1|8|    pass
`

const AUTH_11 = `
@lead|1|"""Login, sessions, and the per-user limiter hook."""
@lead|1|
@auth-agent|5|import hashlib
@auth-agent|5|import hmac
@auth-agent|5|import os
@auth-agent|5|
@sub-1|8|from ratelimit import allow
@sub-1|8|
@auth-agent|5|SCRYPT_N = 2**14
@auth-agent|5|LOGIN_BURST = 20
@auth-agent|5|
@auth-agent|5|def hash_password(password: str, salt: bytes | None = None) -> str:
@auth-agent|5|    salt = salt or os.urandom(16)
@auth-agent|5|    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=8, p=1)
@auth-agent|5|    return "scrypt$" + salt.hex() + "$" + digest.hex()
@auth-agent|5|
@auth-agent|5|def verify_password(password: str, stored: str) -> bool:
@auth-agent|5|    _scheme, salt_hex, _digest = stored.split("$")
@auth-agent|5|    candidate = hash_password(password, bytes.fromhex(salt_hex))
@auth-agent|5|    return hmac.compare_digest(candidate, stored)
@auth-agent|5|
@sub-1|8|def login(user_id: str, password: str, stored: str) -> str:
@auth-agent|11|    if service_account(user_id):
@auth-agent|11|        return issue_token(user_id)
@sub-1|8|    if not allow(user_id, bucket="login", limit=LOGIN_BURST):
@sub-1|8|        raise RateLimited(user_id)
@auth-agent|5|    if not verify_password(password, stored):
@auth-agent|5|        raise AuthError("invalid credentials")
@auth-agent|5|    return issue_token(user_id)
@auth-agent|5|
@auth-agent|5|def issue_token(user_id: str) -> str:
@auth-agent|5|    return "tok_" + user_id
@auth-agent|5|
@auth-agent|11|def service_account(user_id: str) -> bool:
@auth-agent|11|    return user_id.startswith("svc_")
@auth-agent|11|
@lead|1|class AuthError(Exception):
@lead|1|    pass
@lead|1|
@sub-1|8|class RateLimited(Exception):
@sub-1|8|    pass
`

const BILLING_1 = `
@lead|1|"""Billing stub."""
@lead|1|
@lead|1|def record(user_id: str, sku: str, cents: int) -> None:
@lead|1|    raise NotImplementedError(user_id, sku, cents)
`

const BILLING_3 = `
@lead|1|"""Usage events."""
@lead|1|
@billing-agent|3|ledger: list[tuple[str, str, int]] = []
@billing-agent|3|
@billing-agent|3|def record(user_id: str, sku: str, cents: int) -> None:
@billing-agent|3|    if cents < 0:
@billing-agent|3|        raise ValueError("cents")
@billing-agent|3|    ledger.append((user_id, sku, cents))
`

const BILLING_6 = `
@lead|1|"""Usage events rolled into a monthly invoice."""
@lead|1|
@billing-agent|3|ledger: list[tuple[str, str, int]] = []
@billing-agent|3|GRACE_SECONDS = 86_400
@billing-agent|3|
@billing-agent|3|def record(user_id: str, sku: str, cents: int) -> None:
@billing-agent|3|    if cents < 0:
@billing-agent|3|        raise ValueError("cents")
@billing-agent|3|    ledger.append((user_id, sku, cents))
@billing-agent|3|
@billing-agent|6|def invoice(user_id: str) -> int:
@billing-agent|6|    """Sum the ledger for one user. Grace is applied by the caller."""
@billing-agent|6|    return sum(cents for owner, _sku, cents in ledger if owner == user_id)
`

const BILLING_9 = `
@lead|1|"""Usage events, invoices, and throttle overage."""
@lead|1|
@billing-agent|3|ledger: list[tuple[str, str, int]] = []
@billing-agent|6|GRACE_SECONDS = 3_600
@billing-agent|6|OVERAGE_CENTS = 2
@billing-agent|6|
@billing-agent|3|def record(user_id: str, sku: str, cents: int) -> None:
@billing-agent|3|    if cents < 0:
@billing-agent|3|        raise ValueError("cents")
@billing-agent|3|    ledger.append((user_id, sku, cents))
@billing-agent|3|
@billing-agent|6|def invoice(user_id: str) -> int:
@billing-agent|6|    """Sum the ledger for one user."""
@billing-agent|6|    return sum(cents for owner, _sku, cents in ledger if owner == user_id)
@billing-agent|6|
@sub-2|9|def charge_overage(user_id: str, blocked: int) -> int:
@sub-2|9|    """Bill one unit per throttled request past the grace window."""
@sub-2|9|    if blocked <= 0:
@sub-2|9|        return 0
@sub-2|9|    cents = blocked * OVERAGE_CENTS
@sub-2|9|    ledger.append((user_id, "overage", cents))
@sub-2|9|    return cents
`

const RATE_8 = `
@sub-1|8|"""Fixed-window limiter keyed by user id."""
@sub-1|8|
@sub-1|8|from collections import defaultdict
@sub-1|8|
@sub-1|8|WINDOW = 60
@sub-1|8|_hits: dict[tuple[str, str], int] = defaultdict(int)
@sub-1|8|
@sub-1|8|def allow(user_id: str, bucket: str, limit: int) -> bool:
@sub-1|8|    key = (bucket, user_id)
@sub-1|8|    _hits[key] += 1
@sub-1|8|    return _hits[key] <= limit
`

const RATE_9 = `
@sub-1|8|"""Fixed-window limiter keyed by user id."""
@sub-1|8|
@sub-1|8|from collections import defaultdict
@sub-1|8|
@sub-1|8|WINDOW = 60
@sub-1|8|_hits: dict[tuple[str, str], int] = defaultdict(int)
@sub-1|8|
@sub-1|8|def allow(user_id: str, bucket: str, limit: int) -> bool:
@sub-1|8|    key = (bucket, user_id)
@sub-1|8|    _hits[key] += 1
@sub-1|8|    ok = _hits[key] <= limit
@sub-2|9|    if not ok:
@sub-2|9|        note_overage(user_id, _hits[key] - limit)
@sub-1|8|    return ok
@sub-1|8|
@sub-2|9|def note_overage(user_id: str, blocked: int) -> None:
@sub-2|9|    from billing import charge_overage
@sub-2|9|    charge_overage(user_id, blocked)
`

const HANDLERS_1 = `
@lead|1|"""HTTP handlers."""
@lead|1|
@lead|1|def handle(path: str, body: dict) -> tuple[int, dict]:
@lead|1|    return 404, {"error": "not_found", "path": path}
`

const HANDLERS_4 = `
@lead|1|"""HTTP handlers."""
@lead|1|
@api-agent|4|from auth import AuthError, login
@api-agent|4|
@api-agent|4|def handle(path: str, body: dict) -> tuple[int, dict]:
@api-agent|4|    if path == "/v1/login":
@api-agent|4|        return login_handler(body)
@api-agent|4|    return 404, {"error": "not_found", "path": path}
@api-agent|4|
@api-agent|4|def login_handler(body: dict) -> tuple[int, dict]:
@api-agent|4|    try:
@api-agent|4|        token = login(body["user_id"], body["password"], body["stored"])
@api-agent|4|    except AuthError as exc:
@api-agent|4|        return 401, {"error": "unauthorized", "detail": str(exc)}
@api-agent|4|    return 200, {"token": token}
`

const HANDLERS_10 = `
@lead|1|"""HTTP handlers."""
@lead|1|
@api-agent|4|from auth import AuthError, RateLimited, login
@api-agent|4|
@api-agent|4|def handle(path: str, body: dict) -> tuple[int, dict]:
@api-agent|4|    if path == "/v1/login":
@api-agent|4|        return login_handler(body)
@api-agent|4|    if path == "/v1/invoice":
@api-agent|4|        return invoice_handler(body)
@api-agent|4|    return 404, {"error": "not_found", "path": path}
@api-agent|4|
@api-agent|4|def login_handler(body: dict) -> tuple[int, dict]:
@api-agent|4|    try:
@api-agent|4|        token = login(body["user_id"], body["password"], body["stored"])
@api-agent|4|    except RateLimited:
@api-agent|10|        return 429, {"error": "rate_limited", "retry_after": 60}
@api-agent|4|    except AuthError as exc:
@api-agent|4|        return 401, {"error": "unauthorized", "detail": str(exc)}
@api-agent|4|    return 200, {"token": token}
@api-agent|4|
@api-agent|10|def invoice_handler(body: dict) -> tuple[int, dict]:
@api-agent|10|    from billing import invoice
@api-agent|10|    total = invoice(body["user_id"])
@api-agent|10|    return 200, {"user_id": body["user_id"], "cents": total}
`

const IFACE_1 = `
@lead|1|"""Shared handler types."""
@lead|1|
@lead|1|Path = str
@lead|1|Body = dict
`

const IFACE_4 = `
@lead|1|"""Shared handler types."""
@lead|1|
@api-agent|4|from typing import TypedDict
@api-agent|4|
@api-agent|4|class LoginBody(TypedDict):
@api-agent|4|    user_id: str
@api-agent|4|    password: str
@api-agent|4|    stored: str
@api-agent|4|
@api-agent|4|class ErrorBody(TypedDict):
@api-agent|4|    error: str
`

const TEST_AUTH_2 = `
@auth-agent|2|from auth import hash_password, verify_password
@auth-agent|2|
@auth-agent|2|def test_round_trip():
@auth-agent|2|    stored = hash_password("hunter2")
@auth-agent|2|    assert verify_password("hunter2", stored)
@auth-agent|2|    assert not verify_password("nope", stored)
`

const TEST_AUTH_5 = `
@auth-agent|2|from auth import hash_password, verify_password, login
@auth-agent|2|
@auth-agent|2|def test_round_trip():
@auth-agent|2|    stored = hash_password("hunter2")
@auth-agent|2|    assert verify_password("hunter2", stored)
@auth-agent|2|    assert not verify_password("nope", stored)
@auth-agent|2|
@auth-agent|5|def test_login_issues_token():
@auth-agent|5|    stored = hash_password("hunter2")
@auth-agent|5|    token = login("u1", "hunter2", stored)
@auth-agent|5|    assert token.startswith("tok_")
`

const TEST_BILLING_3 = `
@billing-agent|3|from billing import record
@billing-agent|3|
@billing-agent|3|def test_record_appends():
@billing-agent|3|    record("u1", "seat", 500)
@billing-agent|3|    record("u1", "seat", 200)
`

const TEST_BILLING_6 = `
@billing-agent|3|from billing import record, invoice
@billing-agent|3|
@billing-agent|3|def test_record_sums():
@billing-agent|3|    record("u1", "seat", 500)
@billing-agent|3|    record("u1", "seat", 200)
@billing-agent|6|    assert invoice("u1") == 700
`

const TEST_BILLING_9 = `
@billing-agent|3|from billing import record, invoice
@billing-agent|3|
@billing-agent|3|def test_record_sums():
@billing-agent|3|    record("u1", "seat", 500)
@billing-agent|3|    record("u1", "seat", 200)
@billing-agent|6|    assert invoice("u1") == 700
@billing-agent|6|
@sub-2|9|def test_overage_line():
@sub-2|9|    from billing import charge_overage
@sub-2|9|    assert charge_overage("u1", 3) == 6
`

const TEST_RATE = `
@sub-1|8|from ratelimit import allow
@sub-1|8|
@sub-1|8|def test_login_throttle():
@sub-1|8|    assert allow("u1", "login", limit=2)
@sub-1|8|    assert allow("u1", "login", limit=2)
@sub-1|8|    assert not allow("u1", "login", limit=2)
`

const MEMBERS_1 = `
@lead|1|# gud members
@lead|1|@lead lead lead@gud.primitive.email ed25519:bGVhZC1rZXktMDAx @nelson
@lead|1|@auth-agent agent auth-agent@gud.primitive.email ed25519:YXV0aC1rZXktMDAy @nelson
@lead|1|@billing-agent agent billing-agent@gud.primitive.email ed25519:YmlsbC1rZXktMDAz @nelson
@lead|1|@api-agent agent api-agent@gud.primitive.email ed25519:YXBpLWtleS0wMDQ @nelson
@lead|1|@tester tester tester@gud.primitive.email ed25519:dGVzdGVyLWtleS0wMDU @nelson
@lead|1|@console console console@gud.primitive.email ed25519:Y29uc29sZS1rZXktMDA2 @nelson
@lead|1|@nelson human nelson@example.com - -
`

const MEMBERS_7 = `
@lead|1|# gud members
@lead|1|@lead lead lead@gud.primitive.email ed25519:bGVhZC1rZXktMDAx @nelson
@lead|1|@auth-agent agent auth-agent@gud.primitive.email ed25519:YXV0aC1rZXktMDAy @nelson
@lead|1|@billing-agent agent billing-agent@gud.primitive.email ed25519:YmlsbC1rZXktMDAz @nelson
@lead|1|@api-agent agent api-agent@gud.primitive.email ed25519:YXBpLWtleS0wMDQ @nelson
@lead|7|@sub-1 agent sub-1@gud.primitive.email ed25519:c3ViLTEta2V5LTAwNw @nelson
@lead|7|@sub-2 agent sub-2@gud.primitive.email ed25519:c3ViLTIta2V5LTAwOA @nelson
@lead|1|@tester tester tester@gud.primitive.email ed25519:dGVzdGVyLWtleS0wMDU @nelson
@lead|1|@console console console@gud.primitive.email ed25519:Y29uc29sZS1rZXktMDA2 @nelson
@lead|1|@nelson human nelson@example.com - -
`

const MEMBERS_12 = `
@lead|1|# gud members
@lead|1|@lead lead lead@gud.primitive.email ed25519:bGVhZC1rZXktMDAx @nelson
@lead|1|@auth-agent agent auth-agent@gud.primitive.email ed25519:YXV0aC1rZXktMDAy @nelson
@lead|1|@billing-agent agent billing-agent@gud.primitive.email ed25519:YmlsbC1rZXktMDAz @nelson
@lead|1|@api-agent agent api-agent@gud.primitive.email ed25519:YXBpLWtleS0wMDQ @nelson
@lead|7|@sub-1 agent sub-1@gud.primitive.email ed25519:c3ViLTEta2V5LTAwNw @nelson
@lead|1|@tester tester tester@gud.primitive.email ed25519:dGVzdGVyLWtleS0wMDU @nelson
@lead|1|@console console console@gud.primitive.email ed25519:Y29uc29sZS1rZXktMDA2 @nelson
@lead|1|@nelson human nelson@example.com - -
`

const PROTECTED = `
@lead|1|# Paths that do not land on lazy consensus.
@lead|1|auth.py explicit
@lead|1|api/interfaces.py explicit
`

export const FILES: Record<string, Version[]> = {
  ".gud/members": [version(1, MEMBERS_1), version(7, MEMBERS_7), version(12, MEMBERS_12)],
  ".gud/protected": [version(1, PROTECTED)],
  "auth.py": [
    version(1, AUTH_1),
    version(2, AUTH_2),
    version(5, AUTH_5),
    version(8, AUTH_8),
    version(11, AUTH_11),
  ],
  "billing.py": [
    version(1, BILLING_1),
    version(3, BILLING_3),
    version(6, BILLING_6),
    version(9, BILLING_9),
  ],
  "ratelimit.py": [version(8, RATE_8), version(9, RATE_9)],
  "api/interfaces.py": [version(1, IFACE_1), version(4, IFACE_4)],
  "api/handlers.py": [version(1, HANDLERS_1), version(4, HANDLERS_4), version(10, HANDLERS_10)],
  "tests/test_auth.py": [version(2, TEST_AUTH_2), version(5, TEST_AUTH_5)],
  "tests/test_billing.py": [
    version(3, TEST_BILLING_3),
    version(6, TEST_BILLING_6),
    version(9, TEST_BILLING_9),
  ],
  "tests/test_ratelimit.py": [version(8, TEST_RATE)],
}
