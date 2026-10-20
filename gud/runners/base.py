"""Shared pieces of the v1 test runners (SPEC §15.2, §22): the `Runner` protocol, `RunResult`,
byte-exact materialisation of a tree into a directory, a byte-exact tarball of a tree, and a
subprocess helper that runs one shell command in its own process group with a hard timeout.

A tree is the `{path: (mode, bytes)}` map of `gud.core.objects.Tree.to_files()`. Modes are
`100644` (file), `100755` (executable) and `120000` (symlink; the bytes are the link target).
Nothing here rewrites bytes: CR, CRLF, NUL and a missing final LF reach the test as they are in
the tree.
"""

from __future__ import annotations

import io
import os
import shlex
import signal
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from gud.core.objects import valid_path
from gud.core.types import Mode

LOG_LIMIT = 65_536          # bytes of output kept (the `test` text record cap, §7)


class RunnerUnavailable(RuntimeError):
    """The runner cannot run at all here (no docker binary, no InstaBox credentials, ...)."""


class RunnerError(ValueError):
    """A tree cannot be materialised safely (bad path, symlink in a parent, ...)."""


@dataclass(frozen=True)
class RunResult:
    passed: bool
    log: str                                 # output tail, at most LOG_LIMIT bytes of UTF-8
    seconds: float
    runner: str                              # "local", "docker", "instabox", ...
    timed_out: bool = False                  # killed at the timeout (then passed is False)
    exit_code: int | None = None             # None when killed or never started


class Runner(Protocol):
    def run(self, files: Mapping[str, tuple[Mode, bytes]], command: str, *, image: str | None,
            timeout_ms: int, extra_args: Sequence[str] = ()) -> RunResult: ...


# ---------------------------------------------------------------- trees on disk

def _check_path(path: str) -> None:
    if not valid_path(path):
        raise RunnerError(f"unsafe tree path: {path!r}")


def materialize(root: Path, files: Mapping[str, tuple[Mode, bytes]]) -> None:
    """Write `files` under the empty directory `root`, byte-exact with modes; symlinks become
    symlinks. No file is written through a symlink or outside `root` (§22)."""
    base = Path(root)
    for path in sorted(files):
        _check_path(path)
    for path in sorted(files):
        mode, data = files[path]
        parts = path.split("/")
        cur = base
        for seg in parts[:-1]:
            cur = cur / seg
            if cur.is_symlink():
                raise RunnerError(f"parent of {path!r} is a symlink")
            if not cur.exists():
                cur.mkdir()
            elif not cur.is_dir():
                raise RunnerError(f"parent of {path!r} is a file")
        dest = cur / parts[-1]
        if mode == "120000":
            if not data or b"\0" in data:
                raise RunnerError(f"bad symlink target for {path!r}")
            os.symlink(os.fsdecode(data), dest)
            continue
        if mode not in ("100644", "100755"):
            raise RunnerError(f"bad mode {mode!r} for {path!r}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(dest, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
        finally:
            os.close(fd)
        os.chmod(dest, 0o755 if mode == "100755" else 0o644)


def tree_tarball(files: Mapping[str, tuple[Mode, bytes]]) -> bytes:
    """Gzipped tar of the tree: regular files with 0644/0755, symlinks as symlinks, directories
    implied; mtime 0 and sorted paths so the bytes are stable."""
    for path in files:
        _check_path(path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        dirs: set[str] = set()
        for path in sorted(files):
            parts = path.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    info = tarfile.TarInfo(d)
                    info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
                    tar.addfile(info)
            mode, data = files[path]
            info = tarfile.TarInfo(path)
            info.mtime = 0
            if mode == "120000":
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, os.fsdecode(data), 0o777
                tar.addfile(info)
            else:
                info.type, info.size = tarfile.REGTYPE, len(data)
                info.mode = 0o755 if mode == "100755" else 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------- processes

def shell_command(command: str, extra_args: Sequence[str] = ()) -> str:
    """The `.gud/test` command line with `extra_args` (test paths) appended shell-quoted."""
    return " ".join([command] + [shlex.quote(a) for a in extra_args])


def child_env(extra_path: Sequence[str] = ()) -> dict[str, str]:
    """The environment for a test process: ours minus pytest's own control variables."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    if extra_path:
        env["PATH"] = os.pathsep.join(list(extra_path) + [env.get("PATH", "")])
    return env


def kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the process group led by `proc` (it was started with a new session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def tail(data: bytes, limit: int = LOG_LIMIT) -> str:
    """The last `limit` bytes of `data` as text, cut at a UTF-8 boundary (never longer than `limit`)."""
    if len(data) > limit:
        data = data[-limit:]
        text = data.decode("utf-8", errors="ignore")
    else:
        text = data.decode("utf-8", errors="replace")
    while len(text.encode("utf-8")) > limit:
        text = text[1:]
    return text


def run_process(argv: Sequence[str], *, cwd: Path, timeout_ms: int, env: Mapping[str, str] | None = None,
                on_timeout: Callable[[], None] | None = None) -> tuple[int | None, bytes, bool, float]:
    """Run `argv` in a new session with stdout+stderr to a temp file (so a background child holding
    the pipe cannot hang us). On timeout the whole process group is killed. After a normal exit the
    group is killed too, so nothing the test started outlives it.

    Returns (exit code or None if killed, output, timed_out, seconds)."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(list(argv), cwd=cwd, env=dict(env) if env is not None else None,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        timed_out = False
        try:
            code: int | None = proc.wait(timeout=max(timeout_ms, 1) / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            if on_timeout is not None:
                try:
                    on_timeout()
                except Exception:
                    pass
            kill_group(proc)
            proc.wait()
            code = None
        except BaseException:
            kill_group(proc)
            proc.wait()
            raise
        else:
            kill_group(proc)
        seconds = time.monotonic() - started
        size = out.seek(0, io.SEEK_END)
        out.seek(max(0, size - LOG_LIMIT))
        data = out.read()
    return code, data, timed_out, seconds


def result_log(output: bytes, *, timed_out: bool, timeout_ms: int, code: int | None, note: str = "") -> str:
    """The log tail with a one-line verdict at the end (`timeout after ...` or `exit N`)."""
    if timed_out:
        last = f"\n[gud tester] timeout after {timeout_ms / 1000:g}s: process group killed\n"
    else:
        last = f"\n[gud tester] exit {code}\n"
    if note:
        last = f"\n[gud tester] {note}" + last
    return tail(output, LOG_LIMIT - len(last.encode("utf-8"))) + last
