"""Replay and transcript analysis for the eval runs.

Two halves:

- **Replay** (`gud1_metrics`, gud v1 arms only). A fresh replay of the run's whole host log and mail
  (`Replayer.feed` walks log entries and timers in time order, so one feed yields every §19 event the run
  produced, at its decision time) plus the signed messages themselves. From these: links and links/min,
  pushes and how each ended (linked native, linked carried, superseded, rejected), test runs by scope and
  per link, claims (won, lost, updated, released) and claim overlaps with how they were settled, comments by
  target, objections (reason-only vs evidence) and how they were answered, escalations and how they were
  resolved (decider or default), red-tip episodes and time to green, and the task's outcome.
- **Transcripts** (`transcript_analysis`, every arm). For each agent: the repo files it read (Read, the gud
  `read` tool, Grep on a file, and `cat`/`head`/`sed`/... in Bash), the files it changed, the files it read
  outside its claimed scope (gud v1) and outside the files it changed (every arm), its gud tool use, and for
  each of its colliding pushes whether it called `inflight` or `comment` before that push. A push collides
  when another agent's push touched one of the same files and neither was in the other's base while both
  were open. For specs with `meta.json` `guard_files` (refactor-guard), who read the guard file.

    .venv/bin/python evals/v1/analyze.py evals/v1/runs/<stamp>     # print the transcript analysis tables
"""

from __future__ import annotations

import json
import re
import shlex
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

GUD_TOOLS = ("inflight", "comment", "claim", "claim_update", "release", "subtask", "tasks", "task", "inbox", "wait",
             "sync", "push", "status", "approve", "object", "hold", "read", "search", "blame", "log", "thread",
             "forecast", "diff", "no_change")
READ_VERBS = {"cat", "head", "tail", "sed", "less", "more", "grep", "rg", "wc", "diff", "nl", "awk", "bat", "view"}


# ======================================================================== replay (gud v1)

def replay_all(client) -> tuple[Any, list, list[dict]]:
    """(chain, every event, every logged signed message) from a fresh replay of the client's mirror."""
    from gud.core.objects import MemoryStore
    from gud.core.replay import Replayer
    from gud.transport import mime

    log = client.replayer().log()
    entries = log.entries()
    emails, msgs = [], []
    for seq, gid, t in log.mail_ids():
        raw = client.mirror.mail(gid)
        email = mime.to_email(raw) if raw is not None else None
        if email is None:
            continue
        emails.append(email)
        env = email.envelope
        if env is None:
            continue
        msgs.append({"id": gid, "seq": seq, "t": t, "kind": env.kind, "author": env.author, "env": env})
    r = Replayer(MemoryStore(), trusted_host_keys=client.trusted_host_keys)
    delta = r.feed(emails, entries, client.now_ms())
    return r.chain(), list(delta.events), msgs


def push_files(env) -> list[str]:
    out: list[str] = []
    for rec in env.records:
        if rec.name in ("patch", "put", "delete", "mode") and rec.args:
            out.append(rec.args[0])
        elif rec.name == "rename" and len(rec.args) >= 2:
            out += [rec.args[0], rec.args[1]]
    return sorted(set(out))


def push_records(chain, events: Sequence, msgs: Sequence[dict], *, genesis: str | None, t_end: int) -> list[dict]:
    """Every push/revert (not genesis): author, files, received, base seq, how and when it ended."""
    seq_of = {l.state: l.seq for l in chain.links}
    link_of = {l.push: l for l in chain.links}
    ended: dict[str, tuple[int, str]] = {}
    carried: set[str] = set()
    for e in events:
        push = e.data.get("push") if isinstance(e.data, Mapping) else None
        if e.type == "superseded" and push:
            ended.setdefault(push, (e.at, "superseded"))
        elif e.type == "push.rejected" and push:
            ended.setdefault(push, (e.at, str(e.data.get("status") or "rejected")))
        elif e.type == "carried" and push:
            carried.add(push)
    statuses = {}
    for p in chain.proposals.values():
        statuses.setdefault(p.push, p.status)
    out = []
    for m in msgs:
        if m["kind"] not in ("push", "revert") or m["id"] == genesis:
            continue
        env = m["env"]
        link = link_of.get(m["id"])
        if link is not None:
            how = "linked_carried" if link.carried_from else "linked"
            end, end_t = how, link.linked_at
        elif m["id"] in ended:
            end_t, end = ended[m["id"]]
        else:
            end, end_t = f"open:{statuses.get(m['id'], '?')}", t_end
        parent = env.get("Gud-Parent")
        out.append({
            "id": m["id"], "author": m["author"], "t": m["t"], "files": push_files(env), "parent": parent,
            "base_seq": seq_of.get(parent, -1), "end": end, "end_t": end_t, "carried": m["id"] in carried,
            "link_seq": link.seq if link is not None else None, "task": env.get("Gud-Task"),
            "revises": env.get("Gud-Revises"), "intent": (env.text or "").strip().splitlines()[0][:120]
            if (env.text or "").strip() else "",
        })
    _mark_collisions(out)
    return out


