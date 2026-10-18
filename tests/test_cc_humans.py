"""A human CC adds a human, and any key may sign as a lead."""

import json
from dataclasses import replace

from gud import compose, sig
from gud.compose import Identity
from gud.lead import Lead, ScriptedPlanner
from gud.memberfile import MEMBERS, format_members, members_of
from gud.members import with_member
from gud.patch import apply_diff
from gud.replay import replay
from gud.testkit import REPO, Pushed, World
from gud.types import Member
from gud.wire import parse_body


def run(w, window=60):
    return replay(w.repo_mail(), window=window, now=w.clock.t)


def _lead(w):
    return Lead(w.mailbox, w.ids["@lead"], 60, ScriptedPlanner({}), clock=w.clock, pool=["@alice"])


def _mail(w, cc, *, sender=None, body="Please add them.\n", subject="Add a person", **kw):
    return w.mailbox.send(
        from_addr=sender or w.members["@nelson"].address, to=REPO, cc=list(cc),
        subject=subject, body=body, **kw,
    )


def _push(w, parent, tree, source_id, author="@lead", identity=None):
    ident = identity or w.ids[author]
    body, state, tree_hash = compose.push_body(
        ident, parent=parent.id, parent_state=parent.state, parent_tree=parent.tree,
        new_tree=tree, intent="add humans", extra_trailers={"Gud-Source": source_id},
    )
    email = compose.send(
        w.mailbox, ident, w.members, "[gud] members: add", body, in_reply_to=parent.id,
    )
    return Pushed(email, state, tree_hash, tree)


def _human(handle, address, sponsor="@nelson"):
    return Member(handle, "human", address, None, sponsor)


def _cc_pushes(emails):
    found = []
    for email in emails:
        body = parse_body(email.body)
        if body.kind == "push" and body.get("Gud-Source") and ".gud/members" in body.diff:
            found.append(email)
    return found


def _added(parent_tree, email):
    body = parse_body(email.body)
    before = members_of(parent_tree)
    after = members_of(apply_diff(parent_tree, body.diff))
    return [after[handle] for handle in after if handle not in before]


def _unverify(path, message_id):
    lines = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("message_id") == message_id:
            row["sender_verified"] = False
        lines.append(json.dumps(row))
    path.write_text("\n".join(lines) + "\n")


def test_cc_source_counts_as_approval_without_a_tester(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "a = 1\n"})
    source = _mail(w, ["Ada@Example.com", "alice@gud.local"])
    tree = with_member(genesis.tree, _human("@ada", "ada@example.com"))
    pushed = _push(w, genesis, tree, source.message_id)

    chain = run(w)
    assert chain.tip.message_id == pushed.id
    assert chain.tip.linked_at == pushed.email.received_at
    assert not any(parse_body(email.body).kind == "approve" for email in w.repo_mail())
    ada = chain.tip.members["@ada"]
    assert ada.role == "human"
    assert ada.address == "ada@example.com"
    assert ada.public_key is None
    assert ada.sponsor == "@nelson"


def test_members_add_with_code_still_waits_for_a_tester(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "a = 1\n"})
    source = _mail(w, ["ada@example.com"])
    tree = with_member(genesis.tree, _human("@ada", "ada@example.com"))
    tree["app.py"] = "a = 2\n"
    pushed = _push(w, genesis, tree, source.message_id)

    waiting = run(w)
    assert waiting.tip.message_id == genesis.id
    assert waiting.proposals[pushed.id].status == "testing"
    assert waiting.proposals[pushed.id].reason == "waiting for a passing test"

    w.test(pushed)
    assert run(w).tip.message_id == pushed.id


def test_cc_address_missing_from_the_source_is_not_approval(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "a = 1\n"})
    source = _mail(w, ["ada@example.com"])
    tree = with_member(genesis.tree, _human("@ada", "ada@example.com"))
    tree = with_member(tree, _human("@bea", "bea@example.com"))
    pushed = _push(w, genesis, tree, source.message_id)
    w.test(pushed)
    chain = run(w)
    assert chain.tip.message_id == genesis.id
    proposal = chain.proposals[pushed.id]
    assert proposal.status == "needs_approval"
    assert proposal.reason == "needs approval from a human member"

    w.human("@nelson", pushed, "approve")
    chain = run(w)
    assert chain.tip.message_id == pushed.id
    assert "@ada" in chain.tip.members and "@bea" in chain.tip.members


