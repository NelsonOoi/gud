"""Helpers for the gitbridge tests: git repos on disk and synthetic link chains."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from gud.core import envelope as E
from gud.core.objects import Tree
from gud.core.ops import ChangeOp, apply_ops, touched_paths
from gud.core.types import ZERO, LinkState, Mode
from gud.gitbridge import parse_roster

HOST_PRIV = bytes(range(32))
HOST_KEY = E.public_of(HOST_PRIV)
ALICE_PRIV = bytes(range(1, 33))
ALICE_KEY = E.public_of(ALICE_PRIV)
BOB_KEY = E.public_of(bytes(range(2, 34)))

ROSTER = f"""# handle role address [pubkey] [sponsor]
@alice human alice@example.com {ALICE_KEY}
@bob agent bob@example.org {BOB_KEY} @alice
@carol human carol@example.com
@host host repo@example.org
"""

# Everything byte-exactness has to survive: binary with NULs, CRLF, no final LF, empty file,
# executable bit, symlinks (to a file, to a directory, dangling), deep dirs, unicode and odd names.
FILES: dict[str, tuple[Mode, bytes]] = {
    "README.md": ("100644", b"# demo\nhello\nworld\n"),
    "bin/run.sh": ("100755", b"#!/bin/sh\necho hi\n"),
    "data/blob.bin": ("100644", bytes(range(256)) + b"\0\0\0\xff\xfeend"),
    "win/crlf.txt": ("100644", b"line one\r\nline two\r\nlone cr\rend\r\n"),
    "notrail.txt": ("100644", b"no newline at end"),
    "empty.txt": ("100644", b""),
    "link-to-readme": ("120000", b"README.md"),
    "link-to-dir": ("120000", b"a/b"),
    "dangling": ("120000", b"../nowhere/x"),
    "a/b/c/d/deep.txt": ("100644", b"deep\n"),
    "ünïcödé/日本語.txt": ("100644", "naïve café\n".encode()),
    "odd/sp ace & tab\there.txt": ("100644", b"odd\n"),
}

GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_AUTHOR_DATE": "@1700000000 +0000", "GIT_COMMITTER_DATE": "@1700000000 +0000",
}


def git(*args: str, cwd: Path | None = None, input: bytes | None = None, check: bool = True) -> bytes:
    cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.symlinks=true", "-c", "core.attributesFile=/dev/null",
           "-c", "init.defaultBranch=main", *args]
    proc = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, env={**os.environ, **GIT_ENV})
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.decode()}")
    return proc.stdout


def write_files(root: Path, files: Mapping[str, tuple[Mode, bytes]]) -> None:
    for path, (mode, data) in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            os.symlink(os.fsdecode(data), target)
        else:
            target.write_bytes(data)
            os.chmod(target, 0o755 if mode == "100755" else 0o644)


def read_checkout(root: Path) -> dict[str, tuple[Mode, bytes]]:
    """Files of a checked-out directory (minus .git), byte-exact, with modes from lstat."""
    out: dict[str, tuple[Mode, bytes]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            st = os.lstat(full)
            if os.path.islink(full):
                out[rel] = ("120000", os.fsencode(os.readlink(full)))
            else:
                mode: Mode = "100755" if st.st_mode & 0o100 else "100644"
                out[rel] = (mode, Path(full).read_bytes())
    return out


def make_git_repo(root: Path, files: Mapping[str, tuple[Mode, bytes]], *, submodule: str | None = None) -> str:
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", str(root))
    write_files(root, files)
    git("add", "-A", cwd=root)
    if submodule:
        git("update-index", "--add", "--cacheinfo", f"160000,{'ab' * 20},{submodule}", cwd=root)
    git("commit", "-q", "-m", "source", cwd=root)
    return git("rev-parse", "HEAD", cwd=root).decode().strip()


def members_bytes() -> bytes:
    return parse_roster(ROSTER.encode(), HOST_KEY)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def link_of(tree: Tree, *, parent: LinkState | None, author: str, intent: str, t: int,
            files: Sequence[str] = (), carried: bool = False) -> LinkState:
    seq = 0 if parent is None else parent.seq + 1
    push = _sha(f"push/{seq}/{intent}/{tree.hash()}")
    state = _sha(f"carry/{push}/{parent.state}") if carried and parent else push
    return LinkState(
        state=state, seq=seq, push=push, kind="push",
        parent=None if parent is None else parent.state,
        named_parent=ZERO if parent is None else (parent.parent if carried else parent.state),
        author=author, tree_root=tree.root, tree_hash=tree.hash(), blame_root=ZERO, rules_root=ZERO,
        received_at=t - 1500, linked_at=t, task=None, intent=intent, files=tuple(sorted(files)),
        carried_from=parent.parent if carried and parent else None,
    )


class NoBlobs:
    def blob(self, sha256: str) -> bytes | None:
        return None


def build_chain(store, genesis_files: Mapping[str, tuple[Mode, bytes]],
                steps: Sequence[tuple[str, str, Sequence[ChangeOp]]]) -> list[LinkState]:
    """Genesis plus one link per (author, intent, ops) step."""
    tree = Tree.from_files(store, genesis_files)
    links = [link_of(tree, parent=None, author="@alice", intent="genesis", t=1_700_000_000_000,
                     files=list(genesis_files))]
    for n, (author, intent, ops) in enumerate(steps, 1):
        tree = apply_ops(tree, ops, NoBlobs())
        links.append(link_of(tree, parent=links[-1], author=author, intent=intent,
                             t=1_700_000_000_000 + n * 60_000, files=touched_paths(ops), carried=n % 3 == 0))
    return links
