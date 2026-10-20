import argparse
import json
import os
import sys


def load(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def save(path, todos):
    with open(path, "w") as f:
        json.dump(todos, f)


def find(todos, todo_id):
    for t in todos:
        if t["id"] == todo_id:
            return t
    print(f"Error: no todo with id {todo_id}", file=sys.stderr)
    sys.exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="todo.py")
    parser.add_argument("--file")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("add")
    p.add_argument("text", nargs="+")
    sub.add_parser("list")
    for name in ("done", "remove"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_usage()
        sys.exit(2)
    path = args.file or os.environ.get("TODO_FILE") or "todos.json"
    todos = load(path)
    if args.command == "add":
        new_id = max((t["id"] for t in todos), default=0) + 1
        text = " ".join(args.text)
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
    else:
        todos.remove(find(todos, args.id))
        save(path, todos)
        print(f"Removed {args.id}")


if __name__ == "__main__":
    main()
