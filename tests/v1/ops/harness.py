"""A small end-to-end world for the ops tests: a FakeNetwork, a host that logs real MIME mail
from the repo mailbox into a signed host log, and member clients that sign, send through their
own mailboxes and replay what the mirror serves. Members, keys and repo files come from
`tests/v1/kit.py`.

`Host` and `View` implement the mirror surface the ops modules read (`log_entries`, `raw`);
`Client` implements the member-client surface (`handle`, `reader()`, `replayer()`,
`sign_and_send(...)`), standing in for `gud.wc.remote.RepoClient`.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from gud.core import envelope as E
from gud.core.hostlog import HostLog
from gud.core.objects import MemoryStore, Tree
from gud.core.ops import diff_trees, ops_to_records
from gud.core.replay import Replayer
from gud.core.types import ZERO, Hex, LogEntry, Record, Seen
from gud.ops.archive import read_log
from gud.transport import mime
from gud.transport.base import Outgoing
from gud.transport.fake import FakeNetwork

from tests.v1.kit import DEFAULT_MEMBERS, DOMAIN, REPO, T0, Kit, _key

HOST_KEY = _key("@host")


def address(handle: str) -> str:
    return f"{handle.lstrip('@')}@{DOMAIN}"


class Host:
    """Logs every decodable message delivered to the repo mailbox (first copy wins)."""

    def __init__(self, net: FakeNetwork, *, key: bytes = HOST_KEY, log: HostLog | None = None,
                 store: dict[Hex, bytes] | None = None, repo: str = REPO) -> None:
        self.net, self.key, self.repo = net, key, repo
        self.mailbox = net.mailbox(repo)
        self.log = log if log is not None else HostLog.open(repo, key, net.now_ms)
        self.store: dict[Hex, bytes] = store if store is not None else {}
        self.cursor: str | None = None

    def pump(self, *, skip: Sequence[Hex] = (), retime_ms: int = 0) -> list[Hex]:
        """Log what arrived. `skip`: ids withheld (never logged); `retime_ms`: claim later times."""
        items, self.cursor = self.mailbox.list(since=self.cursor, limit=1_000_000)
        logged = []
        for item in items:
            email = mime.to_email(item.raw_mime)
            if email is None or email.gud_id in skip or self.log.seq_of(email.gud_id) is not None:
                continue
            when = max(item.received_at + retime_ms, self.log.head.time + 1)
            self.log.append_mail(email.gud_id, when, self.key, max(self.net.now_ms, when))
            self.store[email.gud_id] = item.raw_mime
            logged.append(email.gud_id)
        return logged

    def tick(self) -> LogEntry:
        return self.log.append_tick(self.key, max(self.net.now_ms, self.log.head.time + 1))

    # mirror surface
    def log_entries(self, since: int, limit: int) -> list[LogEntry]:
        return self.log.entries(since, limit)

    def raw(self, gud_id: Hex) -> bytes | None:
        return self.store.get(gud_id)


class View:
    """What a (possibly lying) mirror serves one member: its own log, a shared mail store."""

    def __init__(self, log: HostLog, store: Mapping[Hex, bytes], repo: str = REPO) -> None:
        self.log, self.store, self.repo = log, store, repo

    def log_entries(self, since: int, limit: int) -> list[LogEntry]:
        return self.log.entries(since, limit)

    def raw(self, gud_id: Hex) -> bytes | None:
        return self.store.get(gud_id)


class Client:
    def __init__(self, net: FakeNetwork, mirror, handle: str, *, key: bytes | None = None) -> None:
        self.net, self.mirror, self.handle = net, mirror, handle
        self.private = key if key is not None else _key(handle)
        self.address = address(handle)
        self.mailbox = net.mailbox(self.address)
        self.store = MemoryStore()
        self._r = Replayer(self.store)

    def sync(self, now: int | None = None) -> None:
        entries = read_log(self.mirror)
        emails = []
        for entry in entries:
            if entry.type == "mail":
                raw = self.mirror.raw(entry.field("id"))
                email = mime.to_email(raw) if raw is not None else None
                if email is not None:
                    emails.append(email)
        self._r.feed(emails, entries, self.net.now_ms if now is None else now)

    # mirror surface: what the host serves this member now (a RepoClient fetches it over HTTP)
    def log_entries(self, since: int, limit: int) -> list[LogEntry]:
        return self.mirror.log_entries(since, limit)

    def raw(self, gud_id: Hex) -> bytes | None:
        return self.mirror.raw(gud_id)

    def replayer(self) -> Replayer:
        return self._r

    def reader(self):
        return self._r.reader()

    def chain(self):
        return self._r.chain()

    def seen(self) -> Seen:
        head = self._r.log().head
        assert head is not None, "sync first"
        return Seen(head.seq, head.hash)

    def sign_and_send(self, kind: str, headers: Mapping[str, str], records: Sequence[Record] = (), *,
                      summary: str, in_reply_to: Hex | None = None, cc: Sequence[str] = (),
                      key: bytes | None = None, seen: Seen | None = None) -> Hex:
        env = E.build(kind, author=self.handle, headers=dict(headers), records=records,
                      sent=self.net.now_ms // 1000, seen=seen or self.seen())
        assert E.validate_kind(env) is None, E.validate_kind(env)
        env = E.sign(env, key if key is not None else self.private)
        gid = E.gud_id(env)
        self.mailbox.send(Outgoing(
            from_addr=self.address, to=(REPO,), cc=tuple(cc), subject=mime.subject_for(env, gid, summary),
            text=mime.render_human_part(env, gid), envelope=E.encode(env)))
        return gid

    # convenience builders
    def push(self, edits: Mapping[str, bytes | None], *, intent: str = "change", parent: Hex | None = None,
             **kw) -> Hex:
        reader = self.reader()
        base = reader.tree(parent)
        new = base
        for path in sorted(edits):
            new = new.delete(path) if edits[path] is None else new.set(path, "100644", edits[path])
        ops = diff_trees(base, new, inline_budget=1 << 30)
        state = parent or reader.chain.tip.state
        records = (Record("text", (), intent.encode()),) + ops_to_records(ops)
        return self.sign_and_send("push", {"Gud-Parent": state, "Gud-Tree": new.hash()}, records,
                                  summary=intent, **kw)

    def genesis(self, files: Mapping[str, bytes]) -> Hex:
        tree = Tree.from_files(self.store, {p: ("100644", b) for p, b in files.items()})
        ops = diff_trees(Tree.empty(self.store), tree, inline_budget=1 << 30)
        records = (Record("text", (), b"genesis"),) + ops_to_records(ops)
        return self.sign_and_send("push", {"Gud-Parent": ZERO, "Gud-Tree": tree.hash()}, records,
                                  summary="genesis")

    def approve(self, target: Hex, **kw) -> Hex:
        return self.sign_and_send("approve", {"Gud-Target": target}, summary="approve", **kw)


class World:
    """Host + one client per kit member, on one FakeNetwork. `step` moves time, logs mail,
    ticks and lets every client sync."""

    def __init__(self, members=DEFAULT_MEMBERS, *, genesis: Mapping[str, bytes] | None = None) -> None:
        self.kit = Kit(members)
        self.net = FakeNetwork(start_ms=T0)
        self.host = Host(self.net)
        self.clients = {h: Client(self.net, self.host, h) for h, _, _ in members}
        if genesis is not None:
            for c in self.clients.values():
                c.sync()
            files = {**self.kit.gud_files(), **genesis}
            self.genesis_id = self.clients["@nelson"].genesis(files)
            self.step()

    def __getitem__(self, handle: str) -> Client:
        return self.clients[handle]

    def step(self, ms: int = 1000, **pump) -> list[Hex]:
        self.net.advance(ms)
        logged = self.host.pump(**pump)
        self.net.advance(1)
        self.host.tick()
        self.sync()
        return logged

    def sync(self) -> None:
        for client in self.clients.values():
            client.sync()

    def tip(self, handle: str = "@nelson"):
        return self.clients[handle].chain().tip


def linked(client: Client, push: Hex) -> bool:
    return any(link.push == push for link in client.chain().links)


def status(client: Client, push: Hex) -> str:
    if linked(client, push):
        return "linked"
    prop = client.chain().push_status(push)
    return prop.status if prop is not None else "unknown"
