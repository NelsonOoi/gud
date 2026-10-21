"""Gmail API mailbox (users.messages.send / history.list / messages.get, Pub/Sub push).

- Auth: OAuth 2.0 for installed apps. `authorize_installed_app` runs the one-time consent flow
  (loopback redirect, PKCE) and returns a refresh token; afterwards the adapter exchanges the
  refresh token at `https://oauth2.googleapis.com/token` for access tokens, cached (mode 0600)
  in `GUD_HOME/gmail/<address>.token.json`. Credentials come from `credentials_path` (Google's
  `authorized_user` JSON: client_id, client_secret, refresh_token) or from config keys.
- `send`: `users.messages.send` with the raw RFC 5322 bytes (`raw`, base64url; messages over
  4 MB go to the media-upload endpoint as `message/rfc822`). Gmail sets its own Message-ID, so
  the adapter reads the sent message's `Message-ID` back (`format=metadata`) and reports it in
  `Sent.message_id`. Gmail also replaces a From that is not the account or a verified send-as
  alias with the account's address. Quota errors (`rateLimitExceeded`, `userRateLimitExceeded`,
  `dailyLimitExceeded`, `quotaExceeded`, HTTP 429 after retries) raise `GmailQuotaError`.
- `list`: `users.history.list` (`historyTypes=messageAdded`) from the cursor's historyId, then
  `messages.get?format=raw` per message. Received time is `internalDate` (ms; for SMTP mail the
  time Google accepted it). Drafts and the account's own sends (SENT without INBOX/SPAM) are
  skipped; Spam is read. `list(since=None)` walks the mailbox with `messages.list` (oldest
  internalDate first) and then continues with history from the historyId taken before the walk.
  A 404 from history.list (the historyId expired, typically after about a week) falls back to
  such a walk from the cursor's last internalDate; delivery is at least once.
- Cursor: `h:<historyId>:<skip ids>:<last internalDate>` (history) or
  `s:<base historyId>:<internalDate>:<ids at that time>` (walk).
- Push: `users.watch` on a Pub/Sub topic (renewed daily; a watch lasts 7 days); Pub/Sub pushes
  to the webhook with a Google-signed OIDC JWT in `Authorization: Bearer`. `verify` checks it
  (RS256 against Google's JWKS, issuer, audience, service-account email, expiry); a verified
  push wakes every subscription, which lists from its cursor. Without a public URL the
  subscription polls `history.list` every `poll_interval` seconds (the fallback the spec allows
  for catch-up). §20.2 gives Gmail a 30 s wake bound.
- Authentication-Results: Gmail's MX writes `Authentication-Results: mx.google.com; ...
  dmarc=<result> ... header.from=<domain>` at the top of every message it receives over SMTP;
  a repo on Gmail pins `authserv-id mx.google.com`.
- Quota: Gmail's documented sending limits, 500 recipients/day for consumer accounts
  (@gmail.com, @googlemail.com) and 2 000/day for Google Workspace (`account_type`, or
  `send_quota_per_day` to override). Gmail suits humans and small swarms only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gud.core.types import Ms

from . import mime
from .base import (
    Capabilities, Outgoing, QuotaExceeded, Received, Sent, Subscription, ThreadSubscription,
    TransportError, cfg_bool, cfg_int, safe_call,
)
from .httputil import RETRY_STATUSES, header, retry_delay

API = "https://gmail.googleapis.com/gmail/v1/users/me"
UPLOAD = "https://gmail.googleapis.com/upload/gmail/v1/users/me"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send")
AUTHSERV_ID = "mx.google.com"
CONSUMER_SEND_PER_DAY = 500
WORKSPACE_SEND_PER_DAY = 2_000
CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
MAX_MESSAGE_BYTES = 25_000_000          # Gmail's limit for a sent message (the upload API allows 35 MB)
SIMPLE_SEND_MAX = 4_000_000             # larger messages use the media-upload endpoint
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
WATCH_RENEW_S = 24 * 3600
_QUOTA_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"})
_MAX_ATTEMPTS = 4


class GmailError(TransportError):
    def __init__(self, status: int, reason: str, message: str):
        self.status, self.reason, self.message = status, reason, message
        super().__init__(f"{status} {reason}: {message}")


class GmailQuotaError(GmailError, QuotaExceeded):
    """Gmail's sending limit or an API rate limit is exhausted."""


