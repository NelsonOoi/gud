"""`DockerRunner`: run the `.gud/test` command in the `.gud/test` `image` with the docker CLI.

The tree is materialised byte-exactly (modes, symlinks) into a temp directory, which is
bind-mounted at `/work` as the working directory:

    docker run --rm --name gud-test-<hex> --network none -v <tmp>:/work -w /work
               [--user <uid>:<gid>] <image> /bin/sh -c '<command> <args>'

On timeout the container is killed (`docker kill`) and the local docker client's process group
too. A policy with no `image` runs on the fallback runner (local by default), as §10.4 says.
Network is off by default (`network="none"`); pass `network="bridge"` for suites that fetch.

This runner was written against the docker CLI reference; this machine has no docker, so it has
only been exercised through `argv()`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from gud.core.types import Mode

from .base import RunResult, RunnerUnavailable, child_env, materialize, result_log, run_process, shell_command
from .local import LocalRunner


class DockerRunner:
    name = "docker"

    def __init__(self, *, docker: str = "docker", network: str | None = "none", user: str | None = "auto",
                 fallback: object | None = None, extra_args: Sequence[str] = (), tmp_root: Path | None = None) -> None:
        self.docker = docker
        self.network = network
        self.user = user
        self.fallback = fallback if fallback is not None else LocalRunner()
        self.docker_args = tuple(extra_args)
        self.tmp_root = Path(tmp_root) if tmp_root is not None else None

    def available(self) -> bool:
        return shutil.which(self.docker) is not None

    def argv(self, workdir: Path, command: str, image: str, name: str, extra_args: Sequence[str] = ()) -> list[str]:
        out = [self.docker, "run", "--rm", "--name", name, "-v", f"{workdir}:/work", "-w", "/work"]
        if self.network:
            out += ["--network", self.network]
        user = self.user
        if user == "auto":
            user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else None
        if user:
            out += ["--user", user]
        out += list(self.docker_args)
        out += [image, "/bin/sh", "-c", shell_command(command, extra_args)]
        return out

    def run(self, files: Mapping[str, tuple[Mode, bytes]], command: str, *, image: str | None,
            timeout_ms: int, extra_args: Sequence[str] = ()) -> RunResult:
        if not image:
            return self.fallback.run(files, command, image=None, timeout_ms=timeout_ms,  # type: ignore[attr-defined]
                                     extra_args=extra_args)
        if not self.available():
            raise RunnerUnavailable(f"docker runner: {self.docker!r} not found on PATH")
        name = "gud-test-" + uuid.uuid4().hex[:16]

        def kill() -> None:
            subprocess.run([self.docker, "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=False)

        with tempfile.TemporaryDirectory(prefix="gud-test-", dir=self.tmp_root) as tmp:
            root = Path(tmp) / "work"
            root.mkdir()
            materialize(root, files)
            code, out, timed_out, seconds = run_process(
                self.argv(root.resolve(), command, image, name, extra_args), cwd=root, timeout_ms=timeout_ms,
                env=child_env(), on_timeout=kill)
        log = result_log(out, timed_out=timed_out, timeout_ms=timeout_ms, code=code, note=f"image {image}")
        return RunResult(passed=(code == 0 and not timed_out), log=log, seconds=seconds, runner=self.name,
                         timed_out=timed_out, exit_code=code)
