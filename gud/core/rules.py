"""Consent v1 (SPEC §13): fading line ownership, declared owners, windows, objections, escalations.

Pure functions over a parent link's state. `core.replay` gathers the inputs (verified votes,
answers, evidence verdicts) for one candidate and calls `evaluate_consent`; it never decides
consent itself.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from typing import Callable, Literal, Mapping, Sequence

from .config import MEMBERS, ConfigError, Members, RepoConfig, RepoRules, parse_members
from .envelope import sha256_hex
from .objects import BlameMap, Tree
from .ops import ChangeOp, touched_lines, touched_paths
from .types import EscalationState, Evidence, Handle, Hex, Ms, ObjectionState

INF: Ms = 1 << 62   # "never"; larger than any protocol time


class ConsentError(ValueError):
    pass


# ---------------------------------------------------------------- plan (§13.1, §13.2)

@dataclass(frozen=True)
class ParentView:
    """What consent needs from the parent link Y."""
    seq: int
    linked_at: Ms
    tree: Tree
    blame: BlameMap
    rules: RepoRules
    link_time: Callable[[int], Ms]


@dataclass(frozen=True)
class ConsentPlan:
    line_owners: tuple[Handle, ...]
    lazy_declared: tuple[Handle, ...]
    explicit: tuple[tuple[str, tuple[Handle, ...]], ...]
    askers: tuple[Handle, ...]
    eligible_objectors: tuple[Handle, ...]
    quorum_voters: tuple[Handle, ...] | None
    quorum_needed: int | None
    members_rule: bool
    declared: tuple[tuple[str, str, tuple[Handle, ...]], ...]
    humans: tuple[Handle, ...] = ()          # human voters at Y (members rule approvers)

    def to_json(self) -> dict:
        return {
            "line_owners": list(self.line_owners), "lazy_declared": list(self.lazy_declared),
            "explicit": [[p, list(o)] for p, o in self.explicit], "askers": list(self.askers),
            "eligible_objectors": list(self.eligible_objectors),
            "quorum_voters": None if self.quorum_voters is None else list(self.quorum_voters),
            "quorum_needed": self.quorum_needed, "members_rule": self.members_rule,
            "declared": [[p, r, list(o)] for p, r, o in self.declared], "humans": list(self.humans),
        }

    @classmethod
    def from_json(cls, d: Mapping) -> ConsentPlan:
        return cls(
            tuple(d["line_owners"]), tuple(d["lazy_declared"]),
            tuple((p, tuple(o)) for p, o in d["explicit"]), tuple(d["askers"]),
            tuple(d["eligible_objectors"]),
            None if d["quorum_voters"] is None else tuple(d["quorum_voters"]),
            d["quorum_needed"], d["members_rule"],
            tuple((p, r, tuple(o)) for p, r, o in d["declared"]), tuple(d.get("humans", ())),
        )


def live(seq: int, parent: ParentView, eval_time: Ms) -> bool:
    """§13.1: blame written at link `seq` still gives a say at parent Y for evaluation time `eval_time`."""
    cfg = parent.rules.config
    if parent.seq - seq < cfg.own_links:
        return True
    return eval_time - parent.link_time(seq) < cfg.own_time


def _def_spans(data: bytes) -> list[tuple[int, int]]:
    """(lineno, end_lineno) of every def/class in a Python source, 1-based; [] if it does not parse."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return []
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", None) or node.lineno
            spans.append((node.lineno, end))
    return spans


def _enclosing_def_line(spans: Sequence[tuple[int, int]], pos: int) -> int | None:
    """0-based index of the innermost def/class line enclosing an insertion before old line `pos`.

    An insertion is inside a def spanning lines L..E (1-based) when it lands after the def line
    and before its last line: L <= pos < E."""
    best: tuple[int, int] | None = None
    for start, end in spans:
        if start <= pos < end and (best is None or start > best[0]):
            best = (start, end)
    return None if best is None else best[0] - 1


