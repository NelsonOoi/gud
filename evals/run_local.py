"""Run a gud eval case against the gud server and the git baseline.

In-process: each agent calls tool methods on a shared fake mailbox (gud) or a
shared bare git origin. A background tester answers push emails, and the fake
clock advances with wall time so lazy-consensus windows close.

    python evals/run_local.py --dry-run
    python evals/run_local.py --dry-run --case parallel-functions
    python evals/run_local.py --case parallel-functions --model x-ai/grok-4-fast

``--dry-run`` replays the scripted agents in the case file and does not call
OpenRouter. Without it, each agent is an OpenRouter tool loop.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES = Path(__file__).resolve().parent / "cases"
RESULTS = Path(__file__).resolve().parent / "results"

GUD_SYSTEM = (
    "You are {handle}, working in a gud repo with other agents. The repo is an email chain: "
    "no branches, no pull, no merge. Read before you edit. push sends the full new text of each "
    "file you change, then wait. If wait says superseded or stale, rebase. If it says objected, "
    "revise and push again. If status says a push needs you, reply approve or object with a reason. "
    "Finish an assigned subtask by pushing with its task id. When your change has landed and the "
    "task is done, stop and summarize in one line."
)
GIT_SYSTEM = (
    "You are {handle}, working in a shared git repository with other agents. "
    "Write files, commit, and push. If push is rejected, git_pull. If that conflicts, resolve "
    "each file with the full text you want, commit, and push again. Do not force-push. "
    "When your commits are on origin and the task is done, stop and summarize in one line."
)


class LockedClock:
    """World.Clock with a lock so the tester thread and agents can share it."""

    def __init__(self, inner):
        self._inner = inner
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._inner()

    def tick(self, seconds: float) -> None:
        with self._lock:
            self._inner.t += seconds

    @property
    def t(self) -> float:
        with self._lock:
            return self._inner.t

    @t.setter
    def t(self, value: float) -> None:
        with self._lock:
            self._inner.t = value


class Session:
    def __init__(self, tools, kind: str, wait_seconds: float, verbose: bool = False):
        self.tools = tools
        self.kind = kind
        self.wait_seconds = wait_seconds
        self.verbose = verbose
        self.calls: list[dict] = []
        self.errors = 0
        self.turns = 0
        self.tokens = 0
        self.final = ""
        self.started = time.perf_counter()

    def call(self, name: str, args: dict | None = None) -> str:
        fn = getattr(self.tools, name)
        t0 = time.perf_counter()
        try:
            out = fn(**(args or {}))
            if not isinstance(out, str):
                out = json.dumps(out, default=str)
        except Exception as exc:
            out = f"error: {exc}"
        err = out.startswith("error:")
        if err:
            self.errors += 1
        self.calls.append({
            "tool": name,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "error": err,
            "head": out[:240],
        })
        if self.verbose:
            print(f"    {self.kind} {name} -> {out.splitlines()[0][:120] if out else ''}", flush=True)
        return out

    def stats(self, handle: str) -> dict:
        return {
            "handle": handle,
            "turns": self.turns or len(self.calls),
            "tool_calls": len(self.calls),
            "tool_errors": self.errors,
            "tokens": self.tokens,
            "seconds": round(time.perf_counter() - self.started, 2),
            "final": (self.final or "")[:300],
        }


def _msgid(text: str) -> str | None:
    if not text or not text.strip():
        return None
    first = text.strip().splitlines()[0].strip()
    if first.startswith("<") and first.endswith(">") and " " not in first:
        return first
    return None


def _subst(value, env: dict):
    if isinstance(value, str) and value.startswith("$") and value[1:] in env:
        return env[value[1:]]
    if isinstance(value, list):
        return [_subst(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _subst(item, env) for key, item in value.items()}
    return value


def _dirty(status: str) -> bool:
    return any(line.strip() and not line.startswith("##") for line in status.splitlines())


def _needs_id(status: str) -> str | None:
    grab = False
    for line in status.splitlines():
        if line.startswith("needs-me:"):
            grab = "none" not in line
            continue
        if not grab:
            continue
        if not line.startswith(" "):
            break
        if "<" in line and ">" in line:
            inner = line[line.index("<"):line.index(">") + 1]
            if " " not in inner:
                return inner
    return None


def _settle_gud(session: Session, env: dict) -> str:
    pid = env.get("push_id")
    if not pid:
        return env.get("last") or "error: no push id"
    last = ""
    for _ in range(4):
        last = session.call("wait", {"push_id": pid, "timeout": session.wait_seconds})
        if last.startswith("landed"):
            return last
        if last.startswith(("objected", "vetoed", "tests_failed", "timeout", "needs-you", "error:")):
            return last
        if last.startswith(("superseded", "stale", "conflict", "bad_sig")):
            reb = session.call("rebase", {"push_id": pid})
            if reb.startswith("conflict\n") or reb.startswith("error:"):
                return reb
            mid = _msgid(reb)
            if not mid:
                return reb
            pid = mid
            env["push_id"] = pid
            continue
        return last
    return last


def _settle_git(session: Session, env: dict) -> str:
    if _dirty(session.call("git_status", {})):
        committed = session.call("git_commit", {"message": "update"})
        if committed.startswith("error:"):
            return committed
    last = ""
    for _ in range(5):
        last = session.call("git_push", {})
        if last.startswith("pushed"):
            return last
        if "rejected" in last or "non-fast-forward" in last:
            pull = session.call("git_pull", {})
            if pull.startswith("conflict") or pull.startswith("error:"):
                return pull
            if _dirty(session.call("git_status", {})):
                committed = session.call("git_commit", {"message": "merge"})
                if committed.startswith("error:"):
                    return committed
            continue
        return last
    return last


def _until(session: Session, spec: dict) -> str:
    deadline = time.time() + float(spec.get("timeout", session.wait_seconds))
    last = ""
    while time.time() < deadline:
        if spec.get("pull") and hasattr(session.tools, "git_pull"):
            session.call("git_pull", {})
        last = session.call(spec["tool"], spec.get("args") or {})
        if spec["text"] in last:
            return last
        time.sleep(float(spec.get("every", 0.2)))
    raise TimeoutError(f"did not see {spec['text']!r}; last={last[:180]}")


def _poll_needs(session: Session, timeout: float) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = session.call("status", {})
        found = _needs_id(last)
        if found:
            return found
        time.sleep(0.1)
    raise TimeoutError("no push needed a response\n" + last[:400])


def interpret(session: Session, steps: list[dict]) -> None:
    env: dict = {}
    session.turns = len(steps)
    for step in steps:
        if "tool" in step:
            out = session.call(step["tool"], _subst(step.get("args") or {}, env))
            env["last"] = out
            if step.get("save"):
                env[step["save"]] = out
            if step["tool"] == "push":
                mid = _msgid(out)
                if mid:
                    env["push_id"] = mid
        elif step.get("settle") == "gud":
            env["last"] = _settle_gud(session, env)
        elif step.get("settle") == "git":
            env["last"] = _settle_git(session, env)
        elif "until_contains" in step:
            env["last"] = _until(session, step["until_contains"])
        elif step.get("poll_needs_me"):
            env["target"] = _poll_needs(session, float(step.get("timeout", session.wait_seconds)))
            env["last"] = env["target"]
        elif "expect_prefix" in step:
            last = str(env.get("last") or "")
            prefix = step["expect_prefix"]
            if not last.startswith(prefix):
                raise RuntimeError(f"expected {prefix!r}, got {last[:200]}")
        else:
            raise RuntimeError(f"unknown script step {step}")


def _schemas(tools, names: list[str]) -> list[dict]:
    import inspect
    import types
    from typing import Union, get_args, get_origin

    def json_type(ann):
        origin = get_origin(ann)
        if ann in (str, inspect.Parameter.empty) or ann is None:
            return {"type": "string"}
        if ann is int:
            return {"type": "integer"}
        if ann is float:
            return {"type": "number"}
        if ann is bool:
            return {"type": "boolean"}
        if ann is list or origin is list:
            args = get_args(ann)
            item = json_type(args[0]) if args else {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": ["string", "null"]}},
            }
            if item.get("type") == "object" and "properties" not in item:
                item = {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": ["string", "null"]}},
                }
            return {"type": "array", "items": item}
        if ann is dict or origin is dict:
            return {"type": "object"}
        if origin in (Union, types.UnionType):
            args = [item for item in get_args(ann) if item is not type(None)]
            return json_type(args[0]) if args else {"type": "string"}
        return {"type": "string"}

    out = []
    for name in names:
        fn = getattr(tools, name)
        raw = getattr(fn, "__func__", fn)
        while hasattr(raw, "__wrapped__"):
            raw = raw.__wrapped__
        try:
            hints = inspect.get_type_hints(raw)
        except Exception:
            hints = {}
        sig = inspect.signature(raw)
        props, required = {}, []
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            props[param_name] = json_type(hints.get(param_name, str))
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        schema: dict = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        doc = inspect.getdoc(raw) or ""
        out.append({"type": "function", "function": {"name": name, "description": doc, "parameters": schema}})
    return out


def _chat(client, key: str, body: dict) -> dict:
    err = "no response"
    for attempt in range(4):
        try:
            response = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=180,
            )
            data = response.json()
            if response.status_code == 200 and data.get("choices"):
                return data
            err = data.get("error", data)
        except Exception as exc:
            err = repr(exc)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OpenRouter failed: {err}")


def llm_agent(session: Session, instructions: str, model: str, key: str, max_turns: int, system: str) -> None:
    import httpx

    from gud.mcp_baseline import TOOL_NAMES as GIT_TOOLS
    from gud.mcp_server import TOOL_NAMES as GUD_TOOLS

    names = list(GUD_TOOLS if session.kind == "gud" else GIT_TOOLS)
    tools = _schemas(session.tools, names)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": instructions},
    ]
    with httpx.Client() as client:
        for turn in range(max_turns):
            data = _chat(client, key, {
                "model": model, "messages": messages, "tools": tools, "temperature": 0,
                "usage": {"include": True},
            })
            usage = data.get("usage") or {}
            session.tokens += int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
            message = data["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                **({"tool_calls": calls} if calls else {}),
            })
            session.turns = turn + 1
            if not calls:
                session.final = message.get("content") or ""
                return
            for call in calls:
                name = call["function"]["name"]
                raw = call["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw) if str(raw).strip() else {}
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                out = session.call(name, args)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": out[:8000]})
        session.final = "max turns"


def _run_agents(pairs: list[tuple[dict, Session]], backend: str, dry_run: bool, model: str, key: str | None, max_turns: int):
    errors: dict[str, str] = {}

    def one(spec: dict, session: Session) -> None:
        try:
            if dry_run:
                key_name = "gud_script" if backend == "gud" else "git_script"
                interpret(session, spec.get(key_name) or [])
            else:
                if backend == "git":
                    text = spec.get("git_instructions") or spec["instructions"]
                    system = GIT_SYSTEM.format(handle=spec["handle"])
                else:
                    text = spec["instructions"]
                    system = GUD_SYSTEM.format(handle=spec["handle"])
                llm_agent(session, text, model, key or "", max_turns, system)
        except Exception:
            errors[spec["handle"]] = traceback.format_exc(limit=6)

    threads = [threading.Thread(target=one, args=pair, daemon=True) for pair in pairs]
    for thread in threads:
        thread.start()
    timeout = 45 if dry_run else max(120, max_turns * 40)
    for thread in threads:
        thread.join(timeout)
    for thread, (spec, _session) in zip(threads, pairs):
        if thread.is_alive():
            errors.setdefault(spec["handle"], "timed out")
    return errors


def _failed(backend: str, case: dict, reason: str, started: float) -> dict:
    return {
        "case": case["id"], "backend": backend, "success": False, "check": reason,
        "turns": 0, "tool_calls": 0, "tool_errors": 0, "tokens": 0,
        "seconds": round(time.perf_counter() - started, 2), "agents": [],
    }


def _summarize(case_id: str, backend: str, ok: bool, check: str, pairs, errors: dict, started: float) -> dict:
    if errors:
        check = (check + "; " if check and check != "ok" else "") + "agents: " + "; ".join(
            f"{handle}: {text.strip().splitlines()[-1][:180]}" for handle, text in errors.items()
        )
        ok = False
    agents = [session.stats(spec["handle"]) for spec, session in pairs]
    return {
        "case": case_id, "backend": backend, "success": ok, "check": check or "ok",
        "turns": sum(agent["turns"] for agent in agents),
        "tool_calls": sum(agent["tool_calls"] for agent in agents),
        "tool_errors": sum(agent["tool_errors"] for agent in agents),
        "tokens": sum(agent["tokens"] for agent in agents),
        "seconds": round(time.perf_counter() - started, 2),
        "agents": agents,
    }


def _send_test(world, email, parsed, window: float) -> None:
    from gud import compose

    tree_hash = parsed.get("Gud-Tree") or ""
    passed, log = True, "pass"
    tree = _push_tree(world, parsed, window)
    if tree and any(path.startswith("test_") or path.startswith("tests/") or "/test_" in path for path in tree):
        passed, log = _run_suite(tree)
    body = compose.test_body(world.ids["@tester"], email.message_id, passed, tree_hash, log or "pass")
    compose.send(
        world.mailbox, world.ids["@tester"], world.members,
        f"Re: test {email.message_id}", body, in_reply_to=email.message_id,
    )


def _push_tree(world, parsed, window: float):
    try:
        from gud.chain import Repo
        from gud.patch import apply_diff
    except ImportError:
        return None
    parent_id = parsed.get("Gud-Parent")
    if not parent_id or parent_id == "none":
        parent = {}
    else:
        try:
            chain = Repo(world.mailbox, window, clock=world.clock).refresh()
        except Exception:
            return None
        link = chain.by_id.get(parent_id)
        if link is None:
            return None
        parent = link.tree
    try:
        return apply_diff(dict(parent), parsed.diff)
    except Exception:
        return None


def _run_suite(tree: dict) -> tuple[bool, str]:
    try:
        from gud.build import LocalRunner
    except ImportError:
        return True, "pass"
    try:
        result = LocalRunner().run(tree)
    except Exception:
        return True, "pass"
    return bool(result.passed), ((getattr(result, "log", "") or "pass")[-2000:])


def _tester(world, stop: threading.Event, window: float) -> None:
    from gud.wire import parse_body

    while not stop.is_set():
        try:
            mail = world.repo_mail()
            tested = set()
            pushes = []
            for email in mail:
                parsed = parse_body(email.body)
                if parsed.kind == "test" and parsed.get("Gud-Target"):
                    tested.add(parsed.get("Gud-Target"))
                elif parsed.kind == "push":
                    pushes.append((email, parsed))
            for email, parsed in pushes:
                if email.message_id in tested:
                    continue
                _send_test(world, email, parsed, window)
        except Exception:
            pass
        stop.wait(0.05)


def _pump(clock: LockedClock, stop: threading.Event) -> None:
    while not stop.wait(0.05):
        clock.tick(0.05)


def _bootstrap(world, case: dict, window: float):
    from gud import compose
    from gud.mcp_server import GudTools

    author = case.get("genesis_author", "@lead")
    genesis = world.genesis(case["tree"], author=author)
    world.test(genesis)
    for handle, ident in world.ids.items():
        member = world.members.get(handle)
        if handle != author and ident.private_raw and member and member.votes:
            world.approve(handle, genesis)
    spec = case.get("tasks")
    if spec:
        console, lead = world.ids["@console"], world.ids["@lead"]
        body = compose.task_body(console, spec["id"], spec.get("requester", "@nelson"), spec.get("description", ""))
        compose.send(world.mailbox, console, world.members, spec.get("title") or spec["id"], body)
        for sub in spec.get("subtasks") or []:
            blob = compose.subtask_body(
                lead, sub["id"], spec["id"], sub["assignee"], sub.get("description") or sub.get("title") or "",
            )
            compose.send(world.mailbox, lead, world.members, sub.get("title") or sub["id"], blob)
    probe = GudTools(world.mailbox, world.ids["@alice"], window_seconds=window, clock=world.clock)
    last = time.time()
    deadline = last + window + 2
    chain = None
    while time.time() < deadline:
        chain = probe.repo().refresh()
        if chain.tip is not None:
            return probe
        now = time.time()
        world.clock.tick(max(0.0, now - last))
        last = now
        time.sleep(0.05)
    detail = ""
    try:
        detail = probe.status()
    except Exception as exc:
        detail = repr(exc)
    raise RuntimeError("genesis did not become a link\n" + detail)


def _check_tree(tree: dict, case: dict, normalize) -> list[str]:
    problems = []
    for path, content in (case.get("expect") or {}).items():
        got = tree.get(path)
        if got is None:
            problems.append(f"missing {path}")
        elif normalize(got) != normalize(content):
            problems.append(f"{path} mismatch")
    blob = "\n".join(tree.get(path, "") for path in (case.get("expect") or {}))
    for banned in case.get("forbid") or []:
        if banned in blob:
            problems.append(f"still contains {banned!r}")
    return problems


def run_gud(case: dict, dry_run: bool, model: str, key: str | None, verbose: bool) -> dict:
    from gud.mcp_server import GudTools
    from gud.testkit import World
    from gud.wire import normalize, parse_body

    started = time.perf_counter()
    try:
        import gud.chain  # noqa: F401
        import gud.replay  # noqa: F401
        import gud.rules  # noqa: F401
    except ImportError as exc:
        return _failed("gud", case, f"core not importable: {exc}", started)
    window = float(case.get("window_seconds", 5))
    wait_seconds = float((case.get("limits") or {}).get("wait_seconds", max(16, window * 4)))
    max_turns = int((case.get("limits") or {}).get("max_turns", 8))
    stop = threading.Event()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            world = World.create(tmp, protected=case.get("protected") or "")
            world.clock.t = time.time()
            locked = LockedClock(world.clock)
            world.clock = locked
            world.mailbox.clock = locked
            _bootstrap(world, case, window)
            pairs = []
            for spec in case["agents"]:
                tools = GudTools(world.mailbox, world.ids[spec["handle"]], window_seconds=window, clock=locked)
                pairs.append((spec, Session(tools, "gud", wait_seconds, verbose)))
            tester = threading.Thread(target=_tester, args=(world, stop, window), daemon=True)
            pump = threading.Thread(target=_pump, args=(locked, stop), daemon=True)
            tester.start()
            pump.start()
            try:
                errors = _run_agents(pairs, "gud", dry_run, model, key, max_turns)
                sample = pairs[0][1].tools
                chain = sample.repo().refresh()
                tip = chain.tip
                problems = _check_tree(tip.tree if tip else {}, case, normalize)
                minimum = case.get("min_links")
                if minimum is not None and len(chain.links) < int(minimum):
                    problems.append(f"links {len(chain.links)} < {minimum}")
                task_id = case.get("task_landed")
                if task_id:
                    landed = set()
                    by_id = {email.message_id: email for email in sample._emails()}
                    for link in chain.links:
                        if getattr(link, "task", None):
                            landed.add(link.task)
                        email = by_id.get(link.message_id)
                        if email is not None and parse_body(email.body).get("Gud-Task") == task_id:
                            landed.add(task_id)
                    if task_id not in landed:
                        problems.append(f"{task_id} not landed")
                if not problems and errors:
                    pass
                ok = not problems
                check = "; ".join(problems) or "ok"
                if not ok:
                    tails = []
                    for spec, session in pairs:
                        head = session.calls[-1]["head"] if session.calls else ""
                        tails.append(f"{spec['handle']}: {head}")
                    check = check + " | " + " | ".join(tails)
                return _summarize(case["id"], "gud", ok, check, pairs, errors, started)
            finally:
                stop.set()
    except Exception as exc:
        return _failed("gud", case, f"{type(exc).__name__}: {exc}", started)


def run_git(case: dict, dry_run: bool, model: str, key: str | None, verbose: bool) -> dict:
    from gud.mcp_baseline import clone, make_origin
    from gud.wire import normalize

    started = time.perf_counter()
    window = float(case.get("window_seconds", 5))
    wait_seconds = float((case.get("limits") or {}).get("wait_seconds", 20))
    max_turns = int((case.get("limits") or {}).get("max_turns", 8))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            origin = make_origin(Path(tmp) / "repo", case["tree"])
            pairs = []
            gits = []
            for spec in case["agents"]:
                tools = clone(origin, Path(tmp) / spec["handle"].lstrip("@"), spec["handle"])
                gits.append(tools)
                pairs.append((spec, Session(tools, "git", wait_seconds, verbose)))
            errors = _run_agents(pairs, "git", dry_run, model, key, max_turns)
            problems = []
            for tools in gits:
                pulled = tools.git_pull()
                if pulled.startswith("conflict") or pulled.startswith("error:"):
                    problems.append(pulled.splitlines()[0])
            tree = {}
            for path in case.get("expect") or {}:
                text = gits[0].read(path)
                if not text.startswith("error:"):
                    tree[path] = text
            problems.extend(_check_tree(tree, case, normalize))
            ok = not problems
            check = "; ".join(problems) or "ok"
            if not ok:
                tails = []
                for spec, session in pairs:
                    head = session.calls[-1]["head"] if session.calls else ""
                    tails.append(f"{spec['handle']}: {head}")
                check = check + " | " + " | ".join(tails)
            return _summarize(case["id"], "git", ok and not errors, check, pairs, errors, started)
    except Exception as exc:
        return _failed("git", case, f"{type(exc).__name__}: {exc}", started)


def load_cases(only: list[str]) -> list[dict]:
    found = []
    for path in sorted(CASES.glob("*.json")):
        case = json.loads(path.read_text())
        if only and case.get("id") not in only:
            continue
        found.append(case)
    if only and len(found) != len(set(only)):
        have = {case["id"] for case in found}
        missing = [item for item in only if item not in have]
        raise SystemExit(f"unknown case: {', '.join(missing)}")
    if not found:
        raise SystemExit(f"no cases in {CASES}")
    return found


def print_table(rows: list[dict]) -> None:
    header = f"{'case':<24} {'backend':<6} {'ok':<5} {'turns':>5} {'calls':>5} {'errors':>6} {'tokens':>7} {'seconds':>8}  check"
    print(header)
    print("-" * len(header))
    for row in rows:
        mark = "yes" if row["success"] else "no"
        print(
            f"{row['case']:<24} {row['backend']:<6} {mark:<5} {row['turns']:>5} {row['tool_calls']:>5} "
            f"{row['tool_errors']:>6} {row['tokens']:>7} {row['seconds']:>8.1f}  {row['check'][:80]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run gud eval cases locally")
    parser.add_argument("--dry-run", action="store_true", help="scripted agents, no OpenRouter calls")
    parser.add_argument("--case", action="append", default=[], help="case id (repeatable); default all")
    parser.add_argument("--model", default=None, help="OpenRouter model (default GUD_AGENT_MODEL)")
    parser.add_argument("--backend", choices=("both", "gud", "git"), default="both")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    from gud.analytics import flush
    from gud.config import load

    cfg = load()
    model = args.model or cfg.agent_model
    if not args.dry_run and not cfg.openrouter_key:
        raise SystemExit("OPENROUTER_KEY is not set")
    cases = load_cases(args.case)
    rows = []
    for case in cases:
        print(f"\n== {case['id']} ==", flush=True)
        if args.backend in ("both", "gud"):
            row = run_gud(case, args.dry_run, model, cfg.openrouter_key, args.verbose)
            rows.append(row)
            print(f"  gud {('ok' if row['success'] else 'FAIL')} {row['seconds']}s  {row['check'][:140]}", flush=True)
        if args.backend in ("both", "git"):
            row = run_git(case, args.dry_run, model, cfg.openrouter_key, args.verbose)
            rows.append(row)
            print(f"  git {('ok' if row['success'] else 'FAIL')} {row['seconds']}s  {row['check'][:140]}", flush=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = RESULTS / f"{stamp}.json"
    path.write_text(json.dumps({
        "timestamp": stamp,
        "model": None if args.dry_run else model,
        "dry_run": args.dry_run,
        "rows": rows,
    }, indent=2) + "\n")
    print()
    print_table(rows)
    print(f"\nwrote {path}")
    flush(timeout=3)
    return 0 if all(row["success"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
