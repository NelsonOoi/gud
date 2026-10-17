"""Unified diffs between trees. `diff_trees` is part of the Phase 0 contract; `apply_diff` belongs to core."""

from __future__ import annotations

import difflib

from .types import Tree
from .wire import normalize


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


def apply_diff(tree: Tree, diff: str) -> Tree:
    """Apply a diff produced by `diff_trees` (or git) to `tree`; raise PatchError if it does not apply cleanly."""
    raise NotImplementedError("core subagent implements this")
