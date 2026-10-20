"""A deterministic stand-in for ``claude -p ... --output-format stream-json`` (no model).

It takes the same argv the harness gives Claude Code and prints the same kinds of stream-json events
(init, assistant with usage and tool_use, user with tool_result, result with total_cost_usd).

FAKE_AGENT_MODE:
  work      (default) do the task: write this agent's share of FAKE_FILES_DIR (files are dealt round-robin
            across agents), plus, with FAKE_CONFLICT=1, a line in README.md at the same place as every other
            agent. git arm: branch, commit, push, `pr open`, approve others, `pr merge`, rebase on conflict.
            gud arm: export, push via the gud tools, wait, rebase or re-push when it loses a race.
  spend     burn reported cost forever (budget-kill test)
  sleep     do nothing until killed (timeout test)
  authfail  print Claude Code's expired-OAuth events and exit 1
  nomcp     report the MCP server as failed, then sleep
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}
MODEL = "claude-sonnet-5-5"
STEP_COST = (1000 * 2.0 + 200 * 10.0) / 1_000_000


class Out:
    def __init__(self):
        self.messages = 0
        self.session = str(uuid.uuid4())

    def emit(self, event: dict) -> None:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

    def init(self, mcp_status: str | None) -> None:
        servers = [] if mcp_status is None else [{"name": "gud", "status": mcp_status, "source": "dynamic"}]
        self.emit({"type": "system", "subtype": "init", "model": MODEL, "session_id": self.session,
                   "tools": ["Bash", "Read"], "mcp_servers": servers})

    def tool(self, name: str, tool_input: dict, result: str) -> None:
        self.messages += 1
        tid = f"toolu_{self.messages}"
        self.emit({"type": "assistant", "message": {
            "id": f"msg_{self.messages}", "model": MODEL, "role": "assistant", "usage": USAGE,
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}})
        self.emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result}]}})

    def text(self, text: str) -> None:
        self.messages += 1
        self.emit({"type": "assistant", "message": {
            "id": f"msg_{self.messages}", "model": MODEL, "role": "assistant", "usage": USAGE,
            "content": [{"type": "text", "text": text}]}})

    def result(self, text: str, is_error: bool = False) -> None:
        self.emit({"type": "result", "subtype": "success", "is_error": is_error, "num_turns": self.messages,
                   "result": text, "total_cost_usd": round(self.messages * STEP_COST, 6),
                   "modelUsage": {MODEL: {"inputTokens": 1000 * self.messages, "outputTokens": 200 * self.messages,
                                          "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}})


def args_of(argv: list[str]) -> dict:
    out = {}
    for flag in ("-p", "--mcp-config", "--model", "--max-budget-usd"):
        if flag in argv:
            out[flag] = argv[argv.index(flag) + 1]
    return out


def my_files(handle: str) -> dict[str, str]:
    src = os.environ.get("FAKE_FILES_DIR")
    n = int(os.environ.get("FAKE_AGENTS", "1"))
    idx = int(handle.lstrip("@a")) - 1
    files = {}
    if src:
        paths = sorted(p for p in Path(src).rglob("*") if p.is_file())
        for i, path in enumerate(paths):
            if i % n == idx:
                files[path.relative_to(src).as_posix()] = path.read_text()
    return files


def sh(out: Out, command: str, cwd: Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
    out.tool("Bash", {"command": command}, (proc.stdout + proc.stderr)[-3000:])
    return proc


# -- git arm --------------------------------------------------------------------------------------

def git_work(out: Out, handle: str) -> str:
    cwd = Path.cwd()
    name = handle.lstrip("@")
    branch = f"work-{name}"
    sh(out, f"git switch -q -c {branch}", cwd)
    for rel, text in my_files(handle).items():
        (cwd / rel).parent.mkdir(parents=True, exist_ok=True)
        (cwd / rel).write_text(text)
    if os.environ.get("FAKE_CONFLICT") == "1":
        readme = cwd / "README.md"
        readme.write_text(readme.read_text() + f"Owner note from {handle}.\n")
    sh(out, f"git add -A && git commit -q -m 'work from {handle}'", cwd)
    sh(out, f"git push -q -u origin {branch}", cwd)
    proc = sh(out, f"pr open --title 'work from {handle}'", cwd)
    mine = re.search(r"opened (PR\d+)", proc.stdout).group(1)
    deadline = time.time() + float(os.environ.get("FAKE_DEADLINE", "60"))
    merged = False
    approved: set[str] = set()
    while time.time() < deadline:
        listing = sh(out, "pr list --all", cwd).stdout
        for pid, status, author in re.findall(r"^(PR\d+) \[(\w+)\] (@\S+)", listing, re.M):
            if status == "open" and author != handle and pid not in approved:
                sh(out, f"pr approve {pid} -m lgtm", cwd)
                approved.add(pid)
        if not merged:
            res = sh(out, f"pr merge {mine}", cwd)
            if res.returncode == 0:
                merged = True
            elif "does not rebase cleanly" in res.stdout:
                sh(out, "git fetch -q origin", cwd)
                reb = sh(out, "git rebase origin/main", cwd)
                if reb.returncode != 0:
                    _union_resolve(cwd / "README.md")
                    sh(out, "git add README.md && git -c core.editor=true rebase --continue", cwd)
                sh(out, f"git push -q --force-with-lease origin {branch}", cwd)
        others_open = [m for m in re.findall(r"^(PR\d+) \[open\] (@\S+)", listing, re.M) if m[1] != handle]
        if merged and not others_open:
            return f"merged {mine}"
        time.sleep(0.5)
    return f"gave up ({'merged' if merged else 'not merged'})"


def _union_resolve(path: Path) -> None:
    keep = []
    for line in path.read_text().splitlines(keepends=True):
        if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
            continue
        keep.append(line)
    path.write_text("".join(keep))


# -- gud arm --------------------------------------------------------------------------------------

def gud_work(out: Out, handle: str, prompt: str, mcp_path: str) -> str:
    cfg = json.loads(Path(mcp_path).read_text())["mcpServers"]["gud"]
    for key, value in cfg["env"].items():
        os.environ[key] = value
    sys.path.insert(0, cfg["env"]["PYTHONPATH"])
    from gud.config import load
    from gud.mailbox import open_mailbox
    from gud.mcp_server import GudTools

    key = re.search(r'private_key "([^"]+)"', prompt).group(1)
    conf = load()
    tools = GudTools(open_mailbox(conf), window_seconds=conf.window_seconds)

    def call(name: str, **kw) -> str:
        result = getattr(tools, name)(**kw)
        out.tool(f"mcp__gud__{name}", kw if name not in {"push", "reply", "rebase"} else {**kw, "private_key": "***"},
                 result)
        return result

    call("tasks")
    call("export", dir=str(Path.cwd() / "tip1"))
    edits = [{"path": rel, "new": text} for rel, text in my_files(handle).items()]
    if os.environ.get("FAKE_CONFLICT") == "1":
        readme = tools.read("README.md")
        edits.append({"path": "README.md", "old": readme, "new": readme + f"Owner note from {handle}.\n"})
    res = call("push", intent=f"work from {handle}", edits=edits, task="T1", handle=handle, private_key=key)
    push_id = res.splitlines()[0]
    deadline = time.time() + float(os.environ.get("FAKE_DEADLINE", "60"))
    while time.time() < deadline:
        res = call("wait", push_id=push_id, timeout=5, handle=handle)
        if res.startswith("landed"):
            return f"landed {push_id}"
        if res.startswith("needs-you"):
            call("reply", target=res.split()[1], action="approve", handle=handle, private_key=key)
            continue
        if res.split(" ", 1)[0] in {"superseded", "stale"}:
            res = call("rebase", push_id=push_id, handle=handle, private_key=key)
            if res.startswith("conflict"):
                readme = tools.read("README.md")
                have = set(tools.ls().splitlines())
                edits = [{"path": rel, "new": text} for rel, text in my_files(handle).items() if rel not in have]
                edits.append({"path": "README.md", "old": readme, "new": readme + f"Owner note from {handle}.\n"})
                res = call("push", intent=f"redo from {handle}", edits=edits, task="T1", handle=handle,
                           private_key=key)
            push_id = res.splitlines()[0]
    return "gave up"


# -- gud v1 arm (gud1-cc) ---------------------------------------------------------------------------

def gud1_work(out: Out, handle: str, mcp_path: str) -> str:
    """Claim T1 (the first claimant wins; the rest work unclaimed), check inflight, write this agent's files
    into the working copy, push, and wait. When the push is superseded (another agent's link touched
    README.md), restore README.md to the base, sync, re-apply the note and push again."""
    cfg = json.loads(Path(mcp_path).read_text())["mcpServers"]["gud"]
    args = cfg["args"]
    sys.path.insert(0, cfg["env"]["PYTHONPATH"])
    from gud.mcp_v1 import GudTools, open_member

    client, wc_root = open_member(args[args.index("--home") + 1], handle, args[args.index("--wc") + 1])
    tools = GudTools(client, wc_root, poll_s=0.3)
    cwd = Path.cwd()

    def call(name: str, **kw) -> str:
        result = getattr(tools, name)(**kw)
        out.tool(f"mcp__gud__{name}", kw, result)
        return result

    files = my_files(handle)
    conflict = os.environ.get("FAKE_CONFLICT") == "1"
    call("tasks")
    call("claim", task="T1", files=sorted(files) + (["README.md"] if conflict else []))
    call("inflight")
    out.tool("Read", {"file_path": str(cwd / "README.md")}, (cwd / "README.md").read_text())

    def apply() -> None:
        for rel, text in files.items():
            (cwd / rel).parent.mkdir(parents=True, exist_ok=True)
            (cwd / rel).write_text(text)
            out.tool("Write", {"file_path": str(cwd / rel), "content": "..."}, "ok")
        if conflict:
            readme = cwd / "README.md"
            readme.write_text(readme.read_text() + f"Owner note from {handle}.\n")

    def redo() -> None:
        (cwd / "README.md").write_text(call("read", path="README.md", at="base"))
        call("sync")
        if conflict:
            readme = cwd / "README.md"
            readme.write_text(readme.read_text() + f"Owner note from {handle}.\n")

    def push() -> str:
        for _ in range(3):
            res = call("push", intent=f"work from {handle}", task="T1")
            if not res.startswith("error"):
                return res.split()[1]
            redo()            # "links since your base changed README.md ... sync first"
        raise SystemExit("push kept failing")

    apply()
    sh(out, "python -m pytest -q", cwd)
    pid = push()
    deadline = time.time() + float(os.environ.get("FAKE_DEADLINE", "60"))
    while time.time() < deadline:
        call("wait", push=pid, timeout=3)
        chain = client.chain()
        if any(l.push == pid for l in chain.links):
            return f"linked {pid[:12]}"
        prop = chain.push_status(pid)
        if prop is not None and prop.status in ("superseded", "stale", "conflict"):
            redo()
            pid = push()
    return "gave up"


def main() -> int:
    argv = sys.argv[1:]
    opts = args_of(argv)
    mode = os.environ.get("FAKE_AGENT_MODE", "work")
    handle = os.environ.get("GUD_EVAL_HANDLE", "@a1")
    out = Out()
    if mode == "authfail":
        out.emit({"type": "system", "subtype": "init", "model": MODEL, "mcp_servers": []})
        out.emit({"type": "assistant", "error": "authentication_failed", "message": {
            "id": "x", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0},
            "content": [{"type": "text", "text": "Failed to authenticate: OAuth session expired and could not be refreshed"}]}})
        out.emit({"type": "result", "is_error": True, "total_cost_usd": 0, "num_turns": 1,
                  "result": "Failed to authenticate: OAuth session expired and could not be refreshed"})
        return 1
    if mode == "nomcp":
        out.init("failed")
        time.sleep(600)
        return 0
    out.init("connected" if "--mcp-config" in opts else None)
    if mode == "spend":
        while True:
            out.text("thinking...")
            time.sleep(0.05)
    if mode == "sleep":
        time.sleep(600)
        return 0
    if "--mcp-config" in opts and "gud.cli_v1" in Path(opts["--mcp-config"]).read_text():
        text = gud1_work(out, handle, opts["--mcp-config"])
    elif "--mcp-config" in opts:
        text = gud_work(out, handle, opts["-p"], opts["--mcp-config"])
    else:
        text = git_work(out, handle)
    if os.environ.get("FAKE_ESCALATE") == "1" and handle == "@a1":
        text += "\nESCALATE: should pricing show yearly plans?"
    out.text(text)
    out.result(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
