"""Browser QA. After a task-done, serve that tip and mail each defect to bugs@.

`GUD_QA_RUNNER=instabox` (the default) runs browser-use in one reused InstaBox
(`gud-qa`, template `agent-browser`). `GUD_QA_RUNNER=local` serves the tree on
this machine and drives a headless browser-use agent here.

A report is plain mail (no signature, not a reply) so bugs.is_bug_report accepts it.
`QA-Checked: <task> <tip>` in the body is how a restarted process skips a tip it
already reported. A tip with no findings is remembered only in this process.

The public port stays published until the next check replaces the site, or until
`close` (swarm shutdown). `GUD_QA_HOLD=0` unpublishes and stops http.server as
soon as the agent returns. The box itself is kept.

The task prompt includes a short per-page structure outline taken from the HTML
source (`Page structure (from source)`). `GUD_QA_VISION` defaults on; set it to
`0` to send the browser agent no screenshots.
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import json
import os
import re
import shlex
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from .bugs import bugs_address_for
from .build import InstaBoxError, InstaBoxRunner, _materialize
from .inbox import Gate, Inbox, next_window_close
from .replay import replay
from .tasks import derive
from .types import Email, Tree
from .wire import TRAILER_RE, parse_body

_MAX_FINDINGS = 5
_ATTEMPTS = 2
_RETRY_AFTER = 30.0
_BOX_NAME = "gud-qa"
_BROWSER_USE = "0.13.11"
_VENV = "/data/home/qa-venv"
_SITE_ROOT = "/data/home/site"
_CHECKED = re.compile(r"(?m)^QA-Checked:[ \t]+(\S+)[ \t]+(\S+)[ \t]*$")
_OPENROUTER = "https://openrouter.ai/api/v1"
_OUTLINE_CHARS = 480
_OUTLINE_HEADINGS = 6
_OUTLINE_LINKS = 8
_OUTLINE_IMAGES = 4
_OUTLINE_GROUPS = 6
_OUTLINE_LISTS = 4
_OUTLINE_TABLES = 4
_OUTLINE_FORMS = 3
_HEADING_CHARS = 48
_IGNORE_TAGS = frozenset({"head", "script", "style", "noscript", "template", "svg"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_LISTS = frozenset({"ul", "ol", "dl"})
_VISION_OFF = frozenset({"0", "off", "false", "no"})
# Drop cheaper lines first so a long page still keeps tables and repeated groups.
_OUTLINE_DROP = ("images", "links", "forms", "lists", "headings", "repeated", "tables")

# Runs inside the box. The OpenRouter key arrives as an exec env var, never in this text.
_AGENT_PY = r'''
import asyncio, base64, glob, json, logging, os, shutil, sys, time

def chromium():
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    for pattern in ("/usr/bin/chromium*", "/usr/lib/chromium/chromium", "/snap/bin/chromium"):
        for path in sorted(glob.glob(pattern)):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None

def main():
    url = os.environ["GUD_QA_URL"]
    task = base64.b64decode(os.environ["GUD_QA_TASK_B64"]).decode()
    model = os.environ.get("GUD_QA_MODEL") or "google/gemini-2.5-flash"
    key = os.environ.get("OPENROUTER_API_KEY") or ""
    steps = max(1, int(os.environ.get("GUD_QA_MAX_STEPS") or "8"))
    vision = (os.environ.get("GUD_QA_VISION") or "1").strip().lower()
    use_vision = vision not in ("0", "off", "false", "no")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    binary = chromium()
    if not binary:
        raise RuntimeError("chromium not found on this box")
    logging.getLogger("browser_use").setLevel(logging.WARNING)
    from pydantic import BaseModel, Field
    from browser_use import Agent, Browser, ChatOpenRouter

    class Finding(BaseModel):
        title: str = Field(description="Short defect summary")
        steps: str = Field(description="Steps a person can follow")
        expected: str
        actual: str
        page: str = Field(description="Path such as pricing.html")

    class Report(BaseModel):
        findings: list[Finding] = Field(default_factory=list)

    llm = ChatOpenRouter(
        model=model, api_key=key, base_url="https://openrouter.ai/api/v1",
        temperature=0, max_retries=1, timeout=60,
        extra_body={"reasoning": {"effort": "none"}},
    )
    browser = Browser(
        headless=True, executable_path=binary, chromium_sandbox=False,
        enable_default_extensions=False, user_data_dir="/tmp/gud-qa-chrome",
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    agent = Agent(
        task=task, llm=llm, browser=browser, output_model_schema=Report,
        use_vision=use_vision, flash_mode=True, use_judge=False, calculate_cost=True,
        enable_planning=False, enable_signal_handler=False, max_failures=2,
        max_actions_per_step=4, step_timeout=50, llm_timeout=45,
    )
    started = time.monotonic()
    real_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        history = asyncio.run(agent.run(max_steps=steps))
    finally:
        sys.stdout = real_out
    if history is None:
        raise RuntimeError("qa browser agent did not finish")
    parsed = None
    try:
        parsed = history.structured_output
    except Exception:
        parsed = None
    if parsed is None:
        raw = (history.final_result() or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = Report.model_validate_json(raw[start:end + 1])
            except Exception:
                parsed = None
    if parsed is None:
        if not history.is_done():
            raise RuntimeError("qa browser agent did not finish")
        raise RuntimeError("qa browser agent returned no findings list")
    usage = None
    raw = getattr(history, "usage", None)
    if raw is not None:
        usage = {
            "prompt_tokens": int(raw.total_prompt_tokens),
            "completion_tokens": int(raw.total_completion_tokens),
            "total_tokens": int(raw.total_tokens),
            "total_cost": float(raw.total_cost),
        }
    payload = {
        "findings": [item.model_dump() for item in parsed.findings],
        "usage": usage,
        "seconds": round(time.monotonic() - started, 3),
    }
    real_out.write(json.dumps(payload) + "\n")
    real_out.flush()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stderr.write("qa agent failed: %s\n" % exc)
        raise SystemExit(1)
'''


def _runner_kind() -> str:
    return (os.environ.get("GUD_QA_RUNNER") or "instabox").strip().lower()


def _html_pages(tree: Tree) -> list[str]:
    return [path for path in sorted(tree) if path.lower().endswith((".html", ".htm"))]


def _vision_enabled() -> bool:
    raw = (os.environ.get("GUD_QA_VISION") or "1").strip().lower()
    return raw not in _VISION_OFF


def _agent_envs(openrouter_key: str, model: str, max_steps: int, url: str, task: str) -> dict[str, str]:
    return {
        "OPENROUTER_API_KEY": openrouter_key,
        "GUD_QA_MODEL": model,
        "GUD_QA_MAX_STEPS": str(max_steps),
        "GUD_QA_URL": url,
        "GUD_QA_TASK_B64": base64.b64encode(task.encode()).decode(),
        "GUD_QA_VISION": "1" if _vision_enabled() else "0",
        "BROWSER_USE_SETUP_LOGGING": "false",
    }


def _clip_text(text: str, limit: int) -> str:
    text = " ".join((text or "").split()).replace('"', "'")
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _colspan(raw: str) -> int:
    try:
        return min(20, max(1, int(raw)))
    except (TypeError, ValueError):
        return 1


def _image_label(attrs: dict) -> str:
    src = (attrs.get("src") or "").strip()
    if src.startswith("data:"):
        return "data-image"
    if src:
        name = src.split("?", 1)[0].rstrip("/").split("/")[-1]
        return _clip_text(name or src, 60)
    alt = (attrs.get("alt") or "").strip()
    return _clip_text(alt, 40) if alt else "img"


class _PageOutline(HTMLParser):
    """Structure of one HTML page. Head, script, style, and svg children are skipped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._ignore = 0
        self._heading: tuple[str, list[str]] | None = None
        self._table_stack: list[dict] = []
        self._list_stack: list[dict] = []
        self._form_stack: list[dict] = []
        self.headings: list[tuple[str, str]] = []
        self.tables: list[dict] = []
        self.lists: list[dict] = []
        self.forms: list[dict] = []
        self.links: list[str] = []
        self.images: list[str] = []
        self.groups: Counter = Counter()

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        attrs = {str(key).lower(): ("" if value is None else str(value)) for key, value in attrs}
        if tag in _IGNORE_TAGS:
            self._ignore += 1
            if tag == "svg" and self._ignore == 1:
                self.images.append("svg")
            return
        if self._ignore:
            return
        if tag in _HEADINGS:
            self._finish_heading()
            self._heading = (tag, [])
        elif tag == "table":
            rec = {"rows": 0, "max_cols": 0, "cells": 0, "in_row": False}
            self.tables.append(rec)
            self._table_stack.append(rec)
        elif tag == "tr" and self._table_stack:
            rec = self._table_stack[-1]
            if rec["in_row"]:
                rec["max_cols"] = max(rec["max_cols"], rec["cells"])
            rec["rows"] += 1
            rec["cells"] = 0
            rec["in_row"] = True
        elif tag in ("td", "th") and self._table_stack and self._table_stack[-1]["in_row"]:
            self._table_stack[-1]["cells"] += _colspan(attrs.get("colspan"))
        elif tag in _LISTS:
            rec = {"tag": tag, "items": 0}
            self.lists.append(rec)
            self._list_stack.append(rec)
        elif tag == "li" and self._list_stack and self._list_stack[-1]["tag"] in ("ul", "ol"):
            self._list_stack[-1]["items"] += 1
        elif tag == "dt" and self._list_stack and self._list_stack[-1]["tag"] == "dl":
            self._list_stack[-1]["items"] += 1
        elif tag == "form":
            rec = {"fields": 0}
            self.forms.append(rec)
            self._form_stack.append(rec)
        elif tag in ("input", "select", "textarea", "button") and self._form_stack:
            self._form_stack[-1]["fields"] += 1
        elif tag == "a":
            href = _clip_text(attrs.get("href", "").strip(), 80)
            if href:
                self.links.append(href)
        elif tag == "img":
            self.images.append(_image_label(attrs))
        seen: set[str] = set()
        for token in attrs.get("class", "").split():
            if not token or token in seen or len(token) > 40:
                continue
            seen.add(token)
            self.groups[f"{tag}.{token}"] += 1

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in _IGNORE_TAGS and self._ignore:
            self._ignore -= 1
            return
        if self._ignore:
            return
        if tag in _HEADINGS:
            self._finish_heading(tag)
        elif tag == "tr" and self._table_stack and self._table_stack[-1]["in_row"]:
            rec = self._table_stack[-1]
            rec["max_cols"] = max(rec["max_cols"], rec["cells"])
            rec["in_row"] = False
            rec["cells"] = 0
        elif tag == "table" and self._table_stack:
            rec = self._table_stack.pop()
            if rec["in_row"]:
                rec["max_cols"] = max(rec["max_cols"], rec["cells"])
                rec["in_row"] = False
        elif tag in _LISTS and self._list_stack and self._list_stack[-1]["tag"] == tag:
            self._list_stack.pop()
        elif tag == "form" and self._form_stack:
            self._form_stack.pop()

    def handle_data(self, data):
        if self._ignore or self._heading is None or not data:
            return
        self._heading[1].append(data)

    def close(self):
        super().close()
        self._finish_heading()
        while self._table_stack:
            rec = self._table_stack.pop()
            if rec["in_row"]:
                rec["max_cols"] = max(rec["max_cols"], rec["cells"])

    def _finish_heading(self, tag: str | None = None):
        if self._heading is None:
            return
        level, parts = self._heading
        if tag is not None and tag != level:
            return
        self.headings.append((level, _clip_text("".join(parts), _HEADING_CHARS)))
        self._heading = None


