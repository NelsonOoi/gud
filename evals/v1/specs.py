"""Task specs and their hidden acceptance tests.

A spec is ``specs/<name>/spec.md`` (the task text every agent gets) plus ``specs/<name>/seed/`` (the
starting repo: a README stub and one trivial passing test). The hidden tests live in
``hidden/<name>/test_hidden.py``, outside anything copied into an agent's workspace, and run only
after the agents stop.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPECS = HERE / "specs"
HIDDEN = HERE / "hidden"
SKIP = {".git", "__pycache__", ".pytest_cache", "node_modules"}
# The test command every arm uses: the gud tester (gud.build.LocalRunner), the git merge gate,
# and the "final tests" check on the finished tree.
TEST_ARGS = ("-m", "pytest", "-q", "-x")


@dataclass(frozen=True)
class Spec:
    name: str
    title: str
    text: str
    seed: Path
    hidden: Path


def names() -> list[str]:
    return sorted(p.name for p in SPECS.iterdir() if (p / "spec.md").is_file())


def load(name: str) -> Spec:
    root = SPECS / name
    if not (root / "spec.md").is_file():
        raise SystemExit(f"unknown spec {name!r}; have {', '.join(names())}")
    text = (root / "spec.md").read_text().strip()
    first = text.splitlines()[0]
    title = first.lstrip("#").strip()
    body = "\n".join(text.splitlines()[1:]).strip()
    return Spec(name, title, body, root / "seed", HIDDEN / name)


def read_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or SKIP & set(rel.parts) or path.suffix == ".pyc":
            continue
        try:
            out[rel.as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return out


def write_tree(root: Path, tree: dict[str, str]) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in tree.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def clean_env() -> dict[str, str]:
    """Environment for running a tree's tests: no PYTHONPATH or pytest settings leaking in."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST_", "GUD_")) and k not in {"PYTHONPATH", "TODO_FILE"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_visible_tests(tree_dir: Path, timeout: float = 180) -> dict:
    """The repo's own suite (what agents could run), with the same command the tester uses."""
    with tempfile.TemporaryDirectory(prefix="gudv1-final-") as tmp:
        shutil.copytree(tree_dir, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*SKIP))
        try:
            proc = subprocess.run([sys.executable, *TEST_ARGS], cwd=tmp, env=clean_env(),
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"passed": False, "rc": None, "tail": "timeout"}
    return {"passed": proc.returncode == 0, "rc": proc.returncode,
            "tail": (proc.stdout + proc.stderr)[-2000:]}


def run_hidden(spec: Spec, tree_dir: Path, timeout: float = 300) -> dict:
    """Run the hidden tests against a copy of ``tree_dir``. Returns pass counts and per-test results."""
    tests = sorted(spec.hidden.glob("test_*.py"))
    if not tests:
        raise SystemExit(f"no hidden tests for {spec.name} in {spec.hidden}")
    with tempfile.TemporaryDirectory(prefix="gudv1-hidden-") as tmp:
        work = Path(tmp) / "tree"
        shutil.copytree(tree_dir, work, ignore=shutil.ignore_patterns(*SKIP))
        suite = Path(tmp) / "hidden"
        suite.mkdir()
        for path in tests:
            shutil.copy(path, suite / path.name)
        report = Path(tmp) / "report.xml"
        env = clean_env()
        env["EVAL_TREE"] = str(work)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(suite),
                 "-c", os.devnull, f"--junitxml={report}", str(suite)],
                cwd=suite, env=env, capture_output=True, text=True, timeout=timeout,
            )
            tail = (proc.stdout + proc.stderr)[-3000:]
        except subprocess.TimeoutExpired:
            return {"passed": 0, "failed": 0, "total": 0, "pass_rate": 0.0, "tests": {}, "tail": "timeout"}
        results = _junit(report) if report.exists() else {}
    passed = sum(1 for ok in results.values() if ok)
    total = len(results)
    return {
        "passed": passed, "failed": total - passed, "total": total,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "tests": results, "tail": tail,
    }


def _junit(path: Path) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.get("name") or "?"
        failed = any(child.tag in {"failure", "error", "skipped"} for child in case)
        out[name] = not failed
    return out
