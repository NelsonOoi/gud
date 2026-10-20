"""Content-addressed storage and persistent, structurally shared trees and blame (SPEC §5.1, §5.2, §5.6).

Layout (private): one content-defined node tree per directory (see the engine section).
Node boundaries depend only on keys, so node ids depend only on the map's contents, never on
its edit history: equal maps have equal roots. Changing one path rewrites the few small nodes
on its path (about 8 items each); every other node is shared with the previous version.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .types import MODES, Handle, Hex, Mode, ObjectStoreLike


class ObjectError(ValueError):
    pass


def _sha(data: bytes) -> Hex:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- stores

class MemoryStore:
    """In-memory content-addressed store."""

    def __init__(self) -> None:
        self._data: dict[Hex, bytes] = {}

    def get(self, key: Hex) -> bytes:
        return self._data[key]

    def put(self, data: bytes) -> Hex:
        key = _sha(data)
        if key not in self._data:
            self._data[key] = bytes(data)
        return key

    def has(self, key: Hex) -> bool:
        return key in self._data


class DiskStore:
    """`<root>/<2 hex>/<62 hex>`, written atomically (temp file + rename)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: Hex) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise KeyError(key)
        return self.root / key[:2] / key[2:]

    def get(self, key: Hex) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def put(self, data: bytes) -> Hex:
        key = _sha(data)
        path = self._path(key)
        if path.exists():
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        return key

    def has(self, key: Hex) -> bool:
        try:
            return self._path(key).exists()
        except KeyError:
            return False


# ---------------------------------------------------------------- paths and tree hash

def valid_path(path: str) -> bool:
    """§5.1 path rules, including the reserved `.gud/BASE` and `.gud/local/`."""
    if not isinstance(path, str) or not path or "\0" in path:
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    for seg in path.split("/"):
        if seg in ("", ".", "..") or seg.encode("utf-8").lower() == b".git":
            return False
    if path == ".gud/BASE" or path == ".gud/local" or path.startswith(".gud/local/"):
        return False
    return True


def _hash_line(path: str, mode: str, blob: Hex) -> bytes:
    return path.encode("utf-8") + b"\0" + mode.encode("ascii") + b"\0" + blob.encode("ascii") + b"\n"


def tree_hash(entries: Iterable[tuple[str, Mode, Hex]]) -> Hex:
    """§5.2 over (path, mode, sha256(bytes)); sorted by path bytes here."""
    h = hashlib.sha256()
    for path, mode, blob in sorted(entries, key=lambda e: e[0].encode("utf-8")):
        h.update(_hash_line(path, mode, blob))
    return h.hexdigest()


# ---------------------------------------------------------------- persistent map engine
#
# A directory is a content-defined tree of nodes (a "prolly tree"). Height-0 nodes (chunks)
# hold sorted entries; height-h nodes (h >= 1) hold (last key, count, child) refs to
# height-(h-1) nodes. Every key has a level: the number of leading all-zero 5-bit groups of
# its hash, so P(level >= k) = 32**-k. A height-h node ends right after an item whose key
# has level >= h + 1. The root of a directory is the single node at the lowest height >= 1
# that holds all items. Node ids therefore depend only on contents, never on history.

_DIR_MAGIC = b"gud-dir/1"
_CHUNK_MAGIC = b"gud-chunk/1"
_LEVEL_BITS = 3
_LEVEL_MASK = (1 << _LEVEL_BITS) - 1


@dataclass(frozen=True)
class _Sub:
    node: Hex
    count: int


@dataclass(frozen=True)
class _Dir:
    height: int
    keys: tuple[str, ...]      # last key under each child
    counts: tuple[int, ...]
    children: tuple[Hex, ...]
    count: int


@dataclass(frozen=True)
class _Chunk:
    keys: tuple[str, ...]
    values: tuple[Any, ...]    # tuple[str, ...] for a leaf, _Sub for a sub-directory


class _LRU(OrderedDict):
    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap

    def lookup(self, key):
        try:
            value = self[key]
            self.move_to_end(key)
        except KeyError:   # absent, or evicted by another thread in between
            return None
        return value

    def remember(self, key, value) -> None:
        self[key] = value
        try:
            self.move_to_end(key)
            while len(self) > self.cap:
                self.popitem(last=False)
        except KeyError:
            pass


# Node ids are content hashes, so a parse cache keyed by id is valid across stores.
_NODES = _LRU(1 << 16)
_PARTS = _LRU(1 << 16)
_HASHES = _LRU(1 << 12)


