"""The working copy (SPEC-v1 §22): an ordinary directory plus `.gud/BASE` and `.gud/local/`.

- **Materialise** writes a link's tree byte-exactly: modes (`100755` sets the executable bits,
  `100644` clears them), symlinks as symlinks, no newline conversion. Nothing is written
  through a symlink or outside the directory: every parent directory is checked with
  `lstat`, a symlink or file in the way of a directory is replaced, and files are written to a
  temporary name (`O_NOFOLLOW | O_EXCL`) and renamed into place.
- **Working tree**: the files under the root minus ignored paths (`.gudignore` plus the
  built-ins), read byte-exactly, mode from the user executable bit, symlink targets as bytes.
  A tracked path that is ignored keeps its base entry (it is never pushed, changed or deleted
  by a push). An index under `.gud/local/index.json` (stat data -> sha256) avoids rehashing
  unchanged files; entries modified within 2 s of the scan that recorded them are rehashed.
- **Push** is `diff_trees(base tree, working tree)` signed with `Gud-Parent` = `.gud/BASE`,
  after the secret scan. When the base is behind the tip and a link since the base touched a
  path the push touches, the push would not be carried (file mode) or would not merge (line
  mode): it is refused and nothing is sent; run `gud sync` first.
- **Sync** is `three_way(base, working tree, tip)`. On conflict nothing in the working copy
  changes, `.gud/BASE` stays, and the conflicts are recorded in `.gud/local/sync.json` with
  diff3-style marker files under `.gud/local/conflicts/<path>` for text conflicts. Sync never
  auto-resolves.
- **Verify** rebuilds replay from the mirror (genesis or a valid checkpoint) and compares it
  with the cache, then checks the cached tip and base trees' blobs against their hashes.
"""

from __future__ import annotations

import difflib
import json
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from gud.core import envelope as E
from gud.core.config import ConfigError, rules_of
from gud.core.envelope import sha256_hex
from gud.core.objects import Entry, ObjectError, Tree, check_tree_shape, valid_path
from gud.core.ops import (
    ChangeOp, OpError, Put, apply_ops, diff_trees, is_text, ops_to_records, render_unified, split_lines,
    three_way,
)
from gud.core.replay import ReplayError
from gud.core.rules import ConsentPlan
from gud.core.types import ZERO, Hex, LinkState, Mode, Record

from .ignore import IgnoreRules
from .remote import RemoteError, RepoClient
from .secrets import SecretHit, scan

BASE_FILE = ".gud/BASE"
LOCAL_DIR = ".gud/local"
_RACY_NS = 2_000_000_000
_ENVELOPE_SLACK = 8192


class WcError(RuntimeError):
    """A working-copy operation cannot proceed; the message says why and what to do."""


class ArtifactsFound(WcError):
    """A push would add build output (`gud.wc.ignore.artifacts`). `paths` lists it."""

    def __init__(self, paths: Sequence[str]) -> None:
        from .ignore import ignore_lines
        super().__init__(f"these look like build output, not source: {', '.join(paths)}; add "
                         f"{', '.join(ignore_lines(paths))} to .gudignore, or pass --allow-artifacts")
        self.paths = tuple(paths)


class SecretsFound(WcError):
    """The secret scan refused a push. `hits` lists every match."""

    def __init__(self, hits: Sequence[SecretHit]) -> None:
        lines = ", ".join(f"{h.path}:{h.line} ({h.pattern})" for h in hits)
        super().__init__(f"secret scan refused the push: {lines}; remove the secret, or pass "
                         f"--allow-secret PATH if it is not one")
        self.hits = tuple(hits)


@dataclass(frozen=True)
class Status:
    base: Hex
    tip: Hex
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    mode_changed: tuple[str, ...]
    behind: int                                      # links between base and tip
    conflicts: tuple[str, ...] = ()                  # from the last sync that stopped
    pushes: tuple[tuple[Hex, str], ...] = ()         # (push id, status) of pushes sent from here

    @property
    def clean(self) -> bool:
        return not (self.added or self.modified or self.deleted or self.mode_changed)

    def to_json(self) -> dict:
        d = asdict(self)
        d["clean"] = self.clean
        d["pushes"] = [{"push": p, "status": s} for p, s in self.pushes]
        return d


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    new_base: Hex
    conflicts: tuple[str, ...]
    written: tuple[str, ...] = ()                    # paths changed in the working copy
    markers: tuple[str, ...] = ()                    # conflict marker files written (relative to root)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PushResult:
    push: Hex
    parent: Hex
    ops: int
    blobs: int
    forecast: ConsentPlan | None
    files: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"push": self.push, "parent": self.parent, "ops": self.ops, "blobs": self.blobs,
                "files": list(self.files), "forecast": self.forecast.to_json() if self.forecast else None}


