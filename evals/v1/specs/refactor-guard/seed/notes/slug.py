"""Note slugs: the file name of a note, derived from its title."""

from __future__ import annotations

import re


def slugify(title: str) -> str:
    """`Hello, World!` -> `hello-world`: lowercase letters and digits; every run of anything else becomes one
    hyphen; no leading or trailing hyphen. A title with nothing left is `untitled`."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "untitled"
