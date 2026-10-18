"""The tester agent. A push is tested once its parent is a link; a pass reply is what lets it land.

A root push (`Gud-Parent: none`) is tested only while replay has not accepted it as a link.

One pass of `step` replays the repo mailbox, runs untested pushes whose diff applies and whose
tree hash matches, and sends a signed `test` email. It repeats until a reply cannot unlock
any further child. `run_forever` polls the tester's own inbox between passes.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

from . import compose
from .build import RunnerPool
from .inbox import Gate, Inbox, next_window_close
from .memberfile import members_of
from .patch import PatchError, apply_diff
from .types import Email, Mailbox, Tree
from .wire import parse_body, tree_hash


@dataclass
class _Candidate:
    message_id: str
    subject: str
    members: dict
    tree: Tree
    tree_hash: str
    author: str = ""


# A push in one of these states can never link, so testing it is wasted work.
_DEAD = {"superseded", "stale", "conflict", "bad_sig", "vetoed"}


class Tester:
    def __init__(self, mailbox: Mailbox, identity: compose.Identity, runner, window: float,
                 clock=time.time, *, max_concurrent: int = 5, gate: Gate | None = None):
        self.mailbox = mailbox
        self.identity = identity
        self.runner = runner
        self.window = window
        self.clock = clock
        self.pool = RunnerPool(runner, max_concurrent=max_concurrent)
        self.inbox = Inbox(mailbox, identity.address, mailbox.repo_address)
        self.gate = gate or Gate()
        # Pushes this tester answered, kept across steps: its own replies reach the repo later.
        self._answered_ids: set[str] = set()
        self._waiting = False
        self._window_at: float | None = None

    def step(self) -> list[Email]:
        """Test every push that is ready. Returns the `test` emails sent, oldest work first."""
        from .replay import replay

        mail = self.inbox.read()
        if not self.gate.due(bool(mail), self._waiting, self._window_at):
            return []
        sent: list[Email] = []
        answered = self._answered_ids
        while True:
            emails = self.mailbox.list(recipient=self.mailbox.repo_address)
            answered |= self._answered(emails)
            chain = replay(emails, self.window, self.clock())
            self._window_at = next_window_close(chain)
            work = [job for job in self._candidates(emails, chain) if job.message_id not in answered]
            self._waiting = self._untested_children(emails, chain, answered)
            if not work:
                return sent
            futs = [(job, self.pool.submit(job.tree)) for job in work]
            results = []
            errors: list[Exception] = []
            for job, fut in futs:
                try:
                    results.append((job, fut.result()))
                except Exception as exc:
                    errors.append(exc)
            for job, result in results:
                body = compose.test_body(
                    self.identity, target=job.message_id, passed=result.passed,
                    tree=job.tree_hash, log=result.log,
                )
                email = compose.send(
                    self.mailbox, self.identity, job.members, "Re: " + job.subject, body,
                    in_reply_to=job.message_id, cc_handles=compose.reply_cc(job.members, job.author),
                )
                sent.append(email)
                answered.add(job.message_id)
                status = "pass" if result.passed else "fail"
                print(
                    f"test {self.identity.handle} {status} {job.message_id} "
                    f"{job.tree_hash[:12]} {result.seconds:.2f}s",
                    flush=True,
                )
            if errors:
                raise errors[0]

    def run_forever(self, poll_timeout: float = 30) -> None:
        """`step`, then long-poll the tester's inbox. Returns on Ctrl-C."""
        last_seen = self._high_water()
        try:
            while True:
                self.step()
                fresh = self.mailbox.wait(recipient=self.identity.address, since=last_seen,
                                          timeout=poll_timeout)
                if fresh:
                    last_seen = max(last_seen, max(item.received_at for item in fresh))
        except KeyboardInterrupt:
            return
        finally:
            self.close()

    def close(self) -> None:
        closer = getattr(self.runner, "close", None)
        if closer is not None:
            closer()
        self.pool.shutdown(wait=False)

    def _high_water(self) -> float:
        existing = self.mailbox.list(recipient=self.identity.address)
        if not existing:
            return 0.0
        return existing[-1].received_at

    def _answered(self, emails: list[Email]) -> set[str]:
        found: set[str] = set()
        author = self.identity.handle
        sender = self.identity.address.lower()
        for email in emails:
            if email.from_addr.lower() != sender:
                continue
            parsed = parse_body(email.body)
            if parsed.kind != "test" or parsed.get("Gud-Author") != author:
                continue
            target = parsed.get("Gud-Target")
            if target:
                found.add(target)
        return found

    def _candidates(self, emails: list[Email], chain) -> list[_Candidate]:
        work: list[_Candidate] = []
        for email in emails:
            parsed = parse_body(email.body)
            if parsed.kind != "push":
                continue
            parent_id = parsed.get("Gud-Parent") or ""
            claimed = parsed.get("Gud-Tree") or ""
            if not claimed:
                continue
            if parent_id in ("", "none"):
                # Root push. It has no parent link. Test it only when replay has not
                # already accepted it, so a core that demands a pass on genesis can start.
                if _is_link(chain, email.message_id):
                    continue
                base, members = {}, None
            else:
                parent = _parent_link(chain, parent_id)
                if parent is None:
                    continue
                prop = (getattr(chain, "proposals", None) or {}).get(email.message_id)
                if prop is not None and prop.status in _DEAD:
                    continue
                base, members = dict(parent.tree), parent.members
            try:
                tree = apply_diff(base, parsed.diff)
            except PatchError:
                continue
            if tree_hash(tree) != claimed:
                continue
            work.append(_Candidate(
                message_id=email.message_id, subject=email.subject,
                members=members if members is not None else members_of(tree),
                tree=tree, tree_hash=claimed, author=parsed.get("Gud-Author") or "",
            ))
        return work

    def _untested_children(self, emails: list[Email], chain, answered: set[str]) -> bool:
        """True while a push waits on its parent linking: the tester has to look again soon."""
        for email in emails:
            parsed = parse_body(email.body)
            if parsed.kind != "push" or email.message_id in answered:
                continue
            parent_id = parsed.get("Gud-Parent") or ""
            if parent_id in ("", "none") or _is_link(chain, email.message_id):
                continue
            prop = (getattr(chain, "proposals", None) or {}).get(email.message_id)
            if prop is None or prop.status not in _DEAD:
                return True
        return False


