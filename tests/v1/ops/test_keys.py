"""gud.ops.keys: key files, rotation, lost-key recovery, revocation, host key rotation."""

from __future__ import annotations

import os
import stat

import pytest

from gud.core import envelope as E
from gud.core.config import MEMBERS, parse_members
from gud.core.hostlog import HostLog, LogError
from gud.ops import keys
from gud.ops.keys import KeysError, KeyStore

from .harness import HOST_KEY, World, linked, status

GENESIS = {"app.py": b"a = 1\n", "docs/readme.md": b"hello\n"}


@pytest.fixture
def world() -> World:
    return World(genesis=GENESIS)


# ---------------------------------------------------------------- key files


def test_keystore_files_are_private(tmp_path):
    ks = KeyStore(tmp_path)
    private, public = ks.generate("@alice")
    path = ks.path("@alice")
    assert path == tmp_path / "keys" / "alice.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert ks.load("@alice") == private and ks.public("@alice") == public
    with pytest.raises(KeysError):
        ks.generate("@alice")
    os.chmod(path, 0o644)
    with pytest.raises(KeysError, match="readable"):
        ks.load("@alice")


def test_keystore_stage_and_promote(tmp_path):
    ks = KeyStore(tmp_path)
    old, _ = ks.generate("@alice")
    new, new_pub = ks.stage("@alice")
    assert stat.S_IMODE(ks.path("@alice", staged=True).stat().st_mode) == 0o600
    assert ks.load("@alice") == old                       # current key unchanged until promote
    assert ks.promote("@alice") == new_pub
    assert ks.load("@alice") == new and not ks.has("@alice", staged=True)
    with pytest.raises(KeysError):
        ks.promote("@alice")
    with pytest.raises(KeysError):
        ks.path("alice")                                  # not a handle


def test_edit_members_keeps_other_lines():
    a_key, host_key, new = (E.generate_key()[1].encode() for _ in range(3))
    data = (b"# roster\n@a agent a@x.org " + a_key + b" @h # the agent\n"
            b"@h human h@x.org - -\n@host host r@x.org " + host_key + b" -\n")
    new = new.decode()
    out = keys.edit_members(data, "@a", key=new)
    assert parse_members(out).get("@a").public_key == new
    assert out.split(b"\n")[0] == b"# roster" and out.endswith(b"-\n")
    assert b"# the agent" in out
    assert parse_members(keys.edit_members(data, "@a", remove=True)).get("@a") is None
    with pytest.raises(KeysError):
        keys.edit_members(data, "@nobody", key=new)
    with pytest.raises(KeysError):
        keys.edit_members(data, "@host", remove=True)     # exactly one host line (§10.2)


# ---------------------------------------------------------------- rotation


def test_rotation_old_key_signs_then_stops_working(world, tmp_path):
    alice, nelson = world["@alice"], world["@nelson"]
    old_pub = E.public_of(alice.private)
    ks = KeyStore(tmp_path)
    ks.save("@alice", alice.private)
    push, new_private = keys.rotate_member_key(alice, keystore=ks)
    world.step()
    env = alice.reader().email(push).envelope
    assert env.author == "@alice" and E.verify(env, old_pub)          # the old key signs the change
    assert status(alice, push) == "needs_approval"                     # members rule
    nelson.approve(push)
    world.step()
    assert linked(alice, push)
    new_pub = E.public_of(new_private)
    assert keys.current_key(alice.reader(), "@alice") == new_pub
    assert ks.load("@alice", staged=True) == new_private
    ks.promote("@alice")
    assert ks.load("@alice") == new_private

    # after the rotation links, the old key no longer signs anything that counts
    stale = alice.push({"app.py": b"a = 2\n"}, key=alice.private)
    world.step()
    assert status(alice, stale) == "bad_sig"
    alice.private = new_private
    fresh = alice.push({"app.py": b"a = 3\n"})
    world.step(100_000)
    assert linked(alice, fresh)


def test_rotation_by_a_non_member_is_refused(world):
    world["@alice"].handle = "@mallory"
    with pytest.raises(KeysError):
        keys.rotate_member_key(world["@alice"])


