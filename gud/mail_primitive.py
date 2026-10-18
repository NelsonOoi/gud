"""Primitive (primitive.dev) mailbox. Same interface as gud.mail_fake.FileMailbox.

Field mapping (OpenAPI schemas at docs/primitive-openapi.json):

- ``Email.message_id`` is the RFC 5322 Message-ID (``EmailDetail.message_id`` /
  ``SentEmailSummary.message_id``), wrapped in angle brackets.
- ``Email.provider_id`` is Primitive's row id (``emails.id`` or ``sent_emails.id``).
- Inbound ``from`` is ``from_email`` (fallback ``from_address``, ``from_header``, ``sender``).
- ``to`` / ``cc`` come from ``parsed.to_addresses`` and ``parsed.cc`` (``EmailAddress``
  objects), else ``to_email`` / ``recipient`` / ``to_addresses`` / ``cc``.
- Threading comes from ``parsed.in_reply_to`` and ``parsed.references`` (sent mail
  uses the wire headers ``in_reply_to`` and ``email_references``).
- ``received_at`` is ``received_at``, or ``created_at`` on a sent-email row.
- ``sender_verified`` is the boolean of that name when Primitive sends one
  (``ReplyEmail.sender_verified``). Otherwise it is true when ``auth`` shows DMARC
  pass with SPF or DKIM alignment, or an aligned DKIM pass (``EmailAuth``), or
  ``sender_connected_agent_verified``. Mail Primitive sent for us from our own
  domain (DKIM/SPF for that domain, ``dkim_domain``, or a ``relay`` object) counts
  as verified. ``send`` returns mail we just handed to the relay, so it is verified.
"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .types import Email

DEFAULT_BASE_URL = "https://api.primitive.dev/v1"
_NIL_ID = "00000000-0000-0000-0000-000000000000"
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_REPLY_ONLY = (
    "This Primitive account is still reply-only and must be upgraded before it "
    "can send to new recipients. It can send only to addresses that have already "
    "sent it authenticated mail. Upgrade by confirming an email through the claim flow"
)


class PrimitiveError(Exception):
    """A Primitive API error. ``code`` is the stable ``error.code`` string."""

    def __init__(self, code: str, message: str, *, status: int | None = None):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


class PrimitiveMailbox:
    def __init__(self, api_key: str, repo_address: str, base_url: str = DEFAULT_BASE_URL,
                 client: httpx.Client | None = None, list_ttl: float = 0.0):
        """``list_ttl`` > 0 lets threads sharing this client reuse a listing that young.

        Every agent thread in a swarm shares one client; without it each thread lists the
        repo several times a second and the account hits Primitive's 3000 requests/minute.
        """
        self.api_key = api_key
        self.repo_address = repo_address
        self.base_url = base_url.rstrip("/")
        self.list_ttl = list_ttl
        self._client = client or httpx.Client(timeout=httpx.Timeout(90.0, connect=10.0))
        self._bodies: dict[str, str] = {}
        self._by_provider: dict[str, Email] = {}
        self._by_message_id: dict[str, Email] = {}
        self._tail_cursor: dict[str, str] = {}
        self._listed: dict[tuple[str, float | None], tuple[float, list[Email]]] = {}
        self._lock = threading.Lock()

    def send(self, *, from_addr: str, to: str, cc: list[str] | None = None, subject: str,
             body: str, in_reply_to: str | None = None,
             references: list[str] | None = None) -> Email:
        payload: dict = {"from": from_addr, "to": to, "subject": subject, "body_text": body}
        if cc:
            payload["cc"] = list(cc)
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to
        if references:
            payload["references"] = list(references)
        env = self._request("POST", "/send-mail", json_body=payload,
                            headers={"Idempotency-Key": str(uuid.uuid4())})
        result = dict(_data(env) or {})
        if result.get("id") and not _sent_complete(result):
            try:
                detail = _data(self._request("GET", f"/sent-emails/{result['id']}"))
                if isinstance(detail, dict):
                    result = {**result, **detail}
            except PrimitiveError:
                pass
        if result.get("id") and not result.get("message_id") and _domain(from_addr):
            # Primitive delivers our own sends as <primitive-direct-{sent id}@{sender domain}>.
            result["message_id"] = f"<primitive-direct-{result['id']}@{_domain(from_addr)}>"
        email = self._from_record(result, outbound=True, fallback={
            "from_addr": from_addr, "to": to, "cc": list(cc or []), "subject": subject,
            "body": body, "in_reply_to": in_reply_to, "references": list(references or []),
        })
        self._remember(email)
        with self._lock:
            self._listed.clear()
        return email

    def list(self, *, recipient: str | None = None, since: float | None = None) -> list[Email]:
        recipient = (recipient or self.repo_address).lower()
        if self.list_ttl > 0:
            with self._lock:
                hit = self._listed.get((recipient, since))
            if hit is not None and time.monotonic() - hit[0] < self.list_ttl:
                return list(hit[1])
        emails = self._rows_to_emails(self._iter_rows(recipient, since))
        if since is not None:
            emails = [e for e in emails if e.received_at > since]
        emails.sort(key=lambda e: (e.received_at, e.message_id))
        if self.list_ttl > 0:
            with self._lock:
                self._listed[(recipient, since)] = (time.monotonic(), emails)
        return emails

    def wait(self, *, recipient: str, since: float, timeout: float) -> list[Email]:
        """Long-poll ``GET /emails`` with ``wait`` (max 30s per call) until ``timeout``."""
        recipient = recipient.lower()
        deadline = time.monotonic() + max(0.0, timeout)
        found = self.list(recipient=recipient, since=since)
        if found or time.monotonic() >= deadline:
            return found
        cursor = self._tail_cursor.get(recipient) or _time_cursor(since)
        for _ in range(100):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                return []
            wait_param = min(30, max(1, math.ceil(remaining)))
            started = time.monotonic()
            body = self._request("GET", "/emails", params={
                "recipient": recipient, "since": cursor, "wait": wait_param, "limit": 100,
            }, timeout=wait_param + 15)
            elapsed = time.monotonic() - started
            new_cursor = (body.get("meta") or {}).get("cursor") if isinstance(body, dict) else None
            if new_cursor:
                cursor = new_cursor
                self._tail_cursor[recipient] = cursor
            rows = body.get("data") or [] if isinstance(body, dict) else []
            fresh = [e for e in self._rows_to_emails(rows) if e.received_at > since]
            if fresh:
                fresh.sort(key=lambda e: (e.received_at, e.message_id))
                return fresh
            # A live server holds the socket for `wait` seconds. An instant empty
            # page (tests, or a proxy that dropped the long-poll) still consumes
            # that window so we don't spin.
            if elapsed < min(0.5, wait_param / 2.0):
                deadline -= float(wait_param)
        return []

    def get(self, message_id: str) -> Email | None:
        angled = _angle(message_id)
        if message_id in self._by_message_id:
            return self._by_message_id[message_id]
        if angled and angled in self._by_message_id:
            return self._by_message_id[angled]
        if message_id in self._by_provider:
            return self._by_provider[message_id]
        if _UUID.match(message_id or ""):
            for path, outbound in ((f"/emails/{message_id}", False), (f"/sent-emails/{message_id}", True)):
                try:
                    data = _data(self._request("GET", path))
                except PrimitiveError as exc:
                    if exc.status == 404 or exc.code in {"not_found", "http_404"}:
                        continue
                    raise
                if isinstance(data, dict):
                    email = self._from_record(data, outbound=outbound)
                    self._remember(email)
                    return email
        for row in self._iter_rows(self.repo_address.lower(), None):
            if _angle(row.get("message_id")) == angled:
                return self._materialize(row)
        return None

    def parse_webhook(self, payload: dict) -> Email:
        """Parse an inbound webhook event (``email.received`` and the same email shape)."""
        if not isinstance(payload, dict):
            raise PrimitiveError("invalid_webhook", "webhook payload must be a JSON object")
        record = _unwrap_webhook(payload)
        if not isinstance(record, dict) or not (
            record.get("id") or record.get("message_id") or record.get("from_email") or record.get("sender")
        ):
            raise PrimitiveError("invalid_webhook", "inbound webhook payload has no email")
        email = self._from_record(record, outbound=False)
        self._remember(email)
        return email

    def _iter_rows(self, recipient: str, since: float | None):
        params: dict = {"recipient": recipient, "limit": 100}
        if since is not None:
            params["date_from"] = _iso_floor(since)
        cursor = None
        seen: set[str] = set()
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            body = self._request("GET", "/emails", params=page_params)
            data = body.get("data") if isinstance(body, dict) else None
            for row in data or []:
                yield row
            cursor = (body.get("meta") or {}).get("cursor") if isinstance(body, dict) else None
            if not cursor or cursor in seen:
                break
            seen.add(cursor)

    def _materialize(self, row: dict) -> Email | None:
        """The full email for a list row, or None when its body could not be fetched.

        A row without a body is never cached: a snippet has no trailers, and caching it
        would hide the email from replay for the life of this client.
        """
        pid = str(row.get("id") or "")
        if pid and pid in self._by_provider and row.get("body_text") is None:
            return self._by_provider[pid]
        if row.get("body_text") is None and pid:
            if pid in self._bodies:
                row = {**row, "body_text": self._bodies[pid]}
            else:
                try:
                    detail = _data(self._request("GET", f"/emails/{pid}"))
                except PrimitiveError:
                    return None
                if not isinstance(detail, dict):
                    return None
                row = {**row, **detail}
                if row.get("body_text") is None and not (row.get("parsed") or {}).get("body_text"):
                    return None
        email = self._from_record(row, outbound=False)
        self._remember(email)
        return email

    def _rows_to_emails(self, rows) -> list[Email]:
        out = []
        for row in rows:
            email = self._materialize(row)
            if email is not None:
                out.append(email)
        return out

    def _from_record(self, record: dict, *, outbound: bool, fallback: dict | None = None) -> Email:
        fallback = fallback or {}
        parsed = record.get("parsed") if isinstance(record.get("parsed"), dict) else {}
        from_addr = (
            record.get("from_email") or record.get("from_address")
            or _bare(record.get("from_header") or "") or _bare(record.get("from") or "")
            or record.get("sender") or fallback.get("from_addr") or ""
        )
        to_addrs = _addr_list(parsed.get("to_addresses")) or _addr_list(record.get("to_addresses"))
        if not to_addrs:
            one = record.get("to_email") or record.get("to_address") or record.get("recipient") or record.get("to")
            to_addrs = [_bare(one)] if one else ([fallback["to"]] if fallback.get("to") else [])
        cc_addrs = _addr_list(parsed.get("cc"))
        if not cc_addrs:
            cc_addrs = _addr_list(record.get("cc")) if record.get("cc") is not None else list(fallback.get("cc") or [])
        subject = record.get("subject")
        if subject is None:
            subject = fallback.get("subject") or ""
        body = record.get("body_text")
        if body is None:
            body = parsed.get("body_text")
        if body is None:
            body = fallback.get("body")
        if body is None:
            body = ""
        in_reply = _first_id(parsed.get("in_reply_to"))
        if in_reply is None:
            in_reply = _first_id(record.get("in_reply_to"))
        if in_reply is None:
            in_reply = _angle(fallback.get("in_reply_to")) if fallback.get("in_reply_to") else None
        refs = _ref_list(parsed.get("references"))
        if not refs:
            refs = _ref_list(record.get("references") if record.get("references") is not None
                             else record.get("email_references"))
        if not refs:
            refs = _ref_list(fallback.get("references"))
        if not refs and in_reply:
            refs = [in_reply]
        received = _parse_time(record.get("received_at")) or _parse_time(record.get("created_at"))
        if received is None:
            received = time.time()
        provider_id = str(record["id"]) if record.get("id") else None
        message_id = _angle(record.get("message_id"))
        if not message_id:
            host = "sent.primitive.dev" if outbound else "inbound.primitive.dev"
            message_id = _angle(f"{provider_id or uuid.uuid4().hex}@{host}")
        return Email(
            message_id=message_id, from_addr=from_addr, to=tuple(to_addrs), cc=tuple(cc_addrs),
            subject=subject, body=body, received_at=received,
            sender_verified=self._sender_verified(record, from_addr, outbound=outbound),
            in_reply_to=in_reply, references=tuple(refs), provider_id=provider_id,
        )

    def _sender_verified(self, record: dict, from_addr: str, *, outbound: bool) -> bool:
        if outbound:
            return True
        if isinstance(record.get("sender_verified"), bool):
            return record["sender_verified"]
        if record.get("sender_connected_agent_verified") is True:
            return True
        auth = record.get("auth") if isinstance(record.get("auth"), dict) else {}
        if _auth_verified(auth):
            return True
        own = _domain(self.repo_address)
        if own and _domain(from_addr) == own and _primitive_own_send(record, auth, own):
            return True
        return False

    def _remember(self, email: Email) -> None:
        with self._lock:
            if email.provider_id:
                self._by_provider[email.provider_id] = email
                self._bodies[email.provider_id] = email.body
            if email.message_id:
                self._by_message_id[email.message_id] = email

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None, headers: dict | None = None,
                 timeout: float = 60.0) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        hdrs = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        last: PrimitiveError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            resp = self._client.request(method, url, headers=hdrs, params=query or None,
                                        json=json_body, timeout=timeout)
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
                last = _error_from_response(resp)
                time.sleep(_retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise _error_from_response(resp)
            try:
                body = resp.json()
            except Exception as exc:
                raise PrimitiveError("invalid_response",
                                    f"Primitive returned a non-JSON {resp.status_code} response",
                                    status=resp.status_code) from exc
            if isinstance(body, dict) and body.get("success") is False:
                raise _error_from_response(resp)
            return body
        assert last is not None
        raise last


def _data(body):
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _sent_complete(result: dict) -> bool:
    return bool(result.get("message_id")) and bool(result.get("created_at") or result.get("received_at")) \
        and result.get("body_text") is not None


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
    if code == "recipient_not_allowed":
        message = f"{_REPLY_ONLY}. ({message})" if message else _REPLY_ONLY
    return PrimitiveError(code, message, status=resp.status_code)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("Retry-After")
    if header:
        parsed = _parse_retry_after(header)
        if parsed is not None:
            return parsed
    return min(8.0, 0.5 * (2 ** attempt))


def _parse_retry_after(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return min(300.0, max(0.0, float(value)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return min(300.0, max(0.0, when.timestamp() - time.time()))


def _unwrap_webhook(payload: dict) -> dict:
    email = payload.get("email")
    if isinstance(email, dict):
        return email
    data = payload.get("data")
    if isinstance(data, dict):
        inner = data.get("email")
        return inner if isinstance(inner, dict) else data
    return payload


def _auth_verified(auth: dict) -> bool:
    dmarc = str(auth.get("dmarc") or "").lower()
    if dmarc == "pass" and (auth.get("dmarcSpfAligned") or auth.get("dmarcDkimAligned")):
        return True
    for sig in auth.get("dkimSignatures") or []:
        if isinstance(sig, dict) and str(sig.get("result") or "").lower() == "pass" and sig.get("aligned"):
            return True
    return False


def _primitive_own_send(record: dict, auth: dict, own: str) -> bool:
    """Mail Primitive submitted for our domain: relay delivery, or SPF/DKIM for that domain."""
    if record.get("relay"):
        return True
    for sig in auth.get("dkimSignatures") or []:
        if isinstance(sig, dict) and str(sig.get("result") or "").lower() == "pass" \
                and _domain(str(sig.get("domain") or "")) == own:
            return True
    if _domain(str(record.get("dkim_domain") or "")) == own:
        return True
    spf = str(auth.get("spf") or "").lower()
    if spf == "pass" and (auth.get("dmarcSpfAligned") or _domain(str(auth.get("dmarcFromDomain") or "")) == own):
        return True
    return False


def _addr_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        bare = _bare(value)
        return [bare] if bare else []
    out = []
    for item in value:
        if isinstance(item, str):
            bare = _bare(item)
        elif isinstance(item, dict):
            bare = _bare(item.get("address") or item.get("email") or "")
        else:
            bare = ""
        if bare:
            out.append(bare)
    return out


def _ref_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        angled = _angle(item if isinstance(item, str) else str(item))
        if angled:
            out.append(angled)
    return out


def _first_id(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return _angle(value) if value else None


def _bare(value: str) -> str:
    text = (value or "").strip()
    if "<" in text and ">" in text:
        text = text[text.rfind("<") + 1:text.rfind(">")]
    return text.strip()


def _domain(addr: str) -> str:
    bare = _bare(addr).lower()
    return bare.rsplit("@", 1)[-1] if "@" in bare else ""


def _angle(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("<") and text.endswith(">"):
        return text
    return f"<{text.strip('<>')}>"


def _parse_time(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _iso_floor(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_cursor(ts: float) -> str:
    """Best-effort forward cursor. Primitive's real ``since`` value is ``meta.cursor``."""
    return f"{_iso_floor(ts)}|{_NIL_ID}"