def _mark_collisions(pushes: list[dict]) -> None:
    """P and Q (different authors) collide when they share a file, neither is in the other's base (Q is not
    a link at or below P's base seq, and the reverse), and either they were open at the same time or the
    later one was built on a base older than the earlier one's link."""
    for p in pushes:
        p["collides_with"] = []
    for i, p in enumerate(pushes):
        for q in pushes[i + 1:]:
            if p["author"] == q["author"] or not set(p["files"]) & set(q["files"]):
                continue
            p_in_q = p["link_seq"] is not None and p["link_seq"] <= q["base_seq"]
            q_in_p = q["link_seq"] is not None and q["link_seq"] <= p["base_seq"]
            if p_in_q or q_in_p:
                continue
            # open at the same time, or one built on a base older than the other's link (it never saw it)
            stale = ((p["link_seq"] is not None and q["base_seq"] < p["link_seq"] and q["t"] >= p["t"])
                     or (q["link_seq"] is not None and p["base_seq"] < q["link_seq"] and p["t"] >= q["t"]))
            if stale or (p["t"] <= q["end_t"] and q["t"] <= p["end_t"]):
                p["collides_with"].append(q["id"])
                q["collides_with"].append(p["id"])


def _episodes(events: Sequence, t_end: int) -> list[dict]:
    out: list[dict] = []
    open_: dict | None = None
    for e in sorted(events, key=lambda e: e.at):
        if e.type == "tip.red" and open_ is None:
            open_ = {"red_at": e.at, "red_link": e.data.get("red_link")}
        elif e.type == "tip.bisected" and open_ is not None:
            open_["first_bad"] = e.data.get("first_bad")
            open_["first_bad_author"] = e.data.get("author")
        elif e.type == "tip.green" and open_ is not None:
            open_["green_at"] = e.at
            open_["to_green_s"] = round((e.at - open_["red_at"]) / 1000, 1)
            out.append(open_)
            open_ = None
    if open_ is not None:
        open_["green_at"] = None
        open_["to_green_s"] = None
        open_["red_for_s_at_end"] = round((t_end - open_["red_at"]) / 1000, 1)
        out.append(open_)
    return out