@lru_cache(maxsize=1 << 17)
def _level(key: str) -> int:
    value = int.from_bytes(hashlib.sha256(b"gud-chunk\0" + key.encode("utf-8")).digest()[:8], "big")
    level = 0
    while level < 16 and value & _LEVEL_MASK == 0:
        value >>= _LEVEL_BITS
        level += 1
    return level


def _encode_dir(height: int, refs: Sequence[tuple[str, int, Hex]]) -> bytes:
    out = [_DIR_MAGIC, str(height).encode("ascii")]
    for key, count, child in refs:
        out += [key.encode("utf-8"), str(count).encode("ascii"), child.encode("ascii")]
    return b"\0".join(out) + b"\0"


def _encode_chunk(arity: int, entries: Sequence[tuple[str, Any]]) -> bytes:
    out = [_CHUNK_MAGIC, str(arity).encode("ascii")]
    for key, value in entries:
        out.append(key.encode("utf-8"))
        if isinstance(value, _Sub):
            out += [b"d", value.node.encode("ascii"), str(value.count).encode("ascii")]
        else:
            if len(value) != arity:
                raise ObjectError("value arity mismatch")
            out.append(b"f")
            out += [field.encode("utf-8") for field in value]
    return b"\0".join(out) + b"\0"


EMPTY_DIR: Hex = _sha(_encode_dir(1, ()))
_NODES.remember(EMPTY_DIR, _Dir(1, (), (), (), 0))


def _read_dir(store: ObjectStoreLike, node: Hex) -> _Dir:
    cached = _NODES.lookup(node)
    if cached is not None:
        if not isinstance(cached, _Dir):
            raise ObjectError(f"not a directory node: {node}")
        return cached
    parts = store.get(node).split(b"\0")
    if parts[0] != _DIR_MAGIC or parts[-1] != b"" or (len(parts) - 3) % 3:
        raise ObjectError(f"not a directory node: {node}")
    keys, counts, children = [], [], []
    for i in range(2, len(parts) - 1, 3):
        keys.append(parts[i].decode("utf-8"))
        counts.append(int(parts[i + 1]))
        children.append(parts[i + 2].decode("ascii"))
    parsed = _Dir(int(parts[1]), tuple(keys), tuple(counts), tuple(children), sum(counts))
    _NODES.remember(node, parsed)
    return parsed


def _read_chunk(store: ObjectStoreLike, node: Hex) -> _Chunk:
    cached = _NODES.lookup(node)
    if cached is not None:
        return cached
    parts = store.get(node).split(b"\0")
    if parts[0] != _CHUNK_MAGIC or parts[-1] != b"":
        raise ObjectError(f"not a chunk node: {node}")
    arity = int(parts[1])
    keys, values = [], []
    i = 2
    end = len(parts) - 1
    while i < end:
        keys.append(parts[i].decode("utf-8"))
        if parts[i + 1] == b"d":
            values.append(_Sub(parts[i + 2].decode("ascii"), int(parts[i + 3])))
            i += 4
        else:
            values.append(tuple(p.decode("utf-8") for p in parts[i + 2:i + 2 + arity]))
            i += 2 + arity
    parsed = _Chunk(tuple(keys), tuple(values))
    _NODES.remember(node, parsed)
    return parsed


def _items(store: ObjectStoreLike, node: Hex, height: int) -> list[tuple[Any, ...]]:
    """Items of a node: (key, value) entries at height 0, (key, count, child) refs above."""
    if height == 0:
        chunk = _read_chunk(store, node)
        return list(zip(chunk.keys, chunk.values))
    d = _read_dir(store, node)
    return list(zip(d.keys, d.counts, d.children))


def _count(item: tuple[Any, ...], height: int) -> int:
    if height:
        return item[1]
    return item[1].count if isinstance(item[1], _Sub) else 1


def _write_level(store: ObjectStoreLike, arity: int, items: Sequence[tuple[Any, ...]],
                 height: int) -> list[tuple[str, int, Hex]]:
    """Split height-`height` items into nodes at their boundaries; store them; return refs."""
    refs = []
    start = 0
    for i, item in enumerate(items):
        if _level(item[0]) > height or i == len(items) - 1:
            run = items[start:i + 1]
            count = sum(_count(it, height) for it in run)
            if height == 0:
                node = store.put(_encode_chunk(arity, run))
                _NODES.remember(node, _Chunk(tuple(k for k, _ in run), tuple(v for _, v in run)))
            else:
                node = store.put(_encode_dir(height, run))
                _NODES.remember(node, _Dir(height, tuple(r[0] for r in run), tuple(r[1] for r in run),
                                           tuple(r[2] for r in run), count))
            refs.append((item[0], count, node))
            start = i + 1
    return refs


