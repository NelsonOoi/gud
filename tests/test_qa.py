"""QA mails plain bug reports after task-done. The browser and the site are fakes."""

import urllib.error
import urllib.request

from gud import compose
from gud.bugs import is_bug_report
from gud.lead import Lead, ScriptedPlanner
from gud.qa import QA, LocalSite, _parse_agent_output, _runner_kind
from gud.testkit import World
from gud.wire import parse_body

BUGS = "bugs@gud.local"
ROLES = [
    ("@lead", "lead"), ("@alice", "agent"), ("@console", "console"),
    ("@nelson", "human"), ("@qa", "qa"),
]
FINDING = {
    "title": "Pricing has no comparison table",
    "steps": "Open pricing.html",
    "expected": "A table with Free, Pro $8/mo, and Team $15/user/mo",
    "actual": "Three div cards",
    "page": "pricing.html",
}


def _world(tmp):
    return World.create(tmp, roles=ROLES)


def _done(w, tip, description="Pricing must use a comparison table."):
    body = compose.task_body(w.ids["@console"], "T1", "@nelson", description)
    compose.send(w.mailbox, w.ids["@console"], w.members, "[gud] T1: Build the site", body)
    done = compose.task_done_body(w.ids["@lead"], "T1", tip.id, "landed")
    return compose.send(w.mailbox, w.ids["@lead"], w.members, "[gud] T1 done: Build the site", done)


class FakeSite:
    def __init__(self):
        self.served = 0
        self.stopped = 0
        self.label = ""
        self.tree = None

    def set_label(self, label):
        self.label = label

    def serve(self, tree):
        self.served += 1
        self.tree = tree
        return "http://qa.test/"

    def stop(self):
        self.stopped += 1


class FakeBrowser:
    def __init__(self, findings=None, error=None):
        self.findings = list(findings or [])
        self.error = error
        self.calls = []

    def run(self, url, task, pages):
        self.calls.append({"url": url, "task": task, "pages": pages})
        if self.error:
            raise self.error
        return list(self.findings)


class Approve:
    def __init__(self):
        self.subjects = []

    def review_bug(self, subject, text, paths):
        self.subjects.append(subject)
        return "approve", "The pricing page is missing a comparison table."


def _qa(w, browser, site=None):
    return QA(w.mailbox, w.ids["@qa"], 60, clock=w.clock, runner=site or FakeSite(), browser=browser)


def _bugs(w):
    return w.mailbox.list(recipient=BUGS)


def test_task_done_mails_a_plain_report_once_and_a_restart_does_not_repeat_it(tmp_path):
    w = _world(tmp_path)
    tip = w.genesis({"index.html": "<h1>Home</h1>\n", "pricing.html": "<div class=plan>Pro</div>\n"})
    _done(w, tip)
    browser = FakeBrowser([FINDING])
    site = FakeSite()
    qa = _qa(w, browser, site)

    sent = qa.step()
    assert [email.subject for email in sent] == ["Pricing has no comparison table"]
    assert sent[0].to == (BUGS,)
    assert sent[0].from_addr == w.ids["@qa"].address
    assert sent[0].in_reply_to is None
    assert is_bug_report(sent[0], BUGS)
    assert parse_body(sent[0].body).kind is None
    assert "Gud-Sig" not in sent[0].body
    assert "Steps: Open pricing.html" in sent[0].body
    assert "Expected: A table with Free, Pro $8/mo, and Team $15/user/mo" in sent[0].body
    assert "Actual: Three div cards" in sent[0].body
    assert "Page: pricing.html" in sent[0].body
    assert f"Found by @qa on T1 at tip {tip.id}, URL http://qa.test/" in sent[0].body
    assert f"QA-Checked: T1 {tip.id}" in sent[0].body
    assert "comparison table" in browser.calls[0]["task"]
    assert "Build the site" in browser.calls[0]["task"]
    assert browser.calls[0]["pages"] == ["index.html", "pricing.html"]
    assert site.label
    assert site.stopped == 1

    assert qa.step() == []
    assert len(browser.calls) == 1
    assert len(_bugs(w)) == 1

    again = _qa(w, FakeBrowser([FINDING]))
    assert again.step() == []
    assert again.browser.calls == []
    assert len(_bugs(w)) == 1

    reviewer = Approve()
    lead = Lead(
        w.mailbox, w.ids["@lead"], 60, ScriptedPlanner({}), clock=w.clock, pool=["@alice"],
        bugs_address=BUGS, bug_reviewer=reviewer,
    )
    lead.step()
    tasks = [email for email in w.repo_mail() if parse_body(email.body).get("Gud-Bug") == sent[0].message_id]
    assert len(tasks) == 1
    assert parse_body(tasks[0].body).kind == "task"
    assert "Open pricing.html" in parse_body(tasks[0].body).text
    assert reviewer.subjects == ["Pricing has no comparison table"]


