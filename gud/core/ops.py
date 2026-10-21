"""Change operations (SPEC §5.3), strict byte-wise patches (§5.4), diffing, three-way merge and blame (§5.6)."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol, Sequence, Union

from .objects import BlameMap, BlameRun, Entry, ObjectError, Tree, valid_path
from .types import MODES, REGULAR_MODES, Handle, Hex, Mode, Record


class OpError(ValueError):
    """An operation does not apply; the message says which and why."""


@dataclass(frozen=True)
class Patch:
    path: str
    hunks: bytes


@dataclass(frozen=True)
class Put:
    path: str
    mode: Mode
    sha256: Hex
    size: int
    data: bytes | None


@dataclass(frozen=True)
class Delete:
    path: str


@dataclass(frozen=True)
class Rename:
    src: str
    dst: str


@dataclass(frozen=True)
class ModeOp:
    path: str
    mode: Mode


ChangeOp = Union[Patch, Put, Delete, Rename, ModeOp]


class BlobSource(Protocol):
    def blob(self, sha256: Hex) -> bytes | None: ...


def _key(path: str) -> bytes:
    return path.encode("utf-8")


def _sha(data: bytes) -> Hex:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- records <-> ops

_HEX64 = re.compile(r"[0-9a-f]{64}")
_UINT = re.compile(r"0|[1-9][0-9]*")


def ops_to_records(ops: Sequence[ChangeOp]) -> tuple[Record, ...]:
    out = []
    for op in ops:
        if isinstance(op, Patch):
            out.append(Record("patch", (op.path,), op.hunks))
        elif isinstance(op, Put):
            data = op.data if op.data is not None else b""
            out.append(Record("put", (op.path, op.mode, op.sha256, str(op.size)), data))
        elif isinstance(op, Delete):
            out.append(Record("delete", (op.path,), b""))
        elif isinstance(op, Rename):
            out.append(Record("rename", (op.src, op.dst), b""))
        elif isinstance(op, ModeOp):
            out.append(Record("mode", (op.path, op.mode), b""))
        else:
            raise OpError(f"not a change operation: {op!r}")
    return tuple(out)


def _need_path(path: str) -> str:
    if not valid_path(path):
        raise OpError(f"invalid path {path!r}")
    return path


def records_to_ops(records: Sequence[Record]) -> tuple[ChangeOp, ...]:
    """Op records -> ops (`text` records are skipped). Raises OpError on unknown or malformed records."""
    ops: list[ChangeOp] = []
    arity = {"patch": 1, "put": 4, "delete": 1, "rename": 2, "mode": 2}
    for record in records:
        name, args, data = record.name, record.args, record.data
        if name == "text":
            continue
        if name not in arity:
            raise OpError(f"unknown record {name!r}")
        if len(args) != arity[name]:
            raise OpError(f"{name}: expected {arity[name]} arguments")
        if name == "patch":
            if not data:
                raise OpError("patch: empty data")
            ops.append(Patch(_need_path(args[0]), data))
        elif name == "put":
            path, mode, sha, size_arg = args
            if mode not in MODES or not _HEX64.fullmatch(sha) or not _UINT.fullmatch(size_arg):
                raise OpError(f"put {path!r}: bad arguments")
            size = int(size_arg)
            if len(data) == size:
                ops.append(Put(_need_path(path), mode, sha, size, data))  # type: ignore[arg-type]
            elif not data and size > 0:
                ops.append(Put(_need_path(path), mode, sha, size, None))  # type: ignore[arg-type]
            else:
                raise OpError(f"put {path!r}: length is neither the size nor a blob reference")
        elif data:
            raise OpError(f"{name}: data must be empty")
        elif name == "delete":
            ops.append(Delete(_need_path(args[0])))
        elif name == "rename":
            ops.append(Rename(_need_path(args[0]), _need_path(args[1])))
        else:
            if args[1] not in REGULAR_MODES:
                raise OpError(f"mode {args[0]!r}: bad mode {args[1]!r}")
            ops.append(ModeOp(_need_path(args[0]), args[1]))  # type: ignore[arg-type]
    return tuple(ops)


def touched_paths(ops: Sequence[ChangeOp]) -> tuple[str, ...]:
    paths: set[str] = set()
    for op in ops:
        if isinstance(op, Rename):
            paths.update((op.src, op.dst))
        else:
            paths.add(op.path)
    return tuple(sorted(paths, key=_key))


def blob_refs(ops: Sequence[ChangeOp]) -> tuple[Hex, ...]:
    return tuple(sorted({op.sha256 for op in ops if isinstance(op, Put) and op.data is None}))


# ---------------------------------------------------------------- lines and patches

def is_text(data: bytes) -> bool:
    """Valid UTF-8 and no NUL (§5.6)."""
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def split_lines(data: bytes) -> list[bytes]:
    """§5.4: split after each LF; each line keeps its LF; the last may lack one."""
    if not data:
        return []
    parts = data.split(b"\n")
    lines = [part + b"\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


_HUNK_RE = re.compile(rb"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@")


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[list[bytes]] = field(default_factory=list)   # [op, line bytes]


def _parse_hunks(data: bytes) -> list[_Hunk]:
    if not data or not data.endswith(b"\n"):
        raise OpError("patch data must be non-empty and end with LF")
    hunks: list[_Hunk] = []
    for raw in data[:-1].split(b"\n"):
        if raw.startswith(b"@@"):
            m = _HUNK_RE.match(raw)
            if not m:
                raise OpError(f"bad hunk header {raw[:80]!r}")
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            hunks.append(_Hunk(int(m.group(1)), old_count, int(m.group(3)), new_count))
            continue
        if not hunks:
            raise OpError("patch data must start with a hunk header")
        lead = raw[:1]
        if lead in (b" ", b"-", b"+"):
            hunks[-1].lines.append([lead, raw[1:] + b"\n"])
        elif lead == b"\\":
            lines = hunks[-1].lines
            if not lines or not lines[-1][1].endswith(b"\n"):
                raise OpError("no-newline marker without a line")
            lines[-1][1] = lines[-1][1][:-1]
        else:
            raise OpError(f"bad patch line {raw[:80]!r}")
    for hunk in hunks:
        old = sum(1 for op, _ in hunk.lines if op != b"+")
        new = sum(1 for op, _ in hunk.lines if op != b"-")
        if old != hunk.old_count or new != hunk.new_count:
            raise OpError(f"hunk @@ -{hunk.old_start} counts do not match its lines")
    return hunks


def _hunk_pos(hunk: _Hunk) -> int:
    if hunk.old_count == 0:
        return hunk.old_start
    if hunk.old_start == 0:
        raise OpError("hunk with old lines starts at line 0")
    return hunk.old_start - 1


def apply_patch(old: bytes, hunks: bytes) -> bytes:
    """§5.4 exact application, no fuzz. Raises OpError."""
    lines = split_lines(old)
    out: list[bytes] = []
    cursor = 0
    prev_start = -1
    for hunk in _parse_hunks(hunks):
        pos = _hunk_pos(hunk)
        if hunk.old_start <= prev_start:
            raise OpError("hunks are not in increasing order of old start")
        if pos < cursor:
            raise OpError("hunks overlap")
        if pos + hunk.old_count > len(lines):
            raise OpError(f"hunk @@ -{hunk.old_start} runs past the end of the file")
        out.extend(lines[cursor:pos])
        expected = len(out) + 1 if hunk.new_count else len(out)
        if hunk.new_start != expected:
            raise OpError(f"hunk @@ -{hunk.old_start}: new start {hunk.new_start}, expected {expected}")
        i = pos
        for op, line in hunk.lines:
            if op != b"+":
                if lines[i] != line:
                    raise OpError(f"line {i + 1} does not match the patch")
                i += 1
            if op != b"-":
                out.append(line)
        cursor = pos + hunk.old_count
        prev_start = hunk.old_start
    out.extend(lines[cursor:])
    for line in out[:-1]:
        if not line.endswith(b"\n"):
            raise OpError("patch leaves a line without LF before the end of the file")
    return b"".join(out)


def _emit(out: bytearray, prefix: bytes, line: bytes) -> None:
    out += prefix
    out += line
    if not line.endswith(b"\n"):
        out += b"\n\\ No newline at end of file\n"


def make_patch(old: bytes, new: bytes, context: int = 3) -> bytes:
    """§5.4 hunks (no file headers) turning `old` into `new`; b"" when equal."""
    a, b = split_lines(old), split_lines(new)
    out = bytearray()
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for group in matcher.get_grouped_opcodes(context):
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        old_start = i1 + 1 if i2 > i1 else i1
        new_start = j1 + 1 if j2 > j1 else j1
        out += f"@@ -{old_start},{i2 - i1} +{new_start},{j2 - j1} @@\n".encode("ascii")
        for tag, a1, a2, b1, b2 in group:
            if tag == "equal":
                for line in a[a1:a2]:
                    _emit(out, b" ", line)
                continue
            for line in a[a1:a2]:
                _emit(out, b"-", line)
            for line in b[b1:b2]:
                _emit(out, b"+", line)
    return bytes(out)


# ---------------------------------------------------------------- applying ops

def _check_symlink(mode: str, data: bytes) -> None:
    if mode == "120000" and (not data or b"\0" in data):
        raise OpError("symlink target must be non-empty and contain no NUL")


def _apply_one(tree: Tree, op: ChangeOp, blobs: BlobSource) -> Tree:
    if isinstance(op, Patch):
        entry = tree.get(_need_path(op.path))
        if entry is None:
            raise OpError("path does not exist")
        if entry.mode not in REGULAR_MODES:
            raise OpError("path is not a regular file")
        return tree.set(op.path, entry.mode, apply_patch(tree.read(op.path), op.hunks))
    if isinstance(op, Put):
        _need_path(op.path)
        if op.mode not in MODES:
            raise OpError(f"bad mode {op.mode!r}")
        content = op.data if op.data is not None else blobs.blob(op.sha256)
        if content is None:
            raise OpError(f"blob {op.sha256[:12]} is not available")
        if len(content) != op.size or _sha(content) != op.sha256:
            raise OpError("content does not match its sha256 and size")
        _check_symlink(op.mode, content)
        return tree.set(op.path, op.mode, content)
    if isinstance(op, Delete):
        if _need_path(op.path) not in tree:
            raise OpError("path does not exist")
        return tree.delete(op.path)
    if isinstance(op, Rename):
        entry = tree.get(_need_path(op.src))
        if entry is None:
            raise OpError("source does not exist")
        if _need_path(op.dst) in tree:
            raise OpError("destination exists")
        return tree.delete(op.src).set_entry(op.dst, entry)
    if isinstance(op, ModeOp):
        entry = tree.get(_need_path(op.path))
        if entry is None:
            raise OpError("path does not exist")
        if entry.mode not in REGULAR_MODES or op.mode not in REGULAR_MODES or entry.mode == op.mode:
            raise OpError(f"cannot change mode {entry.mode} to {op.mode}")
        return tree.set_entry(op.path, Entry(op.mode, entry.blob, entry.size))
    raise OpError(f"not a change operation: {op!r}")


def _describe(op: ChangeOp) -> str:
    if isinstance(op, Rename):
        return f"rename {op.src} -> {op.dst}"
    return f"{type(op).__name__.lower().replace('modeop', 'mode')} {op.path}"


def apply_ops(tree: Tree, ops: Sequence[ChangeOp], blobs: BlobSource) -> Tree:
    """Apply ops in order (§5.3). Raises OpError naming the first op that fails."""
    for n, op in enumerate(ops):
        try:
            tree = _apply_one(tree, op, blobs)
        except (OpError, ObjectError, KeyError) as exc:
            reason = exc.args[0] if exc.args else exc
            raise OpError(f"op {n} ({_describe(op)}): {reason}") from exc
    return tree


# ---------------------------------------------------------------- diff_trees

def diff_trees(old: Tree, new: Tree, *, inline_budget: int) -> tuple[ChangeOp, ...]:
    """§5.3 canonical ops (renames by src, then deletes, then put/patch/mode per path).

    Put contents (and patches) that do not fit the remaining inline budget become blob
    references; the caller sends those blobs from `new`."""
    changed = list(old.changed_paths(new))
    removed = [p for p in changed if p not in new]
    added = [p for p in changed if p not in old]
    by_content: dict[tuple[str, str], list[str]] = {}
    for path in added:
        entry = new.get(path)
        assert entry is not None
        by_content.setdefault((entry.mode, entry.blob), []).append(path)

    def rename_ok(dst: str) -> bool:
        segs = dst.split("/")
        if any("/".join(segs[:i]) in old for i in range(1, len(segs))):
            return False
        return not old._is_dir(dst)

    renames: list[Rename] = []
    moved: set[str] = set()
    for src in removed:
        entry = old.get(src)
        assert entry is not None
        for dst in by_content.get((entry.mode, entry.blob), ()):
            if dst not in moved and rename_ok(dst):
                renames.append(Rename(src, dst))
                moved.update((src, dst))
                break
    ops: list[ChangeOp] = list(renames)
    ops += [Delete(p) for p in removed if p not in moved]
    remaining = inline_budget
    for path in changed:
        if path in moved or path in removed:
            continue
        before, after = old.get(path), new.get(path)
        assert after is not None
        data = new.read(path)
        if before is not None and before.blob == after.blob and \
                before.mode in REGULAR_MODES and after.mode in REGULAR_MODES:
            ops.append(ModeOp(path, after.mode))
            continue
        if before is not None and before.blob != after.blob and \
                before.mode in REGULAR_MODES and after.mode in REGULAR_MODES:
            old_data = old.read(path)
            if is_text(old_data) and is_text(data):
                hunks = make_patch(old_data, data)
                if len(hunks) <= len(data) and len(hunks) <= remaining:
                    remaining -= len(hunks)
                    ops.append(Patch(path, hunks))
                    if before.mode != after.mode:
                        ops.append(ModeOp(path, after.mode))
                    continue
        inline = after.size == 0 or after.size <= remaining
        if inline:
            remaining -= after.size
        ops.append(Put(path, after.mode, after.blob, after.size, data if inline else None))
    return tuple(ops)


def render_unified(old: Tree, new: Tree, ops: Sequence[ChangeOp], max_lines: int | None = 200) -> str:
    """Git-style preview for the human part and `gud diff`. Never used by replay."""
    lines: list[str] = []
    shown: set[str] = set()
    for op in ops:
        if isinstance(op, Rename) and old.get(op.src) == new.get(op.dst):
            lines += [f"diff --git a/{op.src} b/{op.dst}", "similarity index 100%",
                      f"rename from {op.src}", f"rename to {op.dst}"]
            shown.update((op.src, op.dst))
    for path in old.changed_paths(new):
        if path in shown:
            continue
        a, b = old.get(path), new.get(path)
        lines.append(f"diff --git a/{path} b/{path}")
        if a is None:
            lines.append(f"new file mode {b.mode}")  # type: ignore[union-attr]
        elif b is None:
            lines.append(f"deleted file mode {a.mode}")
        elif a.mode != b.mode:
            lines += [f"old mode {a.mode}", f"new mode {b.mode}"]
        if a is not None and b is not None and a.blob == b.blob:
            continue
        old_data = old.read(path) if a is not None else b""
        new_data = new.read(path) if b is not None else b""
        if not (is_text(old_data) and is_text(new_data)):
            lines.append(f"Binary files {'a/' + path if a else '/dev/null'} and "
                         f"{'b/' + path if b else '/dev/null'} differ")
            continue
        lines.append(f"--- {'a/' + path if a else '/dev/null'}")
        lines.append(f"+++ {'b/' + path if b else '/dev/null'}")
        lines += make_patch(old_data, new_data).decode("utf-8", "replace").splitlines()
    if max_lines is not None and len(lines) > max_lines:
        extra = len(lines) - max_lines
        lines = lines[:max_lines] + [f"[... {extra} more lines]"]
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------- merge

def _line_ops(a: list[bytes], b: list[bytes]) -> list[tuple[int, int, tuple[bytes, ...]]]:
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [(i1, i2, tuple(b[j1:j2])) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


def _overlaps(x: tuple[int, int, object], y: tuple[int, int, object]) -> bool:
    a1, a2, _ = x
    b1, b2, _ = y
    if a1 == a2 and b1 == b2:
        return a1 == b1
    if a1 == a2:
        return b1 <= a1 < b2
    if b1 == b2:
        return a1 <= b1 < a2
    return a1 < b2 and b1 < a2


def _fmt_ops(ops: Sequence[tuple[int, int, object]]) -> str:
    bits = []
    for a1, a2, _ in ops:
        if a1 == a2:
            bits.append(f"insert at line {a1 + 1}")
        elif a2 == a1 + 1:
            bits.append(f"line {a1 + 1}")
        else:
            bits.append(f"lines {a1 + 1}-{a2}")
    return ", ".join(bits) or "nothing"


def merge_file(base: bytes | None, mine: bytes | None, theirs: bytes | None) -> bytes | None:
    """Three-way merge of one file's bytes; None = absent. Raises OpError on overlap.

    Byte-wise, with difflib opcodes (autojunk off); an edit made
    identically on both sides is applied once instead of conflicting."""
    if mine == base:
        return theirs
    if theirs == base or theirs == mine:
        return mine
    if base is None:
        raise OpError("both added this file with different contents")
    if mine is None:
        raise OpError("you deleted it but the tip changed it")
    if theirs is None:
        raise OpError("the tip deleted it but you changed it")
    if not (is_text(base) and is_text(mine) and is_text(theirs)):
        raise OpError("binary file changed on both sides")
    b_lines = split_lines(base)
    mine_ops, their_ops = _line_ops(b_lines, split_lines(mine)), _line_ops(b_lines, split_lines(theirs))
    shared = set(mine_ops) & set(their_ops)
    mine_only = [op for op in mine_ops if op not in shared]
    their_only = [op for op in their_ops if op not in shared]
    if any(_overlaps(x, y) for x in mine_only for y in their_only):
        raise OpError(f"tip changed {_fmt_ops(their_ops)}; your push changed {_fmt_ops(mine_ops)}")
    out: list[bytes] = []
    i = 0
    for a1, a2, repl in sorted(set(mine_ops) | set(their_ops), key=lambda op: (op[0], op[1])):
        out.extend(b_lines[i:a1])
        out.extend(repl)
        i = max(i, a2)
    out.extend(b_lines[i:])
    merged = b"".join(out)
    if any(not line.endswith(b"\n") for line in split_lines(merged)[:-1]):
        raise OpError("merge joins a line without LF")
    return merged


def _merge_entry(store, base: Entry | None, mine: Entry | None, theirs: Entry | None) -> Entry | None:
    """Merge one path's entries (contents read from `store`). Raises OpError on conflict."""
    if mine == base:
        return theirs
    if theirs == base or theirs == mine:
        return mine
    if base is None:
        raise OpError("both added this file with different contents")
    if mine is None:
        raise OpError("you deleted it but the tip changed it")
    if theirs is None:
        raise OpError("the tip deleted it but you changed it")
    if mine.mode == base.mode:
        mode = theirs.mode
    elif theirs.mode == base.mode or theirs.mode == mine.mode:
        mode = mine.mode
    else:
        raise OpError(f"mode changed on both sides ({mine.mode}, {theirs.mode})")
    if mine.blob == base.blob:
        return Entry(mode, theirs.blob, theirs.size)
    if theirs.blob == base.blob or theirs.blob == mine.blob:
        return Entry(mode, mine.blob, mine.size)
    if not (base.mode in REGULAR_MODES and mine.mode in REGULAR_MODES and theirs.mode in REGULAR_MODES):
        raise OpError("symlink changed on both sides")
    merged = merge_file(store.get(base.blob), store.get(mine.blob), store.get(theirs.blob))
    assert merged is not None
    return Entry(mode, store.put(merged), len(merged))


