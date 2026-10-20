"""Note slugs: the file name of a note, derived from its title."""

from __future__ import annotations

import unicodedata


def slugify(title: str) -> str:
    """`Café au lait` -> `café-au-lait`: NFC, lowercase, letters and digits of any script; every run of
    anything else becomes one hyphen; no leading or trailing hyphen. Nothing left is `untitled`."""
    text = unicodedata.normalize("NFC", title).lower()
    out: list[str] = []
    for ch in text:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "untitled"
