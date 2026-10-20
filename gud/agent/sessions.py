"""Warm agent sessions, one per thread (PLAN Phase 1b "Negotiation sessions", Phase 4).

Events are routed by a session key: the thread key of `gud.events.model.thread_key`
(`push:<negotiation root>`, `task:<id>`, `thread:<target>`, `escalation:<id>`, `event:<id>`),
optionally redirected by an alias table so that a unit of work keeps one context (the runtime
aliases a task's claim and the pushes made for it to the session that claimed it). Bursts are
debounced per key (`gud.events.dispatch.Debouncer`, about 1 s), and turns run one at a time:
every session of a member shares one working copy, so two turns must never edit it at once.

A `Session` is anything with `send(text) -> SessionTurn` and `close(summary) -> str`; the
Claude Agent SDK implementation is `ClaudeSession`, and tests use scripted fakes. Sessions are
closed (with a short summary, kept as notes) when their thread is finished, and the least
recently used one is closed when more than `max_open` are open."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from gud.core.types import LIVE_STATUSES, Chain, Event, Handle
from gud.events.dispatch import Debouncer
from gud.events.model import thread_key

from .budget import turn_tokens


@dataclass
class SessionTurn:
    text: str
    usd: float = 0.0
    tokens: int = 0
    session_id: str | None = None
    is_error: bool = False
    tool_calls: dict[str, int] = field(default_factory=dict)
    error: str = ""


class Session(Protocol):
    def send(self, text: str) -> SessionTurn: ...
    def close(self, summary: bool = True) -> str: ...


def thread_finished(key: str, chain: Chain, me: Handle | None = None) -> bool:
    """Default rule for when a thread's session can be summarised and closed."""
    kind, _, ident = key.partition(":")
    if kind in ("event", "idle"):
        return True
    if kind == "push":
        return not any(p.negotiation == ident or p.push == ident for p in chain.proposals.values()
                       if p.status in LIVE_STATUSES)
    if kind == "task":
        t = chain.tasks.get(ident)
        if t is None or t.status == "done":
            return True
        claim = chain.claims.get(t.claim) if t.claim else None
        mine_live = me is not None and any(p.author == me and p.task == ident and p.status in LIVE_STATUSES
                                           for p in chain.proposals.values())
        if claim is not None and claim.status == "active":
            return claim.holder != me and not mine_live
        return t.status != "open" and not mine_live
    if kind == "thread":
        if ident in chain.tasks:
            return chain.tasks[ident].status == "done"
        return not any(p.push == ident or p.id == ident for p in chain.proposals.values() if p.status in LIVE_STATUSES)
    if kind == "escalation":
        esc = chain.escalations.get(ident)
        return esc is None or esc.status != "open"
    return False