def _counted(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _format_outline(page: _PageOutline) -> str:
    parts: list[str] = []
    if page.headings:
        shown = page.headings[:_OUTLINE_HEADINGS]
        bits = [f'{level} "{text}"' if text else level for level, text in shown]
        extra = len(page.headings) - len(shown)
        line = ", ".join(bits)
        if extra:
            line += f", +{extra}"
        parts.append("headings: " + line)
    else:
        parts.append("headings: none")
    if page.tables:
        shown = page.tables[:_OUTLINE_TABLES]
        bits = [
            f"{_counted(rec['rows'], 'row', 'rows')} x {_counted(rec['max_cols'], 'col', 'cols')}"
            for rec in shown
        ]
        extra = len(page.tables) - len(shown)
        line = ", ".join(bits)
        if extra:
            line += f", +{extra}"
        parts.append("tables: " + line)
    else:
        parts.append("tables: none")
    if page.lists:
        counts = Counter((rec["tag"], rec["items"]) for rec in page.lists)
        bits = []
        seen: set[tuple[str, int]] = set()
        hidden = 0
        for rec in page.lists:
            key = (rec["tag"], rec["items"])
            if key in seen:
                continue
            seen.add(key)
            if len(bits) >= _OUTLINE_LISTS:
                hidden += counts[key]
                continue
            label = f"{rec['tag']} ({_counted(rec['items'], 'item', 'items')})"
            bits.append(f"{counts[key]}x {label}" if counts[key] > 1 else label)
        line = ", ".join(bits)
        if hidden:
            line += f", +{hidden}"
        parts.append("lists: " + line)
    else:
        parts.append("lists: none")
    if page.forms:
        shown = page.forms[:_OUTLINE_FORMS]
        bits = [f"form ({_counted(rec['fields'], 'field', 'fields')})" for rec in shown]
        extra = len(page.forms) - len(shown)
        line = ", ".join(bits)
        if extra:
            line += f", +{extra}"
        parts.append("forms: " + line)
    else:
        parts.append("forms: none")
    if page.links:
        uniq: list[str] = []
        for href in page.links:
            if href not in uniq:
                uniq.append(href)
        shown = uniq[:_OUTLINE_LINKS]
        extra = len(uniq) - len(shown)
        line = ", ".join(shown)
        if extra:
            line += f", +{extra}"
        parts.append("links: " + line)
    else:
        parts.append("links: none")
    if page.images:
        shown = page.images[:_OUTLINE_IMAGES]
        extra = len(page.images) - len(shown)
        line = ", ".join(shown)
        if extra:
            line += f", +{extra}"
        parts.append("images: " + line)
    else:
        parts.append("images: none")
    groups = sorted(
        ((name, count) for name, count in page.groups.items() if count >= 2),
        key=lambda item: (-item[1], item[0]),
    )
    if groups:
        shown = groups[:_OUTLINE_GROUPS]
        extra = len(groups) - len(shown)
        line = ", ".join(f"{count}x {name}" for name, count in shown)
        if extra:
            line += f", +{extra}"
        parts.append("repeated: " + line)
    return _cap_outline(parts)


def _cap_outline(parts: list[str]) -> str:
    kept = list(parts)

    def kind(part: str) -> str:
        return part.split(":", 1)[0]

    for drop in _OUTLINE_DROP:
        if len("; ".join(kept)) <= _OUTLINE_CHARS:
            break
        kept = [part for part in kept if kind(part) != drop]
    line = "; ".join(kept)
    if len(line) > _OUTLINE_CHARS:
        return line[: _OUTLINE_CHARS - 1].rstrip() + "…"
    return line


def _outline_page(html: str) -> str:
    page = _PageOutline()
    try:
        page.feed(html or "")
        page.close()
    except Exception:
        return "unparsed"
    return _format_outline(page)


def _structure_outline(tree: Tree) -> str:
    """One capped line per HTML page: headings, tables, lists, forms, links, images, repeats."""
    pages = _html_pages(tree)
    if not pages:
        return "(no HTML pages)"
    return "\n".join(f"- {path}: {_outline_page(tree.get(path) or '')}" for path in pages)


def _chromium() -> str | None:
    """Newest local Chromium. PLAYWRIGHT_BROWSERS_PATH is often an empty sandbox cache."""
    import glob

    explicit = os.environ.get("GUD_QA_BROWSER")
    if explicit and Path(explicit).is_file():
        return explicit
    roots = [
        Path.home() / "Library/Caches/ms-playwright",
        Path.home() / ".cache/ms-playwright",
    ]
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        roots.insert(0, Path(override))
    matches: list[str] = []
    for root in roots:
        matches.extend(glob.glob(str(root / "chromium-*" / "chrome-mac*" / "Chromium.app/Contents/MacOS/Chromium")))
        matches.extend(glob.glob(str(root / "chromium-*" / "chrome-linux*" / "chrome")))
        matches.extend(glob.glob(str(root / "chromium-*" / "chrome-win" / "chrome.exe")))
    found = sorted(path for path in matches if Path(path).is_file())
    return found[-1] if found else None


def _untag(text: str) -> str:
    """A `Gud-` trailer line in the body makes bugs.is_bug_report reject the mail."""
    lines = []
    for line in text.splitlines():
        if TRAILER_RE.match(line.strip()):
            line = line.replace(":", " :", 1)
        lines.append(line)
    return "\n".join(lines).strip()


def _field(text: str) -> str:
    kept = [line for line in str(text or "").splitlines() if not line.strip().startswith("QA-Checked:")]
    return _untag("\n".join(kept))


def _prompt(title: str, description: str, pages: list[str], url: str, structure: str = "") -> str:
    listed = "\n".join(f"- {page}" for page in pages) or "- (none)"
    outline = ""
    if structure.strip():
        outline = f"Page structure (from source):\n{structure.strip()}\n\n"
    return (
        f"The site is at {url}\n"
        f"Task: {title}\n{description.strip()}\n\n"
        f"HTML pages:\n{listed}\n\n"
        f"{outline}"
        "Check every requirement in the task that a visitor can see on the rendered pages: "
        "content, prices, links, layout, and whether something is laid out as the task names it "
        "(a table has rows and columns, a list has items). "
        "You may use the page structure outline to check structural requirements. "
        "When the task requires a table, list, or form, the outline is the source structure: "
        "if that line shows none, report the mismatch. "
        "A repeated group of elements is not a table unless the outline shows a table. "
        "If the outline lists a link target the task names, that link is present; do not report it missing and do not require a button. "
        "Before you return, include a finding for every page where the task requires a table, list, or form and that page's outline shows none. "
        "Report a defect only when a requirement written in the task is missing or wrong on the rendered page. "
        "Do not report placeholder copy, wording, or polish the task does not require. "
        "Click the nav links and open every HTML page. "
        "Requirements about <head> markup are covered by tests. "
        "Do not report markup the browser does not display, such as the doctype, meta tags, "
        "or the <title> and stylesheet links in <head>. "
        "Report only concrete, reproducible defects. "
        "If the site meets the task, return an empty findings list."
    )


def _finding(item) -> dict[str, str] | None:
    if isinstance(item, dict):
        data = item
    else:
        data = {name: getattr(item, name, "") for name in ("title", "steps", "expected", "actual", "page")}
    title = " ".join(str(data.get("title") or "").split())
    if not title:
        return None
    return {
        "title": title[:140],
        "steps": _field(data.get("steps") or ""),
        "expected": _field(data.get("expected") or ""),
        "actual": _field(data.get("actual") or ""),
        "page": " ".join(str(data.get("page") or "").split())[:200],
    }


def _normalize(items) -> list[dict[str, str]]:
    found = []
    for item in items or []:
        row = _finding(item)
        if row is not None:
            found.append(row)
    return found


def _parse_agent_output(text: str) -> tuple[list, dict | None]:
    """The agent script's last stdout line is a JSON object."""
    line = ""
    for raw in reversed((text or "").splitlines()):
        raw = raw.strip()
        if raw.startswith("{") and raw.endswith("}"):
            line = raw
            break
    if not line:
        raise RuntimeError("qa browser agent returned no findings list")
    data = json.loads(line)
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:300])
    usage = data.get("usage")
    return list(data.get("findings") or []), usage if isinstance(usage, dict) else None


