"""Run a local GreenMail (SMTP + IMAP) server for the imap_smtp conformance tests.

GreenMail standalone takes its setup from system properties: `greenmail.<proto>.port` and
`greenmail.<proto>.hostname` per protocol (do not combine them with `greenmail.setup.test.*`,
which defines the same servers again), `greenmail.api.port`/`greenmail.api.hostname` for its
REST API, and `greenmail.users=login:password@domain,...`.

The jar is found through `GUD_GREENMAIL_JAR`, else `.tools/greenmail-standalone.jar` in this
checkout or any parent directory (a git worktree lives under the main checkout). Tests skip
cleanly when java or the jar is missing.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

JAR_NAME = "greenmail-standalone.jar"
DOMAIN = "gud.test"
PASSWORD = "pw"
USERS = ("repo", "alice", "bob", "human", "box1", "box2")


def find_jar() -> Path | None:
    env = os.environ.get("GUD_GREENMAIL_JAR")
    if env:
        return Path(env) if Path(env).is_file() else None
    for parent in Path(__file__).resolve().parents:
        cand = parent / ".tools" / JAR_NAME
        if cand.is_file():
            return cand
    return None


def find_java() -> str | None:
    for cand in (os.environ.get("GUD_JAVA"), shutil.which("java"), "/usr/bin/java"):
        if cand and Path(cand).exists():
            try:
                ok = subprocess.run([cand, "-version"], capture_output=True, timeout=20).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            if ok:
                return cand
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _banner(port: int, timeout: float = 1.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        return s.recv(256)


@dataclass
class GreenMail:
    host: str
    smtp_port: int
    imap_port: int
    api_port: int
    proc: subprocess.Popen
    log: Path

    def address(self, user: str) -> str:
        return f"{user}@{DOMAIN}"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def start(log_dir: Path, *, startup_timeout: float = 60.0) -> GreenMail:
    jar, java = find_jar(), find_java()
    if jar is None or java is None:
        pytest.skip(f"GreenMail needs java and {JAR_NAME} (java={java}, jar={jar})")
    smtp, imap, api = _free_port(), _free_port(), _free_port()
    users = ",".join(f"{u}:{PASSWORD}@{DOMAIN}" for u in USERS)
    log = Path(log_dir) / "greenmail.log"
    cmd = [
        java, "-Xmx1g",
        f"-Dgreenmail.smtp.port={smtp}", "-Dgreenmail.smtp.hostname=127.0.0.1",
        f"-Dgreenmail.imap.port={imap}", "-Dgreenmail.imap.hostname=127.0.0.1",
        f"-Dgreenmail.api.port={api}", "-Dgreenmail.api.hostname=127.0.0.1",
        f"-Dgreenmail.users={users}",
        "-jar", str(jar),
    ]
    fh = open(log, "wb")
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + startup_timeout
    while True:
        if proc.poll() is not None:
            fh.close()
            raise RuntimeError(f"GreenMail exited with {proc.returncode}: {log.read_text()[-2000:]}")
        try:
            if _banner(smtp).startswith(b"220") and _banner(imap).startswith(b"* OK"):
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            proc.kill()
            raise RuntimeError(f"GreenMail did not start in {startup_timeout}s: {log.read_text()[-2000:]}")
        time.sleep(0.2)
    return GreenMail("127.0.0.1", smtp, imap, api, proc, log)