def _write_root(store: ObjectStoreLike, arity: int, items: Sequence[tuple[Any, ...]],
                height: int) -> tuple[Hex, int]:
    """Build upward from height-`height` items to the canonical directory root."""
    if not items:
        return EMPTY_DIR, 0
    while True:
        refs = _write_level(store, arity, items, height)
        height += 1
        if len(refs) == 1 and height > 1:
            root, count = refs[0][2], refs[0][1]
            break
        items = refs
    d = _read_dir(store, root)
    while d.height > 1 and len(d.children) == 1:
        root = d.children[0]
        d = _read_dir(store, root)
    return root, count


def _dir_find(store: ObjectStoreLike, d: _Dir, key: str) -> Any:
    while True:
        i = bisect_left(d.keys, key)
        if i == len(d.keys):
            return None
        if d.height == 1:
            chunk = _read_chunk(store, d.children[i])
            j = bisect_left(chunk.keys, key)
            if j < len(chunk.keys) and chunk.keys[j] == key:
                return chunk.values[j]
            return None
        d = _read_dir(store, d.children[i])


def _dir_update(store: ObjectStoreLike, arity: int, root: Hex, key: str, value: Any) -> tuple[Hex, int]:
    """Set (value) or remove (None) one key of a directory; returns (new root, file count).

    Rewrites the nodes on the path to the key and, where a boundary key is removed, merges
    with the next node at each height (which may sit under a different parent)."""
    top = _read_dir(store, root)
    if not top.children:
        if value is None:
            raise KeyError(key)
        return _write_root(store, arity, [(key, value)], 0)
    height = top.height
    work: dict[int, list[tuple[Any, ...]]] = {}
    lo: dict[int, int] = {}
    hi: dict[int, int] = {}
    node = root
    for h in range(height, 0, -1):
        work[h] = _items(store, node, h)
        i = min(bisect_left([it[0] for it in work[h]], key), len(work[h]) - 1)
        lo[h], hi[h] = i, i + 1
        node = work[h][i][2]
    entries = _items(store, node, 0)
    j = bisect_left([k for k, _ in entries], key)
    present = j < len(entries) and entries[j][0] == key
    if value is None:
        if not present:
            raise KeyError(key)
        del entries[j]
    elif present:
        if entries[j][1] == value:
            return root, top.count
        entries[j] = (key, value)
    else:
        entries.insert(j, (key, value))
    work[0] = entries

    def extend(h: int) -> bool:
        """Append the items of the next height-h node (in key order) to work[h]."""
        if h >= height:
            return False
        if hi[h + 1] >= len(work[h + 1]) and not extend(h + 1):
            return False
        work[h].extend(_items(store, work[h + 1][hi[h + 1]][2], h))
        hi[h + 1] += 1
        return True

    for h in range(height):
        items = work[h]
        while not items or _level(items[-1][0]) <= h:
            if not extend(h):
                break
        work[h + 1][lo[h + 1]:hi[h + 1]] = _write_level(store, arity, items, h)
    return _write_root(store, arity, work[height], height)


def _map_get(store: ObjectStoreLike, root: Hex, path: str) -> Any:
    if not path:
        return None
    segs = path.split("/")
    node = root
    for seg in segs[:-1]:
        sub = _dir_find(store, _read_dir(store, node), seg + "/")
        if not isinstance(sub, _Sub):
            return None
        node = sub.node
    value = _dir_find(store, _read_dir(store, node), segs[-1])
    return None if isinstance(value, _Sub) else value


def _map_set(store: ObjectStoreLike, arity: int, node: Hex, segs: list[str], value: Any,
             prefix: str = "") -> tuple[Hex, int]:
    d = _read_dir(store, node)
    name = segs[0]
    if len(segs) == 1:
        if value is not None and _dir_find(store, d, name + "/") is not None:
            raise ObjectError(f"{prefix + name} is a directory")
        return _dir_update(store, arity, node, name, value)
    if _dir_find(store, d, name) is not None:
        if value is None:
            raise KeyError(prefix + "/".join(segs))
        raise ObjectError(f"{prefix + name} is a file")
    sub = _dir_find(store, d, name + "/")
    if sub is None and value is None:
        raise KeyError(prefix + "/".join(segs))
    sub_node, count = _map_set(store, arity, sub.node if sub else EMPTY_DIR, segs[1:], value,
                               prefix + name + "/")
    return _dir_update(store, arity, node, name + "/", _Sub(sub_node, count) if count else None)