def consent_plan(parent: ParentView, before: Tree, after: Tree, ops: Sequence[ChangeOp],
                 author: Handle, eval_time: Ms) -> ConsentPlan:
    """§13.1–13.3: who is asked and which parts apply, for a change `before -> after` (via `ops`) on Y."""
    rules = parent.rules
    members = rules.members
    voters = set(members.voters())
    humans = tuple(h for h in members.humans() if h in voters)
    touched = touched_lines(before, after, ops)
    owners: set[Handle] = set()

    def note(owner: Handle, seq: int) -> None:
        if owner != "-" and live(seq, parent, eval_time):
            owners.add(owner)

    for path in sorted(touched.removed):
        if path == MEMBERS:
            continue
        lines = list(parent.blame.lines(path))
        for i in touched.removed[path]:
            if 0 <= i < len(lines):
                note(*lines[i])
    for path in touched.whole_file:
        if path == MEMBERS:
            continue
        for run in parent.blame.get(path):
            note(run.owner, run.seq)
    for path in sorted(touched.inserted_at):
        if path == MEMBERS or not path.endswith(".py") or path not in before:
            continue
        spans = _def_spans(before.read(path))
        if not spans:
            continue
        lines = list(parent.blame.lines(path))
        for pos in touched.inserted_at[path]:
            idx = _enclosing_def_line(spans, pos)
            if idx is not None and 0 <= idx < len(lines):
                note(*lines[idx])
    line_owners = tuple(sorted((owners - {author}) & voters))

    lazy: set[Handle] = set()
    explicit: list[tuple[str, tuple[Handle, ...]]] = []
    declared: list[tuple[str, str, tuple[Handle, ...]]] = []
    quorum = False
    members_touched = False
    for path in touched_paths(ops):
        rule = rules.owners.rule_for(path)
        if rule is not None:
            named = tuple(h for h in rule.owners if members.get(h) is not None)
            declared.append((path, rule.rule, named))
            if rule.rule == "lazy":
                lazy.update(h for h in named if h != author and h in voters)
            elif rule.rule == "explicit":
                explicit.append((path, tuple(sorted({h for h in named if h != author and h in voters}))))
            else:
                quorum = True
        builtin = rules.owners.builtin_for(path)
        if builtin == "quorum":
            quorum = True
            declared.append((path, "quorum", ()))
        elif builtin == "members":
            members_touched = True
            declared.append((path, "members", ()))

    askers = set(line_owners) | lazy
    for _, names in explicit:
        askers.update(names)
    askers &= voters
    eligible = askers | set(humans)

    quorum_voters: tuple[Handle, ...] | None = None
    quorum_needed: int | None = None
    if quorum:
        after_members: Members | None = None
        if members_touched and MEMBERS in after:
            try:
                after_members = parse_members(after.read(MEMBERS))
            except ConfigError:
                after_members = None
        keep = []
        for handle in sorted(voters):
            if after_members is None or after_members.get(handle) == members.get(handle):
                keep.append(handle)
        quorum_voters = tuple(keep)
        quorum_needed = len(keep) // 2 + 1
    return ConsentPlan(
        line_owners=line_owners,
        lazy_declared=tuple(sorted(lazy)),
        explicit=tuple(explicit),
        askers=tuple(sorted(askers)),
        eligible_objectors=tuple(sorted(eligible)),
        quorum_voters=quorum_voters,
        quorum_needed=quorum_needed,
        members_rule=members_touched,
        declared=tuple(declared),
        humans=humans,
    )


# ---------------------------------------------------------------- votes (§13.4, §9)

@dataclass(frozen=True)
class Vote:
    id: Hex
    kind: Literal["approve", "object", "hold", "veto"]
    by: Handle
    received: Ms
    sent: Ms | None
    evidence: Evidence | None = None
    reason: str = ""
    target: Hex | None = None                # negotiation root the vote is about (escalation decisions)


def future_skewed(vote: Vote, cfg: RepoConfig) -> bool:
    """§9: a vote sent more than S after it was received is void."""
    return vote.sent is not None and vote.sent > vote.received + cfg.skew


def vote_counts(vote: Vote, close: Ms, cfg: RepoConfig) -> bool:
    """§13.4 vote rule toward window close `close`: sent by close, received by close + G."""
    if future_skewed(vote, cfg):
        return False
    if vote.received > close + cfg.grace:
        return False
    return vote.sent is None or vote.sent <= close


# ---------------------------------------------------------------- evaluation (§13.3–13.5)

@dataclass(frozen=True)
class Answer:
    id: Hex
    at: Ms
    answers: Hex | None                      # objection id, or None = answers every objection before `at`


@dataclass(frozen=True)
class EvidenceVerdict:
    objection: Hex
    on: Hex
    on_push: Hex
    base_result: Literal["pass", "fail"] | None
    result: Literal["pass", "fail"]
    received: Ms
    contains_file: bool


