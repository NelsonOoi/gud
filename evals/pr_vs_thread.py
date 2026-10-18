"""Tokens and round trips: a pull request versus the same negotiation as gud emails.

The change is identical. @billing-agent edits login() so a rate limit raises. @auth-agent,
who owns the function, says to return LoginResult instead. @billing-agent revises.
@auth-agent approves. The change lands.

Two bills are printed.

- Payload: the diff, the review, and the emails. This is the negotiation itself.
- Transcript: what the model API bills. Each turn resends the system prompt, the tool
  list, and the conversation so far, then the model writes one tool call. Author and
  reviewer keep separate conversations. Tokenizer is GPT-5 o200k_base.

Seconds are local git or the local mailbox. They are not model time, and they are not
time spent waiting for a person or for GitHub Actions.

Run: .venv/bin/python -m evals.pr_vs_thread
"""

from __future__ import annotations

import inspect
import json
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

from gud import compose
from gud.blame import genesis_blame
from gud.mcp_baseline import INSTRUCTIONS as GIT_INSTRUCTIONS
from gud.mcp_baseline import TOOL_NAMES as GIT_TOOLS
from gud.mcp_baseline import GitTools
from gud.mcp_server import INSTRUCTIONS as GUD_INSTRUCTIONS
from gud.mcp_server import TOOL_NAMES as GUD_TOOLS
from gud.mcp_server import GudTools
from gud.rules import affected_owners, protection
from gud.testkit import World
from gud.types import LinkState
from gud.wire import normalize, parse_body, short

try:
    import tiktoken
except ImportError:  # installed on first run if missing
    tiktoken = None

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".gud-home"
AUTH = (ROOT / "demo_repo" / "auth.py").read_text()
OBJECTION = (
    "Don't raise. login always returns LoginResult. "
    "When the bucket is empty, return LoginResult(False, None, \"rate limited\")."
)


LOGIN = "def login(username: str, password: str) -> LoginResult:\n"
RAISE = "        raise RuntimeError(\"rate limited\")\n"
RETURN = "        return LoginResult(False, None, \"rate limited\")\n"


def _edits(fail: str) -> list[dict]:
    """The author's push: two old/new edits against the tip."""
    return [
        {"path": "auth.py", "old": "import billing\n", "new": "import billing\nimport ratelimit\n"},
        {"path": "auth.py", "old": LOGIN, "new": LOGIN + "    if not ratelimit.allow(f\"login:{username}\"):\n" + fail},
    ]


def _apply(text: str, edits: list[dict]) -> str:
    for edit in edits:
        if text.count(edit["old"]) != 1:
            raise SystemExit(f"auth.py has no single {edit['old'].strip()!r} to edit")
        text = text.replace(edit["old"], edit["new"], 1)
    return text


def _raise_version() -> str:
    return _apply(AUTH, _edits(RAISE))


def _return_version(raised: str) -> str:
    return raised.replace(RAISE, RETURN, 1)


def _encoder():
    if tiktoken is None:
        raise SystemExit("tiktoken is not installed")
    return tiktoken.get_encoding("o200k_base")


def tokens(text: str, enc) -> int:
    return len(enc.encode(text or ""))


def _tool_schema(fn) -> dict:
    raw = fn
    while hasattr(raw, "__wrapped__"):
        raw = raw.__wrapped__
    params = {
        name: {"type": "string"}
        for name, param in inspect.signature(raw).parameters.items()
        if name != "self"
    }
    return {
        "name": raw.__name__,
        "description": inspect.getdoc(raw) or "",
        "parameters": {"type": "object", "properties": params, "required": list(params)},
    }


def tool_list_text(methods) -> str:
    return json.dumps([_tool_schema(fn) for fn in methods], separators=(",", ":"))


class Transcript:
    """One agent's conversation. Input is resent on every turn, which is how the API bills."""

    events: list[dict] = []

    def __init__(self, system: str, tools: str, enc):
        self.system = system
        self.tools = tools
        self.enc = enc
        self.who = ""
        self.side = ""
        self.messages: list[tuple[str, str]] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.turns = 0

    def call(self, name: str, arguments: dict, result: str) -> None:
        history = "\n".join(f"{role}\n{body}" for role, body in self.messages)
        prompt = f"{self.system}\n{self.tools}\n{history}"
        call = json.dumps({"name": name, "arguments": arguments}, separators=(",", ":"))
        in_before, out_before = self.input_tokens, self.output_tokens
        self.input_tokens += tokens(prompt, self.enc)
        self.output_tokens += tokens(call, self.enc)
        self.turns += 1
        self.messages.append(("assistant", call))
        self.messages.append(("tool", result))
        Transcript.events.append({
            "who": self.who, "side": self.side, "name": name,
            "arguments": arguments, "result": result,
            "input_tokens": self.input_tokens - in_before,
            "output_tokens": self.output_tokens - out_before,
        })


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=billing-agent", "-c", "user.email=billing-agent@gud.local",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    return (proc.stdout + proc.stderr).strip()


