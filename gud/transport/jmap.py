"""JMAP mailbox (RFC 8620 core, RFC 8621 mail and submission).

API mapping:

- Session: `GET <session_url>` (Bearer token, or Basic with username/password) gives `apiUrl`,
  `uploadUrl`, `downloadUrl`, `eventSourceUrl`, the account (`primaryAccounts[mail]`, or the
  one whose name is the address) and the limits. The session is refetched when an API response
  reports a different `sessionState`, and on 401.
- `send`: upload the raw RFC 5322 bytes (`POST uploadUrl`, `message/rfc822`), then one request
  `Email/import` (into the `sent` mailbox, else `drafts`) + `EmailSubmission/set` with the
  envelope (`mailFrom` = From, `rcptTo` = To + Cc) referencing the import by creation id. The
  bytes are submitted unchanged, so the Message-ID is the one in the message. The identity is
  the one whose email equals From (or a `*@domain` wildcard identity); none -> `forbiddenFrom`.
  An `alreadyExists` import (servers MAY forbid duplicate imports) submits the existing Email.
- `list`: `Email/changes` from the cursor's state (cursor = JMAP Email state); the created
  Emails are read with `Email/get` (`blobId`, `receivedAt`, `mailboxIds`) and their raw bytes
  downloaded from `downloadUrl` (the blob of an Email is the message as received, incl. the
  receiving MTA's Authentication-Results). Emails only in `sent`/`drafts` are the account's own
  sends and are skipped; Junk is read. `receivedAt` is the delivery time. `list(since=None)`
  walks the whole mailbox with `Email/query` sorted by `receivedAt` (then continues with
  changes from the state taken before the walk). `cannotCalculateChanges` (a state too old)
  falls back to such a walk from the cursor's last `receivedAt`; delivery is at least once.
- Cursor: `<mode>|<state>|<receivedAt ms>|<ids>|<partial>` with each field percent-encoded.
  `mode` is `c` (changes since `state`) or `q` (walk by receivedAt, then changes since
  `state`). Inside a batch the position is "after receivedAt R, and after these ids at R".
  The batch is ordered by `receivedAt`, ties in the server's order.
- `wait`/`subscribe`: EventSource push (RFC 8620 §7.3, `types=Email`, `closeafter=no`) on one
  long-lived stream; every `state` event triggers `list()` catch-up. The stream reconnects
  with exponential backoff and catches up after each reconnect. Without an `eventSourceUrl`
  the adapter polls.
- `capabilities`: `max_message_bytes` = min(core `maxSizeUpload`, the MIME size implied by
  mail `maxSizeAttachmentsPerEmail`), unless configured.
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote, unquote

import httpx

from gud.core.types import Ms

from . import mime
from .base import (
    MIME_OVERHEAD, Capabilities, Outgoing, QuotaExceeded, Received, Sent, Subscription,
    ThreadSubscription, TransportError, cfg_bool, cfg_int, safe_call,
)
from .httputil import RETRY_STATUSES, iso_ms, parse_iso_ms, retry_delay

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
USING = [CORE, MAIL, SUBMISSION]
OWN_ROLES = frozenset({"sent", "drafts"})
DEFAULT_MAX_MESSAGE_BYTES = 50_000_000
_QUOTA_ERRORS = frozenset({"overQuota", "rateLimit", "urn:ietf:params:jmap:error:limit"})
_MAX_ATTEMPTS = 4
_PAGE = 256


class JmapError(TransportError):
    """A JMAP request, method or set error. `type` is the JMAP error type."""

    def __init__(self, type_: str, description: str = "", *, status: int | None = None):
        self.type, self.description, self.status = type_, description, status
        super().__init__(f"{type_}: {description}" if description else type_)


class JmapQuotaError(JmapError, QuotaExceeded):
    """`overQuota`, `rateLimit`, a request limit, or HTTP 429 after retries."""


class _CannotCalculateChanges(Exception):
    pass


def _error(type_: str, description: str = "", status: int | None = None) -> JmapError:
    cls = JmapQuotaError if (type_ in _QUOTA_ERRORS or status == 429) else JmapError
    return cls(type_, description, status=status)


# ---------------------------------------------------------------- cursor codec


def encode_cursor(mode: str, state: str, received: Ms | None = None, ids: tuple[str, ...] = (),
                  partial: bool = False) -> str:
    fields = [mode, quote(state, safe=""), "" if received is None else str(int(received)),
              ",".join(quote(i, safe="") for i in ids), "1" if partial else "0"]
    return "|".join(fields)


def decode_cursor(cursor: str) -> tuple[str, str, Ms | None, tuple[str, ...], bool]:
    parts = cursor.split("|")
    if len(parts) != 5 or parts[0] not in ("c", "q") or not parts[1] \
            or (parts[2] and not parts[2].isdigit()) or parts[4] not in ("0", "1"):
        raise TransportError(f"invalid jmap cursor {cursor!r}")
    ids = tuple(unquote(i) for i in parts[3].split(",") if i)
    return parts[0], unquote(parts[1]), (int(parts[2]) if parts[2] else None), ids, parts[4] == "1"


def expand(template: str, **values: str) -> str:
    """RFC 6570 level-1 expansion of the JMAP URL templates (`{accountId}`, `{blobId}`, ...)."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", quote(value, safe=""))
    return out


