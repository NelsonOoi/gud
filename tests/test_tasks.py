"""Task derivation: subtasks, reassignment, proposed/landed, task-done, notify."""

from gud import compose
from gud.replay import replay
from gud.tasks import derive, next_task_id, open_subtasks_for
from gud.testkit import REPO, World


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
