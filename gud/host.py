"""The v1 host process (SPEC-v1 §11.2, §19, §21): HTTP mirror + intake + dispatch.

Endpoints (`create_app`):

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/api/v1/info` | open |
| GET | `/api/v1/log?since=&limit=&wait=` | member (long-poll up to 60 s with `wait`) |
| GET | `/api/v1/mail/<gud id>` | member; raw MIME as `message/rfc822` |
| GET | `/api/v1/mail?since=&limit=` | member; `{"mail": [{"seq", "id", "raw"}], "next"}` |
| GET | `/api/v1/object/<sha256>`, `/api/v1/snapshot/<sha256>` | member |
| GET | `/api/v1/events?member=&after=` (`Last-Event-ID`) | that member; SSE |
| POST | `/api/v1/delivery` | member; `{"webhook": url}` / `{"sse": true}` / `{"email": true}` |
| GET | `/api/v1/bugs` | member |
| POST | `/api/v1/webhook/<provider>` | the provider's own signature (`WebhookVerifier`) |
| POST | `/api/v1/admin/{tick,catch-up,pump,inject,handover}` | loopback only |
| GET | `/api/v1/view/*` (UI views, all-events SSE at `/api/v1/view/stream`) | member (`gud.host_view`) |
| POST | `/api/v1/view/act/{task,vote,comment,admit}` | loopback only; signed with a `--ui-home` key |
| GET | `/`, `/assets/*`, other non-API paths | open: the built web UI (`ui/dist`) |

"Member" is mirror auth v1 (§21): `X-Gud-Handle`, `X-Gud-Time`, `X-Gud-Sig` over
`"{METHOD} {path_with_query} {time} {sha256(body)}"`, verified against `.gud/members` at the tip,
±300 s. Requests from loopback are trusted (`loopback_hosts`); a loopback caller may
name the member with `X-Gud-Handle` (or `handle` in a delivery body). Admin/demo mutations are
loopback only. Provider webhooks have their own route table (`Intake.verifiers`, keyed by
transport name), so a new adapter's `WebhookVerifier` plugs in with `register_verifier`; put
them on a separate, tunnelled port with `create_webhook_app` when the main port must not be
reachable from outside.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from gud.core import envelope as E
from gud.core.config import Members
from gud.core.hostlog import encode_entry
from gud.core.objects import DiskStore, MemoryStore
from gud.core.replay import Replayer
from gud.core.types import Handle, ObjectStoreLike
from gud.events.dispatch import (
    AUTH_HANDLE, DeliveryRegistry, Dispatcher, Journal, format_time, request_bytes, sign_request, verify_request,
)
from gud.events.intake import Intake, IntakeError, WebhookRejected, received_now
from gud.events.model import Delivery
from gud.host_view import EventFeed, HostView, add_static_routes, add_view_routes, default_ui_dist, load_signers
from gud.transport import mime, registry as transports
from gud.transport.base import Mailbox, TransportError

__all__ = [
    "create_app", "create_webhook_app", "verify_request", "sign_request", "request_bytes", "format_time",
    "Host", "open_host", "run_host", "serve", "serve_dir", "LOOPBACK_HOSTS", "load_signers",
]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_LIMIT = 1000
MAX_WAIT_S = 60.0


def _path_with_query(request) -> str:
    query = request.scope.get("query_string", b"").decode("latin-1")
    return request.url.path + (f"?{query}" if query else "")


def create_app(intake: Intake, dispatcher: Dispatcher, registry: DeliveryRegistry, *, repo: str,
               bugs: Mailbox | None = None, loopback_hosts: frozenset[str] | set[str] | None = None,
               clock: Callable[[], float] | None = None, sse_keepalive_s: float = 15.0,
               feed: EventFeed | None = None, ui_signers: Mapping[Handle, bytes] | None = None,
               ui_dist: Path | None | bool = None) -> FastAPI:
    """The §21 mirror, delivery registration, SSE, provider webhooks and loopback admin, plus the
    UI view API (`gud.host_view`, `/api/v1/view/*`) and the built UI at `/`. `feed` is the
    host's all-events feed (an in-memory one, backfilled from the journals, when None);
    `ui_signers` are member keys the loopback UI actions may sign with; `ui_dist` is the built UI
    (default `gud.host_view.default_ui_dist()`, False for none)."""
    app = FastAPI(title="gud host v1")
    loopback = frozenset(LOOPBACK_HOSTS if loopback_hosts is None else loopback_hosts)
    now = clock or intake.clock
    app.state.intake, app.state.dispatcher, app.state.registry = intake, dispatcher, registry

    def is_loopback(request: Request) -> bool:
        return (request.client.host if request.client else "") in loopback

    async def caller(request: Request, body: bytes = b"") -> Handle | None:
        """The verified member handle; None for a trusted loopback caller naming nobody."""
        if is_loopback(request):
            handle = request.headers.get(AUTH_HANDLE)
            return handle if handle and E.is_handle(handle) else None
        members = intake.members()
        if members is None:
            # before genesis there is no member to sign as, and the genesis author needs the
            # log head for its Gud-Seen: the mirror is open until genesis links
            return None
        handle = verify_request(members, request.method, _path_with_query(request), body,
                                request.headers, now())
        if handle is None:
            raise HTTPException(401, "member signature required")
        return handle

    def loopback_only(request: Request) -> None:
        if not is_loopback(request):
            raise HTTPException(403, "admin endpoints are loopback-only")

    def int_param(request: Request, name: str, default: int, lo: int = 0, hi: int | None = None) -> int:
        raw = request.query_params.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise HTTPException(400, f"{name} must be an integer") from exc
        value = max(lo, value)
        return min(hi, value) if hi is not None else value

    def float_param(request: Request, name: str, default: float, hi: float) -> float:
        raw = request.query_params.get(name)
        if raw is None or raw == "":
            return default
        try:
            return max(0.0, min(hi, float(raw)))
        except ValueError as exc:
            raise HTTPException(400, f"{name} must be a number") from exc

    @app.get("/api/v1/info")
    def info():
        head = intake.head()
        caps = intake.mailbox.capabilities()
        return {"repo": repo, "bugs_address": getattr(bugs, "address", None), "host_key": intake.log.key,
                "log_head": {"seq": head.seq, "hash": head.hash, "time": head.time},
                "max_message_bytes": caps.max_message_bytes, "transport": caps.name}

    @app.get("/api/v1/log")
    async def get_log(request: Request):
        await caller(request)
        since = int_param(request, "since", 0)
        limit = int_param(request, "limit", 500, 1, MAX_LIMIT)
        wait = float_param(request, "wait", 0.0, MAX_WAIT_S)
        if wait > 0:
            await asyncio.to_thread(intake.wait_log, since, wait)
        entries = intake.log_entries(since, limit)
        head = intake.head()
        return {"entries": [encode_entry(e).decode("utf-8") for e in entries],
                "head": {"seq": head.seq, "hash": head.hash}}

    @app.get("/api/v1/mail")
    async def list_mail(request: Request):
        await caller(request)
        since = int_param(request, "since", 0)
        limit = int_param(request, "limit", 100, 1, MAX_LIMIT)
        wait = float_param(request, "wait", 0.0, MAX_WAIT_S)
        rows, nxt = intake.mail_since(since, limit)
        if not rows and wait > 0:
            await asyncio.to_thread(intake.wait_log, max(since, len(intake.log)), wait)
            rows, nxt = intake.mail_since(since, limit)
        out = []
        for seq, gid in rows:
            raw = intake.raw(gid)
            out.append({"seq": seq, "id": gid, "raw": base64.b64encode(raw).decode() if raw is not None else None})
        return {"mail": out, "next": nxt}

    @app.get("/api/v1/mail/{gud_id}")
    async def get_mail(gud_id: str, request: Request):
        await caller(request)
        raw = intake.raw(gud_id) if intake.log.seq_of(gud_id) is not None else None
        if raw is None:
            raise HTTPException(404, "no such mail")
        return Response(raw, media_type="message/rfc822")

    @app.get("/api/v1/object/{sha256}")
    async def get_object(sha256: str, request: Request):
        await caller(request)
        data = intake.object(sha256)
        if data is None:
            raise HTTPException(404, "no such object")
        return Response(data, media_type="application/octet-stream")

    @app.get("/api/v1/snapshot/{sha256}")
    async def get_snapshot(sha256: str, request: Request):
        await caller(request)
        data = intake.snapshot_bytes(sha256)
        if data is None:
            raise HTTPException(404, "no such snapshot")
        return Response(data, media_type="application/json")

    @app.get("/api/v1/events")
    async def events(request: Request):
        who = await caller(request)
        member = request.query_params.get("member") or who
        if not member or not E.is_handle(member):
            raise HTTPException(400, "member is required")
        if who is not None and who != member and not is_loopback(request):
            raise HTTPException(403, "a member reads only its own events")
        after = int_param(request, "after", 0)
        last = request.headers.get("Last-Event-ID")
        if last and last.strip().isdigit():
            after = max(after, int(last.strip()))
        follow = request.query_params.get("follow", "1") not in ("0", "false", "no")
        max_s = float_param(request, "max", 0.0, 86_400.0) or None
        gen = dispatcher.sse(member, after, follow=follow, keepalive_s=sse_keepalive_s, max_s=max_s)
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/v1/delivery")
    async def set_delivery(request: Request):
        body = await request.body()
        who = await caller(request, body)
        try:
            obj = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "body must be JSON") from exc
        if not isinstance(obj, dict):
            raise HTTPException(400, "body must be a JSON object")
        handle = who or obj.get("handle")
        if not handle or not E.is_handle(str(handle)):
            raise HTTPException(400, "which member? (sign the request)")
        members = intake.members()
        if members is None or members.get(handle) is None:
            raise HTTPException(404, f"{handle} is not a member")
        if obj.get("webhook"):
            delivery = Delivery(handle, "webhook", str(obj["webhook"]))
        elif obj.get("sse"):
            delivery = Delivery(handle, "sse")
        elif obj.get("email"):
            delivery = Delivery(handle, "email")
        else:
            raise HTTPException(400, 'body is {"webhook": url}, {"sse": true} or {"email": true}')
        try:
            registry.set(delivery)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        dispatcher.wake()
        return {"handle": delivery.handle, "channel": delivery.channel, "url": delivery.url,
                "cursor": dispatcher.acked(handle), "journal_head": dispatcher.journal.head(handle)}

    @app.get("/api/v1/bugs")
    async def get_bugs(request: Request):
        await caller(request)
        if bugs is None:
            return {"bugs": []}
        try:
            items, _ = await asyncio.to_thread(bugs.list, since=None, limit=MAX_LIMIT)
        except TransportError as exc:
            raise HTTPException(502, str(exc)) from exc
        out = []
        for item in items:
            parsed = mime.parse(item.raw_mime)
            out.append({"id": E.unsigned_id(item.raw_mime), "from": parsed.from_addr, "subject": parsed.subject,
                        "text": parsed.text, "received_at": item.received_at})
        return {"bugs": out}

    _webhook_routes(app, intake)

    @app.post("/api/v1/admin/tick")
    def admin_tick(request: Request):
        loopback_only(request)
        delta = intake.tick(force=True)
        return {"ticked": delta is not None, "head": intake.head().seq}

    @app.post("/api/v1/admin/catch-up")
    def admin_catch_up(request: Request):
        loopback_only(request)
        return {"logged": intake.catch_up()}

    @app.post("/api/v1/admin/pump")
    def admin_pump(request: Request):
        loopback_only(request)
        return {"delivered": dispatcher.pump()}

    @app.post("/api/v1/admin/handover")
    async def admin_handover(request: Request):
        loopback_only(request)
        try:
            obj = json.loads(await request.body() or b"{}")
            entry = intake.handover(str(obj["key"]), repo=obj.get("repo") or None)
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(400, f"handover: {exc}") from exc
        return {"seq": entry.seq, "hash": entry.hash, "next": entry.field("next"), "repo": entry.field("repo")}

    @app.post("/api/v1/admin/inject")
    async def admin_inject(request: Request):
        loopback_only(request)
        raw = await request.body()
        delta = await asyncio.to_thread(intake.on_received, received_now(raw, intake.clock))
        return {"logged": delta is not None, "events": [e.type for e in delta.events] if delta else []}

    @app.exception_handler(IntakeError)
    async def intake_error(_request, exc):  # pragma: no cover - defensive
        return JSONResponse({"detail": str(exc)}, status_code=500)

    if feed is None:
        feed = EventFeed(None, dispatcher.journal)
        dispatcher.observers.append(feed.add)
    view = HostView(intake, repo=repo, dispatcher=dispatcher, registry=registry, feed=feed, bugs=bugs,
                    signers=ui_signers, clock=now)
    app.state.view = view
    add_view_routes(app, view, caller=caller, is_loopback=is_loopback, sse_keepalive_s=sse_keepalive_s)
    add_static_routes(app, None if ui_dist is False else (ui_dist if ui_dist is not None else default_ui_dist()))
    return app


def _webhook_routes(app, intake: Intake) -> None:
    @app.post("/api/v1/webhook/{provider}")
    async def provider_webhook(provider: str, request: Request):
        body = await request.body()
        headers = {k: v for k, v in request.headers.items()}
        try:
            accepted = await asyncio.to_thread(intake.on_webhook, provider, headers, body)
        except KeyError as exc:
            raise HTTPException(404, f"no webhook for provider {provider!r}") from exc
        except WebhookRejected as exc:
            raise HTTPException(401, str(exc)) from exc
        except TransportError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"accepted": accepted}


def create_webhook_app(intake: Intake) -> FastAPI:
    """Only `POST /api/v1/webhook/<provider>`, for a port that is tunnelled to the internet."""
    app = FastAPI(title="gud host v1 webhooks")
    _webhook_routes(app, intake)
    return app


# ---------------------------------------------------------------- assembling a host

@dataclass
class Host:
    """One host: intake, dispatcher, registry, replayer and the app, sharing one home."""
    home: Path
    repo: str
    intake: Intake
    dispatcher: Dispatcher
    registry: DeliveryRegistry
    replayer: Replayer
    mailbox: Mailbox
    bugs: Mailbox | None = None
    feed: EventFeed | None = None
    ui_signers: Mapping[Handle, bytes] | None = None

    def app(self, **kw) -> FastAPI:
        kw.setdefault("feed", self.feed)
        kw.setdefault("ui_signers", self.ui_signers)
        return create_app(self.intake, self.dispatcher, self.registry, repo=self.repo, bugs=self.bugs, **kw)

    def save(self) -> None:
        self.intake.save()


def open_host(home: Path, *, repo: str, mailbox: Mailbox, host_private: bytes, bugs: Mailbox | None = None,
              clock: Callable[[], float] = time.time, http: httpx.Client | None = None,
              store: ObjectStoreLike | None = None, persist_replay: bool = True,
              dispatcher_kw: Mapping | None = None, intake_kw: Mapping | None = None,
              base: Path | None = None, ui_signers: Mapping[Handle, bytes] | None = None) -> Host:
    """Build (or resume) a host under `home/host/` (or `base`): log.jsonl, mail/, journal/,
    delivery.json, intake.json, replay/ (the replay cache) and `home/objects` (the object store)."""
    home = Path(home)
    base = Path(base) if base is not None else home / "host"
    base.mkdir(parents=True, exist_ok=True)
    if store is None:
        store = DiskStore(home / "objects") if persist_replay else MemoryStore()
    state_dir = base / "replay" if persist_replay else None
    replayer = None
    if state_dir is not None and (state_dir / "state.json").exists():
        try:
            replayer = Replayer.load(state_dir, store)
        except Exception:
            replayer = None
    if replayer is None:
        replayer = Replayer(store, state_dir=state_dir)
    journal = Journal(base / "journal")
    reg = DeliveryRegistry(base / "delivery.json")
    holder: dict = {}

    def members() -> Members | None:
        intake = holder.get("intake")
        return intake.members() if intake is not None else None

    dispatcher = Dispatcher(journal, reg, repo=repo, host_private=host_private, send=mailbox.send, members=members,
                            http=http, clock=clock, **dict(dispatcher_kw or {}))
    feed = EventFeed(base / "ui-events.jsonl", journal)
    dispatcher.observers.append(feed.add)
    kw = dict(repo=repo, mailbox=mailbox, log_path=base / "log.jsonl", host_private=host_private,
              mail_store=base / "mail", dispatcher=dispatcher, clock=clock, **dict(intake_kw or {}))
    try:
        intake = Intake(replayer=replayer, **kw)
    except IntakeError:
        if not len(replayer.log()):
            raise
        intake = Intake(replayer=Replayer(store, state_dir=state_dir), **kw)   # stale cache: rebuild
    holder["intake"] = intake
    return Host(home, repo, intake, dispatcher, reg, intake.replayer, mailbox, bugs, feed=feed, ui_signers=ui_signers)


def run_host(home: Path, *, repo: str, mailbox: Mailbox, host_private: bytes, bind: str = "127.0.0.1:8787",
             webhook_bind: str | None = None, bugs: Mailbox | None = None, stop: threading.Event | None = None,
             log_level: str = "warning", base: Path | None = None, store: ObjectStoreLike | None = None,
             on_ready: Callable[[Host], None] | None = None, ui_signers: Mapping[Handle, bytes] | None = None,
             ui_dist: Path | None | bool = None) -> None:
    """Run a host until `stop` (or Ctrl-C): intake loop + dispatcher thread + the HTTP app (and,
    with `webhook_bind`, a second server with only the provider webhook route)."""
    import uvicorn

    stop = stop or threading.Event()
    host = open_host(home, repo=repo, mailbox=mailbox, host_private=host_private, bugs=bugs, base=base, store=store,
                     ui_signers=ui_signers)
    if on_ready is not None:
        on_ready(host)
    worker = threading.Thread(target=host.intake.run, args=(stop,), name="gud-intake", daemon=True)
    worker.start()
    servers = []

    def server(app, where: str) -> "uvicorn.Server":
        h, _, p = where.rpartition(":")
        return uvicorn.Server(uvicorn.Config(app, host=h or "127.0.0.1", port=int(p), log_level=log_level))

    servers.append(server(host.app(ui_dist=ui_dist), bind))
    if webhook_bind:
        servers.append(server(create_webhook_app(host.intake), webhook_bind))
    threads = [threading.Thread(target=s.run, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        while not stop.is_set() and all(t.is_alive() for t in threads):
            stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for s in servers:
            s.should_exit = True
        for t in threads:
            t.join(timeout=10)
        worker.join(timeout=10)


def _host_config(home: Path, transport: str) -> dict[str, str]:
    """`home/host/transport.toml` (top-level keys plus a table named after the transport), else
    `GUD_<TRANSPORT>_*` from the environment."""
    import os
    import tomllib

    path = Path(home) / "host" / "transport.toml"
    if path.exists():
        data = tomllib.loads(path.read_text("utf-8"))
        cfg = {k: str(v) for k, v in data.items() if not isinstance(v, dict) and k != "transport"}
        table = data.get(transport)
        if isinstance(table, dict):
            cfg.update({k: str(v) for k, v in table.items() if not isinstance(v, dict)})
        return cfg
    prefix = f"GUD_{transport.upper()}_"
    return {k[len(prefix):].lower(): v for k, v in os.environ.items() if k.startswith(prefix)}


def serve(home: Path, *, transport: str, bind: str = "127.0.0.1:8787") -> None:
    """`gud host serve`: the host key from `home/host/host.key` (64 hex), the repo address from
    the transport config's `address`, the mailbox from the transport registry."""
    home = Path(home)
    key_path = home / "host" / "host.key"
    try:
        private = bytes.fromhex(key_path.read_text("utf-8").strip())
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"no host key at {key_path} (run `gud host init`)") from exc
    cfg = _host_config(home, transport)
    mailbox = transports.open_mailbox(transport, cfg)
    repo = cfg.get("repo") or mailbox.address
    run_host(home, repo=repo, mailbox=mailbox, host_private=private, bind=bind,
             webhook_bind=cfg.get("webhook_bind") or None)