def _map_build(store: ObjectStoreLike, arity: int, items: Iterable[tuple[str, tuple[str, ...]]]) -> Hex:
    top: dict[str, Any] = {}
    for path, value in items:
        segs = path.split("/")
        d = top
        for seg in segs[:-1]:
            if seg in d:
                raise ObjectError(f"{seg} is both a file and a directory")
            d = d.setdefault(seg + "/", {})
        leaf = segs[-1]
        if leaf + "/" in d:
            raise ObjectError(f"{path} is both a file and a directory")
        d[leaf] = value

    def build(d: dict[str, Any]) -> tuple[Hex, int]:
        entries: list[tuple[str, Any]] = []
        for key in sorted(d):
            value = d[key]
            if isinstance(value, dict):
                node, count = build(value)
                entries.append((key, _Sub(node, count)))
            else:
                entries.append((key, value))
        return _write_root(store, arity, entries, 0)

    return build(top)[0]


def _leaves(store: ObjectStoreLike, node: Hex) -> Iterator[Hex]:
    d = _read_dir(store, node)
    for child in d.children:
        if d.height == 1:
            yield child
        else:
            yield from _leaves(store, child)


def _map_walk(store: ObjectStoreLike, node: Hex, prefix: str = "") -> Iterator[tuple[str, tuple[str, ...]]]:
    # Keys sort with "/" appended to directory names, which is exactly full-path byte order.
    for chunk_id in _leaves(store, node):
        chunk = _read_chunk(store, chunk_id)
        for key, value in zip(chunk.keys, chunk.values):
            if isinstance(value, _Sub):
                yield from _map_walk(store, value.node, prefix + key)
            else:
                yield prefix + key, value


def _map_changed(store: ObjectStoreLike, a: Hex, b: Hex, prefix: str, out: list[str]) -> None:
    if a == b:
        return
    la, lb = list(_leaves(store, a)), list(_leaves(store, b))
    shared = set(la) & set(lb)

    def entries(leaves: list[Hex]) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for chunk_id in leaves:
            if chunk_id not in shared:
                chunk = _read_chunk(store, chunk_id)
                found.update(zip(chunk.keys, chunk.values))
        return found

    ea, eb = entries(la), entries(lb)
    for key in ea.keys() | eb.keys():
        va, vb = ea.get(key), eb.get(key)
        if va == vb:
            continue
        if key.endswith("/"):
            _map_changed(store, va.node if va else EMPTY_DIR, vb.node if vb else EMPTY_DIR, prefix + key, out)
        else:
            out.append(prefix + key)


def _hash_parts(store: ObjectStoreLike, chunk_id: Hex, prefix: str) -> tuple[Any, ...]:
    """A chunk's §5.2 lines at `prefix`, as bytes runs and (sub node, sub prefix) references."""
    key = (prefix, chunk_id)
    cached = _PARTS.lookup(key)
    if cached is not None:
        return cached
    parts: list[Any] = []
    run = bytearray()
    chunk = _read_chunk(store, chunk_id)
    for name, value in zip(chunk.keys, chunk.values):
        if isinstance(value, _Sub):
            if run:
                parts.append(bytes(run))
                run = bytearray()
            parts.append((value.node, prefix + name))
        else:
            run += _hash_line(prefix + name, value[0], value[1])
    if run:
        parts.append(bytes(run))
    result = tuple(parts)
    _PARTS.remember(key, result)
    return result


def _hash_into(h: Any, store: ObjectStoreLike, node: Hex, prefix: str) -> None:
    for chunk_id in _leaves(store, node):
        for part in _hash_parts(store, chunk_id, prefix):
            if isinstance(part, bytes):
                h.update(part)
            else:
                _hash_into(h, store, part[0], part[1])


# ---------------------------------------------------------------- Tree

@dataclass(frozen=True)
class Entry:
    mode: Mode
    blob: Hex
    size: int


def _check_path(path: str) -> None:
    if not valid_path(path):
        raise ObjectError(f"invalid path {path!r}")


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ObjectError(f"invalid mode {mode!r}")


