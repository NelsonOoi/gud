"""The gud v1 arms: a v1 repo in local host mode over a shared FakeMailbox root, a v1 tester process,
and either Claude Code sessions on the gud v1 MCP server (`gud1-cc`) or reference agents (`gud1-ref`).

Layout under the run's work root:

    mail/                    the FakeMailbox root (every address: repo@gud.local, a1@gud.local, ...)
    host/                    the LocalHost directory (host key, signed host log, raw mail); `local:<dir>`
    host-objects/            the host's object store
    homes/<a1>/              GUD_HOME of each member (key, transport.toml, remote.json, replay cache, objects)
    agents/<a1>/             the agent's v1 working copy (`WorkingCopy.clone`): its cwd
    mcp/<a1>.json            gud1-cc: the agent's --mcp-config (python -m gud.mcp_v1 --home --handle --wc)
    tester/                  the tester's state

Members: @a1..@aN (agent), @tester (tester), @nelson (human requester and sponsor; signs genesis and the
task, never acts afterwards), and the host line. There is no lead: the task (T1, the spec text, with the
acceptance check `python -m pytest -q`) goes on the board and the agents decide who does what with claims,
subtasks, comments and pushes.

Test policy (`.gud/test`), `--gud-test-policy`:

- `tip` (default): the PLAN's default shape, no push waits for a test; the tester tests the tip after every
  link (`on-tip links=1 time=2m`), so a failing link turns the chain red, opens a fix task and is bisected.
- `gate`: every push needs a passing `python -m pytest -q -x` before it links (`on-paths **`), like the git
  arm's merge gate.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from gud.core import envelope as E  # noqa: E402
from gud.core.types import LIVE_STATUSES, Record  # noqa: E402
from gud.gitbridge import DEFAULT_OWNERS, parse_roster  # noqa: E402
from gud.transport.fake import FakeMailbox  # noqa: E402
from gud.wc.copy import WorkingCopy, create_genesis  # noqa: E402
from gud.wc.remote import LocalHost, RepoClient, save_key, write_transport_toml  # noqa: E402

DOMAIN = "gud.local"
REPO_ADDR = f"repo@{DOMAIN}"
ACCEPT_CMD = "python -m pytest -q"
POLICIES = {
    "tip": "command python -m pytest -q -x\non-tip links=1 time=2m\ntimeout 3m\n",
    "gate": "command python -m pytest -q -x\non-paths **\non-tip off\ntimeout 3m\n",
}
IGNORE = ["__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".DS_Store"]


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("GUD_", "PYTEST_")) and k != "PYTHONPATH"}


class Gud1Arm:
    name = "gud1"
    metrics_key = "gud1"

    def __init__(self, root: Path, spec, handles: list[str], *, window: float = 20.0, python: str = sys.executable,
                 policy: str = "tip", grace: float = 5.0):
        if policy not in POLICIES:
            raise ValueError(f"unknown test policy {policy!r}")
        self.root = Path(root)
        self.spec = spec
        self.handles = list(handles)
        self.window = float(window)
        self.grace = float(grace)
        self.python = python
        self.policy = policy
        self.mail = self.root / "mail"
        self.host_dir = self.root / "host"
        self.host_url = f"local:{self.host_dir}"
        self.keys: dict[str, bytes] = {}
        self.homes: dict[str, Path] = {}
        self.tester: subprocess.Popen | None = None
        self.tester_log: Path | None = None
        self.task_id: str | None = None
        self.genesis: str | None = None
        self._t0 = 0.0
        self._nelson: RepoClient | None = None

    # -- setup -----------------------------------------------------------------------------

    def _home(self, handle: str) -> Path:
        home = self.root / "homes" / handle.lstrip("@")
        save_key(home, handle, self.keys[handle], overwrite=True)
        write_transport_toml(home / "members" / handle / "transport.toml", "fake",
                             {"root": str(self.mail), "address": f"{handle.lstrip('@')}@{DOMAIN}"})
        (home / "remote.json").write_text(json.dumps({"host_url": self.host_url, "repo": REPO_ADDR, "handle": handle}))
        return home

    def client(self, handle: str) -> RepoClient:
        return RepoClient(self.host_url, handle, self.keys[handle],
                          send_via=FakeMailbox(self.mail, f"{handle.lstrip('@')}@{DOMAIN}"),
                          repo_address=REPO_ADDR, home=self.homes[handle])

    @property
    def nelson(self) -> RepoClient:
        if self._nelson is None:
            self._nelson = self.client("@nelson")
        return self._nelson

    def config_bytes(self) -> bytes:
        w = int(self.window) if self.window.is_integer() else self.window
        g = int(self.grace) if self.grace.is_integer() else self.grace
        return f"window {w}s\ngrace {g}s\nlease 10m\nescalate 3m\nfix-offer 2m\n".encode()

    def setup(self) -> None:
        from specs import read_tree

        self.root.mkdir(parents=True, exist_ok=True)
        everyone = self.handles + ["@tester", "@nelson"]
        self.keys = {h: E.generate_key()[0] for h in everyone}
        host = LocalHost.create(self.host_dir, repo=REPO_ADDR, transport="fake", cfg={"root": str(self.mail)},
                                host_private=E.generate_key()[0], objects=self.root / "host-objects")
        roster = "\n".join(
            [f"{h} agent {h.lstrip('@')}@{DOMAIN} {E.public_of(self.keys[h])} @nelson" for h in self.handles]
            + [f"@tester tester tester@{DOMAIN} {E.public_of(self.keys['@tester'])} @nelson",
               f"@nelson human nelson@{DOMAIN} {E.public_of(self.keys['@nelson'])} -",
               f"@host host {REPO_ADDR}"]) + "\n"
        members = parse_roster(roster.encode(), host.host_key)
        seed = read_tree(self.spec.seed)
        files = {p: ("100644", t.encode()) for p, t in seed.items()}
        ignore = list(IGNORE)
        for line in seed.get(".gitignore", "").splitlines():
            if line.strip() and line.strip() not in ignore:
                ignore.append(line.strip())
        files.update({
            ".gud/members": ("100644", members), ".gud/config": ("100644", self.config_bytes()),
            ".gud/test": ("100644", POLICIES[self.policy].encode()), ".gud/owners": ("100644", DEFAULT_OWNERS),
            ".gudignore": ("100644", ("\n".join(ignore) + "\n").encode()),
        })
        self.homes = {h: self._home(h) for h in everyone}
        nelson = self.nelson
        nelson.sync_log()
        self.genesis = create_genesis(nelson, files, f"genesis: {self.spec.name} seed")
        deadline = time.time() + 30
        while time.time() < deadline:
            nelson.sync_log()
            if nelson.chain().tip is not None:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("genesis did not link")
        for handle in self.handles:
            WorkingCopy.clone(self.client(handle), self.workdir(handle, create=False))

    def start_services(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.tester_log = log_dir / "tester.log"
        env = {**_clean_env(), "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        argv = [self.python, str(HERE / "gud1_tester.py"), "--home", str(self.homes["@tester"]), "--handle", "@tester",
                "--host", self.host_url, "--repo", REPO_ADDR, "--mail", str(self.mail),
                "--state", str(self.root / "tester"), "--extra-path", str(Path(self.python).parent), "--poll", "2"]
        self.tester = subprocess.Popen(argv, cwd=self.root, env=env, stdout=self.tester_log.open("w"),
                                       stderr=subprocess.STDOUT, start_new_session=True)

    def mark_start(self, t0: float) -> None:
        self._t0 = t0

    def seed_task(self) -> None:
        nelson = self.nelson
        nelson.sync_log()
        records = (Record("text", (), self.task_text().encode()), Record("accept", (), ACCEPT_CMD.encode()))
        self.task_id = nelson.sign_and_send("task", {"Gud-Title": self.spec.title}, records, summary=self.spec.title)
        deadline = time.time() + 30
        while time.time() < deadline:
            nelson.sync_log()
            if self.task_id in nelson.chain().tasks:
                return
            time.sleep(0.2)
        raise RuntimeError("the task was not logged")

    def task_text(self) -> str:
        """The task on the board: the spec text plus the one ground rule every arm's prompt also states."""
        return (self.spec.text + "\n\nHidden acceptance tests will check the finished repository against this "
                "text, so follow it exactly.")

    def live_proposals(self) -> list:
        nelson = self.nelson
        nelson.sync_log()
        return [p for p in nelson.chain().proposals.values() if p.status in LIVE_STATUSES]

    def drain(self, max_s: float) -> float:
        """After the agents stop: let open consent windows close and the tester finish (no live proposals)."""
        t = time.time()
        deadline = t + max_s
        while time.time() < deadline:
            if not self.live_proposals():
                break
            time.sleep(2)
        return round(time.time() - t, 1)

    def stop_services(self) -> None:
        if self.tester is not None and self.tester.poll() is None:
            os.killpg(self.tester.pid, signal.SIGTERM)
            try:
                self.tester.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.tester.pid, signal.SIGKILL)

    # -- agents ----------------------------------------------------------------------------

    def workdir(self, handle: str, create: bool = True) -> Path:
        path = self.root / "agents" / handle.lstrip("@")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def agent_env(self, handle: str) -> dict[str, str]:
        return {"GUD_EVAL_HANDLE": handle}

    # -- results ---------------------------------------------------------------------------

    def final_tree(self) -> dict[str, str]:
        nelson = self.nelson
        nelson.sync_log()
        if nelson.chain().tip is None:
            return {}
        out = {}
        for path, (_mode, data) in nelson.reader().files().items():
            if path.startswith(".gud/"):
                continue
            out[path] = data.decode("utf-8", "replace")
        return out

    def metrics(self, wall_s: float, transcripts: dict[str, list[dict]]) -> dict:
        import analyze
        return analyze.gud1_metrics(self.nelson, t0=self._t0, wall_s=wall_s, genesis=self.genesis,
                                    task=self.task_id, handles=self.handles)

    def keep_artifacts(self, run_dir: Path) -> None:
        log = self.host_dir / "log.jsonl"
        if log.exists():
            shutil.copy(log, run_dir / "host-log.jsonl")
        mail = self.host_dir / "mail"
        if mail.exists():
            shutil.copytree(mail, run_dir / "mail", dirs_exist_ok=True)


