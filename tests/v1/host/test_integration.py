"""Host regressions found while integrating the v1 layers (members clone and sync against the
real host, not a double)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from gud.core import envelope as E
from gud.core.objects import BlameMap
from gud.core.replay import Snapshot, snapshot_at
from gud.core.types import Seen
from gud.host_v1 import sign_request
from gud.transport import mime
from tests.v1.events.harness import World


def _client(w: World) -> TestClient:
    return TestClient(w.host.app(loopback_hosts=frozenset()))


def _get(c: TestClient, w: World, handle: str, path: str):
    return c.get(path, headers=sign_request(handle, w.kit.key(handle), "GET", path, b"", w.net.clock()))


def test_mirror_is_open_before_genesis(tmp_path):
    """Before genesis nobody can sign a mirror request, but the genesis author needs the log head
    for its Gud-Seen: reads are open until genesis links, then signatures are required."""
    w = World(tmp_path)
    try:
        c = _client(w)
        page = c.get("/api/v1/log?since=0")
        assert page.status_code == 200 and len(page.json()["entries"]) == len(w.intake.log)
        assert c.get("/api/v1/mail?since=0").status_code == 200
        w.genesis({"app.py": "a = 1\n"}, author="@alice")
        assert w.chain().tip is not None
        assert c.get("/api/v1/log?since=0").status_code == 401
        assert _get(c, w, "@bob", "/api/v1/log?since=0").status_code == 200
    finally:
        w.close()


def test_snapshot_named_by_a_logged_checkpoint_is_served(tmp_path):
    """Clone from a checkpoint (§17) against the real host: the snapshot a logged checkpoint names
    is computed on request, and tree and blame nodes are served from the object store."""
    w = World(tmp_path)
    try:
        w.genesis({"app.py": "a = 1\n"}, author="@alice")
        head = w.intake.head()
        store = w.host.replayer.reader().store
        snap = snapshot_at(w.intake.log_entries(0, head.seq + 1), lambda gid: mime.to_email(w.intake.raw(gid)),
                           head.seq, store)
        sha = E.sha256_hex(snap)
        tip = w.chain().tip
        c = _client(w)
        assert _get(c, w, "@carol", f"/api/v1/snapshot/{sha}").status_code == 404     # not named yet
        headers = {"Gud-Log": Seen(head.seq, head.hash).render(), "Gud-Link": tip.state, "Gud-Tree": tip.tree_hash,
                   "Gud-Snapshot": sha}
        for who in ("@nelson", "@alice", "@bob"):
            w.kit.now = w.net.now_ms
            w.send(w.kit.sign("checkpoint", who, headers, log=False, seen=w.seen()))
        got = _get(c, w, "@carol", f"/api/v1/snapshot/{sha}")
        assert got.status_code == 200 and got.content == snap
        assert _get(c, w, "@carol", "/api/v1/snapshot/" + "0" * 64).status_code == 404
        last = Snapshot.from_bytes(snap).state["links"][-1]
        for node in (last["tree_root"], last["blame_root"]):
            r = _get(c, w, "@carol", f"/api/v1/object/{node}")
            assert r.status_code == 200 and E.sha256_hex(r.content) == node
        assert BlameMap.load(store, last["blame_root"]).get("app.py")
        # the index survives a host restart
        w.restart()
        got = _get(_client(w), w, "@carol", f"/api/v1/snapshot/{sha}")
        assert got.status_code == 200 and got.content == snap
    finally:
        w.close()
