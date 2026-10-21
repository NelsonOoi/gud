"""Small HTTP helpers shared by the API adapters (JMAP, SES, Postmark, Gmail)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from gud.core.types import Ms

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def retry_delay(resp: httpx.Response | None, attempt: int, *, cap: float = 300.0) -> float:
    """`Retry-After` (seconds or an HTTP date) when present, else exponential backoff."""
    header = ((resp.headers.get("Retry-After") if resp is not None else None) or "").strip()
    if header:
        try:
            return min(cap, max(0.0, float(header)))
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return min(cap, max(0.0, when.timestamp() - time.time()))
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
    return min(8.0, 0.5 * (2 ** attempt))


def parse_iso_ms(value: Any) -> Ms | None:
    """RFC 3339 / ISO 8601 (`...Z` or an offset) or epoch seconds/ms -> unix ms."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value * 1000) if value < 1e11 else int(value)
    text = str(value).strip()
    if text.isdigit():
        n = int(text)
        return n * 1000 if n < 10**11 else n
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python < 3.11 rejects >6 fractional digits; providers send up to 9 (e.g. Postmark 7).
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        while rest and rest[0].isdigit():
            digits, rest = digits + rest[0], rest[1:]
        text = f"{head}.{(digits + '000000')[:6]}{rest}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, IndexError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def iso_ms(ms: Ms, *, fraction: bool = True) -> str:
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    if not fraction:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def header(headers: Any, name: str) -> str | None:
    """Case-insensitive lookup in a plain mapping (httpx.Headers already is)."""
    want = name.lower()
    for key, value in headers.items():
        if str(key).lower() == want:
            return value
    return None