# ---------------------------------------------------------------- push building (shared)

class _Blobs:
    def __init__(self, store: Any) -> None:
        self.store = store

    def blob(self, sha: Hex) -> bytes | None:
        return self.store.get(sha) if self.store.has(sha) else None


def build_push(client: RepoClient, parent: Hex, before: Tree, after: Tree, intent: str, *,
               kind: str = "push", headers: Mapping[str, str] | None = None
               ) -> tuple[Any, list[bytes], tuple[ChangeOp, ...]]:
    """Signed push (or genesis when `parent` is ZERO) from `before` to `after`, within the §6
    budget: contents that do not fit inline become blob references. Returns
    (envelope, blob contents to send first, ops)."""
    budget = client.budget()
    text = intent.encode("utf-8")
    inline = max(0, budget - _ENVELOPE_SLACK - len(text))
    h = {"Gud-Parent": parent, "Gud-Tree": after.hash(), **dict(headers or {})}
    while True:
        ops = diff_trees(before, after, inline_budget=inline)
        if not ops:
            raise WcError("nothing to push: the working copy equals its base")
        records = ((Record("text", (), text),) if text else ()) + ops_to_records(ops)
        env = client.build_signed(kind, h, records)
        size = len(E.encode(env))
        if size <= budget:
            break
        if inline == 0:
            raise WcError(f"push is {size} bytes even with every file as a blob; the budget is {budget}")
        inline = max(0, inline - (size - budget) - _ENVELOPE_SLACK)
    try:
        check = apply_ops(before, ops, _Blobs(after.store))
    except OpError as exc:
        raise WcError(f"internal: operations do not apply: {exc}") from exc
    if check.hash() != after.hash():
        raise WcError("internal: operations do not rebuild the working tree")
    blobs: dict[Hex, bytes] = {}
    for op in ops:
        if isinstance(op, Put) and op.data is None and op.sha256 not in blobs:
            blobs[op.sha256] = after.store.get(op.sha256)
    return env, list(blobs.values()), ops


def create_genesis(client: RepoClient, files: Mapping[str, tuple[Mode, bytes]], intent: str = "genesis") -> Hex:
    """Sign and send a genesis push (§12.2) for `files`. The `.gud/members` host line must hold
    the host log's key and the author must be a member with a push role and this key."""
    try:
        tree = Tree.from_files(client.store, files)
    except ObjectError as exc:
        raise WcError(f"genesis: {exc}") from exc
    problem = check_tree_shape(tree)
    if problem:
        raise WcError(f"genesis: {problem}")
    try:
        rules = rules_of(tree)
    except ConfigError as exc:
        raise WcError(f"genesis: a config file does not parse: {exc}") from exc
    host_key = client.info().get("host_key")
    if rules.members.host().public_key != host_key:
        raise WcError("genesis: the host line in .gud/members does not hold the host log's key")
    me = rules.members.get(client.handle)
    if me is None or me.role not in ("agent", "human", "console", "tester"):
        raise WcError(f"genesis: {client.handle} is not a member with a push role")
    if me.public_key != E.public_of(client._key):
        raise WcError(f"genesis: {client.handle}'s key in .gud/members is not this member's key")
    env, blobs, ops = build_push(client, ZERO, Tree.empty(client.store), tree, intent)
    return client.send(env, summary=intent.splitlines()[0] if intent else "genesis",
                       text=render_unified(Tree.empty(client.store), tree, ops), blobs=blobs)


def push_tree(client: RepoClient, parent: Hex, after: Tree, intent: str, *,
              headers: Mapping[str, str] | None = None) -> Hex:
    """Push `after` against link `parent` (no working copy; used by members pushes)."""
    before = client.reader().tree(parent)
    env, blobs, ops = build_push(client, parent, before, after, intent, headers=headers)
    return client.send(env, summary=intent.splitlines()[0] if intent else "push",
                       text=render_unified(before, after, ops), blobs=blobs)


# ---------------------------------------------------------------- conflict markers

def _changes(a: list[bytes], b: list[bytes]) -> list[tuple[int, int, tuple[bytes, ...]]]:
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [(i1, i2, tuple(b[j1:j2])) for tag, i1, i2, j1, j2 in m.get_opcodes() if tag != "equal"]