def test_agent_error_sends_nothing_and_retries_once_after_a_pause(tmp_path):
    w = _world(tmp_path)
    tip = w.genesis({"index.html": "<h1>Home</h1>\n"})
    _done(w, tip)
    site = FakeSite()
    browser = FakeBrowser(error=RuntimeError("browser down"))
    qa = _qa(w, browser, site)

    assert qa.step() == []
    assert _bugs(w) == []
    assert site.stopped == 1
    assert qa.step() == []
    assert len(browser.calls) == 1

    w.tick(31)
    assert qa.step() == []
    assert len(browser.calls) == 2
    w.tick(31)
    assert qa.step() == []
    assert len(browser.calls) == 2
    assert site.served == 2
    assert _bugs(w) == []


def test_findings_are_capped(tmp_path):
    w = _world(tmp_path)
    tip = w.genesis({"index.html": "<h1>Home</h1>\n"})
    _done(w, tip)
    findings = [{**FINDING, "title": f"Defect {i}"} for i in range(6)]
    qa = _qa(w, FakeBrowser(findings))
    sent = qa.step()
    assert [email.subject for email in sent] == [f"Defect {i}" for i in range(5)]
    assert qa.step() == []
    assert len(_bugs(w)) == 5


def test_local_site_serves_index():
    site = LocalSite()
    url = site.serve({"index.html": "<h1>hello qa</h1>\n", "pricing.html": "<table></table>\n"})
    try:
        with urllib.request.urlopen(url + "index.html", timeout=2) as resp:
            assert resp.status == 200
            assert b"hello qa" in resp.read()
        with urllib.request.urlopen(url + "pricing.html", timeout=2) as resp:
            assert b"<table>" in resp.read()
    finally:
        site.stop()
    try:
        urllib.request.urlopen(url + "index.html", timeout=1)
    except (urllib.error.URLError, OSError):
        return
    raise AssertionError("server still accepted a connection after stop")


def test_default_runner_is_instabox(tmp_path, monkeypatch):
    monkeypatch.delenv("GUD_QA_RUNNER", raising=False)
    assert _runner_kind() == "instabox"
    w = _world(tmp_path)
    qa = QA(w.mailbox, w.ids["@qa"], 60, clock=w.clock)
    try:
        from gud.qa import InstaBoxSite
        assert isinstance(qa.runner, InstaBoxSite)
        assert qa.browser is qa.runner.browser
    finally:
        qa.close()


def test_agent_stdout_parses_the_last_json_line():
    text = "\n".join([
        "browser started",
        '{"noise": true}',
        '{"findings": [{"title": "No table", "steps": "open pricing", "expected": "table", "actual": "divs", "page": "pricing.html"}], '
        '"usage": {"total_cost": 0.01, "total_tokens": 12}}',
    ])
    findings, usage = _parse_agent_output(text)
    assert findings[0]["title"] == "No table"
    assert usage["total_cost"] == 0.01
    assert "sk-" not in __import__("gud.qa", fromlist=["_AGENT_PY"])._AGENT_PY
