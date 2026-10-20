"""`LocalRunner`: materialise the tree into a fresh temp directory on this machine and run the
`.gud/test` command there with `/bin/sh -c`, in its own process group, killed at the timeout.

The image named in `.gud/test` is not used (§10.4: no image means the local runner); a run with an
image says so in its log. Use `DockerRunner` to honour it.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Mapping, Sequence

from gud.core.types import Mode

from .base import RunResult, child_env, materialize, result_log, run_process, shell_command


class LocalRunner:
    name = "local"

    def __init__(self, *, shell: str = "/bin/sh", extra_path: Sequence[str] = (),
                 tmp_root: Path | None = None) -> None:
        self.shell = shell
        self.extra_path = tuple(extra_path)
        self.tmp_root = Path(tmp_root) if tmp_root is not None else None
        self._lock = threading.Lock()
        self.runs = 0                                   # how many commands this runner executed

    def run(self, files: Mapping[str, tuple[Mode, bytes]], command: str, *, image: str | None = None,
            timeout_ms: int, extra_args: Sequence[str] = ()) -> RunResult:
        with self._lock:
            self.runs += 1
        note = f"image {image} ignored by the local runner" if image else ""
        with tempfile.TemporaryDirectory(prefix="gud-test-", dir=self.tmp_root) as tmp:
            root = Path(tmp) / "work"
            root.mkdir()
            materialize(root, files)
            code, out, timed_out, seconds = run_process(
                [self.shell, "-c", shell_command(command, extra_args)], cwd=root, timeout_ms=timeout_ms,
                env=child_env(self.extra_path))
        log = result_log(out, timed_out=timed_out, timeout_ms=timeout_ms, code=code, note=note)
        return RunResult(passed=(code == 0 and not timed_out), log=log, seconds=seconds, runner=self.name,
                         timed_out=timed_out, exit_code=code)
