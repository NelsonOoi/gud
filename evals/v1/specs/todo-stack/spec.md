# Build a todo library with a command-line app, an HTTP API and docs

Build a small todo system at the repo root, standard library only. It has four parts that must agree with each
other: a library, a command-line app and an HTTP API that both use the library, and docs for all three.

**The library, `todolib.py`**, holds all storage, validation and logic. Its API (names, signatures, exception
types) is up to you, but `cli.py` and `api.py` must use it and must not read or write the store file
themselves (no `open`, `read_text`/`write_text` or `json.load`/`json.dump` in them).

- The store is a JSON file holding a list of todos, oldest first:
  `{"id": 1, "text": "buy milk", "done": false, "tags": ["home"], "due": "2026-11-01"}`. A missing file is an
  empty list. The file is read and written on every operation (no in-memory cache), so the command-line app
  and the API can work on the same file at the same time.
- A new todo's id is one more than the highest id stored (1 for an empty list).
- `text` is stripped and must not be empty. `due` is `null` or a real calendar date written `YYYY-MM-DD`.
  Tags are lowercased, must match `[a-z0-9-]+`, and duplicates are dropped (first occurrence kept).
- Invalid input raises an error whose message says what is wrong; an unknown id raises an error whose
  message is exactly `no todo with id 7` (for id 7). The app and the API show these library messages
  unchanged, so the same mistake gives the same message in both.

**The command-line app**: `python cli.py [--file PATH] COMMAND ...`, using `argparse` with one subcommand per
action. Without `--file`, use the `TODO_FILE` environment variable, and without that `todos.json` in the
current directory.

- `add TEXT... [--tag TAG]... [--due DATE]` joins the words with spaces and prints `Added 1: buy milk`.
- `list [--tag TAG] [--open | --done]` prints one line per todo in id order: `[ ] 1 buy milk` for open,
  `[x] 2 call bob` for done, followed by ` #tag` for each tag and ` (due 2026-11-01)` if it has a due date,
  e.g. `[ ] 1 buy milk #home #errand (due 2026-11-01)`. `--tag` keeps todos with that tag; `--open` and
  `--done` filter by state. With nothing to show it prints `No todos.`
- `done ID` prints `Done 1`. `tag ID TAG...` adds tags and prints `Tagged 1: #home #errand` (all its tags
  after the change). `remove ID` prints `Removed 1`.
- Errors from the library print `Error: <message>` to stderr: exit status 1 for an unknown id, 2 for invalid
  input. A non-integer id, or no command at all, is an argparse usage error (exit status 2). Success exits 0.

**The HTTP API**: `python api.py [--port PORT] [--file PATH]` serves JSON on 127.0.0.1 (default port 8000;
the store file is chosen as for the app) with `http.server`:

- `GET /todos` lists todos in id order, optionally filtered by `?tag=TAG` and `?status=open` or `?status=done`.
- `POST /todos` with `{"text": ..., "tags": [...], "due": ...}` (only `text` required) creates one: 201 and the todo.
- `GET /todos/<id>` returns one todo. `PATCH /todos/<id>` with any of `text`, `done`, `tags` (replaces them)
  and `due` updates it and returns the todo. `DELETE /todos/<id>` removes it: 204 with no body.
- Errors are JSON `{"error": "<message>"}`: 404 for an unknown todo id (the library's message) and for any
  other path, 400 for invalid input (the library's message) or a body that is not a JSON object.

**Docs**, in `docs/`, linked from README.md:

- `docs/library.md` documents every public function and class of `todolib` and every public method of those
  classes, each named in backticks (for example `` `add_todo(text, tags=None, due=None)` ``).
- `docs/cli.md` documents every subcommand and option, each in backticks (`add`, `--due`, ...).
- `docs/http.md` documents every endpoint as method and path in backticks, for example `` `GET /todos/<id>` ``.

Add pytest tests under tests/ for the library, the app and the API, and keep `python -m pytest -q` passing.