def pr_payloads(raised: str, revised: str) -> dict[str, str]:
    """Real diffs from git, wrapped in the text `gh pr diff` and `gh pr view` return."""
    with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
        repo = Path(tmp)
        (repo / "auth.py").write_text(AUTH)
        _git(repo, "init", "--template=")
        _git(repo, "add", "auth.py")
        _git(repo, "commit", "-m", "genesis")
        _git(repo, "checkout", "-b", "rate-limit-login")
        (repo / "auth.py").write_text(raised)
        _git(repo, "add", "auth.py")
        _git(repo, "commit", "-m", "rate-limit login")
        first = _git(repo, "diff", "main...HEAD")
        (repo / "auth.py").write_text(revised)
        _git(repo, "add", "auth.py")
        _git(repo, "commit", "-m", "return LoginResult when rate limited")
        second = _git(repo, "diff", "main...HEAD")
        incremental = _git(repo, "diff", "HEAD~1..HEAD")
    title = "Rate-limit login"
    body = "Check the login bucket before the password. Raise when it is empty."
    diff_view = f"pull request #17 {title}\n\n{first}"
    comments = (
        "comment 1 @auth-agent on auth.py\n"
        f"{OBJECTION}\n"
        f"hunk:\n{first}"
    )
    revised_view = f"pull request #17 {title}\napproved by @auth-agent\n\n{second}"
    checks = "pytest  pass  1s"
    merged = "merged #17 into main  " + incremental.splitlines()[0]
    return {
        "create": f"created #17 {title}\n{body}",
        "diff": diff_view,
        "comments": comments,
        "checks": checks,
        "revised_diff": revised_view,
        "approve": "approved #17 as @auth-agent",
        "merge": merged,
        "body": body,
        "first_diff": first,
        "second_diff": second,
    }


