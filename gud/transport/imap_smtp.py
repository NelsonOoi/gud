"""IMAP (RFC 9051/3501) + SMTP submission (RFC 6409) mailbox, stdlib only (imaplib, smtplib).

- Reads INBOX and the junk folder (configured name, else the folder LIST marks `\\Junk`).
- Cursor: one `<folder>:<UIDVALIDITY>:<UID>` part per folder, joined by `;` (folder names
  percent-encoded). A UIDVALIDITY change rescans that folder from UID 1 (duplicates are
  deduped downstream by gud id).
- Received time is the server's INTERNALDATE (one-second resolution).
- Push intake is IMAP IDLE on INBOX over a dedicated connection, re-issued every `idle_poll`
  seconds (which also bounds how late junk-folder mail is noticed) and reconnected with
  backoff; every wake-up and reconnect catches up with `list()` from the last cursor.
- Python 3.13's imaplib has no IDLE, so the IDLE connection reads its socket unbuffered and
  waits with select(); see `_IdleMixin`.
"""

from __future__ import annotations

import imaplib
import re
import select
import smtplib
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parseaddr
from typing import Callable, Literal, Mapping
from urllib.parse import quote, unquote

from gud.core.types import Ms

from . import mime
from .base import (
    Capabilities, Outgoing, QuotaExceeded, Received, Sent, Subscription, ThreadSubscription,
    TransportError, cfg_int, cfg_list, safe_call,
)

Tls = Literal["ssl", "starttls", "none"]
DEFAULT_MAX_MESSAGE_BYTES = 10 * 1024 * 1024
_FETCH_BATCH = 16
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
_INTERNALDATE = re.compile(rb'INTERNALDATE "\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)"')
_UID = re.compile(rb"\bUID (\d+)")
_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$')
_QUOTA_WORDS = ("quota", "rate", "limit", "too many", "exceeded")


def parse_internaldate(meta: bytes) -> Ms | None:
    m = _INTERNALDATE.search(meta)
    if not m:
        return None
    day, mon, year, hh, mm, ss, sign, oh, om = m.groups()
    month = _MONTHS.get(mon.decode().capitalize())
    if month is None:
        return None
    offset = timedelta(hours=int(oh), minutes=int(om))
    tz = timezone(offset if sign == b"+" else -offset)
    dt = datetime(int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=tz)
    return int(dt.timestamp()) * 1000


def encode_cursor(state: Mapping[str, tuple[int, int]], order: list[str]) -> str:
    return ";".join(f"{quote(f, safe='')}:{state[f][0]}:{state[f][1]}" for f in order if f in state)


def decode_cursor(cursor: str | None) -> dict[str, tuple[int, int]]:
    if not cursor:
        return {}
    out: dict[str, tuple[int, int]] = {}
    for part in cursor.split(";"):
        try:
            folder, uv, uid = part.rsplit(":", 2)
            out[unquote(folder)] = (int(uv), int(uid))
        except ValueError as exc:
            raise TransportError(f"invalid imap cursor {cursor!r}") from exc
    return out


def _quote_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote_mailbox(token: bytes) -> str:
    text = token.decode("utf-8", "replace").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = re.sub(r'\\(.)', r"\1", text[1:-1])
    return text


class _IdleMixin:
    """Unbuffered socket file so select() on the socket tells the truth about pending input."""

    def _rawfile(self) -> None:
        self.file = self.sock.makefile("rb", buffering=0)  # type: ignore[attr-defined]

    def read(self, size: int) -> bytes:
        chunks, got = [], 0
        while got < size:
            data = self.file.read(size - got)  # type: ignore[attr-defined]
            if not data:
                break
            chunks.append(data)
            got += len(data)
        return b"".join(chunks)


