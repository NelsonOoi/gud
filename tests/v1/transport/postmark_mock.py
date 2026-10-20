"""An in-process stand-in for the Postmark API (httpx.MockTransport), for CI.

Built from Postmark's public API docs:

- `POST /email` (`X-Postmark-Server-Token`): From must be on a confirmed Sender Signature domain
  (ErrorCode 400 otherwise); the rendered message must fit 10 MB (ErrorCode 300); `fail_next`
  injects HTTP 429 or ErrorCode 405 (out of credits). Postmark renders the MIME itself (here with
  Python's email package, CRLF line endings), sets `Message-ID: <MessageID@mtasv.net>` whatever
  the request's Headers say, and adds `X-PM-Message-Id` and a `DKIM-Signature`. The response is
  `{To, SubmittedAt, MessageID, ErrorCode: 0, Message: "OK"}`.
- Inbound for `inbound_domain`: Postmark's MX prepends `Return-Path`, `Received` and
  `X-Spam-*`/`Received-SPF` headers (no Authentication-Results, no DMARC verdict) and POSTs the
  inbound JSON (FromFull, ToFull, CcFull, OriginalRecipient, MessageID, Headers, Attachments,
  TextBody, `RawEmail` when `raw_enabled`) to the webhook with HTTP Basic credentials, from one
  of the documented webhook IPs (passed as the `X-Gud-Remote-Addr` the server would set).
- `GET /server` (`RawEmailEnabled`, `InboundAddress`), `GET /messages/inbound` (search, newest
  first, `count`/`offset`, `recipient`), `GET /messages/inbound/{id}/details` (with RawEmail).
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, getaddresses
from typing import Callable

import httpx

from gud.transport.postmark import REMOTE_ADDR_HEADER, WEBHOOK_IPS, basic_auth_header

HOOK_USER, HOOK_PASSWORD = "postmark-hook", "hook-pass-123"
MAX_MESSAGE = 10_000_000


def _iso(ts: float) -> str:
    # Postmark writes 7 fractional digits and a local offset, e.g. 2026-10-20T01:02:03.1234567-04:00
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0+00:00"


@dataclass
class Inbound:
    id: str
    payload: dict
    received: float
    recipient: str


@dataclass
class MockPostmark:
    sender_domain: str = "pm.test"
    inbound_domain: str = "pm.test"
    raw_enabled: bool = True
    clock: Callable[[], float] = time.time
    tokens: dict = field(default_factory=lambda: {"pm-agent": "agent", "pm-repo": "repo"})
    webhook_sink: Callable[[dict, bytes], None] | None = None
    sent: dict = field(default_factory=dict)
    inbound: list = field(default_factory=list)
    fail_next: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    hook_ip: str = WEBHOOK_IPS[0]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def _error(self, status: int, code: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"ErrorCode": code, "Message": message})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        server = self.tokens.get(request.headers.get("x-postmark-server-token", ""))
        if server is None:
            return self._error(401, 10, "The Server Token you provided in the X-Postmark-Server-Token request header was invalid.")
        if self.fail_next:
            status = self.fail_next.pop(0)
            if status == 405:
                return self._error(422, 405, "Not allowed to send: you have run out of credits.")
            return httpx.Response(status, json={"ErrorCode": 429 if status == 429 else 500, "Message": "injected"},
                                  headers={"Retry-After": "0"})
        if request.method == "POST" and path == "/email":
            return self._send(json.loads(request.content))
        if request.method == "GET" and path == "/server":
            return httpx.Response(200, json={"ID": 1, "Name": server, "RawEmailEnabled": self.raw_enabled,
                                             "InboundAddress": f"{uuid.uuid5(uuid.NAMESPACE_DNS, server).hex}@inbound.postmarkapp.com",
                                             "InboundHookUrl": "https://hook.example/postmark", "DeliveryType": "Live"})
        if request.method == "GET" and path == "/messages/inbound":
            return self._search(request.url.params)
        if request.method == "GET" and path.startswith("/messages/inbound/") and path.endswith("/details"):
            pid = path.split("/")[3]
            with self._lock:
                found = next((m for m in self.inbound if m.id == pid), None)
            if found is None:
                return self._error(422, 701, "This message was not found.")
            return httpx.Response(200, json={**found.payload, "Status": "Processed", "ReceivedAt": _iso(found.received)})
        return self._error(404, 404, path)

    def _send(self, body: dict) -> httpx.Response:
        for key in ("From", "To"):
            if not body.get(key):
                return self._error(422, 300, f"Invalid email request: {key} is required")
        if not body.get("TextBody") and not body.get("HtmlBody"):
            return self._error(422, 300, "Provide either email TextBody or HtmlBody or both.")
        sender = getaddresses([body["From"]])[0][1]
        if sender.rsplit("@", 1)[-1].lower() != self.sender_domain:
            return self._error(422, 400, f"The 'From' address you supplied ({sender}) is not a Sender Signature on "
                                         "your account. Please add and confirm this address in order to be able to use it.")
        pid = str(uuid.uuid4())
        msg = EmailMessage(policy=SMTP)
        msg["From"] = body["From"]
        msg["To"] = body["To"]
        if body.get("Cc"):
            msg["Cc"] = body["Cc"]
        msg["Subject"] = body.get("Subject") or ""
        msg["Date"] = formatdate(self.clock(), localtime=False)
        for h in body.get("Headers") or []:
            if h["Name"].lower() in ("in-reply-to", "references"):
                msg[h["Name"]] = h["Value"]
        msg["Message-ID"] = f"<{pid}@mtasv.net>"
        msg["X-PM-Message-Id"] = pid
        msg.set_content(body.get("TextBody") or "")
        for att in body.get("Attachments") or []:
            maintype, _, subtype = (att.get("ContentType") or "application/octet-stream").partition("/")
            msg.add_attachment(base64.b64decode(att["Content"]), maintype=maintype, subtype=subtype, filename=att["Name"])
        raw = (f"DKIM-Signature: v=1; a=rsa-sha256; d={self.sender_domain}; s=pm; c=relaxed/relaxed; bh=x; b=y\r\n"
               .encode() + msg.as_bytes())
        if len(raw) > MAX_MESSAGE:
            return self._error(422, 300, "Invalid email request: message exceeds 10 MB")
        self.sent[pid] = body
        rcpts = [a for _, a in getaddresses([body["To"], body.get("Cc") or ""]) if a]
        for rcpt in rcpts:
            if rcpt.rsplit("@", 1)[-1].lower() == self.inbound_domain:
                self.receive(raw, rcpt, mail_from=sender)
        return httpx.Response(200, json={"To": body["To"], "SubmittedAt": _iso(self.clock()), "MessageID": pid,
                                         "ErrorCode": 0, "Message": "OK"})

    def receive(self, raw: bytes, recipient: str, *, mail_from: str) -> Inbound:
        from gud.transport import mime
        now = self.clock()
        eol = b"\r\n" if b"\r\n" in raw[:4096] else b"\n"
        added = [f"Return-Path: <{mail_from}>",
                 f"Received: by p-pm-inboundg01a.mock (Postfix, from userid 996) id 4AB; {formatdate(now)}",
                 "X-Spam-Checker-Version: SpamAssassin 3.4.0", "X-Spam-Status: No",
                 "X-Spam-Score: -0.1", "X-Spam-Tests: DKIM_SIGNED,DKIM_VALID,SPF_PASS",
                 f"Received-SPF: pass (sender SPF authorized) identity=mailfrom; envelope-from=\"{mail_from}\""]
        stored = eol.join(a.encode() for a in added) + eol + raw
        parsed = mime.parse(stored)
        pid = str(uuid.uuid4())
        payload = {
            "FromName": "", "MessageStream": "inbound", "From": parsed.from_addr,
            "FromFull": {"Email": parsed.from_addr, "Name": "", "MailboxHash": ""},
            "To": ", ".join(parsed.to), "ToFull": [{"Email": a, "Name": "", "MailboxHash": ""} for a in parsed.to],
            "Cc": ", ".join(parsed.cc), "CcFull": [{"Email": a, "Name": "", "MailboxHash": ""} for a in parsed.cc],
            "Bcc": "", "BccFull": [], "OriginalRecipient": recipient, "Subject": parsed.subject,
            "MessageID": pid, "ReplyTo": "", "MailboxHash": "", "Date": formatdate(now), "TextBody": parsed.text,
            "HtmlBody": "", "StrippedTextReply": "", "Tag": "",
            "Headers": [{"Name": k, "Value": v} for k, v in parsed.headers
                        if k.lower() not in ("from", "to", "cc", "subject", "date")],
            "Attachments": [],
        }
        if self.raw_enabled:
            payload["RawEmail"] = stored.decode("utf-8", "surrogateescape")
        item = Inbound(pid, payload, now, recipient.lower())
        with self._lock:
            self.inbound.append(item)
        if self.webhook_sink is not None:
            headers = {"Authorization": basic_auth_header(HOOK_USER, HOOK_PASSWORD),
                       "Content-Type": "application/json", "User-Agent": "Postmark", REMOTE_ADDR_HEADER: self.hook_ip}
            threading.Thread(target=self.webhook_sink, args=(headers, json.dumps(payload).encode()), daemon=True).start()
        return item

    def inject(self, raw: bytes, recipient: str) -> Inbound:
        from gud.transport import mime
        return self.receive(raw, recipient, mail_from=mime.parse(raw).from_addr)

    def _search(self, params) -> httpx.Response:
        count, offset = int(params.get("count") or 0), int(params.get("offset") or 0)
        if not 0 < count <= 500 or count + offset > 10_000:
            return self._error(422, 300, "count must be 1-500 and count + offset at most 10000")
        recipient = (params.get("recipient") or "").lower()
        with self._lock:
            rows = [m for m in reversed(self.inbound) if not recipient or m.recipient == recipient
                    or recipient in m.payload["To"].lower()]
        page = rows[offset:offset + count]
        return httpx.Response(200, json={"TotalCount": len(rows), "InboundMessages": [
            {k: m.payload[k] for k in ("From", "FromName", "FromFull", "To", "ToFull", "Cc", "CcFull", "ReplyTo",
                                      "OriginalRecipient", "Subject", "Date", "MailboxHash", "MessageID", "Tag")}
            | {"Status": "Processed", "ReceivedAt": _iso(m.received), "Attachments": []} for m in page]})