@dataclass(frozen=True)
class ConsentInputs:
    plan: ConsentPlan
    author: Handle
    window_open: Ms
    votes: Sequence[Vote]
    answers: Sequence[Answer]
    verdicts: Sequence[EvidenceVerdict]
    negotiation_objections: Sequence[ObjectionState]
    escalation: EscalationState | None
    cfg: RepoConfig
    push: Hex | None = None                  # the push being evaluated (selects verdicts on its candidates)
    negotiation: Hex | None = None           # its negotiation root (matches escalation chosen/declined)


@dataclass(frozen=True)
class ConsentResult:
    holds_at: Ms | None
    status: Literal["ok", "window", "objected", "escalated", "needs_approval", "needs_quorum", "vetoed", "declined"]
    reason: str
    close: Ms
    held_by: Handle | None
    objections: tuple[ObjectionState, ...]
    approvals: tuple[tuple[Handle, Ms], ...]
    quorum_have: int | None
    next_change: Ms | None


def is_confirmed(verdict: EvidenceVerdict) -> bool:
    return verdict.base_result == "pass" and verdict.result == "fail"


def first_verdict(verdicts: Sequence[EvidenceVerdict], objection: Hex, push: Hex | None,
                  until: Ms) -> EvidenceVerdict | None:
    """The first evidence verdict on a candidate of `push` for `objection` received by `until`."""
    best = None
    for v in verdicts:
        if v.objection == objection and v.on_push == push and v.received <= until:
            if best is None or (v.received, v.on) < (best.received, best.on):
                best = v
    return best


def _first_answer(answers: Sequence[Answer], objection: Hex, after: Ms, until: Ms) -> Answer | None:
    best = None
    for a in answers:
        if a.at > after and a.at <= until and (a.answers is None or a.answers == objection):
            if best is None or (a.at, a.id) < (best.at, best.id):
                best = a
    return best


