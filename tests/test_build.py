"""Local runner, pool cap, and InstaBox credential gate."""

from __future__ import annotations

import io
import json
import tarfile
import threading
import time
from pathlib import Path

import pytest

from gud.build import (
    InstaBoxRunner,
    LocalRunner,
    RunnerPool,
    RunnerUnavailable,
    _token_stale,
    _tree_tarball,
    make_runner,
)
from gud.config import load
from gud.types import RunResult
from gud.wire import tree_hash

DEMO = Path(__file__).resolve().parents[1] / "demo_repo"
_SKIP_DIRS = {".gud", "__pycache__", ".pytest_cache"}


def demo_tree() -> dict[str, str]:
    """Every file under demo_repo except `.gud` and bytecode caches."""
    tree: dict[str, str] = {}
    for path in DEMO.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(DEMO)
        if any(part in _SKIP_DIRS for part in rel.parts) or path.suffix == ".pyc":
            continue
        tree[rel.as_posix()] = path.read_text(encoding="utf-8")
    return tree


def test_local_runner_passes_on_demo_repo():
    tree = demo_tree()
    result = LocalRunner().run(tree)
    assert result.passed, result.log
    assert result.runner == "local"
    assert result.tree_hash == tree_hash(tree)
    assert result.seconds >= 0
    assert "timeout" not in result.log.lower()


def test_local_runner_fails_when_a_test_is_broken():
    tree = demo_tree()
    tree["tests/test_broken.py"] = "def test_broken():\n    assert False\n"
    result = LocalRunner().run(tree)
    assert not result.passed
    assert result.runner == "local"
    assert "FAILED" in result.log or "AssertionError" in result.log


def test_local_runner_timeout():
    runner = LocalRunner(timeout=0.6, command=("python", "-c", "import time; time.sleep(30)"))
    started = time.monotonic()
    result = runner.run({"x.py": "x = 1\n"})
    elapsed = time.monotonic() - started
    assert not result.passed
    assert "timeout" in result.log.lower()
    assert elapsed < 5
    assert result.tree_hash == tree_hash({"x.py": "x = 1\n"})


def test_local_runner_rejects_path_escape():
    runner = LocalRunner(timeout=5, command=("python", "-c", "print('ran')"))
    with pytest.raises(ValueError):
        runner.run({"../outside.py": "x = 1\n"})


def test_runner_pool_caps_concurrency():
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}
    release = threading.Event()

    class Blocking:
        def run(self, tree):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            release.wait(timeout=5)
            with lock:
                state["current"] -= 1
            return RunResult(True, "ok\n", tree_hash(tree), 0.0, "local")

    pool = RunnerPool(Blocking(), max_concurrent=5)
    try:
        futures = [pool.submit({f"f{i}.py": "x = 1\n"}) for i in range(8)]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with lock:
                if state["current"] == 5:
                    break
            time.sleep(0.02)
        with lock:
            assert state["current"] == 5
            assert state["peak"] == 5
        time.sleep(0.15)
        with lock:
            assert state["current"] == 5
        release.set()
        assert all(fut.result(timeout=5).passed for fut in futures)
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_tree_tarball_roundtrip(tmp_path):
    tree = {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"}
    blob = _tree_tarball(tree)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        tar.extractall(tmp_path, filter="data")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (tmp_path / "pkg" / "b.py").read_text(encoding="utf-8") == "y = 2\n"
    with pytest.raises(ValueError):
        _tree_tarball({"../x.py": "no\n"})


def test_instabox_unavailable_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("INSTABOX_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    runner = InstaBoxRunner(api_key=None)
    with pytest.raises(RunnerUnavailable, match="INSTABOX_API_KEY"):
        runner.run({"test_x.py": "def test_x():\n    assert True\n"})


def test_instabox_api_key_and_cli_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("INSTABOX_API_KEY", "ibk_from_env")
    creds = tmp_path / "instabox"
    creds.mkdir()
    (creds / "credentials.json").write_text(json.dumps({
        "server": "https://api.instabox.sh",
        "accessToken": "access-token",
        "accessExpiresAt": "2099-01-01T00:00:00Z",
        "refreshToken": "refresh-token",
        "refreshExpiresAt": "2099-01-01T00:00:00Z",
    }))
    from_env = InstaBoxRunner()._resolve_auth()
    assert from_env.mode == "api_key" and from_env.secret == "ibk_from_env"
    explicit = InstaBoxRunner(api_key="ibk_explicit")._resolve_auth()
    assert explicit.mode == "api_key" and explicit.secret == "ibk_explicit"
    monkeypatch.delenv("INSTABOX_API_KEY")
    from_cli = InstaBoxRunner(api_key=None)._resolve_auth()
    assert from_cli.mode == "bearer" and from_cli.secret == "access-token"


def test_token_staleness():
    assert not _token_stale("2099-01-01T00:00:00Z")
    assert _token_stale("2000-01-01T00:00:00Z")
    assert _token_stale("")
    assert _token_stale("not-a-date")


def test_make_runner_selects_backend(monkeypatch):
    monkeypatch.setenv("GUD_RUNNER", "local")
    assert isinstance(make_runner(load()), LocalRunner)
    monkeypatch.setenv("GUD_RUNNER", "instabox")
    monkeypatch.setenv("INSTABOX_API_KEY", "ibk_test")
    runner = make_runner(load())
    assert isinstance(runner, InstaBoxRunner)
    assert runner.api_key == "ibk_test"
    assert runner.image == "universal"
    monkeypatch.setenv("GUD_RUNNER", "nope")
    with pytest.raises(ValueError):
        make_runner(load())
