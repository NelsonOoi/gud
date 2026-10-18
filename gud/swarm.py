"""Run the lead, the workers, and the tester as threads on one mailbox.

    python -m gud.swarm --mode scripted|llm --workers 4
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from .bugs import LLMBugReviewer, bugs_address_for
from .build import make_runner
from .config import load
from .inbox import Gate
from .keys import load_identity
from .lead import LLMPlanner, Lead, ScriptedPlanner
from .mailbox import open_member_mailbox
from .tester import Tester
from .worker import LLMCoder, ScriptedCoder, Worker

# Subagents first, then the named agents, so a four-worker swarm is @sub-1..@sub-4
# and the demo's fifth assignment (the conflicting edit) is a different person.
ORDER = [
    "@sub-1", "@sub-2", "@sub-3", "@sub-4",
    "@auth-agent", "@billing-agent", "@api-agent",
]


class Swarm:
    """Lead + N workers + tester. `start`/`stop` for the demo; `run` blocks until Ctrl-C."""

    def __init__(self, cfg, mailbox, mode: str = "scripted", workers: int | None = 4, clock=time.time,
                 handles: list[str] | None = None, runner=None, poll: float | None = None,
                 stale_after: float = 120, genesis_timeout: float = 60):
        if mode not in {"scripted", "llm"}:
            raise ValueError(f"unknown swarm mode {mode!r}")
        self.cfg = cfg
        self.mailbox = mailbox
        self.mode = mode
        self.clock = clock
        remote = getattr(cfg, "mailbox", "fake") != "fake" or getattr(cfg, "host_url", None)
        # Real mail: long-poll the inbox for 2s, replay the repo every 3s while busy, 20s idle.
        self.poll = poll if poll is not None else (2.0 if remote else 0.4)
        busy_every, idle_every = (3.0, 20.0) if remote else (0.5, 5.0)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._stopped = False
        self._threads: list[threading.Thread] = []
        self._active = 0
        self._active_lock = threading.Lock()
        members = _members(mailbox, cfg.window_seconds, clock, genesis_timeout)
        chosen = _choose(members, workers, handles)
        if not chosen:
            raise SystemExit("no agent members to start; run `gud init` first")
        if mode == "llm" and not cfg.openrouter_key:
            raise SystemExit("OPENROUTER_KEY is required for --mode llm")
        planner = (LLMPlanner(cfg.openrouter_key, cfg.agent_model) if mode == "llm"
                   else ScriptedPlanner())

        def gate():
            return Gate(busy_every=busy_every, idle_every=idle_every)

        def coder():
            if mode == "llm":
                return LLMCoder(cfg.openrouter_key, cfg.agent_model)
            return ScriptedCoder()
        bugs = (getattr(cfg, "bugs_address", "") or "").strip() or bugs_address_for(mailbox.repo_address)
        self.lead = Lead(
            mailbox, _identity(cfg, "@lead", members["@lead"].address), cfg.window_seconds, planner,
            clock=clock, stale_after=stale_after, pool=chosen, gate=gate(),
            reviewer=coder() if mode == "llm" else None,
            bugs_address=bugs,
            bug_reviewer=LLMBugReviewer(cfg.openrouter_key, cfg.agent_model) if mode == "llm" else None,
        )
        self.workers = [
            Worker(mailbox, _identity(cfg, handle, members[handle].address), cfg.window_seconds,
                   coder(), clock=clock, gate=gate())
            for handle in chosen
        ]
        self.tester = Tester(
            mailbox, _identity(cfg, "@tester", members["@tester"].address), runner or make_runner(cfg),
            cfg.window_seconds, clock=clock, gate=gate(),
        )
        self.handles = chosen
        self._agents = [self.lead, *self.workers, self.tester]

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = []
        for agent in self._agents:
            thread = threading.Thread(target=self._loop, args=(agent,), name=agent.identity.handle,
                                      daemon=True)
            thread.start()
            self._threads.append(thread)
        print(f"swarm: {self.mode} lead + {', '.join(self.handles)} + tester", flush=True)

    def pause(self) -> None:
        """Stop calling step. In-flight steps finish; `pause_and_wait` blocks until they do."""
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def pause_and_wait(self, timeout: float = 5) -> None:
        self.pause()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._active_lock:
                if self._active == 0:
                    return
            time.sleep(0.05)

    def stop(self, timeout: float = 5) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        self._pause.clear()
        closer = getattr(self.tester, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception as exc:
                print(f"tester close: {exc}", flush=True)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def run(self) -> None:
        """Block until Ctrl-C, then stop the threads."""
        self.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nswarm: stopping", flush=True)
        finally:
            self.stop()

    def _loop(self, agent) -> None:
        """Step, then sleep on the agent's own inbox until mail for it arrives or `poll` passes."""
        while not self._stop.is_set():
            with self._active_lock:
                if self._pause.is_set() or self._stop.is_set():
                    paused = True
                else:
                    self._active += 1
                    paused = False
            if paused:
                self._stop.wait(0.05)
                continue
            try:
                agent.step()
            except Exception as exc:
                print(f"{agent.identity.handle} step error: {exc}", flush=True)
            finally:
                with self._active_lock:
                    self._active -= 1
            if self._stop.is_set():
                break
            try:
                agent.inbox.wait(self.poll)
            except Exception as exc:
                print(f"{agent.identity.handle} wait error: {exc}", flush=True)
                self._stop.wait(self.poll)


def _members(mailbox, window: float, clock, timeout: float = 60):
    """Members at the tip. Waits up to `timeout` for genesis: real mail arrives seconds after `gud init`."""
    from .chain import Repo
    deadline = time.monotonic() + timeout
    while True:
        try:
            return Repo(mailbox, window, clock=clock).tip().members
        except LookupError as exc:
            if time.monotonic() >= deadline:
                raise SystemExit("repo has no genesis; run `gud init` first") from exc
            time.sleep(1.0)


def _choose(members: dict, workers: int | None, handles: list[str] | None) -> list[str]:
    if handles:
        return [handle for handle in handles if handle in members and members[handle].role == "agent"]
    present = [handle for handle in ORDER if handle in members and members[handle].role == "agent"]
    for handle, member in sorted(members.items()):
        if member.role == "agent" and handle not in present:
            present.append(handle)
    if workers is None or workers <= 0:
        return present
    return present[:workers]


def _identity(cfg, handle: str, address: str | None = None):
    path = cfg.key_path(handle)
    if not path.exists():
        raise SystemExit(f"no private key for {handle} at {path}; run `gud init` first")
    return load_identity(cfg, handle, address=address)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gud.swarm")
    parser.add_argument("--mode", choices=("scripted", "llm"), default="scripted")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    cfg = load()
    Swarm(cfg, open_member_mailbox(cfg), mode=args.mode, workers=args.workers).run()


if __name__ == "__main__":
    main()
    sys.exit(0)
