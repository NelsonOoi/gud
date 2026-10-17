"""Unified diffs between trees. `diff_trees` is part of the Phase 0 contract; `apply_diff` belongs to core."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .types import Tree
from .wire import normalize

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SKIP_PREFIXES = (
    "index ", "new file", "deleted file", "old mode", "new mode",
    "similarity ", "rename ", "copy ",
)


def diff_trees(old: Tree, new: Tree) -> str:
    """Git-style unified diff (a/ b/ prefixes, /dev/null for adds and deletes), files in sorted order."""
    out: list[str] = []
    for path in sorted(set(old) | set(new)):
        a = normalize(old[path]) if path in old else None
        b = normalize(new[path]) if path in new else None
        if a == b:
            continue
        a_lines = a.splitlines(keepends=True) if a is not None else []
        b_lines = b.splitlines(keepends=True) if b is not None else []
        out.append(f"diff --git a/{path} b/{path}\n")
        out.extend(difflib.unified_diff(
            a_lines, b_lines,
            fromfile=f"a/{path}" if a is not None else "/dev/null",
            tofile=f"b/{path}" if b is not None else "/dev/null",
            n=3,
        ))
    return "".join(out)


class PatchError(Exception):
    pass


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _FileDiff:
    path: str
    old_exists: bool
    new_exists: bool
    hunks: tuple[_Hunk, ...]


def _header_path(line: str, prefix: str) -> str | None:
    payload = line.split("\t", 1)[0].strip()[4:]
    if payload == "/dev/null":
        return None
    if not payload.startswith(prefix):
        raise PatchError(f"expected {prefix} path in {line!r}")
    return payload[len(prefix):]


def _parse_hunk(raw: list[str], i: int) -> tuple[_Hunk, int]:
    match = _HUNK_RE.match(raw[i])
    if not match:
        raise PatchError(f"bad hunk header: {raw[i]!r}")
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    i += 1
    lines: list[tuple[str, str]] = []
    old_seen = new_seen = 0
    while i < len(raw) and (old_seen < old_count or new_seen < new_count):
        line = raw[i]
        if line.startswith("\\"):
            if not lines or not lines[-1][1].endswith("\n"):
                raise PatchError("newline marker without a line")
            op, content = lines[-1]
            lines[-1] = (op, content[:-1])
            i += 1
            continue
        if not line or line[0] not in " +-":
            raise PatchError(f"bad hunk line: {line!r}")
        op = line[0]
        content = line[1:] + "\n"
        if op == " ":
            old_seen += 1
            new_seen += 1
        elif op == "-":
            old_seen += 1
        else:
            new_seen += 1
        lines.append((op, content))
        i += 1
    if old_seen != old_count or new_seen != new_count:
        raise PatchError(f"truncated hunk at line {old_start}")
    return _Hunk(old_start, old_count, new_start, new_count, tuple(lines)), i


def _parse_file(raw: list[str], i: int) -> tuple[_FileDiff, int]:
    if i + 1 >= len(raw) or not raw[i + 1].startswith("+++ "):
        raise PatchError("missing +++ header")
    old_path = _header_path(raw[i], "a/")
    new_path = _header_path(raw[i + 1], "b/")
    i += 2
    if old_path is None and new_path is None:
        raise PatchError("diff touches no file")
    if old_path is not None and new_path is not None and old_path != new_path:
        raise PatchError(f"rename not supported: {old_path} -> {new_path}")
    path = new_path if new_path is not None else old_path
    assert path is not None
    hunks: list[_Hunk] = []
    while i < len(raw) and raw[i].startswith("@@"):
        hunk, i = _parse_hunk(raw, i)
        hunks.append(hunk)
    if not hunks:
        raise PatchError(f"no hunks for {path}")
    return _FileDiff(path, old_path is not None, new_path is not None, tuple(hunks)), i


def _parse_diff(diff: str) -> list[_FileDiff]:
    if not diff or not diff.strip():
        return []
    raw = diff.splitlines()
    files: list[_FileDiff] = []
    i = 0
    while i < len(raw):
        line = raw[i]
        if line.startswith("diff --git ") or line.startswith(_SKIP_PREFIXES) or line == "":
            i += 1
            continue
        if line.startswith("--- "):
            file_diff, i = _parse_file(raw, i)
            files.append(file_diff)
            continue
        raise PatchError(f"unexpected diff line: {line!r}")
    return files


def _apply_hunks(old: str, hunks: tuple[_Hunk, ...], path: str) -> str:
    old_lines = old.splitlines(keepends=True) if old else []
    new_lines: list[str] = []
    cursor = 0
    for hunk in hunks:
        pos = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if pos < cursor:
            raise PatchError(f"{path}: overlapping hunk")
        if pos > len(old_lines):
            raise PatchError(f"{path}: hunk starts past end of file")
        new_lines.extend(old_lines[cursor:pos])
        cursor = pos
        for op, content in hunk.lines:
            if op == " ":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise PatchError(f"{path}: context mismatch at line {cursor + 1}")
                new_lines.append(content)
                cursor += 1
            elif op == "-":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise PatchError(f"{path}: removal mismatch at line {cursor + 1}")
                cursor += 1
            elif op == "+":
                new_lines.append(content)
    new_lines.extend(old_lines[cursor:])
    return "".join(new_lines)


def _apply_file(tree: dict[str, str], file_diff: _FileDiff) -> None:
    path = file_diff.path
    if not file_diff.old_exists:
        if path in tree:
            raise PatchError(f"adding existing file {path}")
        old = ""
    else:
        if path not in tree:
            raise PatchError(f"missing file {path}")
        old = tree[path]
    raw_new = _apply_hunks(old, file_diff.hunks, path)
    if not file_diff.new_exists:
        if raw_new != "":
            raise PatchError(f"incomplete delete of {path}")
        del tree[path]
        return
    tree[path] = normalize(raw_new)


def apply_diff(tree: Tree, diff: str) -> Tree:
    """Apply a diff produced by `diff_trees` (or git) to `tree`; raise PatchError if it does not apply cleanly."""
    result = {path: normalize(text) for path, text in tree.items()}
    for file_diff in _parse_diff(diff):
        _apply_file(result, file_diff)
    return result


def diffstat(diff: str) -> dict:
    """`{"files": [paths], "added": int, "removed": int}` for a unified diff."""
    files: list[str] = []
    added = removed = 0
    for file_diff in _parse_diff(diff):
        files.append(file_diff.path)
        for hunk in file_diff.hunks:
            for op, _content in hunk.lines:
                if op == "+":
                    added += 1
                elif op == "-":
                    removed += 1
    return {"files": files, "added": added, "removed": removed}
