"""Key management for gud v1 (PLAN Phase 6, SPEC §9, §10.2, §11.6, §21).

* `KeyStore` keeps each member's Ed25519 private key under `GUD_HOME/keys/<name>.key`
  (directory 0700, files 0600, base64 of the 32 raw bytes). A rotation stages the new key as
  `<name>.next.key` until the members push that installs it links; `promote` then replaces the
  old key, which is deleted. `load` refuses a key file that group or others can read.
* Member key changes are ordinary pushes that edit `.gud/members` (§21): the members rule
  (§13.3) needs one human approve.
  - `rotate_member_key`: the member signs the change with the **old** key (verified against the
    members at the named parent, §9), replacing their own `public_key`.
  - `recover_member_key` + `sponsor_approve`: the key is lost, so another member authors the
    push and the member's sponsor approves it. `sponsor_approve` checks that the approver is the
    sponsor and that the push changes nothing but that member's key.
  - `revoke_member`: removes the member's line; once it links, every live push by them dies
    (§12.3 step 2) and their signatures stop verifying.
* Host keys rotate in the log (§11.6): `rotate_host_key` appends the `handover` entry signed by
  the old key and the new key's `open`, and `host_key_push` sends the members push that moves the
  `host` line to the new key.

Every client-side function takes a `MemberClient` (the `gud.wc.remote.RepoClient` surface:
`handle`, `reader()`, `sign_and_send(...)`) and needs a synced replay view of the tip.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat
from pathlib import Path
from typing import Protocol, Sequence

from gud.core import envelope as E
from gud.core.config import MEMBERS, ConfigError, Members, parse_members
from gud.core.hostlog import HostLog
from gud.core.objects import Tree
from gud.core.ops import diff_trees, ops_to_records
from gud.core.types import Envelope, Handle, Hex, LogEntry, Ms, Record, Seen

KEYS_DIR = "keys"
PUSH_ROLES = frozenset({"agent", "human", "console", "tester"})


class KeysError(ValueError):
    pass


# ---------------------------------------------------------------- local key files


def _name(handle: Handle) -> str:
    if not E.is_handle(handle):
        raise KeysError(f"bad handle {handle!r}")
    return handle[1:]


def _write_secret(path: Path, data: bytes) -> None:
    """Atomic 0600 write; the directory is made 0700."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def decode_private(text: str) -> bytes:
    """Raw 32-byte seed from the key file text (base64). Raises KeysError."""
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeysError("key file is not base64") from exc
    if len(raw) != 32:
        raise KeysError("key file does not hold a 32-byte Ed25519 key")
    return raw


