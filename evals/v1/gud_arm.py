"""The gud arm: today's v0 gud over the fake mailbox, one member key per agent, the gud MCP server.

Layout under the run's work root:

    gud-home/            GUD_HOME: keys/, mailbox.jsonl (the fake mailbox; the repo is the mail to repo@gud.local)
    agents/<a1>/         the agent's scratch working directory (starts empty; filled with MCP `export`)
    mcp/<a1>.json        the agent's --mcp-config (python -m gud.mcp_server, stdio)

Members: @a1..@aN (agent, voters), @tester (tester), @seed (console: signs genesis and the task email,
does not vote, so the seed lines have no owner who must consent), @nelson (human requester; never acts).

There is no lead. The task goes out as one signed `task` email (T1) to every member; agents see it
with the `tasks` tool. v0 has no way for an agent to claim or split work, so agents coordinate only
through push intents, `status`/`wait`, and approve/object replies (see README.md, Limitations).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from gud import compose, sig  # noqa: E402
from gud.mail_fake import FileMailbox  # noqa: E402
from gud.memberfile import MEMBERS, format_members  # noqa: E402
from gud.replay import replay  # noqa: E402
from gud.types import Member  # noqa: E402
from gud.wire import normalize, parse_body  # noqa: E402

DOMAIN = "gud.local"
TASK_ID = "T1"
HERE = Path(__file__).resolve().parent


class GudArm:
    name = "gud"

    def __init__(self, root: Path, spec, handles: list[str], *, window: float = 30.0, python: str = sys.executable):
        self.root = Path(root)
        self.spec = spec
        self.handles = list(handles)
        self.window = float(window)
        self.python = python
        self.home = self.root / "gud-home"
        self.keys: dict[str, bytes] = {}
        self.tester: subprocess.Popen | None = None
        self.tester_log: Path | None = None
        self.mailbox = FileMailbox(self.home / "mailbox.jsonl", f"repo@{DOMAIN}", DOMAIN)
        self.genesis_id: str | None = None
        self.task_id_email: str | None = None

    # -- setup -----------------------------------------------------------------------------

    def env(self) -> dict[str, str]:
        """Environment for every gud process (MCP servers, tester). Pins the fake mailbox and turns off extras."""
        return {
            "GUD_HOME": str(self.home),
            "GUD_MAILBOX": "fake",
            "GUD_RUNNER": "local",
            "GUD_WINDOW_SECONDS": str(int(self.window) if self.window.is_integer() else self.window),
            "GUD_DOMAIN": DOMAIN,
            "GUD_HOST_URL": "",
            "GUD_MCP_TOKEN": "",
            "ARMATURE_API_KEY": "",
            "PYTHONPATH": str(REPO),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def setup(self) -> None:
        from specs import read_tree

        members: dict[str, Member] = {}
        roles = [(h, "agent") for h in self.handles] + [("@tester", "tester"), ("@seed", "console")]
        for handle, role in roles:
            raw, public = sig.generate()
            sig.save_private(self.home / "keys" / f"{handle.lstrip('@')}.key", raw)
            self.keys[handle] = raw
            members[handle] = Member(handle, role, f"{handle.lstrip('@')}@{DOMAIN}", public, "@nelson")
        members["@nelson"] = Member("@nelson", "human", f"nelson@{DOMAIN}", None, None)
        tree = {path: normalize(text) for path, text in read_tree(self.spec.seed).items()}
        tree[MEMBERS] = format_members(members)
        seed = self.identity("@seed")
        body, _state, _hash = compose.push_body(seed, parent=None, parent_state=None, parent_tree={},
                                                new_tree=tree, intent=f"genesis: {self.spec.name} seed")
        email = compose.send(self.mailbox, seed, members, "[gud] genesis", body)
        self.genesis_id = email.message_id
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.chain().tip is not None:
                return
            time.sleep(0.2)
        raise RuntimeError("genesis did not replay as a link")

    def identity(self, handle: str) -> compose.Identity:
        return compose.Identity(handle, f"{handle.lstrip('@')}@{DOMAIN}", self.keys[handle])

    def private_key_b64(self, handle: str) -> str:
        return base64.b64encode(self.keys[handle]).decode()

    def chain(self):
        return replay(self.mailbox.list(recipient=self.mailbox.repo_address), self.window, time.time())

    def start_services(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.tester_log = log_dir / "tester.log"
        env = {**_base_env(), **self.env()}
        self.tester = subprocess.Popen(
            [self.python, str(HERE / "gud_tester.py"), "--handle", "@tester"],
            cwd=self.root, env=env, stdout=self.tester_log.open("w"), stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def seed_task(self) -> None:
        seed = self.identity("@seed")
        chain = self.chain()
        body = compose.task_body(seed, TASK_ID, "@nelson", self.spec.text)
        email = compose.send(self.mailbox, seed, chain.tip.members, f"[gud] {TASK_ID}: {self.spec.title}", body)
        self.task_id_email = email.message_id

    def stop_services(self) -> None:
        if self.tester is not None and self.tester.poll() is None:
            self.tester.terminate()
            try:
                self.tester.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.tester.kill()

    # -- agents ----------------------------------------------------------------------------

    def workdir(self, handle: str) -> Path:
        path = self.root / "agents" / handle.lstrip("@")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def mcp_config(self, handle: str) -> Path:
        path = self.root / "mcp" / f"{handle.lstrip('@')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": {"gud": {
            "command": self.python, "args": ["-m", "gud.mcp_server"], "env": self.env(),
        }}}, indent=2))
        return path

    def agent_env(self, handle: str) -> dict[str, str]:
        return {"GUD_EVAL_HANDLE": handle}

    def instructions(self, handle: str) -> str:
        work = self.workdir(handle)
        key = self.private_key_b64(handle)
        return f"""How this repo works (gud):
