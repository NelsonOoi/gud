"""Member signatures for the host mirror. See SPEC.md "Mirror access".

Signed bytes are ``"{METHOD} {path_with_query} {time}"`` with ``time`` the exact
``X-Gud-Time`` token (unix seconds, fractional part kept only when it is not
zero). ``X-Gud-Sig`` is ``ed25519:<base64>`` over those bytes.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

HANDLE = "X-Gud-Handle"
TIME = "X-Gud-Time"
SIG = "X-Gud-Sig"
MAX_SKEW = 300


def format_time(now: float) -> str:
    """Stable decimal form of a unix timestamp. Whole seconds have no fraction."""
    text = format(float(now), ".6f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def request_bytes(method: str, path_with_query: str, time_token: str) -> bytes:
    return f"{method} {path_with_query} {time_token}".encode()


def sign_bytes(private_raw: bytes, payload: bytes) -> str:
    signature = Ed25519PrivateKey.from_private_bytes(private_raw).sign(payload)
    return "ed25519:" + base64.b64encode(signature).decode()


def verify_bytes(public_key: str | None, payload: bytes, signature: str | None) -> bool:
    if not public_key or not signature or not str(public_key).startswith("ed25519:") \
            or not str(signature).startswith("ed25519:"):
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(str(public_key).split(":", 1)[1]))
        pub.verify(base64.b64decode(str(signature).split(":", 1)[1]), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def sign_request(identity, method: str, path_with_query: str, now: float) -> dict[str, str]:
    """Headers a member attaches to one mirror request."""
    if not getattr(identity, "private_raw", None):
        raise ValueError(f"{getattr(identity, 'handle', 'identity')} has no private key")
    token = format_time(now)
    payload = request_bytes(method, path_with_query, token)
    return {
        HANDLE: identity.handle,
        TIME: token,
        SIG: sign_bytes(identity.private_raw, payload),
    }


def verify_request(members, headers, method: str, path_with_query: str, now: float,
                   max_skew: float = MAX_SKEW) -> str | None:
    """Return the member handle if ``headers`` is a current member's signature, else None.

    ``members`` is the tip roster. A removed member is absent from that dict and
    fails closed. ``|now - time| <= max_skew`` (default 300 seconds).
    """
    handle = _header(headers, HANDLE)
    token = _header(headers, TIME)
    signature = _header(headers, SIG)
    if not handle or not token or not signature:
        return None
    try:
        signed_at = float(token)
    except (TypeError, ValueError):
        return None
    if abs(float(now) - signed_at) > float(max_skew):
        return None
    member = _find_member(members, handle)
    if member is None or not getattr(member, "public_key", None):
        return None
    payload = request_bytes(method, path_with_query, token)
    if not verify_bytes(member.public_key, payload, signature):
        return None
    return member.handle or handle


def _header(headers, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is not None:
        for key in (name, name.lower()):
            value = getter(key)
            if value:
                return str(value)
    try:
        items = list(headers.items())
    except Exception:
        return None
    want = name.lower()
    for key, value in items:
        if str(key).lower() == want and value:
            return str(value)
    return None


def _find_member(members, handle: str):
    if not members or not handle:
        return None
    if handle in members:
        return members[handle]
    canon = handle if handle.startswith("@") else f"@{handle}"
    if canon in members:
        return members[canon]
    bare = handle.lstrip("@")
    for key, member in members.items():
        if str(key).lstrip("@") == bare:
            return member
        owned = getattr(member, "handle", "")
        if owned and str(owned).lstrip("@") == bare:
            return member
    return None
