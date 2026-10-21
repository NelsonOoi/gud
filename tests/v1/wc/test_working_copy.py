"""Working copy end to end against the real v1 host (gud.host; SPEC-v1 §21, §22)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from gud.wc.copy import BASE_FILE, SecretsFound, WcError, WorkingCopy, conflict_markers

from tests.v1.kit import Kit, link_of, status
from tests.v1.wc.conftest import Net

APP = "def f():\n    return 1\n\nx = 1\n"


def _repo_count(net: Net) -> int:
    return len(net.host.mailbox.list()[0])


def _link(net: Net, handle: str, push: str) -> None:
    """Get `push` linked: the genesis author owns every line, so @nelson approves."""
    net.settle()
    net.host.approve("@nelson", push)
    net.settle()
    c = net.client(handle)
    c.sync_log()
    assert status(c.chain(), push) == "linked", c.chain().push_status(push)


def test_clone_edit_push_link_second_member_syncs(net: Net):
    g = net.genesis({"app.py": APP, "README.md": "hello\n"})
    alice = net.clone("@alice")
    bob = net.clone("@bob")
    assert alice.base == g == bob.base
    assert (alice.root / "app.py").read_text() == APP
    assert (alice.root / BASE_FILE).read_text() == g + "\n"

    (alice.root / "app.py").write_text(APP.replace("x = 1", "x = 2"))
    st = alice.status()
    assert st.modified == ("app.py",) and st.behind == 0 and not st.clean
    assert "-x = 1" in alice.diff() and "+x = 2" in alice.diff()

    result = alice.push("x is two")
    assert result.parent == g and result.ops == 1 and result.blobs == 0
    assert result.forecast is not None and result.forecast.askers == ("@nelson",)
    net.settle()
    alice.client.sync_log()
    assert status(alice.client.chain(), result.push) == "window"      # waits for its owner
    _link(net, "@alice", result.push)

    sync = bob.sync()
    assert sync.ok and sync.new_base == alice.client.chain().tip.state and sync.written == ("app.py",)
    assert (bob.root / "app.py").read_text() == APP.replace("x = 1", "x = 2")
    assert bob.base == sync.new_base and bob.status().clean
    # alice's base catches up without writing anything
    assert alice.fast_forward() and alice.base == sync.new_base and alice.status().clean
    assert alice.status().pushes == ((result.push, "linked"),)


def test_concurrent_edit_sync_conflict_nothing_pushed(net: Net):
    net.genesis({"app.py": APP})
    alice, bob = net.clone("@alice"), net.clone("@bob")
    (alice.root / "app.py").write_text(APP.replace("x = 1", "x = 2"))
    _link(net, "@alice", alice.push("two").push)

    mine = APP.replace("x = 1", "x = 3")
    (bob.root / "app.py").write_text(mine)
    before = _repo_count(net)
    with pytest.raises(WcError, match="gud sync"):
        bob.push("three")
    assert _repo_count(net) == before                    # nothing was sent

    base = bob.base
    result = bob.sync()
    assert not result.ok and result.new_base == base
    assert len(result.conflicts) == 1 and result.conflicts[0].startswith("app.py: ")
    assert (bob.root / "app.py").read_text() == mine     # nothing written
    assert bob.base == base
    st = bob.status()
    assert st.conflicts == result.conflicts and st.behind == 1 and st.modified == ("app.py",)
    marker = (bob.root / result.markers[0]).read_text()
    assert "<<<<<<< mine\n" in marker and "x = 3\n" in marker and "x = 2\n" in marker and ">>>>>>> tip" in marker
    assert marker.startswith("def f():\n")               # clean parts merged
    assert _repo_count(net) == before

    # resolving by taking the tip's line makes the next sync succeed
    (bob.root / "app.py").write_text(APP.replace("x = 1", "x = 2"))
    assert bob.sync().ok and bob.status().conflicts == () and bob.status().clean


def test_disjoint_edits_merge_and_carry(net: Net):
    net.genesis({"a.txt": "a\n", "b.txt": "b\n"})
    alice, bob = net.clone("@alice"), net.clone("@bob")
    (alice.root / "a.txt").write_text("a\nmore\n")
    p = alice.push("a").push
    net.settle()
    alice.client.sync_log()
    assert status(alice.client.chain(), p) == "linked"   # appended lines have no owner
    (bob.root / "b.txt").write_text("b\nbob\n")
    (bob.root / "new.txt").write_text("new\n")
    # bob is behind but touches other files: the push is allowed and carried
    q = bob.push("b").push
    net.settle()
    bob.client.sync_log()
    assert status(bob.client.chain(), q) == "linked"
    link = link_of(bob.client.chain(), q)
    assert link.carried_from == bob.base
    # sync merges the tip into bob's copy and keeps his files
    r = bob.sync()
    assert r.ok and (bob.root / "a.txt").read_text() == "a\nmore\n" and bob.status().clean


def test_unsynced_local_edit_survives_sync(net: Net):
    net.genesis({"a.txt": "a\n", "b.txt": "b\n"})
    alice, bob = net.clone("@alice"), net.clone("@bob")
    (alice.root / "a.txt").write_text("a\nalice\n")
    alice.push("a")
    net.settle()
    (bob.root / "b.txt").write_text("b\nlocal only\n")
    r = bob.sync()
    assert r.ok and r.written == ("a.txt",)
    assert (bob.root / "b.txt").read_text() == "b\nlocal only\n"
    assert bob.status().modified == ("b.txt",)


def test_gudignore_paths_never_pushed(net: Net):
    net.genesis({".gudignore": "*.log\nbuild/\n!keep.log\n", "src.py": "x\n"})
    alice = net.clone("@alice")
    (alice.root / "debug.log").write_text("noise\n")
    (alice.root / "keep.log").write_text("kept\n")
    (alice.root / "build").mkdir()
    (alice.root / "build" / "out.bin").write_bytes(b"\0\1")
    (alice.root / "sub").mkdir()
    (alice.root / "sub" / "x.log").write_text("noise\n")
    (alice.root / ".git").mkdir()
    (alice.root / ".git" / "HEAD").write_text("ref\n")
    assert alice.status().added == ("keep.log",)
    result = alice.push("add keep")
    assert result.files == ("keep.log",)
    net.settle()
    c = alice.client
    c.sync_log()
    tip = c.reader().tree()
    assert "keep.log" in tip and "debug.log" not in tip and "build/out.bin" not in tip
    assert not any(p.startswith((".git", ".gud/local", "sub/")) or p == ".gud/BASE" for p in tip.paths())


def test_secret_scan_blocks_aws_key(net: Net):
    net.genesis({"src.py": "x\n"})
    alice = net.clone("@alice")
    (alice.root / "settings.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    before = _repo_count(net)
    with pytest.raises(SecretsFound) as exc:
        alice.push("settings")
    assert [(h.path, h.line, h.pattern) for h in exc.value.hits] == [("settings.py", 1, "aws-access-key-id")]
    assert _repo_count(net) == before
    result = alice.push("settings (example key)", allow_secret=["settings.py"])
    assert result.files == ("settings.py",)


def _snapshot_dir(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir.split(os.sep)[0] == ".gud" and (rel_dir == ".gud/local" or rel_dir.startswith(".gud/local")):
            continue
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if rel.startswith(".gud/local") or rel == ".gud/BASE":
                continue
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(full))
            else:
                out[rel] = (oct(st.st_mode & 0o777), Path(full).read_bytes())
    return out


def test_byte_exact_round_trip_through_push_and_clone(tmp_path: Path):
    net = Net(tmp_path, max_message_bytes=200_000)      # budget about 98 kB: the big file goes by blobs
    net.genesis({"README": "x\n"})
    alice = net.clone("@alice")
    big = bytes(range(256)) * 1200 + b"\0tail"           # 307 kB binary, several blob chunks
    files = {
        "bin/data.bin": b"\x00\xff\xfe\r\n\n\x00 binary \x80\x81",
        "crlf.txt": b"one\r\ntwo\r\nthree\r\n",
        "lone-cr.txt": b"a\rb\rc",
        "no-final-newline.txt": b"last line",
        "nested/deep/dir/file.py": b"print('hi')\n",
        "big.bin": big,
        "empty": b"",
    }
    for rel, data in files.items():
        p = alice.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    script = alice.root / "run.sh"
    script.write_bytes(b"#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    os.symlink("nested/deep/dir/file.py", alice.root / "link.py")
    os.symlink("../README", alice.root / "nested" / "up")
    result = alice.push("round trip")
    assert result.blobs == 1
    net.settle()
    alice.client.sync_log()
    assert status(alice.client.chain(), result.push) == "linked"
    tip = alice.client.reader().tree()
    assert tip.get("run.sh").mode == "100755" and tip.get("link.py").mode == "120000"
    assert tip.read("link.py") == b"nested/deep/dir/file.py" and tip.read("big.bin") == big

    bob = net.clone("@bob")
    assert _snapshot_dir(bob.root) == _snapshot_dir(alice.root)
    assert (bob.root / "crlf.txt").read_bytes() == files["crlf.txt"]
    assert os.stat(bob.root / "run.sh").st_mode & 0o111
    assert os.readlink(bob.root / "link.py") == "nested/deep/dir/file.py"
    assert bob.working_tree().hash() == tip.hash()

    # mode-only change, file -> directory, and symlink retarget round trip through sync
    (alice.root / "run.sh").chmod(0o644)
    os.unlink(alice.root / "link.py")
    os.symlink("README", alice.root / "link.py")
    os.unlink(alice.root / "empty")
    (alice.root / "empty").mkdir()
    (alice.root / "empty" / "now-a-dir").write_bytes(b"\r\n")
    alice.fast_forward()
    r2 = alice.push("modes")
    net.settle()
    assert bob.sync().ok
    assert not os.stat(bob.root / "run.sh").st_mode & 0o111
    assert os.readlink(bob.root / "link.py") == "README"
    assert (bob.root / "empty" / "now-a-dir").read_bytes() == b"\r\n"
    assert _snapshot_dir(bob.root) == _snapshot_dir(alice.root)
    assert r2.ops >= 3


def test_materialize_never_writes_through_symlinks(net: Net, tmp_path: Path):
    net.genesis({"README": "x\n"})
    alice = net.clone("@alice")
    outside = tmp_path / "outside"
    outside.mkdir()
    tree = alice.client.reader().tree().set("sub/file.txt", "100644", b"inside\n")
    os.symlink(outside, alice.root / "sub")              # an attacker-planted link
    alice.materialize(tree)
    assert list(outside.iterdir()) == []
    assert not (alice.root / "sub").is_symlink()
    assert (alice.root / "sub" / "file.txt").read_bytes() == b"inside\n"


def test_verify_detects_tampered_cache(net: Net):
    net.genesis({"a.txt": "a\n"})
    alice = net.clone("@alice")
    (alice.root / "a.txt").write_text("a\nb\n")
    alice.push("b")
    net.settle()
    assert alice.verify() == []
    assert alice.verify(from_checkpoint=False) == []

    state_path = alice.client.cache_dir / "state.json"
    doc = json.loads(state_path.read_text())
    doc["engine"]["links"][-1]["intent"] = "forged"
    doc["engine"]["links"][-1]["author"] = "@bob"
    state_path.write_text(json.dumps(doc))
    tampered = WorkingCopy(alice.root, net.client("@alice", fresh=True))
    diffs = tampered.verify()
    assert any("link 1" in d and "author" in d for d in diffs), diffs

    # a tampered object (the bytes of the tip's file) is caught too
    clean = net.client("@alice", fresh=True, home=net.tmp / "homes" / "alice2")
    clean.sync_log()
    entry = clean.reader().tree().get("a.txt")
    obj = net.tmp / "homes" / "alice2" / "objects" / entry.blob[:2] / entry.blob[2:]
    obj.chmod(0o644)
    obj.write_bytes(b"evil\n")
    from gud.wc.copy import verify_cache
    assert any("a.txt" in d and "do not match" in d for d in verify_cache(clean))


def test_clone_from_checkpoint(net: Net):
    net.genesis({"a.txt": "a\n"})
    alice = net.clone("@alice")
    for i in range(3):
        (alice.root / f"f{i}.txt").write_text(f"{i}\n")
        alice.fast_forward()
        alice.push(f"file {i}")
        net.settle()
    alice.client.sync_log()
    head = alice.client.replayer().log().head.seq
    snap = alice.client.snapshot_at(head)
    from gud.core.envelope import sha256_hex
    from gud.core.replay import Snapshot
    link = Snapshot.from_bytes(snap).state["links"][-1]
    entry = alice.client.replayer().log().at(head)
    headers = {"Gud-Log": f"{head}:{entry.hash}", "Gud-Link": link["state"], "Gud-Tree": link["tree_hash"],
               "Gud-Snapshot": sha256_hex(snap)}
    for who in ("@nelson", "@alice", "@bob"):                 # 3 of the 5 voters
        c = net.client(who)
        c.sync_log()
        c.sign_and_send("checkpoint", headers, summary="cp")
    net.settle()
    (alice.root / "after.txt").write_text("after\n")
    alice.fast_forward()
    alice.push("after the checkpoint")
    net.settle()

    carol = net.client("@carol")
    assert carol.bootstrap(checkpoint=True) == "checkpoint"
    wc = WorkingCopy.clone(carol, net.tmp / "wc" / "carol")
    assert (wc.root / "after.txt").read_text() == "after\n" and (wc.root / "f0.txt").exists()
    alice.client.sync_log()
    assert carol.chain().links == alice.client.chain().links
    assert wc.verify(from_checkpoint=True) == []
    assert wc.verify(from_checkpoint=False) == []
    # the cache keeps working after a restart (objects older than the checkpoint come from the mirror)
    again = net.client("@carol", fresh=True)
    assert again.chain().links == carol.chain().links
    assert again.reader().tree(again.chain().links[1].state).read("f0.txt") == b"0\n"


def test_mirror_auth_rejects_bad_signatures(net: Net):
    net.genesis({"a.txt": "a\n"})
    from fastapi.testclient import TestClient
    tc = TestClient(net.host.app(), base_url="http://testserver")
    assert tc.get("/api/v1/info").status_code == 200
    assert tc.get("/api/v1/log?since=0&limit=5").status_code == 401
    from gud.wc.remote import sign_request
    good = sign_request("@alice", net.kit.key("@alice"), "GET", "/api/v1/log?since=0&limit=5", b"", net.clock())
    assert tc.get("/api/v1/log?since=0&limit=5", headers=good).status_code == 200
    wrong_path = tc.get("/api/v1/log?since=1&limit=5", headers=good)
    assert wrong_path.status_code == 401
    forged = sign_request("@alice", net.kit.key("@bob"), "GET", "/api/v1/log?since=0&limit=5", b"", net.clock())
    assert tc.get("/api/v1/log?since=0&limit=5", headers=forged).status_code == 401
    stale = sign_request("@alice", net.kit.key("@alice"), "GET", "/api/v1/log?since=0&limit=5", b"", net.clock() - 301)
    assert tc.get("/api/v1/log?since=0&limit=5", headers=stale).status_code == 401
    outsider = sign_request("@mallory", net.kit.key("@mallory"), "GET", "/api/v1/log?since=0&limit=5", b"", net.clock())
    assert tc.get("/api/v1/log?since=0&limit=5", headers=outsider).status_code == 401
    # POST bodies are covered by the signature
    body = b'{"sse": true}'
    signed = sign_request("@alice", net.kit.key("@alice"), "POST", "/api/v1/delivery", body, net.clock())
    assert tc.post("/api/v1/delivery", content=body, headers=signed).status_code == 200
    assert tc.post("/api/v1/delivery", content=b'{"email": true}', headers=signed).status_code == 401
    # the client signs every request, so its reads succeed
    c = net.client("@bob")
    c.sync_log()
    c.set_delivery({"sse": True})
    assert net.host.deliveries["@bob"] == {"sse": True}
    assert ("GET", "/api/v1/log?since=0&limit=500", "@bob") in net.host.requests


def test_sync_rejects_a_forged_log(net: Net):
    net.genesis({"a.txt": "a\n"})
    c = net.client("@alice")
    c.sync_log()
    # the host rewrites history: a different log signed by another key
    other = Kit()
    other.host_key = b"\x01" * 32
    net.host.forge_log(other.host_key)
    net.clock.advance(2000)
    from gud.wc.remote import RemoteError
    with pytest.raises(RemoteError, match="rolled its log back|fork"):
        c.sync_log()
    for _ in range(6):                                    # a longer forged history does not chain either
        net.clock.advance(2000)
        net.host.forged_tick()
    with pytest.raises(RemoteError, match="verification"):
        c.sync_log()
    assert len(c.replayer().log()) >= 2 and c.chain().tip is not None   # the verified prefix is kept


def test_in_process_mirror(local_net: Net):
    g = local_net.genesis({"a.txt": "a\n"})
    alice = local_net.clone("@alice")
    assert alice.base == g
    (alice.root / "b.txt").write_text("b\n")
    p = alice.push("b").push
    local_net.settle()
    alice.client.sync_log()
    assert status(alice.client.chain(), p) == "linked"


def test_conflict_markers_helper():
    base = b"a\nb\nc\nd\n"
    mine = b"a\nB1\nc\nd\nmine-tail\n"
    theirs = b"a\nB2\nc\nD\n"
    out = conflict_markers(base, mine, theirs)
    assert out == (b"a\n<<<<<<< mine\nB1\n||||||| base\nb\n=======\nB2\n>>>>>>> tip\nc\nD\nmine-tail\n")