def test_cc_source_does_not_cover_a_removal_or_a_non_human(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "a = 1\n"})
    source = _mail(w, ["ada@example.com"])
    current = dict(members_of(genesis.tree))
    del current["@carol"]
    current["@ada"] = _human("@ada", "ada@example.com")
    removed = dict(genesis.tree)
    removed[MEMBERS] = format_members(current)
    removal = _push(w, genesis, removed, source.message_id)
    w.test(removal)
    chain = run(w)
    assert chain.proposals[removal.id].status == "needs_approval"
    assert chain.tip.message_id == genesis.id

    _raw, public = sig.generate()
    agent_source = _mail(w, ["bea@example.com"], subject="Add Bea")
    agent_tree = with_member(
        genesis.tree, Member("@bea", "agent", "bea@example.com", public, "@nelson"),
    )
    agent = _push(w, genesis, agent_tree, agent_source.message_id)
    w.test(agent)
    chain = run(w)
    assert chain.proposals[agent.id].status == "needs_approval"
    assert "@bea" not in chain.tip.members


def test_unverified_agent_or_gud_kind_source_is_not_approval(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({})
    source = _mail(w, ["ada@example.com"])
    tree = with_member(genesis.tree, _human("@ada", "ada@example.com"))
    pushed = _push(w, genesis, tree, source.message_id)
    w.test(pushed)
    emails = [
        replace(email, sender_verified=False) if email.message_id == source.message_id else email
        for email in w.repo_mail()
    ]
    chain = replay(emails, 60, w.clock.t)
    assert chain.proposals[pushed.id].status == "needs_approval"
    assert chain.tip.message_id == genesis.id

    agent = World.create(tmp_path / "agent")
    agent_genesis = agent.genesis({})
    agent_mail = _mail(agent, ["bea@example.com"], sender=agent.members["@alice"].address, subject="From Alice")
    bea_tree = with_member(agent_genesis.tree, _human("@bea", "bea@example.com"))
    agent_push = _push(agent, agent_genesis, bea_tree, agent_mail.message_id)
    agent.test(agent_push)
    assert run(agent).proposals[agent_push.id].status == "needs_approval"
    assert run(agent).tip.message_id == agent_genesis.id

    kind = World.create(tmp_path / "kind")
    kind_genesis = kind.genesis({})
    kind_mail = _mail(kind, ["cy@example.com"], subject="Kind", body="Hello\n\nGud-Kind: notify\n")
    cy_tree = with_member(kind_genesis.tree, _human("@cy", "cy@example.com"))
    kind_push = _push(kind, kind_genesis, cy_tree, kind_mail.message_id)
    kind.test(kind_push)
    chain = run(kind)
    assert chain.proposals[kind_push.id].status == "needs_approval"
    assert chain.tip.message_id == kind_genesis.id


def test_non_lead_signature_must_match_the_roster_key(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "a = 1\n"})
    raw, public = sig.generate()
    ident = Identity("@alice", w.ids["@alice"].address, raw)
    tree = dict(genesis.tree)
    tree["n.py"] = "n = 1\n"
    body, state, tree_hash = compose.push_body(
        ident, parent=genesis.id, parent_state=genesis.state, parent_tree=genesis.tree,
        new_tree=tree, intent="alice other key",
    )
    email = compose.send(w.mailbox, ident, w.members, "[gud] alice other key", body, in_reply_to=genesis.id)
    parsed = parse_body(email.body)
    assert parsed.get("Gud-Sig", "").startswith("ed25519:")
    assert sig.verify(public, parsed.trailers, parsed.diff)
    assert not sig.verify(w.members["@alice"].public_key, parsed.trailers, parsed.diff)
    chain = run(w)
    assert chain.proposals[email.message_id].status == "bad_sig"
    assert chain.proposals[email.message_id].reason == "signature does not verify"
    assert chain.tip.message_id == genesis.id
    assert state and tree_hash


def test_lead_role_accepts_a_different_key(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "a = 1\n"})
    raw, public = sig.generate()
    ident = Identity("@lead", w.ids["@lead"].address, raw)
    tree = dict(genesis.tree)
    tree["n.py"] = "n = 1\n"
    body, state, tree_hash = compose.push_body(
        ident, parent=genesis.id, parent_state=genesis.state, parent_tree=genesis.tree,
        new_tree=tree, intent="lead other key",
    )
    email = compose.send(w.mailbox, ident, w.members, "[gud] lead other key", body, in_reply_to=genesis.id)
    parsed = parse_body(email.body)
    assert parsed.get("Gud-Sig", "").startswith("ed25519:")
    assert sig.verify(public, parsed.trailers, parsed.diff)
    assert not sig.verify(w.members["@lead"].public_key, parsed.trailers, parsed.diff)
    pushed = Pushed(email, state, tree_hash, tree)

    waiting = run(w)
    assert waiting.proposals[pushed.id].status == "testing"
    assert waiting.tip.message_id == genesis.id
    w.test(pushed)
    chain = run(w)
    assert chain.tip.message_id == pushed.id
    assert chain.tip.author == "@lead"
    assert chain.tip.members["@lead"].public_key == w.members["@lead"].public_key


