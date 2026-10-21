import dataclasses
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from gud.core import envelope as E
from gud.core.config import MEMBERS
from gud.core.objects import MemoryStore, Tree
from gud.core.ops import Delete, ModeOp, Patch, Put, Rename, make_patch
from gud.core.types import Seen
from gud.gitbridge import (
    GitBridgeError, GitMirror, MirrorDivergence, commit_message, genesis_from_plan, import_git, parse_trailers,
)

from .util import (
    ALICE_PRIV, FILES, HOST_KEY, ROSTER, build_chain, git, link_of, make_git_repo, members_bytes, read_checkout,
)


def _put(path, mode, data):
    return Put(path, mode, E.sha256_hex(data), len(data), data)


GENESIS = {**FILES, MEMBERS: ("100644", members_bytes())}
STEPS = [
    ("@alice", "edit the readme", [Patch("README.md", make_patch(FILES["README.md"][1], b"# demo\nhello there\nworld\n"))]),
    ("@bob", "binary and mode", [_put("data/new.bin", "100644", b"\0\1\2\0" * 100), ModeOp("bin/run.sh", "100644")]),
    ("@bob", "move a file", [Rename("notrail.txt", "moved/notrail.txt")]),
    ("@alice", "drop a blob, add a link", [Delete("data/blob.bin"), _put("link2", "120000", b"moved")]),
    ("@carol", "file becomes a dir", [Delete("empty.txt"), _put("empty.txt/inner", "100644", b"")]),
    ("@bob", "", [_put("win/crlf.txt", "100644", b"crlf\r\nonly\r\n")]),
    ("@alice", "retarget a symlink\n\nGud-Link: " + "f" * 64 + "\nGud-Tree: " + "e" * 64,
     [_put("link-to-readme", "120000", b"moved/notrail.txt")]),
    ("@nobody", "make unicode file executable \0 with a NUL",
     [ModeOp("ünïcödé/日本語.txt", "100755")]),
]


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def links(store):
    return build_chain(store, GENESIS, STEPS)


def bare(path: Path) -> str:
    git("init", "-q", "--bare", str(path))
    return str(path)


def first_parent_log(remote: str, branch: str = "gud") -> list[str]:
    out = git("--git-dir", remote, "rev-list", "--first-parent", "--reverse", f"refs/heads/{branch}")
    return out.decode().split()


