#!/usr/bin/env python3
"""``pr``: a minimal local pull-request protocol for the git arm.

    pr open --title "add pricing page" [--branch BRANCH] [--body TEXT]
    pr list [--all]
    pr show ID
    pr approve ID [-m MESSAGE]
    pr comment ID -m MESSAGE
    pr merge ID
    pr close ID
    pr wait [--timeout SECONDS]

State lives next to the shared bare repo, outside every clone: ``$GUD_EVAL_ROOT/prs/<ID>.json`` plus an
append-only ``events.jsonl``. ``main`` is protected by a pre-receive hook; only ``pr merge`` can move it.

``pr merge`` holds one lock for the whole repo and requires, in order:
  1. the PR is open and has an approval from an agent other than its author (approvals persist across
     later pushes to the branch, as on GitHub by default);
  2. the branch rebases cleanly onto the current main (otherwise: rebase locally, resolve, force-push, retry);
  3. ``python -m pytest -q -x`` passes on the rebased tree (the same command the gud tester runs).
Then main is fast-forwarded to the rebased commits and the branch is updated to match.

Identity comes from ``$GUD_EVAL_HANDLE``. Stdlib only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get("GUD_EVAL_ROOT", ".")).resolve()
ORIGIN = ROOT / "origin.git"
PRS = ROOT / "prs"
EVENTS = PRS / "events.jsonl"
HANDLE = os.environ.get("GUD_EVAL_HANDLE", "@unknown")
PYTHON = os.environ.get("GUD_EVAL_PYTHON", sys.executable)
TEST_TIMEOUT = float(os.environ.get("GUD_EVAL_TEST_TIMEOUT", "120"))


def git(*args: str, cwd: Path | None = None, env_extra: dict | None = None, check: bool = False):
    env = dict(os.environ)
    env.update({"GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0"})
    env.update(env_extra or {})
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}")
    return proc


def origin(*args: str, check: bool = False):
    return git("--git-dir", str(ORIGIN), *args, check=check)


def sha_of(ref: str) -> str | None:
    proc = origin("rev-parse", "--verify", "-q", ref)
    return proc.stdout.strip() or None


@contextmanager
def locked():
    PRS.mkdir(parents=True, exist_ok=True)
    with open(PRS / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def event(kind: str, **fields) -> None:
    row = {"t": round(time.time(), 3), "kind": kind, "by": HANDLE, **fields}
    with open(EVENTS, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(row) + "\n")
        fh.flush()
        fcntl.flock(fh, fcntl.LOCK_UN)


def pr_path(pid: str) -> Path:
    return PRS / f"{pid}.json"


def norm_id(raw: str) -> str:
    raw = raw.strip().upper()
    if raw.startswith("#"):
        raw = raw[1:]
    return raw if raw.startswith("PR") else f"PR{raw}"


def load(pid: str) -> dict:
    path = pr_path(norm_id(pid))
    if not path.exists():
        raise SystemExit(f"no such PR {pid} (see `pr list`)")
    return json.loads(path.read_text())


def save(pr: dict) -> None:
    path = pr_path(pr["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pr, indent=2))
    tmp.replace(path)


def all_prs() -> list[dict]:
    rows = [json.loads(p.read_text()) for p in PRS.glob("PR*.json")]
    return sorted(rows, key=lambda pr: int(pr["id"][2:]))


def current_branch() -> str:
    proc = git("rev-parse", "--abbrev-ref", "HEAD")
    return proc.stdout.strip()


# -- commands ----------------------------------------------------------------------------------

def cmd_open(args) -> int:
    branch = args.branch or current_branch()
    if not branch or branch in {"HEAD", "main"}:
        print("error: open a PR from a branch other than main (git switch -c my-branch; git push -u origin my-branch)")
        return 1
    if sha_of(f"refs/heads/{branch}") is None:
        print(f"error: branch {branch} is not on origin yet; run `git push -u origin {branch}` first")
        return 1
    with locked():
        for pr in all_prs():
            if pr["branch"] == branch and pr["status"] == "open":
                print(f"error: {pr['id']} is already open for {branch}")
                return 1
        n = 1 + max((int(pr["id"][2:]) for pr in all_prs()), default=0)
        pr = {"id": f"PR{n}", "author": HANDLE, "branch": branch, "title": args.title, "body": args.body or "",
              "status": "open", "created": time.time(), "approvals": [], "comments": [], "merge_attempts": 0}
        save(pr)
        event("opened", pr=pr["id"], branch=branch, title=args.title)
    print(f"opened {pr['id']} {branch}: {args.title}")
    print(f"next: another agent approves with `pr approve {pr['id']}`, then `pr merge {pr['id']}`")
    return 0


def _line(pr: dict) -> str:
    approvers = sorted({a["by"] for a in pr["approvals"]})
    head = sha_of(f"refs/heads/{pr['branch']}") or "?"
    return (f"{pr['id']} [{pr['status']}] {pr['author']} {pr['branch']}@{head[:8]} "
            f"approvals={','.join(approvers) or '-'} comments={len(pr['comments'])} :: {pr['title']}")


def cmd_list(args) -> int:
    rows = [pr for pr in all_prs() if args.all or pr["status"] == "open"]
    main = sha_of("refs/heads/main") or "?"
    print(f"main@{main[:8]}")
    if not rows:
        print("(no open PRs)" if not args.all else "(no PRs)")
    for pr in rows:
        print(_line(pr))
    return 0


def cmd_show(args) -> int:
    pr = load(args.id)
    print(_line(pr))
    if pr.get("body"):
        print(pr["body"])
    for a in pr["approvals"]:
        print(f"  approved by {a['by']}: {a.get('message', '')}")
    for c in pr["comments"]:
        print(f"  comment by {c['by']}: {c['message']}")
    proc = origin("diff", f"refs/heads/main...refs/heads/{pr['branch']}")
    diff = proc.stdout
    if len(diff) > 30000:
        diff = diff[:30000] + "\n... (diff truncated)"
    print(diff or "(no diff against main)")
    return 0


def cmd_approve(args) -> int:
    with locked():
        pr = load(args.id)
        if pr["status"] != "open":
            print(f"error: {pr['id']} is {pr['status']}")
            return 1
        if pr["author"] == HANDLE:
            print("error: you cannot approve your own PR")
            return 1
        head = sha_of(f"refs/heads/{pr['branch']}")
        pr["approvals"].append({"by": HANDLE, "sha": head, "message": args.message or "", "t": time.time()})
        save(pr)
        event("approved", pr=pr["id"], sha=head)
    print(f"approved {pr['id']}")
    return 0


def cmd_comment(args) -> int:
    with locked():
        pr = load(args.id)
        pr["comments"].append({"by": HANDLE, "message": args.message, "t": time.time()})
        save(pr)
        event("commented", pr=pr["id"], message=args.message[:300])
    print(f"commented on {pr['id']}")
    return 0


def cmd_close(args) -> int:
    with locked():
        pr = load(args.id)
        if pr["status"] != "open":
            print(f"error: {pr['id']} is {pr['status']}")
            return 1
        pr["status"] = "closed"
        save(pr)
        event("closed", pr=pr["id"])
    print(f"closed {pr['id']}")
    return 0


def _tests(tree: Path) -> tuple[bool, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run([PYTHON, "-m", "pytest", "-q", "-x"], cwd=tree, env=env,
                              capture_output=True, text=True, timeout=TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]


def cmd_merge(args) -> int:
    with locked():
        pr = load(args.id)
        if pr["status"] != "open":
            print(f"error: {pr['id']} is {pr['status']}")
            return 1
        pr["merge_attempts"] = pr.get("merge_attempts", 0) + 1
        save(pr)
        others = sorted({a["by"] for a in pr["approvals"] if a["by"] != pr["author"]})
        if not others:
            event("merge_rejected", pr=pr["id"], reason="no_approval")
            print(f"rejected: {pr['id']} needs an approval from another agent (`pr approve {pr['id']}`)")
            return 1
        branch = pr["branch"]
        if sha_of(f"refs/heads/{branch}") is None:
            event("merge_rejected", pr=pr["id"], reason="no_branch")
            print(f"rejected: branch {branch} is not on origin")
            return 1
        tmp = Path(tempfile.mkdtemp(prefix="merge-", dir=ROOT / "merge"))
        try:
            git("clone", "-q", str(ORIGIN), str(tmp), check=True)
            git("checkout", "-q", "-B", "work", f"origin/{branch}", cwd=tmp, check=True)
            base = git("rev-parse", "origin/main", cwd=tmp).stdout.strip()
            rebase = git("rebase", "origin/main", cwd=tmp)
            if rebase.returncode != 0:
                files = git("diff", "--name-only", "--diff-filter=U", cwd=tmp).stdout.split()
                git("rebase", "--abort", cwd=tmp)
                event("merge_rejected", pr=pr["id"], reason="rebase_conflict", files=files, main=base)
                print(f"rejected: {branch} does not rebase cleanly onto main ({', '.join(files) or 'conflict'}).")
                print(f"Fix: git fetch origin && git rebase origin/main, resolve, `git push --force-with-lease`, "
                      f"then `pr merge {pr['id']}` again.")
                return 1
            passed, log = _tests(tmp)
            event("test_run", pr=pr["id"], passed=passed)
            if not passed:
                event("merge_rejected", pr=pr["id"], reason="tests_failed")
                print(f"rejected: tests failed on {branch} rebased onto main:\n{log}")
                return 1
            head = git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
            push = git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=tmp, env_extra={"GUD_EVAL_MERGE": "1"})
            if push.returncode != 0:
                event("merge_rejected", pr=pr["id"], reason="push_failed", detail=push.stderr[-500:])
                print(f"rejected: could not update main:\n{push.stderr}")
                return 1
            git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}", cwd=tmp)
            pr.update(status="merged", merged_sha=head, merged_at=time.time(), merged_by=HANDLE)
            save(pr)
            event("merged", pr=pr["id"], sha=head, author=pr["author"], rebased=head != base)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"merged {pr['id']} into main at {head[:8]}. Update your clone: git pull --rebase origin main")
    return 0


def _view() -> str:
    rows = [(pr["id"], pr["status"], len(pr["approvals"]), len(pr["comments"]),
             sha_of(f"refs/heads/{pr['branch']}")) for pr in all_prs()]
    return json.dumps([rows, sha_of("refs/heads/main")])


def cmd_wait(args) -> int:
    start = _view()
    deadline = time.time() + max(1.0, min(float(args.timeout), 600.0))
    while time.time() < deadline:
        time.sleep(1.0)
        if _view() != start:
            print("changed:")
            return cmd_list(argparse.Namespace(all=False))
    print("timeout: nothing changed")
    return cmd_list(argparse.Namespace(all=False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pr", description="local pull requests for the shared repo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open")
    p.add_argument("--title", "-t", required=True)
    p.add_argument("--branch", "-b")
    p.add_argument("--body")
    p.set_defaults(fn=cmd_open)
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_list)
    for name, fn in (("show", cmd_show), ("merge", cmd_merge), ("close", cmd_close)):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.set_defaults(fn=fn)
    p = sub.add_parser("approve")
    p.add_argument("id")
    p.add_argument("-m", "--message")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("comment")
    p.add_argument("id")
    p.add_argument("-m", "--message", required=True)
    p.set_defaults(fn=cmd_comment)
    p = sub.add_parser("wait")
    p.add_argument("--timeout", default="60")
    p.set_defaults(fn=cmd_wait)
    args = ap.parse_args(argv)
    PRS.mkdir(parents=True, exist_ok=True)
    (ROOT / "merge").mkdir(parents=True, exist_ok=True)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
