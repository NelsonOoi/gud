"""HTTP API for the UI and demo driver. Shapes are in api_contract.md.

Run: .venv/bin/python -m gud.api   (serves http://localhost:8787, and ui/dist if built)
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import httpx

from . import compose, config, sig
from .bugs import bugs_address_for, derive_bugs
from .chain import Repo
from .config import domain_address, write_swarm_file
from .console import Console, ConsoleError, activity, all_members, email_kind, handle_of
from .hostauth import verify_request
from .joins import join_requests
from .keys import load_identity
from .mail_primitive import PrimitiveError, PrimitiveMailbox
from .patch import diffstat
from .open import (
    boot_swarm, catalog_addresses, human_members, prepare_agent, repo_address, send_genesis,
    start_swarm, stop_swarm, swarm_is_on,
)
from .repos import (
    check_token, header_rows, read_active, read_token, write_active, write_token,
)
from .types import Email, LinkState, Member, ProposalState
from .wire import parse_body

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})


class Ctx:
    def __init__(self, cfg: config.Config, mailbox=None, clock=time.time, *,
                 host_url: str | None = None, loopback_hosts: set[str] | None = None,
                 auto_welcome: bool = True, welcome_interval: float = 1.0,
                 start_welcome_thread: bool = True):
        self.cfg = cfg
        self.clock = clock
        self.loopback_hosts = set(LOOPBACK_HOSTS if loopback_hosts is None else loopback_hosts)
        self.host_url = host_url if host_url is not None else (os.environ.get("GUD_PUBLIC_URL") or "http://127.0.0.1:8787")
        self.auto_welcome = auto_welcome
        self.welcome_interval = welcome_interval
        self.start_welcome_thread = start_welcome_thread
        self._welcome_lock = threading.Lock()
        self._welcomed: set[str] = set()
        self._welcome_thread: threading.Thread | None = None
        self.token_client = None
        self.view_epoch = 0
        self._view_lock = threading.Lock()
        self.needs_token = False
        self._repos_lock = threading.Lock()
        self._repos_cache: tuple[float, list[str]] | None = None
        self._repos_event: threading.Event | None = None
        self.swarm = None
        self.swarm_enabled = False
        self._swarm_lock = threading.Lock()
        self._bind_mailbox(mailbox)

    def _bind_mailbox(self, mailbox) -> None:
        """Open the GUI mailbox. A passed-in mailbox is kept as-is (tests)."""
        from .mailbox import open_mailbox

        self.mailbox = mailbox
        if self.mailbox is None and (self.cfg.mailbox or "fake").strip().lower() == "fake":
            self.mailbox = open_mailbox(self.cfg)
        elif self.mailbox is None:
            key = self.cfg.primitive_api_key or read_token(self.cfg.home)
            if not key:
                self.needs_token = True
            else:
                self.mailbox = PrimitiveMailbox(
                    key, read_active(self.cfg.home) or self.cfg.repo_address, list_ttl=1.0,
                )
        if self.mailbox is not None and mailbox is None:
            saved = read_active(self.cfg.home)
            if saved:
                self.mailbox.repo_address = saved
        if self.mailbox is None:
            self.repo = None
            self.console = None
            return
        self.needs_token = False
        self.repo = Repo(self.mailbox, self.cfg.window_seconds, clock=self.clock)
        self.console = Console(self.cfg, mailbox=self.mailbox, clock=self.clock)

    def identity(self, handle: str) -> compose.Identity:
        try:
            return load_identity(self.cfg, handle, address=domain_address(self.cfg.domain, handle))
        except (FileNotFoundError, OSError):
            raise HTTPException(400, f"no local key for {handle}") from None


def link_json(l: LinkState) -> dict:
    return {"id": l.message_id, "seq": l.seq, "author": l.author, "subject": l.subject,
            "intent": l.intent, "state": l.state, "tree_hash": l.tree_hash,
            "received_at": l.received_at, "linked_at": l.linked_at, "task": l.task,
            "diffstat": diffstat(l.diff) if l.diff else {"files": [], "added": 0, "removed": 0}}


def proposal_json(p: ProposalState) -> dict:
    return {k: getattr(p, k) for k in (
        "message_id", "parent", "author", "subject", "received_at", "status", "reason",
        "affected_owners", "approvals", "objections", "window_closes_at", "protected_rule",
        "quorum_needed", "quorum_have", "test_result", "task")} | {"id": p.message_id}


def member_json(m: Member, tip: LinkState | None, added_by: dict[str, str]) -> dict:
    owned = 0
    if tip:
        owned = sum(1 for lines in tip.blame.values() for owner, _ in lines if owner == m.handle)
    return {"handle": m.handle, "role": m.role, "address": m.address,
            "fingerprint": sig.fingerprint(m.public_key) if m.public_key else None,
            "sponsor": m.sponsor, "votes": m.votes, "lines_owned": owned,
            "added_by_link": added_by.get(m.handle)}


def bugs_address(ctx) -> str:
    raw = (getattr(ctx.cfg, "bugs_address", "") or "").strip()
    return raw.lower() if raw else bugs_address_for(ctx.mailbox.repo_address)


def bug_rows(ctx) -> list[Email]:
    try:
        return ctx.mailbox.list(recipient=bugs_address(ctx))
    except Exception:
        return []


def email_json(chain, e: Email) -> dict:
    return {"id": e.message_id, "from": handle_of(chain, e.from_addr), "to": list(e.to), "cc": list(e.cc),
            "subject": e.subject, "body": e.body, "received_at": e.received_at,
            "in_reply_to": e.in_reply_to, "sender_verified": e.sender_verified, "kind": parse_body(e.body).kind}


def raw_email(e: Email) -> dict:
    return {
        "message_id": e.message_id, "from_addr": e.from_addr, "to": list(e.to), "cc": list(e.cc),
        "subject": e.subject, "body": e.body, "received_at": e.received_at,
        "sender_verified": e.sender_verified, "in_reply_to": e.in_reply_to,
        "references": list(e.references), "provider_id": e.provider_id,
    }


class TaskIn(BaseModel):
    title: str
    description: str = ""
    requester: str


class RepoIn(BaseModel):
    address: str


class MailboxIn(BaseModel):
    token: str


class OpenRepoIn(BaseModel):
    name: str
    humans: list[str]


class SwarmIn(BaseModel):
    enabled: bool


class AgentIn(BaseModel):
    handle: str
    role: str | None = None


def select_repo(ctx: Ctx, address: str, *, persist: bool) -> None:
    """Point the GUI mailbox at ``address``. Persisting writes ``GUD_HOME/repo.txt``."""
    address = address.strip().lower()
    with ctx._view_lock:
        current = (ctx.mailbox.repo_address or "").lower()
        ctx.mailbox.repo_address = address
        if current != address:
            ctx.view_epoch += 1
            ctx._welcomed.clear()
            if ctx.console is not None:
                ctx.console.invalidate()
        if persist:
            write_active(ctx.cfg.home, address)


_REPOS_TTL = 60.0


def mailbox_repos(ctx: Ctx, *, refresh: bool = False) -> list[str]:
    """Genesis addresses. A fresh list is reused for a minute, and one scan serves every caller."""
    if ctx.mailbox is None:
        return []
    now = time.monotonic()
    with ctx._repos_lock:
        cached = ctx._repos_cache
        if cached is not None and not refresh and now - cached[0] < _REPOS_TTL:
            return list(cached[1])
        event = ctx._repos_event
        if event is not None and not refresh:
            leader = False
        else:
            event = threading.Event()
            ctx._repos_event = event
            leader = True
    if not leader:
        event.wait()
        with ctx._repos_lock:
            cached = ctx._repos_cache
        return list(cached[1]) if cached is not None else []
    try:
        repos = catalog_addresses(header_rows(ctx.mailbox))
        with ctx._repos_lock:
            ctx._repos_cache = (time.monotonic(), repos)
        return repos
    finally:
        with ctx._repos_lock:
            ctx._repos_event = None
        event.set()


def repo_catalog(ctx: Ctx) -> dict:
    """Genesis addresses in the signed-in mailbox. Does not download bodies."""
    if ctx.mailbox is None:
        return {"active": None, "repos": [], "needs_token": True}
    repos = mailbox_repos(ctx)
    saved = read_active(ctx.cfg.home)
    # The inbound scan lags a send. Keep the repo we just opened selected.
    if saved and "@" in saved and saved not in repos:
        repos = [saved, *repos]
    if saved in repos:
        active = saved
    elif repos:
        active = repos[0]
    else:
        active = None
    if active and (ctx.mailbox.repo_address or "").lower() != active:
        select_repo(ctx, active, persist=False)
    return {"active": active, "repos": repos, "needs_token": False}


class MemberIn(BaseModel):
    action: str
    handle: str
    role: str | None = None
    address: str | None = None
    public_key: str | None = None
    sponsor: str | None = None


def _signed_path(request) -> str:
    path = request.scope.get("path") or request.url.path
    raw = request.scope.get("query_string") or b""
    query = raw.decode() if isinstance(raw, bytes) else str(raw)
    return f"{path}?{query}" if query else path


def _approve_as_human(ctx: Ctx, push_id: str, as_handle: str | None = None) -> None:
    """Mail ``approve`` from the human member. Replay counts it only when the sender verifies."""
    if ctx.console is None:
        raise HTTPException(409, "mailbox sign-in required")
    try:
        if as_handle:
            ctx.console.decide(push_id, "approve", "", as_handle)
        else:
            ctx.console.decide(push_id, "approve")
    except ConsoleError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _human_approver(ctx: Ctx, members: dict) -> str | None:
    """The configured human when they are on the roster, otherwise the first human."""
    humans = [member.handle for member in members.values() if member.role == "human"]
    if ctx.console is not None and ctx.console.human_handle() in humans:
        return None
    return humans[0] if humans else None


def _tip_members(ctx: Ctx) -> dict:
    if ctx.repo is None:
        return {}
    chain = ctx.repo.refresh()
    tip = chain.tip
    return dict(tip.members) if tip is not None else {}


def _welcome_once(ctx: Ctx, chain=None) -> list:
    if not ctx.auto_welcome or ctx.mailbox is None or ctx.repo is None:
        return []
    with ctx._welcome_lock:
        if not ctx.cfg.key_path("@console").exists():
            return []
        identity = ctx.identity("@console")
        if chain is None:
            chain = ctx.repo.refresh()
        from .joins import welcome_new_members
        return welcome_new_members(chain, ctx.mailbox, identity, ctx.host_url, ctx._welcomed)


def _start_welcome(ctx: Ctx) -> None:
    if ctx._welcome_thread is not None or not ctx.auto_welcome or not ctx.start_welcome_thread:
        return

    def loop() -> None:
        while True:
            try:
                _welcome_once(ctx)
            except Exception:
                pass
            time.sleep(max(0.2, float(ctx.welcome_interval)))

    thread = threading.Thread(target=loop, name="gud-welcome", daemon=True)
    ctx._welcome_thread = thread
    thread.start()


def _roster_has(tip: LinkState, join: dict) -> bool:
    handle = join.get("handle") or ""
    if handle and handle in tip.members:
        return True
    address = (join.get("address") or "").lower()
    if not address:
        return False
    return any(member.address.lower() == address for member in tip.members.values())


def create_app(ctx: Ctx | None = None) -> FastAPI:
    ctx = ctx or Ctx(config.load())
    app = FastAPI(title="gud")
    app.state.ctx = ctx

    @app.middleware("http")
    async def require_member(request, call_next):
        host = request.client.host if request.client else ""
        if host in ctx.loopback_hosts:
            return await call_next(request)
        if request.method == "GET" and request.url.path == "/api/info":
            return await call_next(request)
        if request.method != "GET":
            return JSONResponse({"detail": "mutating endpoints are loopback-only"}, status_code=403)
        members = _tip_members(ctx)
        handle = verify_request(members, request.headers, request.method, _signed_path(request), ctx.clock())
        if handle is None:
            return JSONResponse({"detail": "member signature required"}, status_code=401)
        return await call_next(request)

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def chain():
        if ctx.mailbox is None or ctx.repo is None:
            raise HTTPException(409, "mailbox sign-in required")
        current = ctx.repo.refresh()
        if ctx.auto_welcome:
            _welcome_once(ctx, current)
        return current

    def link_or_tip(c, at: str | None) -> LinkState:
        l = c.by_id.get(at) if at else c.tip
        if l is None:
            raise HTTPException(404, "no such link")
        return l

    @app.get("/api/repos")
    def get_repos():
        return repo_catalog(ctx)

    @app.post("/api/repo")
    def post_repo(body: RepoIn):
        if ctx.mailbox is None:
            raise HTTPException(409, "mailbox sign-in required")
        address = body.address.strip().lower()
        if "@" not in address:
            raise HTTPException(400, "address must include @")
        repos = mailbox_repos(ctx)
        if address not in repos:
            repos = mailbox_repos(ctx, refresh=True)
        if address not in repos:
            raise HTTPException(404, "no such repo")
        select_repo(ctx, address, persist=True)
        return {"active": address, "repos": repos, "needs_token": False}

    @app.post("/api/repos")
    def post_repos(body: OpenRepoIn):
        """Genesis only. Does not start the swarm."""
        if ctx.mailbox is None:
            raise HTTPException(409, "mailbox sign-in required")
        if not hasattr(ctx.mailbox, "repo_address"):
            raise HTTPException(409, "mailbox has no repo address")
        try:
            address = repo_address(ctx.cfg, body.name)
            humans = human_members(body.humans)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if address in catalog_addresses(header_rows(ctx.mailbox)):
            raise HTTPException(409, f"{address} already has a genesis")
        previous = ctx.mailbox.repo_address
        select_repo(ctx, address, persist=False)
        try:
            send_genesis(ctx.cfg, ctx.mailbox, humans)
        except PrimitiveError as exc:
            select_repo(ctx, previous, persist=False)
            raise HTTPException(502, exc.message) from exc
        write_active(ctx.cfg.home, address)
        with ctx._repos_lock:
            ctx._repos_cache = None
        return repo_catalog(ctx)

    @app.get("/api/swarm")
    def get_swarm():
        return {"enabled": swarm_is_on(ctx)}

    @app.post("/api/swarm")
    def post_swarm(body: SwarmIn):
        if not body.enabled:
            stop_swarm(ctx)
            write_swarm_file(ctx.cfg.home, False)
            return {"enabled": False}
        if ctx.mailbox is None:
            raise HTTPException(409, "mailbox sign-in required")
        try:
            start_swarm(ctx)
        except SystemExit as exc:
            message = str(exc).strip() or "repo has no genesis"
            raise HTTPException(409, message) from None
        write_swarm_file(ctx.cfg.home, True)
        return {"enabled": True}

    @app.post("/api/agents")
    def post_agent(body: AgentIn):
        from .members import propose_add

        current = chain()
        if current.tip is None:
            raise HTTPException(409, "no genesis yet")
        try:
            prepared = prepare_agent(ctx.cfg, body.handle, body.role, current.tip.members)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if prepared.addition is not None:
            console = ctx.identity("@console")
            try:
                email = propose_add(ctx.mailbox, console, current.tip, prepared.addition)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            _approve_as_human(ctx, email.message_id, _human_approver(ctx, current.tip.members))
        return {
            "handle": prepared.handle,
            "address": prepared.address,
            "public_key": prepared.public_key,
            "private_key": prepared.private_key,
        }

    @app.post("/api/mailbox")
    def post_mailbox(body: MailboxIn):
        token = body.token.strip()
        if not token:
            raise HTTPException(400, "token is required")
        try:
            check_token(token, client=ctx.token_client)
        except (PrimitiveError, httpx.HTTPError) as exc:
            raise HTTPException(401, "that credential was rejected") from exc
        write_token(ctx.cfg.home, token)
        saved = read_active(ctx.cfg.home) or ctx.cfg.repo_address
        ctx.mailbox = PrimitiveMailbox(token, saved, client=ctx.token_client, list_ttl=1.0)
        ctx.needs_token = False
        ctx.repo = Repo(ctx.mailbox, ctx.cfg.window_seconds, clock=ctx.clock)
        ctx.console = Console(ctx.cfg, mailbox=ctx.mailbox, clock=ctx.clock)
        ctx.view_epoch += 1
        ctx._welcomed.clear()
        ctx._repos_cache = None
        return repo_catalog(ctx)

    @app.get("/api/info")
    def info():
        if ctx.mailbox is None or ctx.repo is None:
            raise HTTPException(409, "mailbox sign-in required")
        current = ctx.repo.refresh()
        tip = current.tip
        return {
            "repo": ctx.mailbox.repo_address,
            "window_seconds": ctx.cfg.window_seconds,
            "tip_seq": tip.seq if tip else None,
        }

    @app.get("/api/state")
    def state():
        c = chain()
        tip = c.tip
        added_by: dict[str, str] = {}
        seen: set[str] = set()
        for l in c.links:
            for h in l.members:
                if h not in seen:
                    seen.add(h)
                    added_by[h] = l.message_id
        current = tip.members if tip else {}
        removed_by: dict[str, LinkState] = {}
        for prev, l in zip(c.links, c.links[1:]):
            for h in prev.members:
                if h not in l.members:
                    removed_by[h] = l
        removed = []
        for h, m in all_members(c).items():
            if h not in current:
                link = removed_by.get(h)
                removed.append(member_json(m, tip, added_by) | {
                    "removed_by_link": link.message_id if link else None,
                    "removed_at": link.linked_at if link else None})
        local = sorted(f"@{p.stem}" for p in ctx.cfg.keys_dir.glob("*.key")) if ctx.cfg.keys_dir.exists() else []
        return {"repo": ctx.mailbox.repo_address, "now": ctx.clock(), "window_seconds": ctx.cfg.window_seconds,
                "bugs_address": bugs_address(ctx),
                "tip": link_json(tip) if tip else None,
                "links": [link_json(l) for l in reversed(c.links)],
                "proposals": sorted((proposal_json(p) for p in c.proposals.values()),
                                    key=lambda p: p["received_at"], reverse=True),
                "members": [member_json(m, tip, added_by) for m in current.values()],
                "removed_members": removed,
                "local_signers": [h for h in local if h in current]}

    @app.get("/api/mail")
    def get_mail(since: float | None = None, limit: int = 500, wait: float | None = None):
        """``wait`` long-polls the local cache, capped at 30 seconds. It does not call Primitive."""
        chain()
        if limit < 0:
            limit = 0
        deadline = None
        if wait is not None and wait > 0:
            deadline = time.monotonic() + min(30.0, float(wait))

        def page_of():
            emails = ctx.mailbox.list(recipient=ctx.mailbox.repo_address, since=since)
            if since is not None:
                emails = [email for email in emails if email.received_at > since]
            emails.sort(key=lambda email: (email.received_at, email.message_id))
            page = emails[:limit] if limit else []
            next_since = page[-1].received_at if limit and len(page) == limit else None
            return {"emails": [raw_email(email) for email in page], "next_since": next_since}

        while True:
            body = page_of()
            if body["emails"] or deadline is None or time.monotonic() >= deadline:
                return body
            time.sleep(0.2)

    @app.get("/api/joins")
    def get_joins():
        return join_requests(chain())

    @app.post("/api/joins/{id:path}/propose")
    def propose_join(id: str):
        from .members import propose_add
        current = chain()
        if current.tip is None:
            raise HTTPException(409, "no genesis yet")
        found = next((row for row in join_requests(current) if row["id"] == id), None)
        if found is None:
            raise HTTPException(404, "no such join request")
        if not found["valid"]:
            raise HTTPException(409, "join request is not valid")
        if found["status"] == "proposed":
            raise HTTPException(409, "join request is already proposed")
        if found["status"] == "member" or _roster_has(current.tip, found):
            raise HTTPException(409, "handle or address is already a member")
        console = ctx.identity("@console")
        member = Member(
            found["handle"], found["role"], found["address"], found["public_key"] or None, found["sponsor"],
        )
        try:
            email = propose_add(
                ctx.mailbox, console, current.tip, member, reason=found["reason"], join_id=found["id"],
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"push_id": email.message_id}

    @app.post("/api/joins/{id:path}/approve")
    def approve_join(id: str):
        """Propose the join if it is still pending, then record the human's approve."""
        from .members import propose_add
        current = chain()
        if current.tip is None:
            raise HTTPException(409, "no genesis yet")
        found = next((row for row in join_requests(current) if row["id"] == id), None)
        if found is None:
            raise HTTPException(404, "no such join request")
        if not found["valid"]:
            raise HTTPException(409, "join request is not valid")
        if found["status"] == "member" or _roster_has(current.tip, found):
            raise HTTPException(409, "handle or address is already a member")
        push_id = found.get("push_id") if found["status"] == "proposed" else None
        if not push_id:
            console = ctx.identity("@console")
            member = Member(
                found["handle"], found["role"], found["address"], found["public_key"] or None, found["sponsor"],
            )
            try:
                email = propose_add(
                    ctx.mailbox, console, current.tip, member, reason=found["reason"], join_id=found["id"],
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            push_id = email.message_id
        _approve_as_human(ctx, push_id)
        return {"push_id": push_id}

    @app.get("/api/links/{id:path}/diff")
    def link_diff(id: str):
        c = chain()
        l = c.by_id.get(id)
        if l:
            return {"id": id, "diff": l.diff}
        e = next((e for e in c.emails if e.message_id == id), None)
        if e is None:
            raise HTTPException(404, "unknown message")
        return {"id": id, "diff": parse_body(e.body).diff}

    @app.get("/api/tree")
    def tree(at: str | None = None):
        c = chain()
        l = link_or_tip(c, at)
        files = []
        for path in sorted(l.tree):
            owners: dict[str, int] = {}
            for owner, _ in l.blame.get(path, []):
                owners[owner] = owners.get(owner, 0) + 1
            files.append({"path": path, "lines": len(l.tree[path].splitlines()), "owners": owners})
        return {"at": l.message_id, "files": files}

    @app.get("/api/blame")
    def blame(path: str, at: str | None = None):
        c = chain()
        l = link_or_tip(c, at)
        if path not in l.tree:
            raise HTTPException(404, "no such file")
        current = c.tip.members if c.tip else {}
        lines = [{"n": i + 1, "text": text, "owner": owner, "link_id": link_id, "orphaned": owner not in current}
                 for i, (text, (owner, link_id)) in enumerate(zip(l.tree[path].splitlines(), l.blame[path]))]
        return {"path": path, "at": l.message_id, "lines": lines}

    @app.get("/api/tasks")
    def get_tasks():
        return ctx.console.task_rows(chain())

    @app.get("/api/bugs")
    def get_bugs():
        return ctx.console.bug_rows_json(chain())

    @app.post("/api/bugs/{id:path}/review")
    def review_bug(id: str, body: dict):
        """A human review. The lead opens the ticket after an approval. Works on Primitive too."""
        chain()
        action = (body.get("action") or "").strip().lower()
        if action not in {"approve", "reject"}:
            raise HTTPException(400, "action must be approve or reject")
        who = body.get("from") or "@nelson"
        try:
            return {"id": ctx.console.decide(id, action, body.get("reason") or "", who)}
        except ConsoleError as exc:
            message = "unknown bug report" if exc.status == 404 else str(exc)
            raise HTTPException(exc.status, message) from exc

    @app.get("/api/activity")
    def get_activity(limit: int = 100):
        return activity(chain(), limit)

    @app.get("/api/thread/{id:path}")
    def thread(id: str):
        c = chain()
        return [email_json(c, e) for e in ctx.repo.thread(id)]

    @app.post("/api/tasks")
    def post_task(body: TaskIn):
        chain()
        try:
            return {"id": ctx.console.create_task(body.title, body.description, body.requester)}
        except ConsoleError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/api/members")
    def post_member(body: MemberIn):
        from . import members as memberlib
        c = chain()
        if c.tip is None:
            raise HTTPException(409, "no genesis yet")
        console = ctx.identity("@console")
        if body.action == "add":
            if not (body.role and body.address):
                raise HTTPException(400, "role and address are required")
            public_key = body.public_key
            if not public_key and body.role != "human":
                from .keys import create_identity
                _, public_key = create_identity(ctx.cfg, body.handle)
            m = Member(body.handle, body.role, body.address.lower(), public_key, body.sponsor)
            email = memberlib.propose_add(ctx.mailbox, console, c.tip, m)
        elif body.action == "remove":
            email = memberlib.propose_remove(ctx.mailbox, console, c.tip, body.handle)
        else:
            raise HTTPException(400, "action must be add or remove")
        _approve_as_human(ctx, email.message_id)
        return {"push_id": email.message_id}

    @app.post("/api/reply")
    def post_reply(body: dict):
        chain()
        try:
            return {"id": ctx.console.human_reply(body["target"], body.get("from") or "@nelson", body["text"])}
        except ConsoleError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/api/approve")
    def post_approve(body: dict):
        c = chain()
        ident = ctx.identity(body["as"])
        target = next((e for e in c.emails if e.message_id == body["target"]), None)
        if target is None:
            raise HTTPException(404, "unknown target")
        members = c.tip.members if c.tip else {}
        e = compose.send(ctx.mailbox, ident, members, f"Re: {target.subject}",
                         compose.approve_body(ident, target.message_id), in_reply_to=target.message_id)
        return {"id": e.message_id}

    @app.get("/api/events")
    async def events():
        async def gen():
            epoch = -1
            last_mark = None
            seen = 0
            last_tip = None
            last_props: dict[str, str] = {}
            seen_joins: set[str] = set()
            seen_bugs: dict[str, str] = {}

            def remember(c, reports) -> None:
                nonlocal seen, last_tip, last_props, seen_joins, seen_bugs
                seen = len(c.emails) + len(c.links) - 1
                last_tip = c.tip.message_id if c.tip else None
                last_props = {pid: p.status for pid, p in c.proposals.items()}
                seen_joins = {row["id"] for row in join_requests(c)}
                seen_bugs = {report.id: report.status for report in reports}

            while True:
                if ctx.mailbox is None or ctx.repo is None:
                    await asyncio.sleep(1.0)
                    continue
                mark = _cache_mark(ctx.mailbox)
                if mark is not None and epoch >= 0 and mark == last_mark and ctx.view_epoch == epoch:
                    # The cache has not grown, so the replay would see the same chain.
                    await asyncio.sleep(0.2)
                    continue
                last_mark = mark
                if ctx.view_epoch != epoch:
                    epoch = ctx.view_epoch
                    c = await asyncio.to_thread(chain)
                    members = c.tip.members if c.tip else {}
                    rows = await asyncio.to_thread(bug_rows, ctx)
                    remember(c, derive_bugs(rows, c.emails, members, bugs_address(ctx)))
                    await asyncio.sleep(1.0)
                    continue
                c = await asyncio.to_thread(chain)
                for a in reversed(activity(c, 10_000)[: max(0, len(c.emails) + len(c.links) - 1 - seen)]):
                    yield {"event": "mail", "data": json.dumps(a)}
                seen = len(c.emails) + len(c.links) - 1
                if c.tip and c.tip.message_id != last_tip:
                    last_tip = c.tip.message_id
                    yield {"event": "chain", "data": json.dumps({"tip": link_json(c.tip)})}
                for pid, p in c.proposals.items():
                    if last_props.get(pid) != p.status:
                        last_props[pid] = p.status
                        yield {"event": "proposal", "data": json.dumps(proposal_json(p))}
                for join in join_requests(c):
                    if join["id"] not in seen_joins:
                        seen_joins.add(join["id"])
                        yield {"event": "join", "data": json.dumps(join)}
                members = c.tip.members if c.tip else {}
                rows = await asyncio.to_thread(bug_rows, ctx)
                for report in derive_bugs(rows, c.emails, members, bugs_address(ctx)):
                    if seen_bugs.get(report.id) != report.status:
                        seen_bugs[report.id] = report.status
                        yield {"event": "bug", "data": json.dumps(report.to_json())}
                if mark is None:
                    await asyncio.sleep(1.0)
        return EventSourceResponse(gen())

    _start_welcome(ctx)
    boot_swarm(ctx)
    dist = Path(__file__).resolve().parent.parent / "ui" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    return app


def _cache_mark(mailbox):
    """Size and mtime of the local mailbox file, or None when this mailbox has no cache."""
    path = getattr(mailbox, "path", None)
    if path is None:
        return None
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))


def main() -> None:
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8787, log_level="warning")


if __name__ == "__main__":
    main()
