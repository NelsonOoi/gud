import argparse
import json
import os
import sys


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save(path, todos):
    with open(path, "w") as f:
        json.dump(todos, f)


def find(todos, todo_id):
    for t in todos:
        if t["id"] == todo_id:
            return t
    print(f"Error: no todo with id {todo_id}", file=sys.stderr)
    sys.exit(1)


def build_parser():
    p = argparse.ArgumentParser(prog="todo.py")
    p.add_argument("--file", help="JSON file storing the todos")
    sub = p.add_subparsers(dest="command")
    a = sub.add_parser("add", help="add a todo")
    a.add_argument("text", nargs="+")
    sub.add_parser("list", help="list todos")
    for name in ("done", "remove"):
        s = sub.add_parser(name, help=f"{name} a todo")
        s.add_argument("id", type=int)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not args.command:
        p.print_usage()
        sys.exit(2)
    path = args.file or os.environ.get("TODO_FILE") or "todos.json"
    todos = load(path)
    if args.command == "add":
        text = " ".join(args.text)
        new_id = max((t["id"] for t in todos), default=0) + 1
        todos.append({"id": new_id, "text": text, "done": False})
        save(path, todos)
        print(f"Added {new_id}: {text}")
    elif args.command == "list":
        if not todos:
            print("No todos.")
        for t in sorted(todos, key=lambda t: t["id"]):
            print(f"[{'x' if t['done'] else ' '}] {t['id']} {t['text']}")
    elif args.command == "done":
        find(todos, args.id)["done"] = True
        save(path, todos)
        print(f"Done {args.id}")
    elif args.command == "remove":
        todos.remove(find(todos, args.id))
        save(path, todos)
        print(f"Removed {args.id}")


if __name__ == "__main__":
    main()
