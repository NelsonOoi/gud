"""An in-process JMAP server (httpx.MockTransport), built from RFC 8620 / RFC 8621, for CI.

What it models (Fastmail-like):

- Session resource with core limits (`maxSizeUpload`, `maxObjectsInGet`, ...), account-level
  mail limits (`maxSizeAttachmentsPerEmail`), `apiUrl`, `uploadUrl`, `downloadUrl` and
  `eventSourceUrl` templates. One account per token.
- Mailbox/get, Identity/get, Email/get (`ids: []` gives the state), Email/query (sort by
  receivedAt, `after` filter, position/limit), Email/changes (`maxChanges`, `hasMoreChanges`,
  `cannotCalculateChanges` for states older than `min_state`), Email/import (content-addressed
  blobs; a duplicate import into the same mailbox is `alreadyExists`), EmailSubmission/set
  (creation-id back-references; `forbiddenFrom` when the message's From is not the identity).
- Delivery to accounts on the mock domain. The receiving MTA prepends `Received` and
  `Authentication-Results: mx.jmap.test; dmarc=<pass|fail> header.from=<domain>`: pass when the
  submitting account's identity is the From address. `receivedAt` has one-second resolution.
  Raw bytes are otherwise stored unchanged and the Message-ID is kept.
- EventSource (RFC 8620 §7.3): `event: state` with a StateChange on every change, `ping`
  events every `ping` seconds; `drop_streams()` ends every open stream (a forced disconnect).
- RFC 7807 problem details for request-level errors, e.g. an upload over `maxSizeUpload`.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import unquote

import httpx

from gud.transport import mime

BASE = "https://jmap.mock"
MAIL = "urn:ietf:params:jmap:mail"


def _utc(seconds: float) -> str:
    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


@dataclass
class MockEmail:
    id: str
    blob_id: str
    mailbox_ids: dict
    received_at: str
    state: int
    keywords: dict = field(default_factory=dict)


@dataclass
class Account:
    id: str
    address: str
    token: str
    state: int = 0
    emails: dict = field(default_factory=dict)          # id -> MockEmail
    log: list = field(default_factory=list)             # (state, email id) for creations
    mailboxes: dict = field(default_factory=dict)       # id -> role
    submissions: dict = field(default_factory=dict)


@dataclass
class MockJmap:
    domain: str = "jmap.test"
    authserv_id: str = "mx.jmap.test"
    max_size_upload: int = 50_000_000
    max_size_attachments: int = 50_000_000
    max_objects_in_get: int = 500
    clock: Callable[[], float] = time.time
    min_state: int = 0
    accounts: dict = field(default_factory=dict)        # address -> Account
    blobs: dict = field(default_factory=dict)           # blob id -> bytes
    requests: list = field(default_factory=list)
    fail_next: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self._cond = threading.Condition()
        self._generation = 0
        self.open_streams = 0

    # ------------------------------------------------------------ setup

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def account(self, local: str) -> Account:
        address = f"{local}@{self.domain}".lower()
        if address not in self.accounts:
            acc_id = "u" + hashlib.sha1(address.encode()).hexdigest()[:8]
            acc = Account(id=acc_id, address=address, token=f"token-{local}")
            for role in ("inbox", "junk", "sent", "drafts", "trash"):
                acc.mailboxes[f"mb-{role}"] = role
            self.accounts[address] = acc
        return self.accounts[address]

    def session_url(self) -> str:
        return f"{BASE}/jmap/session"

    # ------------------------------------------------------------ delivery

    def _store_blob(self, raw: bytes) -> str:
        blob_id = "G" + hashlib.sha1(raw).hexdigest()
        self.blobs[blob_id] = raw
        return blob_id

    def _create_email(self, acc: Account, raw: bytes, mailbox: str, keywords: dict | None = None) -> MockEmail:
        with self._cond:
            acc.state += 1
            email = MockEmail(id="M" + uuid.uuid4().hex[:20], blob_id=self._store_blob(raw),
                              mailbox_ids={mailbox: True}, received_at=_utc(self.clock()),
                              state=acc.state, keywords=keywords or {})
            acc.emails[email.id] = email
            acc.log.append((acc.state, email.id))
            self._cond.notify_all()
        return email

    def deliver(self, raw: bytes, address: str, *, verdict: str, junk: bool = False) -> MockEmail | None:
        """The receiving MTA: prepend Received + Authentication-Results, store in Inbox/Junk."""
        acc = self.accounts.get(address.lower())
        if acc is None:
            return None
        sender = mime.parse(raw).from_addr
        domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else "unknown.invalid"
        eol = b"\r\n" if b"\r\n" in raw[:4096] else b"\n"
        stamp = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        head = (f"Received: from mx.mock (mx.{self.domain}) by {self.domain} with ESMTPS; {stamp}".encode() + eol
                + f"Authentication-Results: {self.authserv_id}; dmarc={verdict} header.from={domain}".encode() + eol)
        return self._create_email(acc, head + raw, "mb-junk" if junk else "mb-inbox")

    def inject(self, raw: bytes, address: str, verdict: str) -> MockEmail | None:
        """Mail from an outside MTA, which the receiving MTA rates with this DMARC verdict."""
        return self.deliver(raw, address, verdict=verdict)

    def drop_streams(self) -> int:
        with self._cond:
            self._generation += 1
            dropped = self.open_streams
            self._cond.notify_all()
        return dropped

    # ------------------------------------------------------------ HTTP

    def _problem(self, status: int, type_: str, detail: str = "", **extra) -> httpx.Response:
        return httpx.Response(status, json={"type": type_, "status": status, "detail": detail, **extra},
                              headers={"content-type": "application/problem+json"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        token = (request.headers.get("authorization") or "").removeprefix("Bearer ")
        acc = next((a for a in self.accounts.values() if a.token == token), None)
        if acc is None:
            return httpx.Response(401, text="unauthorized")
        if self.fail_next:
            status = self.fail_next.pop(0)
            return httpx.Response(status, headers={"Retry-After": "0"}, text="injected")
        if request.method == "GET" and path == "/jmap/session":
            return httpx.Response(200, json=self._session(acc))
        if request.method == "POST" and path == "/jmap/api/":
            return self._api(acc, json.loads(request.content))
        if request.method == "POST" and path.startswith("/jmap/upload/"):
            if path.split("/")[3] != acc.id:
                return self._problem(403, "urn:ietf:params:jmap:error:forbidden")
            if len(request.content) > self.max_size_upload:
                return self._problem(413, "urn:ietf:params:jmap:error:limit", "upload too large",
                                     limit="maxSizeUpload")
            blob_id = self._store_blob(request.content)
            return httpx.Response(201, json={"accountId": acc.id, "blobId": blob_id,
                                             "type": request.headers.get("content-type"),
                                             "size": len(request.content)})
        if request.method == "GET" and path.startswith("/jmap/download/"):
            parts = path.split("/")
            blob = self.blobs.get(unquote(parts[4]))
            owned = any(e.blob_id == unquote(parts[4]) for e in acc.emails.values())
            if blob is None or parts[3] != acc.id or not owned:
                return httpx.Response(404, text="blob not found")
            return httpx.Response(200, content=blob, headers={"content-type": "message/rfc822"})
        if request.method == "GET" and path == "/jmap/eventsource/":
            ping = max(1, int(request.url.params.get("ping") or 30))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=self._events(acc, ping, request.url.params.get("closeafter") == "state"))
        return httpx.Response(404, text=path)

    def _session(self, acc: Account) -> dict:
        return {
            "capabilities": {
                "urn:ietf:params:jmap:core": {
                    "maxSizeUpload": self.max_size_upload, "maxConcurrentUpload": 4,
                    "maxSizeRequest": 10_000_000, "maxConcurrentRequests": 4, "maxCallsInRequest": 16,
                    "maxObjectsInGet": self.max_objects_in_get, "maxObjectsInSet": 500,
                    "collationAlgorithms": ["i;ascii-casemap"]},
                MAIL: {}, "urn:ietf:params:jmap:submission": {},
            },
            "accounts": {acc.id: {"name": acc.address, "isPersonal": True, "isReadOnly": False,
                                  "accountCapabilities": {
                                      MAIL: {"maxMailboxesPerEmail": None, "maxMailboxDepth": 10,
                                             "maxSizeMailboxName": 490,
                                             "maxSizeAttachmentsPerEmail": self.max_size_attachments,
                                             "emailQuerySortOptions": ["receivedAt"],
                                             "mayCreateTopLevelMailbox": True},
                                      "urn:ietf:params:jmap:submission": {"maxDelayedSend": 0,
                                                                          "submissionExtensions": {}}}}},
            "primaryAccounts": {MAIL: acc.id, "urn:ietf:params:jmap:submission": acc.id},
            "username": acc.address,
            "apiUrl": f"{BASE}/jmap/api/",
            "downloadUrl": f"{BASE}/jmap/download/{{accountId}}/{{blobId}}/{{name}}?type={{type}}",
            "uploadUrl": f"{BASE}/jmap/upload/{{accountId}}/",
            "eventSourceUrl": f"{BASE}/jmap/eventsource/?types={{types}}&closeafter={{closeafter}}&ping={{ping}}",
            "state": "session-1",
        }

    def _events(self, acc: Account, ping: int, close_after_state: bool):
        with self._cond:
            generation = self._generation
            seen = acc.state
            self.open_streams += 1
        try:
            while True:
                with self._cond:
                    self._cond.wait_for(lambda: acc.state != seen or self._generation != generation, timeout=ping)
                    if self._generation != generation:
                        return
                    changed = acc.state != seen
                    seen = acc.state
                if changed:
                    data = {"@type": "StateChange", "changed": {acc.id: {"Email": str(seen)}}}
                    yield f"event: state\ndata: {json.dumps(data)}\n\n".encode()
                    if close_after_state:
                        return
                else:
                    yield f"event: ping\ndata: {json.dumps({'@type': 'Ping', 'interval': ping})}\n\n".encode()
        finally:
            with self._cond:
                self.open_streams -= 1

    # ------------------------------------------------------------ methods

    def _api(self, acc: Account, body: dict) -> httpx.Response:
        if "urn:ietf:params:jmap:core" not in body.get("using", []):
            return self._problem(400, "urn:ietf:params:jmap:error:unknownCapability")
        created: dict[str, str] = {}
        out = []
        for name, args, call_id in body.get("methodCalls", []):
            if args.get("accountId") != acc.id:
                out.append(["error", {"type": "accountNotFound"}, call_id])
                continue
            handler = getattr(self, "_m_" + name.replace("/", "_"), None)
            if handler is None:
                out.append(["error", {"type": "unknownMethod"}, call_id])
                continue
            try:
                out.append([name, handler(acc, args, created), call_id])
            except _MethodError as exc:
                out.append(["error", {"type": exc.type, "description": exc.description}, call_id])
        return httpx.Response(200, json={"methodResponses": out, "sessionState": "session-1",
                                         "createdIds": created})

    def _m_Mailbox_get(self, acc: Account, args: dict, created: dict) -> dict:
        names = {"inbox": "Inbox", "junk": "Spam", "sent": "Sent", "drafts": "Drafts", "trash": "Trash"}
        return {"accountId": acc.id, "state": "mb1", "notFound": [],
                "list": [{"id": mb, "name": names[role], "role": role} for mb, role in acc.mailboxes.items()]}

    def _m_Identity_get(self, acc: Account, args: dict, created: dict) -> dict:
        return {"accountId": acc.id, "state": "id1", "notFound": [],
                "list": [{"id": f"I-{acc.id}", "name": "", "email": acc.address, "mayDelete": False}]}

    def _m_Email_get(self, acc: Account, args: dict, created: dict) -> dict:
        ids = args.get("ids")
        ids = list(acc.emails) if ids is None else ids
        if len(ids) > self.max_objects_in_get:
            raise _MethodError("requestTooLarge")
        found, missing = [], []
        for eid in ids:
            email = acc.emails.get(eid)
            if email is None:
                missing.append(eid)
                continue
            found.append({"id": email.id, "blobId": email.blob_id, "threadId": "T" + email.id,
                          "mailboxIds": dict(email.mailbox_ids), "receivedAt": email.received_at,
                          "size": len(self.blobs[email.blob_id]), "keywords": dict(email.keywords)})
        return {"accountId": acc.id, "state": str(acc.state), "list": found, "notFound": missing}

    def _ordered(self, acc: Account) -> list[MockEmail]:
        return sorted(acc.emails.values(), key=lambda e: (_parse_utc(e.received_at), e.state))

    def _m_Email_query(self, acc: Account, args: dict, created: dict) -> dict:
        filt = args.get("filter") or {}
        emails = self._ordered(acc)
        if filt.get("after"):
            after = _parse_utc(filt["after"])
            emails = [e for e in emails if _parse_utc(e.received_at) >= after]
        if filt.get("inMailbox"):
            emails = [e for e in emails if filt["inMailbox"] in e.mailbox_ids]
        sort = args.get("sort") or []
        if sort and not sort[0].get("isAscending", True):
            emails = list(reversed(emails))
        position = int(args.get("position") or 0)
        limit = int(args.get("limit") or 256)
        page = emails[position:position + limit]
        return {"accountId": acc.id, "queryState": str(acc.state), "canCalculateChanges": False,
                "position": position, "ids": [e.id for e in page], "total": len(emails)}

    def _m_Email_changes(self, acc: Account, args: dict, created: dict) -> dict:
        try:
            since = int(args.get("sinceState"))
        except (TypeError, ValueError):
            raise _MethodError("cannotCalculateChanges", "unknown state")
        if since < self.min_state or since > acc.state:
            raise _MethodError("cannotCalculateChanges", "state too old")
        max_changes = int(args.get("maxChanges") or 500)
        newer = [(s, eid) for s, eid in acc.log if s > since]
        take = newer[:max_changes]
        new_state = take[-1][0] if take else acc.state
        if len(newer) <= max_changes:
            new_state = acc.state
        return {"accountId": acc.id, "oldState": str(since), "newState": str(new_state),
                "hasMoreChanges": len(newer) > max_changes, "created": [eid for _, eid in take],
                "updated": [], "destroyed": []}

    def _m_Email_import(self, acc: Account, args: dict, created: dict) -> dict:
        done, failed = {}, {}
        for cid, spec in (args.get("emails") or {}).items():
            raw = self.blobs.get(spec.get("blobId"))
            if raw is None:
                failed[cid] = {"type": "notFound", "description": "blob"}
                continue
            if len(raw) > self.max_size_upload:
                failed[cid] = {"type": "tooLarge"}
                continue
            mailbox = next(iter(spec.get("mailboxIds") or {}), None)
            dup = next((e for e in acc.emails.values() if e.blob_id == spec["blobId"] and mailbox in e.mailbox_ids), None)
            if dup is not None:
                failed[cid] = {"type": "alreadyExists", "existingId": dup.id}
                continue
            email = self._create_email(acc, raw, mailbox, spec.get("keywords"))
            created[f"#{cid}"] = email.id
            done[cid] = {"id": email.id, "blobId": email.blob_id, "threadId": "T" + email.id, "size": len(raw)}
        return {"accountId": acc.id, "oldState": None, "newState": str(acc.state),
                "created": done or None, "notCreated": failed or None}

    def _m_EmailSubmission_set(self, acc: Account, args: dict, created: dict) -> dict:
        done, failed = {}, {}
        for cid, spec in (args.get("create") or {}).items():
            email_id = spec.get("emailId", "")
            email_id = created.get(email_id, email_id) if email_id.startswith("#") else email_id
            email = acc.emails.get(email_id)
            if email is None:
                failed[cid] = {"type": "invalidProperties", "properties": ["emailId"]}
                continue
            if spec.get("identityId") != f"I-{acc.id}":
                failed[cid] = {"type": "invalidProperties", "properties": ["identityId"]}
                continue
            raw = self.blobs[email.blob_id]
            sender = mime.parse(raw).from_addr.lower()
            if sender != acc.address:
                failed[cid] = {"type": "forbiddenFrom", "description": "From is not one of your identities"}
                continue
            envelope = spec.get("envelope") or {}
            rcpts = [r["email"] for r in envelope.get("rcptTo") or []]
            if not rcpts:
                failed[cid] = {"type": "noRecipients"}
                continue
            sub_id = "S" + uuid.uuid4().hex[:16]
            acc.submissions[sub_id] = {"emailId": email_id, "rcpts": rcpts}
            for rcpt in rcpts:
                self.deliver(raw, rcpt, verdict="pass")
            created[f"#{cid}"] = sub_id
            done[cid] = {"id": sub_id, "undoStatus": "final"}
        return {"accountId": acc.id, "oldState": "0", "newState": "1",
                "created": done or None, "notCreated": failed or None}


class _MethodError(Exception):
    def __init__(self, type_: str, description: str = "") -> None:
        super().__init__(type_)
        self.type, self.description = type_, description
