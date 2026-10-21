"""Run a grid of eval cells (spec × arm × trial) under one hard spending cap.

    .venv/bin/python evals/v1/grid.py --specs acme-site-shared todo-stack refactor-guard \\
        --arms git gud-cc gud-ref --agents 4 --trials 2 --budget-usd 1.2 --cap-usd 30 \\
        --out evals/v1/runs/<stamp>-v1 --parallel 3

Each cell is one `run.py` process (one arm, one spec, one trial), so a crash in one cell cannot take the
others down. Every cell passes the same `--cap-usd` and `--ledger`, and `run.py` reserves its budget from
the ledger before it starts (see cap.py), so the cap holds across concurrent cells: a cell that would
exceed it gets a smaller budget, or is skipped (`outcome: skipped_cap`). Cells run in trial order, then
spec, then arm, `--parallel` at a time. A cell whose summary already exists in `--out` is not rerun
unless its outcome is `harness_error` and `--rerun-errors` is given.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--specs", nargs="+", required=True)
    ap.add_argument("--arms", nargs="+", required=True)
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--budget-usd", type=float, required=True)
    ap.add_argument("--cap-usd", type=float, required=True)
    ap.add_argument("--ledger", type=Path, default=HERE / "runs" / "v1-spend.json")
    ap.add_argument("--timeout", type=float, default=1200)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--rerun-errors", action="store_true")
    ap.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="more run.py arguments")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    cells = [(t, s, a) for t in range(1, args.trials + 1) for s in args.specs for a in args.arms]
    env = {**os.environ, "PYTHONPATH": str(REPO)}

    def run_cell(cell):
        trial, spec, arm = cell
        name = f"{arm}-{spec}-n{args.agents}-t{trial}"
        summary = args.out / name / "summary.json"
        if summary.exists():
            outcome = json.loads(summary.read_text()).get("outcome")
            if outcome != "harness_error" or not args.rerun_errors:
                return name, f"exists ({outcome})"
        argv = [sys.executable, str(HERE / "run.py"), "--arm", arm, "--spec", spec, "--agents", str(args.agents),
                "--trials", "1", "--trial-start", str(trial), "--budget-usd", str(args.budget_usd),
                "--timeout", str(args.timeout), "--out", str(args.out), "--cap-usd", str(args.cap_usd),
                "--ledger", str(args.ledger)] + list(args.extra)
        log = args.out / f"{name}.log"
        t0 = time.time()
        with log.open("w") as fh:
            code = subprocess.call(argv, cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT)
        outcome = json.loads(summary.read_text()).get("outcome") if summary.exists() else "no summary"
        line = f"{name}: exit {code}, {outcome}, {time.time() - t0:.0f}s"
        print(line, flush=True)
        return name, line

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        results = list(pool.map(run_cell, cells))
    for name, line in results:
        print(f"  {name}: {line}")
    sys.path.insert(0, str(HERE))
    from cap import CapLedger
    from report import report
    print(report([args.out]))
    print(f"spend: {CapLedger(args.ledger, args.cap_usd).state()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
