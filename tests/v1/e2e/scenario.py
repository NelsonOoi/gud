"""The v1 end-to-end scenario, run as separate processes over one transport.

Processes: `gud host serve` (host_v1: intake + dispatcher + §21 mirror) and `gud tester` run as
long-lived subprocesses; every member action is one `gud1 --json` subprocess with the member's
own GUD_HOME and mail account. The test process only plays the agent-side webhook receiver of
one member (a tiny HTTP server, verifying the host's signatures with `EventClient`), reads
another member's SSE stream, and writes files into working copies.

Members: @human (human, genesis author and requester), @alice and @bob (agents, join and are
admitted), @box1 (tester). The flow (each step asserts what SPEC-v1 says should happen):

 1. host init + serve; @human inits from a seed directory (`--host-url`); the others join and
    @human admits them; @alice and @bob clone; @box1 runs `gud tester`; @bob registers a webhook,
    @human SSE.
 2. @human pushes a new file: no owner is asked, it links on consent alone (default policy).
 3. @alice and @bob push edits to that file concurrently: both wait for its owner; the first
    received links when the window closes, the other is superseded (file-mode carry); the loser
    syncs and pushes again, @human approves, it links.
 4. @human pushes a change to @bob's line: @bob's webhook gets `review.requested`; @bob objects
    with a failing test (`object --test`); the tester confirms it; @human's SSE stream gets
    `objected`; @human revises with the test in the tree, the tester clears it, the revision links.
 5. @human opens a task with acceptance criteria; @bob claims it. @alice breaks a tested line
    (@human approves): the tester's tip run fails, `tip.red`, bisect names the link, a fix task
    is opened naming it. @bob's task work links while red: work done, not done. @alice claims the
    fix task and fixes it: `tip.green`, the fix task is done, and only then the tester's
    acceptance run completes the task.
 5b. @alice runs the reference agent (scripted session backend): `task.opened` reaches it by
    webhook, it claims the task and pushes through the MCP tools; @human accepts.
 6. `verify --from-genesis` on every member, the same tip and blame everywhere; `mirror git` to a
    bare repo and `git fsck`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from gud.events.dispatch import EventClient
from tests.v1.events.harness import Receiver
from tests.v1.procs import Proc, gud, serve, wait_for

PY = sys.executable


@dataclass
class Transport:
    """How members and the host reach mail: CLI `--transport/--set` arguments per account."""
    name: str
    repo: str
    address: Callable[[str], str]
    settings: Callable[[str], dict[str, str]]       # account user -> transport settings
    window: str = "8s"
    grace: str = "2s"
    slow: float = 1.0                               # timeout multiplier
    host_user: str = "repo"
    count_sent: Callable[[], int] | None = None     # emails sent so far (where measurable)
    env: dict[str, str] = field(default_factory=dict)   # extra environment for every process
    on_tip: str = "links=1 time=10m"                    # the seed's .gud/test on-tip cadence

    def args(self, user: str) -> list[str]:
        out = ["--transport", self.name]
        for k, v in self.settings(user).items():
            out += ["--set", f"{k}={v}"]
        return out


@dataclass
class Member:
    handle: str
    role: str
    home: Path
    wc: Path | None = None


@dataclass
class Scenario:
    tmp: Path
    t: Transport
    members: dict[str, Member] = field(default_factory=dict)
    procs: list[Proc] = field(default_factory=list)
    url: str = ""
    host_key: str = ""
    receiver: Receiver | None = None
    timings: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    sse: dict[str, list] = field(default_factory=dict)

    # ------------------------------------------------------------ plumbing

    def run(self, who: str, *args: str, code: int | None = 0, cwd: Path | None = None, timeout: float = 240) -> dict:
        m = self.members[who]
        return gud(m.home, *args, cwd=cwd or m.wc or m.home, code=code, handle=m.handle, timeout=timeout,
                   extra_env=self.t.env)

    def gud(self, home: Path, *args: str, **kw) -> dict:
        return gud(home, *args, extra_env=self.t.env, **kw)

    def wait(self, pred, what: str, timeout: float = 60) -> object:
        return wait_for(pred, timeout=timeout * self.t.slow, what=what, interval=0.5)

    def status(self, who: str, push: str) -> str:
        shown = self.run(who, "show", push)
        if shown["type"] == "link":
            return "linked"
        return shown.get("proposal", {}).get("status", shown["type"])

    def task(self, who: str, alias_or_id: str) -> dict:
        return self.run(who, "task", "show", alias_or_id)["task"]

    def close(self) -> None:
        for p in reversed(self.procs):
            p.stop()
        if self.receiver is not None:
            self.receiver.close()

    def logs(self) -> str:
        return "\n".join(f"--- {p.log.name}\n{p.output()[-4000:]}" for p in self.procs)

    # ------------------------------------------------------------ 1. setup

    def seed(self, root: Path) -> None:
        root.mkdir(parents=True)
        (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "conftest.py").write_text("")
        (root / "tests").mkdir()
        (root / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n"
                                                     "    assert add(2, 3) == 5\n")
        (root / ".gud").mkdir()
        (root / ".gud" / "test").write_text(f"command {PY} -m pytest -q -p no:cacheprovider\ntimeout 2m\n"
                                            f"on-tip {self.t.on_tip}\n")
        (root / ".gud" / "config").write_text(f"window {self.t.window}\ngrace {self.t.grace}\nskew 120s\n"
                                              f"fix-offer 5m\n")

    def setup(self) -> None:
        t = self.t
        hoster = self.tmp / "homes" / "hoster"
        host_dir = self.tmp / "hostdir"
        made = self.gud(hoster, "host", "init", "--repo", t.repo, *t.args(t.host_user), "--dir", str(host_dir),
                        handle="@hoster")
        self.host_key = made["host_key"]
        server, self.url = serve(hoster, host_dir, self.tmp / "logs" / "host.log", extra_env=t.env)
        self.procs.append(server)

        for user, role in (("human", "human"), ("alice", "agent"), ("bob", "agent"), ("box1", "tester")):
            self.members[user] = Member(f"@{user}", role, self.tmp / "homes" / user)
        human = self.members["human"]
        self.gud(human.home, "keys", "new", "--address", t.address("human"), *t.args("human"), handle="@human")
        roster = self.tmp / "roster"
        roster.write_text(f"@human human {t.address('human')}\n@host host {t.repo}\n")
        human.wc = self.tmp / "wc-human"
        self.seed(human.wc)
        start = time.monotonic()
        out = self.run("human", "init", "--repo", t.repo, "--member-file", str(roster), "--host-url", self.url,
                       "-m", "seed")
        self.timings["genesis_s"] = time.monotonic() - start
        assert out["dir"] == str(human.wc.resolve()) and out["files"] == 8, out      # incl. .gudignore

        for user in ("alice", "bob", "box1"):
            m = self.members[user]
            j = self.gud(m.home, "join", self.url, "--handle", m.handle, "--address", t.address(user), "--role", m.role,
                    "--sponsor", "@human", "--reason", f"{user} wants to help", *t.args(user))
            self.wait(lambda: self.run("human", "show", j["join"][:16], code=None).get("type") == "message",
                      f"join of {m.handle} logged")
            admit = self.run("human", "admit", j["join"][:16])
            self.wait(lambda: self.status("human", admit["push"]) == "linked", f"admission of {m.handle}")
        for user in ("alice", "bob"):
            m = self.members[user]
            m.wc = self.tmp / f"wc-{user}"
            c = self.gud(m.home, "clone", self.url, str(m.wc), handle=m.handle, cwd=self.tmp)
            assert c["base"] == c["tip"], c
        # agent-side receivers: @bob by webhook (verified with the host key), @human by SSE
        self.receiver = Receiver(self.tmp / "bob-events.json", lambda: [self.host_key], time.time)
        self.receiver.client.repo = t.repo                  # events name the repo they come from
        self.run("bob", "delivery", "--webhook", self.receiver.url)
        self.run("human", "delivery", "--sse")
        self.run("alice", "delivery", "--sse")
        self.run("box1", "delivery", "--sse")
        tester = Proc(self.members["box1"].home, "tester", "--interval", "0.5", log=self.tmp / "logs" / "tester.log",
                      handle="@box1", extra_env=t.env)
        self.procs.append(tester)

    # ------------------------------------------------------------ helpers on working copies

    def write(self, who: str, path: str, text: str) -> None:
        p = self.members[who].wc / path  # type: ignore[operator]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def sync(self, who: str) -> dict:
        out = self.run(who, "sync")
        assert out["ok"], out
        return out

    def push(self, who: str, message: str, *extra: str) -> dict:
        start = time.monotonic()
        out = self.run(who, "push", "-m", message, *extra)
        out["_sent_s"] = time.monotonic() - start
        return out

    def sse_events(self, who: str) -> list:
        """The member's events so far over SSE (`GET /api/v1/events?follow=0`, signed)."""
        m = self.members[who]
        from gud.wc.remote import load_key

        class Mirror:
            host_url = self.url
            private_raw = load_key(m.home, m.handle)
            http = httpx.Client(timeout=30)

        client = EventClient(self.tmp / f"sse-{who}.json", lambda: [self.host_key])   # resumes at its cursor
        got = self.sse.setdefault(who, [])
        got.extend(client.stream(Mirror, m.handle, follow=False))
        return got

    # ------------------------------------------------------------ 2. a new file links on consent alone

    def step_new_file(self) -> str:
        self.sync("human")
        self.write("human", "util.py", "def double(x):\n    return 2 * x\n\n\ndef triple(x):\n    return 3 * x\n")
        start = time.monotonic()
        p = self.push("human", "add util")
        self.wait(lambda: self.status("human", p["push"]) == "linked", "util.py links on consent alone", 30)
        self.timings["new_file_link_s"] = time.monotonic() - start
        assert p["forecast"]["askers"] == [], p
        return p["push"]

    # ------------------------------------------------------------ 3. a race on one file

    def step_race(self) -> tuple[str, str]:
        for who in ("alice", "bob"):
            self.sync(who)
        self.write("alice", "util.py", "def double(x):\n    return x + x\n\n\ndef triple(x):\n    return 3 * x\n")
        self.write("bob", "util.py", "def double(x):\n    return 2 * x\n\n\ndef triple(x):\n    return x * 3\n")
        results: dict[str, dict] = {}

        def go(who: str) -> None:
            results[who] = self.push(who, f"{who} edits util")

        threads = [threading.Thread(target=go, args=(w,)) for w in ("alice", "bob")]
        start = time.monotonic()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        for who in ("alice", "bob"):
            assert results[who]["forecast"]["askers"] == ["@human"], results[who]

        def settled():
            s = {w: self.status("human", results[w]["push"]) for w in ("alice", "bob")}
            return s if sorted(s.values()) == ["linked", "superseded"] else None

        final = self.wait(settled, "one push links, the other is superseded", 60)
        self.timings["race_link_s"] = time.monotonic() - start
        winner = next(w for w, s in final.items() if s == "linked")      # type: ignore[union-attr]
        loser = "bob" if winner == "alice" else "alice"
        self.notes.append(f"race: {winner} linked, {loser} superseded")
        # the loser merges the winner's edit (different lines) and pushes again
        merged = self.sync(loser)
        assert merged["written"] == ["util.py"], merged
        text = (self.members[loser].wc / "util.py").read_text()       # type: ignore[operator]
        assert "x + x" in text and "x * 3" in text, text
        again = self.push(loser, f"{loser} edits util (again)")
        review = self.run("human", "review")
        assert any(i.get("push") == again["push"] for i in review["items"]), review
        self.run("human", "approve", again["push"][:16], "-m", "ok")
        self.wait(lambda: self.status("human", again["push"]) == "linked", "the re-push links", 30)
        return winner, again["push"]

    # ------------------------------------------------------------ 4. objection with a failing test

    def step_objection(self) -> str:
        self.sync("human")
        self.write("human", "util.py", "def double(x):\n    return x + x\n\n\ndef triple(x):\n    return x * 4\n")
        start = time.monotonic()
        p = self.push("human", "triple: faster")
        assert p["forecast"]["askers"] == ["@bob"], p
        rr = self.wait(lambda: [e for e in self.receiver.of("review.requested") if e.data["push"] == p["push"]],
                       "@bob's webhook gets review.requested", 20)
        self.timings["review_requested_s"] = time.monotonic() - start
        test = "from util import triple\n\n\ndef test_triple():\n    assert triple(2) == 6\n"
        self.write("bob", "tests/test_util.py", test)
        obj = self.run("bob", "object", p["push"][:16], "-m", "triple(2) must be 6", "--test", "tests/test_util.py")
        assert obj["logged"], obj
        self.timings["objection_sent_s"] = time.monotonic() - start
        self.wait(lambda: any(o["status"] == "confirmed" for o in
                              self.run("human", "show", p["push"]).get("proposal", {}).get("objections", [])),
                  "the tester confirms the evidence", 60)
        objected = self.wait(lambda: [e for e in self.sse_events("human") if e.type == "objected"
                                      and e.data["push"] == p["push"]], "@human's SSE stream gets objected", 20)
        assert objected[0].data["by"] == "@bob"                        # type: ignore[index]
        # the revision fixes triple and carries the test into the tree
        self.write("human", "util.py", "def double(x):\n    return x + x\n\n\ndef triple(x):\n"
                                      "    \"\"\"Three times x.\"\"\"\n    return x * 3\n")
        self.write("human", "tests/test_util.py", test)
        r = self.push("human", "triple: keep it right, with bob's test", "--revises", p["push"])
        self.wait(lambda: self.status("human", r["push"]) == "linked", "the revision links", 90)
        self.timings["objection_to_link_s"] = time.monotonic() - start
        assert self.status("human", p["push"]) != "linked" and rr
        return r["push"]

    # ------------------------------------------------------------ 5. red tip, fix task, acceptance

    def step_red_tip(self) -> None:
        task = self.run("human", "task", "new", "multiply", "-m", "add mul()", "--accept", "tests/test_mul.py")
        self.wait(lambda: self.run("bob", "show", task["id"][:16], code=None).get("type") == "task", "task logged")
        tid = task["id"]
        self.run("bob", "claim", tid[:16], "--files", "mul.py,tests/test_mul.py")
        self.wait(lambda: self.task("bob", tid)["status"] == "claimed", "@bob's claim wins")
        # @alice breaks add(): a tested line of @human's, who approves
        self.sync("alice")
        self.write("alice", "calc.py", "def add(a, b):\n    return a - b\n")
        bad = self.push("alice", "add: subtract")
        self.run("human", "approve", bad["push"][:16])
        self.wait(lambda: self.status("human", bad["push"]) == "linked", "the breaking push links", 30)
        start = time.monotonic()

        def fix_task():
            for t in self.run("alice", "tasks")["tasks"]:
                if t["kind"] == "fix" and t["cited_link"]:
                    return t
            return None

        fix = self.wait(fix_task, "a fix task opens", 90)
        self.timings["red_to_fix_task_s"] = time.monotonic() - start
        log = self.run("alice", "log", "-n", "1")["links"][0]
        assert fix["cited_link"] == log["state"] and fix["offered_to"] == "@alice", (fix, log)    # type: ignore[index]
        # @bob's task work links while red: work done, not done
        self.sync("bob")
        self.write("bob", "mul.py", "def mul(a, b):\n    return a * b\n")
        self.write("bob", "tests/test_mul.py", "from mul import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
        work = self.push("bob", "mul", "--task", tid[:16])
        self.wait(lambda: self.status("bob", work["push"]) == "linked", "the task work links", 30)
        self.wait(lambda: self.task("bob", tid)["status"] == "work_done", "the task's work is done", 30)
        time.sleep(3 * self.t.slow)                                     # the tester has had time to act
        assert self.task("bob", tid)["status"] == "work_done", "a task must not be done while red"
        # @alice claims the fix task (offered to her) and fixes add()
        self.run("alice", "claim", fix["id"][:16], "--files", "calc.py")              # type: ignore[index]
        self.sync("alice")
        self.write("alice", "calc.py", "def add(a, b):\n    return a + b\n")
        f = self.push("alice", "add: fix", "--task", fix["id"][:16])                  # type: ignore[index]
        self.wait(lambda: self.status("alice", f["push"]) == "linked", "the fix links", 30)
        self.wait(lambda: self.task("alice", fix["id"])["status"] == "done", "the fix task is done (green)", 90)  # type: ignore[index]
        self.timings["red_to_green_s"] = time.monotonic() - start
        self.wait(lambda: self.task("human", tid)["status"] == "done", "the task is done after its accept run", 90)
        done = self.task("human", tid)
        assert done["done_at"] >= self.task("alice", fix["id"])["done_at"], done         # type: ignore[index]

    # ------------------------------------------------------------ 5b. the reference agent

    def step_agent(self) -> None:
        """@alice runs the reference agent (gud.agent.runtime) with a scripted session backend:
        it registers a webhook with the host, gets `task.opened` for a task @human opens, claims it
        through the MCP tools and pushes; @human accepts, the task is done."""
        from gud.agent.budget import Budget
        from gud.agent.runtime import AgentRuntime
        from gud.agent.sessions import SessionTurn
        from gud.wc.copy import WorkingCopy
        from gud.wc.remote import client_from_home

        m = self.members["alice"]
        self.sync("alice")
        client = client_from_home(m.home, m.handle)
        texts: list[str] = []
        agent_log: list[str] = []
        holder: dict = {}

        class Scripted:
            def __init__(self, key: str) -> None:
                self.key = key

            def send(self, text: str) -> SessionTurn:
                texts.append(text)
                tools = holder["rt"].tools
                for t in client.chain().tasks.values():
                    if t.title == "docs" and t.status == "open":
                        claim = json.loads(tools.claim(t.alias, ["DOCS.md"]))
                        if claim.get("error"):
                            break
                        (m.wc / "DOCS.md").write_text("# docs\n")             # type: ignore[operator]
                        out = json.loads(tools.push("write DOCS.md", task=t.alias))
                        return SessionTurn(text=f"pushed {out.get('push', '')[:12]}", usd=0.0, tokens=0,
                                           session_id=self.key)
                return SessionTurn(text="nothing to do", usd=0.0, tokens=0, session_id=self.key)

            def close(self, summary: bool = True) -> str:
                return ""

        rt = AgentRuntime(client, WorkingCopy(m.wc, client), budget=Budget(None, None),  # type: ignore[arg-type]
                          session_factory=Scripted, debounce_s=0.2, poll_s=1.0, idle_s=3600,
                          listen="127.0.0.1:0", log=agent_log.append)
        holder["rt"] = rt
        stop = threading.Event()
        runner = threading.Thread(target=rt.run, args=(stop,), daemon=True)
        runner.start()
        try:
            time.sleep(1.0)                                              # the webhook is registered
            start = time.monotonic()
            task = self.run("human", "task", "new", "docs", "-m", "write DOCS.md")
            try:
                self.wait(lambda: self.task("human", task["id"])["status"] == "work_done",
                          "the agent claims the task and its push links", 60)
            except AssertionError:
                (self.tmp / "logs" / "agent.json").write_text(json.dumps({"log": agent_log, "turns": texts}, indent=1))
                raise
            self.timings["agent_task_to_link_s"] = time.monotonic() - start
            assert any("task.opened" in t for t in texts), texts
            self.run("human", "approve", task["id"][:16], "-m", "thanks")
            self.wait(lambda: self.task("human", task["id"])["status"] == "done", "requester accepts", 30)
        finally:
            stop.set()
            runner.join(timeout=20)

    # ------------------------------------------------------------ 6. verify, mirror

    def step_verify_and_mirror(self) -> dict:
        tips, blames = {}, {}
        for who in ("human", "alice", "bob", "box1"):
            v = self.run(who, "verify", "--from-genesis", cwd=self.members[who].home)
            assert v["ok"] and v["differences"] == [], (who, v)
            tips[who] = self.run(who, "log", "-n", "1", cwd=self.members[who].home)["links"][0]["state"]
            blames[who] = [self.run(who, "blame", p, cwd=self.members[who].home)["lines"]
                           for p in ("calc.py", "util.py", "mul.py")]
        assert len(set(tips.values())) == 1, tips
        assert all(b == blames["human"] for b in blames.values()), blames
        out: dict = {"tip": tips["human"]}
        if shutil.which("git"):
            bare = self.tmp / "mirror.git"
            subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
            m = self.run("human", "mirror", "git", str(bare))
            links = self.run("human", "log", "-n", "1000")["links"]
            assert m["pushed"] == len(links), (m, len(links))
            subprocess.run(["git", "-C", str(bare), "fsck", "--strict"], check=True, capture_output=True)
            count = subprocess.run(["git", "-C", str(bare), "rev-list", "--count", "gud"], check=True,
                                   capture_output=True, text=True).stdout.strip()
            assert int(count) == len(links)
            out["mirrored"] = len(links)
        return out

    def run_all(self) -> dict:
        try:
            self.setup()
            self.step_new_file()
            self.step_race()
            self.step_objection()
            self.step_red_tip()
            self.step_agent()
            result = self.step_verify_and_mirror()
        except BaseException:
            sys.stderr.write(self.logs())
            raise
        result.update(self.timings)
        result["notes"] = self.notes
        return result


def wait_link(sc: Scenario, who: str, push: str, timeout: float = 60) -> None:
    sc.wait(lambda: sc.status(who, push) == "linked", f"{push[:12]} links", timeout)


def smoke_flow(sc: Scenario) -> None:
    """The reduced flow for live providers (few emails): setup; a new file; an approved edit of
    another member's line; another new file; the objection with a failing test and its revision;
    verify everywhere."""
    sc.setup()
    sc.step_new_file()
    sc.sync("bob")
    sc.write("bob", "util.py", "def double(x):\n    return 2 * x\n\n\ndef triple(x):\n    return x * 3\n")
    start = time.monotonic()
    p = sc.push("bob", "bob edits util")
    assert p["forecast"]["askers"] == ["@human"], p
    sc.run("human", "approve", p["push"][:16])
    sc.wait(lambda: sc.status("human", p["push"]) == "linked", "the approved push links", 90)
    sc.timings["approved_push_link_s"] = time.monotonic() - start
    sc.sync("alice")
    sc.write("alice", "notes.txt", "alice's notes\n")
    start = time.monotonic()
    a = sc.push("alice", "notes")
    sc.wait(lambda: sc.status("alice", a["push"]) == "linked", "a new file links", 60)
    sc.timings["consent_only_link_s"] = time.monotonic() - start
    sc.step_objection()
    tips = set()
    for who in ("human", "alice", "bob", "box1"):
        v = sc.run(who, "verify", "--from-genesis", cwd=sc.members[who].home)
        assert v["ok"], (who, v)
        tips.add(sc.run(who, "log", "-n", "1", cwd=sc.members[who].home)["links"][0]["state"])
    assert len(tips) == 1, tips

