# Build a todo command-line app

Build `todo.py`, a small Python command-line todo list, at the repo root. Standard library only, using `argparse`
with one subcommand per action.

Usage: `python todo.py [--file PATH] COMMAND ...`

- `--file PATH` is the JSON file the todos are stored in. Without it, use the `TODO_FILE` environment variable,
  and without that, `todos.json` in the current directory. A missing file means an empty list.
- The file holds a JSON list of objects, oldest first: `{"id": 1, "text": "buy milk", "done": false}`.
- `add TEXT...` joins the words with spaces, stores a new todo, and prints `Added 1: buy milk`.
  The new id is one more than the highest id currently stored (1 for an empty list).
- `list` prints one line per todo in id order: `[ ] 1 buy milk` for open, `[x] 2 call bob` for done.
  With no todos it prints `No todos.`
- `done ID` marks the todo done and prints `Done 1`.
- `remove ID` deletes the todo and prints `Removed 1`.
- An unknown id prints `Error: no todo with id 7` to stderr and exits with status 1. A non-integer id is an
  argparse usage error (exit status 2). Running with no command prints usage and exits with status 2.
- Every successful command exits with status 0.

Add pytest tests under tests/ that cover every command, and keep `python -m pytest -q` passing.