def _touch(x: tuple[int, int, object], y: tuple[int, int, object]) -> bool:
    a1, a2, _ = x
    b1, b2, _ = y
    if a1 == a2 and b1 == b2:
        return a1 == b1
    if a1 == a2:
        return b1 <= a1 < b2
    if b1 == b2:
        return a1 <= b1 < a2
    return a1 < b2 and b1 < a2


def _apply_region(base: list[bytes], start: int, end: int, changes: Sequence[tuple[int, int, tuple[bytes, ...]]]) -> list[bytes]:
    out: list[bytes] = []
    i = start
    for a1, a2, repl in sorted(changes, key=lambda c: (c[0], c[1])):
        out.extend(base[i:a1])
        out.extend(repl)
        i = max(i, a2)
    out.extend(base[i:end])
    return out


def conflict_markers(base: bytes, mine: bytes, theirs: bytes, tip_label: str = "tip") -> bytes:
    """A diff3-style merge of three text versions: clean where the sides do not overlap,
    `<<<<<<< mine` / `||||||| base` / `=======` / `>>>>>>> <tip_label>` blocks where they do."""
    b, m, t = split_lines(base), split_lines(mine), split_lines(theirs)
    tagged = [(c, 0) for c in _changes(b, m)] + [(c, 1) for c in _changes(b, t)]
    tagged.sort(key=lambda ct: (ct[0][0], ct[0][1], ct[1]))
    clusters: list[list[tuple[tuple[int, int, tuple[bytes, ...]], int]]] = []
    for item in tagged:
        if clusters and any(_touch(item[0], other[0]) for other in clusters[-1] if other[1] != item[1]):
            clusters[-1].append(item)
        elif clusters and any(_touch(item[0], other[0]) for other in clusters[-1]):
            clusters[-1].append(item)
        else:
            clusters.append([item])

    def ends(lines: list[bytes]) -> list[bytes]:
        if lines and not lines[-1].endswith(b"\n"):
            return lines[:-1] + [lines[-1] + b"\n"]
        return lines

    out: list[bytes] = []
    pos = 0
    for cl in clusters:
        start = min(c[0][0] for c in cl)
        end = max(c[0][1] for c in cl)
        out.extend(b[pos:start])
        mine_c = [c for c, side in cl if side == 0]
        their_c = [c for c, side in cl if side == 1]
        mine_v = _apply_region(b, start, end, mine_c)
        their_v = _apply_region(b, start, end, their_c)
        if not mine_c or not their_c or mine_v == their_v:
            out.extend(mine_v if mine_c else their_v)
        else:
            out.append(b"<<<<<<< mine\n")
            out.extend(ends(mine_v))
            out.append(b"||||||| base\n")
            out.extend(ends(b[start:end]))
            out.append(b"=======\n")
            out.extend(ends(their_v))
            out.append(f">>>>>>> {tip_label}\n".encode("utf-8"))
        pos = max(pos, end)
    out.extend(b[pos:])
    return b"".join(out)


# ---------------------------------------------------------------- the working copy

def find_root(start: Path) -> Path | None:
    """The nearest directory at or above `start` holding `.gud/BASE`."""
    cur = Path(start).resolve()
    for d in (cur, *cur.parents):
        if (d / BASE_FILE).is_file():
            return d
    return None


def read_local_config(root: Path) -> dict:
    path = Path(root) / LOCAL_DIR / "config.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def write_local_config(root: Path, data: Mapping[str, Any]) -> None:
    path = Path(root) / LOCAL_DIR / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(data), indent=1, sort_keys=True) + "\n", "utf-8")


