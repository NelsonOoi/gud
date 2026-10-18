"""Lead, workers, and the tester, driven by step() and a fake clock. No threads, no network."""

from types import SimpleNamespace

from gud import compose
from gud.build import LocalRunner
from gud.cli import read_dir
from gud.config import ROOT
from gud.lead import LLMPlanner, Lead, ScriptedPlanner
from gud.replay import replay
from gud.tasks import derive
from gud.testkit import World
from gud.types import RunResult
from gud.worker import LLMCoder, NoChange, ScriptedCoder, Worker
from gud.wire import tree_hash


class OkRunner:
    def run(self, tree):
        return RunResult(True, "ok\n", tree_hash(tree), 0.0, "ok")

    def close(self):
        pass


def snap(w, window):
    return replay(w.repo_mail(), window=window, now=w.clock.t)


def send_task(w, title, description="please"):
    seen = replay(w.repo_mail(), window=60, now=w.clock.t)
    from gud.tasks import next_task_id
    task_id = next_task_id(seen.emails)
    body = compose.task_body(w.ids["@console"], task_id, "@nelson", description)
    compose.send(w.mailbox, w.ids["@console"], w.members, f"[gud] {task_id}: {title}", body)
    return task_id


def test_scripted_planner_splits_demo_tasks_and_llm_classes_construct():
    planner = ScriptedPlanner()
    tree = {"auth.py": "x = 1\n"}

    def task(title):
        return SimpleNamespace(id="T1", title=title, description="")

    rate = planner.split(task("Add per-user rate limiting to login and bill overage requests"), tree)
    lock = planner.split(task("Lockout and rate limit collide in login"), tree)
    protected = planner.split(task("Record the rate-limit decision on the protected interface"), tree)
    assert len(rate) == 4
    assert len(lock) == 1 and "gud-conflict" in lock[0][1]
    assert len(protected) == 1 and protected[0][2] == ["api/interfaces.py"]
    assert [item[0] for item in rate] == [
        "Rate-limit login", "Bill overage in charge", "Map 429 and meter charges", "Add penalize",
    ]

    coder = ScriptedCoder()
    action, reason = coder.review("+# gud-bad-edit\n+    return 1\n", "login")
    assert action == "object" and "rate-limit" in reason
    action, reason = coder.review("+    if _failures.get(username, 0) >= 5:\n", "")
    assert action == "approve"

    llm_planner = LLMPlanner("sk-test", "test-model")
    llm_coder = LLMCoder("sk-test", "test-model")
    assert llm_planner.model == "test-model"
    assert llm_coder.api_key == "sk-test"


def test_llm_coder_retries_a_truncated_reply_then_backs_off(monkeypatch):
    import gud.worker as worker_mod

    calls = []
    replies = iter(['{"files": {"a.txt": "unterminated', '{"files": {"a.txt": "fixed\\n"}}'])

    def chat(api_key, model, messages, temperature=0):
        calls.append(temperature)
        return next(replies)

    monkeypatch.setattr(worker_mod, "_chat", chat)
    now = [0.0]
    coder = LLMCoder("sk-test", "test-model", clock=lambda: now[0])
    sub = SimpleNamespace(id="T1.1", title="write a", description="", files=["a.txt"])
    assert coder.edit(sub, {"a.txt": "old\n"}, {}) == {"a.txt": "fixed\n"}
    assert calls == [0, 0.3]

    calls.clear()
    monkeypatch.setattr(worker_mod, "_chat", lambda *a, **k: calls.append(1) or "not json")
    assert coder.edit(sub, {"a.txt": "old\n"}, {}) is None
    assert len(calls) == 2
    assert coder.edit(sub, {"a.txt": "old\n"}, {}) is None
    assert len(calls) == 2
    now[0] = 31.0
    coder.edit(sub, {"a.txt": "old\n"}, {})
    assert len(calls) == 4


