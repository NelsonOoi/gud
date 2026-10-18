"""The shared feed: snapshots, deltas, decisions, and reading a file without cloning."""

from __future__ import annotations

import base64

import pytest

from gud import sig
from gud.config import Config
from gud.console import Console, ConsoleError
from gud.lead import Lead, ScriptedPlanner
from gud.replay import replay
from gud.testkit import REPO, World
from gud.types import Member
from gud.wire import parse_body

BUGS = "bugs@gud.local"


def _cfg(tmp_path, **overrides) -> Config:
    fields = dict(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None, instabox_api_key=None,
        armature_api_key=None, openrouter_key=None, agent_model="test",
    )
    fields.update(overrides)
    return Config(**fields)


def _console(w: World, tmp_path, refresh_seconds: float = 0, **overrides) -> Console:
    return Console(
        _cfg(tmp_path, **overrides), mailbox=w.mailbox, clock=w.clock,
        identity=w.ids["@console"], refresh_seconds=refresh_seconds,
    )


def _rewrite(w: World, parent, path: str, text: str, intent: str = "edit"):
    tree = dict(parent.tree)
    tree[path] = text
    return w.push("@alice", parent=parent, tree=tree, intent=intent)


def _report(w: World, subject="Save crashes", body="Clicking save throws.\n"):
    return w.mailbox.send(from_addr="sam@acme.example", to=BUGS, subject=subject, body=body)


def _status(w: World, message_id: str) -> str:
    chain = replay(w.repo_mail(), 60, w.clock.t + 1)
    prop = chain.proposals.get(message_id)
    assert prop is not None, message_id
    return prop.status


def test_snapshot_lists_tip_tasks_bugs_and_events(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "x = 1\n"})
    bug = _report(w)
    c = _console(w, tmp_path)
    tid = c.create_task("Add a table", "comparison table", "@nelson")
    snap = c.state()

    assert snap["tip"]["seq"] == 0
    assert snap["tip"]["author"] == "@lead"
    assert len(snap["tip"]["state"]) == 12
    assert snap["tasks"][0]["id"] == tid
    assert snap["tasks"][0]["title"] == "Add a table"
    assert snap["bugs"][0]["id"] == bug.message_id
    assert snap["bugs"][0]["status"] == "pending"
    kinds = {event["kind"] for event in snap["events"]}
    assert {"task", "bug", "link", "push"} <= kinds
    link = next(event for event in snap["events"] if event["kind"] == "link")
    assert "added" in link and "removed" in link
    assert snap["cursor"].endswith(snap["events"][0]["id"]) or "|" in snap["cursor"]
    assert any(item["id"] == bug.message_id and item["verbs"] == ["approve", "reject"] for item in snap["needs_you"])


def test_since_returns_only_newer_events_and_changed_rows(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "x = 1\n"})
    c = _console(w, tmp_path)
    before = c.state()
    old = {event["id"] for event in before["events"]}
    tid = c.create_task("Ship the parser", "split it", "@nelson")
    delta = c.state(since=before["cursor"])

    assert delta["delta"] is True
    assert delta["cursor"] != before["cursor"]
    assert old.isdisjoint({event["id"] for event in delta["events"]})
    assert any(event["kind"] == "task" and event["task"] == tid for event in delta["events"])
    assert [task["id"] for task in delta["tasks"]] == [tid]
    assert delta["bugs"] == []


def test_cache_holds_one_repo_between_polls(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "x = 1\n"})
    c = _console(w, tmp_path, refresh_seconds=60)
    first = c.state()
    _rewrite(w, parent, "app.py", "x = 2\n")
    second = c.state()
    assert second["cursor"] == first["cursor"]
    c.invalidate()
    third = c.state()
    assert third["cursor"] != first["cursor"]
    assert any(event["kind"] == "push" for event in third["events"])


def test_needs_you_names_open_pushes_and_pending_bugs(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "def f():\n    return 1\n"})
    pushed = _rewrite(w, parent, "app.py", "def f():\n    return 2\n")
    bug = _report(w)
    needs = _console(w, tmp_path).state()["needs_you"]
    by_id = {item["id"]: item for item in needs}
    assert by_id[pushed.id]["verbs"] == ["approve", "object", "veto"]
    assert by_id[pushed.id]["kind"] == "push"
    assert by_id[bug.message_id]["kind"] == "bug"
    assert by_id[bug.message_id]["verbs"] == ["approve", "reject"]


def test_focus_thread_diff_and_file_blame(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "def f():\n    return 1\n"})
    pushed = _rewrite(w, parent, "app.py", "def f():\n    return 2\n", intent="return 2")
    c = _console(w, tmp_path)

    thread = c.state(focus=pushed.id)["focus"]
    assert thread["kind"] == "thread"
    assert any(email["id"] == pushed.id for email in thread["emails"])
    assert "return 2" in thread["diff"]

    file = c.state(focus="app.py")["focus"]
    assert file["kind"] == "file"
    assert file["path"] == "app.py"
    assert any(line["owner"] == "@lead" and "return 1" in line["text"] for line in file["lines"])
    assert c.state(focus="missing.py")["focus"]["kind"] == "missing"

    tid = c.create_task("Explain f", "what it returns", "@nelson")
    task = c.state(focus=tid)["focus"]
    assert task["kind"] == "thread"
    assert any(email["kind"] == "task" for email in task["emails"])


def test_create_task_sends_a_signed_task_from_console(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "x = 1\n"})
    tid = _console(w, tmp_path).create_task("Add a table", "Pro is $8", "@nelson")
    email = next(item for item in w.repo_mail() if parse_body(item.body).get("Gud-Task") == tid)
    parsed = parse_body(email.body)
    assert parsed.kind == "task"
    assert parsed.get("Gud-Author") == "@console"
    assert parsed.get("Gud-Requester") == "@nelson"
    assert "Pro is $8" in parsed.text
    assert sig.verify(w.members["@console"].public_key, parsed.trailers, parsed.diff)