class WorkingCopy:
    def __init__(self, root: Path, client: RepoClient) -> None:
        self.root = Path(root).resolve()
        self.client = client
        self.warnings: list[str] = []

    # ------------------------------------------------------------ clone

    @classmethod
    def clone(cls, client: RepoClient, root: Path, *, at: Hex | None = None, checkpoint: bool = True) -> WorkingCopy:
        root = Path(root)
        if root.exists() and not root.is_dir():
            raise WcError(f"{root} is not a directory")
        if root.exists() and any(p.name != ".gud" for p in root.iterdir()):
            raise WcError(f"{root} is not empty")
        if (root / BASE_FILE).exists():
            raise WcError(f"{root} is already a working copy")
        client.bootstrap(checkpoint=checkpoint)
        client.sync_log()
        chain = client.chain()
        if chain.tip is None:
            raise WcError("the repo has no genesis link yet")
        target = client.resolve(at) if at else chain.tip.state
        if chain.link(target) is None:
            raise WcError(f"{target[:12]} is not a link")
        root.mkdir(parents=True, exist_ok=True)
        wc = cls(root, client)
        wc._init_local()
        wc._materialize(client.reader().tree(target), current_base=Tree.empty(client.store))
        wc._write_base(target)
        wc._scan(client.reader().tree(target))
        return wc

    @classmethod
    def adopt(cls, client: RepoClient, root: Path, base: Hex) -> WorkingCopy:
        """Turn an existing directory whose files equal link `base` into a working copy (after
        `gud init` pushed its genesis): writes the tree's missing files and `.gud/BASE`."""
        wc = cls(root, client)
        wc._init_local()
        tree = client.reader().tree(base)
        wc._materialize(tree, current_base=Tree.empty(client.store))
        wc._write_base(base)
        wc._scan(tree)
        return wc

    def _init_local(self) -> None:
        (self.root / LOCAL_DIR).mkdir(parents=True, exist_ok=True)
        cfg = read_local_config(self.root)
        cfg.update({"handle": self.client.handle, "host_url": self.client.host_url, "repo": self.client.repo})
        write_local_config(self.root, cfg)

    # ------------------------------------------------------------ base

    @property
    def base(self) -> Hex:
        try:
            text = (self.root / BASE_FILE).read_text("ascii")
        except FileNotFoundError as exc:
            raise WcError(f"{self.root} is not a working copy (no {BASE_FILE})") from exc
        value = text.strip()
        if not E.is_hex64(value) or text != value + "\n":
            raise WcError(f"{BASE_FILE} is malformed")
        return value

    def _write_base(self, state: Hex) -> None:
        path = self.root / BASE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".BASE.{os.getpid()}.tmp")
        tmp.write_text(state + "\n", "ascii")
        os.replace(tmp, path)

    def _base_link(self) -> LinkState:
        base = self.base
        link = self.client.chain().link(base)
        if link is None:
            raise WcError(f"base {base[:12]} is not a link in the local cache; run `gud sync`")
        return link

    def _merge_base(self, base_link: LinkState, base_tree: Tree, wt: Tree, tip: LinkState) -> Tree:
        """The base for merging the tip into the working tree: `base_tree`, except that a path the
        working tree changed takes its entry at the latest link after the base where it already
        equals the working tree's (this member's own linked push, or an identical edit). Without
        this, a tip that built on top of one's own linked change (in the same lines) would
        conflict with it."""
        changed = list(base_tree.changed_paths(wt))
        if not changed:
            return base_tree
        links = [l for l in self.client.chain().links if base_link.seq < l.seq <= tip.seq]
        if not links:
            return base_tree
        reader = self.client.reader()
        adjusted = base_tree
        for link in reversed(links):
            if not changed:
                break
            tree = reader.tree(link.state)
            for path in list(changed):
                if tree.get(path) == wt.get(path):
                    entry = tree.get(path)
                    adjusted = adjusted.set_entry(path, entry) if entry is not None else (
                        adjusted.delete(path) if path in adjusted else adjusted)
                    changed.remove(path)
        if check_tree_shape(adjusted) is not None:
            return base_tree
        return adjusted

    def _base_tree(self) -> Tree:
        self._base_link()
        return self.client.reader().tree(self.base)

    # ------------------------------------------------------------ scanning

    def _index(self) -> dict:
        path = self.root / LOCAL_DIR / "index.json"
        try:
            return json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            return {"scan_ns": 0, "entries": {}}

    def _save_index(self, index: Mapping[str, Any]) -> None:
        path = self.root / LOCAL_DIR / "index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".index.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(index, separators=(",", ":"), sort_keys=True), "utf-8")
        os.replace(tmp, path)

    def _scan(self, base_tree: Tree) -> Tree:
        """The working tree, built as `base_tree` plus the differences found on disk."""
        store = self.client.store
        ignore = IgnoreRules.load(self.root, base_tree)
        old = self._index()
        old_entries: Mapping[str, list] = old.get("entries", {})
        trusted_before = int(old.get("scan_ns", 0)) - _RACY_NS
        scan_ns = time.time_ns()
        new_entries: dict[str, list] = {}
        found: dict[str, Entry] = {}
        self.warnings = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            names = list(filenames)
            keep = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                rel = f"{rel_dir}/{d}" if rel_dir else d
                if os.path.islink(full):
                    names.append(d)
                elif not ignore.ignored(rel, True):
                    keep.append(d)
            dirnames[:] = sorted(keep)
            for name in sorted(names):
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if ignore.ignored(rel):
                    continue
                if not valid_path(rel):
                    self.warnings.append(f"skipped {rel!r}: not a valid gud path (SPEC-v1 5.1)")
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    target = os.readlink(os.fsencode(full))
                    if not target or b"\0" in target:
                        self.warnings.append(f"skipped {rel}: empty symlink")
                        continue
                    found[rel] = Entry("120000", store.put(target), len(target))
                    continue
                if not stat.S_ISREG(st.st_mode):
                    self.warnings.append(f"skipped {rel}: not a regular file or symlink")
                    continue
                mode: Mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
                sig = [st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino]
                cached = old_entries.get(rel)
                sha = None
                if cached is not None and cached[:4] == sig and st.st_mtime_ns < trusted_before \
                        and store.has(cached[4]):
                    sha = cached[4]
                if sha is None:
                    with open(full, "rb") as fh:
                        data = fh.read()
                    sha = store.put(data)
                    size = len(data)
                else:
                    size = st.st_size
                new_entries[rel] = sig + [sha]
                found[rel] = Entry(mode, sha, size)
        tree = base_tree
        for path, entry in base_tree.items():
            if path not in found and not ignore.ignored(path):
                tree = tree.delete(path)
        for path in sorted(found, key=lambda p: p.encode("utf-8")):
            if tree.get(path) != found[path]:
                try:
                    tree = tree.set_entry(path, found[path])
                except ObjectError as exc:
                    self.warnings.append(f"skipped {path}: {exc}")
        self._save_index({"scan_ns": scan_ns, "entries": new_entries})
        return tree

    def working_tree(self) -> Tree:
        return self._scan(self._base_tree())

    # ------------------------------------------------------------ status / diff

    def _pushes(self) -> tuple[tuple[Hex, str], ...]:
        path = self.root / LOCAL_DIR / "pushes.jsonl"
        if not path.exists():
            return ()
        chain = self.client.chain()
        linked = {link.push for link in chain.links}
        out = []
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            pid = json.loads(line)["push"]
            if pid in linked:
                status = "linked"
            else:
                prop = chain.push_status(pid)
                status = prop.status if prop is not None else "sent"
            out.append((pid, status))
        return tuple(out)

    def status(self) -> Status:
        base_tree = self._base_tree()
        wt = self._scan(base_tree)
        added, modified, deleted, moded = [], [], [], []
        for path in base_tree.changed_paths(wt):
            a, b = base_tree.get(path), wt.get(path)
            if a is None:
                added.append(path)
            elif b is None:
                deleted.append(path)
            elif a.blob != b.blob or (a.mode == "120000") != (b.mode == "120000"):
                modified.append(path)
            else:
                moded.append(path)
        chain = self.client.chain()
        tip = chain.tip
        base_link = chain.link(self.base)
        behind = (tip.seq - base_link.seq) if tip is not None and base_link is not None else 0
        return Status(self.base, tip.state if tip else self.base, tuple(added), tuple(modified), tuple(deleted),
                      tuple(moded), behind, self._conflicts(), self._pushes())

    def _conflicts(self) -> tuple[str, ...]:
        path = self.root / LOCAL_DIR / "sync.json"
        if not path.exists():
            return ()
        return tuple(json.loads(path.read_text("utf-8")).get("conflicts", ()))

    def diff(self, paths: Sequence[str] = ()) -> str:
        base_tree = self._base_tree()
        wt = self._scan(base_tree)
        if paths:
            wanted = [p.strip("/") for p in paths]

            def chosen(path: str) -> bool:
                return any(path == w or path.startswith(w + "/") for w in wanted)

            filtered = base_tree
            for path in base_tree.changed_paths(wt):
                if not chosen(path):
                    continue
                entry = wt.get(path)
                filtered = filtered.delete(path) if entry is None else filtered.set_entry(path, entry)
            wt = filtered
        if wt.root == base_tree.root:
            return ""
        ops = diff_trees(base_tree, wt, inline_budget=1 << 62)
        return render_unified(base_tree, wt, ops, max_lines=None)

    # ------------------------------------------------------------ push

    def fast_forward(self) -> bool:
        """Move `.gud/BASE` to the tip when the working copy already holds everything the links
        since the base changed (for example after this member's own push linked). Writes no
        file; returns whether BASE moved."""
        client = self.client
        base_link = self._base_link()
        tip = client.chain().tip
        if tip is None or tip.state == base_link.state:
            return False
        base_tree = client.reader().tree(base_link.state)
        wt = self._scan(base_tree)
        tip_tree = client.reader().tree(tip.state)
        if tip.tree_hash not in self._pushed_trees():
            merged = three_way(self._merge_base(base_link, base_tree, wt, tip), wt, tip_tree)
            if merged.conflicts or merged.tree.hash() != wt.hash():
                return False
        self._write_base(tip.state)
        self._scan(tip_tree)
        self._clear_conflicts()
        return True

    def _pushed_trees(self) -> set[Hex]:
        """Tree hashes of the pushes sent from this working copy: when the tip's tree is one of
        them, every difference between the tip and the working tree is a local edit made since."""
        path = self.root / LOCAL_DIR / "pushes.jsonl"
        if not path.exists():
            return set()
        out = set()
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                tree = json.loads(line).get("tree")
                if tree:
                    out.add(tree)
        return out

    def push(self, intent: str, *, task: Hex | None = None, revises: Hex | None = None,
             carry: bool = True, allow_secret: Sequence[str] = (), allow_artifacts: bool = False) -> PushResult:
        client = self.client
        client.sync_log()
        self.fast_forward()
        base_link = self._base_link()
        base_tree = client.reader().tree(base_link.state)
        wt = self._scan(base_tree)
        if wt.root == base_tree.root or wt.hash() == base_tree.hash():
            raise WcError("nothing to push: the working copy equals its base")
        problem = check_tree_shape(wt)
        if problem:
            raise WcError(f"the working tree is not a valid gud tree: {problem}")
        try:
            rules_of(wt)
        except ConfigError as exc:
            raise WcError(f"a .gud config file does not parse, so the push could not link: {exc}") from exc
        touched = list(base_tree.changed_paths(wt))
        if not allow_artifacts:
            from .ignore import artifacts
            found = artifacts(p for p in touched if p in wt and p not in base_tree)
            if found:
                raise ArtifactsFound(found)
        hits = scan({p: wt.read(p) for p in touched if p in wt}, allow_secret)
        if hits:
            raise SecretsFound(hits)
        chain = client.chain()
        tip = chain.tip
        assert tip is not None
        if tip.state != base_link.state:
            if not carry:
                raise WcError(f"your base {base_link.state[:12]} is {tip.seq - base_link.seq} link(s) behind the "
                              f"tip and --no-carry was given; run `gud sync` first")
            since: set[str] = set()
            for link in chain.links[base_link.seq + 1:]:
                since.update(link.files)
            overlap = sorted(set(touched) & since)
            if overlap:
                mode = client.reader().rules().config.carry
                tip_tree = client.reader().tree()
                if mode == "file" or three_way(base_tree, wt, tip_tree, paths=overlap).conflicts:
                    raise WcError(f"links since your base changed {', '.join(overlap)}; the push would not "
                                  f"carry onto the tip. Run `gud sync` first")
        headers: dict[str, str] = {}
        if task:
            headers["Gud-Task"] = task
        if revises:
            headers["Gud-Revises"] = revises
        if not carry:
            headers["Gud-Carry"] = "none"
        env, blobs, ops = build_push(client, base_link.state, base_tree, wt, intent, headers=headers)
        push_id = client.send(env, summary=intent.splitlines()[0] if intent else "push",
                              text=render_unified(base_tree, wt, ops), blobs=blobs)
        try:
            forecast = client.reader().forecast(base_link.state, ops, client.handle, client.now_ms())
        except (OpError, ReplayError, ValueError, KeyError):
            forecast = None
        path = self.root / LOCAL_DIR / "pushes.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"push": push_id, "parent": base_link.state, "intent": intent, "tree": wt.hash(),
                                 "at": client.now_ms()}) + "\n")
        return PushResult(push_id, base_link.state, len(ops), len(blobs), forecast, tuple(touched))

    # ------------------------------------------------------------ sync

    def sync(self) -> SyncResult:
        client = self.client
        client.sync_log()
        base_link = self._base_link()
        tip = client.chain().tip
        assert tip is not None
        base_tree = client.reader().tree(base_link.state)
        wt = self._scan(base_tree)
        if tip.state == base_link.state:
            self._clear_conflicts()
            return SyncResult(True, tip.state, ())
        tip_tree = client.reader().tree(tip.state)
        merged = three_way(self._merge_base(base_link, base_tree, wt, tip), wt, tip_tree)
        if merged.conflicts:
            markers = self._record_conflicts(merged.conflicts, base_tree, wt, tip_tree, tip.state)
            return SyncResult(False, base_link.state, tuple(merged.conflicts), (), markers)
        written = tuple(wt.changed_paths(merged.tree))
        self._materialize(merged.tree, current_base=base_tree, current=wt)
        self._write_base(tip.state)
        self._scan(tip_tree)
        self._clear_conflicts()
        return SyncResult(True, tip.state, (), written)

    def _record_conflicts(self, conflicts: Sequence[str], base: Tree, mine: Tree, theirs: Tree, tip: Hex) -> tuple[str, ...]:
        candidates = sorted(base.changed_paths(mine), key=len, reverse=True)
        paths = []
        for line in conflicts:
            path = next((p for p in candidates if line.startswith(p + ": ")), line.split(": ", 1)[0])
            paths.append(path)
        cdir = self.root / LOCAL_DIR / "conflicts"
        self._clear_conflicts()
        markers = []
        for path in paths:
            b, m, t = base.get(path), mine.get(path), theirs.get(path)
            if not (b and m and t) or "120000" in (b.mode, m.mode, t.mode):
                continue
            bb, mb, tb = base.read(path), mine.read(path), theirs.read(path)
            if not (is_text(bb) and is_text(mb) and is_text(tb)) or not valid_path(path):
                continue
            out = cdir / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(conflict_markers(bb, mb, tb, f"tip {tip[:12]}"))
            markers.append(f"{LOCAL_DIR}/conflicts/{path}")
        state = {"tip": tip, "base": self.base, "conflicts": list(conflicts), "paths": paths, "markers": markers}
        (self.root / LOCAL_DIR / "sync.json").write_text(json.dumps(state, indent=1) + "\n", "utf-8")
        return tuple(markers)

    def _clear_conflicts(self) -> None:
        import shutil
        shutil.rmtree(self.root / LOCAL_DIR / "conflicts", ignore_errors=True)
        (self.root / LOCAL_DIR / "sync.json").unlink(missing_ok=True)

    # ------------------------------------------------------------ verify

    def verify(self, *, from_checkpoint: bool = True) -> list[str]:
        return verify_cache(self.client, from_checkpoint=from_checkpoint, base=self.base)

    # ------------------------------------------------------------ materialise

    def materialize(self, tree: Tree) -> None:
        """Make the (non-ignored) files equal `tree`, byte-exactly; nothing through symlinks."""
        try:
            current_base = self._base_tree()
        except WcError:
            current_base = Tree.empty(self.client.store)
        self._materialize(tree, current_base=current_base)
        self._scan(tree)

    def _materialize(self, tree: Tree, *, current_base: Tree, current: Tree | None = None) -> None:
        if current is None:
            current = self._scan(current_base)
        changed = list(current.changed_paths(tree))

        def mode_only(old: Entry | None, new: Entry | None) -> bool:
            return old is not None and new is not None and old.blob == new.blob \
                and "120000" not in (old.mode, new.mode)

        for path in changed:                      # removals first: a file may become a directory
            old, new = current.get(path), tree.get(path)
            if old is not None and not mode_only(old, new):
                self._remove(path)
        for path in changed:
            old, new = current.get(path), tree.get(path)
            if new is None:
                continue
            if mode_only(old, new):
                self._chmod(path, new.mode)
            else:
                self._write(path, new)

    def _safe_parent(self, path: str) -> Path:
        """Create the parent directories of `path` as real directories under the root."""
        cur = self.root
        segs = path.split("/")[:-1]
        for seg in segs:
            cur = cur / seg
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                os.mkdir(cur)
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                os.unlink(cur)
                os.mkdir(cur)
        return cur

    def _parent_ok(self, path: str) -> Path | None:
        """The parent directory if every component is a real directory, else None."""
        cur = self.root
        for seg in path.split("/")[:-1]:
            cur = cur / seg
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                return None
        return cur

    def _remove(self, path: str) -> None:
        parent = self._parent_ok(path)
        if parent is None:
            return
        full = parent / path.rsplit("/", 1)[-1]
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            return
        os.unlink(full)
        cur = parent
        while cur != self.root:
            try:
                os.rmdir(cur)
            except OSError:
                break
            cur = cur.parent

    def _chmod(self, path: str, mode: Mode) -> None:
        parent = self._parent_ok(path)
        if parent is None:
            return
        full = parent / path.rsplit("/", 1)[-1]
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fchmod(fd, 0o755 if mode == "100755" else 0o644)
        finally:
            os.close(fd)

    def _write(self, path: str, entry: Entry) -> None:
        data = self.client.store.get(entry.blob)
        if sha256_hex(data) != entry.blob:
            raise WcError(f"object store holds wrong bytes for {path}; run `gud verify`")
        parent = self._safe_parent(path)
        name = path.rsplit("/", 1)[-1]
        full = parent / name
        try:
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                try:
                    os.rmdir(full)
                except OSError as exc:
                    raise WcError(f"cannot replace directory {path} with a file: it is not empty") from exc
        except FileNotFoundError:
            pass
        tmp = parent / f".gud-tmp.{os.getpid()}.{name}"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        if entry.mode == "120000":
            os.symlink(data, os.fsencode(tmp))
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp, flags, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=False) as fh:
                    fh.write(data)
                os.fchmod(fd, 0o755 if entry.mode == "100755" else 0o644)
            finally:
                os.close(fd)
        os.replace(tmp, full)


