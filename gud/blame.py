"""Line-level ownership. Blame maps each path to one (owner, link id) per line."""

from __future__ import annotations

import ast
import difflib

from .patch import PatchError, _parse_diff
from .types import Blame, Tree
from .wire import normalize

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _lines(text: str) -> list[str]:
    return normalize(text).splitlines()


def genesis_blame(tree: Tree, owner: str, link_id: str) -> Blame:
    """Every line of `tree` was written by `owner` in `link_id`."""
    return {path: [(owner, link_id)] * len(_lines(text)) for path, text in tree.items()}


def update_blame(old_tree: Tree, old_blame: Blame, new_tree: Tree, owner: str, link_id: str) -> Blame:
    """Carry blame across equal lines; inserted and replaced lines belong to `owner`."""
    out: Blame = {}
    for path, text in new_tree.items():
        new_lines = _lines(text)
        if path not in old_tree:
            out[path] = [(owner, link_id)] * len(new_lines)
            continue
        old_lines = _lines(old_tree[path])
        previous = old_blame.get(path, [])
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        blamed: list[tuple[str, str]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                blamed.extend(previous[i1:i2])
            elif tag in {"insert", "replace"}:
                blamed.extend([(owner, link_id)] * (j2 - j1))
        out[path] = blamed
    return out


def _scan(diff: str) -> dict[str, tuple[set[int], set[int], list[tuple[int, int]]]]:
    """Per path: removed 1-based lines, pure-insertion k's, rewrite spans (lo, hi) inclusive."""
    found: dict[str, tuple[set[int], set[int], list[tuple[int, int]]]] = {}
    for file_diff in _parse_diff(diff):
        removed: set[int] = set()
        insertions: set[int] = set()
        rewrites: list[tuple[int, int]] = []
        for hunk in file_diff.hunks:
            next_old = hunk.old_start if hunk.old_count else hunk.old_start + 1
            minus: list[int] = []
            plus = False
            anchor: int | None = None

            def flush() -> None:
                nonlocal minus, plus, anchor
                if plus and not minus and anchor is not None:
                    insertions.add(anchor)
                elif plus and minus:
                    rewrites.append((minus[0], minus[-1]))
                minus = []
                plus = False
                anchor = None

            for op, _content in hunk.lines:
                if op == " ":
                    flush()
                    next_old += 1
                elif op == "-":
                    if anchor is None:
                        anchor = next_old - 1
                    minus.append(next_old)
                    removed.add(next_old)
                    next_old += 1
                elif op == "+":
                    if anchor is None:
                        anchor = next_old - 1
                    plus = True
            flush()
        found[file_diff.path] = (removed, insertions, rewrites)
    return found


def _owner_at(blame: Blame, path: str, lineno: int) -> str | None:
    lines = blame.get(path) or []
    idx = lineno - 1
    if idx < 0 or idx >= len(lines):
        return None
    return lines[idx][0]


def removed_owners(old_tree: Tree, old_blame: Blame, diff: str,
                   skip: frozenset[str] | None = None) -> set[str]:
    """Owners of old lines the diff deletes or rewrites (the '-' lines)."""
    owners: set[str] = set()
    skipped = skip or frozenset()
    try:
        scanned = _scan(diff)
    except PatchError:
        return owners
    for path, (removed, _insertions, _rewrites) in scanned.items():
        if path in skipped or path not in old_tree:
            continue
        for lineno in removed:
            owner = _owner_at(old_blame, path, lineno)
            if owner:
                owners.add(owner)
    return owners


def _encloses_insertion(node: ast.AST, k: int) -> bool:
    end = node.end_lineno or node.lineno
    return node.lineno <= k < end


def _encloses_rewrite(node: ast.AST, lo: int, hi: int) -> bool:
    """True when some gap in the rewritten region sits strictly inside the node."""
    end = node.end_lineno or node.lineno
    return lo <= end and hi > node.lineno


def enclosing_owners(old_tree: Tree, old_blame: Blame, diff: str) -> set[str]:
    """Owners of `def`/`class` lines around insertions and rewrites in `.py` files.

    A pure insertion between old line k and k+1 is inside a node when
    `node.lineno <= k < node.end_lineno`. Rewritten regions use the same test
    on the gaps the region covers. Unparseable files contribute nothing.
    """
    owners: set[str] = set()
    try:
        scanned = _scan(diff)
    except PatchError:
        return owners
    for path, (_removed, insertions, rewrites) in scanned.items():
        if not path.endswith(".py") or path not in old_tree:
            continue
        if not insertions and not rewrites:
            continue
        try:
            parsed = ast.parse(normalize(old_tree[path]))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(parsed):
            if not isinstance(node, _DEF_NODES):
                continue
            hit = any(_encloses_insertion(node, k) for k in insertions)
            hit = hit or any(_encloses_rewrite(node, lo, hi) for lo, hi in rewrites)
            if not hit:
                continue
            owner = _owner_at(old_blame, path, node.lineno)
            if owner:
                owners.add(owner)
    return owners
