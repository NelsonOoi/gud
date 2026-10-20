import json
import os
import subprocess
import sys

TODO = os.path.join(os.path.dirname(__file__), "..", "todo.py")


def run(tmp_path, *args, env=None, file=True):
    cmd = [sys.executable, TODO]
    if file:
        cmd += ["--file", str(tmp_path / "t.json")]
    return subprocess.run(cmd + list(args), capture_output=True, text=True,
                          cwd=tmp_path, env=env)


def test_add_and_list(tmp_path):
    r = run(tmp_path, "add", "buy", "milk")
    assert r.stdout == "Added 1: buy milk\n" and r.returncode == 0
    assert run(tmp_path, "add", "call bob").stdout == "Added 2: call bob\n"
    assert run(tmp_path, "list").stdout == "[ ] 1 buy milk\n[ ] 2 call bob\n"


def test_empty_list(tmp_path):
    assert run(tmp_path, "list").stdout == "No todos.\n"


def test_done(tmp_path):
    run(tmp_path, "add", "a")
    r = run(tmp_path, "done", "1")
    assert r.stdout == "Done 1\n" and r.returncode == 0
    assert run(tmp_path, "list").stdout == "[x] 1 a\n"


def test_remove_and_id_after_max(tmp_path):
    for t in "abc":
        run(tmp_path, "add", t)
    assert run(tmp_path, "remove", "2").stdout == "Removed 2\n"
    run(tmp_path, "remove", "3")
    assert run(tmp_path, "add", "d").stdout == "Added 2: d\n"
    data = json.loads((tmp_path / "t.json").read_text())
    assert data == [{"id": 1, "text": "a", "done": False},
                    {"id": 2, "text": "d", "done": False}]


def test_unknown_id(tmp_path):
    for cmd in ("done", "remove"):
        r = run(tmp_path, cmd, "7")
        assert r.returncode == 1
        assert r.stderr == "Error: no todo with id 7\n" and r.stdout == ""


def test_bad_id_and_no_command(tmp_path):
    assert run(tmp_path, "done", "x").returncode == 2
    r = run(tmp_path)
    assert r.returncode == 2 and "usage" in (r.stdout + r.stderr).lower()


def test_env_var_and_default(tmp_path):
    env = dict(os.environ, TODO_FILE=str(tmp_path / "env.json"))
    run(tmp_path, "add", "x", env=env, file=False)
    assert (tmp_path / "env.json").exists()
    env.pop("TODO_FILE")
    run(tmp_path, "add", "y", env=env, file=False)
    assert (tmp_path / "todos.json").exists()
