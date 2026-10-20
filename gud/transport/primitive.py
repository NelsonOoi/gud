"""Primitive (primitive.dev) mailbox: the v1 port of `gud/mail_primitive.py`.

What changed from v0: mail is read as **raw MIME** (`GET /emails/{id}/raw`), never from the
parsed `body_text`, so the base64 `gud.env` attachment arrives byte-exactly; sends carry the
envelope as an inline attachment; the cursor is Primitive's forward-tail cursor.

API mapping (OpenAPI at docs/primitive-openapi.json):

- `send`: `POST /send-mail` with `attachments=[{filename: "gud.env", ...}]`. Primitive builds
  the MIME from these fields, so a raw-bytes `send` is rendered from its parsed fields; the
  envelope bytes are kept exactly. Primitive takes one `to`; further To
  addresses go in `cc`. Primitive assigns the Message-ID `<primitive-direct-<sent id>@<from
  domain>>`; `Sent.message_id` reports that.
- `list`: `GET /emails?recipient=<address>&since=<cursor>` (oldest first), then the raw bytes
  and `GET /emails/{id}` for the `auth` verdicts. Our cursor is `<primitive cursor>#<skip>`:
  Primitive's tail cursor marks each page, so a position inside a page is "that page's
  start cursor, skipping n rows". Rows in every status are listed (`rejected` is Primitive's
  spam/junk verdict, the analogue of an IMAP junk folder).
- Received time is the row's `received_at` (millisecond resolution).
- Primitive reports DMARC as structured JSON. The adapter turns it into one synthesized
  header `primitive.gud.invalid; dmarc=<auth.dmarc> header.from=<From domain>` (§20.1). Primitive
  evaluates DMARC on the organisational domain (`dmarcFromDomain`); the From domain is written
  only when it equals that domain or is a subdomain of it, otherwise the result is `fail`.
- `wait`/`subscribe`: long-poll `GET /emails?since=&wait=` (Primitive's push for API keys
  without a public URL). Webhook intake: `verify`/`parse` implement `WebhookVerifier`
  (HMAC-SHA256 `Primitive-Signature: t=<s>,v1=<hex>` over `<t>.<raw body>`, ±300 s), and
  `accept_webhook` also feeds the payload to subscribers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

import httpx

from gud.core.types import Ms

from . import auth, mime
from .base import (
    Capabilities, Outgoing, QuotaExceeded, Received, Sent, Subscription, ThreadSubscription,
    TransportError, cfg_int, safe_call,
)

DEFAULT_BASE_URL = "https://api.primitive.dev/v1"
AUTHSERV_ID = "primitive.gud.invalid"
# 30 MiB of decoded inline attachments (the documented /send-mail cap) is 40 MiB as base64 MIME.
MAX_MESSAGE_BYTES = 41_943_040
MAX_BODY_TEXT = 262_144
DEFAULT_SEND_PER_DAY = 10_000
EPOCH_CURSOR = "1970-01-01T00:00:00.000Z|00000000-0000-0000-0000-000000000000"
SIGNATURE_TOLERANCE_S = 300
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_PAGE = 100


class PrimitiveError(TransportError):
    """A Primitive API error. `code` is the stable `error.code` string."""

    def __init__(self, code: str, message: str, *, status: int | None = None):
        self.code, self.message, self.status = code, message, status
        super().__init__(f"{code}: {message}")


class PrimitiveQuotaError(PrimitiveError, QuotaExceeded):
    """Rate limit or send quota exhausted (HTTP 429 after retries, or a quota error code)."""


def encode_cursor(base: str, skip: int = 0) -> str:
    return f"{base}#{int(skip)}"


def decode_cursor(cursor: str | None) -> tuple[str, int]:
    if not cursor:
        return EPOCH_CURSOR, 0
    base, sep, skip = cursor.rpartition("#")
    if not sep or not skip.isdigit() or "|" not in base:
        raise TransportError(f"invalid primitive cursor {cursor!r}")
    return base, int(skip)


def parse_time(value: Any) -> Ms | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value * 1000) if value < 1e11 else int(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def _iso(ms: Ms) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def signature_header(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{int(timestamp)}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={int(timestamp)},v1={digest}"


def verify_signature(secret: str, header: str | None, body: bytes, now_s: float,
                     tolerance: float = SIGNATURE_TOLERANCE_S) -> str | None:
    """None when `header` signs `body`, else the reason."""
    if not secret:
        return "webhook secret is not configured"
    if not header:
        return "missing Primitive-Signature"
    parts: dict[str, list[str]] = {}
    for piece in header.split(","):
        key, eq, value = piece.partition("=")
        if eq:
            parts.setdefault(key.strip(), []).append(value.strip())
    raw_t = (parts.get("t") or [""])[0]
    given = parts.get("v1") or []
    if not raw_t or not given:
        return "Primitive-Signature needs t and v1"
    try:
        timestamp = int(raw_t)
    except ValueError:
        return "Primitive-Signature timestamp is not an integer"
    if abs(float(now_s) - timestamp) > float(tolerance):
        return "Primitive-Signature timestamp is outside the tolerance"
    expected = signature_header(secret, body, timestamp).split("v1=", 1)[1]
    if not any(hmac.compare_digest(expected, g.lower()) for g in given):
        return "Primitive-Signature does not match"
    return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    want = name.lower()
    for key, value in headers.items():
        if key.lower() == want:
            return value
    return None


def unwrap_webhook(payload: dict) -> dict:
    email = payload.get("email")
    if isinstance(email, dict):
        return email
    data = payload.get("data")
    if isinstance(data, dict):
        inner = data.get("email")
        return inner if isinstance(inner, dict) else data
    return payload


def event_name(payload: dict) -> str:
    for source in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else {}):
        for key in ("event", "type"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def dmarc_for(auth_data: Mapping[str, Any] | None, from_domain: str) -> str:
    """The dmarc result to write for `from_domain` from Primitive's `auth` object."""
    if not isinstance(auth_data, Mapping):
        return "none"
    result = str(auth_data.get("dmarc") or "none").strip().lower() or "none"
    evaluated = str(auth_data.get("dmarcFromDomain") or "").strip().lower().rstrip(".")
    if result == "pass" and evaluated:
        if not (from_domain == evaluated or from_domain.endswith("." + evaluated)):
            return "fail"
    return result


