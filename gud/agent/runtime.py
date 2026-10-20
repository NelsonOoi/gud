"""The reference agent runtime (`gud agent --handle @x`, PLAN Phase 4 "Reference agent").

    python -m gud.agent.runtime --handle @x [--home H] [--wc DIR] [--listen :PORT] [--advertise URL]
                                [--model claude-sonnet-5-5] [--budget-usd N] [--budget-tokens N]
                                [--task-budget-usd N] [--debounce 1] [--max-open 8] [--poll 2] [--idle 300]

One process per member. It

1. **receives events** (§19): as a webhook receiver (`--listen :PORT`, registered with the host
   through `POST /api/v1/delivery`), or from the host's SSE stream, and always by polling the
   repo (`RepoClient.sync_log()`; replay derives the same events with the same ids), which is the
   only channel in local host mode and the fallback elsewhere. Every channel feeds the member's
   event journal (`gud.mcp_v1.MemberEvents`), deduped by event id; the runtime routes journal
   entries after its persisted cursor, so a restart resumes where it stopped and never routes an
   event twice;
2. **routes** each event to a warm session per thread (`SessionManager`), debounced about 1 s;
   a task claimed in a session, and the pushes made for it, are aliased to that session so the
   work keeps one context;
3. runs each turn in a **Claude Agent SDK** session whose cwd is the member's working copy, with
   the native Read/Edit/Write/Bash/Grep/Glob tools and the gud MCP server (`python -m
   gud.mcp_v1`, bound to this member and working copy), and one general system prompt;
4. charges **budgets** per member and per (root) task; when one runs out it escalates
   (`comment` with `Gud-Escalate: budget`), releases the claims concerned and stops spending;
5. **closes** sessions whose threads are finished, keeping their summaries as notes for later
   sessions; and when idle with unclaimed open work on the board, wakes once per board state
   to consider claiming.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from gud.core.types import LIVE_STATUSES, Chain, Event, Hex
from gud.events.dispatch import EventClient
from gud.events.model import thread_key
from gud.mcp_v1 import GudTools, MemberEvents, ToolError, is_echo, journal_deltas, open_member
from gud.wc.copy import WcError, WorkingCopy
from gud.wc.remote import RemoteError, RepoClient

from .budget import Budget, Ledger
from .prompt import SUMMARY_PROMPT, render_batch, system_prompt
from .sessions import AsyncLoop, ClaudeSession, Session, SessionManager, SessionTurn, thread_finished

DEFAULT_MODEL = "claude-sonnet-5-5"
NATIVE_TOOLS = ("Read", "Edit", "Write", "Bash", "Grep", "Glob")
_OPEN_WORK = ("task.opened", "claim.lapsed", "subtask.posted", "fix.opened")


def _slug(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:80]


class AgentRuntime:
    def __init__(self, client: RepoClient, wc: WorkingCopy, *, model: str = DEFAULT_MODEL, budget: Budget,
                 listen: str | None = None, task_budget: Budget | None = None,
                 session_factory: Callable[[str], Session] | None = None, state_dir: Path | None = None,
                 debounce_s: float = 1.0, max_open: int = 8, poll_s: float = 2.0, idle_s: float = 300.0,
                 advertise: str | None = None, max_turns: int = 80, turn_timeout_s: float = 1800.0,
                 extra_prompt: str = "", deny_paths: Sequence[str] = (), mcp_env: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] | None = None) -> None:
        self.client = client
        self.wc = wc
        self.me = client.handle
        self.model = model
        self.listen = listen
        self.advertise = advertise
        self.poll_s = poll_s
        self.idle_s = idle_s
        self.max_turns = max_turns
        self.turn_timeout_s = turn_timeout_s
        self.extra_prompt = extra_prompt
        self.deny_paths = tuple(deny_paths)
        self.mcp_env = dict(mcp_env or {})
        self.clock = clock
        self.monotonic = monotonic
        self._log = log or (lambda line: print(f"[{self.me}] {line}", file=sys.stderr, flush=True))
        self.state_dir = Path(state_dir) if state_dir else client.home / "members" / self.me / "agent"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger.load(budget, task_usd=task_budget.usd if task_budget else None,
                                  task_tokens=task_budget.tokens if task_budget else None,
                                  path=self.state_dir / "budget.json")
        self.journal = MemberEvents(client.home, self.me)
        journal_deltas(client, self.journal)
        self.events = EventClient(self.state_dir / "events.json", self._host_keys, repo=client.repo)
        self.tools = GudTools(client, wc.root, json=True, events=self.journal)
        self._state = self._load_state()
        self._incoming: "queue.Queue[Event]" = queue.Queue()
        self._chain: Chain | None = None
        self._loop: AsyncLoop | None = None
        self._last_turn = monotonic()
        self._server: ThreadingHTTPServer | None = None
        self.manager = SessionManager(session_factory or self.claude_session, debounce_s=debounce_s, max_open=max_open,
                                      key=self._session_key, render=self._render, before_turn=self._before_turn,
                                      after_turn=self._after_turn, finished=self._finished, on_close=self._on_close,
                                      clock=monotonic)
        self.manager.aliases.update(self._state.get("aliases", {}))
        self.stopped_reason: str | None = None

    # ------------------------------------------------------------ state

    def _load_state(self) -> dict:
        path = self.state_dir / "runtime.json"
        if path.exists():
            try:
                return json.loads(path.read_text("utf-8"))
            except ValueError:
                pass
        return {"routed": 0, "aliases": {}, "sessions": {}, "notes": [], "idle_sig": None, "idle_n": 0}

    def _save_state(self) -> None:
        self._state["aliases"] = dict(self.manager.aliases)
        path = self.state_dir / "runtime.json"
        tmp = path.with_name(f".runtime.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._state, indent=1, sort_keys=True), "utf-8")
        os.replace(tmp, path)

    def _host_keys(self) -> list[str]:
        keys = []
        try:
            keys.append(self.client.replayer().log().key)
        except Exception:
            pass
        try:
            host = self.client.reader().members().host()
            if host.public_key:
                keys.append(host.public_key)
        except Exception:
            pass
        return [k for k in dict.fromkeys(keys) if k]

    def chain(self) -> Chain:
        if self._chain is None:
            self._chain = self.client.chain()
        return self._chain

    def _sync(self) -> Chain:
        try:
            self.client.sync_log()                   # journals the delta's events (journal_deltas)
        except RemoteError as exc:
            self._log(f"sync failed: {exc}")
        self._chain = self.client.chain()
        return self._chain

    # ------------------------------------------------------------ events in

    def on_events(self, events: Sequence[Event]) -> None:
        """Record events (deduped by id into the member journal) and route the new journal
        entries to their sessions."""
        self.journal.record(events)
        self._route_journal()

    def _route_journal(self) -> int:
        new = self.journal.since(int(self._state.get("routed", 0)))
        if not new:
            return 0
        self._state["routed"] = new[-1][0]
        self._save_state()
        wake = [e for _, e in new if not self.quiet(e)]
        self.manager.route(wake)
        return len(wake)

    def quiet(self, event: Event) -> bool:
        """Events that only confirm this member's own action, or are pure information, are kept in
        the journal (inbox shows them) but do not wake a turn."""
        d = event.data
        if is_echo(event, self.me):
            return True
        if event.type == "window.closing":
            prop = self.chain().proposals.get(d.get("candidate")) if isinstance(d.get("candidate"), str) else None
            return prop is None or prop.author == self.me
        return event.type in ("approved", "held", "test.passed", "carried", "task.ready", "task.done", "push.received")

    def accept_webhook(self, headers: Mapping[str, str], body: bytes) -> int:
        """HTTP status for one webhook POST (§19.3): 200 for new and duplicate events, 401 when the
        signature or body fails."""
        before = self.events.rejected
        event = self.events.accept_webhook(headers, body, self.clock())
        if event is not None:
            self._incoming.put(event)
            return 200
        return 401 if self.events.rejected > before else 200

    def _serve_webhooks(self) -> str:
        host, _, port = (self.listen or "").rpartition(":")
        host = host or "127.0.0.1"
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                code = runtime.accept_webhook({k: v for k, v in self.headers.items()}, body)
                self.send_response(code)
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok\n")

            def log_message(self, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer((host, int(port or 0)), Handler)
        threading.Thread(target=self._server.serve_forever, name="gud-agent-hook", daemon=True).start()
        bound = self._server.server_address[1]
        url = self.advertise or f"http://{host}:{bound}/hook"
        try:
            self.client.set_delivery({"webhook": url})
        except RemoteError as exc:
            self._log(f"could not register the webhook {url}: {exc}")
        self._log(f"listening for webhooks on {host}:{bound} ({url})")
        return url

    def _stream_sse(self, stop: threading.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                for event in self.events.stream(self.client, self.me, timeout=300):
                    self._incoming.put(event)
                    backoff = 1.0
                    if stop.is_set():
                        return
            except Exception as exc:
                self._log(f"SSE: {exc}; retrying in {backoff:.0f}s")
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

    # ------------------------------------------------------------ routing

    def _roots(self, chain: Chain) -> dict[Hex, Hex]:
        return {p.push: p.negotiation for p in chain.proposals.values()} | {
            p.id: p.negotiation for p in chain.proposals.values()}

    def _session_key(self, event: Event) -> str:
        chain = self.chain()
        base = thread_key(event, self._roots(chain))
        if base in self.manager.aliases:
            return self.manager.aliases[base]
        if base.startswith(("push:", "thread:")):
            root = base.partition(":")[2]
            prop = chain.push_status(root)
            task = prop.task if prop is not None else None
            if task is None:
                link = next((l for l in chain.links if l.push == root), None)
                task = link.task if link is not None else None
                author = link.author if link is not None else None
            else:
                author = prop.author  # type: ignore[union-attr]
            if task and author == self.me:
                tk = f"task:{task}"
                return self.manager.aliases.get(tk, tk)
        return base

    def _task_of(self, key: str, events: Sequence[Event]) -> Hex | None:
        """The root task a turn works for (for the per-task budget)."""
        chain = self.chain()
        task = None
        kind, _, ident = key.partition(":")
        if kind == "task":
            task = ident
        elif kind == "push":
            prop = chain.push_status(ident)
            task = prop.task if prop else None
        for thread in self.manager.threads_of(key):
            if task:
                break
            if thread.startswith("task:"):
                task = thread[5:]
        for e in events:
            if task:
                break
            t = e.data.get("task")
            task = t if isinstance(t, str) else None
        seen = set()
        while task and task in chain.tasks and chain.tasks[task].parent and task not in seen:
            seen.add(task)
            task = chain.tasks[task].parent
        return task

    # ------------------------------------------------------------ turns

    def _render(self, key: str, events: list[Event], new: bool) -> str:
        chain = self.chain()
        notes = list(self._state.get("notes", []))[-6:] if new else []
        status = f"Budget: {self.ledger.member.describe()}."
        return render_batch(events, chain, key=key, notes=notes, status=status, idle=key.startswith("idle"))

    def _mine(self, chain: Chain) -> tuple[set[Hex], set[Hex]]:
        claims = {c.task for c in chain.claims.values() if c.holder == self.me and c.status == "active"}
        pushes = {p.negotiation for p in chain.proposals.values() if p.author == self.me and p.status in LIVE_STATUSES}
        return claims, pushes

    def _before_turn(self, key: str, events: list[Event]) -> str | None:
        task = self._task_of(key, events)
        why = self.ledger.blocked(task)
        if why:
            self._escalate(task, why)
            return why
        self._before_mine = self._mine(self.chain())
        return None

    def _after_turn(self, key: str, events: list[Event], turn: SessionTurn) -> None:
        self._last_turn = self.monotonic()
        task = self._task_of(key, events)
        self.ledger.charge(turn.usd, turn.tokens, task)
        if turn.session_id:
            self._state.setdefault("sessions", {})[key] = turn.session_id
        record = {"at": int(self.clock() * 1000), "key": key, "task": task, "events": [e.type for e in events],
                  "usd": round(turn.usd, 6), "tokens": turn.tokens, "tool_calls": turn.tool_calls,
                  "is_error": turn.is_error, "error": turn.error, "text": turn.text[-2000:]}
        with open(self.state_dir / "turns.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        self._log(f"turn {key} ({len(events)} events): ${turn.usd:.3f} {turn.tokens} tok; "
                  f"{(turn.text.splitlines() or [''])[-1][:160]}")
        chain = self._sync()
        # claims taken and pushes sent during this turn belong to this session's unit of work
        claims0, pushes0 = getattr(self, "_before_mine", (set(), set()))
        claims1, pushes1 = self._mine(chain)
        for task in sorted(claims1 - claims0):
            if f"task:{task}" not in self.manager.sessions:
                self.manager.alias(f"task:{task}", key)
        for root in sorted(pushes1 - pushes0):
            self.manager.alias(f"push:{root}", key)
        if key.startswith("idle") and any(v == key for v in self.manager.aliases.values()):
            self._state["idle_n"] = int(self._state.get("idle_n", 0)) + 1
        self._save_state()

    def _finished(self, key: str, chain: Chain) -> bool:
        return thread_finished(key, chain, self.me)

    def _on_close(self, key: str, summary: str) -> None:
        if summary:
            notes = list(self._state.get("notes", []))
            notes.append(f"[{key}] " + " ".join(summary.split())[:600])
            self._state["notes"] = notes[-20:]
        self._state.get("sessions", {}).pop(key, None)
        self._save_state()

    def _escalate(self, task: Hex | None, why: str) -> None:
        member = why.startswith("member")
        if member:
            if self.ledger.member_escalated:
                return
            self.ledger.member_escalated = True
        elif not task or task in self.ledger.escalated:
            return
        if task:
            self.ledger.escalated.add(task)
        self.ledger.save()
        chain = self._sync()
        targets = set()
        if member:
            targets |= {c.task for c in chain.claims.values() if c.holder == self.me and c.status == "active"}
        if task:
            targets.add(task)
        for tid in sorted(targets):
            self._log(f"escalating {tid[:12]}: {why}")
            out = self.tools.comment(tid, f"{self.me} stops working on this: {why}. Escalating to the requester.",
                                     task=tid, escalate="budget")
            if "error" in out:
                self._log(f"escalation comment failed: {out}")
            for c in chain.claims.values():
                if c.holder == self.me and c.status == "active" and (c.task == tid or self._root(chain, c.task) == tid):
                    self.tools.release(c.id, note=f"budget: {why}")
        if member:
            self.stopped_reason = why

    @staticmethod
    def _root(chain: Chain, task: Hex) -> Hex:
        seen = set()
        while task in chain.tasks and chain.tasks[task].parent and task not in seen:
            seen.add(task)
            task = chain.tasks[task].parent  # type: ignore[assignment]
        return task

    # ------------------------------------------------------------ idle

    def _open_work(self, chain: Chain) -> list[Hex]:
        out = []
        for t in chain.tasks.values():
            if t.status != "open":
                continue
            claim = chain.claims.get(t.claim) if t.claim else None
            if claim is not None and claim.status == "active":
                continue
            if t.children and any(c in chain.tasks and chain.tasks[c].status != "done" for c in t.children):
                continue
            if t.kind == "fix" and t.offered_to and t.offered_to != self.me and t.offer_until and \
                    t.offer_until > self.client.now_ms():
                continue
            out.append(t.id)
        return sorted(out)

    def idle_check(self, *, force: bool = False, startup: bool = False) -> bool:
        """Wake an idle session once per board state when unclaimed open work exists (and, at
        startup, when something already needs this member: a review, an objection, ...)."""
        if self.manager.pending or self.ledger.blocked(None):
            return False
        if not force and self.monotonic() - self._last_turn < self.idle_s:
            return False
        chain = self.chain()
        work = self._open_work(chain)
        needs: list[str] = []
        if startup and chain.tip is not None:
            try:
                needs = sorted(json.dumps(n, sort_keys=True) for n in self.tools._needs(chain)
                               if n.get("type") in ("review", "objection", "escalation"))
            except Exception:
                needs = []
        if not work and not needs:
            return False
        sig = hashlib.sha256((",".join(work + needs) + "|" + str(len(chain.links))).encode()).hexdigest()
        if sig == self._state.get("idle_sig"):
            return False
        self._state["idle_sig"] = sig
        self._save_state()
        self.manager.wake(f"idle:{self._state.get('idle_n', 0)}")
        return True

    # ------------------------------------------------------------ the loop

    def step(self, *, poll: bool = True) -> int:
        """One pass: take incoming events, poll the repo, route new journal entries, run due turns,
        close finished sessions. Returns the number of turns run."""
        drained = []
        while True:
            try:
                drained.append(self._incoming.get_nowait())
            except queue.Empty:
                break
        if drained:
            self.journal.record(drained)
        chain = self._sync() if (poll or drained) else self.chain()      # pushed events: sync before the turn
        self._route_journal()
        n = self.manager.pump()
        if n:
            chain = self._sync()
        self.manager.close_finished(chain)
        return n

    def run(self, stop: threading.Event) -> None:
        """Webhook receiver (listen) or SSE when the host is HTTP; polling always (the only
        channel in local host mode). Returns when `stop` is set or the member budget ran out."""
        threads = []
        push_channel = False
        if self.listen:
            self._serve_webhooks()
            push_channel = True
        elif self.client.host_url.startswith(("http://", "https://")):
            try:
                self.client.set_delivery({"sse": True})
            except RemoteError as exc:
                self._log(f"could not register for SSE: {exc}")
            t = threading.Thread(target=self._stream_sse, args=(stop,), name="gud-agent-sse", daemon=True)
            t.start()
            threads.append(t)
            push_channel = True
        poll_every = max(self.poll_s, 10.0) if push_channel else self.poll_s
        self._sync()
        if not self._route_journal():
            self.idle_check(force=True, startup=True)
        next_poll = 0.0
        try:
            while not stop.is_set():
                now = self.monotonic()
                poll = now >= next_poll
                if poll:
                    next_poll = now + poll_every
                self.step(poll=poll)
                if self.stopped_reason:
                    self._log(f"stopping: {self.stopped_reason}")
                    break
                self.idle_check()
                due = self.manager.next_due()
                wait = min(next_poll - self.monotonic(), (due - self.monotonic()) if due else poll_every, 0.5)
                stop.wait(max(0.05, wait))
        finally:
            self.manager.close_all(summary=False)
            if self._server is not None:
                self._server.shutdown()
            if self._loop is not None:
                self._loop.stop()
            self._save_state()

    # ------------------------------------------------------------ Claude Agent SDK

    def mcp_server_config(self) -> dict:
        import gud
        env = {"PYTHONPATH": str(Path(gud.__file__).resolve().parents[1]), "GUD_HOME": str(self.client.home),
               "GUD_HANDLE": self.me, **self.mcp_env}
        return {"gud": {"type": "stdio", "command": sys.executable,
                        "args": ["-m", "gud.mcp_v1", "--home", str(self.client.home), "--handle", self.me,
                                 "--wc", str(self.wc.root)], "env": env}}

    def options(self, resume: str | None = None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions
        denied = []
        # absolute paths in permission rules are written //abs/path (as in evals/v1/agents.py)
        for path in self.deny_paths:
            for tool in ("Read", "Edit", "Write", "Glob", "Grep"):
                denied.append(f"{tool}(/{Path(path).resolve()}/**)")
        denied.append(f"Read(/{self.client.home}/members/*/key)")
        remaining = self.ledger.member.remaining_usd
        return ClaudeAgentOptions(
            model=self.model or None,
            cwd=str(self.wc.root),
            system_prompt=system_prompt(self.me, str(self.wc.root), budget=self.ledger.member.describe(),
                                        extra=self.extra_prompt),
            tools=list(NATIVE_TOOLS),
            allowed_tools=list(NATIVE_TOOLS) + ["mcp__gud"],
            disallowed_tools=denied,
            permission_mode="dontAsk",
            mcp_servers=self.mcp_server_config(),
            strict_mcp_config=True,
            setting_sources=[],
            max_turns=self.max_turns,
            max_budget_usd=remaining if remaining else None,
            resume=resume,
            # no bytecode or pytest caches in the working copy: they are not source (push refuses them)
            env={"GUD_HANDLE": self.me, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"},
        )

    def _transcript(self, key: str, msg: Any) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
        rows = []
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    rows.append({"type": "text", "text": b.text})
                elif isinstance(b, ToolUseBlock):
                    rows.append({"type": "tool_use", "name": b.name, "input": b.input})
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    content = b.content if isinstance(b.content, str) else json.dumps(b.content, default=str)
                    rows.append({"type": "tool_result", "error": bool(b.is_error), "content": (content or "")[:4000]})
        elif isinstance(msg, ResultMessage):
            rows.append({"type": "result", "cost": msg.total_cost_usd, "turns": msg.num_turns, "error": msg.is_error,
                         "session": msg.session_id})
        if rows:
            path = self.state_dir / "transcripts" / f"{_slug(key)}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps({"at": int(self.clock() * 1000), **row}, default=str) + "\n")

    def claude_session(self, thread_key: str) -> Session:
        """A Claude Agent SDK session for one thread: cwd = the working copy, tools = native +
        the gud MCP server, the general system prompt. Resumes the thread's earlier SDK session
        after a restart."""
        if self._loop is None:
            self._loop = AsyncLoop()
        resume = self._state.get("sessions", {}).get(thread_key)
        return ClaudeSession(thread_key, self.options, self._loop, resume=resume, turn_timeout_s=self.turn_timeout_s,
                             on_message=self._transcript,
                             on_extra_turn=lambda turn: self.ledger.charge(turn.usd, turn.tokens,
                                                                           self._task_of(thread_key, [])),
                             summary_prompt=SUMMARY_PROMPT)


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gud agent", description="the gud reference agent (Claude Agent SDK)")
    parser.add_argument("--handle", required=False, default=None)
    parser.add_argument("--home", default=None, help="GUD_HOME (default $GUD_HOME or ~/.gud)")
    parser.add_argument("--wc", default=None, help="working copy (default: the member's, cloned if missing)")
    parser.add_argument("--listen", default=None, metavar="[HOST]:PORT", help="webhook receiver, registered with the host")
    parser.add_argument("--advertise", default=None, help="public URL of the webhook receiver")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget-usd", type=float, default=None)
    parser.add_argument("--budget-tokens", type=int, default=None)
    parser.add_argument("--task-budget-usd", type=float, default=None)
    parser.add_argument("--task-budget-tokens", type=int, default=None)
    parser.add_argument("--debounce", type=float, default=1.0)
    parser.add_argument("--max-open", type=int, default=8)
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument("--idle", type=float, default=300.0, help="seconds without a turn before an idle wake-up")
    parser.add_argument("--max-turns", type=int, default=80, help="agentic steps per wake-up")
    parser.add_argument("--deny-path", action="append", default=[], help="paths the session may not read")
    args = parser.parse_args(argv)
    try:
        client, wc_root = open_member(args.home, args.handle, args.wc)
    except (ToolError, RemoteError) as exc:
        print(f"gud agent: {exc}", file=sys.stderr)
        return 2
    journal_deltas(client, MemberEvents(client.home, client.handle))   # the clone's sync is journaled too
    if wc_root is None or not (wc_root / ".gud/BASE").exists():
        target = wc_root or client.home / "members" / client.handle / "wc"
        try:
            wc = WorkingCopy.clone(client, target)
        except WcError as exc:
            print(f"gud agent: cannot clone a working copy into {target}: {exc}", file=sys.stderr)
            return 2
    else:
        wc = WorkingCopy(wc_root, client)
    runtime = AgentRuntime(client, wc, model=args.model, budget=Budget(args.budget_usd, args.budget_tokens),
                           task_budget=Budget(args.task_budget_usd, args.task_budget_tokens),
                           listen=args.listen, advertise=args.advertise, debounce_s=args.debounce,
                           max_open=args.max_open, poll_s=args.poll, idle_s=args.idle, max_turns=args.max_turns,
                           deny_paths=args.deny_path)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    runtime.run(stop)
    return 0 if not runtime.stopped_reason else 1


if __name__ == "__main__":
    sys.exit(main())