def test_scripted_swarm_lands_a_two_subtask_task(tmp_path):
    window = 60
    w = World.create(tmp_path)
    w.genesis({"app.py": "VALUE = 1\n"})
    planner = ScriptedPlanner({
        "widget": [
            ("Write alpha", "create alpha.py", ["alpha.py"]),
            ("Write beta", "create beta.py", ["beta.py"]),
        ],
    })

    def edit(subtask, tree, blame, feedback=None):
        if "alpha" in subtask.title.lower():
            return {"alpha.py": "ALPHA = 1\n"}
        if "beta" in subtask.title.lower():
            return {"beta.py": "BETA = 1\n"}
        return {}

    from gud.tester import Tester

    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock, pool=["@alice", "@bob"])
    workers = [
        Worker(w.mailbox, w.ids[handle], window, ScriptedCoder(edit_fn=edit), clock=w.clock)
        for handle in ("@alice", "@bob")
    ]
    tester = Tester(w.mailbox, w.ids["@tester"], OkRunner(), window, clock=w.clock)
    send_task(w, "Build the widget")

    lead.step()
    lead.step()
    subtasks = [email for email in w.repo_mail() if compose_kind(email) == "subtask"]
    assert len(subtasks) == 2

    done = None
    for _ in range(12):
        lead.step()
        for worker in workers:
            worker.step()
        tester.step()
        tasks = derive(snap(w, window))
        if tasks and tasks[0].status == "done" and tasks[0].notified_at is not None:
            done = tasks[0]
            break
    assert done is not None, _dump(w, window)
    tip = snap(w, window).tip
    assert tip.tree["alpha.py"] == "ALPHA = 1\n"
    assert tip.tree["beta.py"] == "BETA = 1\n"
    assert {sub.status for sub in done.subtasks} == {"landed"}
    assert any(prop.status == "superseded" for prop in snap(w, window).proposals.values())
    notes = [email for email in w.repo_mail() if compose_kind(email) == "notify"]
    assert notes and any("nelson@" in addr for email in notes for addr in email.to)


def test_llm_coder_can_say_the_subtask_needs_no_change(monkeypatch):
    import gud.worker as worker_mod

    seen = {}

    def chat(api_key, model, messages, temperature=0):
        seen["system"] = messages[0]["content"]
        return '{"files": {}, "done": "doctype is already there"}'

    monkeypatch.setattr(worker_mod, "_chat", chat)
    coder = LLMCoder("sk-test", "test-model")
    sub = SimpleNamespace(id="T1.1", title="Add the doctype", description="index.html", files=["index.html"])
    result = coder.edit(sub, {"index.html": "<!doctype html>\n"}, {})
    assert isinstance(result, NoChange)
    assert result.reason == "doctype is already there"
    assert '"done"' in seen["system"]


def test_llm_coder_reviews_with_the_review_model(monkeypatch):
    import gud.worker as worker_mod

    models = []

    def chat(api_key, model, messages, temperature=0):
        models.append(model)
        if "review" in messages[0]["content"] or "vote" in messages[0]["content"]:
            return '{"action": "object", "reason": "keep KEEP"}'
        return '{"files": {"a.txt": "new\\n"}}'

    monkeypatch.setattr(worker_mod, "_chat", chat)
    coder = LLMCoder("sk-test", "edit-model", review_model="review-model")
    assert coder.review("-KEEP = 1\n", "ctx") == ("object", "keep KEEP")
    sub = SimpleNamespace(id="T1.1", title="write a.txt", description="a.txt", files=["a.txt"])
    coder.edit(sub, {"a.txt": "old\n"}, {})
    assert models == ["review-model", "edit-model"]
    assert LLMCoder("sk-test", "edit-model").review_model == "edit-model"


def test_a_coder_that_changes_nothing_is_asked_once_then_signs_no_change(tmp_path):
    window = 60
    w = World.create(tmp_path)
    w.genesis({"index.html": "<!doctype html>\n"})
    planner = ScriptedPlanner({"doctype": [("Add the doctype", "index.html needs one", ["index.html"])]})
    calls = []

    def edit(subtask, tree, blame, feedback=None):
        calls.append(subtask.id)
        return {"index.html": "<!doctype html>\n"}

    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock, pool=["@alice"])
    alice = Worker(w.mailbox, w.ids["@alice"], window, ScriptedCoder(edit_fn=edit), clock=w.clock)
    send_task(w, "Fix the doctype")
    lead.step()
    lead.step()
    assert alice.step() == []
    sent = alice.step()
    assert [compose_kind(email) for email in sent] == ["no-change"]
    assert len(calls) == 1
    for _ in range(3):
        assert alice.step() == []
    assert len(calls) == 1