def _keep_url() -> bool:
    raw = (os.environ.get("GUD_QA_HOLD") or "keep").strip().lower()
    return raw not in {"0", "off", "stop", "unpublish"}


class LocalSite:
    """Serve a tree with `http.server` on 127.0.0.1 and a free port."""

    def __init__(self):
        self._httpd = None
        self._thread: threading.Thread | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None

    def serve(self, tree: Tree) -> str:
        self.stop()
        tmp = tempfile.TemporaryDirectory(prefix="gud-qa-")
        try:
            root = Path(tmp.name)
            _materialize(root, tree)
            handler = _handler(root)
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, name="gud-qa-http", daemon=True)
            thread.start()
        except Exception:
            tmp.cleanup()
            raise
        self._tmp = tmp
        self._httpd = httpd
        self._thread = thread
        host, port = httpd.server_address
        url = f"http://{host}:{port}/"
        _wait_http(url)
        return url

    def stop(self) -> None:
        httpd, thread, tmp = self._httpd, self._thread, self._tmp
        self._httpd = self._thread = self._tmp = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if tmp is not None:
            tmp.cleanup()

    def close(self) -> None:
        self.stop()


def _handler(root: Path):
    directory = str(root)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, format, *args):
            return

    return Handler


def _wait_http(url: str) -> None:
    deadline = time.monotonic() + 5
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                resp.read(64)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(0.02)
    raise RuntimeError(f"qa server did not start: {last}")