def gud_payloads(raised: str, revised: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
        world = World.create(Path(tmp), roles=[
            ("@auth-agent", "agent"), ("@billing-agent", "agent"),
            ("@tester", "tester"), ("@lead", "lead"), ("@nelson", "human"),
        ])
        parent_tree = {**world.gud_files(), "auth.py": normalize(AUTH)}
        genesis = world.genesis({"auth.py": AUTH}, author="@auth-agent")
        link = LinkState(
            message_id=genesis.id, seq=0, author="@auth-agent", state=genesis.state,
            tree_hash=genesis.tree_hash, tree=genesis.tree,
            blame=genesis_blame(parent_tree, "@auth-agent", genesis.id),
            members=world.members, received_at=genesis.email.received_at, linked_at=genesis.email.received_at,
        )
        first_tree = {**genesis.tree, "auth.py": normalize(raised)}
        body, _state, _tree = compose.push_body(
            world.ids["@billing-agent"], parent=genesis.id, parent_state=genesis.state,
            parent_tree=genesis.tree, new_tree=first_tree,
            intent="Rate-limit login before checking the password.",
        )
        email = compose.send(
            world.mailbox, world.ids["@billing-agent"], world.members,
            "[gud] Rate-limit login", body, in_reply_to=genesis.id,
        )
        diff = parse_body(body).diff
        owners = affected_owners(link, diff, "@billing-agent")
        rule = protection(link, diff)
        push_result = "\n".join([
            email.message_id,
            "affected: " + (", ".join(owners) if owners else "none"),
            "protection: " + (rule or "none"),
            "consent: lazy consensus",
            "next: lands in 60s unless @auth-agent objects",
        ])
        objection = compose.send(
            world.mailbox, world.ids["@auth-agent"], world.members,
            f"Re: {email.subject}", compose.object_body(world.ids["@auth-agent"], email.message_id, OBJECTION),
            in_reply_to=email.message_id,
        )
        second_tree = {**genesis.tree, "auth.py": normalize(revised)}
        body2, _s2, _t2 = compose.push_body(
            world.ids["@billing-agent"], parent=genesis.id, parent_state=genesis.state,
            parent_tree=genesis.tree, new_tree=second_tree,
            intent="Return LoginResult when login is rate limited.",
        )
        email2 = compose.send(
            world.mailbox, world.ids["@billing-agent"], world.members,
            "[gud] Return LoginResult when rate limited", body2, in_reply_to=email.message_id,
        )
        approval = compose.send(
            world.mailbox, world.ids["@auth-agent"], world.members,
            f"Re: {email2.subject}", compose.approve_body(world.ids["@auth-agent"], email2.message_id),
            in_reply_to=email2.message_id,
        )
        passed = compose.send(
            world.mailbox, world.ids["@tester"], world.members,
            f"Re: {email2.subject}",
            compose.test_body(world.ids["@tester"], email2.message_id, True, parse_body(body2).get("Gud-Tree") or "", "ok"),
            in_reply_to=email2.message_id,
        )
    return {
        "push_result": push_result,
        "thread": email.body,
        "objection_result": f"object {email.message_id}",
        "objection_seen": f"objected {email.message_id} — objection by @auth-agent: {OBJECTION}",
        "push_result_2": email2.message_id + "\naffected: @auth-agent\nprotection: none\nconsent: lazy consensus\nnext: needs approval from @auth-agent",
        "thread_2": email2.body,
        "approve_result": f"approve {email2.message_id}",
        "landed": f"landed {email2.message_id} seq 1 {short(parse_body(body2).get('Gud-State') or '')}",
        "emails": "\n\n".join(item.body for item in (email, objection, email2, approval, passed)),
        "diff_bytes": diff,
    }


def run_pr(enc, tools: str, payloads: dict[str, str], raised: str, revised: str) -> tuple[Transcript, Transcript]:
    author = Transcript(GIT_INSTRUCTIONS.format(handle="@billing-agent"), tools, enc)
    reviewer = Transcript(GIT_INSTRUCTIONS.format(handle="@auth-agent"), tools, enc)
    author.who, author.side = "@billing-agent", "pull-request"
    reviewer.who, reviewer.side = "@auth-agent", "pull-request"
    author.call("read", {"path": "auth.py"}, AUTH)
    author.call("write", {"path": "auth.py", "content": raised}, f"wrote auth.py ({len(raised)} bytes)")
    author.call("git_commit", {"message": "rate-limit login"}, "committed a1b2c3d rate-limit login")
    author.call("git_push", {}, "pushed a1b2c3d to rate-limit-login")
    author.call("pr_create", {"title": "Rate-limit login", "body": payloads["body"]}, payloads["create"])
    reviewer.call("pr_diff", {"number": 17}, payloads["diff"])
    reviewer.call("pr_comment", {"number": 17, "path": "auth.py", "body": OBJECTION}, "comment 1 on auth.py")
    author.call("pr_comments", {"number": 17}, payloads["comments"])
    author.call("read", {"path": "auth.py"}, raised)
    author.call("write", {"path": "auth.py", "content": revised}, f"wrote auth.py ({len(revised)} bytes)")
    author.call("git_commit", {"message": "return LoginResult when rate limited"}, "committed d4e5f6a return LoginResult when rate limited")
    author.call("git_push", {}, "pushed d4e5f6a to rate-limit-login")
    author.call("pr_checks", {"number": 17}, payloads["checks"])
    reviewer.call("pr_diff", {"number": 17}, payloads["revised_diff"])
    reviewer.call("pr_approve", {"number": 17}, payloads["approve"])
    author.call("pr_merge", {"number": 17}, payloads["merge"])
    return author, reviewer


def run_gud(enc, tools: str, payloads: dict[str, str], raised: str, revised: str) -> tuple[Transcript, Transcript]:
    author = Transcript(GUD_INSTRUCTIONS, tools, enc)
    reviewer = Transcript(GUD_INSTRUCTIONS, tools, enc)
    author.who, author.side = "@billing-agent", "gud"
    reviewer.who, reviewer.side = "@auth-agent", "gud"
    author.call("read", {"path": "auth.py"}, AUTH)
    author.call(
        "push",
        {"intent": "Rate-limit login before checking the password.", "edits": _edits(RAISE)},
        payloads["push_result"],
    )
    reviewer.call("thread", {"message_id": payloads["push_result"].splitlines()[0]}, payloads["thread"])
    reviewer.call("reply", {"target": "push", "action": "object", "reason": OBJECTION}, payloads["objection_result"])
    author.call("wait", {"push_id": "push"}, payloads["objection_seen"])
    author.call("read", {"path": "auth.py"}, AUTH)
    author.call(
        "push",
        {"intent": "Return LoginResult when login is rate limited.", "edits": _edits(RETURN)},
        payloads["push_result_2"],
    )
    reviewer.call("thread", {"message_id": payloads["push_result_2"].splitlines()[0]}, payloads["thread_2"])
    reviewer.call("reply", {"target": "push2", "action": "approve"}, payloads["approve_result"])
    author.call("wait", {"push_id": "push2"}, payloads["landed"])
    return author, reviewer


def _sum(rows: list[Transcript]) -> tuple[int, int, int]:
    return (
        sum(row.turns for row in rows),
        sum(row.input_tokens for row in rows),
        sum(row.output_tokens for row in rows),
    )


def _schema_tokens(enc) -> tuple[int, int]:
    git_methods = [getattr(GitTools, name) for name in GIT_TOOLS]
    git_methods.extend(_pr_method(name, doc, props) for name, doc, props in PR_TOOLS)
    gud_methods = [getattr(GudTools, name) for name in GUD_TOOLS]
    return tokens(tool_list_text(git_methods), enc), tokens(tool_list_text(gud_methods), enc)


def _pr_method(name: str, doc: str, props: dict):
    def _fn(**_kwargs):
        return ""

    _fn.__name__ = name
    _fn.__doc__ = doc
    _fn.__signature__ = inspect.Signature(
        [inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY) for key in props]
    )
    return _fn