def test_no_change_subtask_reaches_task_done(tmp_path):
    window = 60
    w = World.create(tmp_path)
    w.genesis({"index.html": "<!doctype html>\n"})
    planner = ScriptedPlanner({"doctype": [("Add the doctype", "index.html needs one", ["index.html"])]})

    def edit(subtask, tree, blame, feedback=None):
        return {}

    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock, pool=["@alice"])
    alice = Worker(w.mailbox, w.ids["@alice"], window, ScriptedCoder(edit_fn=edit), clock=w.clock)
    send_task(w, "Fix the doctype")
    lead.step()
    assert alice.step() == []
    assert [compose_kind(email) for email in alice.step()] == ["no-change"]

    sent = lead.step()
    assert "task-done" in [compose_kind(email) for email in sent]
    tasks = derive(snap(w, window))
    assert tasks[0].status == "done"
    assert tasks[0].notified_at is not None
    sub = tasks[0].subtasks[0]
    assert sub.status == "no-change"
    assert sub.reason == "no change needed"
    done = next(email for email in w.repo_mail() if compose_kind(email) == "task-done")
    note = next(email for email in w.repo_mail() if compose_kind(email) == "notify")
    assert f"{sub.id} {sub.assignee} no change: no change needed" in done.body
    assert f"{sub.id} {sub.assignee} no change: no change needed" in note.body
    assert any(addr.startswith("nelson@") for addr in note.to)

    from gud.console import feed_events

    event = next(item for item in feed_events(snap(w, window), [], []) if item["kind"] == "no-change")
    assert event["actor"] == "@alice"
    assert event["task"] == sub.id
    assert event["text"] == "no change needed"


def test_mixed_landed_and_no_change_subtasks_finish(tmp_path):
    window = 60
    w = World.create(tmp_path)
    w.genesis({"index.html": "<!doctype html>\n"})
    planner = ScriptedPlanner({
        "widget": [
            ("Write alpha", "create alpha.py", ["alpha.py"]),
            ("Keep the doctype", "index.html already has one", ["index.html"]),
        ],
    })

    def edit(subtask, tree, blame, feedback=None):
        if "alpha" in subtask.title.lower():
            return {"alpha.py": "ALPHA = 1\n"}
        if "doctype" in subtask.title.lower():
            return NoChange("doctype is already there")
        return {}

    from gud.tester import Tester

    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock, pool=["@alice", "@bob"])
    workers = [
        Worker(w.mailbox, w.ids[handle], window, ScriptedCoder(edit_fn=edit), clock=w.clock)
        for handle in ("@alice", "@bob")
    ]
    tester = Tester(w.mailbox, w.ids["@tester"], OkRunner(), window, clock=w.clock)
    send_task(w, "Build the widget")
    lead.step()

    done = None
    for _ in range(12):
        lead.step()
        for worker in workers:
            worker.step()
        tester.step()
        tasks = derive(snap(w, window))
        if tasks and tasks[0].status == "done" and tasks[0].notified_at is not None:
            done = tasks[0]
            break
    assert done is not None, _dump(w, window)
    by_title = {sub.title: sub for sub in done.subtasks}
    assert by_title["Write alpha"].status == "landed"
    assert by_title["Keep the doctype"].status == "no-change"
    assert by_title["Keep the doctype"].reason == "doctype is already there"
    tip = snap(w, window).tip
    assert tip.tree["alpha.py"] == "ALPHA = 1\n"
    assert tip.tree["index.html"] == "<!doctype html>\n"
    body = next(email.body for email in w.repo_mail() if compose_kind(email) == "task-done")
    note = next(email.body for email in w.repo_mail() if compose_kind(email) == "notify")
    line = f"{by_title['Keep the doctype'].id} {by_title['Keep the doctype'].assignee} no change: doctype is already there"
    assert line in body and line in note