class KeyStore:
    """Private keys under `<home>/keys`. One current key per handle, plus one staged rotation key."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.root = self.home / KEYS_DIR

    def path(self, handle: Handle, *, staged: bool = False) -> Path:
        return self.root / (f"{_name(handle)}.next.key" if staged else f"{_name(handle)}.key")

    def has(self, handle: Handle, *, staged: bool = False) -> bool:
        return self.path(handle, staged=staged).exists()

    def save(self, handle: Handle, private_raw: bytes, *, overwrite: bool = False, staged: bool = False) -> Path:
        if len(private_raw) != 32:
            raise KeysError("an Ed25519 private key is 32 bytes")
        path = self.path(handle, staged=staged)
        if path.exists() and not overwrite:
            raise KeysError(f"a key for {handle} already exists at {path}")
        _write_secret(path, base64.b64encode(private_raw) + b"\n")
        return path

    def generate(self, handle: Handle, *, overwrite: bool = False) -> tuple[bytes, str]:
        """New key pair stored as the handle's current key. Returns (private raw, public key)."""
        if self.has(handle) and not overwrite:
            raise KeysError(f"a key for {handle} already exists at {self.path(handle)}")
        private, public = E.generate_key()
        self.save(handle, private, overwrite=overwrite)
        return private, public

    def load(self, handle: Handle, *, staged: bool = False) -> bytes:
        path = self.path(handle, staged=staged)
        try:
            st = path.stat()
        except FileNotFoundError:
            raise KeysError(f"no key for {handle} at {path}") from None
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise KeysError(f"{path} is readable by group or others; chmod 600 it")
        return decode_private(path.read_text("ascii"))

    def public(self, handle: Handle, *, staged: bool = False) -> str:
        return E.public_of(self.load(handle, staged=staged))

    def stage(self, handle: Handle, private_raw: bytes | None = None) -> tuple[bytes, str]:
        """Store the next key for a rotation (overwrites an earlier staged key)."""
        if private_raw is None:
            private_raw, _ = E.generate_key()
        self.save(handle, private_raw, overwrite=True, staged=True)
        return private_raw, E.public_of(private_raw)

    def promote(self, handle: Handle) -> str:
        """The staged key becomes current (call once the rotation push has linked). The old key
        file is replaced, so the old key is gone from this machine. Returns the new public key."""
        staged = self.path(handle, staged=True)
        if not staged.exists():
            raise KeysError(f"no staged key for {handle}")
        public = self.public(handle, staged=True)
        os.replace(staged, self.path(handle))
        return public

    def discard_staged(self, handle: Handle) -> None:
        self.path(handle, staged=True).unlink(missing_ok=True)

    def remove(self, handle: Handle) -> None:
        for staged in (False, True):
            self.path(handle, staged=staged).unlink(missing_ok=True)


# ---------------------------------------------------------------- members file edits


_FIELD_SPLIT = re.compile(r"[ \t]+")


def edit_members(data: bytes, handle: Handle, *, key: str | None = None, remove: bool = False) -> bytes:
    """`.gud/members` bytes with `handle`'s line changed: its public key replaced by `key`, or the
    line removed. Every other line (comments, order, spacing) is kept byte for byte. The result
    must parse (§10.2). Raises KeysError."""
    if (key is None) == (not remove):
        raise KeysError("give exactly one of key= or remove=True")
    if key is not None and not E.is_public_key(key):
        raise KeysError("bad public key")
    lines = data.split(b"\n")
    out: list[bytes] = []
    found = False
    for line in lines:
        body, hash_, comment = line.partition(b"#")
        fields = _FIELD_SPLIT.split(body.decode("utf-8", "replace").strip(" \t"))
        if len(fields) == 5 and fields[0] == handle:
            found = True
            if remove:
                continue
            fields[3] = key  # type: ignore[assignment]
            line = " ".join(fields).encode("utf-8") + ((b" #" + comment) if hash_ else b"")
        out.append(line)
    if not found:
        raise KeysError(f"{handle} is not in {MEMBERS}")
    result = b"\n".join(out)
    try:
        parse_members(result)
    except ConfigError as exc:
        raise KeysError(f"the edited {MEMBERS} does not parse: {exc}") from exc
    return result


def members_push(tree: Tree, parent: Hex, new_members: bytes, *, author: Handle, intent: str,
                 private_raw: bytes, seen: Seen, sent: int, join: Hex | None = None) -> Envelope:
    """A signed `push` on `parent` (whose tree is `tree`) that sets `.gud/members` to
    `new_members`. Pure; the caller sends it."""
    headers, records = members_push_parts(tree, parent, new_members, intent=intent, join=join)
    env = E.build("push", author=author, headers=headers, records=records, sent=sent, seen=seen)
    return E.sign(env, private_raw)


def members_push_parts(tree: Tree, parent: Hex, new_members: bytes, *, intent: str,
                       join: Hex | None = None) -> tuple[dict[str, str], tuple[Record, ...]]:
    """(headers, records) of a members push, for `MemberClient.sign_and_send`."""
    entry = tree.get(MEMBERS)
    if entry is None:
        raise KeysError(f"the parent tree has no {MEMBERS}")
    new_tree = tree.set(MEMBERS, entry.mode, new_members)
    ops = diff_trees(tree, new_tree, inline_budget=1 << 30)
    if not ops:
        raise KeysError(f"{MEMBERS} would not change")
    headers = {"Gud-Parent": parent, "Gud-Tree": new_tree.hash()}
    if join is not None:
        headers["Gud-Join"] = join
    records = ((Record("text", (), intent.encode("utf-8")),) if intent else ()) + ops_to_records(ops)
    return headers, records


