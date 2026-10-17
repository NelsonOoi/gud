"""Live demo of the eight beats in gud.md / DEMO.md.

The scripted swarm does the work. This driver creates the tasks, narrates each
beat, and checks the mailbox. Exit status is non-zero when a beat fails, so
`demo.py --fast` is also the end-to-end test.

    python scripts/demo.py --fast
    python scripts/demo.py --interactive
    python scripts/demo.py --llm
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OPENING = (
    "Git is too slow for agents to finish a complex task. The work is clone, pull, "
    "rebase, conflict, pull request, review, CI. We rebuilt source control. The repo "
    "is an email thread: an agent sends a diff, the owner of those lines has a minute "
    "to object, a VM replies with the tests, and the change is in."
)

BEATS = [
    "Nobody cloned anything. The assignment was an email, and the change is an email.",
    "No review queue. The owner of the lines wasn't touched, so the only wait is the tests.",
    "In git this is a conflict file and a retry loop. Here the author of the line says no, "
    "on the same thread, and the fix lands without a rebase.",
    "The human was not in the review path. They can still reply veto to any push and it never lands.",
    "No branches. The chain has one tip, and the loser is told what to do.",
    "Joining and leaving is a change to the repo, not a seat in an admin console. "
    "The history they wrote does not get rewritten.",
    "Every rule uses the time the email arrived, so any machine rebuilds the same history.",
    "Armature runs the same task through gud and through a git MCP server. "
    "The scorecard lives in evals/.",
]


def main(argv: list[str] | None = None) -> None:
    args = _args(argv)
    if args.fast:
        os.environ["GUD_WINDOW_SECONDS"] = "2"
    elif "GUD_WINDOW_SECONDS" not in os.environ:
        os.environ["GUD_WINDOW_SECONDS"] = "15"

    from gud import compose, members as members_mod
    from gud.chain import Repo
    from gud.cli import cmd_init
    from gud.config import load
    from gud.keys import create_identity, load_identity
    from gud.mail_fake import FileMailbox
    from gud.mailbox import open_mailbox
    from gud.patch import apply_diff
    from gud.replay import replay
    from gud.rules import affected_owners, quorum_voters
    from gud.swarm import Swarm
    from gud.tasks import derive, next_task_id
    from gud.types import Member
    from gud.wire import parse_body
    import gud.sig as sig

    cfg = load()
    started = time.time()
    swarm = None
    state: dict = {}

    def refresh():
        last = None
        for _ in range(5):
            try:
                return state["repo"].refresh()
            except json.JSONDecodeError as exc:
                last = exc
                time.sleep(0.02)
        raise last

    def fail(detail: str) -> None:
        print(f"\nFAIL: {detail}", flush=True)
        try:
            _dump(refresh())
        except Exception as exc:
            print(f"(no chain dump: {exc})", flush=True)
        raise SystemExit(1)

    def beat(number: int, title: str) -> None:
        print(f"\n=== {number}. {title} ===", flush=True)
        print(BEATS[number - 1], flush=True)

    def pause() -> None:
        if args.interactive:
            try:
                input("\n[enter] ")
            except EOFError:
                return
        elif not args.fast:
            time.sleep(0.6)

    def wait_until(pred, timeout: float, detail: str):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                found = pred()
            except json.JSONDecodeError:
                time.sleep(0.05)
                continue
            if found:
                return found
            time.sleep(0.1)
        fail(detail)

    def create_task(title: str, description: str, requester: str = "@nelson") -> str:
        chain = refresh()
        task_id = next_task_id(chain.emails)
        console = load_identity(cfg, "@console")
        body = compose.task_body(console, task_id, requester, description or title)
        members = chain.tip.members if chain.tip else {}
        email = compose.send(state["mailbox"], console, members, f"[gud] {task_id}: {title}", body)
        print(f"  task {task_id} from @console {email.message_id}", flush=True)
        return task_id

    def bless(chain, prop) -> None:
        """Send the agent approvals a quorum push still needs. Humans have no local key."""
        parent = chain.by_id.get(prop.parent)
        email = chain.by_message.get(prop.message_id)
        if parent is None or email is None:
            return
        try:
            new_tree = apply_diff(dict(parent.tree), parse_body(email.body).diff)
        except Exception as exc:
            fail(f"membership diff does not apply: {exc}")
        voters = quorum_voters(parent, new_tree)
        already = state["approvals"].setdefault(prop.message_id, set(prop.approvals))
        deficit = (prop.quorum_needed or 1) - (prop.quorum_have or 0)
        if deficit <= 0:
            return
        for handle in voters:
            if deficit <= 0:
                break
            member = parent.members.get(handle)
            if handle in already or member is None or member.role == "human" or not member.public_key:
                continue
            if handle == prop.author:
                continue
            ident = load_identity(cfg, handle)
            compose.send(
                state["mailbox"], ident, parent.members, f"Re: {email.subject}",
                compose.approve_body(ident, prop.message_id), in_reply_to=prop.message_id,
            )
            already.add(handle)
            deficit -= 1
            print(f"  {handle} approves {prop.message_id}", flush=True)

    def land_quorum(push_id: str, timeout: float):
        swarm.tester.step()
        deadline = time.time() + timeout
        while time.time() < deadline:
            chain = refresh()
            if push_id in chain.by_id:
                return chain
            prop = chain.proposals.get(push_id)
            if prop is None:
                time.sleep(0.05)
                continue
            if prop.status == "needs_quorum":
                bless(chain, prop)
            elif prop.status in {"bad_sig", "conflict", "tests_failed", "vetoed", "objected", "stale"}:
                fail(f"membership push {prop.status}: {prop.reason}")
            time.sleep(0.1)
        fail(f"membership push {push_id} did not land")

    try:
        print(OPENING, flush=True)
        mailbox = open_mailbox(cfg)
        if not mailbox.list(recipient=cfg.repo_address):
            print("demo: mailbox empty, running gud init from demo_repo", flush=True)
            cmd_init(cfg, argparse.Namespace(source=str(ROOT / "demo_repo")))
        state["mailbox"] = mailbox
        state["repo"] = Repo(mailbox, cfg.window_seconds)
        state["approvals"] = {}
        if refresh().tip is None:
            fail("genesis did not land")

        swarm = Swarm(cfg, mailbox, mode="llm" if args.llm else "scripted", workers=None)
        swarm.start()
        limit = 20 if args.fast else 40

        beat(1, "The task fans out")
        task1 = create_task(
            "Add per-user rate limiting to login and bill overage requests",
            "Add per-user rate limiting to login, and bill overage requests. "
            "Split across auth.py, billing.py, api/handlers.py, and ratelimit.py.",
        )

        def split_ready():
            tasks = derive(refresh())
            found = next((task for task in tasks if task.id == task1), None)
            if found is None or len(found.subtasks) < 3:
                return None
            return found

        task = wait_until(split_ready, limit, "lead did not split the task")
        assignees = [sub.assignee for sub in task.subtasks]
        print(f"  {task.id} split into {len(task.subtasks)} subtasks:", flush=True)
        for sub in task.subtasks:
            print(f"    {sub.id} {sub.assignee}: {sub.title}", flush=True)
        if len(set(assignees)) < len(assignees):
            fail(f"subtasks were not fanned out across workers: {assignees}")
        pause()

        beat(2, "A clean push lands by lazy consensus")

        def clean():
            chain = refresh()
            for prev, link in zip(chain.links, chain.links[1:]):
                if not affected_owners(prev, link.diff, link.author):
                    return link
            return None

        link = wait_until(clean, limit, "no push landed with an empty affected-owner set")
        print(f"  {link.author} {link.subject} landed as link #{link.seq} with no affected owners",
              flush=True)
        pause()

        beat(3, "A bad edit is a reply, then a revised push lands on approval")

        def rate_limited():
            tip = refresh().tip
            return tip if tip and 'rate limited' in tip.tree.get("auth.py", "") else None

        wait_until(rate_limited, limit, "rate-limit login never landed, so nobody owns those lines")
        task2 = create_task(
            "Lockout and rate limit collide in login",
            "Five wrong passwords lock the account. The first version replaces the rate-limit check; "
            "the owner objects; the revision keeps both.",
        )

        def objection_landed():
            chain = refresh()
            approves: dict[str, list] = {}
            objection = None
            for email in chain.emails:
                parsed = parse_body(email.body)
                reason = (parsed.get("Gud-Reason") or "").lower()
                if parsed.kind == "object" and ("rate-limit" in reason or "do not replace" in reason):
                    objection = email
                if parsed.kind == "approve" and parsed.get("Gud-Target"):
                    approves.setdefault(parsed.get("Gud-Target"), []).append(parsed.get("Gud-Author"))
            if objection is None:
                return None
            for prev, link in zip(chain.links, chain.links[1:]):
                new = link.tree.get("auth.py", "")
                old = prev.tree.get("auth.py", "")
                if "owner kept this check" in new and "owner kept this check" not in old:
                    if link.message_id in approves:
                        return objection, link, approves[link.message_id]
            return None

        objection, revised, approvers = wait_until(
            objection_landed, limit, "objection did not produce an approved revision",
        )
        reason = parse_body(objection.body).get("Gud-Reason")
        print(f"  object {parse_body(objection.body).get('Gud-Author')}: {reason}", flush=True)
        print(f"  revised link #{revised.seq} by {revised.author}, approved by {', '.join(approvers)}",
              flush=True)
        if "gud-bad-edit" in revised.tree.get("auth.py", ""):
            fail("the bad edit landed")
        if "locked" not in revised.tree.get("auth.py", ""):
            fail("the revision did not add the lockout")

        task3 = create_task(
            "Record the rate-limit decision on the protected interface",
            "Add RateLimitDecision and RateLimiter.decide. api/interfaces.py is explicit.",
        )

        def explicit_landed():
            chain = refresh()
            for prev, link in zip(chain.links, chain.links[1:]):
                new = link.tree.get("api/interfaces.py", "")
                old = prev.tree.get("api/interfaces.py", "")
                if "class RateLimitDecision" in new and "class RateLimitDecision" not in old:
                    authors = []
                    for email in chain.emails:
                        parsed = parse_body(email.body)
                        if parsed.kind == "approve" and parsed.get("Gud-Target") == link.message_id:
                            authors.append((email, parsed.get("Gud-Author")))
                    if authors and authors[0][0].received_at <= link.linked_at:
                        return link, authors
            return None

        protected, approvals = wait_until(explicit_landed, limit, "explicit interfaces.py push did not land on approval")
        print(f"  explicit {protected.author} api/interfaces.py landed after "
              f"{', '.join(author for _email, author in approvals)} approved", flush=True)
        if task2 == task3:
            fail("task ids collided")
        pause()

        beat(4, "The human hears once, at the end")

        def task_done():
            tasks = derive(refresh())
            found = next((item for item in tasks if item.id == task1 and item.status == "done"
                          and item.notified_at), None)
            return found

        finished = wait_until(task_done, limit, f"{task1} did not finish with a notify email")
        notes = []
        for email in refresh().emails:
            parsed = parse_body(email.body)
            if parsed.kind == "notify" and parsed.get("Gud-Task") == task1:
                notes.append(email)
        if not notes:
            fail(f"no notify email for {task1}")
        print(f"  {finished.id} done_link {finished.done_link} notified_at {finished.notified_at}", flush=True)
        print("--- requester email ---", flush=True)
        print(notes[0].body.rstrip(), flush=True)
        print("---", flush=True)
        pause()

        beat(5, "Two agents race on one parent")
        swarm.pause_and_wait()
        tip = refresh().tip
        if tip is None:
            fail("no tip before the race")
        parent_id = tip.message_id

        def push_new(handle: str, filename: str, content: str):
            ident = load_identity(cfg, handle)
            tree = dict(tip.tree)
            tree[filename] = content
            body, _state, _tree_hash = compose.push_body(
                ident, parent=tip.message_id, parent_state=tip.state, parent_tree=tip.tree,
                new_tree=tree, intent=f"race {filename}",
            )
            return compose.send(
                mailbox, ident, tip.members, f"[gud] race {filename}", body, in_reply_to=tip.message_id,
            )

        first = push_new("@sub-1", "race_a.py", "A = 1\n")
        second = push_new("@sub-2", "race_b.py", "B = 1\n")
        print(f"  {first.message_id} and {second.message_id} share parent {parent_id}", flush=True)
        if parse_body(first.body).get("Gud-Parent") != parse_body(second.body).get("Gud-Parent"):
            fail("race pushes do not share a parent")
        swarm.resume()

        def race_done():
            chain = refresh()
            tip_now = chain.tip
            if tip_now is None or "race_a.py" not in tip_now.tree or "race_b.py" not in tip_now.tree:
                return None
            superseded = [prop for prop in chain.proposals.values()
                          if prop.message_id in {first.message_id, second.message_id}
                          and prop.status == "superseded"]
            if not superseded:
                return None
            return superseded[0]

        loser = wait_until(race_done, limit, "the losing race push did not rebase onto the tip")
        print(f"  superseded {loser.author} {loser.message_id}: {loser.reason}", flush=True)
        print("  both race_a.py and race_b.py are on the tip", flush=True)
        pause()

        beat(6, "Membership is a diff")
        swarm.pause_and_wait()
        try:
            guest_ident, public = create_identity(cfg, "@guest-agent")
        except FileExistsError:
            guest_ident = load_identity(cfg, "@guest-agent")
            public = sig.public_of(guest_ident.private_raw)
        guest = Member("@guest-agent", "agent", cfg.address("@guest-agent"), public, "@nelson")
        console = load_identity(cfg, "@console")
        tip = refresh().tip
        added = members_mod.propose_add(mailbox, console, tip, guest, reason="guest for the demo")
        print(f"  @console proposes add @guest-agent {added.message_id}", flush=True)
        chain = land_quorum(added.message_id, limit)
        if "@guest-agent" not in chain.tip.members:
            fail("@guest-agent did not join")
        lead = load_identity(cfg, "@lead")
        welcome = members_mod.welcome(mailbox, lead, guest, chain.by_id[added.message_id])
        print(f"  welcome {welcome.message_id} to {guest.address}", flush=True)
        if "Checkpoint" not in welcome.body:
            fail("welcome email has no checkpoint")

        tip = refresh().tip
        removed = members_mod.propose_remove(mailbox, console, tip, "@sub-4", reason="end of the demo")
        print(f"  @console proposes remove @sub-4 {removed.message_id}", flush=True)
        chain = land_quorum(removed.message_id, limit)
        if "@sub-4" in chain.tip.members:
            fail("@sub-4 is still a member")
        if "@guest-agent" not in chain.tip.members:
            fail("removing @sub-4 dropped @guest-agent")
        owned = [(path, owner) for path, rows in chain.tip.blame.items() for owner, _link in rows
                 if owner == "@sub-4"]
        if not owned:
            fail("@sub-4 landed no lines, so removal cannot show orphaned blame")
        print(f"  @sub-4 removed; {len(owned)} blame lines stay, for example {owned[0][0]}", flush=True)

        swarm.stop()
        swarm = None
        tip = refresh().tip
        retired = load_identity(cfg, "@sub-4")
        tree = dict(tip.tree)
        tree["ghost.py"] = "x = 1\n"
        body, _state, _tree_hash = compose.push_body(
            retired, parent=tip.message_id, parent_state=tip.state, parent_tree=tip.tree,
            new_tree=tree, intent="push after removal",
        )
        ghost = compose.send(mailbox, retired, tip.members, "[gud] ghost push", body,
                             in_reply_to=tip.message_id)
        chain = refresh()
        proposal = chain.proposals.get(ghost.message_id)
        if proposal is None or proposal.status != "bad_sig":
            fail(f"removed key was not rejected ({None if proposal is None else proposal.status})")
        print(f"  push signed by @sub-4 is {proposal.status}: {proposal.reason}", flush=True)
        pause()

        beat(7, "The server is not the repo")
        emails = mailbox.list(recipient=mailbox.repo_address)
        now = time.time() + 5
        window = cfg.window_seconds
        first = replay(emails, window, now)
        second = replay(list(emails), window, now)
        if first.tip is None or second.tip is None:
            fail("replay lost the tip")
        if first.tip.state != second.tip.state or first.tip.blame != second.tip.blame:
            fail("replaying the same mailbox twice diverged")
        final_auth = first.tip.tree.get("auth.py", "")
        if "locked" not in final_auth or "rate limited" not in final_auth:
            fail("final tip lost the login rate limit or the lockout")
        if "class RateLimitDecision" not in first.tip.tree.get("api/interfaces.py", ""):
            fail("final tip lost RateLimitDecision")
        if "def penalize(" not in first.tip.tree.get("ratelimit.py", ""):
            fail("final tip lost penalize")
        if isinstance(mailbox, FileMailbox):
            fresh = FileMailbox(mailbox.path, mailbox.repo_address, mailbox.domain)
            third = replay(fresh.list(recipient=mailbox.repo_address), window, now)
            if third.tip.state != first.tip.state or third.tip.blame != first.tip.blame:
                fail("a fresh mailbox read did not rebuild the same tip")
        print(f"  replayed {len(emails)} emails, tip #{first.tip.seq} state {first.tip.state[:12]}, "
              f"blame identical", flush=True)
        pause()

        beat(8, "The number against git")
        evals = ROOT / "evals"
        if not evals.is_dir():
            fail("evals/ is missing")
        print("  Scorecard placeholder. Same tasks, gud versus a git MCP server:", flush=True)
        print(f"    {evals}", flush=True)
        print("    .venv/bin/python evals/run_local.py --dry-run", flush=True)
        print("    GitHub Enterprise: $21 per user per month. One hundred agents is $2,100 before Actions.",
              flush=True)
        print("    Primitive: $1 per 43,000 inbound emails and $1 per 43,000 outbound emails.", flush=True)
        print(f"\ndemo finished in {time.time() - started:.1f}s", flush=True)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        try:
            _dump(refresh())
        except Exception:
            pass
        raise SystemExit(1)
    finally:
        if swarm is not None:
            swarm.stop()


def _dump(chain) -> None:
    tip = chain.tip
    if tip is None:
        print("tip: none", flush=True)
    else:
        print(f"tip #{tip.seq} {tip.author} {tip.subject}", flush=True)
    for prop in sorted(chain.proposals.values(), key=lambda item: item.received_at):
        print(f"  {prop.status:15} {prop.author:16} {prop.subject}  {prop.reason}", flush=True)


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="demo.py")
    parser.add_argument("--interactive", action="store_true", help="wait for Enter between beats")
    parser.add_argument("--fast", action="store_true", help="2s consent window, no pauses")
    parser.add_argument("--llm", action="store_true", help="plan and code with OpenRouter instead of the script")
    return parser.parse_args(argv)


if __name__ == "__main__":
    main()
