"""The todo library: storage, validation and logic. The CLI and the HTTP API both use it."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path

_TAG = re.compile(r"^[a-z0-9-]+$")


class TodoError(Exception):
    """Base class of every todolib error."""


class InvalidTodo(TodoError, ValueError):
    """Invalid input: empty text, a bad date or a bad tag."""


class TodoNotFound(TodoError, LookupError):
    """No todo has the given id."""

    def __init__(self, todo_id: int):
        super().__init__(f"no todo with id {todo_id}")
        self.todo_id = todo_id


def default_path(path: str | os.PathLike | None = None) -> Path:
    """The store file: `path`, else $TODO_FILE, else ./todos.json."""
    if path:
        return Path(path)
    return Path(os.environ.get("TODO_FILE") or "todos.json")


def clean_text(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise InvalidTodo("text must not be empty")
    return text.strip()


def clean_due(due) -> str | None:
    if due is None:
        return None
    if not isinstance(due, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
        raise InvalidTodo(f"due must be a date written YYYY-MM-DD, not {due!r}")
    try:
        _dt.date.fromisoformat(due)
    except ValueError:
        raise InvalidTodo(f"due must be a real date, not {due!r}") from None
    return due


def clean_tags(tags) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str) or not all(isinstance(t, str) for t in tags):
        raise InvalidTodo("tags must be a list of words")
    out: list[str] = []
    for tag in tags:
        tag = tag.strip().lower()
        if not _TAG.match(tag):
            raise InvalidTodo(f"tag {tag!r} must match [a-z0-9-]+")
        if tag not in out:
            out.append(tag)
    return out


class TodoStore:
    """Todos in one JSON file, read and written on every call."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = default_path(path)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8") or "[]")

    def _save(self, todos: list[dict]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(todos, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def _find(self, todos: list[dict], todo_id: int) -> dict:
        for todo in todos:
            if todo["id"] == todo_id:
                return todo
        raise TodoNotFound(todo_id)

    def list(self, tag: str | None = None, status: str | None = None) -> list[dict]:
        """Todos in id order; `tag` keeps those with the tag; `status` is None, "open" or "done"."""
        if status not in (None, "open", "done"):
            raise InvalidTodo("status must be open or done")
        out = sorted(self._load(), key=lambda t: t["id"])
        if tag is not None:
            out = [t for t in out if tag.lower() in t.get("tags", [])]
        if status is not None:
            out = [t for t in out if t["done"] == (status == "done")]
        return out

    def get(self, todo_id: int) -> dict:
        return self._find(self._load(), todo_id)

    def add(self, text: str, tags=None, due: str | None = None) -> dict:
        todo = {"id": 0, "text": clean_text(text), "done": False, "tags": clean_tags(tags), "due": clean_due(due)}
        todos = self._load()
        todo["id"] = max((t["id"] for t in todos), default=0) + 1
        todos.append(todo)
        self._save(todos)
        return todo

    def update(self, todo_id: int, **changes) -> dict:
        """Change any of text, done, tags (replaced) and due."""
        todos = self._load()
        todo = self._find(todos, todo_id)
        unknown = set(changes) - {"text", "done", "tags", "due"}
        if unknown:
            raise InvalidTodo(f"unknown field {sorted(unknown)[0]!r}")
        if "text" in changes:
            todo["text"] = clean_text(changes["text"])
        if "done" in changes:
            if not isinstance(changes["done"], bool):
                raise InvalidTodo("done must be true or false")
            todo["done"] = changes["done"]
        if "tags" in changes:
            todo["tags"] = clean_tags(changes["tags"])
        if "due" in changes:
            todo["due"] = clean_due(changes["due"])
        self._save(todos)
        return todo

    def done(self, todo_id: int) -> dict:
        return self.update(todo_id, done=True)

    def tag(self, todo_id: int, tags) -> dict:
        todo = self.get(todo_id)
        return self.update(todo_id, tags=list(todo.get("tags", [])) + list(tags))

    def remove(self, todo_id: int) -> dict:
        todos = self._load()
        todo = self._find(todos, todo_id)
        todos.remove(todo)
        self._save(todos)
        return todo


def format_todo(todo: dict) -> str:
    """`[ ] 1 buy milk #home (due 2026-11-01)`."""
    line = f"[{'x' if todo['done'] else ' '}] {todo['id']} {todo['text']}"
    line += "".join(f" #{t}" for t in todo.get("tags", []))
    if todo.get("due"):
        line += f" (due {todo['due']})"
    return line