class Gud1CCArm(Gud1Arm):
    """Claude Code headless (same invocation as the git arm) with the gud v1 MCP server."""
    name = "gud1-cc"

    def mcp_config(self, handle: str) -> Path:
        path = self.root / "mcp" / f"{handle.lstrip('@')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        home = self.homes[handle]
        env = {"PYTHONPATH": str(REPO), "GUD_HOME": str(home), "GUD_HANDLE": handle, "PYTHONDONTWRITEBYTECODE": "1"}
        path.write_text(json.dumps({"mcpServers": {"gud": {
            "command": self.python,
            "args": ["-m", "gud.mcp_v1", "--home", str(home), "--handle", handle, "--wc", str(self.workdir(handle))],
            "env": env,
        }}}, indent=2))
        return path

    def instructions(self, handle: str) -> str:
        work = self.workdir(handle)
        w = int(self.window)
        tests = ("The tester runs `python -m pytest -q -x` on every push before it can link; a failing push is dead "
                 "and you push a fixed one." if self.policy == "gate" else
                 "Pushes do not wait for tests. The tester runs `python -m pytest -q -x` on the tip after links land; "
                 "if the tip fails, the repo turns red, the failing link is found by bisect and a fix task is opened "
                 "(offered to that link's author first). Run the tests yourself before you push.")
        return f"""How this repo works (gud v1):
- Your current directory ({work}) is your own gud working copy of the shared repo: an ordinary directory. Read, edit, run and test code here with your normal tools. Do not edit anything under `.gud/` (gud's state and the repo config).
- The shared repo is a signed email chain, reached through the gud MCP tools (mcp__gud__*). There are no branches, pulls or merges. `push(intent=..., task=...)` sends your working copy's changes (its diff against its base) as one signed push, and it becomes a link (part of the repo) when the repo's rules allow; then `wait(push=<id>)` blocks until it links or something needs you. `sync` merges new links into your working copy (it stops on conflicts and never auto-resolves). `status` shows your base, local changes, open pushes and claims.
- If newer links touched none of your push's files, your push is carried onto the tip automatically; if they touched the same files, it is superseded: `sync`, resolve, and push again.
- The work is task T1 on the board (`tasks`, `task T1`). A task or subtask has at most one active claim: `claim(task, files)` takes it and declares the files you expect to touch; the holder can split it into `subtask`s that others claim. `inflight` shows the other agents' open pushes (with diffs) and every active claim. `comment(target, text)` talks to the others on any task, claim or push; `inbox` lists what needs you.
- A push that rewrites lines another agent wrote recently gives that agent {w}s to object first. Answer an objection with a comment or a revised push (`revises=<your push>`). When you are asked to review (inbox or wait says so), read the diff and approve, object with a concrete reason, or comment.
- {tests} T1 is done when its acceptance check (`{ACCEPT_CMD}` on the tip) passes.
- To tell the others what you are taking on, use claims, push intents and comments."""