class SessionManager:
    """One warm session per thread key (events.model.THREAD_KEY), turns run one at a time."""

    def __init__(self, factory: Callable[[str], Session], *, debounce_s: float = 1.0, max_open: int = 8,
                 max_delay_s: float = 5.0, key: Callable[[Event], str] | None = None,
                 render: Callable[[str, list[Event], bool], str] | None = None,
                 before_turn: Callable[[str, list[Event]], str | None] | None = None,
                 after_turn: Callable[[str, list[Event], SessionTurn], None] | None = None,
                 finished: Callable[[str, Chain], bool] | None = None,
                 on_close: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.factory = factory
        self.max_open = max(1, int(max_open))
        self._key = key or thread_key
        self._render = render
        self._before = before_turn
        self._after = after_turn
        self._finished = finished or thread_finished
        self._on_close = on_close
        self.aliases: dict[str, str] = {}
        self.sessions: "OrderedDict[str, Session]" = OrderedDict()
        self.summaries: dict[str, str] = {}
        self.turns: list[tuple[str, int]] = []          # (session key, events in the batch)
        self.skipped: list[tuple[str, str]] = []        # (session key, why)
        self.errors: list[tuple[str, str]] = []
        self._queue: list[tuple[str, list[Event]]] = []
        self._lock = threading.Lock()
        self._debouncer = Debouncer(self._ready, delay_s=debounce_s, max_delay_s=max_delay_s, key=self.key_of,
                                    clock=clock)

    # ------------------------------------------------------------ routing

    def key_of(self, event: Event) -> str:
        base = self._key(event)
        return self.aliases.get(base, base)

    def alias(self, thread: str, session_key: str) -> None:
        if thread != session_key and thread not in self.aliases:
            self.aliases[thread] = session_key

    def route(self, events: Sequence[Event]) -> None:
        """Queue events for their sessions; they are sent after the debounce (see pump)."""
        self._debouncer.add(events)

    def wake(self, key: str, events: Sequence[Event] = ()) -> None:
        """Queue a turn for `key` without debouncing (idle wake-ups, startup)."""
        with self._lock:
            self._queue.append((key, list(events)))

    def _ready(self, key: str, events: list[Event]) -> None:
        with self._lock:
            self._queue.append((key, events))

    @property
    def pending(self) -> int:
        return self._debouncer.pending() + len(self._queue)

    def next_due(self) -> float | None:
        return self._debouncer.next_due()

    # ------------------------------------------------------------ turns

    def pump(self, now: float | None = None) -> int:
        """Flush due batches and run their turns, in order. Returns the number of turns run."""
        self._debouncer.poll(now)
        return self._drain()

    def flush(self) -> int:
        """Send everything pending now (no debounce)."""
        self._debouncer.flush_all()
        return self._drain()

    def _drain(self) -> int:
        n = 0
        while True:
            with self._lock:
                if not self._queue:
                    return n
                key, events = self._queue.pop(0)
            if self._turn(key, events):
                n += 1

    def _turn(self, key: str, events: list[Event]) -> bool:
        if self._before is not None:
            why = self._before(key, events)
            if why:
                self.skipped.append((key, why))
                return False
        new = key not in self.sessions
        session = self._open(key)
        text = self._render(key, events, new) if self._render else "\n".join(f"{e.type} {dict(e.data)}" for e in events)
        try:
            turn = session.send(text)
        except Exception as exc:  # a broken session is dropped; the next event starts a fresh one
            turn = SessionTurn(text="", is_error=True, error=f"{type(exc).__name__}: {exc}")
            self.errors.append((key, turn.error))
            self.sessions.pop(key, None)
            try:
                session.close(summary=False)
            except Exception:
                pass
        self.turns.append((key, len(events)))
        if self._after is not None:
            self._after(key, events, turn)
        return True

    def _open(self, key: str) -> Session:
        if key in self.sessions:
            self.sessions.move_to_end(key)
            return self.sessions[key]
        session = self.factory(key)
        self.sessions[key] = session
        while len(self.sessions) > self.max_open:
            oldest = next(iter(self.sessions))
            self.close(oldest, summary=True)
        return session

    # ------------------------------------------------------------ closing

    def close(self, key: str, summary: bool = True) -> str:
        session = self.sessions.pop(key, None)
        for thread in [t for t, k in self.aliases.items() if k == key]:
            del self.aliases[thread]
        if session is None:
            return ""
        try:
            text = session.close(summary=summary) or ""
        except Exception as exc:
            text = ""
            self.errors.append((key, f"close: {type(exc).__name__}: {exc}"))
        if text:
            self.summaries[key] = text
        if self._on_close is not None:
            self._on_close(key, text)
        return text

    def threads_of(self, key: str) -> list[str]:
        return [key] + sorted(t for t, k in self.aliases.items() if k == key)

    def close_finished(self, chain: Chain) -> list[str]:
        """Summarise and close sessions whose every thread is finished (push linked or died,
        task done or held by someone else, one-shot events)."""
        closed = []
        for key in list(self.sessions):
            if all(self._finished(t, chain) for t in self.threads_of(key)):
                if key in self.sessions:
                    self.close(key, summary=not key.startswith(("event:", "idle")))
                    closed.append(key)
        return closed

    def close_all(self, summary: bool = False) -> None:
        for key in list(self.sessions):
            self.close(key, summary=summary)


# ---------------------------------------------------------------- the Claude Agent SDK session

class AsyncLoop:
    """A private asyncio loop on a daemon thread, so synchronous code can drive SDK clients."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="gud-agent-loop", daemon=True)
        self._thread.start()

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


class ClaudeSession:
    """One Claude Agent SDK conversation (`ClaudeSDKClient`), kept open across turns.

    `options(resume)` builds `ClaudeAgentOptions` (resume = an earlier session id, after a
    restart). The SDK reports `total_cost_usd` cumulatively per session and `usage` per turn, so
    a turn costs the difference of the totals. `on_message(key, message)` sees every SDK message
    (transcripts); `on_extra_turn(turn)` is charged for the closing summary turn."""

    def __init__(self, key: str, options: Callable[[str | None], Any], loop: AsyncLoop, *,
                 resume: str | None = None, turn_timeout_s: float = 1800.0,
                 on_message: Callable[[str, Any], None] | None = None,
                 on_extra_turn: Callable[[SessionTurn], None] | None = None,
                 summary_prompt: str = "") -> None:
        self.key = key
        self._options = options
        self._loop = loop
        self.session_id = resume
        self._timeout = turn_timeout_s
        self._on_message = on_message
        self._on_extra = on_extra_turn
        self._summary_prompt = summary_prompt
        self._client: Any = None
        self._last_total = 0.0
        self.turns = 0

    async def _ensure(self) -> Any:
        if self._client is None:
            from claude_agent_sdk import ClaudeSDKClient
            self._client = ClaudeSDKClient(self._options(self.session_id))
            await self._client.connect()
        return self._client

    async def _send(self, text: str) -> SessionTurn:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
        client = await self._ensure()
        await client.query(text)
        texts: list[str] = []
        tools: dict[str, int] = {}
        turn = SessionTurn(text="")
        async for msg in client.receive_response():
            if self._on_message is not None:
                try:
                    self._on_message(self.key, msg)
                except Exception:
                    pass
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tools[block.name] = tools.get(block.name, 0) + 1
            elif isinstance(msg, ResultMessage):
                total = float(msg.total_cost_usd or 0.0)
                usd = total - self._last_total if total >= self._last_total else total
                self._last_total = total
                turn = SessionTurn(text=(msg.result or "\n".join(texts)).strip(), usd=usd, tokens=turn_tokens(msg.usage),
                                   session_id=msg.session_id, is_error=bool(msg.is_error), tool_calls=tools,
                                   error="; ".join(msg.errors or ()) if msg.is_error else "")
        if not turn.text and texts:
            turn.text = "\n".join(texts).strip()
        if turn.session_id:
            self.session_id = turn.session_id
        self.turns += 1
        return turn

    def send(self, text: str) -> SessionTurn:
        try:
            return self._loop.run(self._send(text), self._timeout)
        except TimeoutError:
            try:
                self._loop.run(self._client.interrupt(), 30)
            except Exception:
                pass
            raise

    def close(self, summary: bool = True) -> str:
        text = ""
        if self._client is not None:
            if summary and self._summary_prompt and self.turns:
                try:
                    turn = self.send(self._summary_prompt)
                    text = turn.text
                    if self._on_extra is not None:
                        self._on_extra(turn)
                except Exception:
                    text = ""
            try:
                self._loop.run(self._client.disconnect(), 60)
            except Exception:
                pass
            self._client = None
        return text


def summarise_usage(turns: Sequence[Mapping[str, Any]]) -> dict:
    """Totals over turn records written by the runtime (usd, tokens, turns, tool calls)."""
    out: dict[str, Any] = {"turns": 0, "usd": 0.0, "tokens": 0, "tool_calls": {}}
    for t in turns:
        out["turns"] += 1
        out["usd"] += float(t.get("usd") or 0.0)
        out["tokens"] += int(t.get("tokens") or 0)
        for name, n in (t.get("tool_calls") or {}).items():
            out["tool_calls"][name] = out["tool_calls"].get(name, 0) + int(n)
    return out
