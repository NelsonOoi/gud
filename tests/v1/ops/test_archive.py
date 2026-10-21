"""gud.ops.archive: export / verify / import, byte-exactness, tamper detection, host handover."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from gud.core import envelope as E
from gud.core.hostlog import HostLog
from gud.core.types import Record
from gud.ops import keys
from gud.ops.archive import (
    LOG_FILE, MANIFEST, MBOX_FILE, SNAPSHOT_FILE, ArchiveError, decode_log, encode_log, export_archive,
    import_archive, load_archive, mbox_blocks, mbox_encode, plan_handover, verify_archive,
)
from gud.transport.fake import FakeNetwork

from .harness import HOST_KEY, REPO, Host, View, World, linked

# Bytes an mbox writer must get exactly right: From_ lines at line start (plain and already
# quoted), CR and CRLF, NUL, non-UTF-8, and no final newline.
UNSIGNED = (b"From: dana@example.org\r\nTo: repo@example.org\r\nMessage-ID: <u1@example.org>\r\n"
            b"Subject: hello\r\n\r\nFrom the top\r\n>From quoted\n>>From twice\rlone cr\x00nul \xff\xfe\r\n"
            b"From end without newline")
GENESIS = {"app.py": b"a = 1\n", "bin.dat": bytes(range(256))}


def build_world() -> World:
    w = World(genesis=GENESIS)
    alice, nelson = w["@alice"], w["@nelson"]
    p = alice.push({"app.py": b"a = 2\nFrom here on\n"}, intent="From the start\n>From quoted")
    w.step()
    nelson.approve(p)
    w.step()
    w.host.mailbox.deliver(UNSIGNED)
    w.step()
    alice.sign_and_send("comment", {"Gud-Target": p}, (Record("text", (), b"From: a comment\n"),),
                        summary="From line in a subject")
    w.step(100_000)
    assert linked(alice, p)
    return w


@pytest.fixture
def world() -> World:
    return build_world()


def logged_raw(host: Host) -> list[bytes]:
    return [host.store[gid] for _, gid, _ in host.log.mail_ids()]


# ---------------------------------------------------------------- round trip


def test_mbox_round_trip_is_byte_exact():
    msgs = [UNSIGNED, b"x\n", b"From \n", b">From x", b"a\r\n\r\n", b"\n"]
    blocks = mbox_blocks(mbox_encode((m, 0) for m in msgs))
    for raw, body in zip(msgs, blocks):
        assert body in (raw, raw + b"\n")


def test_log_text_round_trip(world):
    entries = world.host.log.entries()
    assert decode_log(encode_log(entries)) == entries


def test_export_verify_import_round_trip(world, tmp_path):
    host = world.host
    assert any(raw == UNSIGNED for raw in host.store.values())
    out = export_archive(host, tmp_path / "a", signer=HOST_KEY)
    report = verify_archive(out, host_key=E.public_of(HOST_KEY))
    assert report.ok, report.problems
    assert report.signer == E.public_of(HOST_KEY)
    assert report.messages == len(host.store) and report.entries == len(host.log)
    assert report.tip == world.tip().state and report.snapshot is not None

    loaded = load_archive(out)
    assert list(loaded.entries) == host.log.entries()
    assert list(loaded.mail.values()) == logged_raw(host)                # byte-exact, log order

    # planned handover is required to import under a new key
    new_private, new_pub = E.generate_key()
    with pytest.raises(ArchiveError, match="handover"):
        import_archive(out, repo=REPO, host_private=new_private, now=world.net.now_ms + 5)

    # same host restoring its own backup: nothing appended
    restored = import_archive(out, repo=REPO, host_private=HOST_KEY, now=world.net.now_ms + 5)
    assert restored.entries() == host.log.entries()


def test_export_is_deterministic_and_tar_works(world, tmp_path):
    a = export_archive(world.host, tmp_path / "a")
    b = export_archive(world.host, tmp_path / "b")
    for name in (MANIFEST, LOG_FILE, MBOX_FILE, SNAPSHOT_FILE):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    t1 = export_archive(world.host, tmp_path / "x.tar")
    t2 = export_archive(world.host, tmp_path / "y.tar")
    assert t1.read_bytes() == t2.read_bytes()
    assert verify_archive(t1).ok
    with tarfile.open(t1) as tar:
        assert sorted(tar.getnames()) == sorted((MANIFEST, LOG_FILE, MBOX_FILE, SNAPSHOT_FILE))
    with pytest.raises(ArchiveError):
        export_archive(world.host, tmp_path / "a")                       # never overwrites


def test_export_from_a_member_client(world, tmp_path):
    class LocalOnly:                                                    # no mirror surface, only replay
        def __init__(self, client):
            self.c = client

        def replayer(self):
            return self.c.replayer()

        def reader(self):
            return self.c.reader()

    out = export_archive(LocalOnly(world["@bob"]), tmp_path / "m")
    report = verify_archive(out)
    assert report.ok, report.problems
    assert report.signer is None and report.messages == len(world.host.store)


def test_missing_mail_is_recorded(world, tmp_path):
    host = world.host
    gone = next(gid for gid, raw in host.store.items() if raw == UNSIGNED)
    view = View(host.log, {k: v for k, v in host.store.items() if k != gone})
    out = export_archive(view, tmp_path / "p")
    manifest = json.loads((out / MANIFEST).read_text())
    assert manifest["missing"] == [gone]
    report = verify_archive(out)
    assert not report.ok and report.missing == (gone,)


# ---------------------------------------------------------------- tampering


@pytest.fixture
def exported(world, tmp_path) -> tuple[World, Path]:
    return world, export_archive(world.host, tmp_path / "a", signer=HOST_KEY)


def _blocks(path: Path) -> list[bytes]:
    data = (path / MBOX_FILE).read_bytes()
    starts = [0] + [i + 1 for i in range(len(data)) if data[i:i + 6] == b"\nFrom "]
    return [data[s:e] for s, e in zip(starts, starts[1:] + [len(data)])]


def test_tamper_one_byte_in_a_signed_message(exported):
    world, path = exported
    blocks = _blocks(path)
    target = next(i for i, b in enumerate(blocks) if b"Gud-Kind: comment" in b)
    blocks[target] = blocks[target].replace(b"Gud-Kind: comment", b"Gud-Kind: cOmment", 1)   # human part
    (path / MBOX_FILE).write_bytes(b"".join(blocks))
    report = verify_archive(path)
    assert not report.ok
    comment = [gid for _, gid, _ in world.host.log.mail_ids()][target]
    assert report.altered == (comment,)


def test_tamper_one_byte_in_an_unsigned_message(exported):
    world, path = exported
    data = (path / MBOX_FILE).read_bytes()
    (path / MBOX_FILE).write_bytes(data.replace(b"lone cr", b"lone CR", 1))
    report = verify_archive(path)
    unsigned = next(gid for gid, raw in world.host.store.items() if raw == UNSIGNED)
    assert not report.ok and unsigned in report.missing
    assert any("does not list" in p for p in report.problems)


def test_drop_one_message(exported):
    world, path = exported
    blocks = _blocks(path)
    dropped = [gid for _, gid, _ in world.host.log.mail_ids()][2]
    del blocks[2]
    (path / MBOX_FILE).write_bytes(b"".join(blocks))
    report = verify_archive(path)
    assert not report.ok and report.missing == (dropped,)


def test_reorder_mbox(exported):
    _, path = exported
    blocks = _blocks(path)
    blocks[1], blocks[2] = blocks[2], blocks[1]
    (path / MBOX_FILE).write_bytes(b"".join(blocks))
    report = verify_archive(path)
    assert not report.ok and report.out_of_order


def test_reorder_log(exported):
    _, path = exported
    entries = decode_log((path / LOG_FILE).read_bytes())
    entries[2], entries[3] = entries[3], entries[2]
    (path / LOG_FILE).write_bytes(encode_log(entries))
    report = verify_archive(path)
    assert not report.ok and not report.log_ok and "seq" in report.log_error


def test_reorder_log_with_a_consistent_manifest_still_fails(exported):
    _, path = exported
    entries = decode_log((path / LOG_FILE).read_bytes())
    entries[2], entries[3] = entries[3], entries[2]
    data = encode_log(entries)
    (path / LOG_FILE).write_bytes(data)
    manifest = json.loads((path / MANIFEST).read_text())
    manifest["log"]["sha256"] = E.sha256_hex(data)
    manifest.pop("sig"), manifest.pop("signer")
    (path / MANIFEST).write_text(json.dumps(manifest))
    report = verify_archive(path)
    assert not report.log_ok


def test_manifest_and_snapshot_tampering(exported):
    _, path = exported
    manifest = json.loads((path / MANIFEST).read_text())
    manifest["repo"] = "evil@example.org"
    (path / MANIFEST).write_text(json.dumps(manifest))
    report = verify_archive(path)
    assert any("signature" in p for p in report.problems) and any("repo" in p for p in report.problems)


def test_snapshot_must_replay(exported):
    _, path = exported
    snap = json.loads((path / SNAPSHOT_FILE).read_bytes())
    snap["t"] += 1
    data = E.jcs(snap)
    (path / SNAPSHOT_FILE).write_bytes(data)
    manifest = json.loads((path / MANIFEST).read_text())
    manifest["snapshot"]["sha256"] = E.sha256_hex(data)
    manifest.pop("sig"), manifest.pop("signer")
    (path / MANIFEST).write_text(json.dumps(manifest))
    report = verify_archive(path)
    assert report.problems == ("replaying the archive does not reproduce its snapshot",)


# ---------------------------------------------------------------- planned host handover


def test_planned_handover_migration(world, tmp_path):
    old = world.host
    new_private, new_pub = E.generate_key()
    handover = plan_handover(old.log, HOST_KEY, new_pub, world.net.now_ms + 1)
    out = export_archive(old, tmp_path / "a", signer=HOST_KEY)
    assert verify_archive(out).ok

    net2 = FakeNetwork(start_ms=world.net.now_ms + 60_000)
    box = net2.mailbox(REPO)
    with pytest.raises(ArchiveError, match="repo"):
        import_archive(out, repo="other@example.org", host_private=new_private, now=net2.now_ms)
    log = import_archive(out, repo=REPO, host_private=new_private, mailbox=box, now=net2.now_ms,
                         mail_store=tmp_path / "store", log_path=tmp_path / "log.txt")
    opened = log.head
    assert opened.type == "open" and opened.field("from") == f"{handover.seq}:{handover.hash}"
    assert opened.field("key") == new_pub and log.key == new_pub
    HostLog(log.entries()).verify()                                       # chain spans both hosts
    assert decode_log((tmp_path / "log.txt").read_bytes()) == log.entries()

    # the new mailbox holds the same bytes, in log order
    items, _ = box.list(limit=10_000)
    assert [i.raw_mime for i in items] == logged_raw(old)
    assert {p.stem: p.read_bytes() for p in (tmp_path / "store").iterdir()} == old.store

    # members move to the new host and keep working
    new = Host(net2, key=new_private, log=log, store=dict(old.store))
    new.cursor = box.head()                                               # re-delivered mail is already logged
    for client in world.clients.values():
        client.net, client.mirror = net2, new
        client.mailbox = net2.mailbox(client.address)
    world.net, world.host = net2, new
    world.step()
    nelson = world["@nelson"]
    assert nelson.chain().tip.state == load_archive(out).manifest["snapshot"]["tip"]
    push = keys.host_key_push(nelson, new_pub, reason="moved host")
    world.step()
    assert linked(nelson, push)
    assert nelson.reader().members().host().public_key == new_pub
    work = world["@alice"].push({"app.py": b"a = 5\n"})
    world.step(100_000)
    assert linked(nelson, work)

    # the continued log exports and verifies too, anchored at either host key
    out2 = export_archive(new, tmp_path / "b", signer=new_private)
    for anchor in (E.public_of(HOST_KEY), new_pub):
        assert verify_archive(out2, host_key=anchor).ok
    assert not verify_archive(out2, host_key=E.generate_key()[1]).ok


def test_unplanned_handover_needs_trust(world, tmp_path):
    out = export_archive(world.host, tmp_path / "a")
    new_private, new_pub = E.generate_key()
    with pytest.raises(ArchiveError, match="unplanned"):
        import_archive(out, repo=REPO, host_private=new_private, now=world.net.now_ms + 5)
    log = import_archive(out, repo=REPO, host_private=new_private, now=world.net.now_ms + 5, trust_unplanned=True)
    assert log.head.type == "open" and log.key == new_pub
    assert len(HostLog(log.entries(), trusted_keys=[new_pub])) == len(log)

    # a log continued under an untrusted key does not verify offline
    log.append_tick(new_private, world.net.now_ms + 10)
    world.host.log = log
    out2 = export_archive(world.host, tmp_path / "b", trusted_keys=[new_pub])
    assert not verify_archive(out2).log_ok
    assert verify_archive(out2, trusted_keys=[new_pub]).ok


def test_handover_to_a_different_key_is_refused(world, tmp_path):
    plan_handover(world.host.log, HOST_KEY, E.generate_key()[1], world.net.now_ms + 1)
    out = export_archive(world.host, tmp_path / "a")
    with pytest.raises(ArchiveError, match="different host key"):
        import_archive(out, repo=REPO, host_private=E.generate_key()[0], now=world.net.now_ms + 5)
    with pytest.raises(ArchiveError):
        plan_handover(world.host.log, HOST_KEY, E.generate_key()[1], world.net.now_ms + 9)