def gud1_metrics(client, *, t0: float, wall_s: float, genesis: str | None, task: str | None,
                 handles: Sequence[str]) -> dict:
    from gud.core.config import globs_overlap

    client.sync_log()
    chain, events, msgs = replay_all(client)
    t0ms = int(t0 * 1000)
    t_end = client.now_ms()

    def rel(ms):
        return None if ms is None else round((ms - t0ms) / 1000, 1)

    body = [m for m in msgs if m["id"] != genesis]
    kinds = Counter(m["kind"] for m in body)
    links = [l for l in chain.links if l.seq > 0]
    n_links = len(links)
    minutes = max(wall_s, 1e-9) / 60
    ev_types = Counter(e.type for e in events)
    pushes = push_records(chain, events, msgs, genesis=genesis, t_end=t_end)

    # tests
    tests = [m for m in body if m["kind"] == "test"]
    by_scope = Counter(m["env"].get("Gud-Scope") or "?" for m in tests)
    test_fail = sum(1 for m in tests if (m["env"].get("Gud-Result") or "") != "pass")

    # claims and overlaps
    claim_msgs = [m for m in body if m["kind"] == "claim"]
    claims = [c for c in chain.claims.values() if c.status != "void"]      # void: lost (the item was held)
    lost = sum(1 for c in chain.claims.values() if c.status == "void")
    claim_ids: dict[str, str] = {}             # any claim message id -> claim id
    for c in claims:
        claim_ids[c.id] = c.id
        claim_ids[c.latest] = c.id
    updates = [m for m in claim_msgs if m["env"].get("Gud-Revises") and (m["env"].get("Gud-Release") or "") != "yes"]
    releases = [m for m in claim_msgs if (m["env"].get("Gud-Release") or "") == "yes"]
    comments = [m for m in body if m["kind"] == "comment"]
    tip_paths = [p for p in client.reader().tree().paths()] if chain.tip else []
    overlaps = []
    for e in events:
        if e.type != "claim.overlap":
            continue
        ids = list(e.data.get("claims") or [])
        holders = list(e.data.get("holders") or [])
        tasks = [t for t in (e.data.get("tasks") or []) if t]
        related = set(ids) | {c.latest for c in claims if c.id in ids} | set(tasks)
        talk = [m for m in comments if m["t"] >= e.at and m["author"] in holders
                and (m["env"].get("Gud-Target") in related or m["env"].get("Gud-Task") in tasks)]
        narrowed = [m for m in updates if m["t"] >= e.at and claim_ids.get(m["env"].get("Gud-Revises") or "") in ids]
        released = [m for m in releases if m["t"] >= e.at and claim_ids.get(m["env"].get("Gud-Revises") or "") in ids]
        final = [c for c in claims if c.id in ids]
        still = (len(final) == 2 and all(c.status == "active" for c in final)
                 and globs_overlap(list(final[0].files), list(final[1].files), tip_paths))
        both_pushed = [p["id"] for p in pushes if p["t"] >= e.at and p["author"] in holders and p["collides_with"]
                       and any(q["author"] in holders for q in pushes if q["id"] in p["collides_with"])]
        if narrowed and not still:
            how = "split (claim update)"
        elif released:
            how = "released"
        elif talk and not both_pushed:
            how = "settled in comments"
        elif talk:
            how = "commented, then collided"
        elif both_pushed:
            how = "ignored, collided"
        else:
            how = "ignored, no collision"
        overlaps.append({"at_s": rel(e.at), "holders": holders, "globs": e.data.get("globs"),
                         "comments_after": len(talk), "claim_updates_after": len(narrowed),
                         "releases_after": len(released), "colliding_pushes_after": len(both_pushed),
                         "still_overlapping_at_end": still, "settled": how})

    # comments by target
    ids_push = {m["id"] for m in body if m["kind"] in ("push", "revert")}
    ids_task = set(chain.tasks)
    ids_obj = {m["id"] for m in body if m["kind"] == "object"}

    def target_kind(t):
        if t in claim_ids:
            return "claim"
        if t in ids_push:
            return "push"
        if t in ids_task:
            return "task"
        if t in ids_obj:
            return "objection"
        return "other" if t else "none"

    comment_targets = Counter(target_kind(m["env"].get("Gud-Target")) for m in comments)
    escalate_comments = [m for m in comments if m["env"].get("Gud-Escalate")]

    # objections
    objections = []
    push_by_id = {p["id"]: p for p in pushes}
    revisions: dict[str, list[dict]] = {}
    for p in pushes:
        if p["revises"]:
            revisions.setdefault(p["revises"], []).append(p)
    for m in (m for m in body if m["kind"] == "object"):
        env = m["env"]
        target = env.get("Gud-Target")
        push = push_by_id.get(target)
        if push is None:     # a candidate id (carried): find the push by candidate
            prop = chain.proposals.get(target)
            push = push_by_id.get(prop.push) if prop is not None else None
        author = push["author"] if push else None
        answered_comment = any(c["t"] > m["t"] and c["author"] == author and
                               c["env"].get("Gud-Target") in (m["id"], target, push["id"] if push else None)
                               for c in comments)
        answered_revision = bool(push) and any(r["t"] > m["t"] for r in revisions.get(push["id"], []))
        objections.append({
            "by": m["author"], "push_author": author, "at_s": rel(m["t"]),
            "kind": "evidence" if env.records_named("evidence") else "reason",
            "reason": (env.text or "")[:200], "answered": "revision" if answered_revision else
            ("comment" if answered_comment else "no"), "push_end": push["end"] if push else None,
        })

    # escalations
    escalations = []
    for esc in chain.escalations.values():
        escalations.append({"reason": esc.reason, "status": esc.status, "decided_by": esc.decided_by,
                            "opened_s": rel(esc.opened_at), "decided_s": rel(esc.decided_at),
                            "chosen_author": push_by_id.get(esc.chosen, {}).get("author") if esc.chosen else None,
                            "pushes": len(esc.pushes)})
    task_escalations = [{"reason": e.data.get("reason"), "by": e.data.get("by"), "at_s": rel(e.at)}
                        for e in events if e.type == "escalated" and e.data.get("escalation") is None]

    # red tip
    episodes = _episodes(events, t_end)
    closed = [ep["to_green_s"] for ep in episodes if ep["to_green_s"] is not None]

    # task
    t = chain.tasks.get(task) if task else None
    subtasks = [s for s in chain.tasks.values() if s.kind == "subtask"]
    fixes = [s for s in chain.tasks.values() if s.kind == "fix"]

    # contested pushes (the common conflict columns)
    contested = [p for p in pushes if p["end"] in ("superseded", "conflict", "stale", "declined", "expired")]
    objected_ids = set()
    for m in (m for m in body if m["kind"] == "object"):
        objected_ids.add(m["env"].get("Gud-Target"))
    contested += [p for p in pushes if p["id"] in objected_ids and p not in contested]
    conflicts = []
    for p in sorted(contested, key=lambda p: p["t"]):
        later = [l for l in links if l.author == p["author"] and l.linked_at > p["t"]]
        conflicts.append({"push": p["id"][:12], "author": p["author"], "end": p["end"], "files": p["files"],
                          "resolved": bool(later) or p["end"].startswith("linked"),
                          "resolution": "linked" if p["end"].startswith("linked") else
                          ("later push by the same author linked" if later else "not resolved")})

    per_agent = {}
    for h in handles:
        mine = [p for p in pushes if p["author"] == h]
        per_agent[h] = {
            "pushes": len(mine), "links": sum(1 for l in links if l.author == h),
            "carried_links": sum(1 for l in links if l.author == h and l.carried_from),
            "superseded": sum(1 for p in mine if p["end"] == "superseded"),
            "colliding_pushes": sum(1 for p in mine if p["collides_with"]),
            "claims_won": sum(1 for c in claims if c.holder == h),
            "claim_msgs": sum(1 for m in claim_msgs if m["author"] == h),
            "comments": sum(1 for m in comments if m["author"] == h),
            "objections": sum(1 for m in body if m["kind"] == "object" and m["author"] == h),
            "approvals": sum(1 for m in body if m["kind"] == "approve" and m["author"] == h),
            "claim_scopes": [list(c.files) for c in claims if c.holder == h],
            # every scope the agent held at some point: winning claims and their updates
            "claim_scopes_all": [list(m["env"].get_list("Gud-Files")) for m in claim_msgs
                                 if m["author"] == h and m["env"].get_list("Gud-Files")
                                 and (m["id"] in claim_ids or (m["env"].get("Gud-Revises") or "") in claim_ids)],
        }

    resolved = sum(1 for c in conflicts if c["resolved"])
    linked_pushes = [p for p in pushes if p["end"].startswith("linked")]
    gud1 = {
        "links": n_links,
        "links_per_min": round(n_links / minutes, 3),
        "first_link_s": rel(min((l.linked_at for l in links), default=None)) if links else None,
        "pushes": len(pushes),
        "pushes_by_end": dict(sorted(Counter(p["end"] for p in pushes).items())),
        "carried_links": sum(1 for l in links if l.carried_from),
        "carried_events": ev_types.get("carried", 0),
        "superseded": sum(1 for p in pushes if p["end"] == "superseded"),
        "colliding_pushes": sum(1 for p in pushes if p["collides_with"]),
        "emails": len(body),
        "emails_by_kind": dict(sorted(kinds.items())),
        "emails_per_link": round(len(body) / n_links, 3) if n_links else None,
        "test_runs": len(tests),
        "test_runs_by_scope": dict(sorted(by_scope.items())),
        "test_failures": test_fail,
        "test_runs_per_link": round(len(tests) / n_links, 3) if n_links else None,
        "claims": {"won": len(claims), "lost": max(lost, ev_types.get("claim.lost", 0)), "updates": len(updates),
                   "releases": len(releases), "lapsed": ev_types.get("claim.lapsed", 0),
                   "scopes": [{"holder": c.holder, "task": chain.tasks[c.task].alias if c.task in chain.tasks else None,
                               "files": list(c.files), "status": c.status} for c in claims],
                   "scope_exceeded": ev_types.get("scope.exceeded", 0)},
        "overlaps": {"total": len(overlaps), "by_settlement": dict(Counter(o["settled"] for o in overlaps)),
                     "list": overlaps},
        "comments": {"total": len(comments), "by_target": dict(sorted(comment_targets.items())),
                     "escalation_comments": len(escalate_comments)},
        "objections": {"total": len(objections), "reason": sum(1 for o in objections if o["kind"] == "reason"),
                       "evidence": sum(1 for o in objections if o["kind"] == "evidence"),
                       "answered": dict(Counter(o["answered"] for o in objections)), "list": objections},
        "approvals": kinds.get("approve", 0), "holds": kinds.get("hold", 0),
        "escalations": {"consent": escalations, "task": task_escalations,
                        "resolved_by": dict(Counter(str(e["decided_by"] or "open") for e in escalations))},
        "red_tip": {"episodes": len(episodes), "closed": len(closed),
                    "median_to_green_s": statistics.median(closed) if closed else None,
                    "red_at_end": bool(chain.red.red), "fix_tasks": len(fixes), "list": episodes},
        "task": None if t is None else {"alias": t.alias, "status": t.status, "subtasks": len(subtasks),
                                        "subtask_status": dict(Counter(s.status for s in subtasks)),
                                        "work_done_s": rel(t.work_done_at), "done_s": rel(t.done_at)},
        "events": dict(sorted(ev_types.items())),
        "per_agent": per_agent,
        "link_list": [{"seq": l.seq, "author": l.author, "carried": bool(l.carried_from), "files": list(l.files),
                       "at_s": rel(l.linked_at), "intent": (l.intent or "").strip().splitlines()[0][:100]
                       if (l.intent or "").strip() else ""} for l in links],
        "push_list": [{**{k: v for k, v in p.items() if k not in ("parent",)}, "id": p["id"][:12],
                       "collides_with": [c[:12] for c in p["collides_with"]], "t": rel(p["t"]),
                       "end_t": rel(p["end_t"])} for p in pushes],
        "conflicts": conflicts,
        "_pushes_full": pushes,
    }
    return {
        "integrations": n_links,
        "integrations_per_min": round(n_links / minutes, 3),
        "test_runs": len(tests),
        "test_runs_per_integration": round(len(tests) / n_links, 3) if n_links else None,
        "conflicts_total": len(conflicts),
        "conflicts_resolved": resolved,
        "gud1": gud1,
    }


