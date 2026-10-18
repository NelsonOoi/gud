"""Inbox-driven agents: targeted CC, `Gud-After` ordering, cheap rebases, honest reviews."""

from gud.inbox import Inbox
from gud.lead import Lead, ScriptedPlanner
from gud.merge import merge3, merge_edits
from gud.replay import replay
from gud.tasks import derive
from gud.tester import Tester as RepoTester
from gud.testkit import REPO, World, parsed
from gud.worker import ScriptedCoder, Worker

from test_swarm import OkRunner, compose_kind, send_task, snap


def _addr(w, handle):
    return w.members[handle].address


def _run(w, lead, workers, tester, window, rounds=14):
    for _ in range(rounds):
        lead.step()
        for worker in workers:
            worker.step()
        tester.step()
        tasks = derive(snap(w, window))
        if tasks and tasks[0].status == "done" and tasks[0].notified_at is not None:
            return tasks[0]
    return None


def test_inbox_keeps_only_repo_mail_and_reads_each_email_once(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "VALUE = 1\n"})
    inbox = Inbox(w.mailbox, _addr(w, "@alice"), REPO)
    assert [compose_kind(e) for e in inbox.read()] == ["push"]
    assert inbox.read() == []
    w.mailbox.send(from_addr=_addr(w, "@bob"), to=_addr(w, "@alice"), subject="side chat", body="hi\n")
    assert inbox.read() == []


def test_mail_goes_only_to_the_agents_it_concerns(tmp_path):
    window = 60
    w = World.create(tmp_path)
    w.genesis({"app.py": "VALUE = 1\n"})
    planner = ScriptedPlanner({"widget": [
        ("Write alpha", "create alpha.py", ["alpha.py"]),
        ("Write beta", "create beta.py", ["beta.py"]),
    ]})

    def edit(subtask, tree, blame, feedback=None):
        return {"alpha.py": "A = 1\n"} if "alpha" in subtask.title.lower() else {"beta.py": "B = 1\n"}

    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock, pool=["@alice", "@bob"])
    workers = [Worker(w.mailbox, w.ids[h], window, ScriptedCoder(edit_fn=edit), clock=w.clock)
               for h in ("@alice", "@bob")]
    tester = RepoTester(w.mailbox, w.ids["@tester"], OkRunner(), window, clock=w.clock)
    send_task(w, "Build the widget")
    assert _run(w, lead, workers, tester, window) is not None

    carol = _addr(w, "@carol")
    for email in w.repo_mail()[1:]:
        kind = compose_kind(email)
        if kind in {"subtask", "push", "test", "approve", "task-done"}:
            assert carol not in email.cc, (kind, email.subject)
    subtasks = [e for e in w.repo_mail() if compose_kind(e) == "subtask"]
    for email in subtasks:
        assignee = parsed(email).get("Gud-Assignee")
        others = {_addr(w, h) for h in ("@alice", "@bob", "@tester")} - {_addr(w, assignee)}
        assert not others & set(email.cc), email.cc
    pushes = [e for e in w.repo_mail()[1:] if compose_kind(e) == "push"]
    for email in pushes:
        assert _addr(w, "@tester") in email.cc and _addr(w, "@lead") in email.cc


def test_gud_after_holds_a_subtask_until_its_prerequisite_lands(tmp_path):
    window = 60
    w = World.create(tmp_path)
    w.genesis({"app.py": "VALUE = 1\n"})
    planner = ScriptedPlanner({"site": [
        ("Write styles", "create styles.css", ["styles.css"]),
        ("Write page", "create page.html linking styles.css", ["page.html"], [1]),
    ]})
    seen_styles = []

    def edit(subtask, tree, blame, feedback=None):
        if "styles" in subtask.title.lower():
            return {"styles.css": "body {}\n"}
        seen_styles.append("styles.css" in tree)
        return {"page.html": "<link href=styles.css>\n"}

    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock, pool=["@alice", "@bob"])
    workers = [Worker(w.mailbox, w.ids[h], window, ScriptedCoder(edit_fn=edit), clock=w.clock)
               for h in ("@alice", "@bob")]
    tester = RepoTester(w.mailbox, w.ids["@tester"], OkRunner(), window, clock=w.clock)
    send_task(w, "Build the site")
    done = _run(w, lead, workers, tester, window)
    assert done is not None
    subs = {sub.title: sub for sub in done.subtasks}
    assert subs["Write page"].after == [subs["Write styles"].id]
    assert seen_styles and all(seen_styles)
    assert not any(p.status == "superseded" for p in snap(w, window).proposals.values())


def test_three_way_merge_rebases_without_an_llm():
    assert merge3("a\nb\nc\nd\ne\n", "a\nB\nc\nd\ne\n", "a\nb\nc\nd\nE\n") == "a\nB\nc\nd\nE\n"
    base = "a\nb\nc\n"
    # Both sides appended at the same spot: keep both instead of conflicting.
    assert merge3(base, base + "mine\n", base + "theirs\n") in {
        base + "mine\ntheirs\n", base + "theirs\nmine\n"}
    assert merge3(base, "a\nX\nc\n", "a\nY\nc\n") is None
    five = "a\nb\nc\nd\ne\n"
    tip = {"nav.html": "a\nb\nc\nd\nE\n", "other.txt": "x\n"}
    assert merge_edits({"nav.html": five}, {"nav.html": "a\nB\nc\nd\ne\n", "new.txt": "n\n"}, tip) == {
        "nav.html": "a\nB\nc\nd\nE\n", "new.txt": "n\n"}


def test_superseded_push_is_not_tested_and_stays_superseded(tmp_path):
    window = 60
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "VALUE = 1\n"})
    a = w.push("@alice", parent=g, tree={**g.tree, "a.py": "A = 1\n"})
    w.test(a, passed=True)
    w.tick(1)
    b = w.push("@bob", parent=g, tree={**g.tree, "b.py": "B = 1\n"})
    tester = RepoTester(w.mailbox, w.ids["@tester"], OkRunner(), window, clock=w.clock)
    sent = tester.step()
    assert b.id not in {parsed(e).get("Gud-Target") for e in sent}
    assert snap(w, window).proposals[b.id].status == "superseded"
    w.test(b, passed=False)
    assert snap(w, window).proposals[b.id].status == "superseded"


def test_sibling_alive_at_link_time_stays_superseded_after_a_late_failure(tmp_path):
    window = 60
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "VALUE = 1\n"})
    a = w.push("@alice", parent=g, tree={**g.tree, "a.py": "A = 1\n"})
    b = w.push("@bob", parent=g, tree={**g.tree, "b.py": "B = 1\n"})
    w.test(a, passed=True)
    w.tick(1)
    w.test(b, passed=False)
    chain = replay(w.repo_mail(), window=window, now=w.clock.t)
    assert chain.tip.message_id == a.id
    assert chain.proposals[b.id].status == "superseded"


class _Abstainer:
    def review(self, diff, context):
        return "abstain", "model error"


def test_lead_review_failure_is_no_vote(tmp_path):
    window = 60
    w = World.create(tmp_path)
    g = w.genesis({"app.py": "VALUE = 1\n"})
    w.edit("@alice", g, "app.py", "VALUE = 2\n")
    lead = Lead(w.mailbox, w.ids["@lead"], window, ScriptedPlanner({}), clock=w.clock,
                reviewer=_Abstainer())
    lead.step()
    assert not [e for e in w.repo_mail() if compose_kind(e) in {"approve", "object"}]
