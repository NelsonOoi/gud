"""Rebuild the link chain by replaying a mailbox. Pure function of (emails, window, now)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .blame import genesis_blame, update_blame
from .memberfile import by_address, members_of
from .patch import PatchError, apply_diff
from .rules import affected_owners, protection, quorum_voters
from .sig import verify
from .types import Email, LinkState, Member, ProposalState, Tree
from .wire import ZERO, parse_body, parse_list, state_hash, tree_hash

_PUNCT = ".,!?;:\"'`()[]{}*_"
_VERBS = frozenset({"approve", "veto", "object"})


@dataclass
class Chain:
    links: list[LinkState]
    proposals: dict[str, ProposalState]
    by_id: dict[str, LinkState]
    emails: list[Email]
    by_message: dict[str, Email] = field(default_factory=dict)

    @property
    def tip(self) -> LinkState | None:
        return self.links[-1] if self.links else None


@dataclass
class _Replies:
    approvals: dict[str, float]
    objections: list[dict]
    vetoes: list[tuple[float, str]]
    test_result: str | None
    test_time: float | None
    test_failed: bool


@dataclass
class _Eval:
    outcome: str
    proposal: ProposalState
    author: str = ""
    intent: str = ""
    diff: str = ""
    task: str | None = None
    new_tree: Tree | None = None
    state: str | None = None
    tree_hash: str | None = None
    qualify_at: float | None = None


def replay(emails: list[Email], window: float, now: float) -> Chain:
    """Replay `emails` as of `now`. Later mail is invisible. Order of `emails` does not matter."""
    visible = _visible(emails, now)
    parsed = {email.message_id: parse_body(email.body) for email in visible}
    by_message = {email.message_id: email for email in visible}
    pushes = [email for email in visible if parsed[email.message_id].kind == "push"]

    by_parent: dict[str, list[Email]] = defaultdict(list)
    agent_by_target: dict[str, list[Email]] = defaultdict(list)
    human_by_reply: dict[str, list[Email]] = defaultdict(list)
    for email in visible:
        body = parsed[email.message_id]
        if body.kind == "push":
            parent_id = body.get("Gud-Parent")
            if parent_id:
                by_parent[parent_id].append(email)
        elif body.kind in {"approve", "object", "test"}:
            target = body.get("Gud-Target")
            if target:
                agent_by_target[target].append(email)
        elif body.kind is None and email.in_reply_to:
            human_by_reply[email.in_reply_to].append(email)
    for kids in by_parent.values():
        kids.sort(key=lambda email: (email.received_at, email.message_id))

    links: list[LinkState] = []
    for email in by_parent.get("none", []):
        genesis = _try_genesis(email, parsed[email.message_id])
        if genesis is not None:
            links.append(genesis)
            break

    proposals: dict[str, ProposalState] = {}
    while links:
        parent = links[-1]
        kids = by_parent.get(parent.message_id, [])
        evals = [
            _evaluate(kid, parent, parsed, by_message, agent_by_target, human_by_reply, window, now)
            for kid in kids
        ]
        winner = blocker = None
        for idx, ev in enumerate(evals):
            if ev.outcome == "never":
                continue
            if ev.outcome == "qualified":
                winner = idx
            else:
                blocker = idx
            break
        if winner is not None:
            links.append(_link_from(kids[winner], evals[winner], parent))
            win_id = kids[winner].message_id
            for idx, ev in enumerate(evals):
                if idx == winner:
                    continue
                prop = ev.proposal
                if ev.outcome != "never":
                    prop.status = "superseded"
                    prop.reason = f"superseded by {win_id}"
                proposals[prop.message_id] = prop
            continue
        if blocker is not None:
            block_id = kids[blocker].message_id
            for idx, ev in enumerate(evals):
                prop = ev.proposal
                if idx > blocker and ev.outcome != "never":
                    if ev.outcome == "qualified":
                        prop.status = "qualified"
                    prop.reason = f"waiting on earlier proposal {block_id}"
                proposals[prop.message_id] = prop
        else:
            for ev in evals:
                proposals[ev.proposal.message_id] = ev.proposal
        break

    linked = {link.message_id for link in links}
    for email in pushes:
        if email.message_id in linked or email.message_id in proposals:
            continue
        body = parsed[email.message_id]
        proposals[email.message_id] = ProposalState(
            message_id=email.message_id,
            parent=body.get("Gud-Parent") or "",
            author=body.get("Gud-Author") or "",
            subject=email.subject,
            received_at=email.received_at,
            status="stale",
            reason="parent is not on the chain",
            task=body.get("Gud-Task") or None,
        )
    return Chain(links, proposals, {link.message_id: link for link in links}, visible, by_message)


def _visible(emails: list[Email], now: float) -> list[Email]:
    ordered = sorted(emails, key=lambda email: (email.received_at, email.message_id))
    seen: set[str] = set()
    out: list[Email] = []
    for email in ordered:
        if email.received_at > now or email.message_id in seen:
            continue
        seen.add(email.message_id)
        out.append(email)
    return out


def _human_verb(text: str) -> tuple[str, str] | None:
    parts = text.strip().split(None, 1)
    if not parts:
        return None
    word = parts[0].strip(_PUNCT).lower()
    if word not in _VERBS:
        return None
    rest = parts[1].strip() if len(parts) > 1 else ""
    return word, rest


def _auth_failure(email: Email, trailers: dict[str, str], diff: str, members: dict[str, Member]) -> str | None:
    if not email.sender_verified:
        return "sender is not verified"
    author = trailers.get("Gud-Author")
    member = members.get(author) if author else None
    if member is None or member.role == "human":
        return "author is not an agent member"
    if email.from_addr.lower() != member.address:
        return "From does not match the member address"
    if not verify(member.public_key, trailers, diff):
        return "signature does not verify"
    return None


def _valid_agent(email: Email, trailers: dict[str, str], diff: str, members: dict[str, Member]) -> Member | None:
    if _auth_failure(email, trailers, diff, members):
        return None
    author = trailers.get("Gud-Author")
    return members.get(author) if author else None


def _try_genesis(email: Email, body) -> LinkState | None:
    if body.get("Gud-Parent") != "none" or body.get("Gud-Base") != ZERO:
        return None
    author = body.get("Gud-Author") or ""
    diff = body.diff
    try:
        tree = apply_diff({}, diff)
    except PatchError:
        return None
    consent_ids = parse_list(body.get("Gud-Consent"))
    state = state_hash(ZERO, author, consent_ids, diff)
    digest = tree_hash(tree)
    if body.get("Gud-State") != state or body.get("Gud-Tree") != digest:
        return None
    members = members_of(tree)
    if _auth_failure(email, body.trailers, diff, members):
        return None
    return LinkState(
        message_id=email.message_id, seq=0, author=author, state=state, tree_hash=digest,
        tree=tree, blame=genesis_blame(tree, author, email.message_id), members=members,
        received_at=email.received_at, subject=email.subject, task=body.get("Gud-Task") or None,
        linked_at=email.received_at, intent=body.text, diff=diff,
    )


def _collect(push_id: str, push_tree: str, members: dict[str, Member], consent_ids: list[str],
             agent_emails: list[Email], human_emails: list[Email], by_message: dict[str, Email],
             parsed: dict) -> _Replies:
    approvals: dict[str, float] = {}
    objections: list[dict] = []
    vetoes: list[tuple[float, str]] = []
    tests: list[tuple[float, str, object]] = []

    def add_approval(handle: str, at: float) -> None:
        if handle not in approvals or at < approvals[handle]:
            approvals[handle] = at

    for email in agent_emails:
        body = parsed[email.message_id]
        member = _valid_agent(email, body.trailers, body.diff, members)
        if member is None:
            continue
        if body.kind == "approve" and member.votes:
            add_approval(member.handle, email.received_at)
        elif body.kind == "object" and member.votes:
            objections.append({
                "by": member.handle, "reason": body.get("Gud-Reason") or body.text,
                "id": email.message_id, "at": email.received_at,
            })
        elif body.kind == "test" and member.role == "tester":
            tests.append((email.received_at, email.message_id, body))

    for email in human_emails:
        body = parsed[email.message_id]
        if not email.sender_verified:
            continue
        member = by_address(members, email.from_addr)
        if member is None or member.role != "human":
            continue
        verb = _human_verb(body.text)
        if verb is None:
            continue
        word, rest = verb
        if word == "approve" and member.votes:
            add_approval(member.handle, email.received_at)
        elif word == "object" and member.votes:
            objections.append({
                "by": member.handle, "reason": rest, "id": email.message_id, "at": email.received_at,
            })
        elif word == "veto":
            vetoes.append((email.received_at, member.handle))

    for cid in consent_ids:
        cited = by_message.get(cid)
        if cited is None:
            continue
        body = parsed[cited.message_id]
        if body.kind != "approve" or body.get("Gud-Target") != push_id:
            continue
        member = _valid_agent(cited, body.trailers, body.diff, members)
        if member is not None and member.votes:
            add_approval(member.handle, cited.received_at)

    objections.sort(key=lambda item: (item["at"], item["id"]))
    tests.sort(key=lambda item: (item[0], item[1]))
    vetoes.sort()
    test_result = None
    test_time = None
    test_failed = False
    if tests:
        at, _mid, body = tests[0]
        result = body.get("Gud-Result")
        if result == "pass" and body.get("Gud-Tree") == push_tree:
            test_result = "pass"
            test_time = at
        else:
            test_result = "fail"
            test_failed = True
    return _Replies(approvals, objections, vetoes, test_result, test_time, test_failed)


def _consent(rule: str | None, owners: list[str], approvals: dict[str, float], objections: list[dict],
             author: str, received: float, window: float, now: float, humans: set[str],
             quorum_times: list[float] | None, quorum_needed: int | None,
             ) -> tuple[float | None, str | None, bool, str]:
    """Return (consent_time, status, terminal, reason). `objected` is terminal so a revision can land."""
    if rule == "quorum":
        times = quorum_times or []
        needed = quorum_needed if quorum_needed is not None else (len(times) // 2 + 1)
        if times and len(times) >= needed:
            return sorted(times)[needed - 1], None, False, ""
        return None, "needs_quorum", False, f"needs {needed} quorum approvals, has {len(times)}"

    if rule == "explicit":
        if owners:
            missing = [handle for handle in owners if handle not in approvals]
            if not missing:
                return max(approvals[handle] for handle in owners), None, False, ""
            return None, "needs_approval", False, "needs approval from " + ", ".join(missing)
        others = [at for handle, at in approvals.items() if handle != author]
        if others:
            return min(others), None, False, ""
        return None, "needs_approval", False, "needs approval from another voter"

    if not owners:
        return received, None, False, ""

    close = received + window
    owner_set = set(owners)
    blocking_owners = [obj for obj in objections if obj["by"] in owner_set and obj["at"] < close]
    blocking_humans = [obj for obj in objections if obj["by"] in humans and obj["at"] < close]
    candidates: list[float] = []
    if owners and all(handle in approvals for handle in owners):
        candidates.append(max(approvals[handle] for handle in owners))
    if not blocking_owners and not blocking_humans and close <= now:
        candidates.append(close)
    if candidates:
        return min(candidates), None, False, ""
    if blocking_owners or blocking_humans:
        who = (blocking_owners or blocking_humans)[0]
        text = f"objected by {who['by']}"
        if who["reason"]:
            text += f": {who['reason']}"
        # Terminal: a pending objection would block the revised push the chain is supposed to accept,
        # and there is no withdraw message to clear it.
        return None, "objected", True, text
    return None, "window", False, "waiting for the consensus window"


def _veto_reason(vetoes: list[tuple[float, str]], qualify_at: float | None) -> str | None:
    if not vetoes:
        return None
    if qualify_at is None:
        return f"vetoed by {vetoes[0][1]}"
    blocking = [item for item in vetoes if item[0] <= qualify_at]
    if not blocking:
        return None
    return f"vetoed by {blocking[0][1]}"


def _evaluate(email: Email, parent: LinkState, parsed: dict, by_message: dict[str, Email],
              agent_by_target: dict[str, list[Email]], human_by_reply: dict[str, list[Email]],
              window: float, now: float) -> _Eval:
    body = parsed[email.message_id]
    author = body.get("Gud-Author") or ""
    diff = body.diff
    task = body.get("Gud-Task") or None
    consent_ids = parse_list(body.get("Gud-Consent"))
    auth_reason = _auth_failure(email, body.trailers, diff, parent.members)
    base_ok = body.get("Gud-Base") == parent.state

    new_tree = None
    state = None
    digest = None
    apply_reason = None
    if auth_reason is None and base_ok:
        try:
            new_tree = apply_diff(parent.tree, diff)
        except PatchError as exc:
            apply_reason = f"diff does not apply: {exc}"
        else:
            state = state_hash(parent.state, author, consent_ids, diff)
            digest = tree_hash(new_tree)
            if body.get("Gud-State") != state or body.get("Gud-Tree") != digest:
                apply_reason = "state or tree hash does not match the diff"

    try:
        owners = affected_owners(parent, diff, author)
        rule = protection(parent, diff)
    except PatchError:
        owners = []
        rule = None

    replies = _collect(
        email.message_id, body.get("Gud-Tree") or "", parent.members, consent_ids,
        agent_by_target.get(email.message_id, []), human_by_reply.get(email.message_id, []),
        by_message, parsed,
    )

    quorum_needed = quorum_have = None
    quorum_times: list[float] | None = None
    if rule == "quorum" and new_tree is not None:
        voters = quorum_voters(parent, new_tree)
        quorum_needed = len(voters) // 2 + 1
        quorum_times = []
        for handle in voters:
            if handle == author:
                quorum_times.append(email.received_at)
            elif handle in replies.approvals:
                quorum_times.append(replies.approvals[handle])
        quorum_have = len(quorum_times)

    window_closes = email.received_at + window if rule is None and owners else None
    qualify_at = None
    if auth_reason:
        outcome, status, reason = "never", "bad_sig", auth_reason
    elif not base_ok:
        outcome, status, reason = "never", "stale", "Gud-Base does not match the parent state"
    elif apply_reason:
        outcome, status, reason = "never", "conflict", apply_reason
    elif replies.test_failed:
        outcome, status, reason = "never", "tests_failed", "tests failed"
    else:
        humans = {handle for handle, member in parent.members.items() if member.role == "human"}
        consent_rule = rule if rule in (None, "explicit", "quorum") else "explicit"
        consent_time, consent_status, terminal, consent_reason = _consent(
            consent_rule, owners, replies.approvals, replies.objections, author,
            email.received_at, window, now, humans, quorum_times, quorum_needed,
        )
        q = max(consent_time, replies.test_time) if consent_time is not None and replies.test_time is not None else None
        veto_reason = _veto_reason(replies.vetoes, q)
        if veto_reason:
            outcome, status, reason = "never", "vetoed", veto_reason
        elif consent_time is None:
            outcome = "never" if terminal else "pending"
            status = consent_status or "window"
            reason = consent_reason
        elif replies.test_time is None:
            outcome, status, reason = "pending", "testing", "waiting for a passing test"
        else:
            outcome, status, reason, qualify_at = "qualified", "qualified", "qualified", q

    proposal = ProposalState(
        message_id=email.message_id, parent=body.get("Gud-Parent") or "", author=author,
        subject=email.subject, received_at=email.received_at, status=status, reason=reason,
        affected_owners=list(owners), approvals=sorted(replies.approvals),
        objections=list(replies.objections), window_closes_at=window_closes,
        protected_rule=rule,
        quorum_needed=quorum_needed, quorum_have=quorum_have,
        test_result=replies.test_result, task=task,
    )
    return _Eval(outcome, proposal, author, body.text, diff, task, new_tree, state, digest, qualify_at)


def _link_from(email: Email, ev: _Eval, parent: LinkState) -> LinkState:
    assert ev.new_tree is not None and ev.state is not None and ev.tree_hash is not None
    assert ev.qualify_at is not None
    return LinkState(
        message_id=email.message_id, seq=parent.seq + 1, author=ev.author, state=ev.state,
        tree_hash=ev.tree_hash, tree=ev.new_tree,
        blame=update_blame(parent.tree, parent.blame, ev.new_tree, ev.author, email.message_id),
        members=members_of(ev.new_tree), received_at=email.received_at, subject=email.subject,
        task=ev.task, linked_at=ev.qualify_at, intent=ev.intent, diff=ev.diff,
    )