def _write_entries(tree: Tree, targets: Mapping[str, Entry | None], conflicts: list[str]) -> Tree:
    """Deletes first, then sets, so a file can become a directory and back. Shape errors become conflicts."""
    for path in sorted(targets, key=_key):
        if targets[path] is None and path in tree:
            tree = tree.delete(path)
    for path in sorted(targets, key=_key):
        entry = targets[path]
        if entry is not None:
            try:
                tree = tree.set_entry(path, entry)
            except ObjectError as exc:
                conflicts.append(f"{path}: {exc}")
    return tree


@dataclass(frozen=True)
class MergeResult:
    tree: Tree
    conflicts: tuple[str, ...]


def three_way(base: Tree, mine: Tree, theirs: Tree, *, paths: Iterable[str] | None = None) -> MergeResult:
    """Replay `mine`'s edits (relative to `base`) onto `theirs`; `paths` limits which paths merge.

    Conflicting paths keep `theirs`' entry and get one line each in `conflicts`."""
    candidates = list(base.changed_paths(mine))
    if paths is not None:
        allowed = set(paths)
        candidates = [p for p in candidates if p in allowed]
    targets: dict[str, Entry | None] = {}
    conflicts: list[str] = []
    for path in candidates:
        b, m, t = base.get(path), mine.get(path), theirs.get(path)
        try:
            merged = _merge_entry(theirs.store, b, m, t)
        except OpError as exc:
            conflicts.append(f"{path}: {exc}")
            continue
        if merged != t:
            targets[path] = merged
    tree = _write_entries(theirs, targets, conflicts)
    return MergeResult(tree, tuple(conflicts))


