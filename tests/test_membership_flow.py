"""Membership lands by quorum. Skips until gud.replay can be imported."""

import pytest

from gud import compose, sig
from gud.members import propose_add, propose_remove
from gud.testkit import Pushed, World, parsed
from gud.types import Member
from gud.wire import parse_body

replay = pytest.importorskip("gud.replay")


def _replay(emails, window, now):
    fn = getattr(replay, "replay", None)
    if not callable(fn):
        pytest.skip("gud.replay.replay is not implemented yet")
    return fn(emails, window, now)


def _run(w, window=60.0):
    w.tick(1)
    return _replay(w.repo_mail(), window, w.clock.t)


def _pushed(email) -> Pushed:
    body = parse_body(email.body)
    return Pushed(email, body.get("Gud-State") or "", body.get("Gud-Tree") or "", {})


def test_add_and_remove_member_by_quorum(tmp_path):
    # Default roster voters: @lead @alice @bob @carol @nelson (5). Quorum is 3, and the
    # author counts. @tester and @console do not vote. A membership push drops the
    # subject of the change from the voter set.
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "def f():\n    return 1\n"})
    w.test(genesis)
    carol_file = w.edit("@carol", genesis, "carol.py", "carol = 1\n", intent="carol's file")
    w.test(carol_file)

    chain = _run(w)
    assert chain.tip is not None, "genesis did not become a link"
    assert any(link.message_id == carol_file.id for link in chain.links), (
        f"carol's file did not land; links={[link.message_id for link in chain.links]} "
        f"proposals={[(p.subject, p.status, p.reason) for p in chain.proposals.values()]}"
    )

    raw, public = sig.generate()
    dave = Member("@dave", "agent", "dave@gud.local", public, "@nelson")
    dave_id = compose.Identity("@dave", "dave@gud.local", raw)
    add = propose_add(w.mailbox, w.ids["@lead"], chain.tip, dave, reason="need dave")
    assert "dave@gud.local" not in add.cc

    chain = _run(w)
    proposal = chain.proposals[add.message_id]
    assert proposal.status == "needs_quorum", (
        f"status={proposal.status} reason={proposal.reason} "
        f"have={proposal.quorum_have} need={proposal.quorum_needed}"
    )
    assert proposal.quorum_needed == 3
    assert add.message_id not in {link.message_id for link in chain.links}

    add_push = _pushed(add)
    w.approve("@alice", add_push)
    w.approve("@bob", add_push)
    chain = _run(w)
    assert add.message_id not in {link.message_id for link in chain.links}
    w.test(add_push)
    chain = _run(w)
    assert any(link.message_id == add.message_id for link in chain.links), (
        f"add did not land; proposals={[(p.subject, p.status, p.reason) for p in chain.proposals.values()]}"
    )
    assert chain.tip.message_id == add.message_id
    assert "@dave" in chain.tip.members and chain.tip.members["@dave"].votes

    dave_tree = dict(chain.tip.tree)
    dave_tree["dave.py"] = "dave = 1\n"
    body, _state, _tree_hash = compose.push_body(
        dave_id, parent=chain.tip.message_id, parent_state=chain.tip.state,
        parent_tree=chain.tip.tree, new_tree=dave_tree, intent="dave's file",
    )
    dave_email = compose.send(w.mailbox, dave_id, chain.tip.members, "[gud] dave's file", body,
                              in_reply_to=chain.tip.message_id)
    dave_push = _pushed(dave_email)
    w.test(dave_push)
    chain = _run(w)
    assert chain.tip is not None and chain.tip.message_id == dave_email.message_id, (
        f"dave did not land; tip={getattr(chain.tip, 'message_id', None)} "
        f"proposals={[(p.subject, p.status, p.reason) for p in chain.proposals.values()]}"
    )
    assert "dave.py" in chain.tip.tree

    removal = propose_remove(w.mailbox, w.ids["@lead"], chain.tip, "@carol", reason="off the roster")
    chain = _run(w)
    removal_proposal = chain.proposals[removal.message_id]
    assert removal_proposal.status == "needs_quorum", (
        f"status={removal_proposal.status} reason={removal_proposal.reason} "
        f"have={removal_proposal.quorum_have} need={removal_proposal.quorum_needed}"
    )
    # Six voters after @dave joined; @carol is excluded from this push, leaving 5, so quorum stays 3.
    assert removal_proposal.quorum_needed == 3
    removal_push = _pushed(removal)
    w.approve("@alice", removal_push)
    w.approve("@bob", removal_push)
    w.test(removal_push)
    chain = _run(w)
    assert chain.tip is not None and chain.tip.message_id == removal.message_id, (
        f"remove did not land; proposals={[(p.subject, p.status, p.reason) for p in chain.proposals.values()]}"
    )
    assert "@carol" not in chain.tip.members
    assert "@dave" in chain.tip.members
    assert not any(member.handle == "@carol" and member.votes for member in chain.tip.members.values())
    assert "carol.py" in chain.tip.tree
    carol_blame = chain.tip.blame.get("carol.py", [])
    assert carol_blame, f"blame missing for carol.py; keys={list(chain.tip.blame)}"
    assert any(row[0] == "@carol" for row in carol_blame), carol_blame

    stale = dict(chain.tip.tree)
    stale["carol.py"] = "carol = 2\n"
    body, _state, _tree_hash = compose.push_body(
        w.ids["@carol"], parent=chain.tip.message_id, parent_state=chain.tip.state,
        parent_tree=chain.tip.tree, new_tree=stale, intent="carol after removal",
    )
    rejected = compose.send(w.mailbox, w.ids["@carol"], chain.tip.members, "[gud] carol after removal",
                            body, in_reply_to=chain.tip.message_id)
    chain = _run(w)
    bad = chain.proposals[rejected.message_id]
    assert bad.status == "bad_sig", f"status={bad.status} reason={bad.reason}"
    assert chain.tip.message_id == removal.message_id
    assert "@carol" not in chain.tip.members
    assert any(row[0] == "@carol" for row in chain.tip.blame.get("carol.py", []))
    assert parsed(rejected).get("Gud-Author") == "@carol"
