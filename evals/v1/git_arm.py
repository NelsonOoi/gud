"""The git arm: one bare repo on local disk, a clone and git identity per agent, local PRs.

Layout under the run's work root:

    origin.git/          the shared bare repo (file://). A pre-receive hook rejects pushes to main
                         unless they come from `pr merge`.
    prs/                 PR state (<ID>.json) and events.jsonl, written only by `pr`
    bin/pr, bin/prtool.py  the PR tool, on every agent's PATH
    gitconfig/<a1>       the agent's private global git config (user.name/email, pull.rebase=true)
    agents/<a1>/         the agent's clone (its working directory)

Protocol (same for every agent; see prtool.py): branch, commit, push the branch, `pr open`; another
agent `pr approve`s; `pr merge` lands it if it rebases cleanly onto main and `python -m pytest -q -x`
passes. There is no lead and no issue tracker, matching the gud arm.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

HOOK = """#!/bin/sh
# Only `pr merge` may update main (it sets GUD_EVAL_MERGE=1).
while read old new ref; do
  if [ "$ref" = "refs/heads/main" ] && [ "$GUD_EVAL_MERGE" != "1" ]; then
    echo "main is protected: push a branch, open a PR with 'pr open', and land it with 'pr merge ID'" >&2
    printf '{"t": %s, "kind": "direct_main_push_rejected", "by": "%s"}\\n' "$(date +%s)" "$GUD_EVAL_HANDLE" >> "$GUD_EVAL_ROOT/prs/events.jsonl"
    exit 1
  fi
done
exit 0
"""


def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    full = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", **(env or {})}
    proc = subprocess.run(["git", *args], cwd=cwd, env=full, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stdout}{proc.stderr}")
    return proc


class GitArm:
    name = "git"

    def __init__(self, root: Path, spec, handles: list[str], *, python: str = sys.executable,
                 test_timeout: float = 120):
        self.root = Path(root)
        self.spec = spec
        self.handles = list(handles)
        self.python = python
        self.test_timeout = test_timeout
        self.origin = self.root / "origin.git"
        self.prs = self.root / "prs"
        self._t0 = 0.0

    def mark_start(self, t0: float) -> None:
        self._t0 = t0

    def gitconfig(self, handle: str) -> Path:
        path = self.root / "gitconfig" / handle.lstrip("@")
        path.parent.mkdir(parents=True, exist_ok=True)
        name = handle.lstrip("@")
        path.write_text(
            "[user]\n"
            f"\tname = {handle}\n"
            f"\temail = {name}@agents.local\n"
            "[init]\n\tdefaultBranch = main\n"
            "[pull]\n\trebase = true\n"
            "[commit]\n\tgpgsign = false\n"
            "[advice]\n\tdetachedHead = false\n"
        )
        return path

    def setup(self) -> None:
        from specs import write_tree, read_tree

        self.root.mkdir(parents=True, exist_ok=True)
        self.prs.mkdir(exist_ok=True)
        (self.root / "merge").mkdir(exist_ok=True)
        (self.prs / "events.jsonl").touch()
        seed_env = {"GIT_CONFIG_GLOBAL": str(self.gitconfig("@seed"))}
        _git("init", "-q", "--bare", "-b", "main", str(self.origin), env=seed_env)
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text(HOOK)
        hook.chmod(0o755)
        seed_clone = self.root / "seed-clone"
        _git("clone", "-q", str(self.origin), str(seed_clone), env=seed_env)
        write_tree(seed_clone, read_tree(self.spec.seed))
        _git("add", "-A", cwd=seed_clone, env=seed_env)
        _git("commit", "-q", "-m", f"seed: {self.spec.name}", cwd=seed_clone, env=seed_env)
        _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=seed_clone,
             env={**seed_env, "GUD_EVAL_MERGE": "1", "GUD_EVAL_ROOT": str(self.root)})
        shutil.rmtree(seed_clone)
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        shutil.copy(HERE / "prtool.py", bin_dir / "prtool.py")
        wrapper = bin_dir / "pr"
        wrapper.write_text(f'#!/bin/sh\nexec "{self.python}" "{bin_dir / "prtool.py"}" "$@"\n')
        wrapper.chmod(0o755)
        for handle in self.handles:
            env = self.agent_env(handle)
            _git("clone", "-q", str(self.origin), str(self.workdir(handle, create=False)), env=env)

    def start_services(self, log_dir: Path) -> None:
        pass

    def seed_task(self) -> None:
        pass

    def stop_services(self) -> None:
        pass

    def workdir(self, handle: str, create: bool = True) -> Path:
        path = self.root / "agents" / handle.lstrip("@")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def mcp_config(self, handle: str) -> None:
        return None

    def agent_env(self, handle: str) -> dict[str, str]:
        return {
            "GIT_CONFIG_GLOBAL": str(self.gitconfig(handle)),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GUD_EVAL_ROOT": str(self.root),
            "GUD_EVAL_HANDLE": handle,
            "GUD_EVAL_PYTHON": self.python,
            "GUD_EVAL_TEST_TIMEOUT": str(self.test_timeout),
            "PATH_PREFIX": str(self.root / "bin"),
        }

    def instructions(self, handle: str) -> str:
        work = self.workdir(handle)
        return f"""How this repo works (git with pull requests):