# ---------------------------------------------------------------- verify

def _diff_maps(name: str, a: Mapping[str, Any], b: Mapping[str, Any], out: list[str], limit: int = 20) -> None:
    keys = sorted(set(a) | set(b))
    n = 0
    for key in keys:
        if a.get(key) != b.get(key):
            n += 1
            if n <= limit:
                if key not in a:
                    out.append(f"{name} {key[:12]}: missing from the cache")
                elif key not in b:
                    out.append(f"{name} {key[:12]}: in the cache but not in the rebuilt state")
                else:
                    out.append(f"{name} {key[:12]}: differs from the rebuilt state")
    if n > limit:
        out.append(f"{name}: {n - limit} more differences")


def verify_cache(client: RepoClient, *, from_checkpoint: bool = True, base: Hex | None = None) -> list[str]:
    """§22 verify: differences between the cache and a fresh replay from the mirror; empty = ok."""
    out: list[str] = []
    try:
        client.sync_log()
    except RemoteError as exc:
        out.append(f"sync failed: {exc}")
    cache = client.replayer()
    try:
        fresh = client.rebuild(from_checkpoint=from_checkpoint)
    except (RemoteError, ReplayError) as exc:
        return out + [f"rebuild failed: {exc}"]
    out += [f"mirror: {p}" for p in client.last_problems]
    a, b = cache.chain(), fresh.chain()
    if len(a.links) != len(b.links):
        out.append(f"links: the cache has {len(a.links)}, the rebuilt chain has {len(b.links)}")
    for la, lb in zip(a.links, b.links):
        if la != lb:
            fields = [f for f in la.__dataclass_fields__ if getattr(la, f) != getattr(lb, f)]
            out.append(f"link {la.seq}: {', '.join(fields)} differ (cache {la.state[:12]}, rebuilt {lb.state[:12]})")
    _diff_maps("proposal", a.proposals, b.proposals, out)
    _diff_maps("task", a.tasks, b.tasks, out)
    _diff_maps("claim", a.claims, b.claims, out)
    _diff_maps("escalation", a.escalations, b.escalations, out)
    if a.red != b.red:
        out.append("red state differs")
    if a.log_head != b.log_head:
        out.append(f"log head: cache {a.log_head.render()[:20]}, rebuilt {b.log_head.render()[:20]}")
    if not out and cache.snapshot().hash() != fresh.snapshot().hash():
        out.append("snapshot hash differs from the rebuilt state")
    reader = client.reader()
    for label, state in (("tip", a.tip.state if a.tip else None), ("base", base)):
        if state is None:
            continue
        link = a.link(state)
        if link is None:
            out.append(f"{label} {state[:12]} is not a link in the cache")
            continue
        if b.link(state) is None:
            out.append(f"{label} {state[:12]} is not a link in the rebuilt chain")
        try:
            tree = reader.tree(state)
            if tree.hash() != link.tree_hash:
                out.append(f"{label} tree hash {tree.hash()[:12]} != link tree hash {link.tree_hash[:12]}")
            for path, entry in tree.items():
                data = client.store.get(entry.blob)
                if sha256_hex(data) != entry.blob or len(data) != entry.size:
                    out.append(f"{label} {path}: cached bytes do not match their hash")
        except (KeyError, ObjectError) as exc:
            out.append(f"{label} tree: object store is missing {exc}")
    return out