class GmailAuthError(TransportError):
    """The refresh token was refused (revoked, expired, wrong client)."""


class JwtError(TransportError):
    """A Pub/Sub push whose OIDC token does not verify."""


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str
    refresh_token: str
    token_uri: str = TOKEN_URI

    @classmethod
    def from_file(cls, path: Path | str) -> "OAuthClient":
        """Google's `authorized_user` JSON, or a client-secrets file (`installed`/`web`) with a
        `refresh_token` key added."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GmailAuthError(f"cannot read Gmail credentials {path}: {exc}") from exc
        inner = data.get("installed") or data.get("web") or data
        refresh = data.get("refresh_token") or inner.get("refresh_token")
        if not inner.get("client_id") or not inner.get("client_secret") or not refresh:
            raise GmailAuthError(f"{path} needs client_id, client_secret and refresh_token")
        return cls(inner["client_id"], inner["client_secret"], refresh, inner.get("token_uri") or TOKEN_URI)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- installed-app consent flow


def consent_url(client_id: str, redirect_uri: str, state: str, code_challenge: str,
                scopes: tuple[str, ...] = SCOPES, login_hint: str | None = None) -> str:
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(scopes), "state": state, "code_challenge": code_challenge,
              "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URI}?{urlencode(params)}"


def authorize_installed_app(client_id: str, client_secret: str, *, scopes: tuple[str, ...] = SCOPES,
                            login_hint: str | None = None, open_browser: Callable[[str], Any] | None = None,
                            client: httpx.Client | None = None, timeout: float = 300.0) -> str:
    """One-time consent (RFC 8252 loopback redirect with PKCE). Returns the refresh token."""
    import http.server
    import urllib.parse
    import webbrowser

    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if query.get("state") == state and (query.get("code") or query.get("error")):
                result.update(query)
                done.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"gud: authorization received, you can close this tab.\n")

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = consent_url(client_id, redirect_uri, state, challenge, scopes, login_hint)
        (open_browser or webbrowser.open)(url)
        if not done.wait(timeout):
            raise GmailAuthError("timed out waiting for the OAuth consent redirect")
    finally:
        server.shutdown()
        server.server_close()
    if result.get("error") or not result.get("code"):
        raise GmailAuthError(f"consent refused: {result.get('error')}")
    http_client = client or httpx.Client(timeout=30.0)
    try:
        resp = http_client.post(TOKEN_URI, data={
            "grant_type": "authorization_code", "code": result["code"], "client_id": client_id,
            "client_secret": client_secret, "redirect_uri": redirect_uri, "code_verifier": verifier})
    finally:
        if client is None:
            http_client.close()
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not body.get("refresh_token"):
        raise GmailAuthError(f"code exchange failed: {body.get('error') or resp.status_code}")
    return str(body["refresh_token"])


# ---------------------------------------------------------------- tokens


class TokenCache:
    """Access tokens cached on disk (mode 0600), refreshed from the refresh token."""

    def __init__(self, oauth: OAuthClient, path: Path | None, client: httpx.Client,
                 clock: Callable[[], float] = time.time) -> None:
        self.oauth, self.path, self.client, self.clock = oauth, path, client, clock
        self._lock = threading.Lock()
        self._token: dict | None = None

    def _load(self) -> dict | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("client_id") != self.oauth.client_id:
            return None
        return data

    def _store(self, data: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            now = self.clock()
            if not force:
                cached = self._token or self._load()
                if cached and float(cached.get("expires_at", 0)) > now + 60:
                    self._token = cached
                    return str(cached["access_token"])
            resp = self.client.post(self.oauth.token_uri, data={
                "grant_type": "refresh_token", "refresh_token": self.oauth.refresh_token,
                "client_id": self.oauth.client_id, "client_secret": self.oauth.client_secret}, timeout=30.0)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code >= 400 or not body.get("access_token"):
                raise GmailAuthError(f"token refresh failed: {body.get('error') or resp.status_code} "
                                     f"{body.get('error_description') or ''}".strip())
            data = {"access_token": body["access_token"], "expires_at": now + float(body.get("expires_in") or 3600),
                    "scope": body.get("scope", ""), "client_id": self.oauth.client_id}
            self._token = data
            self._store(data)
            return str(data["access_token"])


# ---------------------------------------------------------------- Pub/Sub push JWT


class GoogleJwtVerifier:
    """RS256 OIDC tokens Pub/Sub attaches to authenticated push requests."""

    def __init__(self, client: httpx.Client, *, audience: str, service_account: str | None = None,
                 jwks_uri: str = JWKS_URI, clock: Callable[[], float] = time.time, leeway: float = 60.0) -> None:
        self.client, self.audience, self.service_account = client, audience, service_account
        self.jwks_uri, self.clock, self.leeway = jwks_uri, clock, leeway
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._expires = 0.0
        self._last_fetch = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        resp = self.client.get(self.jwks_uri, timeout=30.0)
        if resp.status_code != 200:
            raise JwtError(f"JWKS fetch gave HTTP {resp.status_code}")
        keys = {}
        for jwk in (resp.json() or {}).get("keys") or []:
            if jwk.get("kty") != "RSA" or not jwk.get("kid"):
                continue
            n = int.from_bytes(_unb64url(jwk["n"]), "big")
            e = int.from_bytes(_unb64url(jwk["e"]), "big")
            keys[jwk["kid"]] = rsa.RSAPublicNumbers(e, n).public_key()
        max_age = 3600.0
        for part in (resp.headers.get("cache-control") or "").split(","):
            name, _, value = part.strip().partition("=")
            if name == "max-age" and value.isdigit():
                max_age = float(value)
        self._keys, self._expires, self._last_fetch = keys, self.clock() + max_age, self.clock()

    def _key(self, kid: str) -> rsa.RSAPublicKey:
        with self._lock:
            if not self._keys or self.clock() >= self._expires:
                self._refresh()
            key = self._keys.get(kid)
            if key is None and self.clock() - self._last_fetch > 60:
                self._refresh()  # keys rotate; refetch at most once a minute for an unknown kid
                key = self._keys.get(kid)
        if key is None:
            raise JwtError(f"unknown signing key {kid!r}")
        return key

    def verify(self, token: str) -> dict:
        parts = token.split(".")
        if len(parts) != 3:
            raise JwtError("not a JWT")
        try:
            head = json.loads(_unb64url(parts[0]))
            claims = json.loads(_unb64url(parts[1]))
            signature = _unb64url(parts[2])
        except (ValueError, json.JSONDecodeError) as exc:
            raise JwtError("malformed JWT") from exc
        if head.get("alg") != "RS256":
            raise JwtError(f"JWT alg {head.get('alg')!r} is not RS256")
        key = self._key(str(head.get("kid") or ""))
        try:
            key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as exc:
            raise JwtError("JWT signature does not verify") from exc
        now = self.clock()
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise JwtError(f"JWT issuer {claims.get('iss')!r} is not Google")
        aud = claims.get("aud")
        if aud != self.audience and not (isinstance(aud, list) and self.audience in aud):
            raise JwtError(f"JWT audience {aud!r} is not {self.audience!r}")
        if float(claims.get("exp") or 0) < now - self.leeway:
            raise JwtError("JWT has expired")
        if float(claims.get("iat") or 0) > now + self.leeway:
            raise JwtError("JWT is issued in the future")
        if self.service_account is not None:
            if str(claims.get("email") or "").lower() != self.service_account.lower() \
                    or claims.get("email_verified") not in (True, "true"):
                raise JwtError("JWT is not from the configured push service account")
        return claims


# ---------------------------------------------------------------- cursors


def encode_cursor(mode: str, base: str, received: Ms | None = None, ids: tuple[str, ...] = ()) -> str:
    return f"{mode}:{base}:{','.join(ids)}:{'' if received is None else int(received)}"


def decode_cursor(cursor: str) -> tuple[str, int, tuple[str, ...], Ms | None]:
    parts = cursor.split(":")
    if len(parts) != 4 or parts[0] not in ("h", "s") or not parts[1].isdigit() \
            or (parts[3] and not parts[3].isdigit()):
        raise TransportError(f"invalid gmail cursor {cursor!r}")
    return parts[0], int(parts[1]), tuple(i for i in parts[2].split(",") if i), (int(parts[3]) if parts[3] else None)


def delivered(labels: list[str] | tuple[str, ...]) -> bool:
    """Mail delivered to the account: not a draft, and not only the account's own send."""
    labels = set(labels or ())
    if "DRAFT" in labels:
        return False
    return "SENT" not in labels or "INBOX" in labels or "SPAM" in labels


