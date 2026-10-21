"""Postmark mailbox: `POST /email` out, inbound webhook (with `RawEmail`) in.

Outbound: Postmark has no raw-send endpoint, so the adapter renders the message the way
`primitive.py` does: `From`, `To`, `Cc`, `Subject`, `TextBody` (the human part), the envelope as
an attachment `{Name: "gud.env", Content: <base64>, ContentType: application/octet-stream}`
(the bytes arrive exactly), and `Message-ID` / `In-Reply-To` / `References` in `Headers`. A
raw-bytes `send` is re-rendered from its parsed fields (spec note 10). Postmark builds the MIME
and sets the delivered Message-ID to `<MessageID@mtasv.net>`; `Sent.message_id` reports that
(set `preserves_message_id` for a server where Postmark keeps a custom Message-ID header).
Postmark's documented size cap is 10 MB per message including attachments.

Inbound: Postmark POSTs a JSON document per message to the server's inbound webhook URL. It
carries `RawEmail` (the full RFC 5322 source) only when the server's "Include raw email content
in JSON payload" setting is on (`RawEmailEnabled` in `GET /server`); the adapter requires it
and reports `raw_mime=False` otherwise. Postmark does not sign webhooks: authenticate them with
HTTP Basic credentials in the webhook URL (`https://user:pass@host/hook`) and, optionally, the
documented source-IP allow-list (`allowed_ips = postmark`). `verify` refuses everything when
neither is configured.

Accepted messages go into a local `Spool` (a cache of delivered mail); `list`/`wait`/
`subscribe` read it. `catch_up()` pages `GET /messages/inbound` (newest first) until it reaches
messages already stored and ingests the missing ones from `GET /messages/inbound/{id}/details`
(their `RawEmail`). Received time is the local time the webhook arrived (Postmark's webhook has
no delivery timestamp), or the search result's `ReceivedAt` for catch-up copies.

Authentication-Results: Postmark exposes no DMARC verdict for inbound mail, so the adapter
synthesizes none and reports `auth_results="none"`. Unless the operator pins the authserv-id of
an MTA that writes a topmost Authentication-Results before Postmark (config `authserv_id`), no
unsigned human message counts on a Postmark repo; humans sign (§8.3 last paragraph).
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from gud.core.types import Ms

from . import mime
from .base import (
    Capabilities, Outgoing, QuotaExceeded, Received, Sent, Subscription, ThreadSubscription,
    TransportError, cfg_bool, cfg_int, cfg_list, safe_call,
)
from .httputil import RETRY_STATUSES, header, parse_iso_ms, retry_delay
from .spool import Spool

DEFAULT_BASE_URL = "https://api.postmarkapp.com"
MAX_MESSAGE_BYTES = 10_000_000
MESSAGE_ID_DOMAIN = "mtasv.net"
# Postmark's documented webhook source addresses (https://postmarkapp.com/support/article/800-ips-for-firewalls).
WEBHOOK_IPS = ("3.134.147.250", "50.31.156.6", "50.31.156.77", "18.217.206.57")
REMOTE_ADDR_HEADER = "X-Gud-Remote-Addr"      # set by the webhook server from the socket peer
_QUOTA_CODES = {405, 429}
_MAX_ATTEMPTS = 4
_SEARCH_MAX = 10_000                          # Postmark: count + offset <= 10 000


class PostmarkError(TransportError):
    def __init__(self, code: int, message: str, *, status: int | None = None):
        self.code, self.message, self.status = code, message, status
        super().__init__(f"{code}: {message}")


class PostmarkQuotaError(PostmarkError, QuotaExceeded):
    """Out of credits (ErrorCode 405) or rate limited (HTTP 429)."""


def basic_auth_header(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")


def _ip_allowed(addr: str | None, allowed: tuple[str, ...]) -> bool:
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return False
    for net in allowed:
        try:
            if ip in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


class PostmarkMailbox:
    def __init__(self, server_token: str, address: str, *, base_url: str = DEFAULT_BASE_URL,
                 client: httpx.Client | None = None, store: Path | str | None = None,
                 message_stream: str = "outbound", webhook_username: str | None = None,
                 webhook_password: str | None = None, allowed_ips: tuple[str, ...] = (),
                 inbound_aliases: tuple[str, ...] = (), authserv_id: str | None = None,
                 max_message_bytes: int = MAX_MESSAGE_BYTES, send_quota_per_day: int | None = None,
                 preserves_message_id: bool = False, catchup_interval: float = 60.0,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.server_token = server_token
        self.address = address
        self.base_url = base_url.rstrip("/")
        self.message_stream = message_stream
        self.webhook_username = webhook_username
        self.webhook_password = webhook_password
        self.allowed_ips = tuple(allowed_ips)
        self.recipients = tuple(a.lower() for a in (address, *inbound_aliases))
        self.authserv_id = authserv_id
        self.max_message_bytes = max_message_bytes
        self.preserves_message_id = preserves_message_id
        self.catchup_interval = catchup_interval
        self.clock = clock
        self._sleep = sleep
        self._quota = send_quota_per_day
        self._raw_enabled: bool | None = None
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        if store is None:
            home = os.environ.get("GUD_HOME") or str(Path.home() / ".gud")
            store = Path(home) / "inbound" / "postmark" / address.lower()
        self.spool = Spool(store)

    # ------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None,
                 timeout: float = 60.0) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        hdrs = {"X-Postmark-Server-Token": self.server_token, "Accept": "application/json"}
        last: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = self._client.request(method, url, headers=hdrs, params=params, json=json_body, timeout=timeout)
            except httpx.HTTPError as exc:
                last = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    self._sleep(min(8.0, 0.5 * 2 ** attempt))
                    continue
                raise PostmarkError(-1, f"network error: {exc}") from exc
            if resp.status_code in RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
                self._sleep(retry_delay(resp, attempt))
                continue
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.status_code >= 400 or (isinstance(body, dict) and int(body.get("ErrorCode") or 0) != 0):
                code = int(body.get("ErrorCode") or resp.status_code) if isinstance(body, dict) else resp.status_code
                message = str(body.get("Message") or "") if isinstance(body, dict) else (resp.text or "")[:300]
                cls = PostmarkQuotaError if (code in _QUOTA_CODES or resp.status_code == 429) else PostmarkError
                raise cls(code, message, status=resp.status_code)
            return body
        raise PostmarkError(-1, f"network error: {last}")

    # ------------------------------------------------------------ contract

    def capabilities(self) -> Capabilities:
        if self._raw_enabled is None:
            try:
                server = self._request("GET", "/server") or {}
                self._raw_enabled = bool(server.get("RawEmailEnabled", True))
            except TransportError:
                self._raw_enabled = True
        return Capabilities(
            name="postmark", max_message_bytes=self.max_message_bytes, push="webhook",
            preserves_message_id=self.preserves_message_id,
            auth_results="header" if self.authserv_id else "none", authserv_id=self.authserv_id,
            send_quota_per_day=self._quota, received_time_resolution_ms=1, raw_mime=self._raw_enabled,
        )

    def send(self, message: Outgoing | bytes) -> Sent:
        if isinstance(message, (bytes, bytearray)):
            message = mime.outgoing_from_raw(bytes(message))
        if not isinstance(message, Outgoing):
            raise TypeError("send() takes an Outgoing or raw bytes")
        if not [a for a in message.to if a]:
            raise TransportError("message has no recipients")
        size = len(mime.build(message))
        if size > self.max_message_bytes:
            raise PostmarkError(300, f"message is about {size} bytes; Postmark takes at most {self.max_message_bytes}")
        message_id = message.message_id or mime.make_message_id(message.from_addr)
        headers = [{"Name": "Message-ID", "Value": message_id}]
        if message.in_reply_to:
            headers.append({"Name": "In-Reply-To", "Value": message.in_reply_to})
        if message.references:
            headers.append({"Name": "References", "Value": " ".join(message.references)})
        payload: dict[str, Any] = {
            "From": message.from_addr, "To": ", ".join(a for a in message.to if a),
            "Subject": (message.subject or "").replace("\r", " ").replace("\n", " "),
            "TextBody": message.text or " ", "Headers": headers, "MessageStream": self.message_stream,
            "TrackOpens": False, "TrackLinks": "None",
        }
        if message.cc:
            payload["Cc"] = ", ".join(message.cc)
        if message.envelope is not None:
            payload["Attachments"] = [{"Name": mime.ENV_FILENAME, "ContentType": "application/octet-stream",
                                       "Content": base64.b64encode(message.envelope).decode("ascii")}]
        body = self._request("POST", "/email", json_body=payload, timeout=300.0) or {}
        pid = str(body.get("MessageID") or "") or None
        delivered = message_id if (self.preserves_message_id or not pid) else f"<{pid}@{MESSAGE_ID_DOMAIN}>"
        accepted = parse_iso_ms(body.get("SubmittedAt")) or int(self.clock() * 1000)
        return Sent(provider_id=pid, message_id=delivered, accepted_at=accepted)

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        return self.spool.list(since=since, limit=limit)

    def head(self) -> str | None:
        return self.spool.head()

    def wait(self, *, since: str | None, timeout: float) -> list[Received]:
        return self.spool.wait(since=since, timeout=timeout, on_idle=self.catch_up, idle_every=self.catchup_interval)

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription:
        start = since if since is not None else self.head()

        def loop(stop: threading.Event) -> None:
            cursor = start
            next_catch_up = time.monotonic() + self.catchup_interval
            while not stop.is_set():
                if time.monotonic() >= next_catch_up:
                    try:
                        self.catch_up()
                    except TransportError:
                        pass
                    next_catch_up = time.monotonic() + self.catchup_interval
                items, cursor = self.spool.list(since=cursor, limit=100)
                for item in items:
                    if stop.is_set():
                        return
                    safe_call(callback, item)
                if not items:
                    with self.spool._cond:
                        self.spool._cond.wait(self.spool.poll_interval)

        return ThreadSubscription(loop, name=f"postmark-spool-{self.address}")

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    # ------------------------------------------------------------ webhook (WebhookVerifier)

    def verify(self, headers: Mapping[str, str], body: bytes, now: Ms, *, remote_addr: str | None = None) -> bool:
        """Basic credentials (when configured) and source IP (when an allow-list is configured).
        With neither configured nothing verifies. `remote_addr` defaults to the
        `X-Gud-Remote-Addr` header, which the webhook server sets from the socket peer."""
        if not (self.webhook_username or self.webhook_password) and not self.allowed_ips:
            return False
        if self.webhook_username or self.webhook_password:
            given = header(headers, "Authorization") or ""
            want = basic_auth_header(self.webhook_username or "", self.webhook_password or "")
            if not hmac.compare_digest(given.encode(), want.encode()):
                return False
        if self.allowed_ips:
            if not _ip_allowed(remote_addr or header(headers, REMOTE_ADDR_HEADER), self.allowed_ips):
                return False
        return True

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[Received]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError("Postmark webhook body is not JSON") from exc
        if not isinstance(payload, dict):
            raise TransportError("Postmark webhook payload must be a JSON object")
        if not self._for_us(payload):
            return []
        raw = payload.get("RawEmail")
        if not isinstance(raw, str) or not raw:
            raise TransportError("Postmark inbound webhook has no RawEmail; enable \"Include raw email content in "
                                 "JSON payload\" in the server's inbound settings")
        pid = str(payload.get("MessageID") or "")
        if not pid:
            raise TransportError("Postmark inbound webhook has no MessageID")
        when = parse_iso_ms(payload.get("ReceivedAt")) or int(self.clock() * 1000)
        return [Received(raw_mime=raw.encode("utf-8", "surrogateescape"), received_at=when, provider_id=pid, cursor="")]

    def accept_webhook(self, headers: Mapping[str, str], body: bytes, now: Ms | None = None, *,
                       remote_addr: str | None = None) -> list[Received]:
        """Verify, parse and store. Raises TransportError when the request does not verify (the
        route should answer 401/403 then; Postmark retries non-2xx answers)."""
        if not self.verify(headers, body, int(self.clock() * 1000) if now is None else now, remote_addr=remote_addr):
            raise TransportError("Postmark webhook does not verify (Basic credentials or source IP)")
        out = []
        for item in self.parse(headers, body):
            stored = self.spool.ingest(item.raw_mime, item.received_at, item.provider_id, source="webhook")
            if stored is not None:
                out.append(stored)
        return out

    def catch_up(self) -> int:
        """Ingest inbound messages the webhook missed, from Postmark's message search."""
        have = self.spool.ids()
        missing: list[dict] = []
        offset, count = 0, 100
        while offset + count <= _SEARCH_MAX:
            page = (self._request("GET", "/messages/inbound", params={
                "count": count, "offset": offset, "recipient": self.address}) or {}).get("InboundMessages") or []
            fresh = [m for m in page if str(m.get("MessageID") or "") not in have]
            missing.extend(fresh)
            if len(page) < count or not fresh:
                break
            offset += count
        added = 0
        for summary in reversed(missing):  # search is newest first
            pid = str(summary.get("MessageID") or "")
            detail = self._request("GET", f"/messages/inbound/{pid}/details") or {}
            raw = detail.get("RawEmail")
            if not isinstance(raw, str) or not raw:
                continue  # no raw source available: the message cannot be stored byte-exactly
            when = parse_iso_ms(summary.get("ReceivedAt")) or parse_iso_ms(detail.get("ReceivedAt")) \
                or int(self.clock() * 1000)
            if self.spool.ingest(raw.encode("utf-8", "surrogateescape"), when, pid, source="search") is not None:
                added += 1
        return added

    # ------------------------------------------------------------ internals

    def _for_us(self, payload: dict) -> bool:
        found = [str(payload.get("OriginalRecipient") or "").lower()]
        for key in ("ToFull", "CcFull", "BccFull"):
            for entry in payload.get(key) or []:
                if isinstance(entry, dict):
                    found.append(str(entry.get("Email") or "").lower())
        return any(addr in self.recipients for addr in found if addr)


