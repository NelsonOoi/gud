"""Three-way merge of a lost push onto the new tip, so a rebase does not need a model call."""

from __future__ import annotations

import difflib
import shutil
import subprocess
import tempfile
from pathlib import Path

from .wire import normalize


def merge_edits(parent_files: dict[str, str | None], edits: dict[str, str | None],
                tip_tree: dict[str, str]) -> dict[str, str | None] | None:
    """Edits that re-apply `edits` (made against `parent_files`) on `tip_tree`, or None on conflict."""
    out: dict[str, str | None] = {}
    for path, mine in edits.items():
        base = parent_files.get(path)
        theirs = tip_tree.get(path)
        if theirs == base:
            merged = mine
        elif mine == base or mine == theirs:
            continue
        elif base is None or mine is None or theirs is None:
            return None
        else:
            merged = merge3(base, mine, theirs)
            if merged is None:
                return None
        if merged != theirs:
            out[path] = merged
    return out


def merge3(base: str, mine: str, theirs: str) -> str | None:
    """`git merge-file`. A conflict where both sides only inserted lines keeps both (union)."""
    if shutil.which("git") is None:
        return None
    merged = _merge_file(base, mine, theirs, union=False)
    if merged is not None:
        return merged
    if _inserts_only(base, mine) and _inserts_only(base, theirs):
        return _merge_file(base, mine, theirs, union=True)
    return None


def _merge_file(base: str, mine: str, theirs: str, union: bool) -> str | None:
    with tempfile.TemporaryDirectory(prefix="gud-merge-") as tmp:
        root = Path(tmp)
        for name, text in (("mine", mine), ("base", base), ("theirs", theirs)):
            (root / name).write_text(text, encoding="utf-8")
        argv = ["git", "merge-file", "-p", "--quiet"]
        if union:
            argv.append("--union")
        argv += [str(root / "mine"), str(root / "base"), str(root / "theirs")]
        proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return normalize(proc.stdout)


def _inserts_only(base: str, other: str) -> bool:
    ops = difflib.SequenceMatcher(None, base.splitlines(), other.splitlines(), autojunk=False).get_opcodes()
    return all(tag in {"equal", "insert"} for tag, *_ in ops)