def test_old_key_cannot_link_a_push_on_a_pre_rotation_parent(world):
    alice, nelson = world["@alice"], world["@nelson"]
    before = world.tip().state
    old = alice.private
    push, new_private = keys.rotate_member_key(alice)
    world.step()
    nelson.approve(push)
    world.step()
    assert linked(alice, push)
    attack = alice.push({"docs/readme.md": b"pwned\n"}, parent=before, key=old)
    world.step(200_000)
    assert not linked(alice, attack)


# ---------------------------------------------------------------- lost key


def test_lost_key_recovery_needs_the_sponsor(world):
    bob, dana, nelson = world["@bob"], world["@dana"], world["@nelson"]
    new_private, new_pub = E.generate_key()
    push = keys.recover_member_key(bob, "@alice", new_pub, reason="laptop lost")
    world.step()
    assert status(bob, push) == "needs_approval"
    with pytest.raises(KeysError, match="sponsor"):
        keys.sponsor_approve(dana, push, "@alice")      # a human, but not alice's sponsor
    with pytest.raises(KeysError):
        keys.sponsor_approve(bob, push, "@alice")
    keys.sponsor_approve(nelson, push, "@alice")
    world.step()
    assert linked(nelson, push)
    assert keys.current_key(nelson.reader(), "@alice") == new_pub
    alice = world["@alice"]
    alice.private = new_private
    work = alice.push({"docs/readme.md": b"back\n"})
    world.step(100_000)
    assert linked(alice, work)


def test_sponsor_refuses_a_recovery_that_changes_more(world):
    bob, nelson = world["@bob"], world["@nelson"]
    reader = bob.reader()
    _, new_pub = E.generate_key()
    data = keys.edit_members(reader.tree().read(MEMBERS), "@alice", key=new_pub)
    data = keys.edit_members(data, "@bob", key=E.generate_key()[1])    # sneaks in a second change
    push = keys._send_members_push(bob, data, intent="recover @alice", summary="recover")
    world.step()
    with pytest.raises(KeysError, match="more than"):
        keys.sponsor_approve(nelson, push, "@alice")
    with pytest.raises(KeysError):
        keys.recover_member_key(bob, "@bob", new_pub)    # own key: use rotation


# ---------------------------------------------------------------- revocation


def test_revoke_member(world):
    nelson, bob = world["@nelson"], world["@bob"]
    push = keys.revoke_member(nelson, "@bob", "left the project")
    world.step()
    assert linked(nelson, push)                          # a human author satisfies the members rule
    assert nelson.reader().members().get("@bob") is None
    late = bob.push({"app.py": b"a = 9\n"})
    world.step(100_000)
    assert status(bob, late) == "bad_sig"
    with pytest.raises(KeysError):
        keys.revoke_member(nelson, "@host", "no")
    keys.revoke_member(nelson, "@dana", "bye")
    world.step()
    with pytest.raises(KeysError, match="last human"):
        keys.revoke_member(nelson, "@nelson", "bye")


# ---------------------------------------------------------------- host key


def test_host_key_rotation_is_in_the_log(world):
    nelson = world["@nelson"]
    new_private, new_pub = E.generate_key()
    handover, opened, push = keys.rotate_host_key(world.host.log, HOST_KEY, new_private,
                                                  now=world.net.now_ms, client=nelson)
    world.host.key = new_private
    assert handover.type == "handover" and handover.field("next") == new_pub
    assert opened.type == "open" and opened.field("from") == f"{handover.seq}:{handover.hash}"
    HostLog(world.host.log.entries()).verify()
    with pytest.raises(LogError):
        HostLog(world.host.log.entries()).append_tick(HOST_KEY, world.net.now_ms + 10)
    world.step()
    assert linked(nelson, push)
    assert nelson.reader().members().host().public_key == new_pub
    assert nelson.replayer().log().key == new_pub
    work = world["@alice"].push({"app.py": b"a = 4\n"})
    world.step(100_000)
    assert linked(nelson, work)
    with pytest.raises(KeysError):
        keys.rotate_host_key(world.host.log, HOST_KEY, E.generate_key()[0], now=world.net.now_ms)
