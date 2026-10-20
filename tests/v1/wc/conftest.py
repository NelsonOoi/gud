"""Fixtures for the working-copy tests: kit members, a virtual clock, the real v1 host
(`gud.host_v1`: Intake + Dispatcher + the §21 app) over a FakeMailbox root, and RepoClients
that reach it over HTTP (FastAPI TestClient) or in process."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from fastapi import Request

from gud.core import envelope as E
from gud.core.hostlog import HostLog, encode_entry
from gud.core.types import Hex, Record, Seen
from gud.host_v1 import Host, open_host
from gud.transport import mime
from gud.transport.base import Outgoing
from gud.transport.fake import DEFAULT_MAX_MESSAGE_BYTES, FakeMailbox
from gud.wc.copy import WorkingCopy, create_genesis
from gud.wc.remote import RepoClient

from tests.v1.kit import REPO, T0, Kit


class Clock:
    """Virtual wall clock in seconds (float), shared by mailboxes, host and clients."""

    def __init__(self, start_ms: int) -> None:
        self.ms = start_ms

    def __call__(self) -> float:
        return self.ms / 1000

    def advance(self, ms: int) -> None:
        self.ms += ms


class RealHost:
    """The product host (`gud.host_v1.open_host`) on a FakeMailbox root, driven by the test:
    `pull()` is what the intake loop does (catch-up from the repo mailbox, then a tick when one
    is due); `kit_send` signs with the kit keys and mails the repo like any member would."""

    def __init__(self, root: Path, kit: Kit, clock: Clock, *, repo: str = REPO,
                 max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
        self.root = Path(root)
        self.kit = kit
        self.clock = clock
        self.repo = repo
        self.max_message_bytes = max_message_bytes
        self.mail_root = self.root / "mail"
        self.mailbox = FakeMailbox(self.mail_root, repo, clock=clock, max_message_bytes=max_message_bytes)
        self.host: Host = open_host(self.root, repo=repo, mailbox=self.mailbox, host_private=kit.host_key,
                                    clock=clock)
        self.intake = self.host.intake
        self.requests: list[tuple[str, str, str | None]] = []   # (method, path, X-Gud-Handle) seen by app()

    # ------------------------------------------------------------ mail accounts and intake

    def mailbox_for(self, address: str) -> FakeMailbox:
        return FakeMailbox(self.mail_root, address, clock=self.clock, max_message_bytes=self.max_message_bytes)

    def pull(self) -> None:
        self.intake.catch_up()
        self.intake.tick()

    @property
    def deliveries(self) -> dict[str, dict]:
        out = {}
        for d in self.host.registry.all():
            out[d.handle] = {"webhook": d.url} if d.channel == "webhook" else {d.channel: True}
        return out

    # ------------------------------------------------------------ driving consent with kit keys

    def seen(self) -> Seen:
        return self.intake.seen()

    def kit_send(self, kind: str, author: str, headers: Mapping[str, str], records: Sequence[Record] = ()) -> str:
        """Sign with the kit key of `author` and send from the author's address."""
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

    # ------------------------------------------------------------ a misbehaving host

    def forge_log(self, key: bytes) -> None:
        """Replace the served log with a different one signed by `key` (a forking host)."""
        self.intake._log = HostLog.open(self.repo, key, self.clock.ms + 5)
        self._forged_key = key

    def forged_tick(self) -> None:
        self.intake._log.append_tick(self._forged_key, self.clock.ms)

    # ------------------------------------------------------------ §21 over HTTP

    def app(self):
        app = self.host.app()
        harness = self

        @app.middleware("http")
        async def record(request: Request, call_next):
            pq = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            harness.requests.append((request.method, pq, request.headers.get("x-gud-handle")))
            return await call_next(request)

        return app


