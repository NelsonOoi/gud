"""Notes on disk. A note is `<slug>.md` in the notes directory:

    # Title
    tags: home, errand        (only when the note has tags)
    <blank line>
    body text...
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    tags: list[str] = field(default_factory=list)


def clean_tags(tags) -> list[str]:
    out: list[str] = []
    for tag in tags or ():
        tag = tag.strip().lower()
        if tag and tag not in out:
            out.append(tag)
    return out


def parse(slug: str, text: str) -> Note:
    lines = text.split("\n")
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else slug
    rest = lines[1:]
    tags: list[str] = []
    if rest and rest[0].startswith("tags: "):
        tags = clean_tags(rest[0][len("tags: "):].split(","))
        rest = rest[1:]
    body = "\n".join(rest[1:]).strip("\n") if len(rest) > 1 else ""
    return Note(slug, title, body, tags)


def render(note: Note) -> str:
    head = f"# {note.title}\n" + (f"tags: {', '.join(note.tags)}\n" if note.tags else "")
    return head + (f"\n{note.body}\n" if note.body else "")


class NoteStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, slug: str) -> Path:
        return self.root / f"{slug}.md"

    def unique_slug(self, title: str, keep: str | None = None) -> str:
        base = slugify(title)
        slug, n = base, 2
        while self.path(slug).exists() and slug != keep:
            slug, n = f"{base}-{n}", n + 1
        return slug

    def create(self, title: str, body: str = "", tags=None) -> Note:
        note = Note(self.unique_slug(title), title.strip(), body, clean_tags(tags))
        self.save(note)
        return note

    def save(self, note: Note) -> None:
        self.path(note.slug).write_text(render(note), encoding="utf-8")

    def read(self, slug: str) -> Note:
        path = self.path(slug)
        if not path.is_file():
            raise NoteNotFound(slug)
        return parse(slug, path.read_text(encoding="utf-8"))

    def raw(self, slug: str) -> str:
        path = self.path(slug)
        if not path.is_file():
            raise NoteNotFound(slug)
        return path.read_text(encoding="utf-8")

    def list(self, tag: str | None = None) -> list[Note]:
        notes = [self.read(p.stem) for p in sorted(self.root.glob("*.md"), key=lambda p: p.stem)]
        if tag is not None:
            notes = [n for n in notes if tag.lower() in n.tags]
        return notes

    def tag(self, slug: str, tags) -> Note:
        note = self.read(slug)
        note.tags = clean_tags(list(note.tags) + list(tags))
        self.save(note)
        return note

    def rename(self, slug: str, title: str) -> Note:
        note = self.read(slug)
        new_slug = self.unique_slug(title, keep=slug)
        old = self.path(slug)
        note.title, note.slug = title.strip(), new_slug
        self.save(note)
        if new_slug != slug:
            old.unlink()
        return note

    def search(self, text: str) -> list[Note]:
        needle = text.lower()
        return [n for n in self.list() if needle in n.title.lower() or needle in n.body.lower()]

    def delete(self, slug: str) -> None:
        path = self.path(slug)
        if not path.is_file():
            raise NoteNotFound(slug)
        path.unlink()
