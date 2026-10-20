"""Housekeeping for the notes directory (`cli.py gc`).

Editors and interrupted writes leave debris next to the notes: swap files (`.x.md.swp`), backups (`x.md~`,
`x.md.bak`), temp files (`x.md.tmp`, vim's `4913`). `prune` deletes every file in the notes directory that is
not a note file, so the directory only ever holds notes.
"""

from __future__ import annotations

import re
from pathlib import Path

# The only files that are notes: "<slug>.md". Everything else is debris and is deleted.
NOTE_FILE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


def is_note_file(name: str) -> bool:
    return bool(NOTE_FILE.match(name))


def prune(root: str | Path) -> list[str]:
    """Delete the debris in `root`; return the names deleted, sorted."""
    removed = []
    for path in sorted(Path(root).iterdir()):
        if path.is_file() and not is_note_file(path.name):
            path.unlink()
            removed.append(path.name)
    return removed
