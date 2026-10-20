import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_build_runs(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "build.py"), str(tmp_path)], check=True)
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "styles.css").is_file()
