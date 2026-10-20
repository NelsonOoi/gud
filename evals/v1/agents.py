"""Launch agents concurrently, stream their stream-json output, and enforce budget and time.

Each agent is one process (``claude -p ... --output-format stream-json`` in a live run, a fake agent
in the unit tests). A reader thread appends every stdout line to the agent's ``stream.jsonl`` and
feeds ``StreamStats``. The supervisor loop kills:

- one agent whose reported cost reaches its per-agent budget (``budget / N``),
- every agent once the run's total reported cost reaches the run budget,
- every agent at the wall-clock deadline,
- every agent when one reports an authentication failure (a harness problem, not an agent result),
- an agent whose required MCP server did not connect (a harness problem).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from streams import StreamStats

DEFAULT_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
ESCALATE = "ESCALATE:"


@dataclass
class AgentSpec:
    handle: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    log_dir: Path
    require_mcp: str | None = None   # MCP server name that must report "connected"


@dataclass
class AgentRun:
    spec: AgentSpec
    proc: subprocess.Popen | None = None
    stats: StreamStats = field(default_factory=StreamStats)
    started: float = 0.0
    ended: float | None = None
    killed: str | None = None
    exit_code: int | None = None
    stderr_tail: str = ""
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        self.spec.log_dir.mkdir(parents=True, exist_ok=True)
        self.started = time.time()
        self.proc = subprocess.Popen(
            self.spec.argv, cwd=self.spec.cwd, env=self.spec.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            start_new_session=True,
        )
        self._thread = threading.Thread(target=self._read, name=f"agent-{self.spec.handle}", daemon=True)
        self._thread.start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        log = (self.spec.log_dir / "stream.jsonl").open("w")
        try:
            for line in self.proc.stdout:
                log.write(line)
                log.flush()
                with self._lock:
                    self.stats.feed_line(line)
        finally:
            log.close()

    def _read_err(self) -> None:
        path = self.spec.log_dir / "stderr.log"
        with path.open("w") as fh:
            for line in self.proc.stderr:
                fh.write(line)
                fh.flush()
                self.stderr_tail = (self.stderr_tail + line)[-4000:]

    def poll(self) -> bool:
        """True while running."""
        if self.proc is None:
            return False
        code = self.proc.poll()
        if code is None:
            return True
        if self.ended is None:
            if self._thread is not None:
                self._thread.join(timeout=10)
            self.ended = time.time()
            self.exit_code = code
        return False

    def cost(self) -> float:
        with self._lock:
            return self.stats.cost()

    def kill(self, reason: str) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        if self.killed is None:
            self.killed = reason
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                self.proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue

    def summary(self) -> dict:
        with self._lock:
            stats = self.stats
            final = stats.final_text() or ""
            return {
                "handle": self.spec.handle,
                "exit_code": self.exit_code,
                "killed": self.killed,
                "wall_s": round((self.ended or time.time()) - self.started, 1) if self.started else None,
                "turns": stats.turns(),
                "tokens": stats.tokens(),
                "cost_usd": round(stats.cost(), 6),
                "cost_source": stats.cost_source(),
                "model": stats.model,
                "result_is_error": None if stats.result is None else bool(stats.result.get("is_error")),
                "terminal_reason": None if stats.result is None else stats.result.get("terminal_reason"),
                "mcp_servers": stats.mcp_servers,
                "tool_calls": len(stats.tool_calls),
                "tool_counts": _counts(row["name"] for row in stats.tool_calls),
                "escalated": ESCALATE in final,
                "escalation": _escalation(final),
                "final_text": final[-1500:],
                "api_errors": stats.api_errors[:5],
                "stderr_tail": self.stderr_tail[-800:],
            }


def _counts(names) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        out[name] = out.get(name, 0) + 1
    return dict(sorted(out.items()))


def _escalation(text: str) -> str | None:
    for line in text.splitlines():
        if ESCALATE in line:
            return line.split(ESCALATE, 1)[1].strip()[:500]
    return None


@dataclass
class Outcome:
    reason: str                     # "all_exited" | "timeout" | "budget" | "harness_error"
    harness_errors: list[str]
    wall_s: float
    total_cost_usd: float
    killed: dict[str, str]


def supervise(specs: list[AgentSpec], *, run_budget: float, agent_budget: float, timeout: float,
              poll: float = 0.5, on_tick=None, mcp_grace: float = 60.0) -> tuple[list[AgentRun], Outcome]:
    runs = [AgentRun(spec) for spec in specs]
    started = time.time()
    for run in runs:
        run.start()
    deadline = started + timeout
    reason = "all_exited"
    errors: list[str] = []
    try:
        while True:
            alive = [run for run in runs if run.poll()]
            total = sum(run.cost() for run in runs)
            for run in alive:
                with run._lock:
                    stats = run.stats
                    auth = stats.auth_failed()
                    status = stats.mcp_status(run.spec.require_mcp) if run.spec.require_mcp else "n/a"
                    seen_init = bool(stats.mcp_servers) or stats.model is not None
                if auth:
                    errors.append(f"{run.spec.handle}: authentication failed ({stats.api_errors[:1]})")
                    reason = "harness_error"
                elif run.spec.require_mcp and seen_init and status not in ("connected", "pending", None):
                    errors.append(f"{run.spec.handle}: MCP server {run.spec.require_mcp} is {status}")
                    reason = "harness_error"
                elif (run.spec.require_mcp and not seen_init and time.time() - run.started > mcp_grace
                      and stats.events == 0):
                    errors.append(f"{run.spec.handle}: no init event after {mcp_grace:.0f}s")
                    reason = "harness_error"
                if reason == "harness_error":
                    break
                if run.cost() >= agent_budget:
                    run.kill("agent_budget")
            if reason == "harness_error":
                for run in runs:
                    run.kill("harness_error")
                break
            if total >= run_budget:
                for run in runs:
                    run.kill("run_budget")
                reason = "budget"
                break
            if time.time() >= deadline:
                for run in runs:
                    run.kill("timeout")
                reason = "timeout"
                break
            if not alive:
                break
            if on_tick is not None:
                on_tick(runs)
            time.sleep(poll)
    finally:
        for run in runs:
            run.kill("harness_exit")
            run.poll()
    # Auth failures can also arrive as the only event before a fast exit.
    for run in runs:
        if run.stats.auth_failed() and not any(run.spec.handle in e for e in errors):
            errors.append(f"{run.spec.handle}: authentication failed ({run.stats.api_errors[:1]})")
            reason = "harness_error"
    wall = time.time() - started
    return runs, Outcome(
        reason=reason, harness_errors=errors, wall_s=round(wall, 1),
        total_cost_usd=round(sum(run.cost() for run in runs), 6),
        killed={run.spec.handle: run.killed for run in runs if run.killed},
    )


def claude_argv(claude: str, prompt: str, *, model: str | None, agent_budget: float,
                mcp_config: Path | None, allow_mcp: str | None, deny_paths: list[Path],
                tools: tuple[str, ...] = DEFAULT_TOOLS) -> list[str]:
    """The headless Claude Code invocation. Identical across arms except the MCP config.

    - ``--tools`` limits the built-in tools to file editing, search and Bash (no subagents, web or
      scheduling), so both arms have the same tool set; the gud arm adds its MCP server.
    - ``--permission-mode dontAsk`` with ``--allowedTools`` pre-approves those tools; anything else is
      denied instead of prompting (there is nobody to answer).
    - ``--setting-sources ""`` and ``--strict-mcp-config`` keep the user's settings, hooks, plugins
      and MCP servers out of the eval. ``--disable-slash-commands`` turns off skills.
    - ``--max-budget-usd`` is Claude Code's own cap; the supervisor enforces the same number.
    """
    allowed = list(tools) + ([f"mcp__{allow_mcp}"] if allow_mcp else [])
    argv = [
        claude, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--tools", ",".join(tools),
        "--allowedTools", *allowed,
        "--permission-mode", "dontAsk",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--max-budget-usd", f"{agent_budget:.2f}",
    ]
    if model:
        argv += ["--model", model]
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config)]
    deny = []
    for path in deny_paths:
        for tool in ("Read", "Edit", "Write", "Glob", "Grep"):
            deny.append(f"{tool}(/{path}/**)")
    if deny:
        argv += ["--disallowedTools", *deny]
    return argv
