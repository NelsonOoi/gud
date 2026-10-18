import random
from dataclasses import replace

from gud import compose, sig
from gud.chain import Repo
from gud.memberfile import MEMBERS, format_members, members_of
from gud.replay import replay
from gud.testkit import REPO, World
from gud.types import Member
from gud.wire import parse_body


def run(w, window=60):
    return replay(w.repo_mail(), window=window, now=w.clock.t)


def test_genesis_only(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    chain = run(w)
    assert chain.tip is not None
    assert [link.message_id for link in chain.links] == [g.id]
    assert chain.tip.seq == 0
    assert chain.tip.author == "@lead"
    assert chain.tip.linked_at == g.email.received_at
    assert chain.tip.tree["app.py"] == "a = 1\n"
    assert chain.tip.members["@alice"].role == "agent"
    assert chain.proposals == {}
    assert all(owner == "@lead" and link_id == g.id for owner, link_id in chain.tip.blame["app.py"])


def test_push_with_no_affected_owners_lands_once_tested(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    pushed = w.edit("@alice", g, "new.py", "n = 1\n", intent="add new", task="T1")
    waiting = run(w)
    assert waiting.tip.message_id == g.id
    assert waiting.proposals[pushed.id].status == "testing"
    assert waiting.proposals[pushed.id].affected_owners == []
    assert waiting.proposals[pushed.id].task == "T1"

    w.test(pushed)
    chain = run(w)
    assert chain.tip.message_id == pushed.id
    assert chain.tip.seq == 1
    assert chain.tip.intent == "add new"
    assert chain.tip.task == "T1"
    assert chain.tip.linked_at == max(pushed.email.received_at, chain.emails[-1].received_at)
    assert pushed.id not in chain.proposals
    assert chain.tip.blame["new.py"] == [("@alice", pushed.id)]


def test_rewrite_waits_for_the_window(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    pushed = w.edit("@alice", g, "app.py", "a = 2\n", intent="bump")
    w.test(pushed)
    chain = run(w)
    assert chain.tip.message_id == g.id
    prop = chain.proposals[pushed.id]
    assert prop.status == "window"
    assert prop.affected_owners == ["@lead"]
    assert prop.window_closes_at == pushed.email.received_at + 60
    assert prop.test_result == "pass"

    w.tick(59)
    assert run(w).tip.message_id == g.id
    w.tick(2)
    chain = run(w)
    assert chain.tip.message_id == pushed.id
    assert chain.tip.linked_at == pushed.email.received_at + 60
    assert chain.tip.blame["app.py"] == [("@alice", pushed.id)]


def test_objection_blocks_then_revision_lands(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    blocked = w.edit("@alice", g, "app.py", "a = 2\n", intent="bad")
    w.test(blocked)
    w.object("@lead", blocked, "nope")
    chain = run(w)
    assert chain.tip.message_id == g.id
    assert chain.proposals[blocked.id].status == "objected"
    assert chain.proposals[blocked.id].objections[0]["by"] == "@lead"

    revised = w.edit("@alice", g, "app.py", "a = 3\n", intent="good")
    w.approve("@lead", revised)
    w.test(revised)
    chain = run(w)
    assert chain.tip.message_id == revised.id
    assert chain.proposals[blocked.id].status == "objected"

    w.tick(61)
    chain = run(w)
    assert chain.tip.message_id == revised.id
    assert chain.proposals[blocked.id].status == "objected"


def test_late_approval_of_objected_push_does_not_rewrite_history(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    blocked = w.edit("@alice", g, "app.py", "a = 2\n", intent="bad")
    w.test(blocked)
    w.object("@lead", blocked, "nope")
    revised = w.edit("@alice", g, "app.py", "a = 3\n", intent="good")
    w.approve("@lead", revised)
    w.test(revised)
    assert run(w).tip.message_id == revised.id

    w.tick(5)
    w.approve("@lead", blocked)
    w.tick(120)
    chain = run(w)
    assert [l.message_id for l in chain.links] == [g.id, revised.id]
    assert chain.proposals[blocked.id].status == "superseded"


def test_slow_quorum_push_stops_holding_the_chain_after_one_window(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    members = dict(w.members)
    members["@dave"] = Member("@dave", "agent", "dave@gud.local", sig.generate()[1], "@nelson")
    tree = {**g.tree, MEMBERS: format_members(members)}
    vote = w.push("@lead", parent=g, tree=tree, intent="add dave")
    w.test(vote)
    work = w.edit("@alice", g, "new.py", "n = 1\n", intent="unrelated work")
    w.test(work)
    chain = run(w)
    assert chain.tip.message_id == g.id
    assert chain.proposals[work.id].reason.startswith("waiting on earlier proposal")

    w.tick(61)
    chain = run(w)
    assert chain.tip.message_id == work.id
    assert chain.proposals[vote.id].status == "superseded"


def test_owner_approval_overrides_objection(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    pushed = w.edit("@alice", g, "app.py", "a = 2\n")
    w.object("@lead", pushed, "hmm")
    w.approve("@lead", pushed)
    w.test(pushed)
    assert run(w).tip.message_id == pushed.id


def test_non_owner_objection_does_not_block_window(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    pushed = w.edit("@alice", g, "app.py", "a = 2\n")
    w.object("@bob", pushed, "style")
    w.test(pushed)
    assert run(w).proposals[pushed.id].status == "window"
    w.tick(61)
    assert run(w).tip.message_id == pushed.id


def test_explicit_protected_file_needs_owner_approval(tmp_path):
    w = World.create(tmp_path, protected="api/* explicit\n")
    g = w.genesis({"api/x.py": "X = 1\n"})
    pushed = w.edit("@alice", g, "api/x.py", "X = 2\n", intent="bump x")
    w.test(pushed)
    chain = run(w)
    prop = chain.proposals[pushed.id]
    assert prop.status == "needs_approval"
    assert prop.protected_rule == "explicit"
    assert prop.affected_owners == ["@lead"]
    assert chain.tip.message_id == g.id

    w.approve("@lead", pushed)
    chain = run(w)
    assert chain.tip.message_id == pushed.id
    assert chain.tip.seq == 1


def test_explicit_new_file_needs_another_voter(tmp_path):
    w = World.create(tmp_path, protected="api/* explicit\n")
    g = w.genesis({})
    pushed = w.edit("@alice", g, "api/n.py", "n = 1\n")
    w.approve("@alice", pushed)
    w.test(pushed)
    assert run(w).proposals[pushed.id].status == "needs_approval"
    w.human("@nelson", pushed, "approve ship it")
    assert run(w).tip.message_id == pushed.id


def test_human_approval_membership_and_removed_agent_is_bad_sig(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    _raw, pub = sig.generate()
    members = dict(members_of(g.tree))
    members["@dave"] = Member("@dave", "agent", "dave@gud.local", pub, "@nelson")
    tree = dict(g.tree)
    tree[MEMBERS] = format_members(members)
    added = w.push("@alice", parent=g, tree=tree, intent="add dave")
    w.test(added)
    prop = run(w).proposals[added.id]
    assert prop.status == "needs_approval"
    assert prop.protected_rule == "human"
    assert prop.reason == "needs approval from a human member"

    w.approve("@lead", added)
    w.approve("@bob", added)
    assert run(w).proposals[added.id].status == "needs_approval"
    w.human("@nelson", added, "approve")
    chain = run(w)
    assert chain.tip.message_id == added.id
    assert "@dave" in chain.tip.members

    current = dict(members_of(added.tree))
    del current["@bob"]
    removed_tree = dict(added.tree)
    removed_tree[MEMBERS] = format_members(current)
    removed = w.push("@alice", parent=added, tree=removed_tree, intent="remove bob")
    w.test(removed)
    assert run(w).proposals[removed.id].status == "needs_approval"
    assert run(w).proposals[removed.id].protected_rule == "human"
    w.human("@nelson", removed, "approve")
    chain = run(w)
    assert chain.tip.message_id == removed.id
    assert "@bob" not in chain.tip.members
    assert "@dave" in chain.tip.members

    later = w.edit("@bob", removed, "app.py", "a = 9\n", intent="bob after removal")
    chain = run(w)
    assert chain.proposals[later.id].status == "bad_sig"
    assert chain.tip.message_id == removed.id


def test_human_veto_blocks_and_late_veto_does_not(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({})
    blocked = w.edit("@alice", g, "n.py", "n = 1\n")
    w.human("@nelson", blocked, "veto absolutely not")
    w.test(blocked)
    chain = run(w)
    assert chain.tip.message_id == g.id
    assert chain.proposals[blocked.id].status == "vetoed"

    ok = w.edit("@alice", g, "m.py", "m = 1\n")
    w.test(ok)
    assert run(w).tip.message_id == ok.id
    w.human("@nelson", ok, "veto too late")
    assert run(w).tip.message_id == ok.id


def test_failing_test_blocks_that_push_only(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({})
    bad = w.edit("@alice", g, "bad.py", "b = 1\n")
    w.test(bad, passed=False)
    assert run(w).proposals[bad.id].status == "tests_failed"
    good = w.edit("@bob", g, "good.py", "g = 1\n")
    w.test(good)
    chain = run(w)
    assert chain.tip.message_id == good.id
    assert chain.proposals[bad.id].status == "tests_failed"


def test_earliest_received_child_wins_the_race(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({})
    first = w.edit("@alice", g, "a.py", "a = 1\n", intent="first")
    w.tick(5)
    second = w.edit("@bob", g, "b.py", "b = 1\n", intent="second")
    w.test(second)
    w.test(first)
    chain = run(w)
    assert chain.tip.message_id == first.id
    assert chain.proposals[second.id].status == "superseded"
    assert first.id in chain.proposals[second.id].reason


def test_pending_earlier_child_blocks_a_later_qualified_one(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    early = w.edit("@alice", g, "app.py", "a = 2\n", intent="early")
    w.test(early)
    later = w.edit("@bob", g, "new.py", "n = 1\n", intent="later")
    w.test(later)
    chain = run(w)
    assert chain.tip.message_id == g.id
    assert chain.proposals[early.id].status == "window"
    assert chain.proposals[later.id].status == "qualified"
    assert chain.proposals[later.id].reason == f"waiting on earlier proposal {early.id}"

    w.tick(61)
    chain = run(w)
    assert chain.tip.message_id == early.id
    assert chain.proposals[later.id].status == "superseded"


def test_stale_base_and_missing_parent(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    new_tree = dict(g.tree)
    new_tree["z.py"] = "z = 1\n"
    body, _state, _tree = compose.push_body(
        w.ids["@alice"], parent=g.id, parent_state="ab" * 32, parent_tree=g.tree,
        new_tree=new_tree, intent="off base",
    )
    stale = compose.send(w.mailbox, w.ids["@alice"], w.members, "[gud] off base", body, in_reply_to=g.id)
    chain = run(w)
    assert chain.proposals[stale.message_id].status == "stale"
    assert chain.proposals[stale.message_id].reason == "Gud-Base does not match the parent state"
    assert chain.tip.message_id == g.id

    body, _state, _tree = compose.push_body(
        w.ids["@bob"], parent="<missing@gud.local>", parent_state=g.state, parent_tree=g.tree,
        new_tree=new_tree, intent="no parent",
    )
    orphan = compose.send(w.mailbox, w.ids["@bob"], w.members, "[gud] no parent", body)
    chain = run(w)
    assert chain.proposals[orphan.message_id].status == "stale"
    assert chain.proposals[orphan.message_id].reason == "parent is not on the chain"


def test_tampered_diff_is_bad_sig(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    pushed = w.edit("@alice", g, "app.py", "a = 2\n")
    body = pushed.email.body.replace("+a = 2", "+a = 9", 1)
    assert body != pushed.email.body
    w.mailbox.send(from_addr=w.ids["@alice"].address, to=REPO, subject="[gud] tamper",
                   body=body, in_reply_to=g.id)
    chain = run(w)
    bad = [prop for prop in chain.proposals.values() if prop.status == "bad_sig"]
    assert len(bad) == 1
    assert bad[0].message_id != pushed.id
    assert chain.tip.message_id == g.id


def test_conflict_when_tree_hash_does_not_match_apply_result(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    body, _state, _tree = compose.push_body(
        w.ids["@alice"], parent=g.id, parent_state=g.state, parent_tree={},
        new_tree={"only.py": "n = 1\n"}, intent="hash lie",
    )
    email = compose.send(w.mailbox, w.ids["@alice"], w.members, "[gud] hash lie", body, in_reply_to=g.id)
    chain = run(w)
    assert chain.proposals[email.message_id].status == "conflict"
    assert chain.tip.message_id == g.id


def test_insertion_inside_function_makes_that_agent_an_owner(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "def f():\n    return 1\n"})
    pushed = w.edit("@alice", g, "app.py", "def f():\n    x = 2\n    return 1\n", intent="insert")
    w.test(pushed)
    chain = run(w)
    assert chain.tip.message_id == g.id
    prop = chain.proposals[pushed.id]
    assert prop.affected_owners == ["@lead"]
    assert prop.status == "window"
    w.tick(61)
    chain = run(w)
    assert chain.tip.message_id == pushed.id
    assert chain.tip.blame["app.py"] == [
        ("@lead", g.id),
        ("@alice", pushed.id),
        ("@lead", g.id),
    ]


def test_replay_is_order_independent(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "a = 1\n"})
    landed = w.edit("@alice", g, "new.py", "n = 1\n", intent="add")
    w.test(landed)
    pending = w.edit("@bob", landed, "app.py", "a = 2\n", intent="touch")
    w.object("@carol", pending, "not mine")
    w.test(pending)
    w.approve("@lead", pending)
    emails = w.repo_mail()
    now = w.clock.t + 61

    def digest(chain):
        links = [
            (link.message_id, link.seq, link.author, link.state, link.tree_hash, link.tree,
             link.blame, link.linked_at, link.intent, link.task,
             tuple(sorted((handle, member.role) for handle, member in link.members.items())))
            for link in chain.links
        ]
        proposals = {
            pid: (prop.status, prop.reason, tuple(prop.affected_owners), tuple(prop.approvals),
                  tuple((obj["by"], obj["reason"], obj["id"], obj["at"]) for obj in prop.objections),
                  prop.window_closes_at, prop.protected_rule, prop.quorum_needed, prop.quorum_have,
                  prop.test_result, prop.task)
            for pid, prop in chain.proposals.items()
        }
        return links, proposals, [email.message_id for email in chain.emails]

    baseline = digest(replay(emails, 60, now))
    rng = random.Random(7)
    for _ in range(5):
        shuffled = list(emails)
        rng.shuffle(shuffled)
        assert digest(replay(shuffled, 60, now)) == baseline
    assert digest(replay(list(reversed(emails)), 60, now)) == baseline


def test_repo_tip_tree_blame_thread_and_link(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "def f():\n    return 1\n"})
    pushed = w.edit("@alice", g, "app.py", "def f():\n    return 2\n", intent="ret2", task="T3")
    tested = w.test(pushed)
    w.tick(61)
    repo = Repo(w.mailbox, 60, clock=lambda: w.clock.t)
    assert repo.tip().message_id == pushed.id
    assert repo.link(g.id).seq == 0
    assert repo.tree()["app.py"] == "def f():\n    return 2\n"
    assert repo.tree(at=g.id)["app.py"] == "def f():\n    return 1\n"
    blame = repo.blame("app.py")
    assert blame[0] == ("@lead", g.id, "def f():")
    assert blame[1] == ("@alice", pushed.id, "    return 2")
    assert repo.blame("app.py", at=g.id)[1][0] == "@lead"
    approved = w.approve("@bob", pushed)
    thread = repo.thread(pushed.id)
    assert [email.message_id for email in thread] == [pushed.id, tested.message_id, approved.message_id]
    assert parse_body(thread[1].body).kind == "test"
    fresh = replay(w.repo_mail(), 60, w.clock.t)
    assert [link.message_id for link in repo.refresh().links] == [link.message_id for link in fresh.links]


def test_sender_unverified_is_bad_sig(tmp_path):
    w = World.create(tmp_path)
    g = w.genesis({})
    pushed = w.edit("@alice", g, "n.py", "n = 1\n")
    w.test(pushed)
    forged = replace(pushed.email, sender_verified=False, message_id="<forged@gud.local>",
                     received_at=w.clock())
    chain = replay([*w.repo_mail(), forged], 60, w.clock.t)
    assert chain.proposals[forged.message_id].status == "bad_sig"
    assert chain.tip.message_id == pushed.id