class IntakeMirror:
    """The `gud.wc.remote.Mirror` calls served in process by a real Intake (no HTTP); reads
    trigger intake like a live host's push path would."""

    def __init__(self, host: RealHost, handle: str) -> None:
        self.h = host
        self.handle = handle

    def info(self) -> dict:
        intake = self.h.intake
        head = intake.head()
        return {"repo": intake.repo, "bugs_address": None, "host_key": intake.log.key,
                "log_head": {"seq": head.seq, "hash": head.hash, "time": head.time},
                "max_message_bytes": intake.mailbox.capabilities().max_message_bytes, "transport": "fake"}

    def log(self, since: int, limit: int) -> dict:
        head = self.h.intake.head()
        return {"entries": [encode_entry(e).decode() for e in self.h.intake.log_entries(since, limit)],
                "head": {"seq": head.seq, "hash": head.hash}}

    def mail_batch(self, since: int, limit: int) -> dict:
        rows, nxt = self.h.intake.mail_since(since, limit)
        return {"mail": [{"seq": s, "id": g, "raw": base64.b64encode(self.h.intake.raw(g)).decode()} for s, g in rows],
                "next": nxt}

    def mail(self, gud_id: Hex) -> bytes | None:
        return self.h.intake.raw(gud_id) if self.h.intake.log.seq_of(gud_id) is not None else None

    def object(self, sha256: Hex) -> bytes | None:
        return self.h.intake.object(sha256)

    def snapshot(self, sha256: Hex) -> bytes | None:
        return self.h.intake.snapshot_bytes(sha256)

    def delivery(self, body: Mapping[str, Any]) -> dict:
        from gud.events.model import Delivery
        channel = "webhook" if body.get("webhook") else ("sse" if body.get("sse") else "email")
        self.h.host.registry.set(Delivery(self.handle, channel, body.get("webhook")))
        return {"ok": True}

    def pull(self) -> None:
        self.h.pull()


class Net:
    """One repo: kit members, the real host, and a client factory (one GUD_HOME per member)."""

    def __init__(self, tmp: Path, *, http: bool = True, max_message_bytes: int | None = None, **kit_kw: Any) -> None:
        self.tmp = Path(tmp)
        self.kit = Kit(**kit_kw)
        self.clock = Clock(T0)
        kw = {"max_message_bytes": max_message_bytes} if max_message_bytes else {}
        self.host = RealHost(self.tmp / "host", self.kit, self.clock, **kw)
        self.http = http
        self._app = None
        self._clients: dict[str, RepoClient] = {}

    def home(self, handle: str) -> Path:
        return self.tmp / "homes" / handle.lstrip("@")

    def client(self, handle: str, *, fresh: bool = False, home: Path | None = None) -> RepoClient:
        if handle in self._clients and not fresh and home is None:
            return self._clients[handle]
        mailbox = self.host.mailbox_for(self.kit.address(handle))
        kw: dict[str, Any] = {}
        if self.http:
            from fastapi.testclient import TestClient
            if self._app is None:
                self._app = self.host.app()
            url = "http://testserver"
            kw["http"] = TestClient(self._app, base_url=url)
        else:
            url = "inprocess:"
            kw["mirror"] = IntakeMirror(self.host, handle)
        c = RepoClient(url, handle, self.kit.key(handle), send_via=mailbox, repo_address=REPO,
                       home=home or self.home(handle), clock=self.clock, **kw)
        if home is None:
            self._clients[handle] = c
        return c

    def genesis(self, files: Mapping[str, Any], author: str = "@nelson") -> Hex:
        c = self.client(author)
        c.sync_log()
        content = {p: ("100644", v) for p, v in self.kit.gud_files().items()}
        for path, value in files.items():
            content[path] = value if isinstance(value, tuple) else ("100644", value if isinstance(value, bytes)
                                                                     else value.encode())
        gid = create_genesis(c, content, "genesis")
        self.settle()
        c.sync_log()
        assert c.chain().tip is not None and c.chain().tip.state == gid
        return gid

    def settle(self, ms: int = 1000) -> None:
        """Let the host log what was sent, and move the clock on."""
        self.clock.advance(ms)
        self.host.pull()

    def clone(self, handle: str, name: str | None = None, **kw: Any) -> WorkingCopy:
        return WorkingCopy.clone(self.client(handle), self.tmp / "wc" / (name or handle.lstrip("@")), **kw)


@pytest.fixture
def net(tmp_path: Path) -> Net:
    return Net(tmp_path)


@pytest.fixture
def local_net(tmp_path: Path) -> Net:
    return Net(tmp_path, http=False)
