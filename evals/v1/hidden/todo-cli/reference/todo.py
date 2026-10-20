"""Reference todo CLI (hidden from agents; used to validate the hidden tests)."""

import argparse
import json
import os
import sys
from pathlib import Path


def load(path):
    if not path.exists():
        return []
    return json.loads(path.read_text() or "[]")


def save(path, items):
    path.write_text(json.dumps(items, indent=2) + "\n")


def find(items, todo_id):
    for item in items:
        if item["id"] == todo_id:
            return item
    print(f"Error: no todo with id {todo_id}", file=sys.stderr)
    raise SystemExit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="todo")
    parser.add_argument("--file")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("text", nargs="+")
    sub.add_parser("list")
    for name in ("done", "remove"):
        sub.add_parser(name).add_argument("id", type=int)
    args = parser.parse_args(argv)
    path = Path(args.file or os.environ.get("TODO_FILE") or "todos.json")
    items = load(path)
    if args.command == "add":
        item = {"id": max((i["id"] for i in items), default=0) + 1, "text": " ".join(args.text), "done": False}
        items.append(item)
        save(path, items)
        print(f"Added {item['id']}: {item['text']}")
    elif args.command == "list":
        if not items:
            print("No todos.")
        for item in sorted(items, key=lambda i: i["id"]):
            print(f"[{'x' if item['done'] else ' '}] {item['id']} {item['text']}")
    elif args.command == "done":
        find(items, args.id)["done"] = True
        save(path, items)
        print(f"Done {args.id}")
    else:
        items.remove(find(items, args.id))
        save(path, items)
        print(f"Removed {args.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
