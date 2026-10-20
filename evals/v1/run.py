"""Real-agent eval: git vs gud as the coordination layer for a swarm of coding agents.

    .venv/bin/python evals/v1/run.py --arm gud --spec acme-site --agents 2 --trials 1 \\
        --budget-usd 4 --timeout 1200

Every agent is the same headless Claude Code session (same model, same built-in tools, same task text,
same per-agent budget). Only the coordination layer differs (see git_arm.py and gud_arm.py). After the
agents stop, the harness runs the spec's hidden acceptance tests on the final tree and writes
``<out>/<arm>-<spec>-n<N>-t<K>/summary.json``. ``report.py`` aggregates runs.

Agent workspaces live in a temp directory outside this repository, so agents never see the harness,
the hidden tests, or the other arm.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for path in (HERE, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import specs  # noqa: E402
from agents import DEFAULT_TOOLS, ESCALATE, claude_argv, supervise  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-5-5"
SECRET_ENV = ("ARMATURE_API_KEY", "OPENROUTER_KEY", "OPENROUTER_API_KEY", "PRIMITIVE_API_KEY",
              "INSTABOX_API_KEY")


def find_claude() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


ARMS = ("git", "gud", "gud1-cc", "gud1-ref")
ARM_SETS = {"both": ("git", "gud"), "v1": ("git", "gud1-cc", "gud1-ref")}


def make_arm(name: str, root: Path, spec, handles: list[str], window: float, policy: str = "tip"):
    if name == "git":
        from git_arm import GitArm
        return GitArm(root, spec, handles)
    if name == "gud":
        from gud_arm import GudArm
        return GudArm(root, spec, handles, window=window)
    if name == "gud1-cc":
        from gud1_arm import Gud1CCArm
        return Gud1CCArm(root, spec, handles, window=window, policy=policy)
    if name == "gud1-ref":
        from gud1_arm import Gud1RefArm
        return Gud1RefArm(root, spec, handles, window=window, policy=policy)
    raise SystemExit(f"unknown arm {name!r}")


def prompt_for(arm, spec, handle: str, handles: list[str], agent_budget: float) -> str:
    others = ", ".join(h for h in handles if h != handle) or "none"
    return f"""You are {handle}, one of {len(handles)} coding agents working at the same time on the same shared repository. The others are: {others}. There is no lead agent, and no human is available during the run; the other agents are peers who received this same task.

# Task: {spec.title}

{spec.text}

# Ground rules
- Coordinate with the other agents: split the work sensibly, say what you are taking on, and avoid editing the same lines at the same time.
- Work in small, complete increments. Every change you land must keep `python -m pytest -q` passing.
- Hidden acceptance tests will check the finished repository against the task text, so follow it exactly.
- Your budget for this session is about ${agent_budget:.2f}.
- Stop (end with a short summary) when the task is fully done in the shared repository and its tests pass there, or when you cannot make further useful progress. Before stopping, check whether another agent is waiting on a review from you.
- If you are blocked on a decision only a human can make, put a line starting with `{ESCALATE}` followed by the question in your final message.