def evaluate_consent(inp: ConsentInputs, at: Ms) -> ConsentResult:
    """§13.3–13.5 for one candidate at decision time `at`, using only inputs visible at `at`."""
    cfg = inp.cfg
    plan = inp.plan
    E = inp.window_open
    W, G = cfg.window, cfg.grace
    votes = sorted((v for v in inp.votes if v.received <= at and not future_skewed(v, cfg)),
                   key=lambda v: (v.received, v.id))
    first_approve: dict[Handle, Ms] = {}
    for v in votes:
        if v.kind == "approve" and v.by not in first_approve:
            first_approve[v.by] = v.received
    approvals = tuple(sorted(((h, t) for h, t in first_approve.items()), key=lambda x: (x[1], x[0])))
    esc = inp.escalation
    root = inp.negotiation
    chosen_at: Ms = INF
    if esc is not None and esc.status == "resolved" and esc.decided_at is not None and esc.decided_at <= at:
        if root is not None and esc.chosen == root:
            chosen_at = esc.decided_at

    def result(holds_at, status, reason, close=E + W, held_by=None, objections=(), quorum_have=None,
               next_change=None) -> ConsentResult:
        return ConsentResult(holds_at, status, reason, close, held_by, tuple(objections), approvals,
                             quorum_have, next_change)

    for v in votes:
        if v.kind == "veto":
            return result(None, "vetoed", f"vetoed by {v.by}")
    if esc is not None and esc.status == "resolved" and root is not None and root in esc.declined \
            and esc.decided_at is not None and esc.decided_at <= at:
        return result(None, "declined", f"declined by escalation ({esc.decided_by})")

    has_window = bool(plan.askers)
    eligible = set(plan.eligible_objectors)
    close = E + W
    held_by: Handle | None = None
    if has_window:
        for v in votes:
            if v.kind == "hold" and v.by in eligible and vote_counts(v, E + W, cfg):
                close, held_by = E + 2 * W, v.by
                break

    def withdrawn(by: Handle, after: Ms) -> Ms:
        for v in votes:
            if v.kind == "approve" and v.by == by and v.received > after:
                return v.received
        return INF

    states: list[ObjectionState] = []
    intervals: list[tuple[Ms, Ms]] = []
    next_times: list[Ms] = []
    evidence_deadline = close + G + cfg.escalate
    for v in votes:
        if v.kind != "object":
            continue
        counting = has_window and v.by in eligible and vote_counts(v, close, cfg)
        if not counting:
            states.append(ObjectionState(v.id, v.by, v.reason, v.received, v.evidence, "void", None, 0))
            continue
        stop = min(withdrawn(v.by, v.received), chosen_at)
        answer = _first_answer(inp.answers, v.id, v.received, at)
        ans_at = answer.at if answer else INF
        if v.evidence is None:
            end = min(stop, ans_at)
            status = "blocking" if end > at else "advisory"
        else:
            verdict = first_verdict(inp.verdicts, v.id, inp.push, min(at, evidence_deadline))
            if verdict is not None and is_confirmed(verdict):
                end, status = stop, "confirmed"
            elif verdict is not None:
                end = min(stop, max(verdict.received, ans_at))
                status = "refuted" if end > at else "advisory"
            else:
                end = min(stop, max(evidence_deadline, ans_at))
                if at < evidence_deadline:
                    status = "pending_verdict"
                    next_times.append(evidence_deadline)
                else:
                    status = "blocking" if end > at else "advisory"
            if end <= at and status in ("confirmed", "refuted", "pending_verdict", "blocking"):
                status = "advisory"
        intervals.append((v.received, end))
        states.append(ObjectionState(v.id, v.by, v.reason, v.received, v.evidence, status,
                                     answer.id if answer and answer.at <= at else None, 0))
    for o in inp.negotiation_objections:
        stop = min(withdrawn(o.by, o.received), chosen_at)
        clear = INF
        if o.evidence is not None:
            verdict = first_verdict(inp.verdicts, o.id, inp.push, at)
            if verdict is not None and verdict.result == "pass" and verdict.contains_file:
                clear = verdict.received
        end = min(stop, clear)
        start = min(o.received, E)
        intervals.append((start, end))
        status = "cleared" if clear <= at else ("advisory" if end <= at else "confirmed")
        states.append(replace(o, status=status))

    def blocked(tau: Ms) -> bool:
        return any(start <= tau < end for start, end in intervals)

    all_approved: Ms | None = None
    if has_window:
        if all(h in first_approve for h in plan.askers):
            all_approved = max(first_approve[h] for h in plan.askers)
    deadline = close + G

    def lazy_ready(tau: Ms) -> bool:
        if not has_window:
            return True
        return (all_approved is not None and all_approved <= tau) or tau >= deadline

    times = {E, deadline} | set(first_approve.values())
    for start, end in intervals:
        times.update((start, end))
    lazy_at: Ms | None = None
    for tau in sorted(t for t in times if E <= t <= at):
        if not blocked(tau) and lazy_ready(tau):
            lazy_at = tau
            break

    explicit_at: list[Ms | None] = []
    for _, owners in plan.explicit:
        if owners:
            if all(h in first_approve for h in owners):
                explicit_at.append(max(first_approve[h] for h in owners))
            else:
                explicit_at.append(None)
        else:
            others = [t for h, t in first_approve.items() if h != inp.author]
            explicit_at.append(min(others) if others else None)

    quorum_at: Ms | None = None
    quorum_have: int | None = None
    if plan.quorum_voters is not None:
        qtimes = sorted(first_approve[h] for h in plan.quorum_voters if h in first_approve and h != inp.author)
        if inp.author in plan.quorum_voters:
            qtimes = sorted(qtimes + [E])
        quorum_have = len(qtimes)
        need = plan.quorum_needed or 0
        if need == 0:
            quorum_at = E
        elif len(qtimes) >= need:
            quorum_at = max(qtimes[need - 1], E)

    members_at: Ms | None = None
    if plan.members_rule:
        humans = set(plan.humans)
        htimes = [t for h, t in first_approve.items() if h in humans]
        if inp.author in humans:
            htimes.append(E)
        members_at = min(htimes) if htimes else None

    if lazy_at is None and has_window and at < deadline:
        next_times.append(deadline)
    parts: list[Ms | None] = [lazy_at] + explicit_at
    if plan.quorum_voters is not None:
        parts.append(quorum_at)
    if plan.members_rule:
        parts.append(members_at)
    nxt = min((t for t in next_times if t > at), default=None)
    if all(p is not None for p in parts):
        holds = max(p for p in parts if p is not None)
        return result(holds, "ok", "consent holds", close, held_by, states, quorum_have, None)
    blocking_now = blocked(at)
    if blocking_now and esc is not None and esc.status == "open":
        status, reason = "escalated", f"escalated ({esc.reason}); deciders {', '.join(esc.deciders) or '-'}"
    elif blocking_now:
        who = sorted({s.by for s in states if s.status in ("blocking", "pending_verdict", "confirmed", "refuted")})
        status, reason = "objected", f"objected by {', '.join(who) or 'an earlier revision'}"
    elif lazy_at is None:
        waiting = [h for h in plan.askers if h not in first_approve]
        status, reason = "window", f"window closes at {close} (+G); waiting on {', '.join(waiting)}"
    elif any(t is None for t in explicit_at) or (plan.members_rule and members_at is None):
        status, reason = "needs_approval", "needs approval from declared owners or a human member"
    else:
        status, reason = "needs_quorum", f"needs quorum {quorum_have or 0}/{plan.quorum_needed}"
    return result(None, status, reason, close, held_by, states, quorum_have, nxt)