class _IdleIMAP(_IdleMixin, imaplib.IMAP4):
    def open(self, host: str = "", port: int = imaplib.IMAP4_PORT, timeout: float | None = None) -> None:
        super().open(host, port, timeout)
        self._rawfile()

    def starttls(self, ssl_context=None):  # type: ignore[override]
        result = super().starttls(ssl_context)
        self._rawfile()
        return result


class _IdleIMAP_SSL(_IdleMixin, imaplib.IMAP4_SSL):
    def open(self, host: str = "", port: int = imaplib.IMAP4_SSL_PORT, timeout: float | None = None) -> None:
        super().open(host, port, timeout)
        self._rawfile()


def _readable(conn: imaplib.IMAP4, timeout: float) -> bool:
    sock = conn.sock
    if isinstance(sock, ssl.SSLSocket) and sock.pending() > 0:
        return True
    ready, _, _ = select.select([sock], [], [], max(0.0, timeout))
    return bool(ready)


class ImapSmtpMailbox:
    def __init__(self, imap_host: str, imap_port: int, smtp_host: str, smtp_port: int,
                 username: str, password: str, address: str, *,
                 folders: tuple[str, ...] = ("INBOX", "Junk"),
                 imap_tls: Tls = "ssl", smtp_tls: Tls = "starttls",
                 smtp_username: str | None = None, smtp_password: str | None = None,
                 max_message_bytes: int | None = None, authserv_id: str | None = None,
                 send_quota_per_day: int | None = None, preserves_message_id: bool = True,
                 idle_poll: float = 10.0, timeout: float = 60.0,
                 ssl_context: ssl.SSLContext | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.imap_host, self.imap_port = imap_host, int(imap_port)
        self.smtp_host, self.smtp_port = smtp_host, int(smtp_port)
        self.username, self.password = username, password
        self.smtp_username = smtp_username if smtp_username is not None else username
        self.smtp_password = smtp_password if smtp_password is not None else password
        self.address = address
        self.folders = tuple(folders)
        self.imap_tls, self.smtp_tls = imap_tls, smtp_tls
        self.max_message_bytes = max_message_bytes
        self.authserv_id = authserv_id
        self.send_quota_per_day = send_quota_per_day
        self.preserves_message_id = preserves_message_id
        self.idle_poll = idle_poll
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.clock = clock
        self._lock = threading.RLock()
        self._conn: imaplib.IMAP4 | None = None
        self._resolved: list[str] | None = None
        self._has_idle: bool | None = None
        self._smtp_size: int | None = None
        self._smtp_probed = False
        self._wait_lock = threading.Lock()
        self._wait_conn: imaplib.IMAP4 | None = None
        self._idle_conns: set[imaplib.IMAP4] = set()

    # ------------------------------------------------------------ connections

    def _connect(self, *, idle: bool = False) -> imaplib.IMAP4:
        ctx = self.ssl_context or ssl.create_default_context()
        try:
            if self.imap_tls == "ssl":
                cls = _IdleIMAP_SSL if idle else imaplib.IMAP4_SSL
                conn = cls(self.imap_host, self.imap_port, ssl_context=ctx, timeout=self.timeout)
            else:
                cls = _IdleIMAP if idle else imaplib.IMAP4
                conn = cls(self.imap_host, self.imap_port, timeout=self.timeout)
                if self.imap_tls == "starttls":
                    conn.starttls(ssl_context=ctx)
            conn.login(self.username, self.password)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise TransportError(f"imap connect/login to {self.imap_host}:{self.imap_port} failed: {exc}") from exc
        self._has_idle = "IDLE" in conn.capabilities
        return conn

    def _main(self) -> imaplib.IMAP4:
        if self._conn is None:
            self._conn = self._connect()
            self._resolved = None
        return self._conn

    def _drop_main(self) -> None:
        conn, self._conn = self._conn, None
        self._resolved = None
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass

    def _with_conn(self, fn: Callable[[imaplib.IMAP4], object]) -> object:
        """Run fn on the shared connection; reconnect and retry once on a connection error."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    return fn(self._main())
                except (imaplib.IMAP4.abort, OSError, EOFError) as exc:
                    self._drop_main()
                    if attempt:
                        raise TransportError(f"imap connection failed: {exc}") from exc
                except imaplib.IMAP4.error as exc:
                    raise TransportError(f"imap error: {exc}") from exc
        raise AssertionError("unreachable")

    def _resolve_folders(self, conn: imaplib.IMAP4) -> list[str]:
        if self._resolved is not None and conn is self._conn:
            return self._resolved
        typ, data = conn.list()
        names: list[tuple[str, bytes]] = []
        for line in data or []:
            if not isinstance(line, bytes):
                continue
            m = _LIST_LINE.match(line.strip())
            if m:
                names.append((_unquote_mailbox(m.group("name")), m.group("flags").lower()))
        out: list[str] = []
        for want in self.folders:
            exact = [n for n, _ in names if n == want or (want.upper() == "INBOX" and n.upper() == "INBOX")]
            if exact:
                found = exact[0]
            elif want.lower() in ("junk", "spam"):
                special = [n for n, flags in names if b"\\junk" in flags]
                if not special:
                    continue
                found = special[0]
            else:
                continue
            if found not in out:
                out.append(found)
        if conn is self._conn:
            self._resolved = out
        return out

    # ------------------------------------------------------------ contract

    def capabilities(self) -> Capabilities:
        if self._has_idle is None:
            try:
                self._with_conn(lambda conn: None)
            except TransportError:
                pass
        limit = self.max_message_bytes or self._probe_smtp_size() or DEFAULT_MAX_MESSAGE_BYTES
        return Capabilities(
            name="imap_smtp", max_message_bytes=limit, push="idle" if self._has_idle else "none",
            preserves_message_id=self.preserves_message_id, auth_results="header",
            authserv_id=self.authserv_id, send_quota_per_day=self.send_quota_per_day,
            received_time_resolution_ms=1000, raw_mime=True,
        )

    def send(self, message: Outgoing | bytes) -> Sent:
        if isinstance(message, Outgoing):
            raw = mime.build(message, date=formatdate(self.clock(), usegmt=True))
            rcpts = tuple(parseaddr(a)[1] or a for a in (*message.to, *message.cc))
            sender = parseaddr(message.from_addr)[1] or message.from_addr
        elif isinstance(message, (bytes, bytearray)):
            raw = bytes(message)
            rcpts = mime.recipients(raw)
            sender = mime.parse(raw).from_addr
        else:
            raise TypeError("send() takes an Outgoing or raw bytes")
        rcpts = tuple(dict.fromkeys(r for r in rcpts if r))
        if not rcpts:
            raise TransportError("message has no recipients")
        limit = self.max_message_bytes or self._smtp_size
        if limit and len(raw) > limit:
            raise TransportError(f"message is {len(raw)} bytes; the limit is {limit}")
        try:
            with self._smtp() as smtp:
                refused = smtp.sendmail(sender or self.address, list(rcpts), raw)
        except smtplib.SMTPRecipientsRefused as exc:
            raise TransportError(f"all recipients refused: {exc.recipients}") from exc
        except smtplib.SMTPResponseException as exc:
            text = (exc.smtp_error or b"").decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
            if any(word in text.lower() for word in _QUOTA_WORDS):
                raise QuotaExceeded(f"smtp {exc.smtp_code}: {text}") from exc
            raise TransportError(f"smtp {exc.smtp_code}: {text}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise TransportError(f"smtp send failed: {exc}") from exc
        if refused and len(refused) == len(rcpts):
            raise TransportError(f"all recipients refused: {refused}")
        return Sent(provider_id=None, message_id=mime.parse(raw).message_id,
                    accepted_at=int(self.clock() * 1000))

    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]:
        state = decode_cursor(since)
        limit = max(1, int(limit))

        def run(conn: imaplib.IMAP4) -> tuple[list[Received], str | None]:
            folders = self._resolve_folders(conn)
            streams: list[tuple[str, int, list[tuple[int, Ms, bytes]]]] = []
            current = dict(state)
            for folder in folders:
                uv, msgs = self._scan_folder(conn, folder, current.get(folder), limit)
                prev = current.get(folder)
                last = prev[1] if prev and prev[0] == uv else 0
                current[folder] = (uv, last)
                streams.append((folder, uv, msgs))
            order = folders + [f for f in current if f not in folders]
            out: list[Received] = []
            heads = [0] * len(streams)
            while len(out) < limit:
                best = None
                for i, (folder, uv, msgs) in enumerate(streams):
                    if heads[i] < len(msgs):
                        key = (msgs[heads[i]][1], i)
                        if best is None or key < best[0]:
                            best = (key, i)
                if best is None:
                    break
                i = best[1]
                folder, uv, msgs = streams[i]
                uid, when, raw = msgs[heads[i]]
                heads[i] += 1
                current[folder] = (uv, uid)
                out.append(Received(raw_mime=raw, received_at=when, provider_id=f"{folder}:{uv}:{uid}",
                                    cursor=encode_cursor(current, order)))
            return out, (out[-1].cursor if out else since)

        return self._with_conn(run)  # type: ignore[return-value]

    def head(self) -> str | None:
        """Cursor positioned after the newest message of every folder."""

        def run(conn: imaplib.IMAP4) -> str | None:
            folders = self._resolve_folders(conn)
            state: dict[str, tuple[int, int]] = {}
            for folder in folders:
                typ, data = conn.status(_quote_mailbox(folder), "(UIDNEXT UIDVALIDITY)")
                text = b" ".join(d for d in data if isinstance(d, bytes))
                nxt = re.search(rb"UIDNEXT (\d+)", text)
                uv = re.search(rb"UIDVALIDITY (\d+)", text)
                if typ != "OK" or not nxt or not uv:
                    raise TransportError(f"STATUS {folder} failed: {data!r}")
                state[folder] = (int(uv.group(1)), int(nxt.group(1)) - 1)
            return encode_cursor(state, folders) if state else None

        return self._with_conn(run)  # type: ignore[return-value]

    def wait(self, *, since: str | None, timeout: float) -> list[Received]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            items, _ = self.list(since=since)
            remaining = deadline - time.monotonic()
            if items or remaining <= 0:
                return items
            chunk = min(remaining, self.idle_poll)
            if self._has_idle:
                try:
                    with self._wait_lock:
                        if self._wait_conn is None:
                            self._wait_conn = self._connect(idle=True)
                            self._select_inbox(self._wait_conn)
                        self._idle_once(self._wait_conn, chunk)
                except (TransportError, imaplib.IMAP4.error, OSError, EOFError):
                    self._close_quietly(self._wait_conn)
                    self._wait_conn = None
                    time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            else:
                time.sleep(min(chunk, 2.0))

    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription:
        start = since if since is not None else self.head()

        def loop(stop: threading.Event) -> None:
            cursor = start
            backoff = 1.0
            while not stop.is_set():
                conn = None
                try:
                    conn = self._connect(idle=True)
                    self._idle_conns.add(conn)
                    self._select_inbox(conn)
                    cursor = self._drain(cursor, callback, stop)
                    backoff = 1.0
                    while not stop.is_set():
                        if self._has_idle:
                            self._idle_once(conn, self.idle_poll, stop)
                        else:
                            stop.wait(min(self.idle_poll, 5.0))
                        cursor = self._drain(cursor, callback, stop)
                except (TransportError, imaplib.IMAP4.error, OSError, EOFError, ValueError):
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                finally:
                    if conn is not None:
                        self._idle_conns.discard(conn)
                        self._close_quietly(conn)

        return ThreadSubscription(loop, name=f"imap-idle-{self.address}")

    def close(self) -> None:
        with self._lock:
            self._drop_main()
        with self._wait_lock:
            self._close_quietly(self._wait_conn)
            self._wait_conn = None

    # ------------------------------------------------------------ helpers

    def _drain(self, cursor: str | None, callback: Callable[[Received], None], stop: threading.Event) -> str | None:
        while not stop.is_set():
            items, nxt = self.list(since=cursor, limit=100)
            for item in items:
                if stop.is_set():
                    return cursor
                safe_call(callback, item)
                cursor = item.cursor
            if not items:
                return nxt if nxt is not None else cursor
        return cursor

    def _scan_folder(self, conn: imaplib.IMAP4, folder: str, prev: tuple[int, int] | None,
                     limit: int) -> tuple[int, list[tuple[int, Ms, bytes]]]:
        typ, data = conn.select(_quote_mailbox(folder), readonly=True)
        if typ != "OK":
            raise TransportError(f"EXAMINE {folder} failed: {data!r}")
        _, uvdata = conn.response("UIDVALIDITY")
        uv = int(uvdata[0]) if uvdata and uvdata[0] else 0
        last = prev[1] if prev and prev[0] == uv else 0
        typ, data = conn.uid("SEARCH", None, f"UID {last + 1}:*")
        if typ != "OK":
            raise TransportError(f"UID SEARCH {folder} failed: {data!r}")
        uids = sorted(int(x) for x in (data[0] or b"").split() if int(x) > last)[:limit]
        msgs: list[tuple[int, Ms, bytes]] = []
        for i in range(0, len(uids), _FETCH_BATCH):
            batch = uids[i:i + _FETCH_BATCH]
            typ, data = conn.uid("FETCH", ",".join(str(u) for u in batch), "(UID INTERNALDATE BODY.PEEK[])")
            if typ != "OK":
                raise TransportError(f"UID FETCH {folder} failed: {data!r}")
            msgs.extend(self._parse_fetch(data))
        msgs = sorted((m for m in msgs if m[0] > last), key=lambda m: m[0])
        return uv, msgs

    def _parse_fetch(self, data: list) -> list[tuple[int, Ms, bytes]]:
        out = []
        i = 0
        while i < len(data):
            item = data[i]
            if isinstance(item, tuple):
                meta, body = item[0], item[1]
                if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                    meta = meta + b" " + data[i + 1]
                    i += 1
                uid = _UID.search(meta)
                when = parse_internaldate(meta)
                if uid and body is not None:
                    out.append((int(uid.group(1)), when if when is not None else int(self.clock()) * 1000, body))
            i += 1
        return out

    def _select_inbox(self, conn: imaplib.IMAP4) -> None:
        typ, data = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise TransportError(f"EXAMINE INBOX failed: {data!r}")

    def _idle_once(self, conn: imaplib.IMAP4, seconds: float, stop: threading.Event | None = None) -> bool:
        """One IDLE round: returns True when the server reported new mail, False on timeout."""
        tag = conn._new_tag()  # type: ignore[attr-defined]
        conn.tagged_commands.pop(tag, None)  # type: ignore[attr-defined]
        conn.send(tag + b" IDLE\r\n")
        while True:
            line = conn.readline()
            if not line:
                raise imaplib.IMAP4.abort("connection closed during IDLE")
            if line.startswith(b"+"):
                break
            if line.startswith(tag):
                raise imaplib.IMAP4.error(f"IDLE refused: {line!r}")
        activity = False
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if stop is not None and stop.is_set():
                break
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            if _readable(conn, min(0.25, remaining)):
                line = conn.readline()
                if not line:
                    raise imaplib.IMAP4.abort("connection closed during IDLE")
                if b"EXISTS" in line or b"RECENT" in line:
                    activity = True
                    break
                if line.startswith(b"* BYE"):
                    raise imaplib.IMAP4.abort("server closed IDLE")
        conn.send(b"DONE\r\n")
        while True:
            line = conn.readline()
            if not line:
                raise imaplib.IMAP4.abort("connection closed ending IDLE")
            if line.startswith(tag + b" "):
                if not line[len(tag) + 1:].startswith(b"OK"):
                    raise imaplib.IMAP4.error(f"IDLE failed: {line!r}")
                break
        return activity

    def _smtp(self) -> smtplib.SMTP:
        ctx = self.ssl_context or ssl.create_default_context()
        if self.smtp_tls == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self.timeout, context=ctx)
        else:
            smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)
        try:
            smtp.ehlo()
            if self.smtp_tls == "starttls":
                smtp.starttls(context=ctx)
                smtp.ehlo()
            size = smtp.esmtp_features.get("size", "").strip()
            if size.isdigit() and int(size) > 0:
                self._smtp_size = int(size)
            self._smtp_probed = True
            if self.smtp_username and "auth" in smtp.esmtp_features:
                smtp.login(self.smtp_username, self.smtp_password or "")
        except BaseException:
            try:
                smtp.close()
            except Exception:
                pass
            raise
        return smtp

    def _probe_smtp_size(self) -> int | None:
        if not self._smtp_probed:
            try:
                with self._smtp():
                    pass
            except (smtplib.SMTPException, OSError):
                self._smtp_probed = True
        return self._smtp_size

    @staticmethod
    def _close_quietly(conn: imaplib.IMAP4 | None) -> None:
        if conn is None:
            return
        try:
            conn.shutdown()
        except Exception:
            pass

    def _drop_idle_connections(self) -> int:
        """Test hook: break every live IDLE socket (a forced disconnect)."""
        n = 0
        for conn in list(self._idle_conns):
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
                n += 1
            except OSError:
                pass
        return n


def from_config(cfg: Mapping[str, str]) -> ImapSmtpMailbox:
    """Keys: imap_host, imap_port, smtp_host, smtp_port, username, password, address, folders,
    imap_tls (ssl|starttls|none), smtp_tls (ssl|starttls|none), smtp_username, smtp_password,
    max_message_bytes, authserv_id, send_quota_per_day, idle_poll."""
    imap_host = cfg.get("imap_host") or cfg.get("host")
    if not imap_host:
        raise TransportError("imap_smtp needs imap_host")
    username = cfg.get("username") or ""
    address = cfg.get("address") or (username if "@" in username else "")
    if not address:
        raise TransportError("imap_smtp needs address")
    imap_tls = (cfg.get("imap_tls") or "ssl").lower()
    smtp_tls = (cfg.get("smtp_tls") or "starttls").lower()
    for value in (imap_tls, smtp_tls):
        if value not in ("ssl", "starttls", "none"):
            raise TransportError(f"invalid tls mode {value!r}")
    imap_port = cfg_int(cfg, "imap_port", 993 if imap_tls == "ssl" else 143)
    smtp_port = cfg_int(cfg, "smtp_port", {"ssl": 465, "starttls": 587, "none": 25}[smtp_tls])
    return ImapSmtpMailbox(
        imap_host, int(imap_port or 0), cfg.get("smtp_host") or imap_host, int(smtp_port or 0),
        username, cfg.get("password") or "", address,
        folders=cfg_list(cfg, "folders", ("INBOX", "Junk")),
        imap_tls=imap_tls,  # type: ignore[arg-type]
        smtp_tls=smtp_tls,  # type: ignore[arg-type]
        smtp_username=cfg.get("smtp_username"), smtp_password=cfg.get("smtp_password"),
        max_message_bytes=cfg_int(cfg, "max_message_bytes", None),
        authserv_id=cfg.get("authserv_id") or None,
        send_quota_per_day=cfg_int(cfg, "send_quota_per_day", None),
        idle_poll=float(cfg.get("idle_poll") or 10.0),
    )
