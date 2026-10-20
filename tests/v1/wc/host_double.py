"""TEST DOUBLE of the v1 host mirror (SPEC-v1 §11, §21). Not the product host.

The real host (`gud/host_v1.py`: intake + dispatch + the §21 FastAPI app) is written
separately. This double is the smallest host the working-copy tests need, built only from
`gud.core` (host log, replay) and `gud.transport` (FakeMailbox, mime):

- `pull()` is the intake: list the repo address's FakeMailbox, log every decoding v1 envelope
  message and every unsigned message that passes §8.1 (2)/(3) at the tip, append a `tick` when
  the head is more than a second old, and replay (for the tip members that mirror auth needs).
- `app()` serves §21 (`/api/v1/info|log|mail|mail/<id>|object/<sha>|snapshot/<sha>|delivery`)
  with its own mirror-auth v1 check (written here independently of `gud.wc.remote`).
- The double also implements the `Mirror` calls directly, for in-process tests.
- `kit_send(...)` signs messages with `tests/v1/kit.py` keys and delivers them into the repo
  mailbox, so a test can drive consent (approve/object) and testers without a client.

To run the same tests against the real host, build the app with
`gud.host_v1.create_app(intake, dispatcher, registry, repo=REPO)` instead of `HostDouble.app()`
(see `tests/v1/wc/conftest.py`, fixture `net`).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gud.core import envelope as E
from gud.core.config import bare_address
from gud.core.envelope import sha256_hex
from gud.core.hostlog import HostLog, encode_entry
from gud.core.objects import MemoryStore
from gud.core.replay import ReplayError, Replayer
from gud.core.types import Record, Seen
from gud.transport import mime
from gud.transport.base import Outgoing
from gud.transport.fake import DEFAULT_MAX_MESSAGE_BYTES, FakeMailbox
from gud.wc.remote import snapshot_at

from tests.v1.kit import REPO, Kit


class Clock:
    """Virtual wall clock in seconds (float), shared by mailboxes, host and clients."""

    def __init__(self, start_ms: int) -> None:
        self.ms = start_ms

    def __call__(self) -> float:
        return self.ms / 1000

    def advance(self, ms: int) -> None:
        self.ms += ms


class HostDouble:
    """TEST DOUBLE: a minimal §21 host over a FakeMailbox root."""

    def __init__(self, root: Path, kit: Kit, clock: Clock, *, repo: str = REPO,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
        self.root = Path(root)
        self.kit = kit
        self.clock = clock
        self.repo = repo
        self.max_message_bytes = max_message_bytes
        self.mail_root = self.root / "mail"
        self.mailbox = FakeMailbox(self.mail_root, repo, clock=clock, max_message_bytes=max_message_bytes)
        self.store = MemoryStore()
        self.key = kit.host_key
        self.log = HostLog.open(repo, self.key, clock.ms)
        self.raw: dict[str, bytes] = {}
        self.cursor: str | None = None
        self.replayer = Replayer(self.store)
        self.deliveries: dict[str, Any] = {}
        self.requests: list[tuple[str, str, str | None]] = []   # (method, path, handle) seen by app()
        self.auth_required = True

    # ------------------------------------------------------------ mail accounts

    def mailbox_for(self, address: str) -> FakeMailbox:
        return FakeMailbox(self.mail_root, address, clock=self.clock, max_message_bytes=self.max_message_bytes)

    # ------------------------------------------------------------ intake

    def pull(self) -> None:
        appended = False
        while True:
            items, cursor = self.mailbox.list(since=self.cursor)
            if not items:
                break
            for item in items:
                email = mime.to_email(item.raw_mime)
                if email is None or self.log.seq_of(email.gud_id) is not None:
                    continue
                if email.envelope is None and not self._unsigned_ok(email):
                    continue
                now = max(self.clock.ms, self.log.head.time + 1)
                self.log.append_mail(email.gud_id, min(item.received_at, now), self.key, now)
                self.raw[email.gud_id] = item.raw_mime
                appended = True
            self.cursor = cursor
        if not appended and self.clock.ms - self.log.head.time >= 1000:
            self.log.append_tick(self.key, self.clock.ms)
        self._replay()

    def _replay(self) -> None:
        have = len(self.replayer.log())
        entries = self.log.entries(have)
        emails = [mime.to_email(self.raw[gid]) for _, gid, _ in self.log.mail_ids(have)]
        try:
            self.replayer.feed([e for e in emails if e is not None], entries, self.clock.ms)
        except ReplayError:
            pass

    def _unsigned_ok(self, email) -> bool:
        self._replay()
        if self.replayer.tip is None:
            return False
        rules = self.replayer.reader().rules()
        if not rules.config.authserv_id:
            return False
        sender = bare_address(email.from_addr)
        member = rules.members.by_address(sender)
        return member is not None and member.role == "human" and \
            E.dmarc_pass(email.auth_results, rules.config.authserv_id, sender.rpartition("@")[2])

    # ------------------------------------------------------------ driving consent with kit keys

    def seen(self) -> Seen:
        return Seen(self.log.head.seq, self.log.head.hash)

    def kit_send(self, kind: str, author: str, headers: Mapping[str, str], records: Sequence[Record] = ()) -> str:
        """Sign with the kit key of `author` and deliver from the author's address."""
        env = E.build(kind, author=author, headers=dict(headers), records=tuple(records),
                      sent=int(self.clock()), seen=self.seen())
        assert E.validate_kind(env) is None, E.validate_kind(env)
        env = E.sign(env, self.kit.key(author))
        gid = E.gud_id(env)
        addr = self.kit.address(author)
        self.mailbox_for(addr).send(Outgoing(
            from_addr=addr, to=(self.repo,), subject=mime.subject_for(env, gid, kind),
            text=mime.render_human_part(env, gid), envelope=E.encode(env)))
        return gid

    def approve(self, who: str, target: str) -> str:
        return self.kit_send("approve", who, {"Gud-Target": target})

    def kit_object(self, who: str, target: str, reason: str = "no") -> str:
        return self.kit_send("object", who, {"Gud-Target": target}, (Record("text", (), reason.encode()),))

    # ------------------------------------------------------------ the Mirror calls

    def info(self) -> dict:
        head = self.log.head
        return {"repo": self.repo, "bugs_address": "bugs@" + self.repo.rpartition("@")[2],
                "host_key": self.log.key, "log_head": {"seq": head.seq, "hash": head.hash, "time": head.time},
                "max_message_bytes": self.max_message_bytes, "transport": "fake"}

    def log_page(self, since: int, limit: int) -> dict:
        head = self.log.head
        return {"entries": [encode_entry(e).decode() for e in self.log.entries(since, limit)],
                "head": {"seq": head.seq, "hash": head.hash}}

    def mail_batch(self, since: int, limit: int) -> dict:
        out, nxt = [], None
        for entry in self.log.entries(max(0, since)):
            if entry.type != "mail":
                continue
            if len(out) >= limit:
                nxt = entry.seq
                break
            gid = entry.fields[0][1]
            out.append({"seq": entry.seq, "id": gid, "raw": base64.b64encode(self.raw[gid]).decode()})
        return {"mail": out, "next": nxt}

    def mail(self, gud_id: str) -> bytes | None:
        return self.raw.get(gud_id)

    def get_object(self, sha256: str) -> bytes | None:
        return self.store.get(sha256) if self.store.has(sha256) else None

    def snapshot(self, sha256: str) -> bytes | None:
        for _, gid, _ in self.log.mail_ids():
            email = mime.to_email(self.raw[gid])
            env = email.envelope if email else None
            if env is None or env.kind != "checkpoint" or env.get("Gud-Snapshot") != sha256:
                continue
            seq = E.parse_seen(env.get("Gud-Log")).seq
            data = snapshot_at(self.log.entries(), lambda i: mime.to_email(self.raw[i]) if i in self.raw else None,
                               seq, self.store)
            return data if sha256_hex(data) == sha256 else None
        return None

    def delivery(self, body: Mapping[str, Any]) -> dict:
        self.deliveries["-"] = dict(body)
        return {"ok": True}

    # ------------------------------------------------------------ §21 over HTTP

    def _members(self):
        return self.replayer.reader().members() if self.replayer.tip is not None else None

    def _auth(self, method: str, path_with_query: str, body: bytes, headers: Mapping[str, str]) -> str | None:
        """Mirror auth v1, written independently of gud.wc.remote.verify_request."""
        members = self._members()
        if members is None:
            return "-"                                   # before genesis there is no one to check
        handle, token, sig = headers.get("x-gud-handle"), headers.get("x-gud-time"), headers.get("x-gud-sig")
        if not handle or not token or not sig:
            return None
        try:
            if abs(self.clock() - float(token)) > 300:
                return None
        except ValueError:
            return None
        member = members.get(handle)
        if member is None or not member.public_key:
            return None
        payload = f"{method} {path_with_query} {token} {sha256_hex(body)}".encode()
        return handle if E.verify_bytes(member.public_key, payload, sig) else None

    def app(self):
        app = FastAPI()
        double = self

        @app.middleware("http")
        async def auth(request: Request, call_next):
            path = request.url.path
            pq = path + (f"?{request.url.query}" if request.url.query else "")
            body = await request.body()
            handle = None
            if path != "/api/v1/info":
                double.pull()
                handle = double._auth(request.method, pq, body, request.headers)
                if handle is None and double.auth_required:
                    double.requests.append((request.method, pq, None))
                    return JSONResponse({"error": "mirror auth failed"}, status_code=401)
            double.requests.append((request.method, pq, handle))
            return await call_next(request)

        @app.get("/api/v1/info")
        def info():
            return double.info()

        @app.get("/api/v1/log")
        def log(since: int = 0, limit: int = 500):
            return double.log_page(since, limit)

        @app.get("/api/v1/mail")
        def mail_batch(since: int = 0, limit: int = 500):
            return double.mail_batch(since, limit)

        @app.get("/api/v1/mail/{gid}")
        def mail(gid: str):
            raw = double.mail(gid)
            if raw is None:
                return Response(status_code=404)
            return Response(raw, media_type="message/rfc822")

        @app.get("/api/v1/object/{sha}")
        def obj(sha: str):
            data = double.get_object(sha)
            return Response(status_code=404) if data is None else Response(data, media_type="application/octet-stream")

        @app.get("/api/v1/snapshot/{sha}")
        def snap(sha: str):
            data = double.snapshot(sha)
            return Response(status_code=404) if data is None else Response(data, media_type="application/json")

        @app.post("/api/v1/delivery")
        async def delivery(request: Request):
            body = json.loads(await request.body() or b"{}")
            double.deliveries[request.headers.get("x-gud-handle", "-")] = body
            return {"ok": True}

        return app


class InProcess:
    """Wrap the double as an in-process `Mirror` whose reads trigger intake (like a live host)."""

    def __init__(self, double: HostDouble) -> None:
        self.d = double

    def info(self):
        return self.d.info()

    def log(self, since, limit):
        return self.d.log_page(since, limit)

    def mail_batch(self, since, limit):
        return self.d.mail_batch(since, limit)

    def mail(self, gid):
        return self.d.mail(gid)

    def object(self, sha):
        return self.d.get_object(sha)

    def snapshot(self, sha):
        return self.d.snapshot(sha)

    def delivery(self, body):
        return self.d.delivery(body)

    def pull(self):
        self.d.pull()
