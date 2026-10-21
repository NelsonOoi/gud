"""A live host on a FakeNetwork for event-pipeline tests.

    w = World(tmp_path)                        # FakeNetwork + host (intake, dispatcher, journal)
    alice = w.receiver("@alice")               # a real local HTTP webhook receiver, registered
    g = w.genesis({"app.py": "a = 1\\n"}, author="@alice")
    p = w.push("@bob", g, {"app.py": "a = 2\\n"})
    w.pump()
    assert alice.types() == ["review.requested", ...]

Members sign with the `tests.v1.kit.Kit` keys; every message is rendered with
`gud.transport.mime.build` and sent through the member's own `NetworkMailbox` to the repo
address, so it reaches the host exactly as mail would: push intake (`subscribe`), dedupe, host
log, incremental replay, journal, dispatch. Virtual time comes from the network clock.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from gud.core import envelope as E
from gud.events.dispatch import EventClient
from gud.events.model import Delivery
from gud.host import Host, open_host
from gud.transport import mime
from gud.transport.base import Outgoing
from gud.transport.fake import Faults, FakeNetwork
from tests.v1.kit import DEFAULT_MEMBERS, REPO, Kit, Msg

START = 1_760_000_000_000


class Receiver:
    """An agent's webhook endpoint: real HTTP on 127.0.0.1, `EventClient` for verify + dedupe.

    `mode`: "ok" (200), "down" (503, nothing processed), "flaky" (processes, then answers 500 so
    the host re-delivers: the dedupe path)."""

    def __init__(self, state: Path, host_keys: Callable[[], Any], clock: Callable[[], float]) -> None:
        self.client = EventClient(state, host_keys, repo=REPO)
        self.clock = clock
        self.mode = "ok"
        self.events: list = []                 # processed events, once each
        self.arrivals: dict[str, float] = {}   # event id -> perf_counter at first processing
        self.posts = 0
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with receiver.lock:
                    receiver.posts += 1
                    mode = receiver.mode
                if mode == "down":
                    self.send_response(503)
                    self.end_headers()
                    return
                event = receiver.client.accept_webhook(dict(self.headers.items()), body, receiver.clock())
                if event is not None:
                    with receiver.lock:
                        receiver.events.append(event)
                        receiver.arrivals.setdefault(event.id, time.perf_counter())
                self.send_response(500 if mode == "flaky" else 200)
                self.end_headers()

            def log_message(self, *args):  # silence
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def types(self) -> list[str]:
        with self.lock:
            return [e.type for e in self.events]

    def of(self, type: str) -> list:
        with self.lock:
            return [e for e in self.events if e.type == type]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class World:
    def __init__(self, tmp: Path, *, members=DEFAULT_MEMBERS, config: str = "", owners: str = "", test: str = "",
                 faults: Faults | None = None, persist_replay: bool = True, subscribe: bool = True,
                 dispatcher_kw: dict | None = None, intake_kw: dict | None = None) -> None:
        self.tmp = Path(tmp)
        self.net = FakeNetwork(start_ms=START, faults=faults)
        self.kit = Kit(members, config=config or "authserv-id fake.gud.invalid\n", owners=owners, test=test,
                       t0=START)
        self.repo_box = self.net.mailbox(REPO)
        self.persist_replay = persist_replay
        self.dispatcher_kw = {"email_batch_s": 5.0, **(dispatcher_kw or {})}
        self.intake_kw = intake_kw or {}
        self.receivers: dict[str, Receiver] = {}
        self.hops: list[tuple[str, str, float]] = []
        self.delivered: list[tuple[str, str, str, float]] = []
        self.sub = None
        self.host = self._open()
        if subscribe:
            self.subscribe()

    # ------------------------------------------------------------ host lifecycle

    def _open(self) -> Host:
        dk = dict(self.dispatcher_kw)
        dk.setdefault("on_delivered", lambda h, item, ch: self.delivered.append((h, item.event.id, ch, time.perf_counter())))
        ik = {"on_hop": lambda name, gid, t: self.hops.append((name, gid, t)), **self.intake_kw}
        return open_host(self.tmp / "home", repo=REPO, mailbox=self.repo_box, host_private=self.kit.host_key,
                         clock=self.net.clock, persist_replay=self.persist_replay, dispatcher_kw=dk, intake_kw=ik)

    def subscribe(self) -> None:
        self.sub = self.repo_box.subscribe(self.host.intake.on_received)

    def restart(self, *, save: bool = True) -> Host:
        """Stop the host (optionally without saving the replay cache) and open it again."""
        if self.sub is not None:
            self.sub.close()
            self.sub = None
        if save:
            self.host.save()
        self.host = self._open()
        self.subscribe()
        return self.host

    @property
    def intake(self):
        return self.host.intake

    @property
    def dispatcher(self):
        return self.host.dispatcher

    def chain(self):
        return self.host.replayer.chain()

    def pump(self) -> int:
        return self.host.dispatcher.pump()

    def advance(self, ms: int) -> None:
        """Move virtual time, deliver due mail, and tick the host log."""
        self.net.advance(ms)
        self.host.intake.tick()

    def close(self) -> None:
        if self.sub is not None:
            self.sub.close()
        for r in self.receivers.values():
            r.close()

    # ------------------------------------------------------------ members

    def receiver(self, handle: str, *, register: bool = True) -> Receiver:
        r = Receiver(self.tmp / f"client-{handle}.json", self.host.intake.host_keys, self.net.clock)
        self.receivers[handle] = r
        if register:
            self.host.registry.set(Delivery(handle, "webhook", r.url))
        return r

    def seen(self):
        return self.host.intake.seen()

    def send(self, msg: Msg, summary: str = "") -> Msg:
        env = msg.env
        assert env is not None
        author = env.author or "@nelson"
        out = Outgoing(from_addr=self.kit.address(author), to=(REPO,),
                       subject=mime.subject_for(env, msg.id, summary or env.kind or ""),
                       text=mime.render_human_part(env, msg.id), envelope=E.encode(env))
        self.net.mailbox(self.kit.address(author)).send(out)
        return msg

    def _do(self, name: str, *args, send: bool = True, **kw) -> Any:
        self.kit.now = self.net.now_ms
        kw.setdefault("log", False)
        if name != "blob":
            kw.setdefault("seen", self.seen())
        result = getattr(self.kit, name)(*args, **kw)
        if send:
            for msg in (result if isinstance(result, list) else [result]):
                self.send(msg)
        return result

    def __getattr__(self, name: str):
        if name in ("genesis", "push", "revert", "approve", "hold", "object", "veto", "comment", "test", "test_push",
                    "test_tip", "task", "subtask", "claim", "no_change", "checkpoint"):
            return lambda *a, **kw: self._do(name, *a, **kw)
        raise AttributeError(name)

    def unsigned(self, who: str, text: str, *, subject: str = "hello", in_reply_to: str | None = None) -> bytes:
        """A plain human email from `who`'s own mailbox (the fake MTA adds dmarc=pass)."""
        out = Outgoing(from_addr=self.kit.address(who), to=(REPO,), subject=subject, text=text,
                       in_reply_to=in_reply_to)
        self.net.mailbox(self.kit.address(who)).send(out)
        return mime.build(out)
