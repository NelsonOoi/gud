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
    show = sub.add_parser("show", help="print a note")
    show.add_argument("slug")
    sub.add_parser("list", help="list notes: slug and title")
    delete = sub.add_parser("delete", help="delete a note")
    delete.add_argument("slug")
    sub.add_parser("gc", help="delete editor debris from the notes directory")
    return ap


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    store = NoteStore(args.dir or os.environ.get("NOTES_DIR") or "notes-data")
    try:
        if args.command == "new":
            print(store.create(" ".join(args.title), args.body).slug)
        elif args.command == "show":
            note = store.read(args.slug)
            print(f"# {note.title}")
            if note.body:
                print()
                print(note.body)
        elif args.command == "list":
            notes = store.list()
            print("\n".join(f"{n.slug}  {n.title}" for n in notes) if notes else "No notes.")
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
