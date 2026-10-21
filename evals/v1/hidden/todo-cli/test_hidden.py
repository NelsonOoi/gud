"""Hidden acceptance tests for the todo-cli spec. Agents never see this file.

The harness runs them against a copy of the final tree, named by EVAL_TREE.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TREE = Path(os.environ.get("EVAL_TREE", ".")).resolve()
SCRIPT = TREE / "todo.py"


def run(tmp_path, *args, env_file=None, use_flag=True):
    if not SCRIPT.is_file():
        pytest.fail("todo.py is missing")
    store = tmp_path / "store.json"
    argv = [sys.executable, str(SCRIPT)]
    if use_flag:
        argv += ["--file", str(store)]
    argv += list(args)
    env = {k: v for k, v in os.environ.items() if k != "TODO_FILE"}
    if env_file is not None:
        env["TODO_FILE"] = str(env_file)
    return subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)


def stored(tmp_path):
    return json.loads((tmp_path / "store.json").read_text())


def test_add_prints_and_stores(tmp_path):
    proc = run(tmp_path, "add", "buy", "milk")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "Added 1: buy milk"
    assert stored(tmp_path) == [{"id": 1, "text": "buy milk", "done": False}]


def test_ids_increase(tmp_path):
    run(tmp_path, "add", "a")
    proc = run(tmp_path, "add", "b")
    assert proc.stdout.strip() == "Added 2: b"
    assert [t["id"] for t in stored(tmp_path)] == [1, 2]


def test_list_empty(tmp_path):
    proc = run(tmp_path, "list")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "No todos."


def test_list_shows_status(tmp_path):
    run(tmp_path, "add", "buy", "milk")
    run(tmp_path, "add", "call", "bob")
    run(tmp_path, "done", "2")
    proc = run(tmp_path, "list")
    assert proc.returncode == 0
    assert proc.stdout.strip().splitlines() == ["[ ] 1 buy milk", "[x] 2 call bob"]


def test_done(tmp_path):
    run(tmp_path, "add", "x")
    proc = run(tmp_path, "done", "1")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "Done 1"
    assert stored(tmp_path)[0]["done"] is True


def test_remove(tmp_path):
    run(tmp_path, "add", "x")
    run(tmp_path, "add", "y")
    proc = run(tmp_path, "remove", "1")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "Removed 1"
    assert stored(tmp_path) == [{"id": 2, "text": "y", "done": False}]


def test_next_id_after_remove_is_max_plus_one(tmp_path):
    run(tmp_path, "add", "x")
    run(tmp_path, "add", "y")
    run(tmp_path, "remove", "1")
    proc = run(tmp_path, "add", "z")
    assert proc.stdout.strip() == "Added 3: z"


@pytest.mark.parametrize("command", ["done", "remove"])
def test_unknown_id(tmp_path, command):
    run(tmp_path, "add", "x")
    proc = run(tmp_path, command, "7")
    assert proc.returncode == 1
    assert "Error: no todo with id 7" in proc.stderr


def test_non_integer_id_is_usage_error(tmp_path):
    proc = run(tmp_path, "done", "abc")
    assert proc.returncode == 2


def test_no_command_is_usage_error(tmp_path):
    proc = run(tmp_path)
    assert proc.returncode == 2
    assert "usage" in (proc.stdout + proc.stderr).lower()


def test_help_lists_subcommands(tmp_path):
    proc = run(tmp_path, "--help")
    assert proc.returncode == 0
    for name in ("add", "list", "done", "remove"):
        assert name in proc.stdout


def test_todo_file_env(tmp_path):
    target = tmp_path / "env.json"
    proc = run(tmp_path, "add", "from", "env", env_file=target, use_flag=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(target.read_text())[0]["text"] == "from env"


def test_default_file_in_cwd(tmp_path):
    proc = run(tmp_path, "add", "default", use_flag=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((tmp_path / "todos.json").read_text())[0]["text"] == "default"


def test_uses_argparse():
    assert "argparse" in SCRIPT.read_text(), "todo.py must use argparse"
