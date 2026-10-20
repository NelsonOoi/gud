import json
import os
import subprocess
import sys

TODO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "todo.py")


def run(tmp_path, *args, env=None, use_file=True):
    cmd = [sys.executable, TODO]
    if use_file:
        cmd += ["--file", str(tmp_path / "t.json")]
    e = dict(os.environ)
    e.pop("TODO_FILE", None)
    e.update(env or {})
    return subprocess.run(cmd + list(args), capture_output=True, text=True, cwd=tmp_path, env=e)


def test_add_and_list(tmp_path):
    r = run(tmp_path, "add", "buy", "milk")
    assert (r.returncode, r.stdout) == (0, "Added 1: buy milk\n")
    r = run(tmp_path, "add", "call bob")
    assert r.stdout == "Added 2: call bob\n"
    r = run(tmp_path, "list")
    assert r.returncode == 0
    assert r.stdout == "[ ] 1 buy milk\n[ ] 2 call bob\n"
    data = json.loads((tmp_path / "t.json").read_text())
    assert data[0] == {"id": 1, "text": "buy milk", "done": False}


def test_list_empty(tmp_path):
    r = run(tmp_path, "list")
    assert (r.returncode, r.stdout) == (0, "No todos.\n")


def test_done(tmp_path):
    run(tmp_path, "add", "a")
    run(tmp_path, "add", "b")
    r = run(tmp_path, "done", "2")
    assert (r.returncode, r.stdout) == (0, "Done 2\n")
    assert run(tmp_path, "list").stdout == "[ ] 1 a\n[x] 2 b\n"


def test_remove_and_id_after_max(tmp_path):
    for t in "abc":
        run(tmp_path, "add", t)
    r = run(tmp_path, "remove", "2")
    assert (r.returncode, r.stdout) == (0, "Removed 2\n")
    assert run(tmp_path, "list").stdout == "[ ] 1 a\n[ ] 3 c\n"
    assert run(tmp_path, "add", "d").stdout == "Added 4: d\n"
    run(tmp_path, "remove", "4")
    assert run(tmp_path, "add", "e").stdout == "Added 4: e\n"


def test_unknown_id(tmp_path):
    for cmd in ("done", "remove"):
        r = run(tmp_path, cmd, "7")
        assert r.returncode == 1
        assert r.stderr.strip() == "Error: no todo with id 7"
        assert r.stdout == ""


def test_bad_id_and_no_command(tmp_path):
    assert run(tmp_path, "done", "x").returncode == 2
    assert run(tmp_path, "remove", "x").returncode == 2
    r = run(tmp_path)
    assert r.returncode == 2
    assert "usage" in (r.stdout + r.stderr).lower()


def test_env_var_and_default(tmp_path):
    f = tmp_path / "env.json"
    run(tmp_path, "add", "x", env={"TODO_FILE": str(f)}, use_file=False)
    assert f.exists()
    run(tmp_path, "add", "y", use_file=False)
    assert (tmp_path / "todos.json").exists()


def test_file_flag_beats_env(tmp_path):
    run(tmp_path, "add", "x", env={"TODO_FILE": str(tmp_path / "env.json")})
    assert (tmp_path / "t.json").exists()
    assert not (tmp_path / "env.json").exists()
