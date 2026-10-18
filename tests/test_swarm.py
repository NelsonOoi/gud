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
from gud.worker import LLMCoder, ScriptedCoder, Worker
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
    assert coder.edit(sub, {"a.txt": "old\n"}, {}) == {}
    assert len(calls) == 2
    assert coder.edit(sub, {"a.txt": "old\n"}, {}) == {}
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


def compose_kind(email):
    from gud.wire import parse_body
    return parse_body(email.body).kind


def _dump(w, window):
    chain = snap(w, window)
    lines = [f"tip {chain.tip.author if chain.tip else None} {chain.tip.subject if chain.tip else None}"]
    for prop in chain.proposals.values():
        lines.append(f"  {prop.status} {prop.author} {prop.reason}")
    return "\n".join(lines)