class Gud1RefArm(Gud1Arm):
    """The reference agent (`python -m gud.agent.runtime`), one process per member."""
    name = "gud1-ref"

    def mcp_config(self, handle: str) -> None:
        return None

    def instructions(self, handle: str) -> str:
        return "(reference agent: its own system prompt, see gud/agent/prompt.py; the task text is on the board)"

    def run_agents(self, run_dir: Path, *, model: str | None, agent_budget: float, run_budget: float, timeout: float,
                   deny: list[Path], ref_cmd: list[str] | None = None, settle: float = 90.0,
                   done_settle: float = 30.0, poll: float = 3.0, idle: float = 120.0, on_tick=None):
        """Start the N runtimes, stop them when the work settles (no live push and no agent activity for `settle`
        seconds once something linked, or `done_settle` after T1 is done), the run budget is spent, or at the
        timeout. Returns (agent rows, outcome dict, transcripts as tool-call rows)."""
        import analyze
        cmd = list(ref_cmd or [self.python, "-m", "gud.agent.runtime"])
        env = {**_clean_env(), "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        env["PATH"] = os.pathsep.join([str(Path(self.python).parent), env.get("PATH", "/usr/bin:/bin")])
        procs: dict[str, subprocess.Popen] = {}
        started: dict[str, float] = {}
        logs = {}
        for handle in self.handles:
            log_dir = run_dir / "agents" / handle.lstrip("@")
            log_dir.mkdir(parents=True, exist_ok=True)
            argv = cmd + ["--handle", handle, "--home", str(self.homes[handle]), "--wc", str(self.workdir(handle)),
                          "--model", model or "", "--budget-usd", f"{agent_budget:.4f}", "--poll", "2",
                          "--idle", str(idle)]
            for path in deny:
                argv += ["--deny-path", str(path)]
            (log_dir / "argv.json").write_text(json.dumps(argv, indent=1))
            logs[handle] = (log_dir / "runtime.log").open("w")
            procs[handle] = subprocess.Popen(argv, cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=logs[handle],
                                             stderr=subprocess.STDOUT, start_new_session=True)
            started[handle] = time.time()
        t0 = time.time()
        reason = "timeout"
        killed: dict[str, str] = {}
        done_at = None
        errors: list[str] = []
        try:
            while time.time() - t0 < timeout:
                time.sleep(poll)
                spent = sum(self.spent(h) for h in self.handles)
                if on_tick is not None:
                    on_tick(spent)
                if spent >= run_budget:
                    reason = "budget"
                    break
                if all(p.poll() is not None for p in procs.values()):
                    reason = "all_exited"
                    break
                auth = [h for h in self.handles if analyze.ref_auth_failed(self.agent_dir(h))]
                if auth:
                    errors += [f"{h}: authentication failed" for h in auth]
                    reason = "harness_error"
                    break
                self.nelson.sync_log()
                chain = self.nelson.chain()
                live = [p for p in chain.proposals.values() if p.status in LIVE_STATUSES]
                quiet = time.time() - max([self.last_activity(h) for h in self.handles] + [t0])
                task = chain.tasks.get(self.task_id) if self.task_id else None
                if task is not None and task.status == "done" and done_at is None:
                    done_at = time.time()
                linked = len(chain.links) > 1
                if not live and linked and (quiet >= settle or (done_at and quiet >= done_settle)):
                    reason = "settled"
                    break
        finally:
            why = {"budget": "run_budget", "timeout": "timeout", "harness_error": "harness_error"}.get(reason)
            for h, p in procs.items():
                if p.poll() is None:
                    if why:
                        killed[h] = why
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            for h, p in procs.items():
                try:
                    p.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    p.wait(timeout=10)
                    killed.setdefault(h, "harness_exit")
            for fh in logs.values():
                fh.close()
        ended = time.time()
        rows, transcripts = [], {}
        for h in self.handles:
            adir = self.agent_dir(h)
            if adir.exists():
                shutil.copytree(adir, run_dir / "agents" / h.lstrip("@") / "state", dirs_exist_ok=True)
            transcripts[h] = analyze.ref_tool_rows(adir / "transcripts")
            row = analyze.ref_agent_row(h, adir, transcripts[h], model=model, exit_code=procs[h].returncode,
                                        killed=killed.get(h), wall_s=round(ended - started[h], 1))
            rows.append(row)
        outcome = {"reason": "completed" if reason in ("settled", "all_exited") else reason,
                   "stop": reason, "harness_errors": errors, "killed": killed, "task_done_s":
                   round(done_at - t0, 1) if done_at else None}
        return rows, outcome, transcripts

    def agent_dir(self, handle: str) -> Path:
        return self.homes[handle] / "members" / handle / "agent"

    def spent(self, handle: str) -> float:
        try:
            return float(json.loads((self.agent_dir(handle) / "budget.json").read_text())["member"]["spent_usd"])
        except (OSError, ValueError, KeyError):
            return 0.0

    def last_activity(self, handle: str) -> float:
        root = self.agent_dir(handle)
        times = [p.stat().st_mtime for p in root.rglob("*.jsonl")] if root.exists() else []
        return max(times, default=0.0)