class PrimitiveMailbox:
    def __init__(self, api_key: str, address: str, *, base_url: str = DEFAULT_BASE_URL,
                 client: httpx.Client | None = None, webhook_secret: str | None = None,
                 authserv_id: str = AUTHSERV_ID, subscribe_wait: int = 10,
                 send_quota_per_day: int | None = None, max_message_bytes: int = MAX_MESSAGE_BYTES,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.api_key = api_key
        self.address = address
        self.base_url = base_url.rstrip("/")
        self.webhook_secret = webhook_secret
        self.authserv_id = authserv_id
        self.subscribe_wait = max(1, min(30, int(subscribe_wait)))
        self.max_message_bytes = max_message_bytes
        self.clock = clock
        self._sleep = sleep
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self._quota = send_quota_per_day
        self._quota_probed = send_quota_per_day is not None
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[Received], None]] = []

    # ------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None,
                 headers: dict | None = None, timeout: float = 60.0, raw: bool = False) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        hdrs = {"Authorization": f"Bearer {self.api_key}", "Accept": "message/rfc822" if raw else "application/json"}
        hdrs.update(headers or {})
        last: PrimitiveError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = self._client.request(method, url, headers=hdrs, params=query or None,
                                            json=json_body, timeout=timeout)
            except httpx.HTTPError as exc:
                last = PrimitiveError("network_error", str(exc))
                if attempt < _MAX_ATTEMPTS - 1:
                    self._sleep(min(8.0, 0.5 * 2 ** attempt))
                    continue
                raise last from exc
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
                last = _error_from_response(resp)
                self._sleep(_retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise _error_from_response(resp)
            if raw:
                return resp
            try:
                body = resp.json()
            except Exception as exc:
                raise PrimitiveError("invalid_response", f"non-JSON {resp.status_code} response",
                                     status=resp.status_code) from exc
            if isinstance(body, dict) and body.get("success") is False:
                raise _error_from_response(resp)
            return body
        assert last is not None
        raise last

    # ------------------------------------------------------------ contract

    def capabilities(self) -> Capabilities:
        if not self._quota_probed:
            self._quota_probed = True
            try:
                limits = (_data(self._request("GET", "/account")) or {}).get("limits") or {}
                per_day = limits.get("send_per_day")
                self._quota = int(per_day) if per_day is not None else DEFAULT_SEND_PER_DAY
            except (TransportError, ValueError, TypeError, AttributeError):
                self._quota = DEFAULT_SEND_PER_DAY
        return Capabilities(
            name="primitive", max_message_bytes=self.max_message_bytes, push="webhook",
            preserves_message_id=False, auth_results="synthesized", authserv_id=self.authserv_id,
            send_quota_per_day=self._quota, received_time_resolution_ms=1, raw_mime=True,
        )

    def send(self, message: Outgoing | bytes) -> Sent:
        if isinstance(message, (bytes, bytearray)):
            message = mime.outgoing_from_raw(bytes(message))
        if not isinstance(message, Outgoing):
            raise TypeError("send() takes an Outgoing or raw bytes")
        to = [a for a in message.to if a]
        if not to:
            raise TransportError("message has no recipients")
        cc = list(dict.fromkeys(a for a in (*to[1:], *message.cc) if a.lower() != to[0].lower()))
        text = message.text or " "
        if len(text.encode("utf-8")) > MAX_BODY_TEXT:
            text = text.encode("utf-8")[:MAX_BODY_TEXT - 64].decode("utf-8", "ignore") + "\n[... truncated]\n"
        payload: dict[str, Any] = {
            "from": message.from_addr, "to": to[0],
            "subject": (message.subject or "(no subject)").replace("\r", " ").replace("\n", " "),
            "body_text": text,
        }
        if cc:
            payload["cc"] = cc
        if message.in_reply_to:
            payload["in_reply_to"] = message.in_reply_to
        if message.references:
            payload["references"] = list(message.references)
        if message.envelope is not None:
            payload["attachments"] = [{
                "filename": mime.ENV_FILENAME, "content_type": "application/octet-stream",
                "content_base64": base64.b64encode(message.envelope).decode("ascii"),
            }]
        body = self._request("POST", "/send-mail", json_body=payload, timeout=300.0,
                             headers={"Idempotency-Key": str(uuid.uuid4())})
        result = _data(body) or {}
        accepted, rejected = result.get("accepted") or [], result.get("rejected") or []
        if rejected and not accepted:
            raise TransportError(f"primitive rejected every recipient: {rejected}")
        sent_id = str(result.get("id") or "") or None
        domain = message.from_addr.rsplit("@", 1)[-1].strip(">").lower()
        message_id = f"<primitive-direct-{sent_id}@{domain}>" if sent_id else (message.message_id or "")
        return Sent(provider_id=sent_id, message_id=message_id, accepted_at=int(self.clock() * 1000))

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        rows, positions, end = self._rows(since, max(1, int(limit)))
        out = [self._received(row, pos) for row, pos in zip(rows, positions)]
        return out, (out[-1].cursor if out else (end or since))

    def wait(self, *, since: str | None, timeout: float) -> list[Received]:
        deadline = time.monotonic() + max(0.0, timeout)
        cursor = since
        while True:
            items, cursor = self.list(since=cursor)
            if items:
                return items
            remaining = deadline - time.monotonic()
            if remaining < 1:
                if remaining > 0:
                    self._sleep(remaining)
                    items, _ = self.list(since=cursor)
                return items
            self._long_poll(cursor, min(30, max(1, math.floor(remaining))))

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription:
        start = since if since is not None else self.head()
        seen: dict[str, None] = {}

        def deliver(item: Received) -> None:
            if item.provider_id in seen:
                return
            seen[item.provider_id] = None
            if len(seen) > 10_000:
                seen.pop(next(iter(seen)))
            safe_call(callback, item)

        def loop(stop: threading.Event) -> None:
            cursor = start
            backoff = 1.0
            while not stop.is_set():
                try:
                    items, cursor = self.list(since=cursor, limit=100)
                    for item in items:
                        deliver(item)
                    if not items and not stop.is_set():
                        self._long_poll(cursor, self.subscribe_wait)
                    backoff = 1.0
                except TransportError:
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)

        with self._lock:
            self._subscribers.append(deliver)
        sub = ThreadSubscription(loop, name=f"primitive-subscribe-{self.address}")
        mailbox = self

        class _Sub:
            def close(self) -> None:
                with mailbox._lock:
                    if deliver in mailbox._subscribers:
                        mailbox._subscribers.remove(deliver)
                sub.close()

        return _Sub()

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    # ------------------------------------------------------------ webhook intake (WebhookVerifier)

    def verify(self, headers: Mapping[str, str], body: bytes, now: Ms) -> bool:
        header = _header(headers, "Primitive-Signature") or _header(headers, "MyMX-Signature")
        return verify_signature(self.webhook_secret or "", header, body, now / 1000.0) is None

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[Received]:
        """`email.received` webhook -> the delivered message as raw MIME (other events -> [])."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError("webhook body is not JSON") from exc
        if not isinstance(payload, dict):
            raise TransportError("webhook payload must be a JSON object")
        name = event_name(payload)
        if name and name != "email.received":
            return []
        record = unwrap_webhook(payload)
        pid = str(record.get("id") or "")
        recipient = str(record.get("recipient") or record.get("to_email") or "").lower()
        smtp = record.get("smtp") if isinstance(record.get("smtp"), dict) else {}
        rcpt_to = [str(r).lower() for r in (smtp.get("rcpt_to") or [])] if isinstance(smtp, dict) else []
        if (recipient and recipient != self.address.lower()) or (rcpt_to and self.address.lower() not in rcpt_to and not recipient):
            return []
        raw = _inline_raw(record)
        if raw is None:
            url = ((record.get("content") or {}).get("download") or {}).get("url") if isinstance(record.get("content"), dict) else None
            if url:
                resp = self._client.get(url, timeout=120.0)
                raw = resp.content if resp.status_code < 400 else None
        if raw is None:
            if not pid:
                raise TransportError("webhook carries neither raw MIME nor an email id")
            raw = self._raw(pid)
        auth_data = record.get("auth") if isinstance(record.get("auth"), dict) else None
        if auth_data is None and pid:
            auth_data = (_data(self._request("GET", f"/emails/{pid}")) or {}).get("auth")
        when = parse_time(record.get("received_at")) or parse_time(record.get("created_at")) or int(self.clock() * 1000)
        return [Received(raw_mime=self._with_auth(raw, auth_data), received_at=when, provider_id=pid, cursor="")]

    def accept_webhook(self, headers: Mapping[str, str], body: bytes, now: Ms | None = None) -> list[Received]:
        """Verify + parse, then hand each message to every subscriber. Raises TransportError on a
        bad signature."""
        if not self.verify(headers, body, int(self.clock() * 1000) if now is None else now):
            raise TransportError("webhook signature does not verify")
        items = self.parse(headers, body)
        with self._lock:
            subs = list(self._subscribers)
        for item in items:
            for deliver in subs:
                deliver(item)
        return items

    def fetch_webhook_secret(self) -> str:
        data = _data(self._request("GET", "/account/webhook-secret")) or {}
        secret = str(data.get("secret") or "").strip()
        if not secret:
            raise TransportError("Primitive returned no webhook secret")
        self.webhook_secret = secret
        return secret

    # ------------------------------------------------------------ internals

    def head(self) -> str:
        """Tail cursor positioned after the newest message for this address."""
        body = self._request("GET", "/emails", params={"recipient": self.address, "limit": 1})
        rows = _data(body) or []
        if not rows:
            return encode_cursor(EPOCH_CURSOR, 0)
        newest = parse_time(rows[0].get("created_at")) or parse_time(rows[0].get("received_at")) or 0
        cursor = encode_cursor(f"{_iso(max(0, newest - 5000))}|00000000-0000-0000-0000-000000000000", 0)
        while True:
            rows, _positions, end = self._rows(cursor, 10_000)
            if not rows:
                return end or cursor
            cursor = end or cursor

    def _rows(self, since: str | None, limit: int) -> tuple[list[dict], list[str], str | None]:
        """Rows after `since` (oldest first), the cursor after each, and the cursor after all."""
        base, skip = decode_cursor(since)
        rows: list[dict] = []
        positions: list[str] = []
        end: str | None = None
        while len(rows) < limit:
            want = min(_PAGE, skip + (limit - len(rows)))
            body = self._request("GET", "/emails", params={"recipient": self.address, "since": base, "limit": want})
            page = _data(body) or []
            nxt = (body.get("meta") or {}).get("cursor") if isinstance(body, dict) else None
            fresh = page[skip:]
            take = fresh[:limit - len(rows)]
            for i, row in enumerate(take):
                pos = skip + i + 1
                rows.append(row)
                positions.append(encode_cursor(nxt, 0) if (pos == len(page) and nxt) else encode_cursor(base, pos))
            if take:
                end = positions[-1]
            elif nxt and page and len(page) == skip:
                end = encode_cursor(nxt, 0)  # the whole page was already seen: move to its exact end
            if len(take) < len(fresh) or len(page) < want or not nxt:
                break
            base, skip = nxt, 0
        return rows, positions, end

    def _long_poll(self, cursor: str | None, seconds: int) -> None:
        base, skip = decode_cursor(cursor)
        if skip:
            # Inside a page: the rows we skip are already visible, so a long-poll would return at once.
            self._sleep(min(1.0, float(seconds)))
            return
        started = time.monotonic()
        try:
            self._request("GET", "/emails", params={"recipient": self.address, "since": base,
                                                    "wait": int(seconds), "limit": 1},
                          timeout=float(seconds) + 30.0)
        except TransportError:
            self._sleep(1.0)
            return
        elapsed = time.monotonic() - started
        if elapsed < 0.05 and seconds >= 1:
            self._sleep(0.2)  # a proxy or mock that ignores `wait`: don't spin

    def _raw(self, pid: str) -> bytes:
        resp = self._request("GET", f"/emails/{pid}/raw", raw=True, timeout=300.0)
        raw = resp.content
        want = resp.headers.get("X-Content-SHA256")
        if want and hashlib.sha256(raw).hexdigest() != want.strip().lower():
            raise PrimitiveError("integrity_error", f"raw MIME of {pid} does not match X-Content-SHA256")
        return raw

    def _detail(self, pid: str) -> dict:
        detail = _data(self._request("GET", f"/emails/{pid}")) or {}
        for _ in range(10):
            if str(detail.get("status") or "") != "pending":
                break
            self._sleep(0.5)
            detail = _data(self._request("GET", f"/emails/{pid}")) or {}
        return detail if isinstance(detail, dict) else {}

    def _with_auth(self, raw: bytes, auth_data: Mapping[str, Any] | None) -> bytes:
        try:
            sender = mime.parse(raw).from_addr
        except mime.MimeError:
            return raw
        if "@" not in sender:
            return raw
        domain = sender.rsplit("@", 1)[1].lower()
        return auth.prepend(raw, auth.synthesize(self.authserv_id, dmarc_for(auth_data, domain), domain))

    def _received(self, row: dict, cursor: str) -> Received:
        pid = str(row.get("id") or "")
        detail = self._detail(pid)
        raw = self._raw(pid)
        when = parse_time(row.get("received_at")) or parse_time(detail.get("received_at")) \
            or parse_time(row.get("created_at")) or int(self.clock() * 1000)
        return Received(raw_mime=self._with_auth(raw, detail.get("auth")), received_at=when,
                        provider_id=pid, cursor=cursor)


def _inline_raw(record: dict) -> bytes | None:
    """Raw MIME carried inline by a webhook payload, in the shapes Primitive has used."""
    content = record.get("content") if isinstance(record.get("content"), dict) else {}
    candidates = [content.get("raw"), record.get("raw"), record.get("raw_mime")]
    for cand in candidates:
        if isinstance(cand, dict):
            if cand.get("included") is False:
                continue
            data = cand.get("data")
            encoding = str(cand.get("encoding") or "base64").lower()
        else:
            data, encoding = cand, "base64"
        if not isinstance(data, str) or not data:
            continue
        try:
            raw = base64.b64decode(data, validate=False) if encoding == "base64" else data.encode("utf-8")
        except (ValueError, TypeError):
            continue
        sha = cand.get("sha256") if isinstance(cand, dict) else None
        if sha and hashlib.sha256(raw).hexdigest() != str(sha).lower():
            continue
        return raw
    return None


def _data(body: Any) -> Any:
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _error_from_response(resp: httpx.Response) -> PrimitiveError:
    message = (resp.text or "")[:500]
    code = f"http_{resp.status_code}"
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        code = str(err.get("code") or code)
        message = str(err.get("message") or message)
    if resp.status_code == 429 or "quota" in code or "rate_limit" in code:
        return PrimitiveQuotaError(code, message, status=resp.status_code)
    return PrimitiveError(code, message, status=resp.status_code)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    header = (resp.headers.get("Retry-After") or "").strip()
    if header:
        try:
            return min(300.0, max(0.0, float(header)))
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return min(300.0, max(0.0, when.timestamp() - time.time()))
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
    return min(8.0, 0.5 * (2 ** attempt))


def from_config(cfg: Mapping[str, str]) -> PrimitiveMailbox:
    """Keys: api_key, address; optional base_url, webhook_secret, authserv_id,
    send_quota_per_day, max_message_bytes, subscribe_wait."""
    if not cfg.get("api_key"):
        raise TransportError("primitive needs api_key")
    if not cfg.get("address"):
        raise TransportError("primitive needs address")
    return PrimitiveMailbox(
        cfg["api_key"], cfg["address"], base_url=cfg.get("base_url") or DEFAULT_BASE_URL,
        webhook_secret=cfg.get("webhook_secret") or None,
        authserv_id=cfg.get("authserv_id") or AUTHSERV_ID,
        send_quota_per_day=cfg_int(cfg, "send_quota_per_day", None),
        max_message_bytes=cfg_int(cfg, "max_message_bytes", MAX_MESSAGE_BYTES) or MAX_MESSAGE_BYTES,
        subscribe_wait=cfg_int(cfg, "subscribe_wait", 10) or 10,
    )