class _QABox(InstaBoxRunner):
    """Tagged `gud=qa` so the tester's warm pool (`gud=runner`) does not take the box."""

    def _meta(self) -> dict[str, str]:
        return {"gud": "qa", "gud-image": self.image or "-", "gud-snapshot": "-"}


class InstaBoxBrowser:
    """browser-use inside the box, against the site on localhost."""

    def __init__(self, site: "InstaBoxSite"):
        self.site = site
        self.last_usage = None
        self.seconds = 0.0

    def run(self, url: str, task: str, pages: list[str]) -> list[dict]:
        return self.site.run_agent(url, task)


class InstaBoxSite:
    """One `gud-qa` box from `agent-browser`. Reused across checks; resumed if paused."""

    def __init__(self, api_key: str | None = None, template: str = "agent-browser", port: int = 8080, *,
                 openrouter_key: str = "", model: str = "google/gemini-2.5-flash", max_steps: int = 8):
        self.port = port
        self.template = template
        self.openrouter_key = openrouter_key or ""
        self.model = model
        self.max_steps = max_steps
        self.label = "tip"
        self.public_url = ""
        self.agent_url = f"http://127.0.0.1:{port}/"
        self.setup_seconds = 0.0
        self.prep_seconds = 0.0
        self.agent_seconds = 0.0
        self._keep = _keep_url()
        self._box = _QABox(api_key=api_key, image=template, after="destroy", max_machines=1)
        self._mid: str | None = None
        self._counted_setup = False
        self._venv_ready = False
        self.browser = InstaBoxBrowser(self)

    def set_label(self, label: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9._-]", "", label or "")[:48]
        self.label = safe or "tip"

    def serve(self, tree: Tree) -> str:
        first = not self._counted_setup
        started = time.monotonic()
        mid = self._ensure()
        if first:
            self.setup_seconds = time.monotonic() - started
            self._counted_setup = True
            started = time.monotonic()
        try:
            self._publish_tree(mid, tree)
            self.prep_seconds = time.monotonic() - started
            return self.public_url
        except Exception:
            self._silence()
            raise

    def run_agent(self, url: str, task: str) -> list[dict]:
        if not self.openrouter_key.strip():
            raise RuntimeError("OPENROUTER_KEY is not set")
        if not self._mid:
            raise RuntimeError("qa site is not serving")
        which = self._exec(
            self._mid,
            "command -v chromium || command -v chromium-browser || ls /usr/bin/chrom* || true",
            timeout=20,
        )
        print("qa: chromium " + ((which.get("stdout") or "").strip().splitlines() or ["missing"])[0][:160], flush=True)
        print(f"qa: vision {'on' if _vision_enabled() else 'off'}", flush=True)
        print("qa: browser-use starting", flush=True)
        started = time.monotonic()
        try:
            findings, usage = self._run_script(self._mid, url, task)
        finally:
            self.agent_seconds = time.monotonic() - started
            self.browser.seconds = self.agent_seconds
        self.browser.last_usage = usage
        cost = None if not usage else usage.get("total_cost")
        tokens = None if not usage else usage.get("total_tokens")
        print(f"qa: usage tokens={tokens} cost={cost} ({self.agent_seconds:.1f}s)", flush=True)
        return findings

    def stop(self) -> None:
        """After the agent. The default leaves the public URL up for the bug report."""
        if not self._keep:
            self._silence()

    def close(self) -> None:
        """Stop http.server and unpublish. The box stays for the next run."""
        self._silence()
        client = self._box._client
        if client is not None:
            client.close()
            self._box._client = None

    def _ensure(self) -> str:
        if self._mid is None:
            found = self._find()
            if found is None:
                print("qa: creating gud-qa", flush=True)
                self._mid = self._create()
            else:
                print(f"qa: reusing gud-qa ({found.get('state')})", flush=True)
                self._mid = found["id"]
        self._box._ensure_running(self._mid)
        self._tune(self._mid)
        self._install(self._mid)
        return self._mid

    def _find(self) -> dict | None:
        cursor = None
        while True:
            params: list[tuple[str, str]] = [("limit", "100")]
            if cursor:
                params.append(("cursor", cursor))
            page = self._box._request("GET", "/v1/machines", params=params)
            for machine in page.get("machines") or []:
                if machine.get("name") == _BOX_NAME and machine.get("state") not in {"deleted", "deleting"}:
                    return machine
            cursor = page.get("nextCursor")
            if not cursor:
                return None

    def _create(self) -> str:
        op = self._box._request(
            "POST", "/v1/machines", idempotent=True,
            json_body={
                "name": _BOX_NAME,
                "templateId": self.template,
                "metadata": {"gud": "qa"},
                "autoSuspendAfterSeconds": 900,
                "timeoutSeconds": 86400,
                "onTimeout": "pause",
                "vault": False,
                "desiredState": "running",
            },
        )
        mid = op.get("machineId")
        done = self._box._wait_op(op["id"], timeout=240)
        mid = done.get("machineId") or mid
        if not mid:
            raise InstaBoxError(500, "create_failed", "create operation returned no machine id")
        self._box._ensure_running(mid)
        return mid

    def _tune(self, mid: str) -> None:
        try:
            self._box._request(
                "PATCH", f"/v1/machines/{mid}",
                json_body={"autoSuspendAfterSeconds": 900, "timeoutSeconds": 86400},
            )
        except InstaBoxError:
            return

    def _install(self, mid: str) -> None:
        # /data persists across suspend, so this is a no-op after the first boot.
        if self._venv_ready:
            return
        py = shlex.quote(_VENV + "/bin/python")
        check = self._exec(mid, f"{py} -c 'import browser_use'", timeout=60)
        if check.get("exitCode") == 0 and not check.get("timedOut"):
            self._venv_ready = True
            return
        print(f"qa: installing browser-use=={_BROWSER_USE} on gud-qa", flush=True)
        venv = shlex.quote(_VENV)
        self._checked(
            mid,
            "command -v uv >/dev/null || { echo 'uv is not installed on this box'; exit 1; }; "
            f"rm -rf {venv} && uv venv {venv} && "
            f"uv pip install --python {py} 'browser-use=={_BROWSER_USE}'",
            timeout=600,
        )
        self._venv_ready = True
        print("qa: browser-use ready", flush=True)

    def _publish_tree(self, mid: str, tree: Tree) -> None:
        dest = f"{_SITE_ROOT}/{self.label}"
        self._quiet_http(mid)
        # agent-browser exec is uid 1000; /data is root and _upload writes /data/gud-work there.
        self._checked(mid, "sudo -n chmod a+rwx /data", timeout=20)
        self._box._upload(mid, tree)
        qdest = shlex.quote(dest)
        self._checked(
            mid,
            f"rm -rf {qdest} && mkdir -p {shlex.quote(_SITE_ROOT)} && mv /data/gud-work {qdest}",
            timeout=60,
        )
        self._start_http(mid, dest)
        published = self._box._request(
            "POST", f"/v1/machines/{mid}/ports",
            json_body={"port": self.port, "name": "gud-qa"}, idempotent=True,
        )
        url = str(published.get("url") or "").strip()
        if not url:
            raise InstaBoxError(500, "no_url", "port publish returned no url")
        self.public_url = url if url.endswith("/") else url + "/"
        print(f"qa: serving {self.public_url}", flush=True)

    def _start_http(self, mid: str, dest: str) -> None:
        # setsid: exec kills its process group on timeout, which would take the server with it.
        qdest = shlex.quote(dest)
        self._checked(
            mid,
            f"cd {qdest} && setsid python3 -m http.server {self.port} --bind 0.0.0.0 "
            "</dev/null >/data/home/qa-http.log 2>&1 & echo $! > /data/home/qa-http.pid",
            timeout=30,
        )
        probe = (
            "import urllib.request, time, sys\n"
            f"url = 'http://127.0.0.1:{self.port}/'\n"
            "err = 'down'\n"
            "for _ in range(25):\n"
            "    try:\n"
            "        urllib.request.urlopen(url, timeout=2).read(32)\n"
            "        sys.exit(0)\n"
            "    except Exception as exc:\n"
            "        err = str(exc)\n"
            "        time.sleep(0.3)\n"
            "sys.exit(err)\n"
        )
        self._checked(mid, "python3 -", stdin=probe, timeout=30)

    def _run_script(self, mid: str, url: str, task: str) -> tuple[list, dict | None]:
        result = self._exec(
            mid, f"{shlex.quote(_VENV + '/bin/python')} -",
            stdin=_AGENT_PY, timeout=600, cwd="/data/home",
            envs=_agent_envs(self.openrouter_key, self.model, self.max_steps, url, task),
        )
        text = result.get("stdout") or ""
        err = result.get("stderr") or ""
        if result.get("timedOut"):
            raise RuntimeError("qa browser agent timed out")
        if result.get("exitCode") not in (0, None):
            raise RuntimeError(self._redact((err or text)[-800:]) or "qa browser agent failed")
        try:
            return _parse_agent_output(text)
        except Exception as exc:
            detail = self._redact((err or text)[-800:])
            raise RuntimeError(f"{exc}; {detail}".strip("; ")) from exc

    def _quiet_http(self, mid: str) -> None:
        self._exec(
            mid,
            "if [ -f /data/home/qa-http.pid ]; then kill \"$(cat /data/home/qa-http.pid)\" 2>/dev/null || true; "
            "rm -f /data/home/qa-http.pid; fi",
            timeout=20,
        )

    def _silence(self) -> None:
        mid = self._mid
        if not mid:
            return
        try:
            self._quiet_http(mid)
        except Exception:
            pass
        try:
            self._box._request("DELETE", f"/v1/machines/{mid}/ports/{self.port}", idempotent=True)
        except Exception:
            pass
        self.public_url = ""

    def _redact(self, text: str) -> str:
        key = self.openrouter_key
        if key and key in text:
            text = text.replace(key, "***")
        return text

    def _exec(self, mid: str, cmd: str, *, stdin: str | None = None, envs: dict | None = None,
              timeout: float = 60, cwd: str | None = None) -> dict:
        body: dict = {"cmd": cmd, "timeoutSeconds": max(1, min(600, int(timeout)))}
        if cwd:
            body["cwd"] = cwd
        if stdin is not None:
            if len(stdin) > 1_048_576:
                raise ValueError("instabox stdin exceeds 1 MiB")
            body["stdin"] = stdin
        if envs:
            body["envs"] = {str(k): str(v) for k, v in envs.items() if v is not None}
        delay = 0.5
        for attempt in range(5):
            try:
                return self._box._request(
                    "POST", f"/v1/machines/{mid}/exec", json_body=body,
                    timeout=body["timeoutSeconds"] + 30,
                )
            except InstaBoxError as exc:
                if exc.code == "machine_not_running" and attempt == 0:
                    self._box._ensure_running(mid)
                    continue
                if exc.code != "quota_exceeded" or attempt == 4:
                    raise
                time.sleep(delay)
                delay *= 2
        raise InstaBoxError(429, "quota_exceeded", "exec stayed over the concurrency cap")

    def _checked(self, mid: str, cmd: str, **kwargs) -> dict:
        result = self._exec(mid, cmd, **kwargs)
        if result.get("timedOut") or result.get("exitCode") not in (0, None):
            detail = self._redact(((result.get("stderr") or "") + (result.get("stdout") or ""))[-2000:])
            what = "timed out" if result.get("timedOut") else "failed"
            raise InstaBoxError(int(result.get("exitCode") or 1), "exec_failed",
                                f"{cmd.split(' &&')[0][:80]!r} {what}" + (f": {detail}" if detail else ""))
        return result