def invert_ops(base_before: Tree, base_after: Tree, onto: Tree) -> tuple[Tree, tuple[str, ...]]:
    """§15.6 revert of the change base_before -> base_after, applied to `onto`.

    Returns (result tree, conflicting paths)."""
    targets: dict[str, Entry | None] = {}
    conflicts: list[str] = []
    for path in base_before.changed_paths(base_after):
        prev, cur, now = base_before.get(path), base_after.get(path), onto.get(path)
        if now == cur:
            targets[path] = prev
            continue
        try:
            targets[path] = _merge_entry(onto.store, cur, prev, now)
        except OpError:
            conflicts.append(path)
    shape: list[str] = []
    tree = _write_entries(onto, targets, shape)
    conflicts += [line.split(":", 1)[0] for line in shape]
    return tree, tuple(sorted(set(conflicts), key=_key))


# ---------------------------------------------------------------- touched lines (§13.1)

@dataclass(frozen=True)
class TouchedLines:
    removed: Mapping[str, tuple[int, ...]]
    whole_file: tuple[str, ...]
    inserted_at: Mapping[str, tuple[int, ...]]


def _patch_touched(hunks: bytes) -> tuple[set[int], set[int]]:
    """(old line indexes removed, old positions of pure insertions) for one patch."""
    removed: set[int] = set()
    inserted: set[int] = set()
    for hunk in _parse_hunks(hunks):
        i = _hunk_pos(hunk)
        start: int | None = None
        minus = plus = False
        for op, _ in hunk.lines + [[b" ", b""]]:
            if op == b" ":
                if start is not None and plus and not minus:
                    inserted.add(start)
                start, minus, plus = None, False, False
                i += 1
                continue
            if start is None:
                start = i
            if op == b"-":
                removed.add(i)
                minus = True
                i += 1
            else:
                plus = True
    return removed, inserted