def test_lead_signature_must_still_start_with_ed25519(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({})
    tree = dict(genesis.tree)
    tree["n.py"] = "n = 1\n"
    body, _state, _tree_hash = compose.push_body(
        w.ids["@lead"], parent=genesis.id, parent_state=genesis.state, parent_tree=genesis.tree,
        new_tree=tree, intent="bad prefix",
    )
    signature = parse_body(body).get("Gud-Sig")
    tampered = body.replace(f"Gud-Sig: {signature}", "Gud-Sig: v1:nope", 1)
    email = compose.send(w.mailbox, w.ids["@lead"], w.members, "[gud] bad prefix", tampered,
                         in_reply_to=genesis.id)
    chain = run(w)
    assert chain.proposals[email.message_id].status == "bad_sig"
    assert chain.proposals[email.message_id].reason == "signature does not verify"
    assert chain.tip.message_id == genesis.id


def test_lead_proposes_one_human_per_new_cc_and_does_not_approve(tmp_path):
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "a = 1\n"})
    source = _mail(w, [
        "alice@gud.local", "Ada@Example.com", "Bea@Example.com", w.members["@nelson"].address,
    ])
    lead = _lead(w)
    sent = lead.step()
    assert "approve" not in [parse_body(email.body).kind for email in sent]
    assert not any(parse_body(email.body).kind == "approve" for email in w.repo_mail())

    pushes = _cc_pushes(w.repo_mail())
    assert len(pushes) == 2
    added = [_added(genesis.tree, email) for email in pushes]
    assert all(len(members) == 1 for members in added)
    by_address = {members[0].address: members[0] for members in added}
    assert set(by_address) == {"ada@example.com", "bea@example.com"}
    for member in by_address.values():
        assert member.role == "human"
        assert member.public_key is None
        assert member.sponsor == "@nelson"
    for email in pushes:
        body = parse_body(email.body)
        assert body.get("Gud-Source") == source.message_id
        assert body.get("Gud-Author") == "@lead"
        assert body.get("Gud-Parent") == genesis.id
        assert sig.verify(w.members["@lead"].public_key, body.trailers, body.diff)
        assert "ada@example.com" not in email.cc and "bea@example.com" not in email.cc
        assert email.in_reply_to == genesis.id

    # Roster-only adds need no tester. The first links; the second has the same parent and loses.
    chain = run(w)
    assert chain.tip.message_id == pushes[0].message_id
    assert "@ada" in chain.tip.members
    assert chain.tip.members["@ada"].public_key is None
    assert chain.tip.members["@ada"].sponsor == "@nelson"
    assert "@bea" not in chain.tip.members
    assert chain.proposals[pushes[1].message_id].status == "superseded"

    # The lead re-proposes the add it lost on the new tip, once.
    retry = _cc_pushes(lead.step())
    assert len(retry) == 1
    assert parse_body(retry[0].body).get("Gud-Parent") == pushes[0].message_id
    assert _cc_pushes(_lead(w).step()) == []
    chain = run(w)
    assert chain.tip.message_id == retry[0].message_id
    assert {"@ada", "@bea"} <= set(chain.tip.members)


def test_lead_ignores_cc_that_is_not_a_verified_human_to_the_repo(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    outsider = _mail(w, ["zoe@example.com"], sender="sam@acme.example", subject="Outside")
    agent = _mail(w, ["zoe@example.com"], sender=w.members["@alice"].address, subject="From Alice")
    unverified = _mail(w, ["zoe@example.com"], subject="Unverified")
    _unverify(w.mailbox.path, unverified.message_id)
    w.mailbox.send(
        from_addr=w.members["@nelson"].address, to=w.members["@alice"].address,
        cc=[REPO, "zoe@example.com"], subject="Not to the repo", body="Hello.\n",
    )
    assert _cc_pushes(_lead(w).step()) == []
    assert _cc_pushes(w.repo_mail()) == []
    assert outsider.message_id and agent.message_id
