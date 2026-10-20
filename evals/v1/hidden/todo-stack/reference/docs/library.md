# todolib

- `TodoStore(path=None)`: todos in one JSON file (`default_path(path)`), read and written on every call.
  - `list(tag=None, status=None)`, `get(todo_id)`, `add(text, tags=None, due=None)`,
    `update(todo_id, **changes)`, `done(todo_id)`, `tag(todo_id, tags)`, `remove(todo_id)`.
- `default_path(path=None)`: `path`, else `$TODO_FILE`, else `./todos.json`.
- `clean_text(text)`, `clean_due(due)`, `clean_tags(tags)`: validation; raise `InvalidTodo`.
- `format_todo(todo)`: one `list` line.
- Errors: `TodoError` (base), `InvalidTodo`, `TodoNotFound` (message `no todo with id N`).