def test_stale_no_change_does_not_finish_until_the_new_tip_is_checked(tmp_path):
    window = 60
    w = World.create(tmp_path)
    parent = w.genesis({"index.html": "<!doctype html>\n", "other.txt": "a\n"})
    planner = ScriptedPlanner({"doctype": [("Add the doctype", "index.html needs one", ["index.html"])]})
    calls = []

    def edit(subtask, tree, blame, feedback=None):
        calls.append(tree["index.html"])
        return {}

    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock, pool=["@alice"], stale_after=10_000)
    alice = Worker(w.mailbox, w.ids["@alice"], window, ScriptedCoder(edit_fn=edit), clock=w.clock)
    send_task(w, "Fix the doctype")
    lead.step()
    assert alice.step() == []
    declared = alice.step()
    assert [compose_kind(email) for email in declared] == ["no-change"]
    cited = snap(w, window).tip.message_id

    # A new file does not touch index.html, so the no-change still holds. Do not step the
    # lead yet: it would close the task while the statement is still true.
    other = w.edit("@bob", parent, "notes.txt", "b\n", intent="add notes")
    w.test(other)
    assert derive(snap(w, window))[0].subtasks[0].status == "no-change"

    changed = w.edit("@bob", other, "index.html", "<html>\n", intent="drop the doctype")
    w.approve("@lead", changed)
    w.test(changed)
    assert snap(w, window).tip.message_id == changed.id
    assert derive(snap(w, window))[0].subtasks[0].status == "open"
    assert "task-done" not in [compose_kind(email) for email in lead.step()]

    # The old declaration cited the previous tip. The worker checks the new one and signs again.
    assert alice.step() == []
    again = alice.step()
    assert [compose_kind(email) for email in again] == ["no-change"]
    from gud.wire import parse_body
    assert parse_body(again[0].body).get("Gud-Tip") != cited
    assert parse_body(again[0].body).get("Gud-Tip") == snap(w, window).tip.message_id
    sent = lead.step()
    assert "task-done" in [compose_kind(email) for email in sent]
    assert derive(snap(w, window))[0].status == "done"
    assert len(calls) == 2


def test_objection_triggers_a_revision_that_lands(tmp_path):
    window = 60
    w = World.create(tmp_path)
    w.genesis({"app.py": "VALUE = 1\n"})
    planner = ScriptedPlanner({
        "widget": [
            ("Plant the flag", "write shared.py", ["shared.py"]),
            ("Conflict the flag", "rewrite shared.py", ["shared.py"]),
        ],
    })

    def edit(subtask, tree, blame, feedback=None):
        if "plant" in subtask.title.lower():
            return {"shared.py": "KEEP = 1\n"}
        if "conflict" in subtask.title.lower():
            if feedback:
                return {"shared.py": "KEEP = 1  # kept\nEXTRA = 1\n"}
            return {"shared.py": "KEEP = 0\n"}
        return {}

    def review(diff, context=""):
        plus = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
        minus = [line[1:] for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]
        if any("KEEP = 0" in line for line in plus):
            return "object", "keep KEEP = 1; do not replace it"
        if any(line.strip() == "KEEP = 1" for line in minus) and not any("KEEP = 1" in line for line in plus):
            return "object", "keep KEEP = 1; do not replace it"
        return "approve", "KEEP stays"

    from gud.tester import Tester

    coder = ScriptedCoder(edit_fn=edit, review_fn=review)
    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock,
                pool=["@alice", "@bob"], stale_after=10_000)
    alice = Worker(w.mailbox, w.ids["@alice"], window, coder, clock=w.clock)
    bob = Worker(w.mailbox, w.ids["@bob"], window, coder, clock=w.clock)
    tester = Tester(w.mailbox, w.ids["@tester"], OkRunner(), window, clock=w.clock)
    send_task(w, "Build the widget")
    started = w.clock.t

    lead.step()
    alice.step()
    tester.step()
    assert snap(w, window).tip.tree["shared.py"] == "KEEP = 1\n"

    bob.step()
    pending = [prop for prop in snap(w, window).proposals.values() if prop.author == "@bob"]
    assert pending and "@alice" in pending[-1].affected_owners
    bad_id = pending[-1].message_id

    alice.step()
    assert snap(w, window).proposals[bad_id].status == "objected"
    assert any(compose_kind(email) == "object" for email in w.repo_mail())

    bob.step()
    lead.step()
    alice.step()
    tester.step()
    chain = snap(w, window)
    assert "EXTRA = 1" in chain.tip.tree["shared.py"]
    assert "KEEP = 1" in chain.tip.tree["shared.py"]
    assert chain.tip.author == "@bob"
    assert chain.tip.message_id != bad_id
    assert chain.proposals[bad_id].status == "objected"
    assert w.clock.t - started < window


