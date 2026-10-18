"""Task derivation: subtasks, reassignment, proposed/landed, task-done, notify."""

from gud import compose, sig
from gud.replay import replay
from gud.tasks import derive, next_task_id, open_subtasks_for
from gud.testkit import REPO, World
from gud.wire import parse_body


def chain(w, window=60):
    return replay(w.repo_mail(), window=window, now=w.clock.t)


def task_email(w, task_id, title, description="do the work", requester="@nelson"):
    body = compose.task_body(w.ids["@console"], task_id, requester, description)
    return compose.send(w.mailbox, w.ids["@console"], w.members, f"[gud] {task_id}: {title}", body)


def subtask_email(w, sub_id, parent, assignee, title, description="edit the file"):
    body = compose.subtask_body(w.ids["@lead"], sub_id, parent, assignee, description)
    return compose.send(w.mailbox, w.ids["@lead"], w.members, f"[gud] {sub_id}: {title}", body)


def test_derive_subtasks_reassignment_and_open_for(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    task_email(w, "T1", "Rate limit login")
    assert next_task_id(w.repo_mail()) == "T2"
    subtask_email(w, "T1.1", "T1", "@alice", "Rate-limit login")
    subtask_email(w, "T1.2", "T1", "@bob", "Bill overage")

    tasks = derive(chain(w))
    assert [task.id for task in tasks] == ["T1"]
    assert tasks[0].status == "in_progress"
    assert tasks[0].requester == "@nelson"
    assert [(sub.id, sub.assignee, sub.status) for sub in tasks[0].subtasks] == [
        ("T1.1", "@alice", "open"),
        ("T1.2", "@bob", "open"),
    ]
    assert [sub.id for sub in open_subtasks_for(tasks, "@alice")] == ["T1.1"]

    subtask_email(w, "T1.1", "T1", "@carol", "Rate-limit login", description="reassigned")
    tasks = derive(chain(w))
    assert len(tasks[0].subtasks) == 2
    first = tasks[0].subtasks[0]
    assert first.id == "T1.1"
    assert first.assignee == "@carol"
    assert first.description == "reassigned"
    assert open_subtasks_for(tasks, "@alice") == []
    assert [sub.id for sub in open_subtasks_for(tasks, "@carol")] == ["T1.1"]


def test_proposed_then_landed(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "a = 1\n"})
    task_email(w, "T3", "Add a file")
    subtask_email(w, "T3.1", "T3", "@alice", "Add new.py")
    pushed = w.edit("@alice", parent, "new.py", "n = 1\n", intent="add new", task="T3.1")

    tasks = derive(chain(w))
    assert tasks[0].subtasks[0].status == "proposed"
    assert tasks[0].subtasks[0].link_id is None
    assert chain(w).proposals[pushed.id].task == "T3.1"

    w.test(pushed)
    tasks = derive(chain(w))
    assert tasks[0].status == "in_progress"
    assert tasks[0].subtasks[0].status == "landed"
    assert tasks[0].subtasks[0].link_id == pushed.id
    assert open_subtasks_for(tasks, "@alice") == []


def test_task_done_and_notified_at(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "a = 1\n"})
    task_email(w, "T1", "Add a file", description="ship it")
    subtask_email(w, "T1.1", "T1", "@alice", "Add new.py")
    pushed = w.edit("@alice", parent, "new.py", "n = 1\n", task="T1.1")
    w.test(pushed)
    tip = chain(w).tip
    done = compose.task_done_body(w.ids["@lead"], "T1", tip.message_id, "Add a file is done")
    compose.send(w.mailbox, w.ids["@lead"], w.members, "[gud] T1 done: Add a file", done)

    tasks = derive(chain(w))
    assert tasks[0].status == "done"
    assert tasks[0].done_link == tip.message_id
    assert tasks[0].notified_at is None
    assert open_subtasks_for(tasks, "@alice") == []

    note = compose.notify_body(w.ids["@lead"], "shipped\n", task="T1", target=tip.message_id)
    first = w.mailbox.send(from_addr=w.ids["@lead"].address, to=w.ids["@nelson"].address, cc=[REPO],
                           subject="[gud] T1 done: Add a file", body=note)
    w.tick(5)
    later = compose.notify_body(w.ids["@lead"], "again\n", task="T1", target=tip.message_id)
    w.mailbox.send(from_addr=w.ids["@lead"].address, to=w.ids["@nelson"].address, cc=[REPO],
                   subject="[gud] T1 done again", body=later)

    tasks = derive(chain(w))
    assert tasks[0].status == "done"
    assert tasks[0].notified_at == first.received_at