# {arm.instructions(handle)}
"""


def agent_env(arm, handle: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GUD_", "PYTEST_")) and k not in SECRET_ENV and k != "PYTHONPATH"}
    extra = arm.agent_env(handle)
    prefix = [p for p in (extra.pop("PATH_PREFIX", None), str(Path(sys.executable).parent)) if p]
    env["PATH"] = os.pathsep.join(prefix + [env.get("PATH", "/usr/bin:/bin")])
    env.update(extra)
    env.update({
        "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1",
        "MCP_TOOL_TIMEOUT": "600000",
    })
    return env


def build_argv(agent_cmd: str, prompt: str, *, model: str | None, agent_budget: float, mcp_config,
               deny: list[Path]) -> list[str]:
    parts = shlex.split(agent_cmd)
    return parts[:-1] + claude_argv(parts[-1], prompt, model=model, agent_budget=agent_budget,
                                    mcp_config=mcp_config, allow_mcp="gud" if mcp_config else None,
                                    deny_paths=deny)


def check_auth(agent_cmd: str) -> str | None:
    """A free check that the Claude Code login is present. Expiry still shows up as an auth error
    event at the start of the run, which the supervisor turns into a harness error."""
    parts = shlex.split(agent_cmd)
    if Path(parts[-1]).name != "claude":
        return None
    try:
        proc = subprocess.run([parts[-1], "auth", "status"], capture_output=True, text=True, timeout=30)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return f"claude auth status failed: {exc}"
    if not data.get("loggedIn"):
        return "claude is not logged in (run `claude auth login`)"
    return None


def local_test_runs(rows: list[dict]) -> int:
    n = 0
    for row in rows:
        if row["name"] == "Bash" and "pytest" in str((row.get("input") or {}).get("command", "")):
            n += 1
    return n


def hidden_access(transcripts: dict[str, list[dict]], needles: list[str]) -> list[dict]:
    hits = []
    for handle, rows in transcripts.items():
        for row in rows:
            blob = json.dumps(row.get("input"), default=str)
            for needle in needles:
                if needle and needle in blob:
                    hits.append({"agent": handle, "tool": row["name"], "needle": needle})
    return hits


def run_one(*, arm_name: str, spec_name: str, n: int, trial: int, budget: float, timeout: float,
            out: Path, agent_cmd: str, model: str | None, window: float, keep_work: bool,
            work_root: Path | None = None, mcp_grace: float = 90.0, policy: str = "tip",
            ledger=None, ref_cmd: list[str] | None = None, ref_settle: float = 90.0,
            drain_s: float | None = None) -> dict:
    spec = specs.load(spec_name)
    run_dir = out / f"{arm_name}-{spec_name}-n{n}-t{trial}"
    run_dir.mkdir(parents=True, exist_ok=True)
    key = f"{out.name}/{run_dir.name}"
    requested = budget
    if ledger is not None:
        budget = ledger.reserve(key, budget)
        if budget <= 0:
            summary = {"arm": arm_name, "spec": spec_name, "agents_n": n, "trial": trial, "outcome": "skipped_cap",
                       "budget_usd": 0.0, "requested_budget_usd": requested, "cap": ledger.state()}
            (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
            print(f"[{run_dir.name}] skipped: the spending cap is reached ({ledger.state()})", flush=True)
            return summary
    spent = 0.0
    try:
        summary = _run_one(arm_name=arm_name, spec=spec, n=n, trial=trial, budget=budget, timeout=timeout,
                           run_dir=run_dir, out=out, agent_cmd=agent_cmd, model=model, window=window,
                           keep_work=keep_work, work_root=work_root, mcp_grace=mcp_grace, policy=policy,
                           ref_cmd=ref_cmd, ref_settle=ref_settle, drain_s=drain_s)
        spent = float(summary.get("cost_usd") or 0.0)
        summary["requested_budget_usd"] = requested
        if ledger is not None:
            summary["cap"] = {k: v for k, v in ledger.state().items() if k != "runs"}
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        return summary
    except BaseException:
        spent = _partial_cost(run_dir)
        raise
    finally:
        if ledger is not None:
            ledger.commit(key, spent, arm=arm_name, spec=spec_name, n=n, trial=trial)


def _partial_cost(run_dir: Path) -> float:
    """Best-effort spend of a run that crashed: Claude Code stream estimates or reference-agent ledgers."""
    from streams import StreamStats
    total = 0.0
    for stream in run_dir.glob("agents/*/stream.jsonl"):
        stats = StreamStats()
        for line in stream.read_text(errors="replace").splitlines():
            stats.feed_line(line)
        total += stats.cost()
    for budget in run_dir.glob("agents/*/state/budget.json"):
        try:
            total += float(json.loads(budget.read_text())["member"]["spent_usd"])
        except (OSError, ValueError, KeyError):
            pass
    return total


def _run_one(*, arm_name: str, spec, n: int, trial: int, budget: float, timeout: float, run_dir: Path, out: Path,
             agent_cmd: str, model: str | None, window: float, keep_work: bool, work_root: Path | None,
             mcp_grace: float, policy: str, ref_cmd: list[str] | None, ref_settle: float,
             drain_s: float | None) -> dict:
    import analyze

    spec_name = spec.name
    root = Path(tempfile.mkdtemp(prefix=f"gudv1-{arm_name}-", dir=work_root)).resolve()
    handles = [f"@a{i + 1}" for i in range(n)]
    agent_budget = budget / n
    arm = make_arm(arm_name, root, spec, handles, window, policy)
    v1 = arm_name.startswith("gud1")
    ref = arm_name == "gud1-ref"
    print(f"[{run_dir.name}] work root {root}", flush=True)
    tools = list(DEFAULT_TOOLS) + (["mcp__gud__*"] if arm_name != "git" else [])
    summary: dict = {
        "arm": arm_name, "spec": spec_name, "agents_n": n, "trial": trial,
        "requested_model": model, "budget_usd": budget, "agent_budget_usd": round(agent_budget, 4),
        "timeout_s": timeout, "window_s": window if arm_name != "git" else None,
        "test_policy": policy if v1 else None,
        "agent_cmd": "gud.agent.runtime" if ref else shlex.split(agent_cmd)[-1], "work_root": str(root),
        "fairness": {
            "agent": "reference agent (Claude Agent SDK)" if ref else "Claude Code headless",
            "tools": tools,
            "test_command": "python -m pytest -q -x",
            "same_prompt_preamble": not ref,
        },
    }
    auth = check_auth(agent_cmd if not ref or ref_cmd is None else "x")
    if auth:
        summary.update(outcome="harness_error", harness_errors=[auth], cost_usd=0.0)
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        print(f"[{run_dir.name}] {auth}", flush=True)
        return summary
    arm.setup()
    arm.start_services(run_dir)
    t0 = time.time()
    arm.mark_start(t0)
    arm.seed_task()
    deny = [HERE, out.resolve()]
    drained = None
    try:
        if ref:
            agents_out, ref_outcome, transcripts = arm.run_agents(
                run_dir, model=model, agent_budget=agent_budget, run_budget=budget, timeout=timeout, deny=deny,
                ref_cmd=ref_cmd, settle=ref_settle)
            outcome_reason = ref_outcome["reason"]
            harness_errors = ref_outcome["harness_errors"]
            killed = ref_outcome["killed"]
            summary["ref_stop"] = ref_outcome["stop"]
            for row in agents_out:
                row["local_test_runs"] = local_test_runs(transcripts[row["handle"]])
                (run_dir / "agents" / row["handle"].lstrip("@") / "tools.json").write_text(
                    json.dumps(transcripts[row["handle"]], indent=1, default=str))
        else:
            from agents import AgentSpec
            agent_specs = []
            for handle in handles:
                prompt = prompt_for(arm, spec, handle, handles, agent_budget)
                mcp = arm.mcp_config(handle)
                log_dir = run_dir / "agents" / handle.lstrip("@")
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "prompt.txt").write_text(prompt)
                agent_specs.append(AgentSpec(
                    handle=handle, cwd=arm.workdir(handle), env=agent_env(arm, handle), log_dir=log_dir,
                    argv=build_argv(agent_cmd, prompt, model=model, agent_budget=agent_budget, mcp_config=mcp,
                                    deny=deny),
                    require_mcp="gud" if mcp else None,
                ))
            (run_dir / "argv.json").write_text(json.dumps(
                [[a if i != 2 else "<prompt.txt>" for i, a in enumerate(spec_.argv)] for spec_ in agent_specs],
                indent=1))
            runs, outcome = supervise(agent_specs, run_budget=budget, agent_budget=agent_budget, timeout=timeout,
                                      mcp_grace=mcp_grace)
            outcome_reason = "completed" if outcome.reason == "all_exited" else outcome.reason
            harness_errors, killed = outcome.harness_errors, outcome.killed
            transcripts = {run.spec.handle: run.stats.tool_calls for run in runs}
            agents_out = []
            for run in runs:
                (run.spec.log_dir / "tools.json").write_text(json.dumps(run.stats.tool_calls, indent=1, default=str))
                row = run.summary()
                row["local_test_runs"] = local_test_runs(run.stats.tool_calls)
                agents_out.append(row)
        agents_wall = time.time() - t0
        if v1 and outcome_reason != "harness_error":
            drained = arm.drain(drain_s if drain_s is not None else window + 60)
    finally:
        arm.stop_services()
    wall = agents_wall
    final_dir = run_dir / "final_tree"
    if final_dir.exists():
        shutil.rmtree(final_dir)
    tree = arm.final_tree()
    specs.write_tree(final_dir, tree)
    hidden = specs.run_hidden(spec, final_dir)
    visible = specs.run_visible_tests(final_dir)
    coordination = arm.metrics(wall, transcripts)
    mkey = getattr(arm, "metrics_key", arm_name)
    models = sorted({row["model"] for row in agents_out if row["model"]})
    tokens = {k: sum(row["tokens"].get(k, 0) for row in agents_out) for k in ("input", "output", "cache_read",
                                                                               "cache_creation")}
    escalations = sum(1 for row in agents_out if row["escalated"])
    if v1:
        escalations = sum(1 for row in agents_out if row["escalated"]) + (
            0 if ref else len(coordination["gud1"]["escalations"]["task"]))
    summary.update({
        "outcome": outcome_reason,
        "harness_errors": harness_errors,
        "killed": killed,
        "model": models[0] if len(models) == 1 else models,
        "wall_s": round(wall, 1),
        "drain_s": drained,
        "cost_usd": round(sum(row["cost_usd"] for row in agents_out), 4),
        "tokens": tokens,
        "tokens_total": sum(tokens.values()),
        "turns": sum(row["turns"] or 0 for row in agents_out),
        "hidden": {k: hidden[k] for k in ("passed", "failed", "total", "pass_rate")},
        "spec_checks": hidden["tests"],
        "final_tests_pass": visible["passed"],
        "final_files": sorted(tree),
        "escalations": escalations,
        "local_test_runs": sum(row["local_test_runs"] for row in agents_out),
        "hidden_access": hidden_access(transcripts, [str(specs.HIDDEN), "evals/v1/hidden", "test_hidden.py"]),
        **{k: coordination[k] for k in ("integrations", "integrations_per_min", "test_runs",
                                        "test_runs_per_integration", "conflicts_total", "conflicts_resolved")},
        mkey: coordination[mkey],
        "agents": agents_out,
    })
    if ref:
        summary["tokens_ledger"] = sum((row["tokens"] or {}).get("ledger", 0) for row in agents_out)
    # transcript analysis (every arm)
    meta = specs.meta(spec)
    seed_paths = set(specs.read_tree(spec.seed))
    if v1:
        pushes = coordination[mkey].pop("_pushes_full")
        changes: dict[str, set] = {}
        for p in pushes:
            changes.setdefault(p["author"], set()).update(p["files"])
        tracked = seed_paths | set(tree) | {f for p in pushes for f in p["files"]}
        claims = {h: a["claim_scopes_all"] for h, a in coordination[mkey]["per_agent"].items()}
        summary["analysis"] = analyze.transcript_analysis(
            transcripts, roots={h: arm.workdir(h) for h in handles}, tracked=tracked, changes=changes,
            claims=claims, pushes=pushes, guard_files=meta.get("guard_files", ()))
    else:
        changes = analyze.git_changes(root / "origin.git") if arm_name == "git" else {}
        summary["analysis"] = analyze.transcript_analysis(
            transcripts, roots={h: arm.workdir(h) for h in handles}, tracked=seed_paths | set(tree),
            changes=changes, guard_files=meta.get("guard_files", ()))
    if meta.get("guard_files"):
        seed_tree = specs.read_tree(spec.seed)
        summary["context_check"] = {
            "guard_files": meta["guard_files"],
            "guard_changed_in_final_tree": {g: tree.get(g) != seed_tree.get(g) for g in meta["guard_files"]},
            "guard_tests_passed": {t: hidden["tests"].get(t) for t in meta.get("guard_tests", ())},
            "read_by": summary["analysis"]["summary"].get("guard_read_by"),
        }
    (run_dir / "hidden_tests.log").write_text(hidden["tail"])
    (run_dir / "final_tests.log").write_text(visible["tail"])
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    _keep_artifacts(arm, arm_name, root, run_dir)
    if not keep_work:
        shutil.rmtree(root, ignore_errors=True)
    print(f"[{run_dir.name}] {summary['outcome']}: hidden {hidden['passed']}/{hidden['total']}, "
          f"${summary['cost_usd']:.2f}, {summary['wall_s']:.0f}s, integrations {summary['integrations']}", flush=True)
    return summary


def _keep_artifacts(arm, arm_name: str, root: Path, run_dir: Path) -> None:
    if arm_name.startswith("gud1"):
        arm.keep_artifacts(run_dir)
    elif arm_name == "gud":
        mailbox = root / "gud-home" / "mailbox.jsonl"
        if mailbox.exists():
            shutil.copy(mailbox, run_dir / "mailbox.jsonl")
    else:
        prs = root / "prs"
        if prs.exists():
            shutil.copytree(prs, run_dir / "prs", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".lock"))
        log = subprocess.run(["git", "--git-dir", str(root / "origin.git"), "log", "--format=%h %an %s", "main"],
                             capture_output=True, text=True)
        (run_dir / "main.log").write_text(log.stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", required=True, choices=ARMS + tuple(ARM_SETS))
    ap.add_argument("--spec", required=True, choices=specs.names())
    ap.add_argument("--agents", type=int, default=2)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--budget-usd", type=float, required=True, help="per run; each agent gets budget/N")
    ap.add_argument("--timeout", type=float, default=1200, help="wall-clock seconds per run")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL, help="'' lets the CLI pick its default")
    ap.add_argument("--agent-cmd", default=None, help="agent command; default: the claude CLI")
    ap.add_argument("--ref-cmd", default=None, help="gud1-ref agent command (default: python -m gud.agent.runtime)")
    ap.add_argument("--window", type=float, default=None, help="consent window in seconds (gud 30, gud1 20)")
    ap.add_argument("--gud-test-policy", default="tip", choices=("tip", "gate"), help="gud1 arms' .gud/test")
    ap.add_argument("--cap-usd", type=float, default=None, help="hard cap on total spend across runs (ledger)")
    ap.add_argument("--ledger", type=Path, default=HERE / "runs" / "spend.json")
    ap.add_argument("--keep-work", action="store_true", help="keep the temp workspaces")
    ap.add_argument("--work-root", type=Path, default=None)
    args = ap.parse_args(argv)
    out = args.out or HERE / "runs" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    agent_cmd = args.agent_cmd or find_claude()
    arms = ARM_SETS.get(args.arm, (args.arm,))
    ledger = None
    if args.cap_usd is not None:
        from cap import CapLedger
        ledger = CapLedger(args.ledger, args.cap_usd)
    results = []
    for trial in range(1, args.trials + 1):
        for arm in arms:
            window = args.window if args.window is not None else (30.0 if arm == "gud" else 20.0)
            results.append(run_one(
                arm_name=arm, spec_name=args.spec, n=args.agents, trial=trial, budget=args.budget_usd,
                timeout=args.timeout, out=out, agent_cmd=agent_cmd, model=args.model or None,
                window=window, keep_work=args.keep_work, work_root=args.work_root, policy=args.gud_test_policy,
                ledger=ledger, ref_cmd=shlex.split(args.ref_cmd) if args.ref_cmd else None,
            ))
    from report import report
    print(report([out]))
    if ledger is not None:
        print(f"spend: {ledger.state()}")
    return 1 if any(r.get("outcome") == "harness_error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