def _clash(tmp_path, edit, review, window=60):
    """@alice lands shared.py; @bob is assigned to rewrite it. Nothing of @bob's is pushed yet."""
    from gud.tester import Tester

    w = World.create(tmp_path)
    w.genesis({"app.py": "VALUE = 1\n"})
    planner = ScriptedPlanner({
        "widget": [
            ("Plant the flag", "write shared.py with KEEP = 1; KEEP must stay 1", ["shared.py"]),
            ("Conflict the flag", "rewrite shared.py and add EXTRA = 1", ["shared.py"]),
        ],
    })
    coder = ScriptedCoder(edit_fn=edit, review_fn=review)
    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock,
                pool=["@alice", "@bob"], stale_after=10_000)
    alice = Worker(w.mailbox, w.ids["@alice"], window, coder, clock=w.clock)
    bob = Worker(w.mailbox, w.ids["@bob"], window, coder, clock=w.clock)
    tester = Tester(w.mailbox, w.ids["@tester"], OkRunner(), window, clock=w.clock)
    send_task(w, "Build the widget")
    lead.step()
    alice.step()
    tester.step()
    assert snap(w, window).tip.tree["shared.py"] == "KEEP = 1\n"
    return SimpleNamespace(w=w, lead=lead, alice=alice, bob=bob, tester=tester, window=window)


def _bob_pushes(w, window):
    props = [prop for prop in snap(w, window).proposals.values() if prop.author == "@bob"]
    return sorted(props, key=lambda prop: prop.received_at)


def test_review_context_has_the_file_after_own_requirements_and_earlier_objections(tmp_path):
    contexts = []

    def edit(subtask, tree, blame, feedback=None):
        if "plant" in subtask.title.lower():
            return {"shared.py": "KEEP = 1\n"}
        return {"shared.py": "KEEP = 1  # kept\nEXTRA = 1\n" if feedback else "KEEP = 0\n"}

    def review(diff, context=""):
        contexts.append(context)
        if "+KEEP = 0" in diff:
            return "object", "keep KEEP = 1; do not replace it"
        return "approve", "KEEP stays"

    c = _clash(tmp_path, edit, review)
    c.bob.step()
    c.alice.step()
    first = contexts[-1]
    assert "----- shared.py before this push -----\nKEEP = 1\n" in first
    assert "----- shared.py after this push -----\nKEEP = 0\n" in first
    assert "KEEP must stay 1" in first
    assert "The push is for" in first and "add EXTRA = 1" in first
    assert "consent window closes" in first
    assert "Earlier objections" not in first

    c.bob.step()
    c.alice.step()
    second = contexts[-1]
    assert len(contexts) == 2
    assert "Earlier objections on this subtask" in second
    assert "@alice objected to @bob's push" in second
    assert "keep KEEP = 1; do not replace it" in second
    assert "EXTRA = 1" in second.split("after this push", 1)[1]


