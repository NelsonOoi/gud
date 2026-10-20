"""An in-process stand-in for the Primitive API (httpx.MockTransport), for CI.

It models what the live service was observed to do: it renders the MIME itself from the JSON
send (with Python's email package, so the layout differs from gud.transport.mime), rewrites the
Message-ID to `<primitive-direct-<sent id>@<from domain>>`, stores LF-only MIME, delivers one inbound row per
recipient on its domain, keeps raw MIME without Authentication-Results and exposes DMARC as the
`auth` object evaluated on the organisational domain, pages the forward tail with
`<visible-at µs>|<id>` cursors, honours `wait` long-polls, and signs webhooks with HMAC.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import default as LF_POLICY
from email.utils import formatdate, getaddresses
from typing import Callable

import httpx

SECRET = "mock-webhook-secret-not-base64"
ATTACHMENT_CAP = 31_457_280
BODY_CAP = 262_144


def _iso_ms(us: int) -> str:
    return datetime.fromtimestamp(us / 1e6, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_us(us: int) -> str:
    return datetime.fromtimestamp(us / 1e6, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_cursor(value: str) -> tuple[int, str]:
    ts, _, rid = value.partition("|")
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(text)
    return int(round(dt.timestamp() * 1e6)), rid


@dataclass
class Row:
    id: str
    recipient: str
    raw: bytes
    visible_us: int
    received_us: int
    auth: dict
    status: str = "accepted"
    message_id: str = ""
    subject: str = ""
    sender: str = ""


@dataclass
class MockPrimitive:
    domain: str = "agents.primitive.test"
    org_domain: str = "primitive.test"
    clock: Callable[[], float] = time.time
    rows: list[Row] = field(default_factory=list)
    sent: dict[str, dict] = field(default_factory=dict)
    webhook_sink: Callable[[dict, bytes], None] | None = None
    inline_raw_in_webhook: bool = True
    fail_next: list[int] = field(default_factory=list)       # statuses to return before serving
    requests: list[tuple[str, str]] = field(default_factory=list)
    send_count: int = 0

    def __post_init__(self) -> None:
        self._cond = threading.Condition()
        self._last_us = 0

    # ------------------------------------------------------------ plumbing

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), base_url="https://mock.primitive/v1")

    def _now_us(self) -> int:
        with self._cond:
            now = int(self.clock() * 1e6)
            self._last_us = max(now, self._last_us + 1)
            return self._last_us

    def _json(self, data, status: int = 200, meta: dict | None = None) -> httpx.Response:
        body: dict = {"success": status < 400}
        if status < 400:
            body["data"] = data
            if meta is not None:
                body["meta"] = meta
        else:
            body["error"] = data
        return httpx.Response(status, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        self.requests.append((request.method, path))
        if request.headers.get("authorization") != "Bearer test-key":
            return self._json({"code": "unauthorized", "message": "bad key"}, 401)
        if self.fail_next:
            status = self.fail_next.pop(0)
            resp = self._json({"code": "rate_limited" if status == 429 else "internal_error", "message": "injected"}, status)
            resp.headers["Retry-After"] = "0"
            return resp
        parts = [p for p in path.split("/") if p]
        if request.method == "POST" and parts == ["send-mail"]:
            return self._send(json.loads(request.content))
        if request.method == "GET" and parts == ["emails"]:
            return self._list(request.url.params)
        if request.method == "GET" and len(parts) == 2 and parts[0] == "emails":
            row = self._row(parts[1])
            if row is None:
                return self._json({"code": "not_found", "message": "Email not found"}, 404)
            return self._json(self._detail(row))
        if request.method == "GET" and len(parts) == 3 and parts[0] == "emails" and parts[2] == "raw":
            row = self._row(parts[1])
            if row is None:
                return self._json({"code": "not_found", "message": "Email not found"}, 404)
            return httpx.Response(200, content=row.raw, headers={
                "content-type": "message/rfc822", "x-content-sha256": hashlib.sha256(row.raw).hexdigest()})
        if request.method == "GET" and parts == ["account"]:
            return self._json({"limits": {"send_per_day": 10000, "send_per_hour": 1000, "api_per_minute": 3000}})
        if request.method == "GET" and parts == ["account", "webhook-secret"]:
            return self._json({"secret": SECRET})
        return self._json({"code": "not_found", "message": path}, 404)

    # ------------------------------------------------------------ endpoints

    def _send(self, body: dict) -> httpx.Response:
        for key in ("from", "to", "subject"):
            if not body.get(key):
                return self._json({"code": "validation_error", "message": f"{key} is required"}, 400)
        sender = getaddresses([body["from"]])[0][1]
        if sender.rsplit("@", 1)[-1].lower() != self.domain:
            return self._json({"code": "forbidden", "message": "sender domain is not verified"}, 403)
        text = body.get("body_text") or ""
        if len(text.encode("utf-8")) > BODY_CAP:
            return self._json({"code": "validation_error", "message": "body too large"}, 400)
        attachments = body.get("attachments") or []
        decoded = [base64.b64decode(a["content_base64"]) for a in attachments]
        if sum(len(d) for d in decoded) > ATTACHMENT_CAP:
            return self._json({"code": "validation_error", "message": "attachments too large"}, 400)
        sent_id = str(uuid.uuid4())
        self.send_count += 1
        cc = body.get("cc") or []
        cc = [cc] if isinstance(cc, str) else list(cc)
        msg = EmailMessage(policy=LF_POLICY)  # live Primitive stores LF-only MIME
        msg["From"] = body["from"]
        msg["To"] = body["to"]
        if cc:
            msg["Cc"] = ", ".join(cc)
        if body.get("in_reply_to"):
            msg["In-Reply-To"] = body["in_reply_to"]
        if body.get("references"):
            msg["References"] = " ".join(body["references"])
        msg["Subject"] = body["subject"]
        msg["Message-ID"] = f"<primitive-direct-{sent_id}@{sender.rsplit('@', 1)[1]}>"
        msg["Date"] = formatdate(self.clock(), usegmt=True)
        msg.set_content(text)
        for att, data in zip(attachments, decoded):
            maintype, _, subtype = (att.get("content_type") or "application/octet-stream").partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=att["filename"])
        raw = (b"Received: from [127.0.0.1] by out1.mock.primitive (Primitive Mail) with ESMTP\n"
               b"X-Primitive-Sent-At: " + str(int(self.clock() * 1000)).encode() + b"\n" + msg.as_bytes())
        self.sent[sent_id] = body
        accepted = []
        for rcpt in [body["to"], *cc]:
            addr = getaddresses([rcpt])[0][1].lower()
            if addr.rsplit("@", 1)[-1] == self.domain:
                accepted.append(addr)
                self.inject(raw, addr, auth={"dmarc": "pass", "dmarcFromDomain": self.org_domain, "spf": "pass"},
                            message_id=msg["Message-ID"], subject=body["subject"], sender=sender)
        return self._json({"id": sent_id, "status": "submitted_to_agent", "from": sender, "queue_id": None,
                           "accepted": accepted, "rejected": [], "client_idempotency_key": "k",
                           "request_id": "r", "content_hash": "h", "idempotent_replay": False})

    def inject(self, raw: bytes, recipient: str, *, auth: dict, message_id: str = "", subject: str = "",
               sender: str = "", status: str = "accepted") -> Row:
        """Deliver raw bytes to `recipient` as Primitive's MX would, with this DMARC verdict."""
        now = self._now_us()
        row = Row(id=str(uuid.uuid4()), recipient=recipient.lower(), raw=raw, visible_us=now + 30_000,
                  received_us=now, auth=auth, status=status, message_id=message_id, subject=subject, sender=sender)
        with self._cond:
            self.rows.append(row)
            self.rows.sort(key=lambda r: (r.visible_us, r.id))
            self._cond.notify_all()
        if self.webhook_sink is not None:
            payload = {"event": "email.received", "id": f"evt_{row.id}", "email": self._webhook_email(row)}
            body = json.dumps(payload).encode()
            ts = int(self.clock())
            sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
            headers = {"Primitive-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}
            threading.Thread(target=self.webhook_sink, args=(headers, body), daemon=True).start()
        return row

    def _webhook_email(self, row: Row) -> dict:
        content: dict = {"raw": {"included": self.inline_raw_in_webhook, "encoding": "base64",
                                 "size_bytes": len(row.raw), "sha256": hashlib.sha256(row.raw).hexdigest()}}
        if self.inline_raw_in_webhook:
            content["raw"]["data"] = base64.b64encode(row.raw).decode()
        return {"id": row.id, "received_at": _iso_ms(row.received_us), "recipient": row.recipient,
                "smtp": {"rcpt_to": [row.recipient]}, "content": content, "auth": row.auth}

    def _row(self, rid: str) -> Row | None:
        with self._cond:
            for row in self.rows:
                if row.id == rid:
                    return row
        return None

    def _summary(self, row: Row) -> dict:
        return {"id": row.id, "message_id": row.message_id, "recipient": row.recipient, "sender": row.sender,
                "subject": row.subject, "status": row.status, "created_at": _iso_ms(row.received_us + 1000),
                "received_at": _iso_ms(row.received_us), "raw_size_bytes": len(row.raw)}

    def _detail(self, row: Row) -> dict:
        return {**self._summary(row), "auth": row.auth, "parsed": {"body_text": None}}

    def _list(self, params) -> httpx.Response:
        recipient = (params.get("recipient") or "").lower()
        limit = min(100, int(params.get("limit") or 50))
        since = params.get("since")
        wait = int(params.get("wait") or 0)
        if since is None:
            with self._cond:
                rows = [r for r in reversed(self.rows) if not recipient or r.recipient == recipient][:limit]
            return self._json([self._summary(r) for r in rows], meta={"total": len(rows), "limit": limit, "cursor": None})
        after = _parse_cursor(since)
        deadline = time.monotonic() + wait
        with self._cond:
            while True:
                rows = [r for r in self.rows if (r.visible_us, r.id) > after
                        and (not recipient or r.recipient == recipient)][:limit]
                remaining = deadline - time.monotonic()
                if rows or remaining <= 0:
                    break
                self._cond.wait(remaining)
        cursor = f"{_iso_us(rows[-1].visible_us)}|{rows[-1].id}" if rows else since
        return self._json([self._summary(r) for r in rows], meta={"total": len(rows), "limit": limit, "cursor": cursor})