def head(remote: str, branch: str = "gud") -> str | None:
    out = git("--git-dir", remote, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
    return out.decode().strip() or None


def assert_mirror_matches(remote: str, links, tmp_path: Path) -> None:
    commits = first_parent_log(remote)
    assert len(commits) == len(links)
    fsck = subprocess.run(["git", "--git-dir", remote, "fsck", "--full", "--strict", "--no-dangling"],
                          capture_output=True)
    assert fsck.returncode == 0, fsck.stderr
    assert fsck.stdout == b"" and b"error" not in fsck.stderr and b"warning" not in fsck.stderr, fsck.stderr
    clone = tmp_path / f"verify-{len(list(tmp_path.iterdir()))}"
    git("clone", "-q", "--no-checkout", remote, str(clone))
    store = MemoryStore()
    for commit, link in zip(commits, links):
        git("checkout", "-q", "-f", "--detach", commit, cwd=clone)
        git("clean", "-q", "-f", "-d", "-x", cwd=clone)
        files = read_checkout(clone)
        assert Tree.from_files(store, files).hash() == link.tree_hash, link.intent
        body = git("--git-dir", remote, "log", "-1", "--format=%B", commit).decode()
        assert parse_trailers(body) == (link.state, link.tree_hash)
        parents = git("--git-dir", remote, "log", "-1", "--format=%P", commit).decode().split()
        assert len(parents) == (0 if link.parent is None else 1)


def test_mirror_round_trip(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    mirror = GitMirror(remote, tmp_path / "work", store)
    assert mirror.last_mirrored() is None
    assert mirror.mirror(links) == len(links)
    assert mirror.last_mirrored() == links[-1].state
    assert_mirror_matches(remote, links, tmp_path)


def test_commit_identity_and_message(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    GitMirror(remote, tmp_path / "work", store, committer=("Mirror Bot", "bot@example.net")).mirror(links)
    commits = first_parent_log(remote)
    fmt = "%an|%ae|%at|%cn|%ce|%ct"
    rows = [git("--git-dir", remote, "log", "-1", f"--format={fmt}", c).decode().strip().split("|") for c in commits]
    assert rows[0] == ["@alice", "alice@example.com", str((links[0].received_at) // 1000), "Mirror Bot",
                       "bot@example.net", str(links[0].linked_at // 1000)]
    assert rows[2][:2] == ["@bob", "bob@example.org"]
    assert rows[5][:2] == ["@carol", "carol@example.com"]
    assert rows[8][:2] == ["@nobody", "nobody@gud.invalid"]
    raw = git("--git-dir", remote, "cat-file", "commit", commits[1]).decode()
    assert raw.endswith(f"edit the readme\n\nGud-Link: {links[1].state}\nGud-Tree: {links[1].tree_hash}\n")
    empty = git("--git-dir", remote, "log", "-1", "--format=%s", commits[6]).decode().strip()
    assert empty == f"gud link {links[6].state[:12]}"


def test_commit_message_rules(links):
    msg = commit_message(dataclasses.replace(links[1], intent="a\0b\n\n\n"))
    assert msg == f"a\\0b\n\nGud-Link: {links[1].state}\nGud-Tree: {links[1].tree_hash}\n"
    assert parse_trailers(msg) == (links[1].state, links[1].tree_hash)
    tricky = commit_message(links[7])
    assert parse_trailers(tricky) == (links[7].state, links[7].tree_hash)
    assert parse_trailers("intent only\n") is None
    assert parse_trailers(f"x\nGud-Link: {'a' * 64}\nGud-Tree: {'b' * 64}\n") is None  # no blank line
    assert parse_trailers(f"Gud-Tree: {'b' * 64}\nGud-Tree: {'b' * 64}\n") is None


def test_idempotent(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    mirror = GitMirror(remote, tmp_path / "work", store)
    mirror.mirror(links)
    before = head(remote)
    assert mirror.mirror(links) == 0
    assert GitMirror(remote, tmp_path / "work2", store).mirror(links) == 0
    assert head(remote) == before
    assert len(first_parent_log(remote)) == len(links)


def test_deterministic_commits(tmp_path, store, links):
    a, b = bare(tmp_path / "a.git"), bare(tmp_path / "b.git")
    GitMirror(a, tmp_path / "wa", store).mirror(links)
    GitMirror(b, tmp_path / "wb", store).mirror(SimpleNamespace(links=tuple(links)))   # a Chain-like object
    assert head(a) == head(b)


def test_resume_in_fresh_process(tmp_path, store, links):
    reference = bare(tmp_path / "ref.git")
    GitMirror(reference, tmp_path / "wref", store).mirror(links)
    remote = bare(tmp_path / "remote.git")
    assert GitMirror(remote, tmp_path / "w1", store).mirror(links[:4]) == 4
    shutil.rmtree(tmp_path / "w1")
    fresh = GitMirror(remote, tmp_path / "w2", store)
    assert fresh.last_mirrored() == links[3].state
    assert fresh.mirror(links) == len(links) - 4
    assert head(remote) == head(reference)
    assert_mirror_matches(remote, links, tmp_path)


def test_resume_after_interrupted_push(tmp_path, store, links, monkeypatch):
    reference = bare(tmp_path / "ref.git")
    GitMirror(reference, tmp_path / "wref", store).mirror(links)
    remote = bare(tmp_path / "remote.git")
    real_push = GitMirror._push
    calls = []

    def flaky(self, commit, expected_old):
        calls.append(commit)
        if len(calls) == 4:
            raise KeyboardInterrupt("killed between commit-tree and push")
        return real_push(self, commit, expected_old)

    monkeypatch.setattr(GitMirror, "_push", flaky)
    with pytest.raises(KeyboardInterrupt):
        GitMirror(remote, tmp_path / "w1", store).mirror(links)
    monkeypatch.setattr(GitMirror, "_push", real_push)
    assert len(first_parent_log(remote)) == 3
    resumed = GitMirror(remote, tmp_path / "w1", store)      # same workdir: the unpushed commit is there
    assert resumed.mirror(links) == len(links) - 3
    assert head(remote) == head(reference)
    assert_mirror_matches(remote, links, tmp_path)


def test_resume_with_growing_chain(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    mirror = GitMirror(remote, tmp_path / "work", store)
    for n in range(1, len(links) + 1):
        assert mirror.mirror(links[:n]) == 1
    assert_mirror_matches(remote, links, tmp_path)


def _foreign_commit(remote: str, tmp_path: Path, on: str | None) -> str:
    clone = tmp_path / "foreign"
    git("clone", "-q", remote, str(clone))
    if on:
        git("checkout", "-q", "-B", "gud", on, cwd=clone)
    else:
        git("checkout", "-q", "--orphan", "gud", cwd=clone)
    (clone / "x.txt").write_text("someone else\n")
    git("add", "x.txt", cwd=clone)
    git("commit", "-q", "-m", "not from the mirror", cwd=clone)
    git("push", "-q", "origin", "gud", cwd=clone)
    return git("rev-parse", "HEAD", cwd=clone).decode().strip()


def test_divergence_foreign_commit(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    GitMirror(remote, tmp_path / "work", store).mirror(links[:3])
    foreign = _foreign_commit(remote, tmp_path, head(remote))
    with pytest.raises(MirrorDivergence, match="not a gud mirror commit"):
        GitMirror(remote, tmp_path / "work", store).mirror(links)
    assert head(remote) == foreign


def test_divergence_foreign_branch(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    foreign = _foreign_commit(remote, tmp_path, None)
    with pytest.raises(MirrorDivergence):
        GitMirror(remote, tmp_path / "work", store).mirror(links)
    assert head(remote) == foreign


def test_divergence_other_chain(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    GitMirror(remote, tmp_path / "work", store).mirror(links)
    before = head(remote)
    other = build_chain(store, {**GENESIS, "other.txt": ("100644", b"other\n")}, STEPS[:2])
    with pytest.raises(MirrorDivergence, match="not in this chain"):
        GitMirror(remote, tmp_path / "work", store).mirror(other)
    assert head(remote) == before


def test_divergence_tree_mismatch(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    GitMirror(remote, tmp_path / "work", store).mirror(links[:3])
    forged = list(links)
    forged[2] = dataclasses.replace(links[2], tree_hash=links[1].tree_hash, tree_root=links[1].tree_root)
    with pytest.raises(MirrorDivergence, match="cites tree"):
        GitMirror(remote, tmp_path / "work", store).mirror(forged)


def test_branch_moved_during_mirror_is_never_forced(tmp_path, store, links, monkeypatch):
    remote = bare(tmp_path / "remote.git")
    real_push = GitMirror._push
    calls = []
    foreign = {}

    def racing(self, commit, expected_old):
        calls.append(commit)
        if len(calls) == 3:
            foreign["id"] = _foreign_commit(remote, tmp_path, head(remote))
        return real_push(self, commit, expected_old)

    monkeypatch.setattr(GitMirror, "_push", racing)
    with pytest.raises(MirrorDivergence, match="moved"):
        GitMirror(remote, tmp_path / "work", store).mirror(links)
    assert head(remote) == foreign["id"]
    assert len(first_parent_log(remote)) == 3          # two mirror commits + the foreign one


def test_concurrent_identical_mirror_is_fine(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    a = GitMirror(remote, tmp_path / "wa", store)
    b = GitMirror(remote, tmp_path / "wb", store)
    a.mirror(links[:3])
    b.mirror(links[:5])
    assert a.mirror(links) == len(links) - 5
    assert_mirror_matches(remote, links, tmp_path)


def test_input_errors(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")
    with pytest.raises(GitBridgeError, match="not genesis"):
        GitMirror(remote, tmp_path / "work", store).mirror(links[2:])
    with pytest.raises(GitBridgeError, match="not contiguous"):
        GitMirror(remote, tmp_path / "work", store).mirror([links[0], links[2]])
    assert head(remote) == first_parent_log(remote)[-1]   # genesis got mirrored before the gap
    with pytest.raises(GitBridgeError, match="not in the store"):
        GitMirror(remote, tmp_path / "work", MemoryStore()).mirror(links)
    bad = dataclasses.replace(links[1], tree_hash="0" * 64)
    with pytest.raises(GitBridgeError, match="tree hash differs"):
        GitMirror(remote, tmp_path / "work", store).mirror([links[0], bad])


def test_run_once_with_client(tmp_path, store, links):
    remote = bare(tmp_path / "remote.git")

    class FakeClient:
        def __init__(self):
            self.n = 2
            self.synced = 0

        def sync_log(self):
            self.synced += 1

        def chain(self):
            return SimpleNamespace(links=tuple(links[:self.n]))

    client = FakeClient()
    mirror = GitMirror(remote, tmp_path / "work", store, client=client)
    assert mirror.run_once() == 2
    client.n = len(links)
    assert mirror.run_once() == len(links) - 2
    assert client.synced == 2
    stop = threading.Event()
    stop.set()
    mirror.run_forever(stop)                           # returns at once when stopped
    with pytest.raises(GitBridgeError):
        GitMirror(remote, tmp_path / "w2", store).run_once()


def test_import_genesis_mirror_round_trip(tmp_path):
    """git repo -> gud import -> genesis -> more links -> git mirror -> same bytes back."""
    commit = make_git_repo(tmp_path / "src", FILES, submodule="vendor/lib")
    (tmp_path / "roster").write_text(ROSTER)
    plan = import_git(tmp_path / "src", roster=tmp_path / "roster", host_key=HOST_KEY)
    store = MemoryStore()
    env, _ = genesis_from_plan(plan, "@alice", ALICE_PRIV, Seen(0, "c" * 64), store, 1 << 20, sent=1_700_000_000)
    genesis_tree = plan.tree(store)
    genesis = dataclasses.replace(
        link_of(genesis_tree, parent=None, author="@alice", intent=env.text, t=1_700_000_000_000),
        state=E.gud_id(env), push=E.gud_id(env))
    chain = build_chain(store, plan.files, STEPS)
    chain[0] = genesis
    chain[1] = dataclasses.replace(chain[1], parent=genesis.state, named_parent=genesis.state)
    remote = bare(tmp_path / "remote.git")
    assert GitMirror(remote, tmp_path / "work", store).mirror(chain) == len(chain)
    assert_mirror_matches(remote, chain, tmp_path)
    first = first_parent_log(remote)[0]
    assert commit in git("--git-dir", remote, "log", "-1", "--format=%B", first).decode()
    # the mirrored genesis holds the git source's files byte for byte
    src = git("ls-tree", "-r", "HEAD", cwd=tmp_path / "src").decode().splitlines()
    dst = set(git("--git-dir", remote, "ls-tree", "-r", first).decode().splitlines())
    assert {line for line in src if not line.startswith("160000")} <= dst