class BrowserUse:
    """Headless browser-use on this machine. OpenRouter via its OpenAI-compatible client."""

    def __init__(self, api_key: str, model: str, max_steps: int):
        self.api_key = api_key
        self.model = model
        self.max_steps = max_steps
        self.last_usage = None
        self.seconds = 0.0

    def run(self, url: str, task: str, pages: list[str]) -> list[dict]:
        if not (self.api_key or "").strip():
            raise RuntimeError("OPENROUTER_KEY is not set")
        started = time.monotonic()
        try:
            return asyncio.run(self._run(url, task, pages))
        finally:
            self.seconds = time.monotonic() - started

    async def _run(self, url: str, task: str, pages: list[str]) -> list[dict]:
        import logging

        from pydantic import BaseModel, Field

        from browser_use import Agent, Browser, ChatOpenRouter

        logging.getLogger("browser_use").setLevel(logging.WARNING)

        class Finding(BaseModel):
            title: str = Field(description="Short defect summary")
            steps: str = Field(description="Steps a person can follow")
            expected: str
            actual: str
            page: str = Field(description="Path such as pricing.html")

        class Report(BaseModel):
            findings: list[Finding] = Field(default_factory=list)

        llm = ChatOpenRouter(
            model=self.model, api_key=self.api_key, base_url=_OPENROUTER,
            temperature=0, max_retries=1, timeout=60,
            extra_body={"reasoning": {"effort": "none"}},
        )
        browser = Browser(
            headless=True, enable_default_extensions=False, executable_path=_chromium(),
        )
        agent = Agent(
            task=task, llm=llm, browser=browser, output_model_schema=Report,
            use_vision=_vision_enabled(), flash_mode=True, use_judge=False, calculate_cost=True,
            enable_planning=False, enable_signal_handler=False, max_failures=2,
            max_actions_per_step=4, step_timeout=90, llm_timeout=60,
            include_attributes=["class", "href", "id", "role", "type", "title"],
        )
        history = None
        try:
            history = await agent.run(max_steps=self.max_steps)
        finally:
            if history is not None:
                self.last_usage = history.usage
        if history is None or not history.is_done():
            raise RuntimeError("qa browser agent did not finish")
        try:
            parsed = history.structured_output
        except Exception as exc:
            raise RuntimeError(f"qa browser agent returned no findings list: {exc}") from exc
        if parsed is None:
            raise RuntimeError("qa browser agent returned no findings list")
        return [item.model_dump() for item in parsed.findings]


