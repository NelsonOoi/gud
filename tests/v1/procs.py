"""Subprocess helpers for the CLI and end-to-end tests: run `gud` (python -m gud.cli) with this
checkout on PYTHONPATH, and run long-lived `gud host serve` / `gud tester` processes."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import httpx

ROOT = Path(__file__).resolve().parents[2]


def env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    out = {k: v for k, v in os.environ.items() if k not in ("GUD_HOME", "GUD_HANDLE", "GUD_TRANSPORT")}
    out["PYTHONPATH"] = str(ROOT)
    out.setdefault("GUD_WAIT", "10")
    out.update(extra or {})
    return out


def argv(home: Path, *args: str, handle: str | None = None, json_out: bool = True) -> list[str]:
    a = [sys.executable, "-m", "gud.cli", "--home", str(home)] + (["--json"] if json_out else [])
    if handle:
        a += ["--handle", handle]
    return a + list(args)


def gud(home: Path, *args: str, cwd: Path | None = None, code: int | None = 0, handle: str | None = None,
        timeout: float = 180, extra_env: Mapping[str, str] | None = None) -> dict:
    """Run one gud command with --json; assert its exit code (unless None); return its JSON."""
    Path(home).mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(argv(home, *args, handle=handle), cwd=cwd or home, capture_output=True, text=True,
                          env=env(extra_env), timeout=timeout)
    if code is not None:
        assert proc.returncode == code, f"gud {' '.join(args)} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert lines, f"no JSON output from gud {' '.join(args)}: {proc.stderr}"
    out = json.loads(lines[-1])
    out.setdefault("_code", proc.returncode)
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Proc:
    """A long-running gud subprocess (host serve, tester) with its output in a log file."""

    def __init__(self, home: Path, *args: str, log: Path, handle: str | None = None,
                 extra_env: Mapping[str, str] | None = None, cwd: Path | None = None) -> None:
        self.log = Path(log)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.log, "wb")
        Path(home).mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(argv(home, *args, handle=handle), cwd=cwd or home, stdout=self._fh,
                                     stderr=subprocess.STDOUT, env=env(extra_env))

    def alive(self) -> bool:
        return self.proc.poll() is None

    def output(self) -> str:
        self._fh.flush()
        return self.log.read_text(errors="replace")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._fh.close()


def serve(home: Path, host_dir: Path, log: Path, *, port: int | None = None,
          extra_env: Mapping[str, str] | None = None, timeout: float = 60) -> tuple[Proc, str]:
    """`gud host serve --dir host_dir` on a free port; returns (process, base URL) once /info answers."""
    port = port or free_port()
    proc = Proc(home, "host", "serve", "--dir", str(host_dir), "--bind", f"127.0.0.1:{port}", log=log,
                extra_env=extra_env)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    while True:
        if not proc.alive():
            raise RuntimeError(f"host serve exited: {proc.output()[-3000:]}")
        try:
            if httpx.get(url + "/api/v1/info", timeout=2).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            proc.stop()
            raise RuntimeError(f"host serve did not answer in {timeout}s: {proc.output()[-3000:]}")
        time.sleep(0.1)


def wait_for(pred, *, timeout: float = 60, interval: float = 0.25, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last: {last!r})")
