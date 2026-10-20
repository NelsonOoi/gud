"""Hidden acceptance tests for the todo-stack spec. Agents never see this file.

The harness runs them against a copy of the final tree, named by EVAL_TREE. The app and the API run as
subprocesses (`python cli.py ...`, `python api.py --port P --file F`) on a temporary store file.
"""

from __future__ import annotations

import ast
import http.client
import importlib.util
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

TREE = Path(os.environ.get("EVAL_TREE", ".")).resolve()


def _need(name: str) -> Path:
    path = TREE / name
    if not path.is_file():
        pytest.fail(f"{name} is missing")
    return path


def cli(*args, store=None, cwd=None, env_file=None):
    env = {k: v for k, v in os.environ.items() if k != "TODO_FILE"}
    if env_file is not None:
        env["TODO_FILE"] = str(env_file)
    argv = [sys.executable, str(_need("cli.py"))]
    if store is not None:
        argv += ["--file", str(store)]
    proc = subprocess.run(argv + [str(a) for a in args], cwd=cwd or TREE, env=env, capture_output=True, text=True,
                          timeout=30)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def store(tmp_path):
    return tmp_path / "todos.json"


@pytest.fixture
def api(store):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "TODO_FILE"}
    proc = subprocess.Popen([sys.executable, str(_need("api.py")), "--port", str(port), "--file", str(store)],
                            cwd=TREE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"api.py exited: {proc.stderr.read().decode()[-500:]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("api.py did not start listening")
    yield port
    proc.kill()
    proc.wait(timeout=10)


def call(port, method, path, body=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": "application/json"} if data is not None else {}
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    text = resp.read()
    conn.close()
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text.decode(errors="replace")
    return resp.status, parsed


def _sub(todo: dict) -> dict:
    return {k: todo.get(k) for k in ("id", "text", "done", "tags", "due")}


# ---------------------------------------------------------------- the app

def test_cli_add_and_list(store):
    assert cli("add", "buy", "milk", store=store) == (0, "Added 1: buy milk", "")
    assert cli("add", "call bob", "--tag", "Work", "--tag", "phone", "--due", "2026-11-01", store=store)[1] == \
        "Added 2: call bob"
    code, out, _ = cli("list", store=store)
    assert code == 0
    assert out.splitlines() == ["[ ] 1 buy milk", "[ ] 2 call bob #work #phone (due 2026-11-01)"]


def test_cli_store_format(store):
    cli("add", "buy milk", "--tag", "home", "--tag", "HOME", store=store)
    data = json.loads(store.read_text())
    assert data == [{"id": 1, "text": "buy milk", "done": False, "tags": ["home"], "due": None}]


def test_cli_empty_list(store):
    assert cli("list", store=store) == (0, "No todos.", "")


def test_cli_ids_follow_highest(store):
    store.write_text(json.dumps([{"id": 5, "text": "x", "done": False, "tags": [], "due": None}]))
    assert cli("add", "y", store=store)[1] == "Added 6: y"


def test_cli_done_tag_remove(store):
    cli("add", "a", store=store)
    cli("add", "b", store=store)
    assert cli("done", "1", store=store) == (0, "Done 1", "")
    assert cli("tag", "2", "Home", "errand", "home", store=store) == (0, "Tagged 2: #home #errand", "")
    assert cli("list", store=store)[1].splitlines() == ["[x] 1 a", "[ ] 2 b #home #errand"]
    assert cli("remove", "1", store=store) == (0, "Removed 1", "")
    assert cli("list", store=store)[1] == "[ ] 2 b #home #errand"


def test_cli_filters(store):
    cli("add", "a", "--tag", "home", store=store)
    cli("add", "b", "--tag", "work", store=store)
    cli("add", "c", "--tag", "home", store=store)
    cli("done", "3", store=store)
    assert cli("list", "--tag", "home", store=store)[1].splitlines() == ["[ ] 1 a #home", "[x] 3 c #home"]
    assert cli("list", "--open", store=store)[1].splitlines() == ["[ ] 1 a #home", "[ ] 2 b #work"]
    assert cli("list", "--done", store=store)[1] == "[x] 3 c #home"
    assert cli("list", "--tag", "nope", store=store)[1] == "No todos."


def test_cli_unknown_id(store):
    code, out, err = cli("done", "7", store=store)
    assert code == 1 and err == "Error: no todo with id 7"


def test_cli_invalid_input_exit_2(store):
    code, _, err = cli("add", "x", "--due", "2026-02-30", store=store)
    assert code == 2 and err.startswith("Error: ") and len(err) > len("Error: ")
    code, _, err = cli("add", "   ", store=store)
    assert code == 2 and err.startswith("Error: ")
    code, _, err = cli("add", "x", "--tag", "no spaces!", store=store)
    assert code == 2 and err.startswith("Error: ")
    assert not store.exists() or json.loads(store.read_text()) == []


def test_cli_usage_errors(store):
    assert cli("done", "abc", store=store)[0] == 2
    assert cli(store=store)[0] == 2


def test_cli_file_resolution(tmp_path):
    env_file = tmp_path / "env.json"
    cli("add", "from env", env_file=env_file, cwd=tmp_path)
    assert json.loads(env_file.read_text())[0]["text"] == "from env"
    cli("add", "from cwd", cwd=tmp_path)
    assert json.loads((tmp_path / "todos.json").read_text())[0]["text"] == "from cwd"
    explicit = tmp_path / "explicit.json"
    cli("add", "explicit", store=explicit, env_file=env_file, cwd=tmp_path)
    assert json.loads(explicit.read_text())[0]["text"] == "explicit"


def test_cli_uses_argparse_subcommands():
    tree = ast.parse(_need("cli.py").read_text())
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "add_subparsers" in calls


# ---------------------------------------------------------------- the API

def test_api_create_get_list(api):
    status, body = call(api, "POST", "/todos", {"text": " buy milk ", "tags": ["Home"], "due": "2026-11-01"})
    assert status == 201 and _sub(body) == {"id": 1, "text": "buy milk", "done": False, "tags": ["home"],
                                            "due": "2026-11-01"}
    status, body = call(api, "POST", "/todos", {"text": "call bob"})
    assert status == 201 and _sub(body) == {"id": 2, "text": "call bob", "done": False, "tags": [], "due": None}
    status, body = call(api, "GET", "/todos/1")
    assert status == 200 and body["text"] == "buy milk"
    status, body = call(api, "GET", "/todos")
    assert status == 200 and [t["id"] for t in body] == [1, 2]


def test_api_patch_and_delete(api):
    call(api, "POST", "/todos", {"text": "a", "tags": ["x"]})
    status, body = call(api, "PATCH", "/todos/1", {"done": True, "tags": ["Work", "home"], "text": "b"})
    assert status == 200 and _sub(body) == {"id": 1, "text": "b", "done": True, "tags": ["work", "home"], "due": None}
    status, body = call(api, "DELETE", "/todos/1")
    assert status == 204 and body is None
    assert call(api, "GET", "/todos/1")[0] == 404


def test_api_filters(api):
    call(api, "POST", "/todos", {"text": "a", "tags": ["home"]})
    call(api, "POST", "/todos", {"text": "b", "tags": ["work"]})
    call(api, "PATCH", "/todos/2", {"done": True})
    assert [t["id"] for t in call(api, "GET", "/todos?tag=home")[1]] == [1]
    assert [t["id"] for t in call(api, "GET", "/todos?status=done")[1]] == [2]
    assert [t["id"] for t in call(api, "GET", "/todos?status=open")[1]] == [1]


def test_api_errors(api):
    status, body = call(api, "GET", "/todos/7")
    assert status == 404 and body == {"error": "no todo with id 7"}
    assert call(api, "PATCH", "/todos/7", {"done": True}) == (404, {"error": "no todo with id 7"})
    status, body = call(api, "POST", "/todos", {"text": "x", "due": "2026-13-01"})
    assert status == 400 and isinstance(body, dict) and body.get("error")
    status, body = call(api, "POST", "/todos", raw=b"{not json")
    assert status == 400 and isinstance(body, dict) and body.get("error")
    assert call(api, "GET", "/nothing")[0] == 404


# ---------------------------------------------------------------- consistency

def test_app_and_api_share_the_store(store, api):
    cli("add", "from cli", "--tag", "home", store=store)
    status, body = call(api, "GET", "/todos")
    assert status == 200 and [t["text"] for t in body] == ["from cli"]
    call(api, "POST", "/todos", {"text": "from api", "due": "2026-12-24"})
    assert cli("list", store=store)[1].splitlines() == ["[ ] 1 from cli #home", "[ ] 2 from api (due 2026-12-24)"]
    cli("done", "2", store=store)
    assert call(api, "GET", "/todos/2")[1]["done"] is True


@pytest.mark.parametrize("cli_args,body", [
    (("add", "x", "--due", "2026-13-01"), {"text": "x", "due": "2026-13-01"}),
    (("add", "  "), {"text": "  "}),
    (("add", "x", "--tag", "bad tag"), {"text": "x", "tags": ["bad tag"]}),
])
def test_same_message_in_app_and_api(store, api, cli_args, body):
    code, _, err = cli(*cli_args, store=store)
    status, resp = call(api, "POST", "/todos", body)
    assert code == 2 and status == 400
    assert err == f"Error: {resp['error']}", "the app and the API must show the library's message unchanged"


def test_unknown_id_same_message(store, api):
    _, _, err = cli("remove", "9", store=store)
    status, resp = call(api, "DELETE", "/todos/9")
    assert status == 404 and err == "Error: " + resp["error"] == "Error: no todo with id 9"


# ---------------------------------------------------------------- structure and docs

STORE_CALLS = {"open", "read_text", "write_text", "load", "dump"}


@pytest.mark.parametrize("name", ["cli.py", "api.py"])
def test_front_ends_use_the_library(name):
    tree = ast.parse(_need(name).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "todolib" in imported, f"{name} must import todolib"
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name_ = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if name_ in STORE_CALLS:
                bad.append(name_)
    assert not bad, f"{name} must leave the store file to todolib, but calls {sorted(set(bad))}"


def _library():
    path = _need("todolib.py")
    sys.path.insert(0, str(TREE))
    try:
        spec = importlib.util.spec_from_file_location("todolib", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(TREE))
    return mod


def _public_names(mod) -> list[str]:
    names = []
    for name, obj in vars(mod).items():
        if name.startswith("_") or not (inspect.isfunction(obj) or inspect.isclass(obj)):
            continue
        if getattr(obj, "__module__", None) != mod.__name__:
            continue
        names.append(name)
        if inspect.isclass(obj) and not issubclass(obj, BaseException):
            for attr, val in vars(obj).items():
                if attr.startswith("_"):
                    continue
                if inspect.isfunction(val) or isinstance(val, (property, classmethod, staticmethod)):
                    names.append(attr)
    return sorted(set(names))


def _backticked(path: Path) -> str:
    if not path.is_file():
        pytest.fail(f"{path.relative_to(TREE)} is missing")
    return " ".join(re.findall(r"`([^`]+)`", path.read_text(encoding="utf-8")))


def test_library_has_a_public_api():
    assert len(_public_names(_library())) >= 3


def test_docs_library_covers_public_api():
    names = _public_names(_library())
    spans = _backticked(TREE / "docs" / "library.md")
    missing = [n for n in names if not re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", spans)]
    assert not missing, f"docs/library.md does not document {missing}"


def test_docs_cli_covers_commands_and_options():
    spans = _backticked(TREE / "docs" / "cli.md")
    for word in ("add", "list", "done", "tag", "remove", "--file", "--tag", "--due", "--open", "--done"):
        assert re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", spans), f"docs/cli.md does not document {word}"


def test_docs_http_covers_endpoints():
    spans = _backticked(TREE / "docs" / "http.md")
    ident = r"(?:<id>|\{id\}|:id|<todo_id>|\{todo_id\}|:todo_id|ID|\{ID\}|<ID>)"
    for method, path in (("GET", "/todos"), ("POST", "/todos"), ("GET", f"/todos/{ident}"),
                         ("PATCH", f"/todos/{ident}"), ("DELETE", f"/todos/{ident}")):
        pat = rf"{method}\s+{path if path.endswith(')') else re.escape(path)}(?![/\w])"
        assert re.search(pat, spans), f"docs/http.md does not document {method} {path}"


def test_readme_links_docs():
    text = _need("README.md").read_text(encoding="utf-8")
    for doc in ("docs/library.md", "docs/cli.md", "docs/http.md"):
        assert doc in text, f"README.md must link {doc}"
