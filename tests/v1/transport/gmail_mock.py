"""An in-process stand-in for the Gmail API, Google OAuth and Pub/Sub push (httpx.MockTransport).

Built from the public Gmail API reference:

- `POST https://oauth2.googleapis.com/token` (refresh_token and authorization_code grants);
  access tokens expire after `token_ttl` seconds; a revoked refresh token gives `invalid_grant`.
- `users.getProfile`, `users.messages.send` (JSON `raw` base64url, and the media-upload
  endpoint), `users.messages.get` (`format=raw|minimal|metadata`), `users.messages.list`
  (newest first, `q=after:<s>`, `includeSpamTrash`, paging), `users.history.list`
  (`startHistoryId`, `historyTypes`, paging; 404 for a historyId older than `min_history`),
  `users.watch`.
- Sending: a From that is not the account is replaced by the account address; the Message-ID
  is replaced by `<CA+...@mail.gmail.com>`; the sender's copy is labelled SENT. Daily sending
  limit per account (`send_limit`): HTTP 429 `rateLimitExceeded` "Daily user sending limit
  exceeded" once used up.
- Receiving (accounts on the mock): Gmail's MX prepends `Delivered-To`, `Received`,
  `Authentication-Results: mx.google.com; ... dmarc=<pass|fail> (p=NONE ...) header.from=...`
  (pass for mail sent through the account whose address is From) and labels it INBOX (or SPAM).
  `internalDate` is the acceptance time in ms. Each delivery adds a history record.
- Pub/Sub push: for a watched account, each delivery POSTs `{"message": {"data": base64(
  {"emailAddress", "historyId"}), ...}, "subscription": ...}` to `push_sink` with an RS256 OIDC
  token (`iss` https://accounts.google.com, `aud` = `audience`, `email` = the push service
  account) whose key is served as a JWKS at `https://www.googleapis.com/oauth2/v3/certs`.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Callable
from urllib.parse import parse_qsl

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gud.transport import mime

CLIENT_ID, CLIENT_SECRET = "mock-client.apps.googleusercontent.com", "mock-client-secret"
AUDIENCE = "https://hooks.gud.test/gmail-push"
PUSH_SA = "gmail-push@mock-project.iam.gserviceaccount.com"
TOPIC = "projects/mock-project/topics/gud-gmail"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _int_b64(n: int) -> str:
    return b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def sign_jwt(key, claims: dict, kid: str, alg: str = "RS256") -> str:
    head = b64url(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = b64url(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64url(sig)}"


@dataclass
class GMessage:
    id: str
    raw: bytes
    internal_ms: int
    labels: list
    history_id: int


@dataclass
class GAccount:
    address: str
    refresh_token: str
    messages: dict = field(default_factory=dict)
    history: list = field(default_factory=list)       # (record id, message id, labels)
    sent_today: int = 0
    watched: bool = False


@dataclass
class MockGmail:
    domain: str = "gmail.com"
    clock: Callable[[], float] = time.time
    send_limit: int = 500
    token_ttl: int = 3599
    min_history: int = 0
    push_sink: Callable[[dict, bytes], None] | None = None
    audience: str = AUDIENCE
    accounts: dict = field(default_factory=dict)
    tokens: dict = field(default_factory=dict)        # access token -> (address, expiry)
    requests: list = field(default_factory=list)
    token_requests: int = 0
    fail_next: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._history_id = 1000
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "mock-kid-1"

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def account(self, local: str) -> GAccount:
        address = f"{local}@{self.domain}"
        if address not in self.accounts:
            self.accounts[address] = GAccount(address, f"refresh-{local}")
        return self.accounts[address]

    def credentials(self, local: str) -> dict:
        return {"type": "authorized_user", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "refresh_token": self.account(local).refresh_token}

    def jwks(self) -> dict:
        pub = self.key.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": self.kid,
                          "n": _int_b64(pub.n), "e": _int_b64(pub.e)}]}

    def push_token(self, *, audience: str | None = None, email: str = PUSH_SA, iss: str = "https://accounts.google.com",
                   exp_in: int = 3600, key=None, kid: str | None = None) -> str:
        now = int(self.clock())
        claims = {"iss": iss, "aud": audience or self.audience, "azp": "1234", "email": email,
                  "email_verified": True, "iat": now, "exp": now + exp_in, "sub": "1234"}
        return sign_jwt(key or self.key, claims, kid or self.kid)

    def push_body(self, address: str, history_id: int) -> bytes:
        data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": history_id}).encode()).decode()
        mid = str(uuid.uuid4().int)[:16]
        return json.dumps({"message": {"data": data, "messageId": mid, "message_id": mid,
                                       "publishTime": formatdate(self.clock(), usegmt=True)},
                           "subscription": "projects/mock-project/subscriptions/gud-gmail-push"}).encode()

    # ------------------------------------------------------------ HTTP

    def _error(self, code: int, message: str, reason: str, status: str) -> httpx.Response:
        return httpx.Response(code, json={"error": {"code": code, "message": message, "status": status,
                                                    "errors": [{"message": message, "domain": "global", "reason": reason}]}},
                              headers={"Retry-After": "0"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append((request.method, url.host, url.path))
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            return self._token(dict(parse_qsl(request.content.decode())))
        if url.host == "www.googleapis.com" and url.path == "/oauth2/v3/certs":
            return httpx.Response(200, json=self.jwks(), headers={"cache-control": "public, max-age=21600"})
        if url.host != "gmail.googleapis.com":
            return httpx.Response(404, text="unknown host")
        authz = request.headers.get("authorization", "").removeprefix("Bearer ")
        entry = self.tokens.get(authz)
        if entry is None or entry[1] < self.clock():
            return self._error(401, "Request had invalid authentication credentials.", "authError", "UNAUTHENTICATED")
        acc = self.accounts[entry[0]]
        if self.fail_next:
            status = self.fail_next.pop(0)
            return self._error(status, "injected", "backendError", "UNAVAILABLE")
        upload = url.path.startswith("/upload/")
        path = url.path.removeprefix("/upload").removeprefix("/gmail/v1/users/me")
        params = dict(url.params)
        if request.method == "GET" and path == "/profile":
            return httpx.Response(200, json={"emailAddress": acc.address, "messagesTotal": len(acc.messages),
                                             "threadsTotal": len(acc.messages), "historyId": str(self._history_id)})
        if request.method == "POST" and path == "/messages/send":
            raw = request.content if upload else base64.urlsafe_b64decode(json.loads(request.content)["raw"] + "==")
            return self._send(acc, raw)
        if request.method == "GET" and path == "/messages":
            return self._list(acc, params)
        if request.method == "GET" and path.startswith("/messages/"):
            msg = acc.messages.get(path.split("/")[2])
            if msg is None:
                return self._error(404, "Requested entity was not found.", "notFound", "NOT_FOUND")
            return httpx.Response(200, json=self._render(msg, params.get("format") or "full",
                                                         url.params.get_list("metadataHeaders")))
        if request.method == "GET" and path == "/history":
            return self._history(acc, params)
        if request.method == "POST" and path == "/watch":
            body = json.loads(request.content)
            if body.get("topicName") != TOPIC:
                return self._error(400, "Invalid topicName", "invalidArgument", "INVALID_ARGUMENT")
            acc.watched = True
            return httpx.Response(200, json={"historyId": str(self._history_id),
                                             "expiration": str(int((self.clock() + 7 * 86400) * 1000))})
        return self._error(404, f"no route {path}", "notFound", "NOT_FOUND")

    def _token(self, form: dict) -> httpx.Response:
        self.token_requests += 1
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "The OAuth client was not found."})
        if form.get("grant_type") == "authorization_code":
            acc = next(iter(self.accounts.values()))
            return httpx.Response(200, json={"access_token": "x", "expires_in": 3599, "refresh_token": acc.refresh_token,
                                             "scope": "https://www.googleapis.com/auth/gmail.send", "token_type": "Bearer"})
        acc = next((a for a in self.accounts.values() if a.refresh_token == form.get("refresh_token")), None)
        if form.get("grant_type") != "refresh_token" or acc is None:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        token = "ya29." + uuid.uuid4().hex
        self.tokens[token] = (acc.address, self.clock() + self.token_ttl)
        return httpx.Response(200, json={"access_token": token, "expires_in": self.token_ttl, "token_type": "Bearer",
                                         "scope": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"})

    # ------------------------------------------------------------ mail

    def _add(self, acc: GAccount, raw: bytes, labels: list) -> GMessage:
        with self._lock:
            self._history_id += 1
            msg = GMessage(id=uuid.uuid4().hex[:16], raw=raw, internal_ms=int(self.clock() * 1000), labels=labels,
                           history_id=self._history_id)
            acc.messages[msg.id] = msg
            acc.history.append((self._history_id, msg.id))
        return msg

    @staticmethod
    def _set_header(raw: bytes, name: str, value: str) -> bytes:
        sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
        eol = sep[: len(sep) // 2]
        head, _, body = raw.partition(sep)
        lines, out, skipping = head.split(eol), [], False
        for line in lines:
            if line[:1] in (b" ", b"\t") and skipping:
                continue
            skipping = line.lower().startswith(name.lower().encode() + b":")
            if not skipping:
                out.append(line)
        out.append(f"{name}: {value}".encode())
        return eol.join(out) + sep + body

    def _send(self, acc: GAccount, raw: bytes) -> httpx.Response:
        if len(raw) > 35 * 1024 * 1024:
            return self._error(413, "Request payload size exceeds the limit", "uploadTooLarge", "INVALID_ARGUMENT")
        if acc.sent_today >= self.send_limit:
            return self._error(429, "Daily user sending limit exceeded. For more information on Google Workspace sending "
                                    "limits, visit https://support.google.com/a/answer/166852", "rateLimitExceeded",
                               "RESOURCE_EXHAUSTED")
        acc.sent_today += 1
        parsed = mime.parse(raw)
        if parsed.from_addr.lower() != acc.address.lower():
            raw = self._set_header(raw, "From", acc.address)  # Gmail replaces a From it cannot send as
        raw = self._set_header(raw, "Message-ID", f"<CA+{uuid.uuid4().hex}@mail.gmail.com>")
        eol = b"\r\n" if b"\r\n" in raw[:4096] else b"\n"
        raw = f"DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed; d={self.domain}; s=20230601; h=from:to; bh=x; b=y"\
            .encode() + eol + raw
        sent = self._add(acc, raw, ["SENT"])
        for rcpt in mime.recipients(raw):
            if rcpt.lower() in self.accounts:
                self.deliver(raw, rcpt.lower(), verdict="pass")
        return httpx.Response(200, json={"id": sent.id, "threadId": sent.id, "labelIds": ["SENT"]})

    def deliver(self, raw: bytes, address: str, *, verdict: str, spam: bool = False) -> GMessage | None:
        acc = self.accounts.get(address)
        if acc is None:
            return None
        sender = mime.parse(raw).from_addr
        domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else "unknown.invalid"
        eol = b"\r\n" if b"\r\n" in raw[:4096] else b"\n"
        stamp = formatdate(self.clock())
        head = [f"Delivered-To: {address}",
                f"Received: by 2002:a05:6a10:mock with SMTP id x; {stamp}",
                f"Authentication-Results: mx.google.com;       dkim=pass header.i=@{domain} header.s=20230601;"
                f"       spf=pass (google.com: domain of {sender} designates 209.85.220.41 as permitted sender) "
                f"smtp.mailfrom={sender};       dmarc={verdict} (p=NONE sp=QUARANTINE dis=NONE) header.from={domain}",
                f"Return-Path: <{sender}>"]
        stored = eol.join(h.encode() for h in head) + eol + raw
        msg = self._add(acc, stored, ["SPAM"] if spam else ["INBOX", "UNREAD", "CATEGORY_PERSONAL"])
        if acc.watched and self.push_sink is not None:
            headers = {"Authorization": f"Bearer {self.push_token()}", "Content-Type": "application/json",
                       "User-Agent": "APIs-Google; (+https://developers.google.com/webmasters/APIs-Google.html)"}
            threading.Thread(target=self.push_sink, args=(headers, self.push_body(address, msg.history_id)),
                             daemon=True).start()
        return msg

    def inject(self, raw: bytes, address: str, verdict: str) -> GMessage | None:
        return self.deliver(raw, address, verdict=verdict)

    def _render(self, msg: GMessage, fmt: str, metadata: list) -> dict:
        out = {"id": msg.id, "threadId": msg.id, "labelIds": list(msg.labels), "snippet": "",
               "historyId": str(msg.history_id), "internalDate": str(msg.internal_ms), "sizeEstimate": len(msg.raw)}
        if fmt == "raw":
            out["raw"] = base64.urlsafe_b64encode(msg.raw).decode()
        elif fmt == "metadata":
            want = {m.lower() for m in metadata}
            out["payload"] = {"headers": [{"name": k, "value": v} for k, v in mime.parse(msg.raw).headers
                                          if not want or k.lower() in want]}
        return out

    def _list(self, acc: GAccount, params: dict) -> httpx.Response:
        msgs = sorted(acc.messages.values(), key=lambda m: (m.internal_ms, m.history_id), reverse=True)
        if params.get("includeSpamTrash") != "true":
            msgs = [m for m in msgs if "SPAM" not in m.labels and "TRASH" not in m.labels]
        q = params.get("q") or ""
        if q.startswith("after:"):
            after = int(q.split(":", 1)[1]) * 1000
            msgs = [m for m in msgs if m.internal_ms > after]
        size, start = int(params.get("maxResults") or 100), int(params.get("pageToken") or 0)
        page = msgs[start:start + size]
        body: dict = {"resultSizeEstimate": len(msgs)}
        if page:
            body["messages"] = [{"id": m.id, "threadId": m.id} for m in page]
        if start + size < len(msgs):
            body["nextPageToken"] = str(start + size)
        return httpx.Response(200, json=body)

    def _history(self, acc: GAccount, params: dict) -> httpx.Response:
        start = int(params.get("startHistoryId") or 0)
        if start < self.min_history:
            return self._error(404, "Requested entity was not found.", "notFound", "NOT_FOUND")
        records = [(hid, mid) for hid, mid in acc.history if hid > start]
        size, offset = int(params.get("maxResults") or 100), int(params.get("pageToken") or 0)
        page = records[offset:offset + size]
        body: dict = {"historyId": str(self._history_id)}
        if page:
            body["history"] = [{"id": str(hid), "messages": [{"id": mid, "threadId": mid}],
                                "messagesAdded": [{"message": {"id": mid, "threadId": mid,
                                                               "labelIds": list(acc.messages[mid].labels)}}]}
                               for hid, mid in page]
        if offset + size < len(records):
            body["nextPageToken"] = str(offset + size)
        return httpx.Response(200, json=body)
