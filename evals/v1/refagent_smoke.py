"""Live smoke of the gud v1 reference agent (PLAN milestone 4): N reference agents
(`python -m gud.agent.runtime`, Claude Agent SDK) and a local tester build one eval spec in a v1
repo hosted in local host mode over the fake transport. When they settle (or at the timeout or
budget), the hidden tests run on the tip tree. The agents are never given the hidden-test path:
their workspaces are outside this repository, and the path is denied to their file tools.

    .venv/bin/python evals/v1/refagent_smoke.py --spec todo-cli --agents 2 --budget-usd 3 --timeout 1500

Writes `<out>/summary.json` (hidden pass rate, cost, turns, coordination counts and transcript
observations), agent logs and transcripts, and the final tree.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import specs  # noqa: E402
from gud.core import envelope as E  # noqa: E402
from gud.core.types import Record  # noqa: E402
from gud.gitbridge import DEFAULT_OWNERS, parse_roster  # noqa: E402
from gud.tester import Tester, TesterIdentity, make_runner  # noqa: E402
from gud.transport.fake import FakeMailbox  # noqa: E402
from gud.wc.copy import create_genesis  # noqa: E402
from gud.wc.remote import LocalHost, RepoClient, save_key, write_transport_toml  # noqa: E402

REPO = "repo@gud.local"
DOMAIN = "gud.local"


def _member_home(root: Path, handle: str, key: bytes, mail: Path, host_url: str) -> Path:
    home = root / "homes" / handle.lstrip("@")
    save_key(home, handle, key, overwrite=True)
    write_transport_toml(home / "members" / handle / "transport.toml", "fake",
                         {"root": str(mail), "address": f"{handle.lstrip('@')}@{DOMAIN}"})
    (home / "remote.json").write_text(json.dumps({"host_url": host_url, "repo": REPO, "handle": handle}))
    return home


def _client(home: Path, handle: str, key: bytes, mail: Path, host_url: str) -> RepoClient:
    return RepoClient(host_url, handle, key, send_via=FakeMailbox(mail, f"{handle.lstrip('@')}@{DOMAIN}"),
                      repo_address=REPO, home=home)


def _spent(home: Path, handle: str) -> float:
    path = home / "members" / handle / "agent" / "budget.json"
    try:
        return float(json.loads(path.read_text())["member"]["spent_usd"])
    except (OSError, ValueError, KeyError):
        return 0.0


def _last_turn(home: Path, handle: str) -> float:
    """Latest activity of the agent: a turn ending or a transcript line (a turn in progress)."""
    root = home / "members" / handle / "agent"
    times = [p.stat().st_mtime for p in root.rglob("*.jsonl")] if root.exists() else []
    return max(times, default=0.0)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def observe(home: Path, handle: str, wc: Path, claims: list[tuple[str, ...]]) -> dict:
    """Transcript observations: gud tools used, local test runs, and reads outside the claimed files."""
    from gud.core.tasks import scope_matches
    tdir = home / "members" / handle / "agent" / "transcripts"
    tools: dict[str, int] = {}
    reads: set[str] = set()
    tests = 0
    texts = []
    for path in sorted(tdir.glob("*.jsonl")) if tdir.exists() else []:
        for row in _read_jsonl(path):
            if row.get("type") == "tool_use":
                name = row.get("name", "")
                tools[name] = tools.get(name, 0) + 1
                inp = row.get("input") or {}
                if name in ("Read", "Grep", "Glob"):
                    target = inp.get("file_path") or inp.get("path") or ""
                    if target:
                        try:
                            reads.add(Path(target).resolve().relative_to(wc.resolve()).as_posix())
                        except ValueError:
                            reads.add(target)
                if name == "mcp__gud__read" and inp.get("path"):
                    reads.add(inp["path"])
                if name == "Bash" and "pytest" in str(inp.get("command", "")):
                    tests += 1
            elif row.get("type") == "text":
                texts.append(row.get("text", "")[:300])
    scopes = [s for s in claims]
    outside = sorted(r for r in reads if r and not any(scope_matches(list(s), r) for s in scopes))
    return {"tool_counts": dict(sorted(tools.items())), "local_test_runs": tests, "reads": sorted(reads),
            "reads_outside_claims": outside, "claims": [list(s) for s in claims], "last_texts": texts[-5:]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="todo-cli")
    ap.add_argument("--agents", type=int, default=2)
    ap.add_argument("--budget-usd", type=float, default=3.0, help="total; each agent gets 90%% of budget/N")
    ap.add_argument("--timeout", type=float, default=1500)
    ap.add_argument("--settle", type=float, default=150, help="stop after this long without a turn once work linked")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--window", default="20s")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    spec = specs.load(args.spec)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else HERE / "runs" / f"{stamp}-refagent-{args.spec}"
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="gud-refagent-"))           # outside this repository
    mail = work / "mail"
    host_dir = work / "host"
    venv_bin = str(Path(sys.executable).parent)
    os.environ["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")
    os.environ.pop("TODO_FILE", None)

    handles = [f"@a{i + 1}" for i in range(args.agents)]
    keys = {h: E.generate_key()[0] for h in handles + ["@tester", "@nelson"]}
    host_key = E.generate_key()[0]
    host = LocalHost.create(host_dir, repo=REPO, transport="fake", cfg={"root": str(mail)}, host_private=host_key,
                            objects=work / "host-objects")
    host_url = f"local:{host_dir}"
    roster = "\n".join([f"{h} agent {h.lstrip('@')}@{DOMAIN} {E.public_of(keys[h])} @nelson" for h in handles]
                       + [f"@tester tester tester@{DOMAIN} {E.public_of(keys['@tester'])} @nelson",
                          f"@nelson human nelson@{DOMAIN} {E.public_of(keys['@nelson'])} -",
                          f"@host host {REPO}"]) + "\n"
    members = parse_roster(roster.encode(), host.host_key)
    config = f"window {args.window}\ngrace 5s\nlease 30m\n".encode()
    test_policy = b"command python -m pytest -q -x\non-paths **\non-tip off\ntimeout 3m\n"
    files = {p: ("100644", t.encode()) for p, t in specs.read_tree(spec.seed).items()}
    files.update({".gud/members": ("100644", members), ".gud/config": ("100644", config),
                  ".gud/test": ("100644", test_policy), ".gud/owners": ("100644", DEFAULT_OWNERS)})

    homes = {h: _member_home(work, h, keys[h], mail, host_url) for h in handles + ["@tester", "@nelson"]}
    nelson = _client(homes["@nelson"], "@nelson", keys["@nelson"], mail, host_url)
    nelson.sync_log()
    genesis = create_genesis(nelson, files, f"genesis: {spec.name} seed")
    nelson.sync_log()
    assert nelson.chain().tip is not None and nelson.chain().tip.state == genesis, "genesis did not link"
    nelson.sign_and_send("task", {"Gud-Title": spec.title}, (Record("text", (), spec.text.encode()),),
                         summary=spec.title)
    nelson.sync_log()

    stop = threading.Event()
    tester_client = _client(homes["@tester"], "@tester", keys["@tester"], mail, host_url)
    tester = Tester(tester_client, make_runner("local", extra_path=[venv_bin]),
                    identity=TesterIdentity("@tester", keys["@tester"]), state_dir=work / "tester")
    tester_errors: list[str] = []

    def run_tester() -> None:
        while not stop.is_set():
            try:
                tester.step()
            except Exception as exc:  # one failed step is logged and retried
                tester_errors.append(f"{type(exc).__name__}: {exc}")
            stop.wait(2.0)

    threading.Thread(target=run_tester, daemon=True).start()

    per_agent = round(args.budget_usd * 0.9 / args.agents, 2)
    procs: dict[str, subprocess.Popen] = {}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GUD_") and k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(ROOT)
    for h in handles:
        log = open(out / f"agent-{h.lstrip('@')}.log", "w")
        argv_a = [sys.executable, "-m", "gud.agent.runtime", "--handle", h, "--home", str(homes[h]),
                  "--wc", str(work / "wc" / h.lstrip("@")), "--model", args.model, "--budget-usd", str(per_agent),
                  "--poll", "2", "--idle", "120", "--deny-path", str(HERE / "hidden"), "--deny-path", str(ROOT)]
        procs[h] = subprocess.Popen(argv_a, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
    t0 = time.time()
    reason = "timeout"
    try:
        while time.time() - t0 < args.timeout:
            time.sleep(5)
            spent = sum(_spent(homes[h], h) for h in handles)
            if spent >= args.budget_usd:
                reason = "budget"
                break
            if all(p.poll() is not None for p in procs.values()):
                reason = "agents exited"
                break
            nelson.sync_log()
            chain = nelson.chain()
            live = [p for p in chain.proposals.values() if p.status in ("window", "objected", "testing", "qualified",
                                                                       "needs_approval", "carried", "orphan")]
            tip_files = set(nelson.reader().tree().paths())
            quiet = time.time() - max([_last_turn(homes[h], h) for h in handles] + [t0]) > args.settle
            if "todo.py" in tip_files and not live and quiet:
                reason = "settled"
                break
    finally:
        for p in procs.values():
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
        for p in procs.values():
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        stop.set()
    wall = time.time() - t0

    nelson.sync_log()
    chain = nelson.chain()
    final = out / "final_tree"
    shutil.rmtree(final, ignore_errors=True)
    for path, (mode, data) in nelson.reader().files().items():
        target = final / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    hidden = specs.run_hidden(spec, final)
    visible = specs.run_visible_tests(final)
    (out / "hidden_tests.log").write_text(hidden.pop("tail", ""))
    claims_by: dict[str, list[tuple[str, ...]]] = {}
    for c in chain.claims.values():
        claims_by.setdefault(c.holder, []).append(tuple(c.files))
    agents = {}
    for h in handles:
        adir = homes[h] / "members" / h / "agent"
        turns = _read_jsonl(adir / "turns.jsonl")
        agents[h] = {"spent_usd": round(_spent(homes[h], h), 4), "turns": len(turns),
                     "turn_keys": [t["key"][:20] for t in turns], "exit": procs[h].returncode,
                     **observe(homes[h], h, work / "wc" / h.lstrip("@"), claims_by.get(h, []))}
        if adir.exists():
            shutil.copytree(adir, out / "agents" / h.lstrip("@"), dirs_exist_ok=True)
    kinds: dict[str, int] = {}
    for _, gid, _t in nelson.replayer().log().mail_ids():
        email = nelson.reader().email(gid)
        k = email.envelope.kind if email is not None and email.envelope is not None else "unsigned"
        kinds[k] = kinds.get(k, 0) + 1
    statuses: dict[str, int] = {}
    for p in chain.proposals.values():
        statuses[p.status] = statuses.get(p.status, 0) + 1
    summary = {
        "spec": spec.name, "agents": args.agents, "model": args.model, "stop": reason, "wall_s": round(wall, 1),
        "cost_usd": round(sum(a["spent_usd"] for a in agents.values()), 4), "budget_usd": args.budget_usd,
        "hidden": {k: hidden[k] for k in ("passed", "total", "pass_rate")}, "spec_checks": hidden["tests"],
        "final_tests_pass": visible["passed"],
        "links": [{"seq": l.seq, "author": l.author, "task": l.task[:12] if l.task else None,
                   "intent": l.intent.splitlines()[0] if l.intent else "", "files": list(l.files)} for l in chain.links],
        "emails_by_kind": dict(sorted(kinds.items())), "open_proposal_statuses": statuses,
        "tasks": [{"alias": t.alias, "title": t.title, "status": t.status} for t in chain.tasks.values()],
        "claims": [{"holder": c.holder, "task": chain.tasks[c.task].alias if c.task in chain.tasks else c.task[:12],
                    "files": list(c.files), "status": c.status} for c in chain.claims.values()],
        "agents_detail": agents, "tester_errors": tester_errors[-10:], "work_dir": str(work),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps({k: summary[k] for k in ("stop", "wall_s", "cost_usd", "hidden", "final_tests_pass")}, indent=1))
    print(f"summary: {out / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
