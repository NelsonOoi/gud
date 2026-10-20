"""The notes command line.

    python cli.py [--dir DIR] COMMAND ...

DIR defaults to $NOTES_DIR, then ./notes-data.
"""

from __future__ import annotations

import argparse
import os
import sys

from notes.cleanup import prune
from notes.store import NoteNotFound, NoteStore


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cli.py")
    ap.add_argument("--dir", default=None, help="notes directory (default $NOTES_DIR or ./notes-data)")
    sub = ap.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a note; prints its slug")
    new.add_argument("title", nargs="+")
    new.add_argument("--body", default="")
    new.add_argument("--tag", action="append", default=[])
    show = sub.add_parser("show", help="print a note")
    show.add_argument("slug")
    ls = sub.add_parser("list", help="list notes: slug and title")
    ls.add_argument("--tag", default=None)
    tag = sub.add_parser("tag", help="add tags to a note")
    tag.add_argument("slug")
    tag.add_argument("tags", nargs="+")
    rename = sub.add_parser("rename", help="give a note a new title (and slug)")
    rename.add_argument("slug")
    rename.add_argument("title", nargs="+")
    search = sub.add_parser("search", help="notes whose title or body contains the text")
    search.add_argument("words", nargs="+")
    delete = sub.add_parser("delete", help="delete a note")
    delete.add_argument("slug")
    sub.add_parser("gc", help="delete editor debris from the notes directory")
    return ap


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    store = NoteStore(args.dir or os.environ.get("NOTES_DIR") or "notes-data")
    try:
        if args.command == "new":
            print(store.create(" ".join(args.title), args.body, args.tag).slug)
        elif args.command == "show":
            print(store.raw(args.slug), end="")
        elif args.command == "list":
            notes = store.list(tag=args.tag)
            print("\n".join(f"{n.slug}  {n.title}" for n in notes) if notes else "No notes.")
        elif args.command == "tag":
            note = store.tag(args.slug, args.tags)
            print(f"Tagged {note.slug}: {', '.join(note.tags)}")
        elif args.command == "rename":
            print(store.rename(args.slug, " ".join(args.title)).slug)
        elif args.command == "search":
            notes = store.search(" ".join(args.words))
            print("\n".join(f"{n.slug}  {n.title}" for n in notes) if notes else "No matches.")
        elif args.command == "delete":
            store.delete(args.slug)
            print(f"Deleted {args.slug}")
        elif args.command == "gc":
            print(f"Removed {len(prune(store.root))} file(s)")
    except NoteNotFound as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
