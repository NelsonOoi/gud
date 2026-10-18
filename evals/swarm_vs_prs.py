"""The scripted swarm, as mail and as pull requests.

The gud side is the Primitive run of ``scripts/demo.py`` (scripted lead, workers,
and tester). This file re-reads that mailbox and replays the same edits through
local git with the pull-request shape: one branch, one pull request, a review,
a pytest check, and a merge commit. No model is called.

Tokens use the same bill as ``evals.pr_vs_thread``: each agent resends its
system prompt, tool list, and history on every turn, then writes one tool call.
Tokenizer is GPT-5 o200k_base. Seconds on the git side are this machine.
Seconds on the gud side are the wall clock of that Primitive run, passed in
because this process was not the one that sent the mail.

Run from ``gud/``:

    .venv/bin/python -m evals.swarm_vs_prs
"""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gud.mcp_baseline import TOOL_NAMES as GIT_TOOLS
from gud.mcp_baseline import GitTools
from gud.mcp_server import TOOL_NAMES as GUD_TOOLS
from gud.mcp_server import GudTools
from gud.wire import parse_body
from gud.worker import demo_edit

from evals.pr_vs_thread import (
    GIT_INSTRUCTIONS,
    GUD_INSTRUCTIONS,
    PR_TOOLS,
    Transcript,
    _encoder,
    _pr_method,
    tool_list_text,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo_repo"
HOME = ROOT / ".gud-home-primitive-swarm"
REPO = "repo-swarm-20261018@lean-ridge.primitive.email"
# Wall clock of the successful Primitive run of scripts/demo.py on that repo.
GUD_SECONDS = 56.0

OBJECTION = (
    'keep the rate-limit check per user, ratelimit.allow(f"login:{username}"); '
    "one shared key lets one client rate-limit every user"
)


class Sub:
    def __init__(self, title: str):
        self.title = title


def _git(cwd: Path, handle: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    name = handle.lstrip("@")
    proc = subprocess.run(
        [
            "git", "-c", f"user.name={name}", "-c", f"user.email={name}@gud.local",
            "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
            "-c", "core.hooksPath=/dev/null", "-c", "safe.directory=*", *args,
        ],
        cwd=cwd, capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git failed").strip())
    return proc


def _tree(cwd: Path) -> dict[str, str]:
    out = {}
    for path in cwd.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        out[path.relative_to(cwd).as_posix()] = path.read_text(encoding="utf-8")
    return out


def _write(cwd: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        dest = cwd / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")


def _edits(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    """Old/new blocks for the lines that changed. A new file sends the whole text."""
    found = []
    for path, new in after.items():
        old = before.get(path)
        if old is None:
            found.append({"path": path, "old": "", "new": new})
            continue
        if old == new:
            continue
        matcher = difflib.SequenceMatcher(a=old.splitlines(True), b=new.splitlines(True))
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            found.append({
                "path": path,
                "old": "".join(old.splitlines(True)[i1:i2]),
                "new": "".join(new.splitlines(True)[j1:j2]),
            })
    return found


class Hub:
    """A bare origin plus one clone per agent. Pull requests live in memory."""

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        self.clones: dict[str, Path] = {}
        self.prs: list[dict] = []
        self.rejected = 0
        self.rebases = 0
        self.conflicts = 0
        self.ci_runs = 0
        self.ci_failures = 0
        self.roster = {
            "@lead", "@auth-agent", "@billing-agent", "@api-agent",
            "@sub-1", "@sub-2", "@sub-3", "@sub-4", "@tester", "@console", "@nelson",
        }

    def clone(self, handle: str) -> Path:
        dest = self.root / handle.lstrip("@")
        _git(self.root, handle, "clone", str(self.origin), str(dest))
        self.clones[handle] = dest
        return dest

    def fetch(self, handle: str) -> None:
        _git(self.clones[handle], handle, "fetch", "origin")

    def read_main(self, handle: str) -> dict[str, str]:
        self.fetch(handle)
        _git(self.clones[handle], handle, "checkout", "-B", "main", "origin/main")
        _git(self.clones[handle], handle, "reset", "--hard", "origin/main")
        return _tree(self.clones[handle])

    def open_pr(self, handle: str, branch: str, title: str, body: str, files: dict[str, str]) -> dict:
        cwd = self.clones[handle]
        self.fetch(handle)
        _git(cwd, handle, "checkout", "-B", branch, "origin/main")
        _write(cwd, files)
        _git(cwd, handle, "add", "-A")
        _git(cwd, handle, "commit", "-m", title)
        _git(cwd, handle, "push", "-u", "origin", branch)
        diff = _git(cwd, handle, "diff", "origin/main...HEAD").stdout
        number = len(self.prs) + 1
        pr = {
            "number": number, "author": handle, "branch": branch, "title": title,
            "body": body, "diff": diff, "comments": [], "reviews": [], "state": "open",
            "commits": 1,
        }
        self.prs.append(pr)
        return pr

    def push_update(self, handle: str, pr: dict, title: str, files: dict[str, str]) -> str:
        cwd = self.clones[handle]
        _git(cwd, handle, "checkout", pr["branch"])
        _write(cwd, files)
        _git(cwd, handle, "add", "-A")
        status = _git(cwd, handle, "status", "--porcelain").stdout.strip()
        if not status:
            return "unchanged"
        _git(cwd, handle, "commit", "-m", title)
        _git(cwd, handle, "push", "origin", pr["branch"])
        pr["commits"] += 1
        pr["diff"] = _git(cwd, handle, "diff", "origin/main...HEAD").stdout
        pr["state"] = "open"
        return "pushed"

    def update_onto_main(self, handle: str, pr: dict, sub: Sub) -> str:
        """Rebase the branch. A conflict is resolved by re-applying the scripted edit on the tip."""
        cwd = self.clones[handle]
        self.fetch(handle)
        _git(cwd, handle, "checkout", pr["branch"])
        rebasing = _git(cwd, handle, "rebase", "origin/main", check=False)
        self.rebases += 1
        if rebasing.returncode != 0:
            self.conflicts += 1
            _git(cwd, handle, "rebase", "--abort", check=False)
            files = demo_edit(sub, self.read_main(handle), {})
            _git(cwd, handle, "checkout", "-B", pr["branch"], "origin/main")
            _write(cwd, files)
            _git(cwd, handle, "add", "-A")
            _git(cwd, handle, "commit", "-m", pr["title"] + " (rebased)")
            _git(cwd, handle, "push", "--force-with-lease", "origin", pr["branch"])
            pr["commits"] += 1
            pr["diff"] = _git(cwd, handle, "diff", "origin/main...HEAD").stdout
            return "conflict"
        _git(cwd, handle, "push", "--force-with-lease", "origin", pr["branch"])
        pr["diff"] = _git(cwd, handle, "diff", "origin/main...HEAD").stdout
        return "clean"

    def ci(self, pr: dict) -> str:
        cwd = self.clones["@tester"]
        _git(cwd, "@tester", "fetch", "origin")
        _git(cwd, "@tester", "checkout", "--force", "--detach", f"origin/{pr['branch']}")
        _git(cwd, "@tester", "clean", "-fd")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=cwd, capture_output=True, text=True,
        )
        self.ci_runs += 1
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()
        line = tail[-1] if tail else "no output"
        if proc.returncode != 0:
            self.ci_failures += 1
            pr["checks"] = f"pytest fail {line}"
            return pr["checks"]
        pr["checks"] = f"pytest pass {line}"
        return pr["checks"]

    def comment(self, pr: dict, handle: str, body: str) -> None:
        pr["comments"].append({"author": handle, "body": body})
        pr["state"] = "changes"

    def approve(self, pr: dict, handle: str) -> None:
        pr["reviews"].append(handle)
        pr["state"] = "approved"

    def merge(self, pr: dict) -> str:
        if pr["state"] != "approved":
            raise RuntimeError(f"#{pr['number']} merged without approval")
        if not str(pr.get("checks", "")).startswith("pytest pass"):
            raise RuntimeError(f"#{pr['number']} merged with {pr.get('checks')}")
        cwd = self.clones["@lead"]
        self.fetch("@lead")
        _git(cwd, "@lead", "checkout", "-B", "main", "origin/main")
        merged = _git(
            cwd, "@lead", "merge", "--no-ff", "--no-edit",
            "-m", f"Merge pull request #{pr['number']} from {pr['branch']}",
            f"origin/{pr['branch']}", check=False,
        )
        if merged.returncode != 0:
            _git(cwd, "@lead", "merge", "--abort", check=False)
            return "conflict"
        _git(cwd, "@lead", "push", "origin", "main")
        pr["state"] = "merged"
        return "merged"

    def reject_push(self, handle: str) -> str:
        if handle not in self.roster:
            self.rejected += 1
            return f"rejected: {handle} is not a member"
        raise RuntimeError(f"{handle} is still a member")


def _git_tools() -> str:
    methods = [getattr(GitTools, name) for name in GIT_TOOLS]
    methods.extend(_pr_method(name, doc, props) for name, doc, props in PR_TOOLS)
    return tool_list_text(methods)


def _gud_tools() -> str:
    return tool_list_text([getattr(GudTools, name) for name in GUD_TOOLS])


def _agents(handles: list[str], system: str, tools: str, enc, side: str) -> dict[str, Transcript]:
    found = {}
    for handle in handles:
        row = Transcript(system, tools, enc)
        row.who = handle
        row.side = side
        found[handle] = row
    return found


def _mailbox_counts() -> dict[str, int]:
    sys.path.insert(0, str(ROOT))
    from gud.config import load
    from gud.mailbox import open_mailbox
    import os
    os.environ["GUD_HOME"] = str(HOME)
    os.environ["GUD_MAILBOX"] = "primitive"
    os.environ["GUD_DOMAIN"] = "lean-ridge.primitive.email"
    os.environ["GUD_REPO_ADDRESS"] = REPO
    box = open_mailbox(load())
    emails = box.list(recipient=REPO)
    counts: dict[str, int] = {}
    for email in emails:
        kind = parse_body(email.body).kind or "plain"
        counts[kind] = counts.get(kind, 0) + 1
    counts["emails"] = len(emails)
    return counts


def _sum(rows: list[Transcript]) -> tuple[int, int, int]:
    return (
        sum(row.turns for row in rows),
        sum(row.input_tokens for row in rows),
        sum(row.output_tokens for row in rows),
    )


def main() -> None:
    enc = _encoder()
    git_tools, gud_tools = _git_tools(), _gud_tools()
    handles = ["@sub-1", "@sub-2", "@sub-3", "@sub-4", "@auth-agent", "@billing-agent", "@lead", "@nelson", "@tester"]
    git = _agents(handles, GIT_INSTRUCTIONS, git_tools, enc, "pull-request")
    gud = _agents(handles, GUD_INSTRUCTIONS, gud_tools, enc, "gud")

    started = time.perf_counter()
    root = Path(tempfile.mkdtemp(prefix="gud-prs-"))
    try:
        seed = root / "seed"
        shutil.copytree(DEMO, seed, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        members = seed / ".gud" / "members"
        members.parent.mkdir(parents=True, exist_ok=True)
        members.write_text(
            "# handle role address public_key sponsor\n"
            "@lead lead lead@gud.local ed25519:lead @nelson\n"
            "@sub-1 agent sub-1@gud.local ed25519:s1 @nelson\n"
            "@sub-2 agent sub-2@gud.local ed25519:s2 @nelson\n"
            "@sub-3 agent sub-3@gud.local ed25519:s3 @nelson\n"
            "@sub-4 agent sub-4@gud.local ed25519:s4 @nelson\n"
            "@auth-agent agent auth-agent@gud.local ed25519:auth @nelson\n"
            "@billing-agent agent billing-agent@gud.local ed25519:bill @nelson\n"
            "@tester tester tester@gud.local ed25519:test @nelson\n"
            "@nelson human nelson@gud.local - -\n"
        )
        _git(root, "@lead", "init", "-b", "main", str(seed))
        _git(seed, "@lead", "add", "-A")
        _git(seed, "@lead", "commit", "-m", "genesis")
        origin = root / "origin.git"
        _git(root, "@lead", "init", "--bare", "-b", "main", str(origin))
        _git(seed, "@lead", "remote", "add", "origin", str(origin))
        _git(seed, "@lead", "push", "-u", "origin", "main")
        hub = Hub(root)
        hub.origin = origin
        for handle in handles:
            hub.clone(handle)
        clone_seconds = time.perf_counter() - started

        # Four subtasks branch from the same tip, the way the swarm pushes at once.
        tasks = [
            ("@sub-1", "rate-limit-login", "Rate-limit login", Sub("Rate-limit login")),
            ("@sub-2", "bill-overage", "Bill overage in charge", Sub("Bill overage in charge")),
            ("@sub-3", "map-429", "Map 429 and meter charges", Sub("Map 429 and meter charges")),
            ("@sub-4", "penalize", "Add penalize", Sub("Add penalize")),
        ]
        opened = []
        for handle, branch, title, sub in tasks:
            before = hub.read_main(handle)
            files = demo_edit(sub, before, {})
            pr = hub.open_pr(handle, branch, title, title, files)
            opened.append((handle, pr, before, files, sub))

        for handle, pr, before, files, sub in opened:
            git[handle].call("write", {"files": files}, f"wrote {len(files)} files")
            git[handle].call("git_commit", {"message": pr["title"]}, f"committed {pr['title']}")
            git[handle].call("git_push", {}, f"pushed {pr['branch']}")
            git[handle].call(
                "pr_create", {"title": pr["title"], "body": pr["body"]},
                f"created #{pr['number']} {pr['title']}",
            )
            gud[handle].call("read", {"paths": sorted(files)}, "\n".join(f"{path}\n{text}" for path, text in sorted(before.items()) if path in files))
            gud[handle].call("push", {"intent": pr["title"], "edits": _edits(before, files)}, f"pushed {pr['title']}")
            while True:
                checks = hub.ci(pr)
                git[handle].call("pr_checks", {"number": pr["number"]}, checks)
                git["@lead"].call(
                    "pr_diff", {"number": pr["number"]},
                    f"pull request #{pr['number']}\n\n{pr['diff']}",
                )
                hub.approve(pr, "@lead")
                git["@lead"].call("pr_approve", {"number": pr["number"]}, f"approved #{pr['number']}")
                status = hub.merge(pr)
                if status == "merged":
                    git[handle].call("pr_merge", {"number": pr["number"]}, f"merged #{pr['number']}")
                    gud["@lead"].call("reply", {"action": "approve", "target": pr["title"]}, "approve")
                    gud[handle].call("wait", {}, f"landed {pr['title']} {checks}")
                    break
                pr["state"] = "open"
                pr["reviews"] = []
                how = hub.update_onto_main(handle, pr, sub)
                tip = hub.read_main(handle)
                files = demo_edit(sub, tip, {})
                hub.push_update(handle, pr, pr["title"], files)
                git[handle].call("git_pull", {}, f"rebase onto main: {how}")
                git[handle].call("write", {"files": files}, f"rewrote {len(files)} files")
                git[handle].call("git_commit", {"message": pr["title"]}, "committed the rebase")
                git[handle].call("git_push", {}, f"pushed {pr['branch']}")
                gud[handle].call("wait", {}, "superseded")
                gud[handle].call("read", {"paths": sorted(files)}, "tip moved")
                gud[handle].call("push", {"intent": pr["title"], "edits": _edits(tip, files)}, "re-pushed")

        # The lockout rewrites login. Tests pass. The owner objects, then approves the revision.
        author = "@auth-agent"
        before = hub.read_main(author)
        bad = demo_edit(Sub("Add a lockout counter to login"), before, {})
        pr = hub.open_pr(author, "login-lockout", "Add a lockout counter to login", "Lock the account after five failures.", bad)
        checks = hub.ci(pr)
        git[author].call("read", {"path": "auth.py"}, before.get("auth.py", ""))
        git[author].call("write", {"path": "auth.py", "content": bad.get("auth.py", "")}, f"wrote auth.py ({len(bad.get('auth.py', ''))} bytes)")
        git[author].call("git_commit", {"message": pr["title"]}, "committed lockout")
        git[author].call("git_push", {}, "pushed login-lockout")
        git[author].call("pr_create", {"title": pr["title"], "body": pr["body"]}, f"created #{pr['number']} {pr['title']}")
        git["@sub-1"].call("pr_diff", {"number": pr["number"]}, f"pull request #{pr['number']}\n\n{pr['diff']}")
        hub.comment(pr, "@sub-1", OBJECTION)
        git["@sub-1"].call("pr_comment", {"number": pr["number"], "path": "auth.py", "body": OBJECTION}, "comment 1 on auth.py")
        git[author].call("pr_comments", {"number": pr["number"]}, f"comment 1 @sub-1 on auth.py\n{OBJECTION}\nhunk:\n{pr['diff']}")
        revised_files = demo_edit(Sub("Add a lockout counter to login"), before, {}, "objected")
        hub.push_update(author, pr, "Return the per-user rate-limit key", revised_files)
        checks = hub.ci(pr)
        git[author].call("write", {"path": "auth.py", "content": revised_files.get("auth.py", "")}, f"wrote auth.py ({len(revised_files.get('auth.py', ''))} bytes)")
        git[author].call("git_commit", {"message": "keep the per-user key"}, "committed the revision")
        git[author].call("git_push", {}, "pushed login-lockout")
        git[author].call("pr_checks", {"number": pr["number"]}, checks)
        git["@sub-1"].call("pr_diff", {"number": pr["number"]}, f"pull request #{pr['number']}\n\n{pr['diff']}")
        hub.approve(pr, "@sub-1")
        git["@sub-1"].call("pr_approve", {"number": pr["number"]}, f"approved #{pr['number']}")
        hub.merge(pr)
        git[author].call("pr_merge", {"number": pr["number"]}, f"merged #{pr['number']}")
        gud[author].call("read", {"path": "auth.py"}, before.get("auth.py", ""))
        gud[author].call("push", {"intent": "lockout", "edits": _edits(before, bad)}, "pushed lockout")
        gud["@sub-1"].call("thread", {"id": "lockout"}, pr["diff"][:2000])
        gud["@sub-1"].call("reply", {"action": "object", "reason": OBJECTION}, "object")
        gud[author].call("wait", {}, f"objected: {OBJECTION}")
        gud[author].call("read", {"path": "auth.py"}, before.get("auth.py", ""))
        gud[author].call("push", {"intent": "lockout revised", "edits": _edits(before, revised_files)}, "pushed revision")
        gud["@sub-1"].call("reply", {"action": "approve"}, "approve")
        gud[author].call("wait", {}, f"landed revision {checks}")

        # Protected interface. The owner's approval is required before merge.
        author = "@billing-agent"
        before = hub.read_main(author)
        files = demo_edit(Sub("Add RateLimitDecision"), before, {})
        pr = hub.open_pr(author, "rate-limit-decision", "Add RateLimitDecision", "api/interfaces.py is protected.", files)
        checks = hub.ci(pr)
        git[author].call("write", {"path": "api/interfaces.py", "content": files.get("api/interfaces.py", "")}, "wrote api/interfaces.py")
        git[author].call("git_commit", {"message": pr["title"]}, "committed RateLimitDecision")
        git[author].call("git_push", {}, "pushed rate-limit-decision")
        git[author].call("pr_create", {"title": pr["title"], "body": pr["body"]}, f"created #{pr['number']}")
        git[author].call("pr_checks", {"number": pr["number"]}, checks)
        git["@lead"].call("pr_diff", {"number": pr["number"]}, f"pull request #{pr['number']}\n\n{pr['diff']}")
        hub.approve(pr, "@lead")
        git["@lead"].call("pr_approve", {"number": pr["number"]}, f"approved #{pr['number']}")
        hub.merge(pr)
        git[author].call("pr_merge", {"number": pr["number"]}, f"merged #{pr['number']}")
        gud[author].call("push", {"intent": "RateLimitDecision", "edits": _edits(before, files)}, "pushed interface")
        gud["@lead"].call("reply", {"action": "approve", "target": "api/interfaces.py"}, "approve")
        gud[author].call("wait", {}, f"landed interface {checks}")

        # Two pull requests from one tip. The second is behind and updates.
        pr_a = hub.open_pr("@sub-1", "race-a", "Add race_a.py", "race", {"race_a.py": "A = 1\n"})
        pr_b = hub.open_pr("@sub-2", "race-b", "Add race_b.py", "race", {"race_b.py": "B = 1\n"})
        for handle, pr in (("@sub-1", pr_a), ("@sub-2", pr_b)):
            checks = hub.ci(pr)
            git[handle].call("write", {"path": pr["branch"]}, "wrote one new file")
            git[handle].call("git_commit", {"message": pr["title"]}, "committed")
            git[handle].call("git_push", {}, f"pushed {pr['branch']}")
            git[handle].call("pr_create", {"title": pr["title"], "body": "race"}, f"created #{pr['number']}")
            git[handle].call("pr_checks", {"number": pr["number"]}, checks)
            git["@lead"].call("pr_approve", {"number": pr["number"]}, f"approved #{pr['number']}")
            hub.approve(pr, "@lead")
        hub.merge(pr_a)
        git["@sub-1"].call("pr_merge", {"number": pr_a["number"]}, f"merged #{pr_a['number']}")
        how = hub.update_onto_main("@sub-2", pr_b, Sub("race"))
        hub.ci(pr_b)
        git["@sub-2"].call("git_pull", {}, f"branch is behind: {how}")
        git["@sub-2"].call("git_push", {}, "pushed race-b")
        git["@sub-2"].call("pr_checks", {"number": pr_b["number"]}, pr_b["checks"])
        hub.merge(pr_b)
        git["@sub-2"].call("pr_merge", {"number": pr_b["number"]}, f"merged #{pr_b['number']}")
        gud["@sub-1"].call("push", {"intent": "race_a.py", "edits": [{"path": "race_a.py", "old": "", "new": "A = 1\n"}]}, "pushed race_a")
        gud["@sub-2"].call("push", {"intent": "race_b.py", "edits": [{"path": "race_b.py", "old": "", "new": "B = 1\n"}]}, "pushed race_b")
        gud["@sub-1"].call("wait", {}, "landed race_a")
        gud["@sub-2"].call("wait", {}, "superseded")
        gud["@sub-2"].call("push", {"intent": "race_b.py", "edits": [{"path": "race_b.py", "old": "", "new": "B = 1\n"}]}, "re-pushed race_b")
        gud["@sub-2"].call("wait", {}, "landed race_b")

        # Membership is a pull request a human has to approve.
        current = hub.read_main("@lead")[".gud/members"]
        added = current + "@guest-agent agent guest-agent@gud.local ed25519:guest @nelson\n"
        pr = hub.open_pr("@lead", "add-guest", "Add @guest-agent", "A human approves membership.", {".gud/members": added})
        checks = hub.ci(pr)
        git["@lead"].call("write", {"path": ".gud/members", "content": added}, "wrote .gud/members")
        git["@lead"].call("git_commit", {"message": "Add @guest-agent"}, "committed")
        git["@lead"].call("git_push", {}, "pushed add-guest")
        git["@lead"].call("pr_create", {"title": pr["title"], "body": pr["body"]}, f"created #{pr['number']}")
        git["@lead"].call("pr_checks", {"number": pr["number"]}, checks)
        git["@nelson"].call("pr_diff", {"number": pr["number"]}, pr["diff"])
        hub.approve(pr, "@nelson")
        git["@nelson"].call("pr_approve", {"number": pr["number"]}, f"approved #{pr['number']}")
        hub.merge(pr)
        git["@lead"].call("pr_merge", {"number": pr["number"]}, f"merged #{pr['number']}")
        removed = "\n".join(line for line in added.splitlines(True) if not line.startswith("@sub-4 "))
        pr = hub.open_pr("@lead", "remove-sub-4", "Remove @sub-4", "A human approves membership.", {".gud/members": removed})
        checks = hub.ci(pr)
        git["@lead"].call("write", {"path": ".gud/members", "content": removed}, "wrote .gud/members")
        git["@lead"].call("git_commit", {"message": "Remove @sub-4"}, "committed")
        git["@lead"].call("git_push", {}, "pushed remove-sub-4")
        git["@lead"].call("pr_create", {"title": pr["title"], "body": pr["body"]}, f"created #{pr['number']}")
        git["@lead"].call("pr_checks", {"number": pr["number"]}, checks)
        git["@nelson"].call("pr_diff", {"number": pr["number"]}, pr["diff"])
        hub.approve(pr, "@nelson")
        git["@nelson"].call("pr_approve", {"number": pr["number"]}, f"approved #{pr['number']}")
        hub.merge(pr)
        git["@lead"].call("pr_merge", {"number": pr["number"]}, f"merged #{pr['number']}")
        hub.roster.discard("@sub-4")
        rejected = hub.reject_push("@sub-4")
        git["@sub-4"].call("git_push", {}, rejected)
        gud["@lead"].call("push", {"intent": "add @guest-agent", "edits": _edits({".gud/members": current}, {".gud/members": added})}, "proposed add")
        gud["@nelson"].call("reply", {"action": "approve", "target": "members"}, "approve")
        gud["@lead"].call("wait", {}, f"landed add {checks}")
        gud["@lead"].call("push", {"intent": "remove @sub-4", "edits": _edits({".gud/members": added}, {".gud/members": removed})}, "proposed remove")
        gud["@nelson"].call("reply", {"action": "approve", "target": "members"}, "approve")
        gud["@lead"].call("wait", {}, "landed remove")
        gud["@sub-4"].call("push", {"intent": "ghost"}, "rejected: author is not a member")

        git_seconds = time.perf_counter() - started
        merged = [pr for pr in hub.prs if pr["state"] == "merged"]
        comments = sum(len(pr["comments"]) for pr in hub.prs)
        reviews = sum(len(pr["reviews"]) for pr in hub.prs)
        commits = sum(pr["commits"] for pr in hub.prs)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    mail = _mailbox_counts()
    git_turns, git_in, git_out = _sum(list(git.values()))
    gud_turns, gud_in, gud_out = _sum(list(gud.values()))

    def line(label: str, a: str, b: str) -> str:
        return f"{label:<28} {a:>14} {b:>14}"

    print(line("", "pull requests", "gud email"))
    print(line("wall seconds", f"{git_seconds:.1f}", f"{GUD_SECONDS:.1f}"))
    print(line("  of which clone", f"{clone_seconds:.1f}", "0"))
    print(line("turns", str(git_turns), str(gud_turns)))
    print(line("input tokens", f"{git_in:,}", f"{gud_in:,}"))
    print(line("output tokens", f"{git_out:,}", f"{gud_out:,}"))
    print(line("total tokens", f"{git_in + git_out:,}", f"{gud_in + gud_out:,}"))
    print()
    print(f"pull requests opened {len(hub.prs)}, merged {len(merged)}, review comments {comments}, approvals {reviews}")
    print(f"commits on those branches {commits}, rebases {hub.rebases}, content conflicts {hub.conflicts}")
    print(f"pytest runs {hub.ci_runs}, failures {hub.ci_failures}, rejected pushes {hub.rejected}")
    print(f"clones {len(handles)}")
    print()
    print(f"primitive mailbox {REPO}")
    print(f"emails {mail.get('emails', 0)} in {GUD_SECONDS:.1f}s")
    for kind in sorted(k for k in mail if k != "emails"):
        print(f"  {kind:<16} {mail[kind]}")
    print()
    print("Same scripted edits. GitHub shape: branch, pull request, review, pytest, merge commit.")
    print("A pull request does not merge without an approval and a passing check.")
    print("The lockout needs a change request. Membership needs @nelson. The removed agent's push is rejected.")
    print("No model was called. gud seconds are the Primitive run, not this process.")


if __name__ == "__main__":
    main()
