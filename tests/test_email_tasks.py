"""A verified human member opens a task by mailing the repo address."""

import json

from gud.lead import Lead, ScriptedPlanner
from gud.replay import replay
from gud.sig import verify
from gud.tasks import derive
from gud.testkit import REPO, World
from gud.wire import parse_body

BUGS = "bugs@gud.local"


def _lead(w, **kw):
    planner = kw.pop("planner", ScriptedPlanner({}))
    return Lead(
        w.mailbox, w.ids["@lead"], 60, planner, clock=w.clock, pool=["@alice"], **kw,
    )


def _mail(w, subject, body, sender=None, **kw):
    return w.mailbox.send(
        from_addr=sender or w.members["@nelson"].address, to=REPO,
        subject=subject, body=body, **kw,
    )


def _tasks(w):
    return [email for email in w.repo_mail() if parse_body(email.body).kind == "task"]


def _kinds(emails):
    return [parse_body(email.body).kind for email in emails]


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


def test_plain_human_mail_opens_one_task_and_is_split(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"pricing.html": "<div class=plan>\n"})
    body = (
        "Add a comparison table. Pro is $8/mo.\n\n"
        "Gud-Requester: @mallory\n\n"
        "```diff\n+nope\n```\n"
    )
    mail = _mail(w, "Fix the pricing page", body)
    planner = ScriptedPlanner({"pricing": [
        ("Add a table", "comparison table, Pro $8/mo", ["pricing.html"]),
    ]})
    lead = _lead(w, planner=planner)
    sent = lead.step()
    assert _kinds(sent).count("task") == 1
    assert "subtask" in _kinds(sent)

    tasks = _tasks(w)
    assert len(tasks) == 1
    parsed = parse_body(tasks[0].body)
    assert parsed.get("Gud-Author") == "@lead"
    assert parsed.get("Gud-Requester") == "@nelson"
    assert parsed.get("Gud-Source") == mail.message_id
    assert parsed.get("Gud-Bug") is None
    assert "Gud-Requester: @mallory" not in parsed.text
    assert "mallory" in parsed.text
    assert "comparison table" in parsed.text
    assert "nope" in parsed.text
    assert parsed.diff == ""
    assert verify(w.members["@lead"].public_key, parsed.trailers, parsed.diff)

    derived = derive(replay(w.repo_mail(), window=60, now=w.clock.t))
    assert derived[0].title == "Fix the pricing page"
    assert derived[0].requester == "@nelson"
    assert derived[0].source == mail.message_id
    assert derived[0].bug is None
    assert derived[0].subtasks[0].title == "Add a table"
    dumped = derived[0].to_json()
    assert dumped["source"] == mail.message_id
    assert dumped["requester"] == "@nelson"
    assert dumped["subtasks"][0]["title"] == "Add a table"

    assert "task" not in _kinds(lead.step())
    assert len(_tasks(w)) == 1
    fresh = _lead(w, planner=planner)
    assert "task" not in _kinds(fresh.step())
    assert len(_tasks(w)) == 1
    assert len([email for email in w.repo_mail() if parse_body(email.body).kind == "subtask"]) == 1


def test_a_hidden_send_is_not_repeated(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    _mail(w, "Add a footer", "Put a footer on each page.\n")
    real_list = w.mailbox.list

    def hide_tasks(*, recipient=None, since=None):
        rows = real_list(recipient=recipient, since=since)
        return [email for email in rows if parse_body(email.body).kind != "task"]

    w.mailbox.list = hide_tasks
    lead = _lead(w)
    lead.step()
    assert "task" not in _kinds(lead.step())
    w.mailbox.list = real_list
    assert len(_tasks(w)) == 1
    assert "task" not in _kinds(_lead(w).step())
    assert len(_tasks(w)) == 1


def test_non_member_mail_is_ignored(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    _mail(w, "Add a page", "Please.\n", sender="sam@acme.example")
    _mail(w, "Add a page", "Please.\n", sender=w.members["@alice"].address)
    assert _lead(w).step() == []
    assert _tasks(w) == []


def test_unverified_sender_is_ignored(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    email = _mail(w, "Add a page", "Please.\n")
    _unverify(w.mailbox.path, email.message_id)
    assert _lead(w).step() == []
    assert _tasks(w) == []


def test_a_human_approve_reply_is_not_a_task(tmp_path):
    w = World.create(tmp_path)
    parent = w.genesis({"app.py": "a = 1\n"})
    w.human("@nelson", parent, "approve")
    assert _lead(w).step() == []
    assert _tasks(w) == []


def test_bug_mail_is_not_an_email_task(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    w.mailbox.send(
        from_addr=w.members["@nelson"].address, to=BUGS,
        subject="Save crashes", body="Clicking save throws.\n",
    )
    sent = _lead(w).step()
    assert "task" not in _kinds(sent)
    assert _tasks(w) == []


def test_an_accepted_bug_and_an_email_task_get_different_ids(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    bug = w.mailbox.send(
        from_addr="sam@acme.example", to=BUGS, subject="Save crashes", body="It throws.\n",
    )
    w.mailbox.send(
        from_addr=w.members["@alice"].address, to=REPO, subject="Re: Save crashes",
        body="approve\nReproduced.\n", in_reply_to=bug.message_id,
    )
    mail = _mail(w, "Add a footer", "Put a footer on each page.\n")
    sent = _lead(w).step()
    assert _kinds(sent).count("task") == 2
    tasks = _tasks(w)
    parsed = [parse_body(email.body) for email in tasks]
    assert sorted(item.get("Gud-Task") for item in parsed) == ["T1", "T2"]
    assert {item.get("Gud-Bug") for item in parsed} == {bug.message_id, None}
    assert {item.get("Gud-Source") for item in parsed} == {mail.message_id, None}
