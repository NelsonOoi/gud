"""The v1 tester (`gud tester`): a non-model member with role `tester` that runs the repo's test
policy (SPEC §10.4, §15) and signs `test` messages (§7).

Everything it does is derived from the replayed chain, read through a `TesterClient` (the
`RepoClient` subset it needs: `sync_log`, `replayer`, `seen`, `send`; `LocalClient` wraps a
`Replayer`, a fetch function and a send function so it plugs into a mirror follower, its own
mailbox, or a test kit). One `step()`:

1. syncs, scans the log for `test` messages (its own and other testers'), and re-sends any of its
   own results that were signed but never handed to the transport (crash between the two);
2. **push duties** (§15.1, §15.3) on the tip's candidates that need a vouching pass and have none:
   `on-paths` candidates get a `push`-scope test each; with `every-link on` the candidates are
   tested as one batched **stack** in race order (up to `stack-max`), and a failing stack is
   bisected over its prefixes, signing every run, so the passing prefix is vouched for and the
   culprit ends up alone at the front of a later stack, where a failing stack of one kills it;
3. **evidence** (§13.5): every evidence objection that lacks a verdict on a live candidate is run
   on the candidate's tree with the file added (`Gud-Result`) and, when the candidate belongs to
   the objected push, on its parent's tree with the file added (`Gud-Base-Result`). Revisions in
   the negotiation inherit confirmed objections and are re-run the same way;
4. **red tip** (§15.4): while red and `first_bad` is unknown, binary search between `last_green`
   and the lowest failing link, signing a `tip` result per probe;
5. **tip runs** on the `on-tip` cadence (every `links` new links or `time`, whichever first); while
   red with `first_bad` known, every new tip is tested so the chain can turn green;
6. **acceptance** (§16.2, §16.6): items whose work is done and whose criteria are pending are run
   on the tip (`Gud-Accept` paths as arguments to the `.gud/test` command, each `accept` line as
   its own command) while the chain is green;
7. **auto-revert** (§15.6): once `red_since + auto-revert` has passed with `first_bad` known, the
   tester sends the revert of `first_bad` onto the tip.

Idempotence: one result per (scope, target, tree) (plus the link for `accept`), remembered in
`state_dir/tester.json` *before* sending; the log scan also restores that memory if the state is
lost. Identical trees under the same command reuse one run (`runs` cache). Timeouts are honest:
the process group is killed and the result is `fail` with `timeout` in the text.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Protocol, Sequence

from gud.core import envelope as E
from gud.core.config import TestPolicy, glob_match
from gud.core.objects import ObjectError, Tree
from gud.core.ops import invert_ops
from gud.core.replay import ChainReader, Replayer, ReplayError, carried_state, stack_tree
from gud.core.types import (
    DEAD_STATUSES, Chain, Email, Envelope, Event, Handle, Hex, LinkState, LogEntry, Ms, ObjectionState,
    ProposalState, Record, Seen,
)
from gud.runners.base import LOG_LIMIT, RunnerError, RunnerUnavailable, RunResult, Runner
from gud.runners.docker import DockerRunner
from gud.runners.local import LocalRunner

__all__ = [
    "Runner", "RunResult", "RunnerUnavailable", "RunnerError", "LocalRunner", "DockerRunner", "make_runner",
    "runner_from_legacy", "TesterIdentity", "TesterClient", "LocalClient", "mailbox_sender", "Tester",
]

log = logging.getLogger("gud.tester")

_STATE_VERSION = 1
_RUN_CACHE_MAX = 4096
_RUN_LOG_KEEP = 4096


# ---------------------------------------------------------------- runners

def make_runner(kind: Literal["local", "docker", "instabox"] = "local", **kw: Any) -> Runner:
    """`local` -> LocalRunner; `docker` -> DockerRunner (local for policies without an image);
    `instabox` -> InstaBoxRunner (needs INSTABOX_API_KEY or an `instabox login`)."""
    if kind == "local":
        return LocalRunner(**kw)
    if kind == "docker":
        return DockerRunner(**kw)
    if kind == "instabox":
        from gud.runners.instabox import InstaBoxRunner
        return InstaBoxRunner(**kw)
    raise ValueError(f"unknown runner {kind!r} (expected local, docker or instabox)")


runner_from_legacy = make_runner


# ---------------------------------------------------------------- client side

@dataclass(frozen=True)
class TesterIdentity:
    __test__ = False                        # not a pytest class
    handle: Handle
    private_key: bytes                      # raw 32-byte Ed25519 key of the tester member

    @property
    def public_key(self) -> str:
        return E.public_of(self.private_key)


class TesterClient(Protocol):
    """What the tester needs from a member client (a subset of `gud.wc.remote.RepoClient`)."""
    __test__ = False
    def sync_log(self) -> object: ...
    def replayer(self) -> Replayer: ...
    def seen(self) -> Seen: ...
    def send(self, env: Envelope, *, summary: str, text: str = "") -> Hex: ...


class LocalClient:
    """A `TesterClient` over a local `Replayer`.

    `fetch()` returns (emails, log entries) to feed (already-fed items are ignored by replay);
    `send(env, summary, text)` hands one signed envelope to a transport (see `mailbox_sender`)."""

    def __init__(self, replayer: Replayer, fetch: Callable[[], tuple[Iterable[Email], Sequence[LogEntry]]],
                 send: Callable[[Envelope, str, str], object], *, now: Callable[[], Ms] | None = None) -> None:
        self._replayer = replayer
        self._fetch = fetch
        self._send = send
        self._now = now or (lambda: int(time.time() * 1000))

    def sync_log(self) -> object:
        emails, entries = self._fetch()
        return self._replayer.feed(emails, entries, self._now())

    def replayer(self) -> Replayer:
        return self._replayer

    def seen(self) -> Seen:
        head = self._replayer.log().head
        if head is None:
            raise ReplayError("no host log yet")
        return Seen(head.seq, head.hash)

    def send(self, env: Envelope, *, summary: str, text: str = "") -> Hex:
        self._send(env, summary, text)
        return E.gud_id(env)


def mailbox_sender(mailbox: Any, *, from_addr: str, repo: str) -> Callable[[Envelope, str, str], object]:
    """A send function for `LocalClient` that mails the envelope to the repo address through a
    `gud.transport` mailbox (from `registry.open_mailbox`, `FakeMailbox`, ...)."""
    from gud.transport import mime
    from gud.transport.base import Outgoing

    def send(env: Envelope, summary: str, text: str) -> object:
        gid = E.gud_id(env)
        return mailbox.send(Outgoing(
            from_addr=from_addr, to=(repo,), subject=mime.subject_for(env, gid, summary),
            text=text or mime.render_human_part(env, gid), envelope=E.encode(env)))

    return send


# ---------------------------------------------------------------- internals

@dataclass
class _View:
    replayer: Replayer
    reader: ChainReader
    chain: Chain


@dataclass
class _Out:
    """One result to sign: the dedupe key, the `test` headers and the log text."""
    key: str
    scope: str
    headers: dict[str, str]
    result: str
    text: str
    summary: str


@dataclass
class _Job:
    label: str
    fn: Callable[[], list[_Out]]
    outs: list[_Out] = field(default_factory=list)


def _key(scope: str, target: Hex, tree: Hex, link: Hex | None = None) -> str:
    return f"{scope}|{target}|{tree}|{link or ''}"


def _summary(scope: str, result: str, target: Hex) -> str:
    return f"{scope} {result} {target[:12]}"


def _text(r: RunResult, head: str) -> str:
    line = f"{head}\nrunner {r.runner}, {r.seconds:.2f}s" + (", timeout" if r.timed_out else "") + "\n"
    budget = LOG_LIMIT - len(line.encode())
    body = r.log.encode("utf-8")
    if len(body) > budget:
        body = body[-budget:]
    return line + body.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- the tester

class Tester:
    __test__ = False                        # not a pytest class

    def __init__(self, client: TesterClient, runner: Runner, *, identity: TesterIdentity, parallel: int = 1,
                 clock: Callable[[], float] = time.time, state_dir: Path | None = None) -> None:
        if parallel < 1:
            raise ValueError("parallel must be at least 1")
        self.client = client
        self.runner = runner
        self.identity = identity
        self.parallel = parallel
        self.clock = clock
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        # persisted
        self._sent: dict[str, dict] = {}          # key -> {id, env, sent, result, scope}
        self._runs: dict[str, dict] = {}          # run key -> RunResult fields (log tail)
        self._tip_ref: list[int] | None = None    # [link seq, ms] of the latest tip run
        self._reverts: dict[Hex, Hex] = {}        # first_bad -> revert push id
        # rebuilt from the log on every start
        self._scan_next = 0
        self._results: dict[str, str] = {}        # key -> result of the first valid tester result
        self._verdicts: dict[Hex, str] = {}       # link state -> first tip verdict
        self._ev_tests: dict[Hex, list[Hex]] = {} # objection -> Gud-On values of its evidence tests
        self._view_cache: _View | None = None
        self._load()

    # ------------------------------------------------------------ persistence

    def _state_path(self) -> Path | None:
        return self.state_dir / "tester.json" if self.state_dir is not None else None

    def _load(self) -> None:
        path = self._state_path()
        if path is None or not path.exists():
            return
        doc = json.loads(path.read_text())
        if doc.get("version") != _STATE_VERSION or doc.get("handle") != self.identity.handle:
            raise ValueError(f"{path}: state of another tester or version")
        self._sent = dict(doc.get("sent", {}))
        self._runs = dict(doc.get("runs", {}))
        self._tip_ref = doc.get("tip_ref")
        self._reverts = dict(doc.get("reverts", {}))

    def _save(self) -> None:
        path = self._state_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            doc = {"version": _STATE_VERSION, "handle": self.identity.handle, "sent": self._sent,
                   "runs": self._runs, "tip_ref": self._tip_ref, "reverts": self._reverts}
            data = json.dumps(doc, sort_keys=True).encode()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ------------------------------------------------------------ the chain

    def _refresh(self, *, sync: bool = True) -> _View:
        if sync:
            self.client.sync_log()
        r = self.client.replayer()
        reader = r.reader()
        view = _View(r, reader, reader.chain)
        self._scan(view)
        self._view_cache = view
        return view

    def _view(self) -> _View:
        return self._view_cache if self._view_cache is not None else self._refresh()

    def _scan(self, v: _View) -> None:
        """Learn every `test` result on the chain (signed by a tester at the tip)."""
        head = v.chain.log_head.seq
        members = v.reader.members() if v.chain.tip is not None else None
        for seq, gid, at in v.replayer.log().mail_ids(self._scan_next):
            if seq > head:
                break
            self._scan_next = seq + 1
            email = v.reader.email(gid)
            env = email.envelope if email is not None else None
            if env is None or env.kind != "test" or members is None:
                continue
            author = env.author or ""
            if author == self.identity.handle:
                ok = E.verify(env, self.identity.public_key)
            else:
                m = members.get(author)
                ok = m is not None and m.role == "tester" and E.verify(env, m.public_key)
            if not ok:
                continue
            scope, target = env.get("Gud-Scope") or "", env.get("Gud-Target") or ""
            tree, result = env.get("Gud-Tree") or "", env.get("Gud-Result") or ""
            key = _key(scope, target, tree, env.get("Gud-Link") if scope == "accept" else None)
            self._results.setdefault(key, result)
            if author == self.identity.handle and key not in self._sent:
                self._sent[key] = {"id": gid, "env": None, "sent": True, "result": result, "scope": scope}
            if scope == "tip":
                link = v.chain.link(target)
                if link is not None and link.tree_hash == tree and target not in self._verdicts:
                    self._verdicts[target] = result
                    self._note_tip_run(link.seq, at)
            elif scope == "evidence":
                self._ev_tests.setdefault(target, []).append(env.get("Gud-On") or "")

    def _note_tip_run(self, seq: int, at: Ms) -> None:
        with self._lock:
            if self._tip_ref is None or seq > self._tip_ref[0]:
                self._tip_ref = [seq, max(at, self._tip_ref[1] if self._tip_ref else 0)]

    def _known(self, key: str) -> str | None:
        with self._lock:
            rec = self._sent.get(key)
        if rec is not None:
            return rec["result"]
        return self._results.get(key)

    def _done(self, key: str) -> bool:
        return self._known(key) is not None

    # ------------------------------------------------------------ running

    def _run(self, tree: Tree, policy: TestPolicy, extra_args: Sequence[str] = (),
             command: str | None = None) -> RunResult:
        """Run one command on one tree, through the run cache (same tree + command = same run)."""
        cmd = command if command is not None else (policy.command or "")
        rkey = json.dumps([tree.hash(), cmd, policy.image, policy.timeout, list(extra_args)])
        with self._lock:
            hit = self._runs.get(rkey)
        if hit is not None:
            return RunResult(**hit)
        result = self.runner.run(tree.to_files(), cmd, image=policy.image, timeout_ms=policy.timeout,
                                 extra_args=tuple(extra_args))
        keep = dict(result.__dict__)
        keep["log"] = result.log[-_RUN_LOG_KEEP:]
        with self._lock:
            self._runs[rkey] = keep
            while len(self._runs) > _RUN_CACHE_MAX:
                del self._runs[next(iter(self._runs))]
        return result

    def _execute(self, jobs: Sequence[_Job]) -> list[Hex]:
        """Run jobs (at most `parallel` at once), then sign and send their results in job order."""
        if not jobs:
            return []

        def go(job: _Job) -> None:
            try:
                job.outs = job.fn()
            except (RunnerUnavailable, RunnerError) as exc:
                log.warning("tester: %s: %s", job.label, exc)
            except Exception:
                log.exception("tester: %s failed", job.label)

        if self.parallel > 1 and len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="gud-tester") as pool:
                list(pool.map(go, jobs))
        else:
            for job in jobs:
                go(job)
        sent: list[Hex] = []
        for job in jobs:
            for out in job.outs:
                gid = self._sign_and_send(out)
                if gid is not None:
                    sent.append(gid)
        return sent

    def _sign_and_send(self, out: _Out) -> Hex | None:
        """Persist the signed envelope first, then send: a crash in between re-sends the same bytes
        on restart (same gud id), never a second result for the same key."""
        with self._send_lock:
            if self._done(out.key):
                return None
            records = (Record("text", (), out.text.encode("utf-8")),) if out.text else ()
            env = E.build("test", author=self.identity.handle, headers=out.headers, records=records,
                          sent=E.now_sent(self.clock), seen=self.client.seen())
            problem = E.validate_kind(env)
            if problem is not None:
                log.error("tester: refusing to send an invalid test (%s)", problem)
                return None
            env = E.sign(env, self.identity.private_key)
            gid = E.gud_id(env)
            with self._lock:
                self._sent[out.key] = {"id": gid, "env": base64.b64encode(E.encode(env)).decode(), "sent": False,
                                       "result": out.result, "scope": out.scope}
            self._save()
            self.client.send(env, summary=out.summary)
            with self._lock:
                self._sent[out.key]["sent"] = True
            self._save()
            log.info("tester: %s %s", out.summary, gid[:12])
            return gid

    def _resend_pending(self) -> list[Hex]:
        out = []
        with self._lock:
            pending = [(k, rec) for k, rec in self._sent.items() if not rec["sent"] and rec.get("env")]
        for key, rec in pending:
            env = E.decode(base64.b64decode(rec["env"]))
            self.client.send(env, summary=_summary(rec["scope"], rec["result"], env.get("Gud-Target") or ""))
            with self._lock:
                self._sent[key]["sent"] = True
            self._save()
            out.append(rec["id"])
        return out

    # ------------------------------------------------------------ the pass

    def step(self) -> list[Hex]:
        """One pass over every duty. Returns the ids of the `test` (and revert) messages sent."""
        v = self._refresh()
        sent = self._resend_pending()
        jobs = self._push_jobs(v) + self._evidence_jobs(v)
        sent += self._execute(jobs)
        sent += self.bisect()
        tip = self.due_tip_run()
        if tip is not None:
            gid = self.test_tip(tip)
            if gid is not None:
                sent.append(gid)
        sent += self._execute(self._accept_jobs(v))
        rev = self.maybe_auto_revert()
        if rev is not None:
            sent.append(rev)
        self._save()
        self._view_cache = None
        return sent

    def run_forever(self, events: Iterator[Event] | None = None, *, interval: float = 5.0,
                    stop: threading.Event | None = None) -> None:
        """Step on every event (webhook/SSE client), or every `interval` seconds without one."""
        stop = stop or threading.Event()
        try:
            if events is not None:
                self.step()
                for _ in events:
                    if stop.is_set():
                        return
                    self.step()
            else:
                while not stop.is_set():
                    try:
                        self.step()
                    except ReplayError as exc:
                        log.error("tester: replay stopped: %s", exc)
                    stop.wait(interval)
        finally:
            closer = getattr(self.runner, "close", None)
            if closer is not None:
                closer()

    # ------------------------------------------------------------ push and stack duties (§15.1, §15.3)

    @staticmethod
    def _candidates(v: _View) -> list[ProposalState]:
        tip = v.chain.tip
        if tip is None:
            return []
        out = [p for p in v.chain.proposals.values()
               if p.parent == tip.state and p.status not in DEAD_STATUSES and p.status not in ("orphan", "pending_blobs")
               and p.tree_hash is not None and not p.pending_blobs]
        out.sort(key=lambda p: (p.received_at, p.push))
        return out

    @staticmethod
    def _needs(p: ProposalState) -> bool:
        return p.test_needed and p.test_result is None

    def _push_jobs(self, v: _View) -> list[_Job]:
        tip = v.chain.tip
        if tip is None:
            return []
        policy = v.reader.rules(tip.state).test
        if not policy.command:
            return []
        cands = self._candidates(v)
        if not any(self._needs(p) for p in cands):
            return []
        if not policy.every_link:
            return [self._push_job(v, p, policy) for p in cands if self._needs(p)
                    and not self._done(_key("push", p.id, p.tree_hash or ""))]
        # every-link: one stack in race order, parked candidates last (they no longer hold the race)
        order = [p for p in cands if p.status != "parked"] + [p for p in cands if p.status == "parked"]
        pushes: list[Hex] = []
        trees: list[Tree] = []
        props: list[ProposalState] = []
        for p in order:
            if len(pushes) >= policy.stack_max:
                break
            alone, n1 = stack_tree(v.reader, tip.state, [p.push])
            if n1 < 1 or alone.hash() != p.tree_hash:
                break            # the file-mode overlay is not this candidate's tree (line-mode carry)
            tree, n = stack_tree(v.reader, tip.state, pushes + [p.push])
            if n < len(pushes) + 1:
                break            # not carryable onto the stack so far: ends the stack (§15.3)
            pushes.append(p.push)
            trees.append(tree)
            props.append(p)
        last = max((i for i, p in enumerate(props) if self._needs(p)), default=-1)
        if last < 0:
            first = order[0] if order else None
            if first is not None and self._needs(first) and not props:
                # the stack overlay is not this candidate's tree (line-mode carry): test it on its own
                if not self._done(_key("push", first.id, first.tree_hash or "")):
                    return [self._push_job(v, first, policy)]
            return []
        pushes, trees = pushes[:last + 1], trees[:last + 1]
        if self._done(_key("stack", tip.state, trees[-1].hash())):
            return []
        return [self._stack_job(tip.state, pushes, trees, policy)]

    def _push_job(self, v: _View, p: ProposalState, policy: TestPolicy) -> _Job:
        tree = v.reader.candidate_tree(p.id)

        def fn() -> list[_Out]:
            if tree is None:
                return []
            r = self._run(tree, policy)
            res = "pass" if r.passed else "fail"
            return [_Out(_key("push", p.id, tree.hash()), "push",
                         {"Gud-Scope": "push", "Gud-Target": p.id, "Gud-Tree": tree.hash(), "Gud-Result": res},
                         res, _text(r, f"push {p.push[:12]} on {p.parent[:12] if p.parent else '-'}: {res}"),
                         _summary("push", res, p.id))]

        return _Job(f"push {p.id[:12]}", fn)

    def _stack_job(self, base: Hex, pushes: Sequence[Hex], trees: Sequence[Tree], policy: TestPolicy) -> _Job:
        pushes, trees = list(pushes), list(trees)

        def out_for(k: int, r: RunResult) -> _Out:
            res = "pass" if r.passed else "fail"
            h = trees[k - 1].hash()
            return _Out(_key("stack", base, h), "stack",
                        {"Gud-Scope": "stack", "Gud-Target": base, "Gud-Stack": E.render_list(pushes[:k]),
                         "Gud-Tree": h, "Gud-Result": res}, res,
                        _text(r, f"stack of {k} on {base[:12]}: {res}"), _summary(f"stack[{k}]", res, base))

        def result_of(k: int, outs: list[_Out]) -> bool:
            known = self._known(_key("stack", base, trees[k - 1].hash()))
            if known is not None:
                return known == "pass"
            r = self._run(trees[k - 1], policy)
            outs.append(out_for(k, r))
            return r.passed

        def fn() -> list[_Out]:
            outs: list[_Out] = []
            k = len(pushes)
            if result_of(k, outs):
                return outs
            lo, hi = 0, k              # prefix lo passes (the base), prefix hi fails
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if result_of(mid, outs):
                    lo = mid
                else:
                    hi = mid
            return outs

        return _Job(f"stack {len(pushes)} on {base[:12]}", fn)

    def test_push(self, candidate: Hex) -> Hex | None:
        """Push-scope test of one candidate of the tip (§15.1)."""
        v = self._view()
        p = v.chain.proposals.get(candidate)
        if p is None or p.parent is None or p.tree_hash is None:
            return None
        policy = v.reader.rules(p.parent).test
        if not policy.command:
            return None
        sent = self._execute([self._push_job(v, p, policy)])
        return sent[0] if sent else None

    def test_stack(self, base: Hex, pushes: Sequence[Hex]) -> Hex | None:
        """Stack test of `pushes` on link `base` (§15.3); bisects over prefixes on failure. Returns the
        id of the result for the whole stack (as far as the pushes carry in sequence)."""
        v = self._view()
        policy = v.reader.rules(base).test
        if not policy.command or not pushes:
            return None
        trees: list[Tree] = []
        for i in range(1, len(pushes) + 1):
            tree, n = stack_tree(v.reader, base, list(pushes[:i]))
            if n < i:
                break
            trees.append(tree)
        if not trees:
            return None
        sent = self._execute([self._stack_job(base, list(pushes[:len(trees)]), trees, policy)])
        return sent[0] if sent else None

    # ------------------------------------------------------------ tip, red, bisect (§15.4–15.6)

    def _verdict(self, link: LinkState) -> str | None:
        return self._verdicts.get(link.state) or self._known(_key("tip", link.state, link.tree_hash))

    def _tip_out(self, v: _View, link: LinkState) -> _Out | None:
        key = _key("tip", link.state, link.tree_hash)
        if self._done(key):
            return None
        policy = v.reader.rules(link.state).test
        if not policy.command and v.chain.tip is not None:
            policy = v.reader.rules(v.chain.tip.state).test       # a link without a command: today's policy
        if not policy.command:
            return None
        r = self._run(v.reader.tree(link.state), policy)
        res = "pass" if r.passed else "fail"
        return _Out(key, "tip", {"Gud-Scope": "tip", "Gud-Target": link.state, "Gud-Tree": link.tree_hash,
                                 "Gud-Result": res}, res, _text(r, f"tip L{link.seq} {link.state[:12]}: {res}"),
                    _summary("tip", res, link.state))

    def _send_tip(self, v: _View, link: LinkState) -> Hex | None:
        job = _Job(f"tip {link.state[:12]}", lambda: [o for o in [self._tip_out(v, link)] if o is not None])
        sent = self._execute([job])
        if sent:
            self._note_tip_run(link.seq, int(self.clock() * 1000))
        return sent[0] if sent else None

    def test_tip(self, link: Hex) -> Hex | None:
        """Tip-scope test of link `link` (any link, not only the tip)."""
        v = self._view()
        target = v.chain.link(link)
        if target is None:
            return None
        return self._send_tip(v, target)

    def due_tip_run(self) -> Hex | None:
        """The link due for a tip run under the `on-tip` cadence, or None (§15.4: every `links` new links
        or every `time`, whichever comes first). While red with `first_bad` known, every untested tip."""
        v = self._view()
        tip = v.chain.tip
        if tip is None or self._verdict(tip) is not None:
            return None
        policy = v.reader.rules(tip.state).test
        if not policy.command:
            return None
        if v.chain.red.red and v.chain.red.first_bad is not None:
            return tip.state
        if policy.on_tip_links is None and policy.on_tip_time is None:
            return None
        ref_seq, ref_ms = self._tip_ref if self._tip_ref is not None else (-1, v.chain.links[0].linked_at)
        if policy.on_tip_links is not None and tip.seq - ref_seq >= policy.on_tip_links:
            return tip.state
        now = int(self.clock() * 1000)
        if policy.on_tip_time is not None and tip.seq > ref_seq and now - ref_ms >= policy.on_tip_time:
            return tip.state
        return None

    def bisect(self) -> list[Hex]:
        """§15.4: while red and `first_bad` is unknown, binary search between the last green link and
        the lowest failing link above it, one signed `tip` result per probe."""
        v = self._view()
        red = v.chain.red
        if not red.red or red.first_bad is not None:
            return []
        links = v.chain.links
        green = v.chain.link(red.last_green) if red.last_green else None
        lo = green.seq if green is not None else -1
        fails = [l.seq for l in links if l.seq > lo and self._verdict(l) == "fail"]
        if not fails:
            return []
        hi = min(fails)
        sent: list[Hex] = []
        while hi - lo > 1:
            mid = (lo + hi) // 2
            link = links[mid]
            known = self._verdict(link)
            if known is None:
                gid = self._send_tip(v, link)
                if gid is None:
                    break
                sent.append(gid)
                known = self._verdict(link)
            if known == "pass":
                lo = mid
            else:
                hi = mid
        return sent

    def maybe_auto_revert(self) -> Hex | None:
        """§15.6: once `red_since + auto-revert` has passed with `first_bad` known, send the revert of
        `first_bad` onto the tip (one per first_bad, again only if the previous one died)."""
        v = self._view()
        red, tip = v.chain.red, v.chain.tip
        if tip is None or not red.red or red.first_bad is None or red.red_since is None:
            return None
        policy = v.reader.rules(tip.state).test
        if policy.auto_revert is None or int(self.clock() * 1000) < red.red_since + policy.auto_revert:
            return None
        bad = v.chain.link(red.first_bad)
        if bad is None or bad.parent is None:
            return None
        prev = self._reverts.get(bad.state)
        if prev is not None:
            prop = v.chain.push_status(prev)
            linked = any(l.push == prev for l in v.chain.links)
            if linked or prop is None or prop.status not in DEAD_STATUSES:
                return None
        before = v.reader.tree(bad.parent)
        tree, conflicts = invert_ops(before, v.reader.tree(bad.state), v.reader.tree(tip.state))
        if conflicts:
            log.warning("tester: cannot auto-revert %s: conflicts in %s", bad.state[:12], ", ".join(conflicts))
            return None
        env = E.build("revert", author=self.identity.handle, headers={
            "Gud-Parent": tip.state, "Gud-Revert": bad.state, "Gud-Tree": tree.hash()},
            records=(Record("text", (), f"auto-revert of L{bad.seq} {bad.state[:12]} (red since "
                                        f"{red.red_since}, first bad)".encode()),),
            sent=E.now_sent(self.clock), seen=self.client.seen())
        env = E.sign(env, self.identity.private_key)
        gid = E.gud_id(env)
        with self._lock:
            self._reverts[bad.state] = gid
        self._save()
        self.client.send(env, summary=f"auto-revert {bad.state[:12]}")
        return gid

    # ------------------------------------------------------------ evidence (§13.5)

    def _cand_ids(self, v: _View, push: Hex, named_parent: Hex) -> set[Hex]:
        start = v.chain.link(named_parent)
        ids = {push}
        for link in v.chain.links[(start.seq if start else 0):]:
            ids.add(carried_state(push, link.state))
        return ids

    def _evidence_bytes(self, v: _View, o: ObjectionState) -> bytes | None:
        ev = o.evidence
        if ev is None:
            return None
        data = ev.inline
        if data is None:
            email = v.reader.email(o.id)
            if email is not None and email.envelope is not None:
                for rec in email.envelope.records_named("evidence"):
                    if rec.args and rec.args[0] == ev.path and rec.data:
                        data = rec.data
        if data is None and v.reader.store.has(ev.sha256):
            data = v.reader.store.get(ev.sha256)
        if data is None or E.sha256_hex(data) != ev.sha256 or len(data) != ev.size:
            return None
        return data

    def _evidence_jobs(self, v: _View) -> list[_Job]:
        jobs: list[_Job] = []
        for p in sorted(v.chain.proposals.values(), key=lambda p: (p.received_at, p.id)):
            if p.parent is None or p.status in DEAD_STATUSES or p.tree_hash is None:
                continue
            ids = None
            for o in p.objections:
                if o.evidence is None or o.status == "void":
                    continue
                ids = ids if ids is not None else self._cand_ids(v, p.push, p.named_parent)
                if any(on in ids for on in self._ev_tests.get(o.id, ())):
                    continue
                job = self._evidence_job(v, o, p)
                if job is not None:
                    jobs.append(job)
        return jobs

    def _evidence_job(self, v: _View, o: ObjectionState, p: ProposalState) -> _Job | None:
        ev = o.evidence
        assert ev is not None
        data = self._evidence_bytes(v, o)
        cand = v.reader.candidate_tree(p.id)
        if data is None or cand is None or p.parent is None:
            return None
        policy = v.reader.rules(p.parent).test
        if not policy.command:
            return None
        email = v.reader.email(o.id)
        target = email.envelope.get("Gud-Target") if email is not None and email.envelope is not None else None
        own = target is not None and target in self._cand_ids(v, p.push, p.named_parent)
        parent_tree = v.reader.tree(p.parent)

        def with_file(tree: Tree) -> Tree:
            entry = tree.get(ev.path)
            if entry is not None and entry.blob == ev.sha256:
                return tree
            return tree.set(ev.path, "100644", data)

        def fn() -> list[_Out]:
            try:
                on_tree = with_file(cand)
            except (ObjectError, KeyError) as exc:
                log.warning("tester: evidence %s cannot be added to %s: %s", ev.path, p.id[:12], exc)
                return []
            key = _key("evidence", o.id, on_tree.hash())
            if self._done(key):
                return []
            headers = {"Gud-Scope": "evidence", "Gud-Target": o.id, "Gud-On": p.id, "Gud-Tree": on_tree.hash()}
            lines = []
            if own:
                try:
                    base = self._run(with_file(parent_tree), policy, [ev.path])
                    base_res = "pass" if base.passed else "fail"
                except (ObjectError, KeyError):
                    base, base_res = None, "fail"
                headers["Gud-Base-Result"] = base_res
                lines.append(f"base (parent {p.parent[:12]} + {ev.path}): {base_res}")
            r = self._run(on_tree, policy, [ev.path])
            res = "pass" if r.passed else "fail"
            headers["Gud-Result"] = res
            lines.append(f"candidate {p.id[:12]} + {ev.path}: {res}")
            if own:
                verdict = "confirmed" if headers["Gud-Base-Result"] == "pass" and res == "fail" else "refuted"
                lines.append(f"evidence {verdict}")
            return [_Out(key, "evidence", headers, res, _text(r, "\n".join(lines)), _summary("evidence", res, o.id))]

        return _Job(f"evidence {o.id[:12]} on {p.id[:12]}", fn)

    def test_evidence(self, objection: Hex, candidate: Hex) -> Hex | None:
        """Evidence test of `objection` on `candidate` (§13.5)."""
        v = self._view()
        p = v.chain.proposals.get(candidate)
        if p is None:
            return None
        for o in p.objections:
            if o.id == objection:
                job = self._evidence_job(v, o, p)
                sent = self._execute([job] if job else [])
                return sent[0] if sent else None
        return None

    # ------------------------------------------------------------ acceptance (§16.2, §16.6)

    def _accept_jobs(self, v: _View) -> list[_Job]:
        tip = v.chain.tip
        if tip is None or v.chain.red.red or self._verdict(tip) == "fail":
            return []              # a pass received while red does not count (§16.6)
        jobs = []
        for t in sorted(v.chain.tasks.values(), key=lambda t: (t.opened_at, t.id)):
            if t.status != "work_done" or not (t.accept_paths or t.accept_cmds):
                continue
            if self._done(_key("accept", t.id, tip.tree_hash, tip.state)):
                continue
            jobs.append(self._accept_job(v, t.id, tip))
        return jobs

    def _accept_job(self, v: _View, task: Hex, link: LinkState) -> _Job:
        t = v.chain.tasks[task]
        policy = v.reader.rules(link.state).test
        tree = v.reader.tree(link.state)

        def fn() -> list[_Out]:
            key = _key("accept", task, link.tree_hash, link.state)
            notes: list[str] = []
            results: list[RunResult] = []
            ok = True
            if not policy.command:
                ok = False
                notes.append("no .gud/test command: acceptance criteria cannot pass (§10.4)")
            else:
                if t.accept_paths:
                    paths = list(tree.paths())
                    args: list[str] = []
                    for pattern in t.accept_paths:
                        hits = [p for p in paths if glob_match(pattern, p)]
                        if not hits:
                            ok = False
                            notes.append(f"criteria {pattern!r} matches no file")
                        args += [h for h in hits if h not in args]
                    if ok:
                        r = self._run(tree, policy, args)
                        results.append(r)
                        ok = ok and r.passed
                        notes.append(f"{policy.command} {' '.join(args)}: {'pass' if r.passed else 'fail'}")
                for cmd in t.accept_cmds:
                    if not ok:
                        break
                    r = self._run(tree, policy, command=cmd)
                    results.append(r)
                    ok = r.passed
                    notes.append(f"{cmd}: {'pass' if r.passed else 'fail'}")
            res = "pass" if ok else "fail"
            head = f"accept {t.alias} on L{link.seq} {link.state[:12]}: {res}\n" + "\n".join(notes)
            text = _text(results[-1], head) if results else head
            return [_Out(key, "accept", {"Gud-Scope": "accept", "Gud-Target": task, "Gud-Link": link.state,
                                         "Gud-Tree": link.tree_hash, "Gud-Result": res}, res, text,
                         _summary("accept", res, task))]

        return _Job(f"accept {task[:12]}", fn)

    def test_accept(self, task: Hex) -> Hex | None:
        """Accept-scope run of a task's criteria on the tip (§16.2)."""
        v = self._view()
        tip = v.chain.tip
        t = v.chain.tasks.get(task)
        if tip is None or t is None or not (t.accept_paths or t.accept_cmds):
            return None
        sent = self._execute([self._accept_job(v, task, tip)])
        return sent[0] if sent else None


