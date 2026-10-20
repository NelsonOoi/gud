"""Runners: byte-exact materialisation (binary, CRLF, symlink, exec bit), honest timeouts with a
process-group kill, log truncation, tarballs for remote runners, and Docker / InstaBox (live runs
are skipped here: no docker binary, no INSTABOX_API_KEY)."""

import hashlib
import io
import os
import shutil
import stat
import tarfile
import time
from pathlib import Path

import pytest

from gud.runners.base import LOG_LIMIT, RunnerError, materialize, tail, tree_tarball
from gud.runners.docker import DockerRunner
from gud.runners.local import LocalRunner
from gud.tester_v1 import make_runner
from tests.v1.kit import status
from tests.v1.tester.util import PY, chain_tests, make_tester, repo

BINARY = bytes(range(256)) + b"\0\0\xff\xfe"
CRLF = b"line one\r\nline two\r\nno final newline"
FILES = {
    "data/blob.bin": ("100644", BINARY),
    "data/crlf.txt": ("100644", CRLF),
    "bin/run.sh": ("100755", b"#!/bin/sh\necho ran\n"),
    "link-to-blob": ("120000", b"data/blob.bin"),
    "dangling": ("120000", b"../outside/target"),
}


def test_materialize_is_byte_exact(tmp_path):
    materialize(tmp_path, FILES)
    assert (tmp_path / "data/blob.bin").read_bytes() == BINARY
    assert (tmp_path / "data/crlf.txt").read_bytes() == CRLF
    assert os.readlink(tmp_path / "link-to-blob") == "data/blob.bin"
    assert (tmp_path / "link-to-blob").read_bytes() == BINARY
    assert os.readlink(tmp_path / "dangling") == "../outside/target"    # created, never followed
    assert not (tmp_path.parent / "outside").exists()
    assert stat.S_IMODE((tmp_path / "bin/run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "data/crlf.txt").stat().st_mode) == 0o644


def test_materialize_refuses_unsafe_paths(tmp_path):
    for bad in ("../x", "/abs", "a/../b", ".git/config", ".gud/local/x"):
        with pytest.raises(RunnerError):
            materialize(tmp_path / bad.replace("/", "_"), {bad: ("100644", b"x")})


CHECK = f"""import hashlib, os, stat, sys
want = {{p: hashlib.sha256(b).hexdigest() for p, b in {{"data/blob.bin": {BINARY!r}, "data/crlf.txt": {CRLF!r}}}.items()}}
for p, h in want.items():
    assert hashlib.sha256(open(p, "rb").read()).hexdigest() == h, p
assert os.path.islink("link-to-blob") and os.readlink("link-to-blob") == "data/blob.bin"
assert os.access("bin/run.sh", os.X_OK) and not os.access("data/crlf.txt", os.X_OK)
print("byte-exact")
"""


def test_runner_sees_the_tree_byte_exactly():
    files = dict(FILES)
    files["check.py"] = ("100644", CHECK.encode())
    r = LocalRunner().run(files, f"{PY} check.py && ./bin/run.sh", timeout_ms=30_000)
    assert r.passed, r.log
    assert "byte-exact" in r.log and "ran" in r.log and r.exit_code == 0


def test_byte_exact_tree_end_to_end_through_the_tester():
    files = dict(FILES)
    files["check.py"] = ("100644", CHECK.encode())
    k, g = repo(f"command {PY} check.py\non-paths data/**\n", files)
    p = k.push("@alice", g, {"data/new.bin": ("100644", b"\x00\x01")})
    make_tester(k).step()
    chain, _ = k.run()
    assert status(chain, p) == "linked"
    assert "byte-exact" in chain_tests(k, "push")[0].text


def test_timeout_kills_the_process_group(tmp_path):
    pids = tmp_path / "pids"
    cmd = f"sleep 60 & echo $! > {pids}; sleep 60"
    start = time.monotonic()
    r = LocalRunner().run({"x": ("100644", b"x")}, cmd, timeout_ms=500)
    assert time.monotonic() - start < 10
    assert not r.passed and r.timed_out and r.exit_code is None
    assert "timeout after 0.5s" in r.log
    child = int(pids.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("background child survived the timeout")


def test_timeout_is_an_honest_fail_on_the_chain():
    k, g = repo("command sleep 30\ntimeout 1s\non-paths a.txt\n")
    p = k.push("@alice", g, {"a.txt": "a2\n"})
    make_tester(k).step()
    chain, _ = k.run()
    assert status(chain, p) == "tests_failed"
    env = chain_tests(k, "push")[0]
    assert env.get("Gud-Result") == "fail" and "timeout" in env.text


def test_background_child_after_exit_is_reaped(tmp_path):
    pids = tmp_path / "pids"
    r = LocalRunner().run({"x": ("100644", b"x")}, f"sleep 60 & echo $! > {pids}; exit 0", timeout_ms=10_000)
    assert r.passed
    child = int(pids.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("background child outlived the run")


def test_log_is_truncated_to_the_record_limit():
    r = LocalRunner().run({"x": ("100644", b"x")}, f"{PY} -c \"print('é' * 100000)\"", timeout_ms=30_000)
    assert len(r.log.encode()) <= LOG_LIMIT and r.log.rstrip().endswith("exit 0")
    assert len(tail("é".encode() * 10, 5).encode()) <= 5


def test_extra_args_are_quoted():
    r = LocalRunner().run({"a b.txt": ("100644", b"hi\n")}, "cat", timeout_ms=10_000, extra_args=["a b.txt"])
    assert r.passed and "hi" in r.log


def test_tarball_keeps_modes_and_symlinks():
    data = tree_tarball(FILES)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert members["bin/run.sh"].mode == 0o755 and members["data/crlf.txt"].mode == 0o644
        assert members["link-to-blob"].issym() and members["link-to-blob"].linkname == "data/blob.bin"
        assert tar.extractfile("data/blob.bin").read() == BINARY
        assert tar.extractfile("data/crlf.txt").read() == CRLF
        assert members["data"].isdir()
    assert tree_tarball(FILES) == data                    # stable bytes


def test_docker_argv():
    d = DockerRunner(user=None)
    argv = d.argv(Path("/tmp/w"), "pytest -q", "python:3.12-slim", "gud-test-1", ["tests/a b.py"])
    assert argv[:3] == ["docker", "run", "--rm"]
    assert ["-v", "/tmp/w:/work"] == argv[argv.index("-v"):argv.index("-v") + 2]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv[-4:] == ["python:3.12-slim", "/bin/sh", "-c", "pytest -q 'tests/a b.py'"]


def test_docker_without_image_uses_the_fallback():
    r = DockerRunner(docker="definitely-not-docker").run({"x": ("100644", b"x")}, "true", image=None,
                                                         timeout_ms=10_000)
    assert r.passed and r.runner == "local"


def test_make_runner():
    assert isinstance(make_runner("local"), LocalRunner)
    assert isinstance(make_runner("docker"), DockerRunner)
    with pytest.raises(ValueError):
        make_runner("nope")  # type: ignore[arg-type]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_docker_live():
    r = DockerRunner().run(FILES, "test -L link-to-blob && test -x bin/run.sh && cat data/crlf.txt",
                           image="alpine:3.20", timeout_ms=120_000)
    assert r.passed, r.log
    timed = DockerRunner().run(FILES, "sleep 60", image="alpine:3.20", timeout_ms=2_000)
    assert timed.timed_out and not timed.passed


@pytest.mark.skipif(not os.environ.get("INSTABOX_API_KEY"), reason="INSTABOX_API_KEY is not set")
def test_instabox_live():
    from gud.runners.instabox import InstaBoxRunner
    runner = InstaBoxRunner(after="destroy", bootstrap=None)
    try:
        r = runner.run(FILES, "test -L link-to-blob && test -x bin/run.sh && sha256sum data/blob.bin",
                       image=None, timeout_ms=120_000)
        assert r.passed, r.log
        assert hashlib.sha256(BINARY).hexdigest() in r.log
    finally:
        runner.close()


def test_instabox_command_line():
    from gud.runners.instabox import InstaBoxRunner
    rb = InstaBoxRunner(api_key="unused")
    assert rb.test_command("pytest -q", None, ["t.py"]).endswith("cd /data/gud-work && pytest -q t.py")
    assert "docker run --rm" in rb.test_command("pytest -q", "python:3.12", ())