def _no_change(w, who, sub_id, tip_id, reason):
    body = compose.no_change_body(w.ids[who], sub_id, tip_id, reason)
    email = compose.send(w.mailbox, w.ids[who], w.members, f"[gud] {sub_id}: no change", body)
    return email


def test_no_change_ignores_non_assignees_and_drops_when_named_files_change(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"index.html": "<!doctype html>\n", "other.txt": "a\n"})
    task_email(w, "T1", "Doctype")
    subtask_email(w, "T1.1", "T1", "@alice", "Add the doctype",
                  description="Files: index.html\nThe page needs a doctype.")

    _no_change(w, "@bob", "T1.1", parent.id, "looks done")
    assert derive(chain(w))[0].subtasks[0].status == "open"

    forged = compose.no_change_body(w.ids["@alice"], "T1.1", parent.id, "doctype is already there")
    forged = forged.replace("Gud-Reason: doctype is already there", "Gud-Reason: forged")
    compose.send(w.mailbox, w.ids["@alice"], w.members, "[gud] T1.1: no change", forged)
    assert derive(chain(w))[0].subtasks[0].status == "open"

    good = _no_change(w, "@alice", "T1.1", parent.id, "doctype is already there")
    parsed = parse_body(good.body)
    assert sig.verify(w.members["@alice"].public_key, parsed.trailers, parsed.diff)
    sub = derive(chain(w))[0].subtasks[0]
    assert sub.status == "no-change"
    assert sub.reason == "doctype is already there"
    assert open_subtasks_for(derive(chain(w)), "@alice") == []

    notes = w.edit("@alice", parent, "notes.txt", "n = 1\n", intent="add notes")
    w.test(notes)
    assert derive(chain(w))[0].subtasks[0].status == "no-change"

    changed = w.edit("@alice", notes, "index.html", "<html>\n", intent="drop the doctype")
    w.approve("@lead", changed)
    w.test(changed)
    assert derive(chain(w))[0].subtasks[0].status == "open"

    tip = chain(w).tip
    _no_change(w, "@alice", "T1.1", tip.message_id, "still nothing to do")
    sub = derive(chain(w))[0].subtasks[0]
    assert sub.status == "no-change"
    assert sub.reason == "still nothing to do"

    subtask_email(w, "T1.1", "T1", "@carol", "Add the doctype",
                  description="Files: index.html\nreassigned")
    sub = derive(chain(w))[0].subtasks[0]
    assert sub.assignee == "@carol"
    assert sub.status == "open"


def test_no_change_with_no_named_files_drops_on_any_edit(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"index.html": "<!doctype html>\n"})
    task_email(w, "T1", "Look around")
    subtask_email(w, "T1.1", "T1", "@alice", "Check the tree", description="nothing to add")
    _no_change(w, "@alice", "T1.1", parent.id, "already satisfied")
    assert derive(chain(w))[0].subtasks[0].status == "no-change"
    pushed = w.edit("@alice", parent, "notes.txt", "n = 1\n", intent="add notes")
    w.test(pushed)
    assert derive(chain(w))[0].subtasks[0].status == "open"


def test_in_flight_push_blocks_no_change_and_a_link_wins(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"index.html": "<!doctype html>\n"})
    task_email(w, "T1", "Doctype")
    subtask_email(w, "T1.1", "T1", "@alice", "Add the doctype", description="Files: index.html")
    pushed = w.edit("@alice", parent, "index.html", "<!doctype html>\n<html>\n", task="T1.1", intent="wrap it")
    _no_change(w, "@alice", "T1.1", parent.id, "already there")
    assert derive(chain(w))[0].subtasks[0].status == "proposed"
    w.approve("@lead", pushed)
    w.test(pushed)
    sub = derive(chain(w))[0].subtasks[0]
    assert sub.status == "landed"
    assert sub.link_id == pushed.id