def _decode_key(text: str) -> bytes:
    """A host key file: `ed25519:<base64 seed>` (`gud host init`) or 64 hex."""
    value = text.strip()
    if value.startswith("ed25519:"):
        raw = base64.b64decode(value[len("ed25519:"):], validate=True)
    else:
        raw = bytes.fromhex(value)
    if len(raw) != 32:
        raise ValueError("not a 32-byte Ed25519 key")
    return raw


def serve_dir(root: Path, *, bind: str = "127.0.0.1:8787", webhook_bind: str | None = None,
              stop: threading.Event | None = None, log_level: str = "warning",
              on_ready: Callable[[Host], None] | None = None, ui_signers: Mapping[Handle, bytes] | None = None,
              ui_dist: Path | None | bool = None) -> None:
    """`gud host serve`: serve a host directory made by `gud host init` (or `gud init`'s local
    host): `key`, `host.json` (repo, objects), `transport.toml` (the repo mailbox), and the log and
    mail it already holds (`log.jsonl`, `mail/`), so a repo started in local host mode continues
    under the same log. While serving, the directory's `serve.lock` is held and local-mode
    members only read (`gud.wc.remote.LocalHost.served`)."""
    import fcntl

    root = Path(root)
    meta = json.loads((root / "host.json").read_text("utf-8"))
    private = _decode_key((root / "key").read_text("ascii"))
    name, cfg = transports.read_transport_toml(root / "transport.toml")     # `<key>_env` from the environment
    mailbox = transports.open_mailbox(name, cfg)
    objects = Path(meta.get("objects") or (root / "objects"))
    lock = open(root / "serve.lock", "a+b")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(f"{root} is already being served") from exc
        run_host(root, repo=meta["repo"], mailbox=mailbox, host_private=private, bind=bind,
                 webhook_bind=webhook_bind or cfg.get("webhook_bind") or None, stop=stop, log_level=log_level,
                 base=root, store=DiskStore(objects), on_ready=on_ready, ui_signers=ui_signers, ui_dist=ui_dist)
    finally:
        lock.close()

