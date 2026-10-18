"""Write one markdown file per side of the two scripted comparisons, plus the Primitive mailbox.

    .venv/bin/python -m evals.dump_traces
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from gud.config import load
from gud.mailbox import open_mailbox
from gud.wire import parse_body

from evals.pr_vs_thread import Transcript, _encoder, _schema_tokens
from evals.swarm_vs_prs import HOME, REPO

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results"


def _fence(text: str, lang: str = "") -> str:
    body = text if text.endswith("\n") or text == "" else text + "\n"
    tick = "```"
    while tick in body:
        tick += "`"
    return f"{tick}{lang}\n{body}{tick}\n"


def _trace_md(title: str, source: str, events: list[dict], tool_tokens: int = 0) -> str:
    inn = sum(event["input_tokens"] + tool_tokens for event in events)
    out = sum(event["output_tokens"] for event in events)
    note = "Each input figure is the whole prompt on that turn: system, tool list, and history."
    if tool_tokens:
        note = (
            f"Each input figure is the prompt on that turn plus {tool_tokens:,} tool-list tokens, "
            "the same charge as the results table."
        )
    lines = [
        f"# {title}",
        "",
        source,
        "",
        f"{len(events)} turns. Input tokens {inn:,}. Output tokens {out:,}. Total {inn + out:,}.",
        note,
        "No model was called.",
        "",
    ]
    for index, event in enumerate(events, start=1):
        lines.append(f"## {index}. {event['who']} `{event['name']}`")
        lines.append("")
        lines.append(
            f"Input tokens {event['input_tokens'] + tool_tokens:,}. "
            f"Output tokens {event['output_tokens']:,}."
        )
        lines.append("")
        lines.append("**Arguments**")
        lines.append("")
        lines.append(_fence(json.dumps(event["arguments"], indent=2, ensure_ascii=False), "json"))
        lines.append("**Result**")
        lines.append("")
        lines.append(_fence(str(event["result"]), "text"))
    return "\n".join(lines)


def _mail_md(emails: list) -> str:
    lines = [
        "# Primitive swarm mailbox",
        "",
        f"Repo `{REPO}`. {len(emails)} emails in the order Primitive listed them.",
        "This is the scripted `scripts/demo.py` run, not the tool transcript used for the token table.",
        "",
    ]
    for index, email in enumerate(emails, start=1):
        parsed = parse_body(email.body)
        kind = parsed.kind or "plain"
        lines.append(f"## {index}. {kind} — {email.subject}")
        lines.append("")
        lines.append(f"- From: `{email.from_addr}`")
        lines.append(f"- To: {', '.join(f'`{addr}`' for addr in email.to) or '(none)'}")
        if email.cc:
            lines.append(f"- Cc: {', '.join(f'`{addr}`' for addr in email.cc)}")
        lines.append(f"- Message id: `{email.message_id}`")
        if email.in_reply_to:
            lines.append(f"- In reply to: `{email.in_reply_to}`")
        lines.append(f"- Received at: {email.received_at}")
        lines.append(f"- Sender verified: {email.sender_verified}")
        lines.append("")
        lines.append(_fence(email.body, "text"))
    return "\n".join(lines)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    Transcript.events.clear()

    from evals.pr_vs_thread import main as login_main
    login_main()
    login = list(Transcript.events)
    Transcript.events.clear()

    from evals.swarm_vs_prs import main as swarm_main
    swarm_main()
    swarm = list(Transcript.events)
    Transcript.events.clear()

    os.environ["GUD_HOME"] = str(HOME)
    os.environ["GUD_MAILBOX"] = "primitive"
    os.environ["GUD_DOMAIN"] = "lean-ridge.primitive.email"
    os.environ["GUD_REPO_ADDRESS"] = REPO
    emails = open_mailbox(load()).list(recipient=REPO)

    pr_schema, gud_schema = _schema_tokens(_encoder())
    files = {
        "login-pull-request.md": _trace_md(
            "Login negotiation, pull request",
            "Source: `python -m evals.pr_vs_thread`, pull-request side.",
            [event for event in login if event["side"] == "pull-request"],
            pr_schema,
        ),
        "login-gud.md": _trace_md(
            "Login negotiation, gud",
            "Source: `python -m evals.pr_vs_thread`, email side. Fake mailbox, not Primitive.",
            [event for event in login if event["side"] == "gud"],
            gud_schema,
        ),
        "swarm-pull-requests.md": _trace_md(
            "Scripted swarm, pull requests",
            "Source: `python -m evals.swarm_vs_prs`, pull-request side. Local git.",
            [event for event in swarm if event["side"] == "pull-request"],
        ),
        "swarm-gud.md": _trace_md(
            "Scripted swarm, gud tool trace",
            "Source: `python -m evals.swarm_vs_prs`, gud side of the token table. "
            "These are the tool calls billed for the same edits, not the 68 Primitive emails.",
            [event for event in swarm if event["side"] == "gud"],
        ),
        "swarm-primitive.md": _mail_md(emails),
    }
    for name, text in files.items():
        path = OUT / name
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path} ({len(text):,} bytes)", flush=True)


if __name__ == "__main__":
    main()
