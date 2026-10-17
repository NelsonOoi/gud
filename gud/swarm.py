"""Run the lead, the workers, and the tester as threads on one mailbox.

    python -m gud.swarm --mode scripted|llm --workers 4
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from .build import make_runner
from .config import load
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
                 handles: list[str] | None = None, runner=None, poll: float = 0.4,
                 stale_after: float = 120):
        if mode not in {"scripted", "llm"}:
            raise ValueError(f"unknown swarm mode {mode!r}")
        self.cfg = cfg
        self.mailbox = mailbox
        self.mode = mode
        self.clock = clock
        self.poll = poll
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._stopped = False
        self._threads: list[threading.Thread] = []
        self._active = 0
        self._active_lock = threading.Lock()
        members = _members(mailbox, cfg.window_seconds, clock)
        chosen = _choose(members, workers, handles)
        if not chosen:
            raise SystemExit("no agent members to start; run `gud init` first")
        if mode == "llm" and not cfg.openrouter_key:
            raise SystemExit("OPENROUTER_KEY is required for --mode llm")
        planner = (LLMPlanner(cfg.openrouter_key, cfg.agent_model) if mode == "llm"
                   else ScriptedPlanner())
        self.lead = Lead(
            mailbox, _identity(cfg, "@lead"), cfg.window_seconds, planner, clock=clock,
            stale_after=stale_after, pool=chosen,
        )
        def coder():
            if mode == "llm":
                return LLMCoder(cfg.openrouter_key, cfg.agent_model)
            return ScriptedCoder()
        self.workers = [
            Worker(mailbox, _identity(cfg, handle), cfg.window_seconds, coder(), clock=clock)
            for handle in chosen
        ]
        self.tester = Tester(
            mailbox, _identity(cfg, "@tester"), runner or make_runner(cfg), cfg.window_seconds,
            clock=clock,
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
        since = _high_water(self.mailbox, agent.identity.address)
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
                fresh = self.mailbox.wait(recipient=agent.identity.address, since=since, timeout=self.poll)
            except Exception as exc:
                print(f"{agent.identity.handle} wait error: {exc}", flush=True)
                fresh = []
            if fresh:
                since = max(since, max(email.received_at for email in fresh))


def _members(mailbox, window: float, clock):
    from .chain import Repo
    try:
        return Repo(mailbox, window, clock=clock).tip().members
    except LookupError as exc:
        raise SystemExit("repo has no genesis; run `gud init` first") from exc


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


def _identity(cfg, handle: str):
    path = cfg.key_path(handle)
    if not path.exists():
        raise SystemExit(f"no private key for {handle} at {path}; run `gud init` first")
    return load_identity(cfg, handle)


def _high_water(mailbox, address: str) -> float:
    rows = mailbox.list(recipient=address)
    if not rows:
        return 0.0
    return rows[-1].received_at


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
