"""Command-line app for the todo library."""

from __future__ import annotations

import argparse
import sys

import todolib


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cli.py", description="A todo list.")
    ap.add_argument("--file", default=None, help="the JSON store (default $TODO_FILE or ./todos.json)")
    sub = ap.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a todo")
    add.add_argument("text", nargs="+")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--due", default=None)
    ls = sub.add_parser("list", help="list todos")
    ls.add_argument("--tag", default=None)
    state = ls.add_mutually_exclusive_group()
    state.add_argument("--open", action="store_true")
    state.add_argument("--done", action="store_true")
    done = sub.add_parser("done", help="mark a todo done")
    done.add_argument("id", type=int)
    tag = sub.add_parser("tag", help="add tags to a todo")
    tag.add_argument("id", type=int)
    tag.add_argument("tags", nargs="+")
    rm = sub.add_parser("remove", help="remove a todo")
    rm.add_argument("id", type=int)
    return ap


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    store = todolib.TodoStore(args.file)
    try:
        if args.command == "add":
            todo = store.add(" ".join(args.text), tags=args.tag, due=args.due)
            print(f"Added {todo['id']}: {todo['text']}")
        elif args.command == "list":
            status = "open" if args.open else "done" if args.done else None
            todos = store.list(tag=args.tag, status=status)
            print("\n".join(todolib.format_todo(t) for t in todos) if todos else "No todos.")
        elif args.command == "done":
            print(f"Done {store.done(args.id)['id']}")
        elif args.command == "tag":
            todo = store.tag(args.id, args.tags)
            print(f"Tagged {todo['id']}: " + " ".join(f"#{t}" for t in todo["tags"]))
        elif args.command == "remove":
            print(f"Removed {store.remove(args.id)['id']}")
    except todolib.TodoNotFound as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except todolib.InvalidTodo as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