class Tree:
    """Immutable path -> Entry map. Nodes are stored in the store; unchanged subtrees are shared."""

    __slots__ = ("_store", "_root")

    def __init__(self, store: ObjectStoreLike, root: Hex) -> None:
        self._store = store
        self._root = root

    @classmethod
    def empty(cls, store: ObjectStoreLike) -> Tree:
        store.put(_encode_dir(1, ()))
        return cls(store, EMPTY_DIR)

    @classmethod
    def load(cls, store: ObjectStoreLike, root: Hex) -> Tree:
        if root != EMPTY_DIR and not store.has(root):
            raise ObjectError(f"unknown tree root {root}")
        _read_dir(store, root)
        return cls(store, root)

    @classmethod
    def from_files(cls, store: ObjectStoreLike, files: Mapping[str, tuple[Mode, bytes]]) -> Tree:
        items = []
        for path in files:
            mode, data = files[path]
            _check_path(path)
            _check_mode(mode)
            _check_symlink(mode, data)
            items.append((path, (mode, store.put(data), str(len(data)))))
        return cls(store, _map_build(store, 3, items))

    @property
    def store(self) -> ObjectStoreLike:
        return self._store

    @property
    def root(self) -> Hex:
        return self._root

    def hash(self) -> Hex:
        cached = _HASHES.lookup(self._root)
        if cached is None:
            h = hashlib.sha256()
            _hash_into(h, self._store, self._root, "")
            cached = h.hexdigest()
            _HASHES.remember(self._root, cached)
        return cached

    def get(self, path: str) -> Entry | None:
        value = _map_get(self._store, self._root, path)
        return None if value is None else Entry(value[0], value[1], int(value[2]))

    def read(self, path: str) -> bytes:
        entry = self.get(path)
        if entry is None:
            raise KeyError(path)
        return self._store.get(entry.blob)

    def set(self, path: str, mode: Mode, data: bytes) -> Tree:
        _check_mode(mode)
        _check_symlink(mode, data)
        return self.set_entry(path, Entry(mode, self._store.put(data), len(data)))

    def set_entry(self, path: str, entry: Entry) -> Tree:
        _check_path(path)
        _check_mode(entry.mode)
        if entry.mode == "120000" and entry.size == 0:
            raise ObjectError(f"{path}: empty symlink target")
        value = (entry.mode, entry.blob, str(entry.size))
        root, _ = _map_set(self._store, 3, self._root, path.split("/"), value)
        return Tree(self._store, root)

    def delete(self, path: str) -> Tree:
        if not path:
            raise KeyError(path)
        root, _ = _map_set(self._store, 3, self._root, path.split("/"), None)
        return Tree(self._store, root)

    def items(self) -> Iterator[tuple[str, Entry]]:
        for path, value in _map_walk(self._store, self._root):
            yield path, Entry(value[0], value[1], int(value[2]))

    def paths(self) -> Iterator[str]:
        for path, _ in _map_walk(self._store, self._root):
            yield path

    def __len__(self) -> int:
        return _read_dir(self._store, self._root).count

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and _map_get(self._store, self._root, path) is not None

    def _is_dir(self, path: str) -> bool:
        """True when some path in the tree lies under `path + "/"`."""
        node = self._root
        for seg in path.split("/"):
            sub = _dir_find(self._store, _read_dir(self._store, node), seg + "/")
            if not isinstance(sub, _Sub):
                return False
            node = sub.node
        return True

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Tree) and other._root == self._root

    def __hash__(self) -> int:
        return hash(("Tree", self._root))

    def __repr__(self) -> str:
        return f"Tree({self._root[:12]}, {len(self)} files)"

    def changed_paths(self, other: Tree) -> Iterator[str]:
        """Paths whose entry differs between the two trees (incl. added/removed), sorted."""
        out: list[str] = []
        _map_changed(self._store, self._root, other._root, "", out)
        out.sort(key=lambda p: p.encode("utf-8"))
        return iter(out)

    def to_files(self) -> dict[str, tuple[Mode, bytes]]:
        return {path: (entry.mode, self._store.get(entry.blob)) for path, entry in self.items()}


def _check_symlink(mode: str, data: bytes) -> None:
    if mode == "120000" and (not data or b"\0" in data):
        raise ObjectError("symlink target must be non-empty and contain no NUL")


def check_tree_shape(tree: Tree) -> str | None:
    """§5.1: valid paths, no path a proper prefix of another, non-empty symlinks. None if ok."""
    dirs: set[str] = set()
    files: list[str] = []
    for path, entry in tree.items():
        if not valid_path(path):
            return f"invalid path {path!r}"
        if entry.mode not in MODES:
            return f"{path}: invalid mode"
        if entry.mode == "120000" and entry.size == 0:
            return f"{path}: empty symlink"
        segs = path.split("/")
        for i in range(1, len(segs)):
            dirs.add("/".join(segs[:i]))
        files.append(path)
    for path in files:
        if path in dirs:
            return f"{path} is both a file and a directory"
    return None


