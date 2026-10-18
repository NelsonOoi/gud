"""QA mails plain bug reports after task-done. The browser and the site are fakes."""

import urllib.error
import urllib.request

from gud import compose
from gud.bugs import is_bug_report
from gud.lead import Lead, ScriptedPlanner
from gud.qa import (
    QA, LocalSite, _AGENT_PY, _OUTLINE_CHARS, _agent_envs, _outline_page, _parse_agent_output,
    _prompt, _runner_kind, _structure_outline, _vision_enabled,
)
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

    def review_bug(self, subject, text, paths, files=None):
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


def test_a_tip_already_browsed_under_another_task_is_not_browsed_again(tmp_path):
    w = _world(tmp_path)
    tip = w.genesis({"index.html": "<h1>Home</h1>\n"})
    _done(w, tip)
    browser = FakeBrowser([FINDING])
    qa = _qa(w, browser)
    assert len(qa.step()) == 1

    done = compose.task_done_body(w.ids["@lead"], "T2", tip.id, "no change needed")
    compose.send(w.mailbox, w.ids["@lead"], w.members, "[gud] T2 done: Fix it", done)
    assert qa.step() == []
    assert len(browser.calls) == 1
    assert len(_bugs(w)) == 1


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
    assert "sk-" not in _AGENT_PY
    assert "GUD_QA_VISION" in _AGENT_PY
    assert "use_vision=use_vision" in _AGENT_PY
    compile(_AGENT_PY, "<qa-agent>", "exec")


_CARDS = """
<!doctype html>
<html><head>
  <title>Pricing</title>
  <link rel="stylesheet" href="styles.css">
</head><body>
  <h1>Pricing Plans</h1>
  <div class="pricing-table">
    <div class="plan"><h2>Free</h2><ul><li>A</li><li>B</li></ul></div>
    <div class="plan"><h2>Pro</h2><ul><li>A</li><li>B</li></ul></div>
    <div class="plan"><h2>Team</h2><ul><li>A</li><li>B</li></ul></div>
  </div>
  <a href="index.html">Home</a>
  <img src="images/logo.png" alt="Acme">
  <form action="/signup"><input type="email"><button type="submit">Go</button></form>
</body></html>
"""


def test_outline_reports_repeated_groups_and_says_when_there_is_no_table():
    line = _outline_page(_CARDS)
    assert "tables: none" in line
    assert "3x div.plan" in line
    assert 'h1 "Pricing Plans"' in line
    assert "h2 \"Free\"" in line
    assert "3x ul (2 items)" in line
    assert "form (2 fields)" in line
    assert "index.html" in line
    assert "logo.png" in line
    assert "styles.css" not in line
    assert "doctype" not in line.lower()
    assert len(line) <= _OUTLINE_CHARS


def test_outline_counts_table_shape_and_ignores_script_and_head():
    html = """
    <head><title>Hidden</title><link rel="stylesheet" href="styles.css"></head>
    <body>
      <script>var t = '<table><tr><td>nope</td></tr></table><div class="plan">';</script>
      <table>
        <tr><th>A</th><th colspan="2">B</th></tr>
        <tr><td>1</td><td>2</td><td>3</td></tr>
      </table>
      <table>
        <tr><td>only</td><td>
          <table><tr><td>i</td><td>j</td><td>k</td></tr></table>
        </td></tr>
      </table>
      <dl><dt>One</dt><dd>Detail</dd></dl>
    </body>
    """
    line = _outline_page(html)
    assert "tables: none" not in line
    assert "2 rows x 3 cols" in line
    assert "1 row x 2 cols" in line
    assert "1 row x 3 cols" in line
    assert "div.plan" not in line
    assert "styles.css" not in line
    assert 'h1 "Hidden"' not in line
    assert "dl (1 item)" in line


def test_structure_outline_is_one_capped_line_per_html_page():
    tree = {
        "pricing.html": '<div class="plan"></div>' * 3,
        "index.html": "<h1>Home</h1><ul><li>a</li><li>b<ul><li>c</li></ul></li></ul>",
        "styles.css": "div.plan { }",
        "readme.md": "<table><tr><td>x</td></tr></table>",
    }
    text = _structure_outline(tree)
    lines = text.splitlines()
    assert lines[0].startswith("- index.html:")
    assert any(line.startswith("- pricing.html:") and "3x div.plan" in line and "tables: none" in line
               for line in lines)
    assert "styles.css" not in text
    assert "readme.md" not in text
    assert "ul (2 items)" in text
    assert "ul (1 item)" in text
    assert _structure_outline({}) == "(no HTML pages)"
    assert _structure_outline({"a.txt": "<table></table>"}) == "(no HTML pages)"
    hrefs = "".join(f'<a href="{"p" * 70}{i}.html">x</a>' for i in range(20))
    long = _outline_page(f"<body><h1>{'H' * 80}</h1>{hrefs}<img src=\"data:image/png;base64,aaaa\"></body>")
    assert len(long) <= _OUTLINE_CHARS
    assert "tables:" in long
    fitted = _outline_page('<body><a href="a.html">A</a><img src="logo.png"></body>')
    assert "a.html" in fitted
    assert "logo.png" in fitted
    assert _outline_page("<div") != ""


def test_prompt_includes_the_source_outline(tmp_path):
    structure = "- pricing.html: tables: none; repeated: 3x div.plan"
    text = _prompt(
        "Build the site", "Use a comparison table.", ["pricing.html"], "http://qa.test/", structure,
    )
    assert "Page structure (from source):\n" + structure in text
    assert "You may use the page structure outline to check structural requirements." in text
    assert "if that line shows none, report the mismatch" in text
    assert "not a table unless the outline shows a table" in text
    assert "do not report it missing and do not require a button" in text
    assert "that page's outline shows none" in text
    assert "requirement written in the task" in text
    assert "placeholder copy" in text
    assert "Requirements about <head> markup are covered by tests." in text
    assert "doctype" in text

    w = _world(tmp_path)
    html = "<h1>Plans</h1>" + '<div class="plan">x</div>' * 3
    tip = w.genesis({"pricing.html": html, "notes.txt": "nope"})
    _done(w, tip, "three plans in a comparison table")
    browser = FakeBrowser([])
    assert _qa(w, browser).step() == []
    task = browser.calls[0]["task"]
    assert "Page structure (from source):" in task
    assert "- pricing.html:" in task
    assert "3x div.plan" in task
    assert "tables: none" in task
    outline = task.split("Page structure (from source):\n", 1)[1].split("\n\nCheck every", 1)[0]
    assert "notes.txt" not in outline
    assert "three plans in a comparison table" in task


def test_vision_defaults_on_and_is_passed_to_the_box(monkeypatch):
    monkeypatch.delenv("GUD_QA_VISION", raising=False)
    assert _vision_enabled() is True
    assert _agent_envs("k", "m", 8, "http://x", "task")["GUD_QA_VISION"] == "1"
    for off in ("0", "off", "false", "no"):
        monkeypatch.setenv("GUD_QA_VISION", off)
        assert _vision_enabled() is False
        assert _agent_envs("k", "m", 8, "http://x", "task")["GUD_QA_VISION"] == "0"
    monkeypatch.setenv("GUD_QA_VISION", "on")
    assert _vision_enabled() is True