class _History404(Exception):
    pass


class GmailMailbox:
    def __init__(self, credentials_path: Path | str | None, address: str, *, oauth: OAuthClient | None = None,
                 client: httpx.Client | None = None, token_cache: Path | str | None = None,
                 account_type: str = "auto", send_quota_per_day: int | None = None,
                 max_message_bytes: int = MAX_MESSAGE_BYTES, pubsub_topic: str | None = None,
                 push_audience: str | None = None, push_service_account: str | None = None,
                 poll_interval: float = 10.0, preserves_message_id: bool = False,
                 report_message_id: bool = True, api_base: str = API, upload_base: str = UPLOAD,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        if oauth is None:
            if credentials_path is None:
                raise GmailAuthError("gmail needs credentials_path or client_id/client_secret/refresh_token")
            oauth = OAuthClient.from_file(credentials_path)
        self.address = address
        self.api_base, self.upload_base = api_base.rstrip("/"), upload_base.rstrip("/")
        self.max_message_bytes = max_message_bytes
        self.pubsub_topic = pubsub_topic
        self.poll_interval = poll_interval
        self.preserves_message_id = preserves_message_id
        self.report_message_id = report_message_id
        self.clock = clock
        self._sleep = sleep
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        if token_cache is None:
            home = os.environ.get("GUD_HOME") or str(Path.home() / ".gud")
            token_cache = Path(home) / "gmail" / f"{address.lower()}.token.json"
        self.tokens = TokenCache(oauth, Path(token_cache), self._client, clock)
        domain = address.rsplit("@", 1)[-1].lower()
        kind = account_type if account_type in ("consumer", "workspace") else (
            "consumer" if domain in CONSUMER_DOMAINS else "workspace")
        self.account_type = kind
        self._quota = send_quota_per_day if send_quota_per_day is not None else (
            CONSUMER_SEND_PER_DAY if kind == "consumer" else WORKSPACE_SEND_PER_DAY)
        self.jwt = GoogleJwtVerifier(self._client, audience=push_audience, service_account=push_service_account,
                                     clock=clock) if push_audience else None
        self._lock = threading.Lock()
        self._wakers: list[threading.Event] = []
        self.watch_expiration: float | None = None

    # ------------------------------------------------------------ HTTP

    def _request(self, method: str, url: str, *, params: dict | None = None, json_body: Any = None,
                 content: bytes | None = None, headers: dict | None = None, timeout: float = 60.0) -> Any:
        last: Exception | None = None
        refreshed = False
        attempt = 0
        while attempt < _MAX_ATTEMPTS:
            hdrs = {"Authorization": f"Bearer {self.tokens.token()}", "Accept": "application/json"}
            hdrs.update(headers or {})
            try:
                resp = self._client.request(method, url, params=params, json=json_body, content=content,
                                            headers=hdrs, timeout=timeout)
            except httpx.HTTPError as exc:
                last = exc
                attempt += 1
                if attempt < _MAX_ATTEMPTS:
                    self._sleep(min(8.0, 0.5 * 2 ** attempt))
                    continue
                raise GmailError(0, "network", str(exc)) from exc
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self.tokens.token(force=True)
                continue
            error = _google_error(resp) if resp.status_code >= 400 else None
            if error is not None and isinstance(error, GmailQuotaError) and resp.status_code == 403:
                raise error  # daily limits: retrying today will not help
            attempt += 1
            if resp.status_code in RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                self._sleep(retry_delay(resp, attempt - 1))
                continue
            if error is not None:
                raise error
            return resp.json() if resp.content else {}
        raise GmailError(0, "network", str(last))

    # ------------------------------------------------------------ contract

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="gmail", max_message_bytes=self.max_message_bytes,
            push="pubsub" if self.pubsub_topic else "none", preserves_message_id=self.preserves_message_id,
            auth_results="header", authserv_id=AUTHSERV_ID, send_quota_per_day=self._quota,
            received_time_resolution_ms=1, raw_mime=True,
        )

    def send(self, message: Outgoing | bytes) -> Sent:
        if isinstance(message, Outgoing):
            raw = mime.build(message)
        elif isinstance(message, (bytes, bytearray)):
            raw = bytes(message)
        else:
            raise TypeError("send() takes an Outgoing or raw bytes")
        if len(raw) > self.max_message_bytes:
            raise GmailError(400, "messageTooLarge", f"message is {len(raw)} bytes; the limit is {self.max_message_bytes}")
        if not mime.recipients(raw):
            raise TransportError("message has no recipients")
        if len(raw) > SIMPLE_SEND_MAX:
            body = self._request("POST", f"{self.upload_base}/messages/send", params={"uploadType": "media"},
                                 content=raw, headers={"Content-Type": "message/rfc822"}, timeout=600.0)
        else:
            body = self._request("POST", f"{self.api_base}/messages/send",
                                 json_body={"raw": base64.urlsafe_b64encode(raw).decode("ascii")}, timeout=300.0)
        gid = str(body.get("id") or "") or None
        message_id = mime.parse(raw).message_id
        if gid and self.report_message_id:
            try:
                meta = self._request("GET", f"{self.api_base}/messages/{gid}",
                                     params={"format": "metadata", "metadataHeaders": "Message-ID"})
                for h in (meta.get("payload") or {}).get("headers") or []:
                    if str(h.get("name") or "").lower() == "message-id" and h.get("value"):
                        message_id = str(h["value"]).strip()
            except TransportError:
                pass
        return Sent(provider_id=gid, message_id=message_id, accepted_at=int(self.clock() * 1000))

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        limit = max(1, int(limit))
        if since is None:
            return self._walk(self._profile_history(), None, (), limit)
        mode, base, ids, received = decode_cursor(since)
        if mode == "s":
            return self._walk(base, received, ids, limit)
        try:
            return self._history(base, ids, received, limit)
        except _History404:
            return self._walk(self._profile_history(), received, (), limit)

    def wait(self, *, since: str | None, timeout: float) -> list[Received]:
        deadline = time.monotonic() + max(0.0, timeout)
        cursor = since
        waker = self._waker()
        self._ensure_watch()
        try:
            while True:
                waker.clear()
                items, cursor = self.list(since=cursor)
                remaining = deadline - time.monotonic()
                if items or remaining <= 0:
                    return items
                waker.wait(min(self.poll_interval, remaining))
        finally:
            self._drop_waker(waker)

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription:
        start = since if since is not None else self.head()
        waker = self._waker()

        def loop(stop: threading.Event) -> None:
            cursor = start
            backoff = 1.0
            while not stop.is_set():
                try:
                    self._ensure_watch()
                    waker.clear()
                    while not stop.is_set():
                        items, cursor = self.list(since=cursor, limit=100)
                        for item in items:
                            safe_call(callback, item)
                        if not items:
                            break
                    backoff = 1.0
                    waker.wait(self.poll_interval)
                except TransportError:
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)
                if stop.is_set():
                    return

        sub = ThreadSubscription(loop, name=f"gmail-subscribe-{self.address}")
        mailbox = self

        class _Sub:
            def close(self) -> None:
                sub.stopped.set()
                waker.set()
                sub.close()
                mailbox._drop_waker(waker)

        return _Sub()

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def head(self) -> str:
        return encode_cursor("h", str(self._profile_history()))

    def watch(self) -> dict:
        """`users.watch` on the configured topic (INBOX and SPAM). Lasts 7 days; renew daily."""
        if not self.pubsub_topic:
            raise TransportError("no pubsub_topic configured")
        body = self._request("POST", f"{self.api_base}/watch", json_body={
            "topicName": self.pubsub_topic, "labelIds": ["INBOX", "SPAM"], "labelFilterBehavior": "INCLUDE"})
        exp = body.get("expiration")
        self.watch_expiration = int(exp) / 1000.0 if exp else self.clock() + 7 * 86400
        return body

    # ------------------------------------------------------------ Pub/Sub push (WebhookVerifier)

    def verify(self, headers: Mapping[str, str], body: bytes, now: Ms) -> bool:
        if self.jwt is None:
            return False
        authz = header(headers, "Authorization") or ""
        if not authz.startswith("Bearer "):
            return False
        try:
            self.jwt.verify(authz[len("Bearer "):].strip())
        except (TransportError, ValueError, TypeError):
            return False
        return True

    def notification(self, body: bytes) -> dict:
        """The `{emailAddress, historyId}` a Pub/Sub push carries."""
        try:
            envelope = json.loads(body.decode("utf-8"))
            data = json.loads(base64.b64decode((envelope.get("message") or {}).get("data") or ""))
        except (UnicodeDecodeError, ValueError, AttributeError) as exc:
            raise TransportError("not a Pub/Sub push body") from exc
        if not isinstance(data, dict):
            raise TransportError("Pub/Sub data is not a Gmail notification")
        return data

    def parse(self, headers: Mapping[str, str], body: bytes) -> list[Received]:
        """A Gmail push carries no mail, only a historyId; it wakes the subscriptions, which list
        from their cursors. So this returns [] (after checking the body is a notification)."""
        self.notification(body)
        return []

    def accept_webhook(self, headers: Mapping[str, str], body: bytes, now: Ms | None = None) -> dict:
        """Verify the push, check it is for this address, and wake every subscription and wait.
        Returns the notification. Raises TransportError on a bad token (answer 403)."""
        if not self.verify(headers, body, int(self.clock() * 1000) if now is None else now):
            raise JwtError("Pub/Sub push token does not verify")
        note = self.notification(body)
        if str(note.get("emailAddress") or "").lower() != self.address.lower():
            return note
        with self._lock:
            wakers = list(self._wakers)
        for waker in wakers:
            waker.set()
        return note

    # ------------------------------------------------------------ internals

    def _ensure_watch(self) -> None:
        """Start or renew the watch (a watch lasts 7 days; renewed once less than 6 days remain)."""
        if not self.pubsub_topic:
            return
        if self.watch_expiration is not None and self.watch_expiration - self.clock() >= WATCH_RENEW_S * 6:
            return
        try:
            self.watch()
        except TransportError:
            pass  # polling still works; the next loop retries

    def _waker(self) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._wakers.append(event)
        return event

    def _drop_waker(self, event: threading.Event) -> None:
        with self._lock:
            if event in self._wakers:
                self._wakers.remove(event)

    def _profile_history(self) -> int:
        return int(self._request("GET", f"{self.api_base}/profile").get("historyId") or 0)

    def _message(self, gid: str, fmt: str) -> dict | None:
        try:
            return self._request("GET", f"{self.api_base}/messages/{gid}", params={"format": fmt}, timeout=300.0)
        except GmailError as exc:
            if exc.status == 404:
                return None  # deleted since it was added
            raise

    def _received(self, msg: dict, cursor: str) -> Received:
        return Received(raw_mime=_unb64url(str(msg.get("raw") or "")), received_at=int(msg.get("internalDate") or 0),
                        provider_id=str(msg["id"]), cursor=cursor)

    def _history(self, start: int, skip: tuple[str, ...], last: Ms | None,
                 limit: int) -> tuple[list[Received], str | None]:
        pairs: list[tuple[int, str]] = []          # (record id, message id), in history order
        token: str | None = None
        current = start
        while True:
            params = {"startHistoryId": str(start), "historyTypes": "messageAdded", "maxResults": 500}
            if token:
                params["pageToken"] = token
            try:
                body = self._request("GET", f"{self.api_base}/history", params=params)
            except GmailError as exc:
                if exc.status == 404:
                    raise _History404() from exc
                raise
            current = max(current, int(body.get("historyId") or start))
            for record in body.get("history") or []:
                rid = int(record.get("id") or 0)
                if rid <= start:
                    continue
                for added in record.get("messagesAdded") or []:
                    msg = added.get("message") or {}
                    if msg.get("id") and msg["id"] not in skip and delivered(msg.get("labelIds") or ()):
                        pairs.append((rid, str(msg["id"])))
            token = body.get("nextPageToken")
            if not token or len(pairs) > limit:
                break
        pairs = list(dict.fromkeys(pairs))
        out: list[Received] = []
        last_t = last
        for i, (rid, gid) in enumerate(pairs[:limit]):
            msg = self._message(gid, "raw")
            if msg is None or not delivered(msg.get("labelIds") or ("INBOX",)):
                continue
            t = int(msg.get("internalDate") or 0)
            last_t = t
            nxt = pairs[i + 1] if i + 1 < len(pairs) else None
            if nxt is not None and nxt[0] == rid:
                done = tuple(g for r, g in pairs[:i + 1] if r == rid)
                cursor = encode_cursor("h", str(rid - 1), t, done)
            elif nxt is None and len(pairs) <= limit and not token:
                cursor = encode_cursor("h", str(current), t)
            else:
                cursor = encode_cursor("h", str(rid), t)
            out.append(self._received(msg, cursor))
        if out:
            return out, out[-1].cursor
        if len(pairs) <= limit and not token:
            return out, encode_cursor("h", str(current), last_t)
        return out, encode_cursor("h", str(start), last, skip)

    def _walk(self, base: int, after: Ms | None, skip: tuple[str, ...],
              limit: int) -> tuple[list[Received], str | None]:
        params: dict[str, Any] = {"includeSpamTrash": "true", "maxResults": 500}
        if after is not None:
            params["q"] = f"after:{max(0, after // 1000 - 1)}"
        ids: list[str] = []
        while True:
            body = self._request("GET", f"{self.api_base}/messages", params=params)
            ids.extend(str(m["id"]) for m in body.get("messages") or [])
            if not body.get("nextPageToken"):
                break
            params["pageToken"] = body["nextPageToken"]
        rows = []
        for gid in dict.fromkeys(ids):
            meta = self._message(gid, "minimal")
            if meta is None or not delivered(meta.get("labelIds") or ()):
                continue
            t = int(meta.get("internalDate") or 0)
            if after is not None and (t < after or (t == after and gid in skip)):
                continue
            # historyId grows in delivery order, so it orders messages that share a millisecond.
            rows.append((t, int(meta.get("historyId") or 0), gid))
        rows.sort()
        out: list[Received] = []
        for i, (t, _hid, gid) in enumerate(rows[:limit]):
            msg = self._message(gid, "raw")
            if msg is None:
                continue
            same = tuple(g for tt, _h, g in rows[:i + 1] if tt == t)
            if after is not None and t == after:
                same = tuple(dict.fromkeys(skip + same))
            last = i == len(rows) - 1
            cursor = encode_cursor("h", str(base), t) if last else encode_cursor("s", str(base), t, same)
            out.append(self._received(msg, cursor))
        if out:
            return out, out[-1].cursor
        return out, encode_cursor("h", str(base), after) if not rows else encode_cursor("s", str(base), after, skip)