- The shared repo is a gud email chain. You reach it only through the gud MCP tools (mcp__gud__status, read, ls, blame, push, export, wait, rebase, reply, log, thread, tasks). There are no branches, no pull and no merge.
- Your identity: handle "{handle}", private_key "{key}". Pass both on every push, reply and rebase call; pass handle on status and wait.
- Your current directory ({work}) is a scratch area only. To get the files, call export with dir set to a new empty subdirectory of it (for example {work}/tip1, then tip2 for the next one), optionally with the edits you are about to push, and run `python -m pytest -q` there. Editing files on disk does NOT change the repo: only push does.
- push takes old/new edits: {{"path": "a.py", "old": "exact text", "new": "replacement"}} replaces text that occurs exactly once; {{"path": "a.py", "new": "full text"}} creates a new file; {{"path": "a.py", "delete": true}} removes one. Tag every push with task "{TASK_ID}" and write an intent the other agents can read.
- A push lands when the tester replies that `python -m pytest -q -x` passes on it. If it rewrites lines another agent wrote, that agent has {int(self.window)}s to object first. After push, call wait with the push id. If it is superseded or stale (someone else landed first), call rebase; if rebase reports a conflict, read the tip again and push a fresh edit.
- The task is {TASK_ID} in `tasks`. Use status, log and thread to see what the others have pushed and what is pending. When status or wait says a push needs you, read it (thread, read) and reply approve, or object with a concrete reason.
- To tell the others what you are taking on, say it in your push intents (there is no chat or claim tool)."""

    # -- results ---------------------------------------------------------------------------

    def final_tree(self) -> dict[str, str]:
        chain = self.chain()
        return {} if chain.tip is None else dict(chain.tip.tree)

    def metrics(self, wall_s: float, transcripts: dict[str, list[dict]]) -> dict:
        chain = self.chain()
        emails = [e for e in chain.emails if e.message_id != self.genesis_id]
        kinds: dict[str, int] = {}
        for email in emails:
            kind = parse_body(email.body).kind or "plain"
            kinds[kind] = kinds.get(kind, 0) + 1
        links = [link for link in chain.links if link.message_id != self.genesis_id]
        props = list(chain.proposals.values())
        statuses: dict[str, int] = {}
        for prop in props:
            statuses[prop.status] = statuses.get(prop.status, 0) + 1
        link_times = {link.message_id: link.received_at for link in links}

        def later_link(author: str, after: float) -> str | None:
            for link in links:
                if link.author == author and link.received_at > after:
                    return link.message_id
            return None

        conflicts = []
        for prop in sorted(props, key=lambda p: p.received_at):
            if prop.status not in {"superseded", "stale", "conflict", "objected", "tests_failed", "vetoed"}:
                continue
            relanded = later_link(prop.author, prop.received_at)
            conflicts.append({
                "push": prop.message_id, "author": prop.author, "status": prop.status,
                "reason": (prop.reason or "")[:200],
                "objections": [{"by": o.get("by"), "reason": (o.get("reason") or "")[:200]} for o in prop.objections],
                "resolution": "later push by the same author landed" if relanded else "not resolved",
                "resolved": bool(relanded),
            })
        objections = sum(1 for e in emails if parse_body(e.body).kind == "object")
        tests = [parse_body(e.body) for e in emails if parse_body(e.body).kind == "test"]
        test_fail = sum(1 for p in tests if p.get("Gud-Result") != "pass")
        rebase_conflicts = 0
        push_errors = 0
        wait_timeouts = 0
        tool_errors = 0
        for rows in transcripts.values():
            for row in rows:
                name = row["name"]
                if not name.startswith("mcp__gud__"):
                    continue
                if name.endswith("rebase") and row["result"].startswith("conflict"):
                    rebase_conflicts += 1
                if name.endswith("push") and row["result"].startswith("error"):
                    push_errors += 1
                if name.endswith("__wait") and row["result"].startswith("timeout"):
                    wait_timeouts += 1
                if row["result"].startswith("error"):
                    tool_errors += 1
        minutes = max(wall_s, 1e-9) / 60
        n_links = len(links)
        contested = [c for c in conflicts if c["status"] in {"superseded", "stale", "conflict", "objected"}]
        return {
            "integrations": n_links,
            "integrations_per_min": round(n_links / minutes, 3),
            "test_runs": len(tests),
            "test_runs_per_integration": round(len(tests) / n_links, 3) if n_links else None,
            "conflicts_total": len(contested),
            "conflicts_resolved": sum(1 for c in contested if c["resolved"]),
            "gud": {
                "links": n_links,
                "links_per_min": round(n_links / minutes, 3),
                "pushes": kinds.get("push", 0),
                "emails": len(emails),
                "emails_by_kind": dict(sorted(kinds.items())),
                "emails_per_link": round(len(emails) / n_links, 3) if n_links else None,
                "test_runs": len(tests),
                "test_failures": test_fail,
                "test_runs_per_link": round(len(tests) / n_links, 3) if n_links else None,
                "proposal_statuses": dict(sorted(statuses.items())),
                "superseded": statuses.get("superseded", 0),
                "stale": statuses.get("stale", 0),
                "conflict_status": statuses.get("conflict", 0),
                "objected_pushes": statuses.get("objected", 0),
                "objections": objections,
                "approvals": kinds.get("approve", 0),
                "rebase_tool_conflicts": rebase_conflicts,
                "push_tool_errors": push_errors,
                "wait_timeouts": wait_timeouts,
                "tool_errors": tool_errors,
                "first_link_s": None if not links else round(min(link_times.values()) - self._t0, 1),
                "link_list": [{"id": l.message_id, "author": l.author, "seq": l.seq, "task": l.task,
                           "at_s": round(l.received_at - self._t0, 1), "subject": l.subject[:100]} for l in links],
                "conflicts": conflicts,
            },
        }

    _t0: float = 0.0

    def mark_start(self, t0: float) -> None:
        self._t0 = t0


def _base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GUD_") and k != "PYTHONPATH"}