def touched_lines(before: Tree, after: Tree, ops: Sequence[ChangeOp]) -> TouchedLines:
    """Lines and files of `before` that `ops` remove, rewrite or touch as a whole (§13.1, §13.8)."""
    whole: set[str] = set()
    edits: dict[str, list[ChangeOp]] = {}
    for op in ops:
        if isinstance(op, Rename):
            whole.update((op.src, op.dst))
        elif isinstance(op, (Delete, ModeOp)):
            whole.add(op.path)
        else:
            edits.setdefault(op.path, []).append(op)
    removed: dict[str, tuple[int, ...]] = {}
    inserted: dict[str, tuple[int, ...]] = {}
    for path in sorted(edits, key=_key):
        if path in whole:
            continue
        entry = before.get(path)
        if entry is None:
            continue
        old_data = before.read(path)
        if entry.mode not in REGULAR_MODES or not is_text(old_data):
            whole.add(path)
            continue
        path_ops = edits[path]
        if len(path_ops) == 1 and isinstance(path_ops[0], Patch):
            gone, ins = _patch_touched(path_ops[0].hunks)
        else:
            new_entry = after.get(path)
            new_data = after.read(path) if new_entry is not None else b""
            if new_entry is None or new_entry.mode not in REGULAR_MODES or not is_text(new_data):
                whole.add(path)
                continue
            gone, ins = set(), set()
            matcher = difflib.SequenceMatcher(None, split_lines(old_data), split_lines(new_data), autojunk=False)
            for tag, i1, i2, _, _ in matcher.get_opcodes():
                if tag in ("replace", "delete"):
                    gone.update(range(i1, i2))
                elif tag == "insert":
                    ins.add(i1)
        if gone:
            removed[path] = tuple(sorted(gone))
        if ins:
            inserted[path] = tuple(sorted(ins))
    return TouchedLines(removed, tuple(sorted(whole, key=_key)), inserted)