def iter_sse(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    """(event, data) pairs from text/event-stream lines (comments and ids ignored)."""
    event, data = "", []
    for line in lines:
        if line == "":
            if data or event:
                yield event or "message", "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield event or "message", "\n".join(data)


class JmapMailbox:
    def __init__(self, session_url: str, token: str | None, address: str, *,
                 username: str | None = None, password: str | None = None,
                 client: httpx.Client | None = None, account_id: str | None = None,
                 authserv_id: str | None = None, max_message_bytes: int | None = None,
                 send_quota_per_day: int | None = None, received_time_resolution_ms: int = 1000,
                 preserves_message_id: bool = True, ping: int = 15, poll_interval: float = 10.0,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        if not token and not (username and password):
            raise TransportError("jmap needs a token, or username and password")
        self.session_url = session_url
        self.address = address
        self.authserv_id = authserv_id
        self.account_id = account_id
        self.ping = max(1, int(ping))
        self.poll_interval = poll_interval
        self.clock = clock
        self._sleep = sleep
        self._max_message_bytes = max_message_bytes
        self._quota = send_quota_per_day
        self._resolution = max(1, int(received_time_resolution_ms))
        self._preserves_message_id = preserves_message_id
        if token:
            self._auth = f"Bearer {token}"
        else:
            self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self._lock = threading.Lock()
        self._session: dict | None = None
        self._mailboxes: dict[str, str] | None = None     # mailbox id -> role ("" for none)
        self._streams: set[httpx.Response] = set()

    # ------------------------------------------------------------ HTTP and session

    def _http(self, method: str, url: str, *, json_body: Any = None, content: bytes | None = None,
              headers: dict | None = None, timeout: float = 60.0) -> httpx.Response:
        hdrs = {"Authorization": self._auth, "Accept": "application/json"}
        hdrs.update(headers or {})
        last: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = self._client.request(method, url, headers=hdrs, json=json_body, content=content,
                                            timeout=timeout)
            except httpx.HTTPError as exc:
                last = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    self._sleep(min(8.0, 0.5 * 2 ** attempt))
                    continue
                raise JmapError("network_error", str(exc)) from exc
            if resp.status_code in RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
                self._sleep(retry_delay(resp, attempt))
                continue
            if resp.status_code == 401 and attempt == 0 and self._session is not None:
                self._session = None  # credentials or session rotated: one fresh try
                continue
            if resp.status_code >= 400:
                raise _problem(resp)
            return resp
        raise JmapError("network_error", str(last))

    def session(self) -> dict:
        with self._lock:
            if self._session is None:
                resp = self._http("GET", self.session_url)
                try:
                    session = resp.json()
                except ValueError as exc:
                    raise JmapError("invalid_response", "session is not JSON") from exc
                if not isinstance(session, dict) or CORE not in (session.get("capabilities") or {}):
                    raise JmapError("invalid_response", "not a JMAP session resource")
                if not self.account_id:
                    self.account_id = self._pick_account(session)
                self._session = session
            return self._session

    def _pick_account(self, session: dict) -> str:
        primary = (session.get("primaryAccounts") or {}).get(MAIL)
        accounts = session.get("accounts") or {}
        for acc_id, acc in accounts.items():
            if str(acc.get("name") or "").lower() == self.address.lower() and MAIL in (acc.get("accountCapabilities") or {}):
                return acc_id
        if primary:
            return primary
        for acc_id, acc in accounts.items():
            if MAIL in (acc.get("accountCapabilities") or {}):
                return acc_id
        raise JmapError("accountNotFound", "the session has no mail account")

    def _api(self, calls: list[list]) -> list[list]:
        session = self.session()
        resp = self._http("POST", session["apiUrl"], json_body={"using": USING, "methodCalls": calls},
                          headers={"Content-Type": "application/json"})
        try:
            body = resp.json()
        except ValueError as exc:
            raise JmapError("invalid_response", "API response is not JSON") from exc
        state = body.get("sessionState")
        if state and state != session.get("state"):
            with self._lock:
                self._session = None
        return body.get("methodResponses") or []

    def _call(self, name: str, args: dict) -> dict:
        args = {"accountId": self._account(), **args}
        responses = self._api([[name, args, "0"]])
        return _single(responses, name, "0")

    def _account(self) -> str:
        self.session()
        assert self.account_id
        return self.account_id

    def _limits(self) -> tuple[int | None, int | None, int]:
        session = self.session()
        core = (session.get("capabilities") or {}).get(CORE) or {}
        acc = (session.get("accounts") or {}).get(self._account()) or {}
        mail = (acc.get("accountCapabilities") or {}).get(MAIL) or {}
        upload = core.get("maxSizeUpload")
        attachments = mail.get("maxSizeAttachmentsPerEmail")
        per_get = int(core.get("maxObjectsInGet") or 500)
        return (int(upload) if upload else None, int(attachments) if attachments else None, max(1, per_get))

    def mailbox_roles(self, *, refresh: bool = False) -> dict[str, str]:
        if self._mailboxes is None or refresh:
            got = self._call("Mailbox/get", {"ids": None, "properties": ["id", "name", "role"]})
            self._mailboxes = {m["id"]: str(m.get("role") or "") for m in got.get("list") or []}
        return self._mailboxes

    # ------------------------------------------------------------ contract

    def capabilities(self) -> Capabilities:
        session = self.session()
        upload, attachments, _ = self._limits()
        if self._max_message_bytes:
            limit = self._max_message_bytes
        else:
            bounds = [b for b in (upload, (attachments * 78 // 57 + MIME_OVERHEAD) if attachments else None) if b]
            limit = min(bounds) if bounds else DEFAULT_MAX_MESSAGE_BYTES
        return Capabilities(
            name="jmap", max_message_bytes=limit,
            push="eventsource" if session.get("eventSourceUrl") else "none",
            preserves_message_id=self._preserves_message_id, auth_results="header",
            authserv_id=self.authserv_id, send_quota_per_day=self._quota,
            received_time_resolution_ms=self._resolution, raw_mime=True,
        )

    def send(self, message: Outgoing | bytes) -> Sent:
        if isinstance(message, Outgoing):
            raw = mime.build(message)
        elif isinstance(message, (bytes, bytearray)):
            raw = bytes(message)
        else:
            raise TypeError("send() takes an Outgoing or raw bytes")
        limit = self.capabilities().max_message_bytes
        if len(raw) > limit:
            raise JmapError("tooLarge", f"message is {len(raw)} bytes; the limit is {limit}")
        parsed = mime.parse(raw)
        rcpts = mime.recipients(raw)
        if not rcpts:
            raise TransportError("message has no recipients")
        identity = self._identity(parsed.from_addr)
        roles = self.mailbox_roles()
        target = _role(roles, "sent") or _role(roles, "drafts")
        if target is None:
            raise JmapError("invalidArguments", "the account has no sent or drafts mailbox")
        blob = self._upload(raw)
        account = self._account()
        envelope = {"mailFrom": {"email": parsed.from_addr, "parameters": None},
                    "rcptTo": [{"email": r, "parameters": None} for r in rcpts]}
        responses = self._api([
            ["Email/import", {"accountId": account, "emails": {
                "e": {"blobId": blob, "mailboxIds": {target: True}, "keywords": {"$seen": True}}}}, "0"],
            ["EmailSubmission/set", {"accountId": account, "create": {
                "s": {"identityId": identity, "emailId": "#e", "envelope": envelope}}}, "1"],
        ])
        imported = _single(responses, "Email/import", "0")
        failed = (imported.get("notCreated") or {}).get("e")
        if failed:
            if failed.get("type") != "alreadyExists" or not failed.get("existingId"):
                raise _error(str(failed.get("type") or "notCreated"), str(failed.get("description") or ""))
            submitted = self._call("EmailSubmission/set", {"create": {"s": {
                "identityId": identity, "emailId": failed["existingId"], "envelope": envelope}}})
        else:
            submitted = _single(responses, "EmailSubmission/set", "1")
        bad = (submitted.get("notCreated") or {}).get("s")
        if bad:
            raise _error(str(bad.get("type") or "notCreated"), str(bad.get("description") or ""))
        created = (submitted.get("created") or {}).get("s") or {}
        return Sent(provider_id=str(created.get("id") or "") or None, message_id=parsed.message_id,
                    accepted_at=int(self.clock() * 1000))

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        limit = max(1, int(limit))
        if since is None:
            mode, state, received, ids, partial = "q", self._email_state(), None, (), True
        else:
            mode, state, received, ids, partial = decode_cursor(since)
        if mode == "c":
            try:
                rows, end = self._changes_rows(state, received if partial else None, ids if partial else ())
                return self._finish(rows, end, limit, lambda r, i: encode_cursor("c", state, r, i, True), since)
            except _CannotCalculateChanges:
                mode, state, partial = "q", self._email_state(), True
        rows, end = self._query_rows(state, received if partial else None, ids if partial else (), limit)
        return self._finish(rows, end, limit, lambda r, i: encode_cursor("q", state, r, i, True), since)

    def wait(self, *, since: str | None, timeout: float) -> list[Received]:
        deadline = time.monotonic() + max(0.0, timeout)
        cursor = since
        while True:
            items, cursor = self.list(since=cursor)
            remaining = deadline - time.monotonic()
            if items or remaining <= 0:
                return items
            woke = threading.Event()
            if self.session().get("eventSourceUrl"):
                self._await_state(woke, min(remaining, 30.0))
            else:
                self._sleep(min(self.poll_interval, remaining))

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription:
        start = since if since is not None else self.head()
        state = {"cursor": start}

        def drain() -> None:
            while True:
                items, state["cursor"] = self.list(since=state["cursor"], limit=100)
                for item in items:
                    safe_call(callback, item)
                if not items:
                    return

        def loop(stop: threading.Event) -> None:
            backoff = 1.0
            while not stop.is_set():
                try:
                    url = self.session().get("eventSourceUrl")
                    if not url:
                        drain()
                        stop.wait(self.poll_interval)
                        continue
                    with self._stream(url) as resp:
                        drain()  # connected first, then catch up: nothing falls in between
                        for event, _data in iter_sse(_lines(resp, stop)):
                            if stop.is_set():
                                return
                            if event == "state":
                                drain()
                            backoff = 1.0
                    if not stop.is_set():
                        drain()  # the server closed the stream: catch up, then reconnect
                except Exception:  # noqa: BLE001 - any failure reconnects; catch-up covers the gap
                    if stop.is_set():
                        return
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)

        sub = ThreadSubscription(loop, name=f"jmap-eventsource-{self.address}")
        mailbox = self

        class _Sub:
            def close(self) -> None:
                sub.stopped.set()
                mailbox._drop_streams()
                sub.close()

        return _Sub()

    def close(self) -> None:
        self._drop_streams()
        if self._own_client:
            self._client.close()

    def head(self) -> str:
        return encode_cursor("c", self._email_state())

    # ------------------------------------------------------------ internals

    def _email_state(self) -> str:
        return str(self._call("Email/get", {"ids": [], "properties": ["id"]}).get("state") or "")

    def _identity(self, from_addr: str) -> str:
        got = self._call("Identity/get", {"ids": None})
        want = from_addr.lower()
        wildcard = None
        for ident in got.get("list") or []:
            email = str(ident.get("email") or "").lower()
            if email == want:
                return str(ident["id"])
            if email.startswith("*@") and want.endswith(email[1:]):
                wildcard = str(ident["id"])
        if wildcard:
            return wildcard
        raise JmapError("forbiddenFrom", f"no JMAP identity may send as {from_addr!r}")

    def _upload(self, raw: bytes) -> str:
        url = expand(self.session()["uploadUrl"], accountId=self._account())
        resp = self._http("POST", url, content=raw, headers={"Content-Type": "message/rfc822"}, timeout=300.0)
        blob = (resp.json() or {}).get("blobId")
        if not blob:
            raise JmapError("invalid_response", "upload returned no blobId")
        return str(blob)

    def _download(self, blob_id: str) -> bytes:
        url = expand(self.session()["downloadUrl"], accountId=self._account(), blobId=blob_id,
                     type="message/rfc822", name="message.eml")
        return self._http("GET", url, headers={"Accept": "message/rfc822"}, timeout=300.0).content

    def _get_emails(self, ids: list[str]) -> list[dict]:
        _, _, per_get = self._limits()
        out: list[dict] = []
        for i in range(0, len(ids), per_get):
            got = self._call("Email/get", {"ids": ids[i:i + per_get],
                                           "properties": ["id", "blobId", "receivedAt", "mailboxIds", "size"]})
            out.extend(got.get("list") or [])
        by_id = {e["id"]: e for e in out}
        return [by_id[i] for i in ids if i in by_id]

    def _delivered(self, emails: list[dict]) -> list[dict]:
        """Drop the account's own sends (Emails only in sent/drafts mailboxes)."""
        roles = self.mailbox_roles()
        keep = []
        for email in emails:
            boxes = [b for b, on in (email.get("mailboxIds") or {}).items() if on]
            if any(b not in roles for b in boxes):
                roles = self.mailbox_roles(refresh=True)
            if boxes and all(roles.get(b, "") in OWN_ROLES for b in boxes):
                continue
            email["_t"] = parse_iso_ms(email.get("receivedAt")) or 0
            keep.append(email)
        keep.sort(key=lambda e: e["_t"])  # stable: ties keep the server's order
        return keep

    def _changes_rows(self, state: str, received: Ms | None, ids: tuple[str, ...]) -> tuple[list[dict], str]:
        created: dict[str, None] = {}
        current = state
        while True:
            try:
                got = self._call("Email/changes", {"sinceState": current, "maxChanges": 500})
            except JmapError as exc:
                if exc.type == "cannotCalculateChanges":
                    raise _CannotCalculateChanges() from exc
                raise
            for eid in got.get("created") or []:
                created[eid] = None
            new = str(got.get("newState") or current)
            if not got.get("hasMoreChanges") or new == current:
                current = new
                break
            current = new
        rows = self._delivered(self._get_emails(list(created)))
        return _after(rows, received, ids), current

    def _query_rows(self, state: str, received: Ms | None, ids: tuple[str, ...],
                    limit: int) -> tuple[list[dict], str]:
        filt = {"after": iso_ms((received // 1000) * 1000, fraction=False)} if received is not None else None
        position, rows = 0, []
        while True:
            got = self._call("Email/query", {"filter": filt, "sort": [{"property": "receivedAt", "isAscending": True}],
                                             "position": position, "limit": _PAGE})
            page = [str(i) for i in got.get("ids") or []]
            rows.extend(_after(self._delivered(self._get_emails(page)), received, ids))
            position += len(page)
            if len(page) < _PAGE or len(rows) > limit:
                break
        rows.sort(key=lambda e: e["_t"])
        return rows, state

    def _finish(self, rows: list[dict], end_state: str, limit: int,
                partial_cursor: Callable[[Ms, tuple[str, ...]], str], since: str | None
                ) -> tuple[list[Received], str | None]:
        """Turn ordered rows into Received items with per-item cursors."""
        take = rows[:limit]
        done = len(take) == len(rows)
        out: list[Received] = []
        # Ids at the same receivedAt already consumed (the incoming cursor's, plus this batch's).
        prior = _prior_ids(since)
        for i, row in enumerate(take):
            raw = self._download(str(row["blobId"]))
            t = row["_t"]
            same = tuple(r["id"] for r in take[:i + 1] if r["_t"] == t)
            if prior[0] == t:
                same = tuple(dict.fromkeys(prior[1] + same))
            if done and i == len(take) - 1:
                cursor = encode_cursor("c", end_state, t, same, False)
            else:
                cursor = partial_cursor(t, same)
            out.append(Received(raw_mime=raw, received_at=t, provider_id=str(row["id"]), cursor=cursor))
        if out:
            return out, out[-1].cursor
        if done:
            last_t, last_ids = prior
            return out, encode_cursor("c", end_state, last_t, last_ids, False)
        return out, since

    # ------------------------------------------------------------ EventSource

    def _stream(self, url_template: str):
        url = expand(url_template, types="Email", closeafter="no", ping=str(self.ping))
        req = self._client.build_request("GET", url, headers={
            "Authorization": self._auth, "Accept": "text/event-stream", "Cache-Control": "no-cache"},
            timeout=httpx.Timeout(15.0, read=self.ping * 2 + 15.0))
        mailbox = self

        class _Ctx:
            def __enter__(self) -> httpx.Response:
                self.resp = mailbox._client.send(req, stream=True)
                if self.resp.status_code >= 400:
                    self.resp.read()
                    self.resp.close()
                    raise _problem(self.resp)
                mailbox._streams.add(self.resp)
                return self.resp

            def __exit__(self, *exc) -> None:
                mailbox._streams.discard(self.resp)
                self.resp.close()

        return _Ctx()

    def _await_state(self, woke: threading.Event, seconds: float) -> None:
        """Block on the EventSource until a state event or `seconds` pass (used by `wait`)."""
        stop = threading.Event()
        timer = threading.Timer(seconds, stop.set)
        timer.daemon = True
        timer.start()
        try:
            with self._stream(self.session()["eventSourceUrl"]) as resp:
                for event, _data in iter_sse(_lines(resp, stop)):
                    if event == "state":
                        woke.set()
                        return
                    if stop.is_set():
                        return
        except Exception:  # noqa: BLE001 - wait() falls back to listing again
            self._sleep(min(1.0, seconds))
        finally:
            timer.cancel()

    def _drop_streams(self) -> int:
        """Close every open EventSource response (subscriptions reconnect; used by tests too)."""
        streams = list(self._streams)
        for resp in streams:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        return len(streams)


def _lines(resp: httpx.Response, stop: threading.Event) -> Iterator[str]:
    for line in resp.iter_lines():
        yield line
        if stop.is_set():
            yield ""
            return


def _after(rows: list[dict], received: Ms | None, ids: tuple[str, ...]) -> list[dict]:
    if received is None:
        return rows
    skip = set(ids)
    return [r for r in rows if r["_t"] > received or (r["_t"] == received and r["id"] not in skip)]


def _prior_ids(cursor: str | None) -> tuple[Ms | None, tuple[str, ...]]:
    if not cursor:
        return None, ()
    try:
        _mode, _state, received, ids, _partial = decode_cursor(cursor)
    except TransportError:
        return None, ()
    return received, ids


def _role(roles: Mapping[str, str], role: str) -> str | None:
    for box, r in roles.items():
        if r == role:
            return box
    return None


def _single(responses: list[list], name: str, call_id: str) -> dict:
    for resp in responses:
        if len(resp) == 3 and resp[2] == call_id:
            if resp[0] == "error":
                args = resp[1] or {}
                raise _error(str(args.get("type") or "serverFail"), str(args.get("description") or ""))
            if resp[0] != name:
                raise JmapError("invalid_response", f"expected {name}, got {resp[0]}")
            return resp[1] or {}
    raise JmapError("invalid_response", f"no response for {name}")


def _problem(resp: httpx.Response) -> JmapError:
    """RFC 7807 problem details (RFC 8620 §3.6.1) or a plain HTTP error."""
    type_, detail = f"http_{resp.status_code}", (resp.text or "")[:300]
    try:
        body = resp.json()
        if isinstance(body, dict):
            type_ = str(body.get("type") or type_)
            detail = str(body.get("detail") or body.get("description") or detail)
    except Exception:  # noqa: BLE001
        pass
    return _error(type_, detail, resp.status_code)


def from_config(cfg: Mapping[str, str]) -> JmapMailbox:
    """Keys: session_url, address, and token (or username + password); optional account_id,
    authserv_id, max_message_bytes, send_quota_per_day, received_time_resolution_ms,
    preserves_message_id, ping, poll_interval."""
    if not cfg.get("session_url"):
        raise TransportError("jmap needs session_url (e.g. https://api.fastmail.com/jmap/session)")
    if not cfg.get("address"):
        raise TransportError("jmap needs address")
    return JmapMailbox(
        cfg["session_url"], cfg.get("token") or None, cfg["address"],
        username=cfg.get("username") or None, password=cfg.get("password") or None,
        account_id=cfg.get("account_id") or None, authserv_id=cfg.get("authserv_id") or None,
        max_message_bytes=cfg_int(cfg, "max_message_bytes", None),
        send_quota_per_day=cfg_int(cfg, "send_quota_per_day", None),
        received_time_resolution_ms=cfg_int(cfg, "received_time_resolution_ms", 1000) or 1000,
        preserves_message_id=cfg_bool(cfg, "preserves_message_id", True),
        ping=cfg_int(cfg, "ping", 15) or 15,
        poll_interval=float(cfg_int(cfg, "poll_interval", 10) or 10),
    )