# ======================================================================== reference-agent transcripts

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def ref_tool_rows(tdir: Path) -> list[dict]:
    """The reference agent's transcripts as tool-call rows {name, input, result, is_error, t}, in time order."""
    rows: list[dict] = []
    for path in sorted(Path(tdir).glob("*.jsonl")) if Path(tdir).exists() else []:
        pending: list[dict] = []
        for rec in _read_jsonl(path):
            if rec.get("type") == "tool_use":
                row = {"name": rec.get("name") or "", "input": rec.get("input"), "result": "", "is_error": False,
                       "t": (rec.get("at") or 0) / 1000, "session": path.stem}
                pending.append(row)
                rows.append(row)
            elif rec.get("type") == "tool_result" and pending:
                row = pending.pop(0)
                row["result"] = str(rec.get("content") or "")[:6000]
                row["is_error"] = bool(rec.get("error"))
    rows.sort(key=lambda r: r["t"])
    return rows


def ref_auth_failed(adir: Path) -> bool:
    for rec in _read_jsonl(Path(adir) / "turns.jsonl")[-3:]:
        text = f"{rec.get('error') or ''} {rec.get('text') or ''}".lower()
        if rec.get("is_error") and ("authenticat" in text or "oauth" in text):
            return True
    return False


def ref_agent_row(handle: str, adir: Path, rows: list[dict], *, model: str | None, exit_code, killed, wall_s) -> dict:
    """A per-agent row with the same keys as `agents.AgentRun.summary()`."""
    adir = Path(adir)
    turns = _read_jsonl(adir / "turns.jsonl")
    try:
        spent = float(json.loads((adir / "budget.json").read_text())["member"]["spent_usd"])
    except (OSError, ValueError, KeyError):
        spent = sum(float(t.get("usd") or 0) for t in turns)
    sdk_turns = 0
    for path in sorted((adir / "transcripts").glob("*.jsonl")) if (adir / "transcripts").exists() else []:
        sdk_turns += sum(int(r.get("turns") or 0) for r in _read_jsonl(path) if r.get("type") == "result")
    final = turns[-1].get("text", "") if turns else ""
    escalation = None
    for row in rows:
        if row["name"] == "mcp__gud__comment" and isinstance(row.get("input"), dict) and row["input"].get("escalate"):
            escalation = str(row["input"].get("text") or "")[:500]
    return {
        "handle": handle, "exit_code": exit_code, "killed": killed, "wall_s": wall_s,
        "turns": sdk_turns or len(turns), "wakeups": len(turns),
        "tokens": {"ledger": sum(int(t.get("tokens") or 0) for t in turns)},
        "cost_usd": round(spent, 6), "cost_source": "ledger", "model": model,
        "result_is_error": any(t.get("is_error") for t in turns), "terminal_reason": None,
        "mcp_servers": None, "tool_calls": len(rows),
        "tool_counts": dict(sorted(Counter(r["name"] for r in rows).items())),
        "escalated": escalation is not None, "escalation": escalation, "final_text": final[-1500:],
        "api_errors": [t.get("error") for t in turns if t.get("error")][:5], "stderr_tail": "",
        "turn_keys": [t.get("key", "")[:24] for t in turns],
    }