# ---------------------------------------------------------------- blame (§5.6)

def _blame_text(entry: Entry, data: bytes) -> bool:
    return entry.mode != "120000" and is_text(data)


def _fresh_runs(entry: Entry, data: bytes, owner: Handle, seq: int) -> list[BlameRun]:
    if _blame_text(entry, data):
        count = len(split_lines(data))
        return [BlameRun(owner, seq, count)] if count else []
    return [BlameRun(owner, seq, 1)]


def _aligned_runs(old: bytes, new: bytes, old_runs: Sequence[BlameRun], owner: Handle, seq: int) -> list[BlameRun]:
    a, b = split_lines(old), split_lines(new)
    per_line = [(run.owner, run.seq) for run in old_runs for _ in range(run.length)]
    per_line = (per_line + [(owner, seq)] * len(a))[:len(a)]
    out: list[BlameRun] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out += [BlameRun(o, s, 1) for o, s in per_line[i1:i2]]
        elif j2 > j1:
            out.append(BlameRun(owner, seq, j2 - j1))
    return out


_GONE = object()


def update_blame(before: Tree, before_blame: BlameMap, after: Tree, ops: Sequence[ChangeOp],
                 owner: Handle, seq: int) -> BlameMap:
    """§5.6: equal lines keep blame (difflib.SequenceMatcher(autojunk=False)); others go to `owner`.

    Renamed files keep their blame; whole-file (non-text) changes and new files go to `owner`;
    a mode change alone keeps blame."""
    origin: dict[str, object] = {}

    def current(path: str) -> object:
        if path in origin:
            return origin[path]
        return path if path in before else None

    for op in ops:
        if isinstance(op, Rename):
            origin[op.dst] = current(op.src)
            origin[op.src] = _GONE
        elif isinstance(op, Delete):
            origin[op.path] = _GONE
        else:
            src = current(op.path)
            origin[op.path] = None if src is _GONE else src
    blame = before_blame
    order = sorted(origin, key=_key)
    for path in order:
        if path not in after and path in blame:
            blame = blame.delete(path)
    for path in order:
        new_entry = after.get(path)
        if new_entry is None:
            continue
        new_data = after.read(path)
        src = origin[path]
        if not isinstance(src, str):
            runs = _fresh_runs(new_entry, new_data, owner, seq)
        else:
            old_entry = before.get(src)
            assert old_entry is not None
            old_data = before.read(src)
            old_runs = before_blame.get(src)
            old_text, new_text = _blame_text(old_entry, old_data), _blame_text(new_entry, new_data)
            if old_entry.blob == new_entry.blob and old_text == new_text:
                runs = list(old_runs)
            elif old_text and new_text:
                runs = _aligned_runs(old_data, new_data, old_runs, owner, seq)
            else:
                runs = _fresh_runs(new_entry, new_data, owner, seq)
        blame = blame.set(path, runs)
    return blame


def genesis_blame(tree: Tree, owner: Handle) -> BlameMap:
    """Every line and every file belongs to the genesis author (link seq 0)."""
    files = {}
    for path, entry in tree.items():
        files[path] = _fresh_runs(entry, tree.store.get(entry.blob), owner, 0)
    return BlameMap._build(tree.store, files)
