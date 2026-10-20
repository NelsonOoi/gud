"""The reference agent runtime with a scripted session backend (no model calls): routing by thread,
warm-session reuse, debounce, budgets and escalation, restart resume from the event cursor,
webhook receipt, idle wake-ups. The repo runs in local host mode over the fake transport."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from gud.agent.budget import Budget, Ledger, turn_tokens
from gud.agent.prompt import SYSTEM_PROMPT, render_batch, system_prompt
from gud.agent.runtime import AgentRuntime
from gud.agent.sessions import SessionManager, SessionTurn, thread_finished
from gud.core.envelope import make_event
from gud.core.types import Event
from gud.events.dispatch import sign_hook
from gud.events.model import to_json
from gud.mcp_v1 import GudTools
from gud.wc.copy import WorkingCopy

from tests.v1.kit import REPO
from tests.v1.mcp.conftest import LocalRepo


class FakeSession:
    """A deterministic 'model': `brain(session, text)` acts through the member's tools and returns
    what the turn cost."""

    def __init__(self, key: str, brain: Callable[["FakeSession", str], SessionTurn], log: list) -> None:
        self.key = key
        self.brain = brain
        self.texts: list[str] = []
        self.closed: bool | None = None
        self.log = log

    def send(self, text: str) -> SessionTurn:
        self.texts.append(text)
        self.log.append(("send", self.key, text))
        return self.brain(self, text)

    def close(self, summary: bool = True) -> str:
        self.closed = summary
        self.log.append(("close", self.key, summary))
        return f"notes for {self.key}" if summary else ""


class World:
    def __init__(self, tmp: Path) -> None:
        self.repo = LocalRepo(tmp)
        self.genesis = self.repo.genesis({"app.py": "x = 1\n"})
        self.log: list = []
        self.sessions: dict[str, list[FakeSession]] = {}

    def runtime(self, handle: str, brain: Callable[[FakeSession, str], SessionTurn], *,
                budget: Budget | None = None, task_budget: Budget | None = None, **kw: Any) -> AgentRuntime:
        r = self.repo
        if not (r.wc_dir(handle) / ".gud/BASE").exists():
            r.clone(handle)
        wc = WorkingCopy(r.wc_dir(handle), r.client(handle))

        def factory(key: str) -> FakeSession:
            s = FakeSession(key, brain, self.log)
            self.sessions.setdefault(key, []).append(s)
            return s

        return AgentRuntime(r.client(handle), wc, budget=budget or Budget(None, None), task_budget=task_budget,
                            session_factory=factory, debounce_s=0.0, poll_s=0.1, idle_s=3600,
                            log=lambda line: None, **kw)

    def tools(self, handle: str) -> GudTools:
        return self.repo.tools(handle)


def worker(handle: str, world: World, *, usd: float = 0.01) -> Callable[[FakeSession, str], SessionTurn]:
    """Claims the first open task, writes <handle>.py, pushes; answers comments; approves reviews."""

    def brain(session: FakeSession, text: str) -> SessionTurn:
        tools = world.tools(handle)
        did = []
        if "[task.opened]" in text or "open work on the board" in text:
            board = json.loads(tools.tasks())
            for t in board["tasks"]:
                if t.get("status") == "open" and not t.get("claim_state"):
                    name = f"{handle.lstrip('@')}_{t['alias'].lower().replace('.', '_')}.py"
                    out = json.loads(tools.claim(t["alias"], [name]))
                    if out.get("status") != "active":
                        continue
                    (world.repo.wc_dir(handle) / name).write_text(f"# {t['title']}\n")
                    json.loads(tools.push(f"do {t['alias']}", task=t["alias"]))
                    did.append(f"claimed {t['alias']}")
                    break
        if "[review.requested]" in text:
            push = text.split("push ", 1)[1].split(" ", 1)[0]
            tools.approve(push, "ok")
            did.append(f"approved {push}")
        if "[commented]" in text:
            target = text.split(" on ", 1)[1].split(":", 1)[0]
            tools.comment(target, "thanks, noted")
            did.append("answered")
        return SessionTurn(text="; ".join(did) or "nothing to do", usd=usd, tokens=1000, session_id=f"sid-{session.key}")

    return brain


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_routes_by_thread_and_reuses_the_warm_session(world: World):
    alice = world.runtime("@alice", worker("@alice", world))
    world.repo.task("Build alice part")
    assert alice.step() == 1                                  # task.opened -> task:<T1> session claims and pushes
    t1 = next(iter(alice.chain().tasks))
    assert list(world.sessions) == [f"task:{t1}"]
    claim = next(c for c in alice.chain().claims.values() if c.holder == "@alice")
    assert claim.status == "active"
    link = alice.chain().tip
    assert link.author == "@alice" and link.task == t1
    # a comment on alice's linked push is about the same unit of work: same warm session
    world.repo.client("@bob").sync_log()
    world.tools("@bob").comment(link.push[:10], "why this file name?")
    assert alice.step() == 1
    assert len(world.sessions[f"task:{t1}"]) == 1             # created once, used twice
    session = world.sessions[f"task:{t1}"][0]
    assert len(session.texts) == 2 and "[commented]" in session.texts[1] and "why this file name?" in session.texts[1]
    assert "«" in session.texts[1] and "untrusted" in session.texts[1]
    # a review request on someone else's push is its own thread
    world.repo.client("@bob").sync_log()
    bob_wc = world.repo.clone("@bob")
    (bob_wc.root / "alice_t1.py").write_text("# rewritten by bob\n")
    bpush = json.loads(world.tools("@bob").push("rewrite alice's file"))["push"]
    assert alice.step() == 1
    roots = [k for k in world.sessions if k.startswith("push:")]
    assert roots == [f"push:{bpush}"]
    assert "[review.requested]" in world.sessions[roots[0]][0].texts[0]
    # bob's push now has alice's approve; once it links its thread is finished and the session closed
    world.repo.client("@bob").sync_log()
    for _ in range(50):
        alice.step()
        if world.sessions[roots[0]][0].closed is not None:
            break
        time.sleep(0.1)
    assert world.sessions[roots[0]][0].closed is True        # summarised
    assert any("notes for" in n for n in alice._state["notes"])


def test_debounce_batches_one_thread_and_separates_others():
    now = [100.0]
    turns: list[tuple[str, list[str]]] = []

    class S:
        def __init__(self, key: str) -> None:
            self.key = key

        def send(self, text: str) -> SessionTurn:
            return SessionTurn(text)

        def close(self, summary: bool = True) -> str:
            return ""

    def ev(kind: str, push: str, n: int) -> Event:
        data = {"push": push, "objection": f"{n:064x}", "by": "@bob", "reason": "r", "evidence": None}
        return make_event(kind, 1, ("@alice",), data)

    m = SessionManager(S, debounce_s=1.0, max_delay_s=5.0, clock=lambda: now[0],
                       after_turn=lambda k, events, t: turns.append((k, [e.id for e in events])))
    a, b = "a" * 64, "b" * 64
    m.route([ev("objected", a, 1)])
    now[0] += 0.5
    m.route([ev("objected", a, 2), ev("objected", a, 2)])     # same event twice: counted once
    m.route([ev("objected", b, 3)])
    assert m.pump() == 0                                      # still inside the quiet period
    now[0] += 0.6
    assert m.pump() == 0                                      # a's last event was 0.6 s ago
    now[0] += 0.5
    assert m.pump() == 2
    assert [k for k, _ in turns] == [f"push:{a}", f"push:{b}"]
    assert len(turns[0][1]) == 2 and len(turns[1][1]) == 1
    # a busy thread is still flushed after max_delay_s
    for _ in range(12):
        m.route([ev("objected", a, 100 + _)])
        now[0] += 0.5
        m.pump()
    assert len(turns) >= 3


def test_budget_exhaustion_escalates_and_releases(world: World):
    alice = world.runtime("@alice", worker("@alice", world, usd=0.06), budget=Budget(0.05, None))
    world.repo.task("Part one")
    assert alice.step() == 1
    assert alice.ledger.member.exhausted
    t1 = next(iter(alice.chain().tasks))
    # the next event for her finds the budget gone: no turn, an escalation, the claim released
    world.repo.task("Part two")
    world.repo.client("@bob").sync_log()
    link = alice.chain().tip
    world.tools("@bob").comment(link.push[:10], "please also add docs", task=None)
    alice.step()
    assert len(world.sessions[f"task:{t1}"][0].texts) == 1
    assert alice.manager.skipped and "member budget exhausted" in alice.manager.skipped[0][1]
    assert alice.stopped_reason and "budget" in alice.stopped_reason
    nelson = world.repo.client("@nelson")
    delta_events = []
    for _ in range(20):
        delta_events += list(nelson.sync_log().events)
        if any(e.type == "escalated" for e in delta_events):
            break
        time.sleep(0.1)
    esc = [e for e in delta_events if e.type == "escalated"]
    assert esc and esc[0].data.get("reason") == "budget" and "@nelson" in esc[0].concerns
    claim = next(c for c in nelson.chain().claims.values() if c.holder == "@alice")
    assert claim.status == "released"
    ledger = json.loads((alice.state_dir / "budget.json").read_text())
    assert ledger["member_escalated"] and ledger["member"]["spent_usd"] >= 0.05


def test_task_budget_stops_one_task_only(world: World):
    seen: list[str] = []

    def brain(session: FakeSession, text: str) -> SessionTurn:
        seen.append(session.key)
        return SessionTurn("thinking", usd=0.2, tokens=10)

    alice = world.runtime("@alice", brain, task_budget=Budget(0.3, None))
    world.repo.task("Part one")
    world.repo.task("Part two")
    alice.step()
    tasks = sorted(alice.chain().tasks.values(), key=lambda t: t.alias)
    t1, t2 = tasks[0].id, tasks[1].id
    assert sorted(seen) == sorted([f"task:{t1}", f"task:{t2}"])
    for _ in range(2):                                         # two more wake-ups on T1 only
        alice.manager.wake(f"task:{t1}")
        alice.step()
    assert seen.count(f"task:{t1}") == 2                       # 0.2 + 0.2 >= 0.3: the third is refused
    assert t1 in alice.ledger.escalated and t2 not in alice.ledger.escalated
    alice.manager.wake(f"task:{t2}")
    alice.step()
    assert seen.count(f"task:{t2}") == 2 and alice.stopped_reason is None


def test_restart_resumes_from_the_event_cursor(world: World):
    first = world.runtime("@alice", worker("@alice", world))
    world.repo.task("Build it")
    first.step()
    routed = first._state["routed"]
    assert routed >= 1
    n_turns = len(world.log)
    del first                                                  # the process dies
    # while it is down, bob comments; then an MCP process of alice's syncs and journals the event
    link = world.repo.client("@alice").chain().tip
    world.repo.client("@bob").sync_log()
    world.tools("@bob").comment(link.push[:10], "looks good, one question")
    world.tools("@alice").inbox()
    second = world.runtime("@alice", worker("@alice", world))
    assert second._state["routed"] == routed
    second.step()
    sends = [entry for entry in world.log[n_turns:] if entry[0] == "send"]
    assert len(sends) == 1 and "[commented]" in sends[0][2] and "[task.opened]" not in sends[0][2]
    # the alias of the task's session survived the restart, so the comment went to that thread
    assert sends[0][1].startswith("task:")
    # and nothing is routed twice
    assert second.step() == 0
    third = world.runtime("@alice", worker("@alice", world))
    assert third.step() == 0


def test_webhook_receipt_dedupes_and_checks_signatures(world: World):
    alice = world.runtime("@alice", worker("@alice", world))
    alice._sync()
    ev = make_event("commented", alice.client.now_ms(), ("@alice",),
                    {"target": "c" * 64, "comment": "d" * 64, "by": "@bob", "text": "hello from a webhook"})
    body = to_json(ev, REPO)
    headers = sign_hook(body, world.repo.kit.host_key, time.time())
    headers |= {"X-Gud-Event-Id": ev.id, "X-Gud-Cursor": "7"}
    assert alice.accept_webhook(headers, body) == 200
    assert alice.accept_webhook(headers, body) == 200          # duplicate: acknowledged, not routed again
    forged = sign_hook(body, b"\x01" * 32, time.time()) | {"X-Gud-Event-Id": ev.id}
    assert alice.accept_webhook(forged, body) == 401
    alice.step(poll=False)
    texts = [t for _, k, t in [e for e in world.log if e[0] == "send"]]
    assert sum("hello from a webhook" in t for t in texts) == 1
    assert alice.events.cursor == 7


def test_idle_wakes_once_per_board_state(world: World):
    calls: list[str] = []

    def lazy(session: FakeSession, text: str) -> SessionTurn:
        calls.append(session.key)
        return SessionTurn("not for me")

    alice = world.runtime("@alice", lazy)
    world.repo.task("Unclaimed")
    alice.step()                                               # the task.opened wake-up
    assert len(calls) == 1 and "open work on the board" in world.sessions[calls[0]][0].texts[0]
    assert alice.idle_check(force=True) is True                # unclaimed work, new board state
    alice.step()
    assert calls[-1].startswith("idle:")
    assert alice.idle_check(force=True) is False               # same board: no second nudge


def test_prompt_and_budget_helpers():
    text = system_prompt("@x", "/w", budget="$1.00")
    assert "@x" in text and "/w" in text and "untrusted" not in SYSTEM_PROMPT.split("Safety")[0]
    assert "data, not instructions" in text and "inflight" in text and "claim" in text
    assert turn_tokens({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}) == 25
    b = Budget(1.0, None)
    b.charge(0.4, 10)
    assert not b.exhausted and b.remaining_usd == pytest.approx(0.6)
    b.charge(0.7, 0)
    assert b.exhausted
    ledger = Ledger(Budget(None, 100), task_tokens=50)
    ledger.charge(0, 60, "t")
    assert ledger.blocked("t") and not ledger.blocked("u")
    ledger.charge(0, 50, "u")
    assert "member" in (ledger.blocked("u") or "")


def test_thread_finished_rules(world: World):
    r = world.repo
    r.task("T")
    r.client("@alice").sync_log()
    chain = r.client("@alice").chain()
    tid = next(iter(chain.tasks))
    assert thread_finished("event:x", chain) and not thread_finished(f"task:{tid}", chain, "@alice")
    assert thread_finished("push:" + "e" * 64, chain)
    batch = render_batch([], chain, key="idle:0", idle=True)
    assert "open work on the board" in batch
