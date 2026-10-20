"""The gud v1 arms: a real v1 host (`gud host serve`), a real tester (`gud tester`), and either Claude Code
sessions on the gud v1 MCP server (`gud1-cc`, `gud mcp`) or reference agents (`gud1-ref`, `gud agent`).

Everything goes through the gud v1 command line (`python -m gud.cli_v1 --home H --handle @x ...`, with this
checkout on PYTHONPATH), the same way `tests/v1/e2e/scenario.py` drives it:

    gud1 host init --repo repo@gud.local --transport fake --set root=<mail> --dir <host>   (as @hoster)
    gud1 host serve --dir <host> --bind 127.0.0.1:<port>                                   (long-lived)
    gud1 keys new --address <x>@gud.local --transport fake --set root=<mail>               (every member)
    gud1 init <seed dir> --repo ... --member-file <roster with every key> --host-url <url> (as @nelson)
    gud1 task new "<title>" -m "<spec text>" --accept-cmd "python -m pytest -q"            (as @nelson)
    gud1 clone <url> <agents/aK>                                                           (every agent)
    gud1 tester --interval 2                                                               (as @tester)
    gud1 mcp --wc <agents/aK>          gud1-cc: the --mcp-config server of agent K
    gud1 agent --wc <agents/aK> ...    gud1-ref: the reference agent K

Layout under the run's work root:

    mail/              the FakeMailbox root (repo@gud.local and every member's address)
    host/              the host directory (host key, signed host log, raw mail, journals)
    homes/<x>/         GUD_HOME of each member (key, transport.toml, remote.json, replay cache, objects)
    seed/              @nelson's working copy (the genesis tree)
    agents/<aK>/       agent K's working copy: its cwd
    mcp/<aK>.json      gud1-cc: agent K's --mcp-config
    logs/              host.log

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
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from gud.core.types import LIVE_STATUSES  # noqa: E402

DOMAIN = "gud.local"
REPO_ADDR = f"repo@{DOMAIN}"
ACCEPT_CMD = "python -m pytest -q"
POLICIES = {
    "tip": "command python -m pytest -q -x\non-tip links=1 time=2m\ntimeout 3m\n",
    "gate": "command python -m pytest -q -x\non-paths **\non-tip off\ntimeout 3m\n",
}


class Gud1Error(RuntimeError):
    """A gud1 command failed during setup (a harness error, not an agent result)."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
        self.host_url = ""
        self.homes: dict[str, Path] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self._logs: list = []
        self.tester_log: Path | None = None
        self.task_id: str | None = None
        self.genesis: str | None = None
        self._t0 = 0.0
        self._nelson = None

    # -- the gud1 command line -------------------------------------------------------------

    def env(self) -> dict[str, str]:
        """Environment of every gud process: this checkout on PYTHONPATH, the venv's python first on PATH."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GUD_", "PYTEST_")) and k not in ("PYTHONPATH", "TODO_FILE", "NOTES_DIR")}
        env["PYTHONPATH"] = str(REPO)
        env["PATH"] = os.pathsep.join([str(Path(self.python).parent), env.get("PATH", "/usr/bin:/bin")])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def argv(self, handle: str, *args: str) -> list[str]:
        return [self.python, "-m", "gud.cli_v1", "--home", str(self.home(handle)), "--handle", handle, *args]

    def gud1(self, handle: str, *args: str, cwd: Path | None = None, timeout: float = 180) -> dict:
        """Run one `gud1 --json` command as `handle`; its JSON, or Gud1Error."""
        argv = self.argv(handle, "--json", *args)
        proc = subprocess.run(argv, cwd=cwd or self.root, env=self.env(), capture_output=True, text=True,
                              timeout=timeout)
        lines = proc.stdout.strip().splitlines()
        try:
            out = json.loads(lines[-1]) if lines else {}
        except ValueError:
            out = {}
        if proc.returncode != 0 or "error" in out:
            raise Gud1Error(f"gud1 {' '.join(args)} ({handle}) -> {proc.returncode}: {proc.stdout[-800:]}"
                            f"{proc.stderr[-800:]}")
        return out

    def home(self, handle: str) -> Path:
        if handle not in self.homes:
            self.homes[handle] = self.root / "homes" / handle.lstrip("@")
            self.homes[handle].mkdir(parents=True, exist_ok=True)
        return self.homes[handle]

    def address(self, handle: str) -> str:
        return f"{handle.lstrip('@')}@{DOMAIN}"

    def transport(self) -> list[str]:
        return ["--transport", "fake", "--set", f"root={self.mail}"]

    def _spawn(self, name: str, argv: list[str], log: Path, cwd: Path | None = None) -> subprocess.Popen:
        log.parent.mkdir(parents=True, exist_ok=True)
        fh = log.open("w")
        self._logs.append(fh)
        proc = subprocess.Popen(argv, cwd=cwd or self.root, env=self.env(), stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self.procs[name] = proc
        return proc

    @property
    def nelson(self):
        """@nelson's RepoClient over the host's HTTP mirror (read-only use: the harness's own view)."""
        if self._nelson is None:
            from gud.mcp_v1 import open_member
            self._nelson, _ = open_member(self.home("@nelson"), "@nelson", self.root / "seed")
        return self._nelson

    def config_bytes(self) -> bytes:
        w = int(self.window) if self.window.is_integer() else self.window
        g = int(self.grace) if self.grace.is_integer() else self.grace
        return f"window {w}s\ngrace {g}s\nlease 10m\nescalate 3m\nfix-offer 2m\n".encode()

    # -- setup -----------------------------------------------------------------------------

    def setup(self) -> None:
        from gud.wc.ignore import DEFAULT_GUDIGNORE
        from specs import read_tree, write_tree

        self.root.mkdir(parents=True, exist_ok=True)
        self.mail.mkdir(parents=True, exist_ok=True)
        made = self.gud1("@hoster", "host", "init", "--repo", REPO_ADDR, *self.transport(), "--dir", str(self.host_dir))
        port = _free_port()
        self._spawn("host", self.argv("@hoster", "host", "serve", "--dir", str(self.host_dir), "--bind",
                                      f"127.0.0.1:{port}"), self.root / "logs" / "host.log")
        self.host_url = f"http://127.0.0.1:{port}"
        self._wait_host(made.get("host_key"))
        keys = {}
        for handle in self.handles + ["@tester", "@nelson"]:
            keys[handle] = self.gud1(handle, "keys", "new", "--address", self.address(handle),
                                     *self.transport())["public_key"]
        roster = self.root / "roster"
        roster.write_text("\n".join(
            [f"{h} agent {self.address(h)} {keys[h]} @nelson" for h in self.handles]
            + [f"@tester tester {self.address('@tester')} {keys['@tester']} @nelson",
               f"@nelson human {self.address('@nelson')} {keys['@nelson']} -",
               f"@host host {REPO_ADDR}"]) + "\n")
        seed_dir = self.root / "seed"
        seed = read_tree(self.spec.seed)
        write_tree(seed_dir, seed)
        ignore = DEFAULT_GUDIGNORE.rstrip("\n").splitlines()
        for line in seed.get(".gitignore", "").splitlines():
            if line.strip() and line.strip() not in ignore:
                ignore.append(line.strip())
        (seed_dir / ".gudignore").write_text("\n".join(ignore) + "\n")
        (seed_dir / ".gud").mkdir(exist_ok=True)
        (seed_dir / ".gud" / "config").write_bytes(self.config_bytes())
        (seed_dir / ".gud" / "test").write_text(POLICIES[self.policy])
        out = self.gud1("@nelson", "init", str(seed_dir), "--repo", REPO_ADDR, "--member-file", str(roster),
                        "--host-url", self.host_url, "-m", f"genesis: {self.spec.name} seed", cwd=seed_dir)
        self.genesis = out["genesis"]
        for handle in self.handles:
            self.gud1(handle, "clone", self.host_url, str(self.workdir(handle, create=False)))
        self.gud1("@tester", "clone", self.host_url, str(self.root / "tester-wc"))   # saves its remote

    def _wait_host(self, host_key: str | None, timeout: float = 60) -> None:
        import httpx
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.procs["host"].poll() is not None:
                raise Gud1Error(f"gud host serve exited: {(self.root / 'logs' / 'host.log').read_text()[-2000:]}")
            try:
                info = httpx.get(self.host_url + "/api/v1/info", timeout=2).json()
                if host_key is None or info.get("host_key") == host_key:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.2)
        raise Gud1Error("gud host serve did not answer")

    def start_services(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.tester_log = log_dir / "tester.log"
        self._spawn("tester", self.argv("@tester", "tester", "--interval", "2"), self.tester_log,
                    cwd=self.home("@tester"))

    def mark_start(self, t0: float) -> None:
        self._t0 = t0

    def task_text(self) -> str:
        """The task on the board: the spec text plus the one ground rule every arm's prompt also states."""
        return (self.spec.text + "\n\nHidden acceptance tests will check the finished repository against this "
                "text, so follow it exactly.")

    def seed_task(self) -> None:
        out = self.gud1("@nelson", "task", "new", self.spec.title, "-m", self.task_text(), "--accept-cmd", ACCEPT_CMD,
                        cwd=self.root / "seed")
        self.task_id = out.get("id") or out.get("task") or out.get("gud_id")
        deadline = time.time() + 30
        while time.time() < deadline:
            self.nelson.sync_log()
            tasks = self.nelson.chain().tasks
            if self.task_id in tasks or (self.task_id is None and tasks):
                self.task_id = self.task_id or next(iter(tasks))
                return
            time.sleep(0.3)
        raise Gud1Error(f"the task was not logged ({out})")

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
        """Stop the tester (the agents have stopped). The host keeps serving for the harness's analysis."""
        self._stop("tester")

    def close(self) -> None:
        """Stop the tester and the host (idempotent)."""
        self._stop("tester", "host")
        for fh in self._logs:
            fh.close()
        self._logs = []

    def _stop(self, *names: str) -> None:
        for name in names:
            proc = self.procs.get(name)
            if proc is None or proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

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
        host_log = self.root / "logs" / "host.log"
        if host_log.exists():
            shutil.copy(host_log, run_dir / "host.log")


class Gud1CCArm(Gud1Arm):
    """Claude Code headless (same invocation as the git arm) with the gud v1 MCP server (`gud1 mcp`)."""
    name = "gud1-cc"

    def mcp_config(self, handle: str) -> Path:
        path = self.root / "mcp" / f"{handle.lstrip('@')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        env = {"PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        path.write_text(json.dumps({"mcpServers": {"gud": {
            "command": self.python,
            "args": self.argv(handle, "mcp", "--wc", str(self.workdir(handle)))[1:],     # gud1 ... mcp --wc WC
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
- {tests} T1 is done when its claim holder declares it done (`claim_update(claim, done=true)`) or all its subtasks are done, and then its acceptance check (`{ACCEPT_CMD}` on the tip) passes.
- To tell the others what you are taking on, use claims, push intents and comments."""


class Gud1RefArm(Gud1Arm):
    """The reference agent (`gud1 agent`, gud.agent.runtime), one process per member."""
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
        env = self.env()
        procs: dict[str, subprocess.Popen] = {}
        started: dict[str, float] = {}
        logs = {}
        for handle in self.handles:
            log_dir = run_dir / "agents" / handle.lstrip("@")
            log_dir.mkdir(parents=True, exist_ok=True)
            # `gud1 --home H --handle @aK agent --wc WC <runtime options>`; a test passes its own command,
            # which gets the same --home/--handle and options
            prefix = (list(ref_cmd) + ["--home", str(self.home(handle)), "--handle", handle] if ref_cmd
                      else self.argv(handle, "agent"))
            argv = prefix + ["--wc", str(self.workdir(handle)), "--model", model or "", "--budget-usd", f"{agent_budget:.4f}", "--poll", "2",
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
        return self.home(handle) / "members" / handle / "agent"

    def spent(self, handle: str) -> float:
        try:
            return float(json.loads((self.agent_dir(handle) / "budget.json").read_text())["member"]["spent_usd"])
        except (OSError, ValueError, KeyError):
            return 0.0

    def last_activity(self, handle: str) -> float:
        root = self.agent_dir(handle)
        times = [p.stat().st_mtime for p in root.rglob("*.jsonl")] if root.exists() else []
        return max(times, default=0.0)