# ---------------------------------------------------------------- client-side operations


class MemberClient(Protocol):
    """What these operations need from a member's client (`gud.wc.remote.RepoClient`)."""
    handle: Handle

    def reader(self): ...           # gud.core.replay.ChainReader of the synced chain

    def sign_and_send(self, kind: str, headers, records: Sequence[Record] = (), *,
                      summary: str, in_reply_to: Hex | None = None) -> Hex: ...


def _tip(client: MemberClient):
    reader = client.reader()
    tip = reader.chain.tip
    if tip is None:
        raise KeysError("no chain yet (sync first)")
    return reader, tip


def current_key(reader, handle: Handle, at: Hex | None = None) -> str | None:
    """The member's public key at `at` (default: the tip), or None if not a member / no key."""
    member = reader.members(at).get(handle)
    return member.public_key if member is not None else None


def _send_members_push(client: MemberClient, new_members: bytes, *, intent: str, summary: str) -> Hex:
    reader, tip = _tip(client)
    headers, records = members_push_parts(reader.tree(), tip.state, new_members, intent=intent)
    return client.sign_and_send("push", headers, records, summary=summary)


def rotate_member_key(client: MemberClient, *, new_private: bytes | None = None,
                      keystore: KeyStore | None = None) -> tuple[Hex, bytes]:
    """Members push replacing the client's own key, signed with the current (old) key.
    Returns (push id, new private key). With `keystore`, the new key is staged there; call
    `KeyStore.promote` once the push has linked."""
    reader, tip = _tip(client)
    members = reader.members()
    me = members.get(client.handle)
    if me is None:
        raise KeysError(f"{client.handle} is not a member at the tip")
    if me.role not in PUSH_ROLES:
        raise KeysError(f"role {me.role} cannot push; ask a member to send the change")
    if new_private is None:
        new_private, _ = E.generate_key()
    new_public = E.public_of(new_private)
    if me.public_key == new_public:
        raise KeysError("the new key equals the current key")
    data = edit_members(reader.tree().read(MEMBERS), client.handle, key=new_public)
    if keystore is not None:
        keystore.stage(client.handle, new_private)
    push = _send_members_push(client, data, intent=f"rotate key of {client.handle}",
                              summary=f"rotate key {client.handle}")
    return push, new_private


def recover_member_key(client: MemberClient, handle: Handle, new_public: str, *, reason: str = "") -> Hex:
    """Lost-key recovery (§21): a members push, authored by the client (normally the member's
    sponsor), giving `handle` the new public key. It links with the sponsor's approve
    (`sponsor_approve`), which the members rule requires."""
    if handle == client.handle:
        raise KeysError("use rotate_member_key to change your own key")
    reader, _ = _tip(client)
    target = reader.members().get(handle)
    if target is None:
        raise KeysError(f"{handle} is not a member at the tip")
    if target.public_key == new_public:
        raise KeysError(f"{handle} already has that key")
    data = edit_members(reader.tree().read(MEMBERS), handle, key=new_public)
    sponsor = target.sponsor or "any human member"
    intent = f"recover lost key of {handle} (approval: {sponsor})" + (f": {reason}" if reason else "")
    return _send_members_push(client, data, intent=intent, summary=f"recover key {handle}")


def _members_change(reader, push: Hex) -> tuple[Members, Members]:
    """(members at the tip, members in the candidate tree of `push`). Raises KeysError."""
    tree = reader.candidate_tree(push)
    if tree is None:
        raise KeysError(f"push {push[:12]} is not an open candidate on the tip (sync first)")
    try:
        return reader.members(), parse_members(tree.read(MEMBERS))
    except (ConfigError, KeyError) as exc:
        raise KeysError(f"push {push[:12]} has no valid {MEMBERS}") from exc