- Your current directory ({work}) is your own git clone of the shared repo (remote "origin", branch main). Your git identity is already set to {handle}.
- main is protected: you cannot push to it. To land work: create a branch (`git switch -c <name>`), commit, `git push -u origin <name>`, then `pr open --title "..."` (from that branch). Another agent must approve it (`pr approve ID`). Then run `pr merge ID`: it lands the PR only if the branch rebases cleanly onto the current main and `python -m pytest -q -x` passes on the result. If it reports a rebase conflict, run `git fetch origin && git rebase origin/main`, resolve, `git push --force-with-lease`, and merge again.
- `pr list` shows open PRs, `pr show ID` shows a PR's diff, approvals and comments, `pr comment ID -m "..."` comments (use it to ask for changes), `pr wait --timeout 60` blocks until a PR or main changes.
- Review the other agents' PRs when they appear: read the diff (and run it if useful), then approve, or comment with a concrete reason.
- Run `python -m pytest -q` locally before opening or merging. Pull main often (`git pull --rebase origin main`).
- To tell the others what you are taking on, say it in PR titles, bodies and comments (there is no chat or issue tracker)."""

    # -- results ---------------------------------------------------------------------------

    def final_tree(self) -> dict[str, str]:
        from specs import read_tree

        tmp = self.root / "final-clone"
        if tmp.exists():
            shutil.rmtree(tmp)
        _git("clone", "-q", "--branch", "main", str(self.origin), str(tmp))
        tree = read_tree(tmp)
        shutil.rmtree(tmp)
        return tree

    def events(self) -> list[dict]:
        rows = []
        path = self.prs / "events.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def prs_state(self) -> list[dict]:
        return sorted((json.loads(p.read_text()) for p in self.prs.glob("PR*.json")),
                      key=lambda pr: int(pr["id"][2:]))

    def metrics(self, wall_s: float, transcripts: dict[str, list[dict]]) -> dict:
        events = self.events()
        prs = self.prs_state()
        by = lambda kind: [e for e in events if e.get("kind") == kind]  # noqa: E731
        merged = by("merged")
        rejected = by("merge_rejected")
        reasons: dict[str, int] = {}
        for e in rejected:
            reasons[e.get("reason", "?")] = reasons.get(e.get("reason", "?"), 0) + 1
        test_runs = by("test_run")
        merged_prs = {pr["id"] for pr in prs if pr["status"] == "merged"}
        merged_authors = {}
        for pr in prs:
            if pr["status"] == "merged":
                merged_authors.setdefault(pr["author"], []).append(pr.get("merged_at") or 0)
        conflicts = []
        for e in rejected:
            if e.get("reason") != "rebase_conflict":
                continue
            ok = e.get("pr") in merged_prs
            conflicts.append({"kind": "merge_rebase_conflict", "pr": e.get("pr"), "by": e.get("by"),
                              "files": e.get("files"),
                              "resolution": "PR merged later" if ok else "not resolved", "resolved": ok})
        local = {}
        push_rejects = {}
        for handle, rows in transcripts.items():
            for row in rows:
                if row["name"] != "Bash":
                    continue
                result = row["result"]
                if re.search(r"^CONFLICT \(", result, re.M):
                    local[handle] = local.get(handle, 0) + 1
                if "[rejected]" in result or "non-fast-forward" in result:
                    push_rejects[handle] = push_rejects.get(handle, 0) + 1
        for handle, count in local.items():
            ok = handle in merged_authors
            for _ in range(count):
                conflicts.append({"kind": "local_conflict", "by": handle,
                                  "resolution": "agent merged a PR afterwards" if ok else "not resolved",
                                  "resolved": ok})
        minutes = max(wall_s, 1e-9) / 60
        n = len(merged)
        return {
            "integrations": n,
            "integrations_per_min": round(n / minutes, 3),
            "test_runs": len(test_runs),
            "test_runs_per_integration": round(len(test_runs) / n, 3) if n else None,
            "conflicts_total": len(conflicts),
            "conflicts_resolved": sum(1 for c in conflicts if c["resolved"]),
            "git": {
                "merges": n,
                "merges_per_min": round(n / minutes, 3),
                "prs_opened": len(by("opened")),
                "prs_closed": len(by("closed")),
                "approvals": len(by("approved")),
                "comments": len(by("commented")),
                "merge_attempts": n + len(rejected),
                "merge_rejections": dict(sorted(reasons.items())),
                "gate_test_runs": len(test_runs),
                "gate_test_failures": sum(1 for e in test_runs if not e.get("passed")),
                "test_runs_per_merge": round(len(test_runs) / n, 3) if n else None,
                "rebased_merges": sum(1 for e in merged if e.get("rebased")),
                "local_conflicts": dict(sorted(local.items())),
                "push_rejections": dict(sorted(push_rejects.items())),
                "direct_main_push_rejected": len(by("direct_main_push_rejected")),
                "first_merge_s": round(min(e["t"] for e in merged) - self._t0, 1) if merged else None,
                "prs": [{"id": pr["id"], "author": pr["author"], "status": pr["status"], "title": pr["title"][:100],
                         "approvals": sorted({a["by"] for a in pr["approvals"]}),
                         "comments": len(pr["comments"]), "merge_attempts": pr.get("merge_attempts", 0)}
                        for pr in prs],
                "conflicts": conflicts,
            },
        }