def test_env_key_signs_when_no_key_file_exists(tmp_path, monkeypatch):
    w = World.create(tmp_path)
    w.genesis({"app.py": "x = 1\n"})
    monkeypatch.setenv("GUD_KEY", base64.b64encode(w.ids["@console"].private_raw).decode())
    console = Console(_cfg(tmp_path), mailbox=w.mailbox, clock=w.clock, refresh_seconds=0)
    tid = console.create_task("From the env", "no key file", "@nelson")
    email = next(item for item in w.repo_mail() if parse_body(item.body).get("Gud-Task") == tid)
    parsed = parse_body(email.body)
    assert sig.verify(w.members["@console"].public_key, parsed.trailers, parsed.diff)


def test_decide_approve_object_and_veto_change_status(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "def f():\n    return 1\n"})
    objected = _rewrite(w, parent, "app.py", "def f():\n    return 2\n", intent="two")
    _console(w, tmp_path).decide(objected.id, "object", "not yet", "@nelson")
    assert _status(w, objected.id) == "objected"

    vetoed_world = World.create(tmp_path / "veto")
    parent = vetoed_world.genesis({"app.py": "def f():\n    return 1\n"})
    vetoed = _rewrite(vetoed_world, parent, "app.py", "def f():\n    return 9\n", intent="nine")
    _console(vetoed_world, tmp_path / "veto-home").decide(vetoed.id, "veto", "stop", "@nelson")
    assert _status(vetoed_world, vetoed.id) == "vetoed"

    approved = World.create(tmp_path / "approve", protected="notes.txt explicit")
    parent = approved.genesis({"app.py": "x = 1\n"})
    tree = dict(parent.tree)
    tree["notes.txt"] = "hi\n"
    pushed = approved.push("@alice", parent=parent, tree=tree, intent="add notes")
    assert _status(approved, pushed.id) == "needs_approval"
    _console(approved, tmp_path / "approve-home").decide(pushed.id, "approve", "ship it", "@nelson")
    assert _status(approved, pushed.id) == "testing"


def test_bug_approve_leads_the_lead_to_open_a_task(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    bug = _report(w, subject="Save crashes", body="Clicking save throws.\n")
    c = _console(w, tmp_path)
    c.decide(bug.message_id, "approve", "reproduced", "@nelson")
    lead = Lead(
        w.mailbox, w.ids["@lead"], 60, ScriptedPlanner({}), clock=w.clock, pool=["@alice"],
        bugs_address=BUGS,
    )
    sent = lead.step()
    tasks = [email for email in sent if parse_body(email.body).kind == "task"]
    assert len(tasks) == 1
    assert parse_body(tasks[0].body).get("Gud-Bug") == bug.message_id
    again = c.state()
    assert again["bugs"][0]["status"] == "accepted"
    assert again["bugs"][0]["task_id"]
    assert any(event["kind"] == "bug-accepted" for event in again["events"])


def test_wrong_verb_and_unknown_target_are_refused(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "def f():\n    return 1\n"})
    pushed = _rewrite(w, parent, "app.py", "def f():\n    return 2\n")
    bug = _report(w)
    c = _console(w, tmp_path)

    with pytest.raises(ConsoleError) as rejected:
        c.decide(pushed.id, "reject", "no", "@nelson")
    assert rejected.value.status == 400

    with pytest.raises(ConsoleError) as vetoed:
        c.decide(bug.message_id, "veto", "no", "@nelson")
    assert vetoed.value.status == 400

    with pytest.raises(ConsoleError) as missing:
        c.decide("<missing@gud.local>", "approve", "", "@nelson")
    assert missing.value.status == 404

    with pytest.raises(ConsoleError) as stranger:
        c.decide(bug.message_id, "approve", "", "@alice")
    assert stranger.value.status == 400

    c.decide(bug.message_id, "reject", "sales", "@nelson")
    with pytest.raises(ConsoleError) as closed:
        c.decide(bug.message_id, "approve", "", "@nelson")
    assert closed.value.status == 409


def test_gmail_human_sends_the_decision_from_the_agent_domain(tmp_path):
    w = World.create(tmp_path)
    w.members["@nelson"] = Member("@nelson", "human", "nelsonwmooi@gmail.com", None, None)
    parent = w.genesis({"app.py": "def f():\n    return 1\n"})
    pushed = _rewrite(w, parent, "app.py", "def f():\n    return 9\n", intent="nine")
    c = _console(w, tmp_path)
    mid = c.decide(pushed.id, "veto", "stop", "@nelson")
    email = next(item for item in w.repo_mail() if item.message_id == mid)
    assert email.from_addr == "nelson@gud.local"
    assert _status(w, pushed.id) == "vetoed"

    bug = _report(w)
    c.decide(bug.message_id, "reject", "placeholder", "@nelson")
    row = next(item for item in c.state()["bugs"] if item["id"] == bug.message_id)
    assert row["status"] == "rejected"
    assert row["review_by"] == "@nelson"


def test_decide_is_allowed_when_the_mailbox_is_primitive(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "def f():\n    return 1\n"})
    pushed = _rewrite(w, parent, "app.py", "def f():\n    return 3\n")
    c = _console(w, tmp_path, mailbox="primitive")
    mid = c.decide(pushed.id, "veto", "no", "@nelson")
    email = next(item for item in w.repo_mail() if item.message_id == mid)
    assert email.body.startswith("veto\n")
    assert _status(w, pushed.id) == "vetoed"