def test_abstained_review_retries_within_the_window_and_never_votes_late(tmp_path, capsys):
    from gud.worker import REVIEW_RETRY

    window = 60
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "VALUE = 1\n"})
    planted = w.edit("@alice", genesis, "shared.py", "KEEP = 1\n", intent="plant")
    w.test(planted)
    bad = w.edit("@bob", planted, "shared.py", "KEEP = 0\n", intent="replace")
    assert snap(w, window).proposals[bad.id].window_closes_at is not None

    calls = []

    def abstain(diff, context=""):
        calls.append(diff)
        return "abstain", "model gave no answer"

    alice = Worker(w.mailbox, w.ids["@alice"], window, ScriptedCoder(review_fn=abstain), clock=w.clock)
    assert alice.step() == []
    assert alice.step() == []
    assert len(calls) == 1
    w.tick(REVIEW_RETRY + 0.5)
    assert alice.step() == []
    assert len(calls) == 2
    w.tick(window)
    assert alice.step() == []
    assert alice.step() == []
    assert len(calls) == 2
    out = capsys.readouterr().out
    assert out.count(f"late review @alice {bad.id}") == 1

    # A decision that comes back after the window closed is not sent either.
    def slow(diff, context=""):
        w.tick(window)
        return "object", "too late to matter"

    late = w.edit("@carol", planted, "shared.py", "KEEP = 2\n", intent="replace again")
    alice = Worker(w.mailbox, w.ids["@alice"], window, ScriptedCoder(review_fn=slow), clock=w.clock)
    assert alice.step() == []
    assert f"late review @alice {late.id}" in capsys.readouterr().out
    targets = {compose_target(email) for email in w.repo_mail() if compose_kind(email) in {"approve", "object"}}
    assert bad.id not in targets and late.id not in targets


def test_lead_review_retries_and_skips_a_late_vote(tmp_path, capsys):
    from gud.worker import REVIEW_RETRY

    window = 60
    w = World.create(tmp_path)
    genesis = w.genesis({"app.py": "VALUE = 1\n"})
    push = w.edit("@bob", genesis, "app.py", "VALUE = 2\n", intent="bump")
    assert "@lead" in snap(w, window).proposals[push.id].affected_owners
    calls = []

    def abstain(diff, context=""):
        calls.append(context)
        return "abstain", "no answer"

    lead = Lead(w.mailbox, w.ids["@lead"], window, ScriptedPlanner({}), clock=w.clock,
                reviewer=ScriptedCoder(review_fn=abstain))
    lead.step()
    lead.step()
    assert len(calls) == 1
    assert "You wrote the starting files" in calls[0]
    assert "----- app.py after this push -----\nVALUE = 2\n" in calls[0]
    w.tick(REVIEW_RETRY + 0.5)
    lead.step()
    assert len(calls) == 2
    w.tick(window)
    assert lead.step() == []
    assert len(calls) == 2
    assert f"late review @lead {push.id}" in capsys.readouterr().out


def test_objection_feedback_names_the_owner_reason_and_earlier_diff(tmp_path):
    feedback_seen = []

    def edit(subtask, tree, blame, feedback=None):
        if "plant" in subtask.title.lower():
            return {"shared.py": "KEEP = 1\n"}
        if feedback:
            feedback_seen.append(feedback)
            return {"shared.py": "KEEP = 1\nEXTRA = 1\n"}
        return {"shared.py": "KEEP = 0\n"}

    def review(diff, context=""):
        if "+KEEP = 0" in diff:
            return "object", "keep KEEP = 1; the plant subtask requires it"
        return "approve", "KEEP stays"

    c = _clash(tmp_path, edit, review)
    c.bob.step()
    c.alice.step()
    c.bob.step()
    assert len(feedback_seen) == 1
    feedback = feedback_seen[0]
    assert feedback.startswith("objected by @alice: keep KEEP = 1; the plant subtask requires it")
    assert "owned by @alice" in feedback
    assert "Your earlier diff:" in feedback and "+KEEP = 0" in feedback and "-KEEP = 1" in feedback
    assert "approval from @alice" in feedback and "consent window closes" in feedback
    assert "every requirement of" in feedback