def from_config(cfg: Mapping[str, str]) -> PostmarkMailbox:
    """Keys: server_token, address; webhook_username / webhook_password, allowed_ips (comma
    list, or `postmark` for the documented webhook IPs), inbound_aliases, store; optional
    base_url, message_stream, authserv_id, max_message_bytes, send_quota_per_day,
    preserves_message_id, catchup_interval."""
    if not cfg.get("server_token"):
        raise TransportError("postmark needs server_token")
    if not cfg.get("address"):
        raise TransportError("postmark needs address")
    ips = cfg_list(cfg, "allowed_ips", ())
    if ips == ("postmark",):
        ips = WEBHOOK_IPS
    return PostmarkMailbox(
        cfg["server_token"], cfg["address"], base_url=cfg.get("base_url") or DEFAULT_BASE_URL,
        store=cfg.get("store") or None, message_stream=cfg.get("message_stream") or "outbound",
        webhook_username=cfg.get("webhook_username") or None, webhook_password=cfg.get("webhook_password") or None,
        allowed_ips=ips, inbound_aliases=cfg_list(cfg, "inbound_aliases", ()),
        authserv_id=cfg.get("authserv_id") or None,
        max_message_bytes=cfg_int(cfg, "max_message_bytes", MAX_MESSAGE_BYTES) or MAX_MESSAGE_BYTES,
        send_quota_per_day=cfg_int(cfg, "send_quota_per_day", None),
        preserves_message_id=cfg_bool(cfg, "preserves_message_id", False),
        catchup_interval=float(cfg_int(cfg, "catchup_interval", 60) or 60),
    )