# ---------------------------------------------------------------- `gud tester`

def main(argv: Sequence[str] | None = None) -> None:
    """`gud tester --handle @t1 --key <file>`: follow the repo through `gud.wc.remote.RepoClient`."""
    parser = argparse.ArgumentParser(prog="gud tester")
    parser.add_argument("--handle", required=True)
    parser.add_argument("--key", required=True, help="file with the tester's raw 32-byte Ed25519 key (or its hex)")
    parser.add_argument("--host", required=True, help="host mirror URL")
    parser.add_argument("--repo", required=True, help="repo address")
    parser.add_argument("--home", default=os.environ.get("GUD_HOME", str(Path.home() / ".gud")))
    parser.add_argument("--runner", default="local", choices=["local", "docker", "instabox"])
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="one step, then exit")
    args = parser.parse_args(argv)
    handle = args.handle if args.handle.startswith("@") else "@" + args.handle
    raw = Path(args.key).read_bytes()
    key = raw if len(raw) == 32 else bytes.fromhex(raw.decode().strip())
    try:
        from gud.wc.remote import RepoClient  # type: ignore[import-not-found]
    except ImportError:
        sys.exit("gud tester needs gud.wc.remote.RepoClient (the mirror client), which is not built yet; "
                 "embed Tester with a LocalClient instead")
    from gud.transport import registry
    home = Path(args.home)
    name, cfg = registry.member_config(home, handle)
    client = RepoClient(args.host, handle, key, send_via=registry.open_mailbox(name, cfg), repo_address=args.repo,
                        home=home)
    tester = Tester(client, make_runner(args.runner), identity=TesterIdentity(handle, key), parallel=args.parallel,
                    state_dir=home / "tester" / handle)
    if args.once:
        tester.step()
    else:
        tester.run_forever(interval=args.interval)


if __name__ == "__main__":
    main()