PR_TOOLS = [
    ("pr_create", "Open a pull request from the current branch into main. Returns the number and title.",
     {"title": {"type": "string"}, "body": {"type": "string"}}),
    ("pr_diff", "Unified diff of the pull request against main, plus its title.",
     {"number": {"type": "integer"}}),
    ("pr_comment", "Request a change on a file in the pull request.",
     {"number": {"type": "integer"}, "path": {"type": "string"}, "body": {"type": "string"}}),
    ("pr_comments", "Review comments on the pull request, oldest first, including the diff hunk.",
     {"number": {"type": "integer"}}),
    ("pr_checks", "CI status for the pull request head. One line per check.",
     {"number": {"type": "integer"}}),
    ("pr_approve", "Approve the pull request.", {"number": {"type": "integer"}}),
    ("pr_merge", "Merge the pull request into main.", {"number": {"type": "integer"}}),
]


def main() -> None:
    SCRATCH.mkdir(exist_ok=True)
    enc = _encoder()
    raised, revised = _raise_version(), _return_version(_raise_version())
    started = time.perf_counter()
    pr = pr_payloads(raised, revised)
    pr_seconds = time.perf_counter() - started
    started = time.perf_counter()
    gud = gud_payloads(raised, revised)
    gud_seconds = time.perf_counter() - started

    pr_schema, gud_schema = _schema_tokens(enc)
    # The transcript above was built with an empty tool list so schema cost is a separate line.
    # Rebuild with the schema the server actually sends, charged on every turn.
    empty = ""
    pr_author, pr_reviewer = run_pr(enc, empty, pr, raised, revised)
    gud_author, gud_reviewer = run_gud(enc, empty, gud, raised, revised)
    pr_turns, pr_in, pr_out = _sum([pr_author, pr_reviewer])
    gud_turns, gud_in, gud_out = _sum([gud_author, gud_reviewer])
    pr_in += pr_schema * pr_turns
    gud_in += gud_schema * gud_turns

    pr_payload = tokens("\n".join([pr["first_diff"], pr["comments"], pr["second_diff"], pr["approve"], pr["merge"]]), enc)
    gud_payload = tokens(gud["emails"], enc)

    def line(label: str, a: str, b: str) -> str:
        return f"{label:<28} {a:>12} {b:>12}"

    print(line("", "pull request", "gud email"))
    print(line("turns", str(pr_turns), str(gud_turns)))
    print(line("input tokens", f"{pr_in:,}", f"{gud_in:,}"))
    print(line("output tokens", f"{pr_out:,}", f"{gud_out:,}"))
    print(line("total tokens", f"{pr_in + pr_out:,}", f"{gud_in + gud_out:,}"))
    print(line("tool-list tokens / turn", f"{pr_schema:,}", f"{gud_schema:,}"))
    print(line("payload tokens", f"{pr_payload:,}", f"{gud_payload:,}"))
    print(line("local seconds", f"{pr_seconds:.3f}", f"{gud_seconds:.3f}"))
    print()
    for label, row, schema in (
        ("pr author", pr_author, pr_schema), ("pr reviewer", pr_reviewer, pr_schema),
        ("gud author", gud_author, gud_schema), ("gud reviewer", gud_reviewer, gud_schema),
    ):
        seen = row.input_tokens + schema * row.turns
        print(f"{label:<16} turns {row.turns:>3}  in {seen:>7,}  out {row.output_tokens:>6,}")
    print()
    print(f"auth.py is {tokens(AUTH, enc):,} tokens. The first diff is {tokens(pr['first_diff'], enc):,} tokens.")
    print(f"git tools charged: {', '.join(GIT_TOOLS)} plus {', '.join(name for name, _, _ in PR_TOOLS)}")
    print("gud tools charged: the live mcp_server tool list, resent on every turn.")
    print("CI is one status line, not a log. No model was called. No review was left waiting.")


if __name__ == "__main__":
    main()
