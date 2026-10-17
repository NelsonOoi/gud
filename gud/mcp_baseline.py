"""Git-style baseline with the same job as the gud server: several agents, one repo.

Each agent gets a working copy of a shared bare origin and the tools agents
already reach for (status, pull, write, commit, push, resolve). Push is rejected
on a non-fast-forward; pull merges and can conflict. Nothing here is weakened
so a comparison with gud stays fair.

Run: ``python -m gud.mcp_baseline --workdir PATH --handle @alice``
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from gud.analytics import instrument, tracked, unwrap

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp>=2 renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

TOOL_NAMES = (
    "git_status", "git_pull", "read", "write", "git_commit", "git_push", "resolve", "log",
)

INSTRUCTIONS = """You are {handle} in a shared git repo. Other agents push to the same origin.

Write files, commit, and push. If push is rejected (non-fast-forward), git_pull. If pull reports a conflict, resolve the file with the full text you want, commit, and push again. Do not force-push.
"""

_GIT = (
    "git",
    "-c", "user.name={name}",
    "-c", "user.email={email}",
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "safe.directory=*",
    "-c", "init.defaultBranch=main",
    "-c", "core.hooksPath=/dev/null",
)


def _git_cmd(name: str, email: str) -> list[str]:
    return [part.format(name=name, email=email) for part in _GIT]


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git failed").strip()
        raise RuntimeError(detail)
    return proc


def make_origin(path: Path, files: dict[str, str], *, branch: str = "main") -> Path:
    """Create a bare origin containing one commit of ``files``. Returns the origin path."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    seed, origin = path / "seed", path / "origin.git"
    seed.mkdir()
    for rel, text in files.items():
        dest = seed / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    base = _git_cmd("gud", "gud@gud.local")
    _run([*base, "init", "-b", branch, str(seed)])
    _run([*base, "-C", str(seed), "add", "-A"])
    _run([*base, "-C", str(seed), "commit", "-m", "genesis"])
    _run([*base, "init", "--bare", "-b", branch, str(origin)])
    _run([*base, "-C", str(seed), "remote", "add", "origin", str(origin)])
    _run([*base, "-C", str(seed), "push", "-u", "origin", branch])
    return origin


def clone(origin: Path, dest: Path, handle: str) -> "GitTools":
    _run(["git", "-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null", "clone", str(origin), str(dest)])
    return GitTools(dest, handle)


class GitTools:
    """One agent's git working copy. ``handle`` is the committer identity."""

    def __init__(self, workdir: Path | str, handle: str = "@agent"):
        self.workdir = Path(workdir)
        self.handle = handle if handle.startswith("@") else f"@{handle}"
        self.email = f"{self.handle.lstrip('@')}@gud.local"

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [*_git_cmd(self.handle, self.email), "-C", str(self.workdir), *args]
        return _run(cmd, check=check)

    def _resolve(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ValueError("empty or invalid path")
        raw = path.strip()
        if raw.startswith("/") or raw.startswith("~") or ".." in raw.replace("\\", "/").split("/"):
            raise ValueError("path escapes the worktree")
        dest = (self.workdir / raw).resolve()
        root = self.workdir.resolve()
        if dest != root and root not in dest.parents:
            raise ValueError("path escapes the worktree")
        return dest

    def _unmerged(self) -> list[str]:
        proc = self._git("diff", "--name-only", "--diff-filter=U", check=False)
        return [line for line in proc.stdout.splitlines() if line.strip()]

    @tracked("git")
    def git_status(self) -> str:
        """Short status: branch tracking line plus any staged, unstaged, or unmerged paths."""
        proc = self._git("status", "-sb")
        return proc.stdout.strip() or "## clean"

    @tracked("git")
    def git_pull(self) -> str:
        """Fetch origin and merge origin/main. A clean merge is committed for you.

        If both sides edited the same lines, returns ``conflict: <paths>``. Fix each with resolve, then git_commit and git_push.
        """
        fetched = self._git("fetch", "origin", check=False)
        if fetched.returncode != 0:
            return "error: fetch failed\n" + (fetched.stderr or fetched.stdout).strip()
        merged = self._git("merge", "--no-edit", "origin/main", check=False)
        text = (merged.stdout + "\n" + merged.stderr).strip()
        if merged.returncode == 0:
            return "updated\n" + (text or "already up to date")
        bad = self._unmerged()
        if bad:
            return "conflict: " + ", ".join(bad) + "\n" + text
        return "error: merge failed\n" + text

    @tracked("git")
    def read(self, path: str) -> str:
        """Read a file from your working copy."""
        try:
            dest = self._resolve(path)
        except ValueError as exc:
            return f"error: {exc}"
        if not dest.is_file():
            return f"error: no such file {path}"
        return dest.read_text(encoding="utf-8")

    @tracked("git")
    def write(self, path: str, content: str) -> str:
        """Write the full text of a file in your working copy. Does not commit."""
        if content is None or not isinstance(content, str):
            return "error: content must be text"
        if "\x00" in content:
            return "error: binary content is not allowed"
        try:
            dest = self._resolve(path)
        except ValueError as exc:
            return f"error: {exc}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return f"wrote {path} ({len(content)} bytes)"

    @tracked("git")
    def git_commit(self, message: str) -> str:
        """Stage every change and commit with message. Fails when there is nothing to commit."""
        if not str(message).strip():
            return "error: empty commit message"
        self._git("add", "-A")
        status = self._git("status", "--porcelain")
        if not status.stdout.strip():
            return "error: nothing to commit"
        self._git("commit", "-m", message)
        rev = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        return f"committed {rev} {message}"

    @tracked("git")
    def git_push(self) -> str:
        """Push HEAD to origin's main branch. Rejected when the remote has commits you do not have (non-fast-forward)."""
        proc = self._git("push", "origin", "HEAD:main", check=False)
        if proc.returncode == 0:
            rev = self._git("rev-parse", "--short", "HEAD").stdout.strip()
            return f"pushed {rev}"
        detail = (proc.stderr or proc.stdout).strip()
        lowered = detail.lower()
        if "non-fast-forward" in lowered or "rejected" in lowered or "failed to push" in lowered:
            return "rejected: non-fast-forward\n" + detail
        return "error: push failed\n" + detail

    @tracked("git")
    def resolve(self, path: str, content: str) -> str:
        """Replace a conflicted file with the full resolved text and stage it. Then git_commit and git_push."""
        wrote = self.write(path, content)
        if wrote.startswith("error:"):
            return wrote
        self._git("add", "--", path)
        return f"resolved {path}"

    @tracked("git")
    def log(self, limit: int = 20) -> str:
        """Recent commits, newest first, one line each."""
        proc = self._git("log", f"-n{int(limit)}", "--oneline")
        return proc.stdout.strip() or "(no commits)"


def build_server(tools: GitTools):
    """FastMCP server for one agent's git working copy."""
    mcp = FastMCP("git", instructions=INSTRUCTIONS.format(handle=tools.handle))
    hooked = instrument(mcp, actor=tools.handle)
    for name in TOOL_NAMES:
        method = getattr(tools, name)
        if hooked is not None:
            method = unwrap(method)
        mcp.tool(name=name, structured_output=False)(method)
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gud.mcp_baseline", description="git baseline MCP server (stdio)")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--handle", default=os.environ.get("GUD_HANDLE", "@agent"))
    args = parser.parse_args(argv)
    build_server(GitTools(args.workdir, args.handle)).run("stdio")


if __name__ == "__main__":
    main()