# ---------------------------------------------------------------- escalations (§13.6, §13.7)

@dataclass(frozen=True)
class OpenPush:
    push: Hex
    negotiation: Hex
    author: Handle
    received: Ms
    task: Hex | None
    objections: tuple[ObjectionState, ...]
    episodes: Mapping[Handle, int]


BLOCKING_STATUSES = frozenset({"blocking", "pending_verdict", "confirmed", "refuted"})


def detect_escalations(open_pushes: Sequence[OpenPush], at: Ms
                       ) -> tuple[tuple[Literal["mutual", "rounds"], tuple[Hex, ...]], ...]:
    """§13.7 triggers among open pushes at `at`: rounds (an objector at two episodes) and mutual objections."""
    found: set[tuple[str, tuple[Hex, ...]]] = set()
    for p in open_pushes:
        if any(n >= 2 for n in p.episodes.values()):
            found.add(("rounds", (p.negotiation,)))
    ordered = sorted(open_pushes, key=lambda p: (p.received, p.push))
    for i, p in enumerate(ordered):
        for q in ordered[i + 1:]:
            if p.negotiation == q.negotiation or p.author == q.author:
                continue
            p_on_q = any(o.by == p.author and o.status in BLOCKING_STATUSES for o in q.objections)
            q_on_p = any(o.by == q.author and o.status in BLOCKING_STATUSES for o in p.objections)
            if p_on_q and q_on_p:
                found.add(("mutual", tuple(sorted((p.negotiation, q.negotiation)))))
    return tuple(sorted(found))  # type: ignore[return-value]


def escalation_id(reason: str, roots: Sequence[Hex]) -> Hex:
    return sha256_hex(("gud-escalation/1\n" + "\n".join(sorted(roots)) + "\n" + reason + "\n").encode("utf-8"))


def decide_escalation(esc: EscalationState, decider_votes: Sequence[Vote],
                      evidence_uncleared: Mapping[Hex, bool], received: Mapping[Hex, Ms], at: Ms) -> EscalationState:
    """§13.7 decision: first counting decider vote in (opened_at, deadline], else the default at the deadline.

    The vote that opened the escalation (received at opened_at) is not its decision.

    `decider_votes[i].target` is the negotiation root the vote is about."""
    if esc.status == "resolved":
        return esc
    deciders = set(esc.deciders)
    pushes = list(esc.pushes)
    for v in sorted(decider_votes, key=lambda v: (v.received, v.id)):
        if v.received > at or v.received <= esc.opened_at or v.received > esc.deadline:
            continue
        if v.by not in deciders or v.target not in pushes:
            continue
        others = tuple(p for p in pushes if p != v.target)
        if v.kind == "approve":
            return replace(esc, status="resolved", chosen=v.target, declined=others,
                           decided_by=v.by, decided_at=v.received)
        if v.kind in ("object", "veto"):
            chosen = others[0] if esc.reason == "mutual" and others else None
            declined = (v.target,)
            return replace(esc, status="resolved", chosen=chosen, declined=declined,
                           decided_by=v.by, decided_at=v.received)
    if at < esc.deadline:
        return esc
    if esc.reason == "mutual":
        first = min(pushes, key=lambda p: (received.get(p, INF), p))
        return replace(esc, status="resolved", chosen=first, declined=tuple(p for p in pushes if p != first),
                       decided_by="default", decided_at=esc.deadline)
    root = pushes[0]
    if evidence_uncleared.get(root, False):
        return replace(esc, status="resolved", chosen=None, declined=(root,), decided_by="default",
                       decided_at=esc.deadline)
    return replace(esc, status="resolved", chosen=root, declined=(), decided_by="default",
                   decided_at=esc.deadline)
