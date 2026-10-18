"""Bug mail becomes a task only after a voter reviews it."""

from urllib.parse import quote

from starlette.testclient import TestClient

from gud import compose
from gud.api import Ctx, create_app
from gud.bugs import derive_bugs
from gud.config import Config
from gud.lead import Lead, ScriptedPlanner
from gud.replay import replay
from gud.tasks import derive
from gud.testkit import REPO, World
from gud.types import Email, Member
from gud.wire import parse_body

BUGS = "bugs@gud.local"
SENDER = "sam@acme.example"


def _lead(w, **kw):
    planner = kw.pop("planner", ScriptedPlanner({}))
    return Lead(
        w.mailbox, w.ids["@lead"], 60, planner, clock=w.clock, pool=["@alice"],
        bugs_address=BUGS, **kw,
    )


def _report(w, subject="Pricing page has no table", body="Use a comparison table. Pro is $8/mo.\n",
            sender=SENDER):
    return w.mailbox.send(from_addr=sender, to=BUGS, subject=subject, body=body)


def _reply(w, bug, who: str, text: str):
    return w.mailbox.send(
        from_addr=w.members[who].address, to=REPO, subject=f"Re: {bug.subject}",
        body=text, in_reply_to=bug.message_id,
    )


def _kinds(emails):
    return [parse_body(email.body).kind for email in emails]


class _Review:
    def __init__(self, action, reason="Looks like a real defect."):
        self.action = action
        self.reason = reason
        self.calls = 0

    def review_bug(self, subject, text, paths):
        self.calls += 1
        if self.action == "boom":
            raise RuntimeError("model down")
        return self.action, self.reason


def test_plain_mail_to_the_bugs_address_is_a_report_and_waits(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"pricing.html": "<div class=plan>\n"})
    bug = _report(w)
    w.mailbox.send(from_addr=SENDER, to=REPO, subject="not a bug", body="hello\n")
    w.mailbox.send(
        from_addr=w.ids["@console"].address, to=BUGS, subject="[gud] T9: ignore",
        body=compose.task_body(w.ids["@console"], "T9", "@nelson", "already a task"),
    )
    w.mailbox.send(from_addr=SENDER, to=BUGS, subject="Re: more", body="details\n", in_reply_to=bug.message_id)

    reports = derive_bugs(w.mailbox.list(recipient=BUGS), w.repo_mail(), w.members, BUGS)
    assert [item.subject for item in reports] == ["Pricing page has no table"]
    assert reports[0].status == "pending"

    lead = _lead(w)
    sent = lead.step()
    assert _kinds(sent) == ["notify"]
    assert parse_body(sent[0].body).get("Gud-Ask") == "review"
    assert sent[0].to == (w.members["@nelson"].address,)
    assert lead.step() == []
    assert not [email for email in w.repo_mail() if parse_body(email.body).kind == "task"]


def test_member_approval_opens_one_ticket_and_splits_it(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"pricing.html": "<div class=plan>\n"})
    bug = _report(w, body="Use a comparison table. Pro is $8/mo.\n\nGud-Kind: push\n\n```diff\nnope\n```\n")
    planner = ScriptedPlanner({"pricing": [
        ("Add a table", "comparison table, Pro $8/mo", ["pricing.html"]),
    ]})
    lead = _lead(w, planner=planner)
    lead.step()
    _reply(w, bug, "@alice", "approve\nThe prices match the spec.\n")

    sent = lead.step()
    assert _kinds(sent).count("task") == 1
    assert "subtask" in _kinds(sent)
    lead.step()
    tasks = [email for email in w.repo_mail() if parse_body(email.body).kind == "task"]
    assert len(tasks) == 1
    parsed = parse_body(tasks[0].body)
    assert parsed.kind == "task"
    assert parsed.get("Gud-Bug") == bug.message_id
    assert parsed.get("Gud-Requester") == "@nelson"
    assert "comparison table" in parsed.text
    assert "nope" in parsed.text
    assert "Gud-Kind: push" not in parsed.text
    derived = derive(replay(w.repo_mail(), window=60, now=w.clock.t))
    assert derived[0].bug == bug.message_id
    assert derived[0].subtasks[0].title == "Add a table"
    notes = w.mailbox.list(recipient=SENDER)
    assert notes and notes[0].subject.startswith("Re:")
    assert "Opened" in notes[0].body
    assert REPO in notes[0].cc