def _google_error(resp: httpx.Response) -> GmailError:
    reason, message = f"http_{resp.status_code}", (resp.text or "")[:300]
    try:
        err = (resp.json() or {}).get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or message)
            reasons = [str(e.get("reason") or "") for e in err.get("errors") or [] if isinstance(e, dict)]
            reason = next((r for r in reasons if r), str(err.get("status") or reason))
        elif isinstance(err, str):
            reason = err
    except ValueError:
        pass
    quota = resp.status_code == 429 or reason in _QUOTA_REASONS or "sending limit" in message.lower()
    return (GmailQuotaError if quota else GmailError)(resp.status_code, reason, message)


def from_config(cfg: Mapping[str, str]) -> GmailMailbox:
    """Keys: address, and credentials_path (authorized_user JSON) or client_id + client_secret +
    refresh_token; optional token_cache, account_type (consumer|workspace), send_quota_per_day,
    max_message_bytes, pubsub_topic (projects/<p>/topics/<t>), push_audience,
    push_service_account, poll_interval, preserves_message_id."""
    if not cfg.get("address"):
        raise TransportError("gmail needs address")
    oauth = None
    if cfg.get("client_id") and cfg.get("client_secret") and cfg.get("refresh_token"):
        oauth = OAuthClient(cfg["client_id"], cfg["client_secret"], cfg["refresh_token"])
    elif not cfg.get("credentials_path"):
        raise TransportError("gmail needs credentials_path or client_id/client_secret/refresh_token")
    return GmailMailbox(
        cfg.get("credentials_path") or None, cfg["address"], oauth=oauth, token_cache=cfg.get("token_cache") or None,
        account_type=cfg.get("account_type") or "auto", send_quota_per_day=cfg_int(cfg, "send_quota_per_day", None),
        max_message_bytes=cfg_int(cfg, "max_message_bytes", MAX_MESSAGE_BYTES) or MAX_MESSAGE_BYTES,
        pubsub_topic=cfg.get("pubsub_topic") or None, push_audience=cfg.get("push_audience") or None,
        push_service_account=cfg.get("push_service_account") or None,
        poll_interval=float(cfg_int(cfg, "poll_interval", 10) or 10),
        preserves_message_id=cfg_bool(cfg, "preserves_message_id", False),
    )
