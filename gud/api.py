"""HTTP API for the UI and demo driver. Shapes are in api_contract.md.

Run: .venv/bin/python -m gud.api   (serves http://localhost:8787, and ui/dist if built)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import compose, config, sig, tasks as tasklib
from .chain import Repo
from .memberfile import by_address
from .patch import diffstat
from .types import Email, LinkState, Member, ProposalState
from .wire import parse_body, short


class Ctx:
    def __init__(self, cfg: config.Config, mailbox=None, clock=time.time):
        from .mailbox import open_mailbox
        self.cfg = cfg
        self.clock = clock
        self.mailbox = mailbox or open_mailbox(cfg)
        self.repo = Repo(self.mailbox, cfg.window_seconds, clock=clock)

    def identity(self, handle: str) -> compose.Identity:
        path = self.cfg.key_path(handle)
        if not path.exists():
            raise HTTPException(400, f"no local key for {handle}")
        return compose.Identity(handle, self.cfg.address(handle), sig.load_private(path))


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


def all_members(chain) -> dict[str, Member]:
    out: dict[str, Member] = {}
    for l in chain.links:
        out.update(l.members)
    return out


def handle_of(chain, address: str) -> str:
    m = by_address(all_members(chain), address)
    return m.handle if m else address


def email_kind(e: Email) -> str:
    return parse_body(e.body).kind or "human"


def activity(chain, limit: int = 100) -> list[dict]:
    items = []
    for e in chain.emails:
        p = parse_body(e.body)
        kind = p.kind or "human"
        summary = (p.get("Gud-Reason") or p.text or "").strip().splitlines()
        items.append({"id": e.message_id, "at": e.received_at, "kind": kind,
                      "from": p.get("Gud-Author") or handle_of(chain, e.from_addr),
                      "subject": e.subject, "summary": (summary[0] if summary else "")[:200],
                      "target": p.get("Gud-Target") or e.in_reply_to})
    for l in chain.links[1:]:
        items.append({"id": f"link:{l.message_id}", "at": l.linked_at, "kind": "link", "from": l.author,
                      "subject": l.subject, "summary": f"link #{l.seq} {short(l.state)}",
                      "target": l.message_id})
    items.sort(key=lambda a: a["at"], reverse=True)
    return items[:limit]


def email_json(chain, e: Email) -> dict:
    return {"id": e.message_id, "from": handle_of(chain, e.from_addr), "to": list(e.to), "cc": list(e.cc),
            "subject": e.subject, "body": e.body, "received_at": e.received_at,
            "in_reply_to": e.in_reply_to, "sender_verified": e.sender_verified, "kind": parse_body(e.body).kind}


class TaskIn(BaseModel):
    title: str
    description: str = ""
    requester: str


class MemberIn(BaseModel):
    action: str
    handle: str
    role: str | None = None
    address: str | None = None
    public_key: str | None = None
    sponsor: str | None = None


def create_app(ctx: Ctx | None = None) -> FastAPI:
    ctx = ctx or Ctx(config.load())
    app = FastAPI(title="gud")
    app.state.ctx = ctx
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def chain():
        return ctx.repo.refresh()

    def link_or_tip(c, at: str | None) -> LinkState:
        l = c.by_id.get(at) if at else c.tip
        if l is None:
            raise HTTPException(404, "no such link")
        return l

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
        removed = [m for h, m in all_members(c).items() if h not in current]
        return {"repo": ctx.mailbox.repo_address, "now": ctx.clock(), "window_seconds": ctx.cfg.window_seconds,
                "tip": link_json(tip) if tip else None,
                "links": [link_json(l) for l in reversed(c.links)],
                "proposals": sorted((proposal_json(p) for p in c.proposals.values()),
                                    key=lambda p: p["received_at"], reverse=True),
                "members": [member_json(m, tip, added_by) for m in current.values()],
                "removed_members": [member_json(m, tip, added_by) for m in removed]}

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
        return [t.to_json() for t in tasklib.derive(chain())]

    @app.get("/api/activity")
    def get_activity(limit: int = 100):
        return activity(chain(), limit)

    @app.get("/api/thread/{id:path}")
    def thread(id: str):
        c = chain()
        return [email_json(c, e) for e in ctx.repo.thread(id)]

    @app.post("/api/tasks")
    def post_task(body: TaskIn):
        c = chain()
        tid = tasklib.next_task_id(c.emails)
        console = ctx.identity("@console")
        text = compose.task_body(console, tid, body.requester, body.description or body.title)
        members = c.tip.members if c.tip else {}
        compose.send(ctx.mailbox, console, members, f"[gud] {tid}: {body.title}", text)
        return {"id": tid}

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
            m = Member(body.handle, body.role, body.address.lower(), body.public_key, body.sponsor)
            email = memberlib.propose_add(ctx.mailbox, console, c.tip, m)
        elif body.action == "remove":
            email = memberlib.propose_remove(ctx.mailbox, console, c.tip, body.handle)
        else:
            raise HTTPException(400, "action must be add or remove")
        return {"push_id": email.message_id}

    @app.post("/api/reply")
    def post_reply(body: dict):
        if ctx.cfg.mailbox != "fake":
            raise HTTPException(400, "human replies are only simulated with the fake mailbox")
        c = chain()
        who = body.get("from") or "@nelson"
        member = all_members(c).get(who)
        if member is None or member.role != "human":
            raise HTTPException(400, f"{who} is not a human member")
        target = next((e for e in c.emails if e.message_id == body["target"]), None)
        if target is None:
            raise HTTPException(404, "unknown target")
        e = ctx.mailbox.send(from_addr=member.address, to=ctx.mailbox.repo_address,
                             subject=f"Re: {target.subject}", body=body["text"].strip() + "\n",
                             in_reply_to=target.message_id)
        return {"id": e.message_id}

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
            seen = 0
            last_tip = None
            last_props: dict[str, str] = {}
            while True:
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
                await asyncio.sleep(1.0)
        return EventSourceResponse(gen())

    dist = Path(__file__).resolve().parent.parent / "ui" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    return app


def main() -> None:
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8787, log_level="warning")


if __name__ == "__main__":
    main()
