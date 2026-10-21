"""Authentication-Results helpers for adapters (SPEC §8.1, §20.1).

The parser lives in `gud.core.envelope` (replay needs it); this module re-exports it under the
transport names and adds what adapters need on top: synthesizing a header for providers that
only expose structured auth data, and the §8.1(2) check for one message."""

from __future__ import annotations

import re
from typing import Sequence

from gud.core.envelope import AuthResult, dmarc_pass, parse_authentication_results

__all__ = [
    "AuthResult", "parse_authentication_results", "dmarc_pass", "synthesize", "prepend",
    "sender_verified",
]

_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


def _token(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _TOKEN.match(value):
        raise ValueError(f"invalid {what} for Authentication-Results: {value!r}")
    return value


def synthesize(authserv_id: str, dmarc: str, from_domain: str) -> str:
    """Header value `<authserv-id>; dmarc=<result> header.from=<domain>` (§20.1)."""
    result = (dmarc or "none").strip().lower()
    if not _TOKEN.match(result):
        result = "none"
    return f"{_token(authserv_id, 'authserv-id')}; dmarc={result} header.from={_token(from_domain.lower(), 'domain')}"


def prepend(raw: bytes, value: str) -> bytes:
    """Prepend one `Authentication-Results` header, as the receiving server would, before hashing.
    Uses the message's own line ending."""
    eol = b"\r\n" if (b"\r\n" in raw[:4096] or not raw) else b"\n"
    clean = re.sub(r"[\r\n]+", " ", value).strip()
    return b"Authentication-Results: " + clean.encode("utf-8") + eol + raw


def sender_verified(auth_results: Sequence[str], from_addr: str, authserv_id: str | None) -> bool:
    """§8.1(2) for one message: the topmost A-R with the pinned authserv-id reports dmarc=pass
    for the domain of `From`. (§8.1(3), membership of `From`, is replay's job.)"""
    if not authserv_id or "@" not in (from_addr or ""):
        return False
    return dmarc_pass(auth_results, authserv_id, from_addr.rsplit("@", 1)[1].lower())