def make_site(kind: str, api_key: str | None = None, **kwargs):
    if kind == "local":
        return LocalSite()
    if kind == "instabox":
        return InstaBoxSite(api_key=api_key, **kwargs)
    raise ValueError(f"unknown qa runner {kind!r} (expected 'local' or 'instabox')")


class QA:
    def __init__(self, mailbox, identity, window: float, clock=time.time, *,
                 runner=None, browser=None, gate: Gate | None = None,
                 bugs_address: str | None = None, cfg=None, max_findings: int = _MAX_FINDINGS):
        self.mailbox = mailbox
        self.identity = identity
        self.window = window
        self.clock = clock
        self.gate = gate or Gate()
        self.max_findings = max_findings
        self.bugs_address = (
            bugs_address_for(getattr(mailbox, "repo_address", "")) if bugs_address is None
            else bugs_address.strip().lower()
        )
        self.model = os.environ.get("GUD_QA_MODEL") or "google/gemini-2.5-flash"
        try:
            self.max_steps = max(1, int(os.environ.get("GUD_QA_MAX_STEPS") or "8"))
        except ValueError:
            self.max_steps = 8
        self.api_key = (
            os.environ.get("OPENROUTER_KEY") or os.environ.get("OPENROUTER_API_KEY")
            or (getattr(cfg, "openrouter_key", None) if cfg is not None else None) or ""
        )
        if runner is None:
            kind = _runner_kind()
            box_key = getattr(cfg, "instabox_api_key", None) if cfg is not None else None
            if kind == "instabox":
                runner = InstaBoxSite(
                    api_key=box_key, openrouter_key=self.api_key, model=self.model,
                    max_steps=self.max_steps,
                )
                if browser is None:
                    browser = runner.browser
            elif kind == "local":
                runner = LocalSite()
                if browser is None:
                    browser = BrowserUse(self.api_key, self.model, self.max_steps)
            else:
                raise ValueError(f"unknown qa runner {kind!r} (expected 'local' or 'instabox')")
        self.runner = runner
        self.browser = browser
        self.inbox = Inbox(mailbox, identity.address, mailbox.repo_address)
        self._checked: set[tuple[str, str]] = set()
        self._failed: dict[tuple[str, str], tuple[int, float]] = {}
        self._logged: set[tuple[str, str]] = set()
        self._waiting = False
        self._bugs_error: str | None = None
        self._last_url = ""
        self._window_at: float | None = None

    def step(self) -> list[Email]:
        mail = self.inbox.read()
        if not self.gate.due(bool(mail), self._waiting, self._window_at):
            return []
        emails = self.mailbox.list(recipient=self.mailbox.repo_address)
        chain = replay(emails, self.window, self.clock())
        self._window_at = next_window_close(chain)
        prior = self._reported()
        if prior is None:
            self._waiting = True
            return []
        done = self._checked | prior
        sent: list[Email] = []
        waiting = False
        for email in emails:
            parsed = parse_body(email.body)
            if parsed.kind != "task-done":
                continue
            task_id = parsed.get("Gud-Task") or ""
            tip_id = parsed.get("Gud-Tip") or ""
            if not task_id or not tip_id:
                continue
            key = (task_id, tip_id)
            if key in done:
                continue
            # A task whose subtasks all signed no-change finishes on a tip already browsed.
            if any(tip == tip_id for _, tip in done):
                self._checked.add(key)
                done.add(key)
                continue
            tries, retry_at = self._failed.get(key, (0, 0.0))
            if tries >= _ATTEMPTS:
                continue
            if self.clock() < retry_at:
                waiting = True
                continue
            if chain.by_id.get(tip_id) is None:
                waiting = True
                if key not in self._logged:
                    self._logged.add(key)
                    print(f"qa: {task_id} tip {tip_id} is not a link yet", flush=True)
                continue
            try:
                batch = self._report(chain, email, task_id, tip_id)
            except Exception as exc:
                tries += 1
                self._failed[key] = (tries, self.clock() + _RETRY_AFTER)
                again = f"; retrying in {_RETRY_AFTER:.0f}s" if tries < _ATTEMPTS else "; giving up"
                print(f"qa: {task_id} {tip_id}: {exc}{again}", flush=True)
                waiting = waiting or tries < _ATTEMPTS
                continue
            self._checked.add(key)
            done.add(key)
            sent.extend(batch)
            print(f"qa: {task_id} tip {tip_id} {len(batch)} finding(s)", flush=True)
        self._waiting = waiting
        return sent

    def browse(self, tree: Tree, title: str, description: str, *, label: str = "") -> list[dict[str, str]]:
        """Serve `tree` and return findings. Does not send mail."""
        if label and hasattr(self.runner, "set_label"):
            self.runner.set_label(label)
        pages = _html_pages(tree)
        structure = _structure_outline(tree)
        url = self.runner.serve(tree)
        self._last_url = getattr(self.runner, "public_url", None) or url
        target = getattr(self.runner, "agent_url", None) or url
        try:
            raw = self._agent().run(target, _prompt(title, description, pages, target, structure), pages)
        finally:
            self.runner.stop()
        return _normalize(raw)[: self.max_findings]

    def close(self) -> None:
        closer = getattr(self.runner, "close", None)
        if closer is not None:
            closer()
        else:
            self.runner.stop()

    def _agent(self):
        if self.browser is None:
            bound = getattr(self.runner, "browser", None)
            if bound is not None:
                self.browser = bound
            else:
                raise RuntimeError(
                    "local QA runner does not launch a browser; set GUD_QA_RUNNER=instabox or pass browser="
                )
        return self.browser

    def _report(self, chain, email: Email, task_id: str, tip_id: str) -> list[Email]:
        link = chain.by_id[tip_id]
        task = next((item for item in derive(chain) if item.id == task_id), None)
        title = task.title if task and task.title else task_id
        description = task.description if task else parse_body(email.body).text
        findings = self.browse(dict(link.tree), title, description, label=tip_id)
        return [self._send(item, task_id, tip_id, self._last_url) for item in findings]

    def _send(self, finding: dict[str, str], task_id: str, tip_id: str, url: str) -> Email:
        body = "\n".join([
            f"Steps: {finding['steps']}",
            f"Expected: {finding['expected']}",
            f"Actual: {finding['actual']}",
            f"Page: {finding['page']}",
            "",
            f"Found by {self.identity.handle} on {task_id} at tip {tip_id}, URL {url}",
            f"QA-Checked: {task_id} {tip_id}",
        ])
        return self.mailbox.send(
            from_addr=self.identity.address, to=self.bugs_address,
            subject=finding["title"], body=_untag(body) + "\n",
        )

    def _reported(self) -> set[tuple[str, str]] | None:
        if not self.bugs_address:
            return set()
        try:
            rows = self.mailbox.list(recipient=self.bugs_address)
        except Exception as exc:
            message = str(exc)
            if message != self._bugs_error:
                self._bugs_error = message
                print(f"qa: bugs mailbox: {message}", flush=True)
            return None
        self._bugs_error = None
        me = self.identity.address.lower()
        found: set[tuple[str, str]] = set()
        for email in rows:
            if email.from_addr.lower() != me:
                continue
            for match in _CHECKED.finditer(email.body):
                found.add((match.group(1), match.group(2)))
        return found