# ======================================================================== transcripts (every arm)

def _rel_path(target: str, roots: Sequence[Path]) -> str | None:
    if not target:
        return None
    p = Path(target)
    if not p.is_absolute():
        return p.as_posix().lstrip("./") or None
    for root in roots:
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
    return None


def _bash_reads(command: str, tracked: set[str], roots: Sequence[Path]) -> set[str]:
    out: set[str] = set()
    for part in re.split(r"\|\||&&|[|;\n]", command or ""):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if not words:
            continue
        verb = Path(words[0]).name
        if verb not in READ_VERBS:
            continue
        for w in words[1:]:
            rel = _rel_path(w, roots)
            if rel in tracked:
                out.add(rel)
    return out


def files_read(rows: Sequence[dict], tracked: set[str], roots: Sequence[Path]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        name, inp = row.get("name", ""), row.get("input") or {}
        if not isinstance(inp, dict):
            continue
        if name == "Read":
            rel = _rel_path(str(inp.get("file_path") or ""), roots)
            if rel in tracked:
                out.add(rel)
        elif name == "mcp__gud__read":
            rel = str(inp.get("path") or "").strip().lstrip("./")
            if rel in tracked:
                out.add(rel)
        elif name == "Grep":
            rel = _rel_path(str(inp.get("path") or ""), roots)
            if rel in tracked:
                out.add(rel)
        elif name == "Bash":
            out |= _bash_reads(str(inp.get("command") or ""), tracked, roots)
    return out


def _push_index(rows: Sequence[dict], push_id: str) -> int | None:
    for i, row in enumerate(rows):
        if row.get("name") == "mcp__gud__push" and push_id in str(row.get("result") or ""):
            return i
    return None


def git_changes(origin: Path) -> dict[str, set[str]]:
    """{author: files touched in any of their commits on any branch} of a bare repo."""
    proc = subprocess.run(["git", "--git-dir", str(origin), "log", "--all", "--format=@@%an", "--name-only"],
                          capture_output=True, text=True)
    out: dict[str, set[str]] = {}
    who = None
    for line in proc.stdout.splitlines():
        if line.startswith("@@"):
            who = line[2:]
        elif line.strip() and who:
            out.setdefault(who, set()).add(line.strip())
    return out


def transcript_analysis(transcripts: Mapping[str, Sequence[dict]], *, roots: Mapping[str, Path],
                        tracked: Iterable[str], changes: Mapping[str, Iterable[str]],
                        claims: Mapping[str, Sequence[Sequence[str]]] | None = None,
                        pushes: Sequence[dict] | None = None, guard_files: Sequence[str] = ()) -> dict:
    """Per-agent read/coordination analysis (see the module docstring). `claims` and `pushes` are gud v1 only."""
    from gud.core.tasks import scope_matches

    tracked = set(tracked)
    agents = {}
    for handle, rows in transcripts.items():
        rows = list(rows)
        root = roots.get(handle)
        read = files_read(rows, tracked, [root] if root else [])
        changed = set(changes.get(handle, ()))
        counts = Counter(r.get("name", "") for r in rows)
        gud_counts = {t: counts.get(f"mcp__gud__{t}", 0) for t in GUD_TOOLS if counts.get(f"mcp__gud__{t}", 0)}
        row: dict[str, Any] = {
            "files_read": sorted(read), "files_changed": sorted(changed),
            "reads_outside_own_changes": sorted(read - changed),
            "gud_tools": gud_counts,
        }
        if claims is not None:
            scopes = [list(s) for s in claims.get(handle, ())]
            row["claim_scopes"] = scopes
            row["reads_outside_claims"] = sorted(p for p in read if not any(scope_matches(s, p) for s in scopes))
            row["changes_outside_claims"] = sorted(p for p in changed if not any(scope_matches(s, p) for s in scopes))
        if pushes is not None:
            mine = [p for p in pushes if p["author"] == handle]
            colliding = []
            for p in mine:
                if not p["collides_with"]:
                    continue
                idx = _push_index(rows, p["id"])
                before = rows[:idx] if idx is not None else []
                prev = max([i for i, r in enumerate(before) if r.get("name") == "mcp__gud__push"], default=-1)
                since = before[prev + 1:]
                colliding.append({
                    "push": p["id"][:12], "files": p["files"], "end": p["end"], "found_in_transcript": idx is not None,
                    "inflight_before": any(r.get("name") == "mcp__gud__inflight" for r in before),
                    "comment_before": any(r.get("name") == "mcp__gud__comment" for r in before),
                    "inflight_since_last_push": any(r.get("name") == "mcp__gud__inflight" for r in since),
                    "comment_since_last_push": any(r.get("name") == "mcp__gud__comment" for r in since),
                })
            row["pushes"] = len(mine)
            row["colliding_pushes"] = colliding
            # pushes the working copy refused before sending (base behind a link that touched the same files)
            refused = []
            for i, r in enumerate(rows):
                if r.get("name") == "mcp__gud__push" and str(r.get("result") or "").startswith(("error", '{"error"')):
                    refused.append({"error": str(r.get("result"))[:160],
                                    "stale_base": "sync" in str(r.get("result")),
                                    "inflight_before": any(x.get("name") == "mcp__gud__inflight" for x in rows[:i])})
            row["push_refusals"] = refused
        if guard_files:
            row["guard_files_read"] = sorted(set(guard_files) & read)
            row["guard_files_changed"] = sorted(set(guard_files) & changed)
        agents[handle] = row
    coll = [c for a in agents.values() for c in a.get("colliding_pushes", [])]
    summary = {
        "files_read_per_agent": round(sum(len(a["files_read"]) for a in agents.values()) / max(len(agents), 1), 2),
        "reads_outside_own_changes_per_agent": round(
            sum(len(a["reads_outside_own_changes"]) for a in agents.values()) / max(len(agents), 1), 2),
    }
    if claims is not None:
        summary["reads_outside_claims_per_agent"] = round(
            sum(len(a["reads_outside_claims"]) for a in agents.values()) / max(len(agents), 1), 2)
        summary["agents_with_claims"] = sum(1 for a in agents.values() if a["claim_scopes"])
    if pushes is not None:
        summary["colliding_pushes"] = len(coll)
        summary["colliding_with_inflight_before"] = sum(1 for c in coll if c["inflight_before"])
        summary["colliding_with_comment_before"] = sum(1 for c in coll if c["comment_before"])
        summary["colliding_with_inflight_since_last_push"] = sum(1 for c in coll if c["inflight_since_last_push"])
        refusals = [x for a in agents.values() for x in a.get("push_refusals", [])]
        summary["push_refusals"] = len(refusals)
        summary["stale_base_refusals"] = sum(1 for x in refusals if x["stale_base"])
    for tool in ("inflight", "comment", "claim", "claim_update", "subtask"):
        if pushes is not None or claims is not None:
            summary[f"{tool}_calls"] = sum(a["gud_tools"].get(tool, 0) for a in agents.values())
    if guard_files:
        summary["guard_read_by"] = sorted(h for h, a in agents.items() if a["guard_files_read"])
        summary["guard_changed_by"] = sorted(h for h, a in agents.items() if a["guard_files_changed"])
    return {"summary": summary, "agents": agents}


# ======================================================================== CLI

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path)
    args = ap.parse_args(argv)
    for path in args.paths:
        for file in sorted(Path(path).rglob("summary.json")) if path.is_dir() else [path]:
            data = json.loads(file.read_text())
            ana = data.get("analysis") or {}
            print(f"## {data.get('arm')} {data.get('spec')} n{data.get('agents_n')} t{data.get('trial')}")
            print(json.dumps(ana.get("summary"), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