def test_second_objection_stops_the_worker_and_the_lead_escalates_once(tmp_path, capsys):
    from gud.wire import parse_body

    def edit(subtask, tree, blame, feedback=None):
        if "plant" in subtask.title.lower():
            return {"shared.py": "KEEP = 1\n"}
        return {"shared.py": "KEEP = 2\n" if feedback else "KEEP = 0\n"}

    reasons = []

    def review(diff, context=""):
        if "-KEEP = 1" in diff:
            reasons.append(f"keep KEEP = 1 (objection {len(reasons) + 1})")
            return "object", reasons[-1]
        return "approve", "fine"

    c = _clash(tmp_path, edit, review)
    c.bob.step()
    c.alice.step()
    c.bob.step()
    c.alice.step()
    pushes = _bob_pushes(c.w, c.window)
    assert [prop.status for prop in pushes] == ["objected", "objected"]
    assert c.bob.step() == []
    assert c.bob.step() == []
    assert len(_bob_pushes(c.w, c.window)) == 2
    assert "stopped revising" in capsys.readouterr().out

    sent = c.lead.step()
    notes = [email for email in sent if compose_kind(email) == "notify"]
    assert len(notes) == 1
    note = parse_body(notes[0].body)
    assert note.get("Gud-Ask") == "decide"
    assert note.get("Gud-Target") == pushes[-1].message_id
    assert "escalated" in note.text
    assert reasons[0] in note.text and reasons[1] in note.text
    assert any(addr.startswith("nelson@") for addr in notes[0].to)
    assert "lead: escalated" in capsys.readouterr().out

    assert not [email for email in c.lead.step() if compose_kind(email) == "notify"]
    restarted = Lead(c.w.mailbox, c.w.ids["@lead"], c.window, ScriptedPlanner({}), clock=c.w.clock,
                     pool=["@alice", "@bob"], stale_after=10_000)
    assert not [email for email in restarted.step() if compose_kind(email) == "notify"]
    decides = [email for email in c.w.repo_mail()
               if compose_kind(email) == "notify" and parse_body(email.body).get("Gud-Ask") == "decide"]
    assert len(decides) == 1
    task = derive(snap(c.w, c.window))[0]
    assert task.status != "done"
    assert next(sub for sub in task.subtasks if sub.assignee == "@bob").status not in {"landed", "no-change"}


def test_kill_and_rebind_reassigns_after_stale_after(tmp_path):
    window = 60
    w = World.create(tmp_path)
    w.genesis({"app.py": "VALUE = 1\n"})
    planner = ScriptedPlanner({
        "idle": [("Sit still", "do not push", ["app.py"])],
    })
    lead = Lead(w.mailbox, w.ids["@lead"], window, planner, clock=w.clock,
                stale_after=30, pool=["@alice", "@bob", "@carol"])
    send_task(w, "Stay idle")
    lead.step()
    tasks = derive(snap(w, window))
    assert tasks[0].subtasks[0].assignee == "@alice"

    w.tick(31)
    lead.step()
    tasks = derive(snap(w, window))
    assert len(tasks[0].subtasks) == 1
    assert tasks[0].subtasks[0].assignee == "@bob"
    assert len([email for email in w.repo_mail() if compose_kind(email) == "subtask"]) == 2

    lead.step()
    tasks = derive(snap(w, window))
    assert tasks[0].subtasks[0].assignee == "@bob"
    assert len([email for email in w.repo_mail() if compose_kind(email) == "subtask"]) == 2


