"""Aggregate v1 eval runs into markdown tables: mean (min–max) per (arm, spec, N), like livetest/compare.py.

    .venv/bin/python evals/v1/report.py evals/v1/runs/20261020-120000 [more dirs or summary.json files]

Every summary.json found under the given paths is one trial. Columns group trials by spec and N,
with one column per arm.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(paths: list[Path]) -> list[dict]:
    out = []
    for path in paths:
        path = Path(path)
        files = [path] if path.is_file() else sorted(path.rglob("summary.json"))
        for file in files:
            try:
                data = json.loads(file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if "arm" in data and "spec" in data:
                data["_path"] = str(file)
                out.append(data)
    return out


def _nums(values) -> list[float]:
    return [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def span(values, fmt=lambda v: f"{v:.1f}") -> str:
    nums = _nums(values)
    if not nums:
        return "—"
    mean = sum(nums) / len(nums)
    if len(nums) == 1:
        return fmt(mean)
    return f"{fmt(mean)} ({fmt(min(nums))}–{fmt(max(nums))})"


def _count(v: float) -> str:
    return str(int(round(v))) if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def _money(v: float) -> str:
    return f"${v:.2f}"


def _pct(v: float) -> str:
    return f"{100 * v:.0f}%"


def _mix(values) -> str:
    counts: dict[str, int] = {}
    for v in values:
        key = str(v)
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{k} ×{n}" for k, n in counts.items()) or "—"


def _agent(rows, fn):
    """Per-agent values pooled across trials."""
    return [fn(agent) for row in rows for agent in row.get("agents") or []]


ROWS = [
    ("trials", lambda rows: str(len(rows))),
    ("outcome", lambda rows: _mix(r.get("outcome") for r in rows)),
    ("hidden-test pass rate", lambda rows: span([r.get("hidden", {}).get("pass_rate") for r in rows], _pct)),
    ("hidden tests passed", lambda rows: span([r.get("hidden", {}).get("passed") for r in rows], _count)
     + f" / {rows[0].get('hidden', {}).get('total', '?')}"),
    ("repo's own tests pass on final tree", lambda rows: _mix(r.get("final_tests_pass") for r in rows)),
    ("wall time", lambda rows: span([r.get("wall_s") for r in rows], lambda v: f"{v:.0f} s")),
    ("cost (run)", lambda rows: span([r.get("cost_usd") for r in rows], _money)),
    ("cost per agent", lambda rows: span(_agent(rows, lambda a: a.get("cost_usd")), _money)),
    ("tokens per agent (all kinds)", lambda rows: span(_agent(rows, lambda a: None if "ledger" in (a.get("tokens") or {})
                                                              else sum((a.get("tokens") or {}).values())),
                                                       lambda v: f"{v:,.0f}")),
    ("ledger tokens per agent (gud-ref)", lambda rows: span(_agent(rows, lambda a: (a.get("tokens") or {}).get("ledger")),
                                                             lambda v: f"{v:,.0f}")),
    ("output tokens per agent", lambda rows: span(_agent(rows, lambda a: (a.get("tokens") or {}).get("output")),
                                                  lambda v: f"{v:,.0f}")),
    ("turns per agent", lambda rows: span(_agent(rows, lambda a: a.get("turns")), _count)),
    ("agent wall time", lambda rows: span(_agent(rows, lambda a: a.get("wall_s")), lambda v: f"{v:.0f} s")),
    ("agents killed (budget/timeout)", lambda rows: span([len(r.get("killed") or {}) for r in rows], _count)),
    ("integrations (links / merges)", lambda rows: span([r.get("integrations") for r in rows], _count)),
    ("integrations per min", lambda rows: span([r.get("integrations_per_min") for r in rows], lambda v: f"{v:.2f}")),
    ("test runs (tester / merge gate)", lambda rows: span([r.get("test_runs") for r in rows], _count)),
    ("test runs per integration", lambda rows: span([r.get("test_runs_per_integration") for r in rows],
                                                    lambda v: f"{v:.2f}")),
    ("agent-run local test runs", lambda rows: span([r.get("local_test_runs") for r in rows], _count)),
    ("conflicts", lambda rows: span([r.get("conflicts_total") for r in rows], _count)),
    ("conflicts resolved", lambda rows: span([r.get("conflicts_resolved") for r in rows], _count)),
    ("escalations", lambda rows: span([r.get("escalations") for r in rows], _count)),
    ("PRs opened (git)", lambda rows: span([(r.get("git") or {}).get("prs_opened") for r in rows], _count)),
    ("merge rejections (git)", lambda rows: span([sum(((r.get("git") or {}).get("merge_rejections") or {}).values())
                                                  for r in rows if r.get("git")], _count)),
    ("hidden-test access attempts", lambda rows: span([len(r.get("hidden_access") or []) for r in rows], _count)),
]


def gud_block(r: dict) -> dict | None:
    """The gud arms' replay metrics of one run. Runs written before the clean break call the block `gud1`
    (arms `gud1-cc`/`gud1-ref`); a `gud` block in such an old run belongs to the deleted v0 arm and is skipped."""
    if r.get("gud1"):
        return r["gud1"]
    return r.get("gud") if str(r.get("arm", "")).startswith("gud-") else None


def _g1(rows, fn):
    """Values from the gud block of the runs that have one."""
    out = []
    for r in rows:
        g = gud_block(r)
        if g:
            try:
                out.append(fn(g))
            except (KeyError, TypeError):
                out.append(None)
    return out


def _ana(rows, key):
    return [((r.get("analysis") or {}).get("summary") or {}).get(key) for r in rows]


def _mix_g1(rows, fn):
    vals = _g1(rows, fn)
    return _mix(vals) if vals else "—"


ROWS_V1 = [
    ("links per min (gud)", lambda rows: span(_g1(rows, lambda g: g["links_per_min"]), lambda v: f"{v:.2f}")),
    ("test runs per link (gud)", lambda rows: span(_g1(rows, lambda g: g["test_runs_per_link"]), lambda v: f"{v:.2f}")),
    ("pushes (gud)", lambda rows: span(_g1(rows, lambda g: g["pushes"]), _count)),
    ("carried links (gud)", lambda rows: span(_g1(rows, lambda g: g["carried_links"]), _count)),
    ("superseded pushes (gud)", lambda rows: span(_g1(rows, lambda g: g["superseded"]), _count)),
    ("colliding pushes (gud)", lambda rows: span(_g1(rows, lambda g: g["colliding_pushes"]), _count)),
    ("pushes refused before sending: stale base (gud)", lambda rows: span(_ana(rows, "stale_base_refusals"), _count)),
    ("claims won (gud)", lambda rows: span(_g1(rows, lambda g: g["claims"]["won"]), _count)),
    ("claim updates / releases (gud)", lambda rows: span(_g1(rows, lambda g: g["claims"]["updates"]), _count)
     + " / " + span(_g1(rows, lambda g: g["claims"]["releases"]), _count)),
    ("subtasks posted (gud)", lambda rows: span(_g1(rows, lambda g: g["task"]["subtasks"]), _count)),
    ("claim overlaps (gud)", lambda rows: span(_g1(rows, lambda g: g["overlaps"]["total"]), _count)),
    ("overlap settlement (gud)", lambda rows: ", ".join(
        f"{k} ×{v}" for k, v in sorted(_sum_dicts(_g1(rows, lambda g: g["overlaps"]["by_settlement"])).items())) or "—"),
    ("comments (gud)", lambda rows: span(_g1(rows, lambda g: g["comments"]["total"]), _count)),
    ("objections reason / evidence (gud)", lambda rows: span(_g1(rows, lambda g: g["objections"]["reason"]), _count)
     + " / " + span(_g1(rows, lambda g: g["objections"]["evidence"]), _count)),
    ("consent escalations (gud)", lambda rows: span(_g1(rows, lambda g: len(g["escalations"]["consent"])), _count)),
    ("escalations resolved by (gud)", lambda rows: ", ".join(
        f"{k} ×{v}" for k, v in sorted(_sum_dicts(_g1(rows, lambda g: g["escalations"]["resolved_by"])).items())) or "—"),
    ("task escalations (gud)", lambda rows: span(_g1(rows, lambda g: len(g["escalations"]["task"])), _count)),
    ("red-tip episodes (gud)", lambda rows: span(_g1(rows, lambda g: g["red_tip"]["episodes"]), _count)),
    ("median time to green (gud)", lambda rows: span(_g1(rows, lambda g: g["red_tip"]["median_to_green_s"]),
                                                      lambda v: f"{v:.0f} s")),
    ("red at end (gud)", lambda rows: _mix_g1(rows, lambda g: g["red_tip"]["red_at_end"])),
    ("T1 status at end (gud)", lambda rows: _mix_g1(rows, lambda g: g["task"]["status"])),
    ("files read per agent", lambda rows: span(_ana(rows, "files_read_per_agent"), lambda v: f"{v:.1f}")),
    ("reads outside own changes per agent", lambda rows: span(_ana(rows, "reads_outside_own_changes_per_agent"),
                                                               lambda v: f"{v:.1f}")),
    ("reads outside claimed scope per agent (gud)", lambda rows: span(_ana(rows, "reads_outside_claims_per_agent"),
                                                                        lambda v: f"{v:.1f}")),
    ("colliding pushes with inflight before (gud)", lambda rows: span(_ana(rows, "colliding_with_inflight_before"), _count)
     + " of " + span(_ana(rows, "colliding_pushes"), _count)),
    ("colliding pushes with comment before (gud)", lambda rows: span(_ana(rows, "colliding_with_comment_before"), _count)),
    ("inflight / comment calls (gud)", lambda rows: span(_ana(rows, "inflight_calls"), _count) + " / "
     + span(_ana(rows, "comment_calls"), _count)),
    ("guard file read by an agent (context check)", lambda rows: _mix([bool((r.get("context_check") or {}).get("read_by"))
                                                                     for r in rows if r.get("context_check")]) if any(
        r.get("context_check") for r in rows) else "—"),
    ("guard tests passed (context check)", lambda rows: span([sum(1 for v in (r.get("context_check") or {}).get(
        "guard_tests_passed", {}).values() if v) for r in rows if r.get("context_check")], _count) if any(
        r.get("context_check") for r in rows) else "—"),
]


def _sum_dicts(dicts) -> dict:
    out: dict = {}
    for d in dicts:
        for k, v in (d or {}).items():
            out[k] = out.get(k, 0) + v
    return out

EXCLUDED = ("harness_error", "skipped_cap")

NOTES = [
    "Each cell is mean (min–max) over trials; per-agent rows pool every agent in every trial.",
    "Integrations are gud links (excluding genesis) or git PR merges into main.",
    "Test runs are the gud tester's replies or the git merge gate's pytest runs; both run `python -m pytest -q -x`.",
    "gud conflicts are pushes that ended superseded, stale, conflict or objected; git conflicts are merge-gate "
    "rebase conflicts plus `CONFLICT` lines in agents' own git output. Resolved means the same author landed later.",
    "Escalations are final messages containing `ESCALATE:`. Cost is Claude Code's reported total_cost_usd "
    "(list price), or an estimate from usage for a killed agent.",
]


def report(paths: list[Path]) -> str:
    every = load(paths)
    if not every:
        return "(no summary.json found)"
    left_out = [r for r in every if r.get("outcome") in EXCLUDED]
    rows = [r for r in every if r.get("outcome") not in EXCLUDED]
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row["spec"], row["agents_n"], row["arm"]), []).append(row)
    keys = sorted(groups, key=lambda k: (k[0], k[1], k[2]))
    lines = []
    for spec, n in sorted({(k[0], k[1]) for k in keys}):
        cols = [k for k in keys if k[0] == spec and k[1] == n]
        lines.append(f"### {spec}, N={n}")
        lines.append("")
        lines.append("| | " + " | ".join(f"{k[2]}" for k in cols) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in cols) + " |")
        v1 = any(gud_block(r) or r.get("analysis") for k in cols for r in groups[k])
        for title, fn in ROWS + (ROWS_V1 if v1 else []):
            lines.append(f"| {title} | " + " | ".join(fn(groups[k]) for k in cols) + " |")
        lines.append("")
        models = sorted({str(r.get("model")) for k in cols for r in groups[k]})
        lines.append(f"Model: {', '.join(models)}. Total spend in this table: "
                     f"${sum(r.get('cost_usd') or 0 for k in cols for r in groups[k]):.2f}.")
        lines.append("")
    lines.extend(f"- {note}" for note in NOTES)
    if left_out:
        lines.append("")
        lines.append("Runs left out of the tables (not agent results):")
        for r in left_out:
            lines.append(f"- {r['arm']} {r['spec']} n{r['agents_n']} t{r['trial']}: {r.get('outcome')} "
                         f"({'; '.join(map(str, r.get('harness_errors') or [])) or '-'}), ${r.get('cost_usd') or 0:.2f}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path)
    args = ap.parse_args(argv)
    print(report(args.paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