# ---------------------------------------------------------------- BlameMap

@dataclass(frozen=True)
class BlameRun:
    owner: Handle
    seq: int
    length: int


_BLAME_MAGIC = b"gud-blame/1\n"


def _encode_runs(runs: Sequence[BlameRun]) -> bytes:
    return _BLAME_MAGIC + b"".join(f"{r.owner} {r.seq} {r.length}\n".encode("utf-8") for r in runs)


def _compact(runs: Iterable[BlameRun]) -> tuple[BlameRun, ...]:
    out: list[BlameRun] = []
    for run in runs:
        if run.length <= 0:
            continue
        if out and out[-1].owner == run.owner and out[-1].seq == run.seq:
            out[-1] = BlameRun(run.owner, run.seq, out[-1].length + run.length)
        else:
            out.append(run)
    return tuple(out)


class BlameMap:
    """Immutable path -> tuple[BlameRun, ...], structurally shared like Tree; one stored object per file."""

    __slots__ = ("_store", "_root")

    def __init__(self, store: ObjectStoreLike, root: Hex) -> None:
        self._store = store
        self._root = root

    @classmethod
    def empty(cls, store: ObjectStoreLike) -> BlameMap:
        store.put(_encode_dir(1, ()))
        return cls(store, EMPTY_DIR)

    @classmethod
    def load(cls, store: ObjectStoreLike, root: Hex) -> BlameMap:
        if root != EMPTY_DIR and not store.has(root):
            raise ObjectError(f"unknown blame root {root}")
        _read_dir(store, root)
        return cls(store, root)

    @classmethod
    def _build(cls, store: ObjectStoreLike, files: Mapping[str, Sequence[BlameRun]]) -> BlameMap:
        items = []
        for path in files:
            _check_path(path)
            compact = _compact(files[path])
            key = store.put(_encode_runs(compact))
            _NODES.remember(key, compact)
            items.append((path, (key,)))
        return cls(store, _map_build(store, 1, items))

    @property
    def root(self) -> Hex:
        return self._root

    def _runs(self, key: Hex) -> tuple[BlameRun, ...]:
        cached = _NODES.lookup(key)
        if cached is not None:
            return cached
        data = self._store.get(key)
        if not data.startswith(_BLAME_MAGIC):
            raise ObjectError(f"not a blame object: {key}")
        runs = []
        for line in data[len(_BLAME_MAGIC):].decode("utf-8").splitlines():
            owner, seq, length = line.split(" ")
            runs.append(BlameRun(owner, int(seq), int(length)))
        result = tuple(runs)
        _NODES.remember(key, result)
        return result

    def get(self, path: str) -> tuple[BlameRun, ...]:
        value = _map_get(self._store, self._root, path)
        return () if value is None else self._runs(value[0])

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and _map_get(self._store, self._root, path) is not None

    def set(self, path: str, runs: Sequence[BlameRun]) -> BlameMap:
        _check_path(path)
        compact = _compact(runs)
        key = self._store.put(_encode_runs(compact))
        _NODES.remember(key, compact)
        root, _ = _map_set(self._store, 1, self._root, path.split("/"), (key,))
        return BlameMap(self._store, root)

    def delete(self, path: str) -> BlameMap:
        if not path:
            raise KeyError(path)
        root, _ = _map_set(self._store, 1, self._root, path.split("/"), None)
        return BlameMap(self._store, root)

    def rename(self, src: str, dst: str) -> BlameMap:
        value = _map_get(self._store, self._root, src)
        if value is None:
            raise KeyError(src)
        _check_path(dst)
        root, _ = _map_set(self._store, 1, self._root, src.split("/"), None)
        root, _ = _map_set(self._store, 1, root, dst.split("/"), value)
        return BlameMap(self._store, root)

    def lines(self, path: str) -> Iterator[tuple[Handle, int]]:
        for run in self.get(path):
            for _ in range(run.length):
                yield run.owner, run.seq

    def paths(self) -> Iterator[str]:
        for path, _ in _map_walk(self._store, self._root):
            yield path

    def __eq__(self, other: object) -> bool:
        return isinstance(other, BlameMap) and other._root == self._root

    def __hash__(self) -> int:
        return hash(("BlameMap", self._root))