def test_scripted_demo_edits_pass_and_shape_consent(tmp_path):
    """The demo coder's trees stay green, and the conflict push is an objection rather than a window."""
    from gud.wire import parse_body

    coder = ScriptedCoder()
    source = read_dir(ROOT / "demo_repo")

    def apply(tree, title, feedback=None):
        sub = SimpleNamespace(id="T0.1", title=title, description="", assignee="@bob")
        edits = coder.edit(sub, tree, {}, feedback=feedback)
        assert edits, title
        out = dict(tree)
        for path, content in edits.items():
            if content is None:
                out.pop(path, None)
            else:
                out[path] = content
        for path, content in edits.items():
            if path.endswith(".py") and content:
                compile(content, path, "exec")
        return out

    full = dict(source)
    for title in ("Rate-limit login", "Bill overage in charge", "Map 429 and meter charges", "Add penalize"):
        full = apply(full, title)
    full = apply(full, "Replace login with a lockout counter", feedback="objected: keep the rate-limit check")
    full = apply(full, "Add RateLimitDecision")
    bad = apply(apply(dict(source), "Rate-limit login"), "Replace login with a lockout counter")
    assert "gud-bad-edit" in bad["auth.py"]
    assert "rate limited" in bad["auth.py"]
    assert "gud-bad-edit" not in full["auth.py"]
    assert "owner kept this check" in full["auth.py"]
    assert "class RateLimitDecision" in full["api/interfaces.py"]
    assert "def penalize(" in full["ratelimit.py"]

    runner = LocalRunner(timeout=30)
    try:
        for label, tree in (("bad lockout", bad), ("full demo", full)):
            result = runner.run(tree)
            assert result.passed, label + "\n" + result.log
    finally:
        runner.close()

    w = World.create(tmp_path)
    parent = w.genesis(source)
    penalize = _push_edit(w, coder, parent, "@alice", "Add penalize")
    owners = snap(w, 60).proposals[penalize.id].affected_owners
    assert owners == [], owners

    w2 = World.create(tmp_path / "login")
    parent = w2.genesis(source)
    rate = _push_edit(w2, coder, parent, "@bob", "Rate-limit login")
    proposal = snap(w2, 60).proposals[rate.id]
    assert "@lead" in proposal.affected_owners
    w2.approve("@lead", rate)
    w2.test(rate)
    landed = snap(w2, 60).tip
    assert landed.message_id == rate.id

    bad_push = _push_edit(w2, coder, landed, "@alice", "Replace login with a lockout counter")
    proposal = snap(w2, 60).proposals[bad_push.id]
    assert "@bob" in proposal.affected_owners
    w2.object("@bob", bad_push, "keep the rate-limit check; do not replace login")
    assert snap(w2, 60).proposals[bad_push.id].status == "objected"

    revised = _push_edit(w2, coder, landed, "@alice", "Replace login with a lockout counter",
                         feedback="objected: keep the rate-limit check")
    proposal = snap(w2, 60).proposals[revised.id]
    for owner in proposal.affected_owners:
        w2.approve(owner, revised)
    w2.test(revised)
    chain = snap(w2, 60)
    assert chain.tip.message_id == revised.id, _dump(w2, 60)
    assert "gud-bad-edit" not in chain.tip.tree["auth.py"]
    assert "locked" in chain.tip.tree["auth.py"]
    assert parse_body(revised.email.body).diff


def _push_edit(w, coder, parent, author, title, feedback=None):
    sub = SimpleNamespace(id="T0.1", title=title, description="", assignee=author)
    edits = coder.edit(sub, parent.tree, getattr(parent, "blame", {}), feedback=feedback)
    tree = dict(parent.tree)
    for path, content in edits.items():
        if content is None:
            tree.pop(path, None)
        else:
            tree[path] = content
    view = parent if hasattr(parent, "id") else SimpleNamespace(
        id=parent.message_id, state=parent.state, tree=parent.tree,
    )
    return w.push(author, parent=view, tree=tree, intent=title)


def test_coder_reply_with_trailing_junk_still_parses():
    from gud.worker import _parse_edits
    raw = '{"files": {"a.py": "x = 2\\n"}}, "done": ""}'
    assert _parse_edits(raw, {"a.py": "x = 1\n"}) == {"a.py": "x = 2\n"}


def test_revision_identical_to_the_objected_push_is_detected():
    from gud.worker import _repeats
    objected = {"a.py": "x = 2\n", "b.py": "y\n"}
    assert _repeats({"a.py": "x = 1\n", "b.py": "y\n"}, {"a.py": "x = 2\n"}, objected)
    assert not _repeats({"a.py": "x = 1\n", "b.py": "y\n"}, {"a.py": "x = 3\n"}, objected)
    assert not _repeats({"a.py": "x = 1\n"}, {"a.py": "x = 2\n"}, None)


def compose_kind(email):
    from gud.wire import parse_body
    return parse_body(email.body).kind


def compose_target(email):
    from gud.wire import parse_body
    return parse_body(email.body).get("Gud-Target")


def _dump(w, window):
    chain = snap(w, window)
    lines = [f"tip {chain.tip.author if chain.tip else None} {chain.tip.subject if chain.tip else None}"]
    for prop in chain.proposals.values():
        lines.append(f"  {prop.status} {prop.author} {prop.reason}")
    return "\n".join(lines)