def _is_link(chain, message_id: str) -> bool:
    by_id = getattr(chain, "by_id", None) or {}
    if message_id in by_id:
        return True
    return any(link.message_id == message_id for link in getattr(chain, "links", ()) or ())


def _parent_link(chain, parent_id: str):
    """The parent link, from `by_id` or, if that map missed it, from `links`."""
    if not parent_id or parent_id == "none":
        return None
    by_id = getattr(chain, "by_id", None) or {}
    if parent_id in by_id:
        return by_id[parent_id]
    for link in getattr(chain, "links", ()) or ():
        if link.message_id == parent_id:
            return link
    return None


def main(argv: list[str] | None = None) -> None:
    """``python -m gud.tester --handle @tester``"""
    from . import config, sig
    from .build import make_runner

    parser = argparse.ArgumentParser(prog="gud.tester")
    parser.add_argument("--handle", default="@tester")
    args = parser.parse_args(argv)
    cfg = config.load()
    handle = args.handle if str(args.handle).startswith("@") else f"@{args.handle}"
    key_path = cfg.key_path(handle)
    if not key_path.exists():
        sys.exit(f"no private key at {key_path}; run `gud init` first")
    from .mailbox import open_member_mailbox  # written alongside the core; imported only when the CLI runs

    identity = compose.Identity(handle, cfg.address(handle), sig.load_private(key_path))
    Tester(open_member_mailbox(cfg), identity, make_runner(cfg), cfg.window_seconds).run_forever()


if __name__ == "__main__":
    main()
