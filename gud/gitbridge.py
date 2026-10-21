"""Git interop for gud v1 (PLAN Phase 5): `gud import` and the one-way git mirror.

* `import_git` reads one commit of a git repository through the git CLI (`ls-tree -r -z`,
  `cat-file --batch`) and builds a byte-exact genesis tree: contents, modes 100644/100755 and
  symlinks (120000) are kept exactly; submodules are skipped with a warning. `.gud/members`
  comes from a roster file; `.gud/config`, `.gud/test` and `.gud/owners` get defaults unless
  the git tree already carries them. `genesis_from_plan` turns the plan into a signed genesis
  push (SPEC §12.2).
* `GitMirror` turns links into git commits whose trees equal the link trees byte for byte,
  and pushes them append-only to a remote branch. It never force-pushes. Progress lives in the
  remote itself: the head commit's `Gud-Link` trailer names the last mirrored link, so a fresh
  process (empty workdir) resumes where the last one stopped.

Everything goes through the `git` executable; no working tree is ever checked out.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence

from gud.core import envelope as E
from gud.core.config import (
    CONFIG, MEMBERS, OWNERS, TEST, ConfigError, Members, parse_members, rules_of,
)
from gud.core.objects import ObjectError, Tree, check_tree_shape, valid_path
from gud.core.ops import Put, apply_ops, diff_trees, ops_to_records, records_to_ops
from gud.core.types import (
    ZERO, Envelope, Handle, Hex, LinkState, Mode, ObjectStoreLike, Record, Seen,
)

log = logging.getLogger("gud.gitbridge")

__all__ = [
    "GitBridgeError", "MirrorDivergence", "ImportPlan", "import_git", "parse_roster",
    "genesis_from_plan", "GitMirror", "commit_message", "parse_trailers",
    "DEFAULT_CONFIG", "DEFAULT_TEST", "DEFAULT_OWNERS",
]


class GitBridgeError(ValueError):
    """Import or mirror failure (bad repo, bad roster, git error)."""


class MirrorDivergence(GitBridgeError):
    """The remote branch does not continue this chain; the mirror stops without writing."""


# ---------------------------------------------------------------- git plumbing

_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX")


def _env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    if extra:
        env.update(extra)
    return env


def _run(git: str, git_dir: Path, *args: str, input: bytes | None = None,
         env: Mapping[str, str] | None = None) -> bytes:
    cmd = [git, f"--git-dir={git_dir}", *args]
    try:
        proc = subprocess.run(cmd, input=input, capture_output=True, env=_env(env))
    except OSError as exc:
        raise GitBridgeError(f"cannot run {git}: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitBridgeError(f"git {args[0]} failed: {err}")
    return proc.stdout


def _resolve_git_dir(git: str, path: Path) -> Path:
    """The git directory of a working repository or a bare repository at `path`."""
    path = Path(path)
    if not path.exists():
        raise GitBridgeError(f"{path}: no such directory")
    try:
        proc = subprocess.run([git, "-C", str(path), "rev-parse", "--absolute-git-dir", "--show-prefix"],
                              capture_output=True, env=_env())
    except OSError as exc:
        raise GitBridgeError(f"cannot run {git}: {exc}") from exc
    if proc.returncode != 0:
        raise GitBridgeError(f"{path}: not a git repository")
    lines = proc.stdout.decode("utf-8").split("\n")
    if len(lines) > 1 and lines[1].strip():
        raise GitBridgeError(f"{path}: not the top of a git repository (inside it at {lines[1].strip()})")
    return Path(lines[0].strip())


def _cat_blobs(git: str, git_dir: Path, oids: Sequence[str]) -> dict[str, bytes]:
    """Read many blobs with one `git cat-file --batch`; byte-exact (no filters)."""
    if not oids:
        return {}
    out = _run(git, git_dir, "cat-file", "--batch", input=("\n".join(oids) + "\n").encode("ascii"))
    result: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode("ascii", "replace").split(" ")
        if len(header) != 3 or header[1] != "blob":
            raise GitBridgeError(f"cat-file: unexpected reply {' '.join(header)!r} for {oid}")
        size = int(header[2])
        start = nl + 1
        result[oid] = out[start:start + size]
        if out[start + size:start + size + 1] != b"\n":
            raise GitBridgeError("cat-file: truncated output")
        pos = start + size + 1
    return result


# ---------------------------------------------------------------- import

DEFAULT_CONFIG = b"""\
# gud repo config (SPEC section 10.5). Changing this file needs quorum.
window 60s
grace 30s
skew 120s
own-links 50
own-time 24h
lease 30m
escalate 1h
expire 7d
carry file
fix-offer 5m
lease-thrash 3
checkpoint 1000
# authserv-id mx.example.org   (pin it to let verified unsigned human mail count)
"""

DEFAULT_TEST = b"""\
# gud test policy (SPEC section 10.4). With no `command`, no push needs a test.
# command pytest -q
# image python:3.12-slim
# timeout 10m
# on-paths migrations/**,src/auth/**
# on-tip links=5 time=10m
# every-link off
# stack-max 8
# auto-revert off
"""

DEFAULT_OWNERS = b"""\
# gud declared owners (SPEC section 10.3): glob rule owners...
# rule is lazy, explicit or quorum; the last matching line applies.
# src/auth/** explicit @alice @bob
# docs/** lazy @carol
# migrations/** quorum
"""

_DEFAULTS: tuple[tuple[str, bytes], ...] = ((CONFIG, DEFAULT_CONFIG), (TEST, DEFAULT_TEST), (OWNERS, DEFAULT_OWNERS))


@dataclass(frozen=True)
class ImportPlan:
    """A genesis tree ready to sign. `files` is byte-exact from the git tree, plus `.gud/*`."""
    files: Mapping[str, tuple[Mode, bytes]]
    source_commit: str
    intent: str
    ref: str = "main"
    warnings: tuple[str, ...] = ()
    members: Members | None = field(default=None, compare=False)

    def tree(self, store: ObjectStoreLike) -> Tree:
        return Tree.from_files(store, self.files)


def parse_roster(data: bytes, host_key: str) -> bytes:
    """Roster file -> `.gud/members` bytes (draft for review before signing genesis).

    One member per line: `handle role address [pubkey] [sponsor]`; `#` comments and blank lines
    are ignored. With four fields, the fourth is a sponsor when it starts with `@`, else a key.
    A missing key is written `-` (allowed for humans only). The `host` line gets `host_key` when
    it has no key, and must equal it otherwise. Raises GitBridgeError if the result does not
    parse under §10.2."""
    if not E.is_public_key(host_key):
        raise GitBridgeError(f"bad host key {host_key!r}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitBridgeError("roster: not valid UTF-8") from exc
    lines = ["# handle role address public_key sponsor"]
    for n, raw in enumerate(text.splitlines(), 1):
        parts = raw.split("#", 1)[0].split()
        if not parts:
            continue
        if not 3 <= len(parts) <= 5:
            raise GitBridgeError(f"roster:{n}: expected `handle role address [pubkey] [sponsor]`")
        handle, role, address = parts[:3]
        key, sponsor = "-", "-"
        if len(parts) == 4:
            if parts[3].startswith("@"):
                sponsor = parts[3]
            else:
                key = parts[3]
        elif len(parts) == 5:
            key, sponsor = parts[3], parts[4]
        if role == "host":
            if key == "-":
                key = host_key
            elif key != host_key:
                raise GitBridgeError(f"roster:{n}: host key differs from the host's key")
        lines.append(f"{handle} {role} {address} {key} {sponsor}")
    out = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        parse_members(out)
    except ConfigError as exc:
        raise GitBridgeError(f"roster: {exc}") from exc
    return out


def import_git(git_dir: Path, *, ref: str = "main", roster: Path, host_key: str,
               git: str = "git") -> ImportPlan:
    """Read `ref` of the git repository at `git_dir` (working or bare) into an ImportPlan.

    History is not imported; the source commit id is recorded in the intent. Submodules,
    non-blob entries, paths gud cannot hold (§5.1: non-UTF-8, `.git` segments, reserved
    `.gud/BASE` / `.gud/local/`) are skipped, each with a warning. A `.gud/members` in the git
    tree is replaced by the roster; existing `.gud/config`/`test`/`owners` are kept."""
    gd = _resolve_git_dir(git, Path(git_dir))
    try:
        commit = _run(git, gd, "rev-parse", "--verify", "--quiet", "--end-of-options",
                      f"{ref}^{{commit}}").decode("ascii").strip()
    except GitBridgeError as exc:
        raise GitBridgeError(f"{git_dir}: no commit at ref {ref!r}") from exc
    listing = _run(git, gd, "ls-tree", "-r", "-z", "--full-tree", commit)
    warnings: list[str] = []
    wanted: list[tuple[str, Mode, str]] = []
    for rec in listing.split(b"\0"):
        if not rec:
            continue
        meta, _, raw_path = rec.partition(b"\t")
        mode_b, type_b, oid_b = meta.split(b" ")
        mode, typ, oid = mode_b.decode("ascii"), type_b.decode("ascii"), oid_b.decode("ascii")
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f"skipped {raw_path!r}: path is not valid UTF-8")
            continue
        if mode == "160000" or typ == "commit":
            warnings.append(f"skipped submodule {path} (commit {oid}): gud trees hold no submodules")
            continue
        if typ != "blob":
            warnings.append(f"skipped {path}: git object type {typ}")
            continue
        if mode == "100664":
            warnings.append(f"{path}: legacy git mode 100664 imported as 100644")
            mode = "100644"
        if mode not in ("100644", "100755", "120000"):
            warnings.append(f"skipped {path}: unsupported git mode {mode}")
            continue
        if not valid_path(path):
            warnings.append(f"skipped {path}: not a valid gud path (SPEC 5.1)")
            continue
        wanted.append((path, mode, oid))  # type: ignore[arg-type]
    blobs = _cat_blobs(git, gd, sorted({oid for _, _, oid in wanted}))
    files: dict[str, tuple[Mode, bytes]] = {}
    for path, mode, oid in wanted:
        data = blobs[oid]
        if mode == "120000" and (not data or b"\0" in data):
            warnings.append(f"skipped {path}: symlink with an empty or NUL target")
            continue
        files[path] = (mode, data)
    members = parse_roster(Path(roster).read_bytes(), host_key)
    if MEMBERS in files:
        warnings.append(f"{MEMBERS} from git replaced by the roster")
    files[MEMBERS] = ("100644", members)
    for path, default in _DEFAULTS:
        if path not in files:
            files[path] = ("100644", default)
    intent = f"gud import: git {ref} at commit {commit}"
    plan_files = dict(sorted(files.items(), key=lambda kv: kv[0].encode("utf-8")))
    _check_genesis_tree(plan_files)
    for w in warnings:
        log.warning("gud import: %s", w)
    return ImportPlan(MappingProxyType(plan_files), commit, intent, ref, tuple(warnings),
                      parse_members(members))


def _check_genesis_tree(files: Mapping[str, tuple[Mode, bytes]]) -> None:
    from gud.core.objects import MemoryStore
    try:
        tree = Tree.from_files(MemoryStore(), files)
    except ObjectError as exc:
        raise GitBridgeError(f"import: {exc}") from exc
    problem = check_tree_shape(tree)
    if problem:
        raise GitBridgeError(f"import: {problem}")
    try:
        rules_of(tree)
    except ConfigError as exc:
        raise GitBridgeError(f"import: config file does not parse: {exc}") from exc


_PUSH_ROLES = frozenset({"agent", "human", "console", "tester"})


class _StoreBlobs:
    def __init__(self, store: ObjectStoreLike) -> None:
        self._store = store

    def blob(self, sha256: Hex) -> bytes | None:
        return self._store.get(sha256) if self._store.has(sha256) else None


def genesis_from_plan(plan: ImportPlan, author: Handle, private_raw: bytes, seen: Seen,
                      store: ObjectStoreLike, inline_budget: int, *,
                      sent: int | None = None) -> tuple[Envelope, list[bytes]]:
    """Signed genesis `push` (Gud-Parent ZERO) building `plan.files` from the empty tree.

    Returns `(push, blobs)`: `blobs` are the contents of every blob-reference `put` (files that
    did not fit `inline_budget` bytes of inline content), in op order; send them as `blob`
    messages before the push (§6). `author` must be in the plan's members with a push role and
    the key of `private_raw`. `sent` defaults to the current unix time."""
    tree = plan.tree(store)
    try:
        rules = rules_of(tree)
    except ConfigError as exc:
        raise GitBridgeError(f"genesis: {exc}") from exc
    member = rules.members.get(author)
    if member is None or member.role not in _PUSH_ROLES:
        raise GitBridgeError(f"genesis: {author} is not a member with a push role")
    if member.public_key != E.public_of(private_raw):
        raise GitBridgeError(f"genesis: the key does not match {author}'s key in {MEMBERS}")
    ops = diff_trees(Tree.empty(store), tree, inline_budget=inline_budget)
    records = (Record("text", (), plan.intent.encode("utf-8")),) + ops_to_records(ops)
    if sent is None:
        import time
        sent = int(time.time())
    env = E.build("push", author=author, headers={"Gud-Parent": ZERO, "Gud-Tree": tree.hash()},
                  records=records, sent=sent, seen=seen)
    reason = E.validate_kind(env)
    if reason:
        raise GitBridgeError(f"genesis: {reason}")
    check = apply_ops(Tree.empty(store), records_to_ops(env.records), _StoreBlobs(store))
    if check.hash() != tree.hash():
        raise GitBridgeError("genesis: operations do not rebuild the tree")
    blobs = [store.get(op.sha256) for op in ops if isinstance(op, Put) and op.data is None]
    return E.sign(env, private_raw), blobs


# ---------------------------------------------------------------- mirror

_TRAILER_RE = re.compile(r"(Gud-Link|Gud-Tree): ([0-9a-f]{64})")


def commit_message(link: LinkState) -> str:
    """Intent, a blank line, then the `Gud-Link: <state>` and `Gud-Tree: <hash>` trailers.

    NUL (which git refuses in messages) is written as `\\0`. An empty intent becomes
    `gud link <id12>`. The trailers are always the last two lines."""
    intent = link.intent.replace("\0", "\\0").rstrip()
    if not intent:
        intent = f"gud link {link.state[:12]}"
    return f"{intent}\n\nGud-Link: {link.state}\nGud-Tree: {link.tree_hash}\n"


def parse_trailers(message: str) -> tuple[Hex, Hex] | None:
    """(link state, tree hash) from the final paragraph of a mirror commit message, or None."""
    lines = message.rstrip("\n").split("\n")
    if len(lines) < 2:
        return None
    found = {}
    for line in lines[-2:]:
        m = _TRAILER_RE.fullmatch(line)
        if not m:
            return None
        found[m.group(1)] = m.group(2)
    if set(found) != {"Gud-Link", "Gud-Tree"} or len(lines) > 2 and lines[-3] != "":
        return None
    return found["Gud-Link"], found["Gud-Tree"]


@dataclass(frozen=True)
class _Head:
    commit: str
    state: Hex
    tree_hash: Hex


class _MkTree:
    """A `git mktree -z --batch` process: one tree per request, read back interactively."""

    def __init__(self, git: str, git_dir: Path) -> None:
        self._proc = subprocess.Popen([git, f"--git-dir={git_dir}", "mktree", "-z", "--batch"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=_env())

    def make(self, entries: Sequence[tuple[str, str, str, bytes]]) -> str:
        assert self._proc.stdin and self._proc.stdout
        buf = b"".join(f"{mode} {typ} {oid}\t".encode("ascii") + name + b"\0"
                       for mode, typ, oid, name in entries)
        self._proc.stdin.write(buf + b"\0")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline().decode("ascii").strip()
        if not line:
            err = self._proc.stderr.read().decode("utf-8", "replace") if self._proc.stderr else ""
            raise GitBridgeError(f"git mktree failed: {err.strip()}")
        return line

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait()
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream:
                stream.close()


class GitMirror:
    """One-way, append-only git mirror of a gud chain. Never force-pushes.

    `remote` is any git remote URL or path; `workdir` is a local scratch bare repository
    (created on first use, safe to delete); `store` holds the link trees. Each link becomes
    one commit on `refs/heads/<branch>` of the remote: tree = the link's tree, parent = the
    previous mirrored commit, author = the link author's handle and member address at
    `received_at`, committer = the mirror identity at `linked_at`, message =
    `commit_message(link)`. Commits are deterministic, so re-mirroring the same links gives
    the same commit ids."""

    def __init__(self, remote: str, workdir: Path, store: ObjectStoreLike, *, branch: str = "gud",
                 committer: tuple[str, str] = ("gud mirror", "mirror@gud.invalid"),
                 client: Any = None, git: str = "git") -> None:
        self.remote = str(remote)
        self.workdir = Path(workdir)
        self.store = store
        self.branch = branch
        self.committer = committer
        self.client = client
        self.git = git
        self._blob_oids: dict[Hex, str] = {}       # gud sha256 -> git blob id
        self._tree_oids: dict[tuple, str] = {}      # git tree entries -> git tree id
        self._members: dict[Hex, Members | None] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ public API

    def last_mirrored(self) -> Hex | None:
        """State of the last link on the remote branch (from its head commit's trailer)."""
        self._init()
        head = self._remote_head()
        return head.state if head else None

    def mirror(self, links: Iterable[LinkState] | Any) -> int:
        """Commit and push every link after the remote's last mirrored one. Returns the count.

        `links` is the chain in order (genesis first), e.g. `Chain.links`; a `Chain` is also
        accepted. Raises MirrorDivergence when the remote branch does not continue it (a
        commit without our trailers, a link not in `links`, a tree mismatch, or the branch
        moving under us), and GitBridgeError for git or input failures. Each link is pushed as
        soon as its commit exists, so an interruption loses at most the link in flight."""
        if hasattr(links, "links"):
            links = links.links
        with self._lock:
            self._init()
            head = self._remote_head()
            it: Iterator[LinkState] = iter(links)
            prev: LinkState | None = None
            prev_commit: str | None = None
            if head is not None:
                for link in it:
                    if link.state == head.state:
                        prev = link
                        break
                else:
                    raise MirrorDivergence(
                        f"remote {self.branch} head {head.commit[:12]} cites link {head.state[:12]}, "
                        "which is not in this chain")
                if prev.tree_hash != head.tree_hash:
                    raise MirrorDivergence(
                        f"remote {self.branch} head {head.commit[:12]} cites tree {head.tree_hash[:12]} "
                        f"for link {prev.state[:12]}, whose tree is {prev.tree_hash[:12]}")
                prev_commit = head.commit
            count = 0
            for link in it:
                if prev is None:
                    if link.parent is not None:
                        raise GitBridgeError(f"first link {link.state[:12]} to mirror is not genesis")
                elif link.parent != prev.state:
                    raise GitBridgeError(
                        f"links are not contiguous: {link.state[:12]} follows {prev.state[:12]} "
                        f"but its parent is {str(link.parent)[:12]}")
                commit = self._commit(link, prev, prev_commit)
                self._push(commit, prev_commit)
                prev, prev_commit = link, commit
                count += 1
            return count

    def run_once(self) -> int:
        """Sync the client's replay state and mirror its chain. Needs `client` (a RepoClient)."""
        if self.client is None:
            raise GitBridgeError("run_once needs a client; call mirror(links) instead")
        self.client.sync_log()
        return self.mirror(self.client.chain().links)

    def run_forever(self, stop: threading.Event, *, interval: float = 5.0) -> None:
        """Mirror until `stop` is set. Divergence stops the loop; other failures are retried."""
        while not stop.is_set():
            try:
                self.run_once()
            except MirrorDivergence:
                raise
            except (GitBridgeError, OSError) as exc:
                log.warning("git mirror: %s (retrying)", exc)
            stop.wait(interval)

    # ------------------------------------------------------------ git helpers

    def _git(self, *args: str, input: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
        return _run(self.git, self.workdir, *args, input=input, env=env)

    def _init(self) -> None:
        if (self.workdir / "HEAD").exists():
            return
        self.workdir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([self.git, "init", "-q", "--bare", str(self.workdir)],
                           capture_output=True, check=True, env=_env())
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GitBridgeError(f"cannot create mirror workdir {self.workdir}: {exc}") from exc

    def _tracking_ref(self) -> str:
        return f"refs/gud-mirror/{self.branch}"

    def _remote_head(self) -> _Head | None:
        ref = f"refs/heads/{self.branch}"
        listed = self._git("ls-remote", "--end-of-options", self.remote, ref).decode("ascii", "replace")
        if not listed.strip():
            return None
        self._git("fetch", "-q", "--no-tags", "--no-write-fetch-head", "--end-of-options",
                  self.remote, f"+{ref}:{self._tracking_ref()}")
        commit = self._git("rev-parse", "--verify", self._tracking_ref()).decode("ascii").strip()
        raw = self._git("cat-file", "commit", commit)
        _, _, body = raw.partition(b"\n\n")
        trailers = parse_trailers(body.decode("utf-8", "replace"))
        if trailers is None:
            raise MirrorDivergence(
                f"remote {self.branch} head {commit[:12]} is not a gud mirror commit (no Gud-Link/Gud-Tree trailers)")
        return _Head(commit, trailers[0], trailers[1])

    def _push(self, commit: str, expected_old: str | None) -> None:
        try:
            self._git("push", "-q", "--end-of-options", self.remote, f"{commit}:refs/heads/{self.branch}")
        except GitBridgeError as exc:
            moved = None
            try:
                head = self._remote_head()
                moved = head.commit if head else None
            except MirrorDivergence:
                moved = "(not a mirror commit)"
            except GitBridgeError:
                raise exc
            if moved == commit:
                return            # another mirror pushed the identical commit
            if moved != expected_old:
                raise MirrorDivergence(
                    f"remote {self.branch} moved to {str(moved)[:12]} while mirroring; refusing to force") from exc
            raise

    # ------------------------------------------------------------ objects

    def _author(self, link: LinkState, prev: LinkState | None) -> tuple[str, str]:
        for source in (link, prev):
            if source is None:
                continue
            members = self._members_at(source)
            member = members.get(link.author) if members else None
            if member is not None:
                return link.author, member.address
        return link.author, f"{link.author.lstrip('@')}@gud.invalid"

    def _members_at(self, link: LinkState) -> Members | None:
        try:
            entry = Tree.load(self.store, link.tree_root).get(MEMBERS)
        except ObjectError:
            return None
        if entry is None:
            return None
        if entry.blob not in self._members:
            try:
                self._members[entry.blob] = parse_members(self.store.get(entry.blob))
            except (ConfigError, KeyError):
                self._members[entry.blob] = None
        return self._members[entry.blob]

    def _commit(self, link: LinkState, prev: LinkState | None, parent: str | None) -> str:
        tree_oid = self._git_tree(link)
        name, email = self._author(link, prev)
        cname, cemail = self.committer
        env = {
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": f"@{link.received_at // 1000} +0000",
            "GIT_COMMITTER_NAME": cname, "GIT_COMMITTER_EMAIL": cemail,
            "GIT_COMMITTER_DATE": f"@{link.linked_at // 1000} +0000",
        }
        args = ["commit-tree", tree_oid]
        if parent:
            args += ["-p", parent]
        args += ["-F", "-"]
        return self._git(*args, input=commit_message(link).encode("utf-8"), env=env).decode("ascii").strip()

    def _git_tree(self, link: LinkState) -> str:
        try:
            tree = Tree.load(self.store, link.tree_root)
        except ObjectError as exc:
            raise GitBridgeError(f"link {link.state[:12]}: tree not in the store") from exc
        if tree.hash() != link.tree_hash:
            raise GitBridgeError(f"link {link.state[:12]}: stored tree hash differs from the link's")
        entries = list(tree.items())
        self._write_blobs([e.blob for _, e in entries])
        root: dict[bytes, Any] = {}
        for path, entry in entries:
            segs = [s.encode("utf-8") for s in path.split("/")]
            node = root
            for seg in segs[:-1]:
                node = node.setdefault(seg, {})
            node[segs[-1]] = (entry.mode, self._blob_oids[entry.blob])
        if not root:
            return self._git("mktree", input=b"").decode("ascii").strip()
        mk: _MkTree | None = None
        try:
            def build(node: dict[bytes, Any]) -> str:
                nonlocal mk
                rows = []
                for name in sorted(node):
                    value = node[name]
                    if isinstance(value, dict):
                        rows.append(("040000", "tree", build(value), name))
                    else:
                        rows.append((value[0], "blob", value[1], name))
                key = tuple(rows)
                oid = self._tree_oids.get(key)
                if oid is None:
                    if mk is None:
                        mk = _MkTree(self.git, self.workdir)
                    oid = mk.make(rows)
                    self._tree_oids[key] = oid
                return oid
            return build(root)
        finally:
            if mk is not None:
                mk.close()

    def _write_blobs(self, shas: Iterable[Hex]) -> None:
        todo = sorted({s for s in shas if s not in self._blob_oids})
        if not todo:
            return
        with tempfile.TemporaryDirectory(prefix="gud-blobs-", dir=self.workdir) as tmp:
            paths = []
            for i, sha in enumerate(todo):
                p = Path(tmp) / str(i)
                p.write_bytes(self.store.get(sha))
                paths.append(str(p))
            out = self._git("hash-object", "-w", "--no-filters", "--stdin-paths",
                            input=("\n".join(paths) + "\n").encode("utf-8"))
        oids = out.decode("ascii").split()
        if len(oids) != len(todo):
            raise GitBridgeError("git hash-object returned the wrong number of ids")
        self._blob_oids.update(zip(todo, oids))
