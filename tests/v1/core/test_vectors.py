"""Golden vectors in tests/v1/vectors/core.json, for other implementations to check against.

Each vector is checked twice: by the gud.core library, and by an independent computation that
uses only hashlib and the spec's byte rules."""

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

from gud.core import envelope as E
from gud.core.config import rules_of
from gud.core.hostlog import HostLog, decode_entry
from gud.core.objects import MemoryStore, Tree
from gud.core.ops import apply_ops, apply_patch, records_to_ops
from gud.core.types import ZERO

VECTORS = json.loads((Path(__file__).parents[1] / "vectors" / "core.json").read_text())


def _b(text: str) -> bytes:
    return base64.b64decode(text)


def _manual_tree_hash(files) -> str:
    lines = b""
    for f in sorted(files, key=lambda f: f["path"].encode()):
        lines += f["path"].encode() + b"\0" + f["mode"].encode() + b"\0" + hashlib.sha256(_b(f["data"])).hexdigest().encode() + b"\n"
    return hashlib.sha256(lines).hexdigest()


@pytest.mark.parametrize("vec", VECTORS["envelopes"], ids=lambda v: v["name"])
def test_envelope_vectors(vec):
    data = _b(vec["bytes"])
    env = E.decode(data)
    assert E.encode(env) == data
    assert E.signing_bytes(env) == _b(vec["signing_bytes"])
    assert re.sub(rb"(?m)^Gud-Sig: [^\n]*\n", b"", data, count=1) == _b(vec["signing_bytes"])
    assert E.gud_id(env) == vec["gud_id"] == hashlib.sha256(_b(vec["signing_bytes"])).hexdigest()
    assert E.validate_kind(env) is None
    if vec["signed_by"]:
        key = VECTORS["keys"][vec["signed_by"]]
        assert E.verify(env, key)
        seed = bytes.fromhex(VECTORS["keys"]["alice_seed_hex"])
        assert E.sign(env, seed).sig == env.sig          # Ed25519 is deterministic
    if "decoded_put_path" in vec:
        assert env.records_named("put")[0].args[0] == vec["decoded_put_path"]


@pytest.mark.parametrize("vec", VECTORS["trees"], ids=lambda v: v["name"])
def test_tree_vectors(vec):
    files = {f["path"]: (f["mode"], _b(f["data"])) for f in vec["files"]}
    assert Tree.from_files(MemoryStore(), files).hash() == vec["tree_hash"] == _manual_tree_hash(vec["files"])


def test_state_chain_vectors():
    store = MemoryStore()
    tree = Tree.empty(store)
    parent = ZERO
    envs = {v["gud_id"]: E.decode(_b(v["bytes"])) for v in VECTORS["envelopes"]}
    for link in VECTORS["state_chain"]:
        env = envs[link["state"]]
        assert env.get("Gud-Parent") == parent
        tree = apply_ops(tree, records_to_ops(env.records), None)
        assert tree.hash() == env.get("Gud-Tree") == link["tree_hash"]
        parent = link["state"]
    assert rules_of(tree).members.host().public_key == VECTORS["keys"]["host_public"]


def test_log_vectors():
    entries = [decode_entry(item["text"].encode()) for item in VECTORS["log"]]
    for entry, item in zip(entries, VECTORS["log"]):
        signed = item["text"].encode().rsplit(b"sig ", 1)[0]
        assert entry.hash == item["hash"] == hashlib.sha256(signed).hexdigest()
    log = HostLog(entries)
    assert log.key == VECTORS["keys"]["host_public"]
    genesis = E.decode(_b(VECTORS["envelopes"][0]["bytes"]))
    seen = E.parse_seen(genesis.get("Gud-Seen"))
    assert log.check_seen(seen, own_seq=log.seq_of(E.gud_id(genesis))) == "ok"
    assert [gid for _, gid, _ in log.mail_ids()] == [v["state"] for v in VECTORS["state_chain"]]


@pytest.mark.parametrize("vec", VECTORS["patches"])
def test_patch_vectors(vec):
    assert apply_patch(_b(vec["old"]), _b(vec["hunks"])) == _b(vec["new"])
