"""End-to-end demo. Slowish: a scripted swarm, real pytest replies, a 2s consent window."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_demo_fast(tmp_path):
    env = os.environ.copy()
    env["GUD_HOME"] = str(tmp_path / "home")
    env["GUD_MAILBOX"] = "fake"
    env["GUD_RUNNER"] = "local"
    env["GUD_WINDOW_SECONDS"] = "2"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo.py"), "--fast"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=70,
    )
    output = proc.stdout + "\n" + proc.stderr
    assert proc.returncode == 0, output
    for phrase in ("keep the rate-limit check", "bad_sig", "Scorecard", "demo finished"):
        assert phrase in proc.stdout, phrase + "\n" + output
