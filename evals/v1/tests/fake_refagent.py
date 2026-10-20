"""A deterministic stand-in for `python -m gud.agent.runtime` (no model).

It takes the same arguments and runs the real `AgentRuntime` (event journal, routing, sessions, budget
ledger, turn records), but every session is a `FakeSession` that does what tests/fake_agent.py does in the
gud1-cc arm: claim T1 (first claimant wins), check inflight, write this agent's share of FAKE_FILES_DIR into
the working copy (plus, with FAKE_CONFLICT=1, a README.md line every agent adds at the same place), push, and
wait; when superseded, restore README.md, sync, re-apply and push again. It writes transcripts in the
runtime's format (`<state>/transcripts/<key>.jsonl`) and charges a fixed cost per turn.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fake_agent import my_files  # noqa: E402

TURN_USD = 0.004


class FakeSession:
    done: dict[str, bool] = {}

    def __init__(self, runtime, key: str):
        self.runtime = runtime
        self.key = key

    def _row(self, row: dict) -> None:
        path = self.runtime.state_dir / "transcripts" / f"{self.key.replace(':', '_')[:80]}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": int(time.time() * 1000), **row}) + "\n")

    def send(self, text: str):
        from gud.agent.sessions import SessionTurn
        from gud.mcp_v1 import GudTools

        rt = self.runtime
        me = rt.me
        if FakeSession.done.get(me):
            self._row({"type": "text", "text": "nothing to do"})
            self._row({"type": "result", "cost": TURN_USD, "turns": 1, "error": False, "session": self.key})
            return SessionTurn(text="nothing to do", usd=TURN_USD, tokens=100)
        tools = GudTools(rt.client, rt.wc.root, events=rt.journal, poll_s=0.3)
        cwd = rt.wc.root
        calls: dict[str, int] = {}

        def call(name: str, **kw) -> str:
            self._row({"type": "tool_use", "name": f"mcp__gud__{name}", "input": kw})
            result = getattr(tools, name)(**kw)
            self._row({"type": "tool_result", "error": result.startswith("error"), "content": result})
            calls[f"mcp__gud__{name}"] = calls.get(f"mcp__gud__{name}", 0) + 1
            return result

        files = my_files(me)
        conflict = os.environ.get("FAKE_CONFLICT") == "1"
        call("tasks")
        call("claim", task="T1", files=sorted(files) + (["README.md"] if conflict else []))
        call("inflight")
        self._row({"type": "tool_use", "name": "Read", "input": {"file_path": str(cwd / "README.md")}})
        self._row({"type": "tool_result", "error": False, "content": (cwd / "README.md").read_text()})
        for rel, body in files.items():
            (cwd / rel).parent.mkdir(parents=True, exist_ok=True)
            (cwd / rel).write_text(body)
        if conflict:
            (cwd / "README.md").write_text((cwd / "README.md").read_text() + f"Owner note from {me}.\n")

        def redo() -> None:
            (cwd / "README.md").write_text(call("read", path="README.md", at="base"))
            call("sync")
            if conflict:
                (cwd / "README.md").write_text((cwd / "README.md").read_text() + f"Owner note from {me}.\n")

        def push() -> str:
            for _ in range(3):
                res = call("push", intent=f"work from {me}", task="T1")
                if not res.startswith("error"):
                    return res.split()[1]
                redo()
            raise RuntimeError("push kept failing")

        pid = push()
        deadline = time.time() + float(os.environ.get("FAKE_DEADLINE", "60"))
        outcome = "gave up"
        while time.time() < deadline:
            call("wait", push=pid, timeout=3)
            chain = rt.client.chain()
            if any(l.push == pid for l in chain.links):
                outcome = f"linked {pid[:12]}"
                if any(c.holder == me and c.status == "active" for c in chain.claims.values()):
                    call("claim_update", claim="T1", done=True)  # the holder declares T1 done (SPEC §16.6)
                break
            prop = chain.push_status(pid)
            if prop is not None and prop.status in ("superseded", "stale", "conflict"):
                redo()
                pid = push()
        FakeSession.done[me] = True
        self._row({"type": "text", "text": outcome})
        self._row({"type": "result", "cost": TURN_USD, "turns": len(calls), "error": False, "session": self.key})
        return SessionTurn(text=outcome, usd=TURN_USD, tokens=1200, tool_calls=calls)

    def close(self, summary: bool = True) -> str:
        return ""


def main(argv=None) -> int:
    from gud.agent.budget import Budget
    from gud.agent.runtime import AgentRuntime
    from gud.mcp_v1 import MemberEvents, journal_deltas, open_member
    from gud.wc.copy import WorkingCopy

    ap = argparse.ArgumentParser()
    ap.add_argument("--handle")
    ap.add_argument("--home")
    ap.add_argument("--wc")
    ap.add_argument("--model", default="")
    ap.add_argument("--budget-usd", type=float, default=None)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--idle", type=float, default=300.0)
    ap.add_argument("--deny-path", action="append", default=[])
    args = ap.parse_args(argv)
    if os.environ.get("FAKE_AGENT_MODE") == "authfail":
        state = Path(args.home) / "members" / args.handle / "agent"
        state.mkdir(parents=True, exist_ok=True)
        (state / "turns.jsonl").write_text(json.dumps({"key": "idle:0", "is_error": True, "usd": 0, "tokens": 0,
                                                       "error": "Failed to authenticate: OAuth session expired",
                                                       "text": ""}) + "\n")
        time.sleep(600)
        return 1
    client, wc_root = open_member(args.home, args.handle, args.wc)
    journal_deltas(client, MemberEvents(client.home, client.handle))
    wc = WorkingCopy(wc_root, client)
    holder: dict = {}
    runtime = AgentRuntime(client, wc, model=args.model, budget=Budget(args.budget_usd, None),
                           session_factory=lambda key: FakeSession(holder["rt"], key), poll_s=args.poll,
                           idle_s=args.idle, deny_paths=args.deny_path, debounce_s=0.2)
    holder["rt"] = runtime
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    runtime.run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