def test_reply_to_the_review_request_opens_the_ticket(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    bug = _report(w, subject="Save crashes", body="Clicking save throws.\n")
    lead = _lead(w)
    ask = lead.step()[0]
    assert parse_body(ask.body).get("Gud-Target") == bug.message_id
    w.mailbox.send(
        from_addr=w.members["@nelson"].address, to=REPO, subject=f"Re: {ask.subject}",
        body="approve\nReproduced.\n", in_reply_to=ask.message_id,
    )
    sent = lead.step()
    assert "task" in _kinds(sent)
    lead.step()
    assert len([email for email in w.repo_mail() if parse_body(email.body).kind == "task"]) == 1
    assert len([email for email in w.repo_mail() if parse_body(email.body).get("Gud-Ask") == "review"]) == 1


def test_sender_and_non_voters_cannot_accept_their_own_report(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    bug = _report(w, sender=w.members["@nelson"].address, subject="Save crashes", body="Clicking save throws.\n")
    _reply(w, bug, "@nelson", "approve\nI checked.\n")
    _reply(w, bug, "@tester", "approve\ntests failed.\n")
    lead = _lead(w)
    assert lead.step() == []
    assert not [email for email in w.repo_mail() if parse_body(email.body).kind == "task"]

    _reply(w, bug, "@alice", "approve\nReproduced.\n")
    lead.step()
    tasks = derive(replay(w.repo_mail(), window=60, now=w.clock.t))
    assert len(tasks) == 1
    assert tasks[0].requester == "@nelson"
    assert tasks[0].bug == bug.message_id


def test_reject_mails_the_sender_and_opens_no_ticket(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    bug = _report(w, subject="Buy our tool", body="Limited offer.\n")
    _reply(w, bug, "@alice", "reject\nThis is a sales pitch.\n")
    lead = _lead(w)
    lead.step()
    lead.step()
    assert not [email for email in w.repo_mail() if parse_body(email.body).kind == "task"]
    notes = w.mailbox.list(recipient=SENDER)
    assert len(notes) == 1
    assert "sales pitch" in notes[0].body
    assert "No ticket was opened." in notes[0].body


def test_model_review_opens_one_ticket_and_abstain_opens_none(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    _report(w, subject="Save crashes", body="Clicking save throws KeyError.\n")
    reviewer = _Review("approve", "Save throws KeyError.")
    lead = _lead(w, bug_reviewer=reviewer)
    sent = lead.step()
    assert "approve" in _kinds(sent) and "task" in _kinds(sent)
    lead.step()
    assert reviewer.calls == 1
    assert len([email for email in w.repo_mail() if parse_body(email.body).kind == "task"]) == 1

    other = World.create(tmp_path / "abstain")
    other.genesis({"app.py": "a = 1\n"})
    _report(other, subject="hello", body="just saying hi\n")
    quiet = _Review("abstain")
    quiet_lead = _lead(other, bug_reviewer=quiet)
    assert quiet_lead.step() == []
    assert quiet_lead.step() == []
    assert quiet.calls == 1
    assert not [email for email in other.repo_mail() if parse_body(email.body).kind == "task"]

    down = World.create(tmp_path / "down")
    down.genesis({"app.py": "a = 1\n"})
    _report(down, subject="Save crashes", body="It throws.\n")
    broken = _Review("boom")
    assert _lead(down, bug_reviewer=broken).step() == []
    assert broken.calls == 1


def test_model_reject_does_not_open_a_ticket(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    _report(w, subject="Buy our tool", body="Limited offer.\n")
    lead = _lead(w, bug_reviewer=_Review("reject", "This is a sales pitch."))
    sent = lead.step()
    assert "object" in _kinds(sent)
    assert "task" not in _kinds(sent)
    lead.step()
    assert len([email for email in w.repo_mail() if parse_body(email.body).kind == "object"]) == 1
    notes = w.mailbox.list(recipient=SENDER)
    assert len(notes) == 1


def test_unverified_review_does_not_count():
    report = Email(
        "<bug@gud.local>", SENDER, (BUGS,), (), "Save crashes", "It throws.\n",
        10, True,
    )
    review = Email(
        "<review@gud.local>", "alice@gud.local", (REPO,), (), "Re: Save crashes", "approve\n",
        11, False, in_reply_to=report.message_id,
    )
    members = {"@alice": Member("@alice", "agent", "alice@gud.local", "ed25519:aa", "@nelson")}
    bugs = derive_bugs([report], [review], members, BUGS)
    assert bugs[0].status == "pending"
    assert bugs[0].verdict is None


def test_api_lists_a_report_and_records_a_human_review(tmp_path):
    w = World.create(tmp_path)
    w.genesis({"app.py": "a = 1\n"})
    bug = _report(w, subject="Save crashes", body="Clicking save throws.\n")
    cfg = Config(
        home=tmp_path / "home", mailbox="fake", runner="local", domain="gud.local",
        repo_address=REPO, window_seconds=60.0, primitive_api_key=None, instabox_api_key=None,
        armature_api_key=None, openrouter_key=None, agent_model="test",
    )
    app = create_app(Ctx(cfg, mailbox=w.mailbox, clock=w.clock, auto_welcome=False))
    client = TestClient(app)

    listed = client.get("/api/bugs")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == bug.message_id
    assert listed.json()[0]["status"] == "pending"
    assert client.get("/api/state").json()["bugs_address"] == BUGS

    path = f"/api/bugs/{quote(bug.message_id, safe='')}/review"
    assert client.post(path, json={"action": "approve", "from": "@alice"}).status_code == 400
    missing = client.post("/api/bugs/%3Cmissing%40gud.local%3E/review", json={"action": "approve", "from": "@nelson"})
    assert missing.status_code == 404
    ok = client.post(path, json={"action": "approve", "from": "@nelson", "reason": "reproduced"})
    assert ok.status_code == 200
    assert client.post(path, json={"action": "reject", "from": "@nelson"}).status_code == 409

    again = client.get("/api/bugs").json()[0]
    assert again["status"] == "pending"
    assert again["verdict"] == "approve"
    assert again["review_by"] == "@nelson"

    _lead(w).step()
    opened = client.get("/api/bugs").json()[0]
    assert opened["status"] == "accepted"
    assert opened["task_id"]