def sponsor_approve(client: MemberClient, push: Hex, handle: Handle) -> Hex:
    """The sponsor's approve of a key-recovery push for `handle`. Checks that the client is the
    sponsor (or, for a member without a sponsor, a human member) and that the push changes only
    `handle`'s public key. Returns the approve id."""
    reader, _ = _tip(client)
    before, after = _members_change(reader, push)
    target = before.get(handle)
    if target is None:
        raise KeysError(f"{handle} is not a member at the tip")
    me = before.get(client.handle)
    if target.sponsor is not None:
        if client.handle != target.sponsor:
            raise KeysError(f"only {target.sponsor} (the sponsor of {handle}) approves its key recovery")
    elif me is None or me.role != "human":
        raise KeysError(f"{handle} has no sponsor; a human member must approve")
    if me is None or me.role != "human":
        raise KeysError("the members rule needs the approve of a human member")
    changed = sorted(h for h in set(before.by_handle) | set(after.by_handle)
                     if before.get(h) != after.get(h))
    new = after.get(handle)
    if changed != [handle] or new is None or (new.role, new.address, new.sponsor) != \
            (target.role, target.address, target.sponsor):
        raise KeysError(f"push {push[:12]} changes more than the key of {handle}")
    text = f"sponsor approval: key recovery for {handle}".encode("utf-8")
    return client.sign_and_send("approve", {"Gud-Target": push}, (Record("text", (), text),),
                                summary=f"approve key recovery {handle}")


def revoke_member(client: MemberClient, handle: Handle, reason: str) -> Hex:
    """Members push removing `handle` (§21 leaving / revocation). Returns the push id."""
    reader, _ = _tip(client)
    members = reader.members()
    target = members.get(handle)
    if target is None:
        raise KeysError(f"{handle} is not a member at the tip")
    if target.role == "host":
        raise KeysError("the host line cannot be removed; rotate the host key instead")
    if target.role == "human" and members.humans() == (handle,):
        raise KeysError("removing the last human would leave no one to approve members changes")
    data = edit_members(reader.tree().read(MEMBERS), handle, remove=True)
    intent = f"revoke {handle}" + (f": {reason}" if reason else "")
    return _send_members_push(client, data, intent=intent, summary=f"revoke {handle}")


def host_key_push(client: MemberClient, new_public: str, *, reason: str = "") -> Hex:
    """Members push moving the `host` line to `new_public` (§11.6, after the log handover)."""
    reader, _ = _tip(client)
    host = reader.members().host()
    if host.public_key == new_public:
        raise KeysError("the host line already has that key")
    data = edit_members(reader.tree().read(MEMBERS), host.handle, key=new_public)
    intent = f"host key handover to {new_public}" + (f": {reason}" if reason else "")
    return _send_members_push(client, data, intent=intent, summary="host key handover")


def rotate_host_key(log: HostLog, old_private: bytes, new_private: bytes, *, now: Ms,
                    client: MemberClient | None = None) -> tuple[LogEntry, LogEntry, Hex | None]:
    """Same-host key rotation recorded in the log (§11.6 planned handover): `handover` signed by
    the old key, then `open` with the new key citing it. With `client`, also sends the members
    push for the `host` line. Returns (handover, open, push id or None). The host must sign every
    later entry with `new_private`."""
    if log.head is None:
        raise KeysError("the log is not open")
    if E.public_of(old_private) != log.key:
        raise KeysError("old_private is not the current log key")
    new_public = E.public_of(new_private)
    if new_public == log.key:
        raise KeysError("the new host key equals the current one")
    first = max(log.head.time + 1, now - 1)
    handover = log.handover(old_private, new_public, first)
    opened = log.continue_after_handover(new_private, handover.time + 1)
    push = host_key_push(client, new_public) if client is not None else None
    return handover, opened, push

