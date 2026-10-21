"""Notes on disk. A note is `<slug>.md` in the notes directory:

    # Title
    <blank line>
    body text...
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .slug import slugify


class NoteNotFound(LookupError):
    def __init__(self, slug: str):
        super().__init__(f"no note {slug!r}")
        self.slug = slug


@dataclass
class Note:
    slug: str
    title: str
    body: str


def parse(slug: str, text: str) -> Note:
    lines = text.split("\n")
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else slug
    body = "\n".join(lines[2:]).strip("\n") if len(lines) > 2 else ""
    return Note(slug, title, body)


def render(note: Note) -> str:
    return f"# {note.title}\n\n{note.body}\n" if note.body else f"# {note.title}\n"


class NoteStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, slug: str) -> Path:
        return self.root / f"{slug}.md"

    def unique_slug(self, title: str) -> str:
        base = slugify(title)
        slug, n = base, 2
        while self.path(slug).exists():
            slug, n = f"{base}-{n}", n + 1
        return slug

    def create(self, title: str, body: str = "") -> Note:
        note = Note(self.unique_slug(title), title.strip(), body)
        self.save(note)
        return note

    def save(self, note: Note) -> None:
        self.path(note.slug).write_text(render(note), encoding="utf-8")

    def read(self, slug: str) -> Note:
        path = self.path(slug)
        if not path.is_file():
            raise NoteNotFound(slug)
        return parse(slug, path.read_text(encoding="utf-8"))

    def list(self) -> list[Note]:
        return [self.read(p.stem) for p in sorted(self.root.glob("*.md"), key=lambda p: p.stem)]

    def delete(self, slug: str) -> None:
        path = self.path(slug)
        if not path.is_file():
            raise NoteNotFound(slug)
        path.unlink()
