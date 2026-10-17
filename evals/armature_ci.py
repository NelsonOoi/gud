"""Trigger Armature CI runs and print a scorecard.

Workflow ids live in evals/armature_cases.json. Create the workflows in the
Armature dashboard first (see the comment in that file), then:

    python evals/armature_ci.py
    python evals/armature_ci.py --case parallel-functions

Uses ARMATURE_API_KEY from the environment or from gud.config (the repo .env).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES = Path(__file__).resolve().parent / "armature_cases.json"
API = "https://app.armature.tech/api/armature/v1/workflows"
TERMINAL = {"completed", "complete", "succeeded", "success", "failed", "failure", "error", "cancelled", "canceled"}


def _key() -> str:
    import os

    key = os.environ.get("ARMATURE_API_KEY", "").strip()
    if key:
        return key
    from gud.config import load
    found = load().armature_api_key
    if not found:
        raise SystemExit("ARMATURE_API_KEY is not set")
    return found


def _id_of(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    for field in ("id", "run_id", "runId"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    for field in ("run", "data", "result"):
        nested = payload.get(field)
        if isinstance(nested, dict):
            found = _id_of(nested)
            if found:
                return found
    return None


def _status_of(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for field in ("status", "state", "phase"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    for field in ("run", "data", "result"):
        nested = payload.get(field)
        if isinstance(nested, dict):
            found = _status_of(nested)
            if found:
                return found
    return ""


def _score_of(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    for field in ("score", "overall_score", "overallScore", "grade"):
        value = payload.get(field)
        if isinstance(value, (int, float, str)):
            return str(value)
    evaluation = payload.get("evaluation") or payload.get("result") or payload.get("data")
    if isinstance(evaluation, dict) and evaluation is not payload:
        return _score_of(evaluation)
    return ""


def _digest(payload) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:160]
    kept = {}
    for field in ("status", "state", "score", "passed", "summary", "name", "id"):
        if field in payload and not isinstance(payload[field], (dict, list)):
            kept[field] = payload[field]
    metrics = payload.get("metrics") or payload.get("scores")
    if isinstance(metrics, dict):
        kept["metrics"] = {key: metrics[key] for key in list(metrics)[:6]}
    text = json.dumps(kept, default=str) if kept else json.dumps(payload, default=str)[:240]
    return text[:240]


def trigger(client: httpx.Client, workflow_id: str) -> str:
    response = client.post(f"{API}/{workflow_id}/runs", json={})
    if response.status_code >= 300:
        raise RuntimeError(f"POST run {response.status_code}: {response.text[:300]}")
    payload = response.json()
    run_id = _id_of(payload)
    if not run_id:
        raise RuntimeError(f"no run id in {json.dumps(payload)[:300]}")
    return run_id


def poll(client: httpx.Client, workflow_id: str, run_id: str, timeout: float) -> tuple[dict, dict]:
    deadline = time.time() + timeout
    run: dict = {}
    while time.time() < deadline:
        response = client.get(f"{API}/{workflow_id}/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if _status_of(run).lower() in TERMINAL:
            break
        time.sleep(2)
    else:
        run = {**(run or {}), "status": _status_of(run) or "timeout"}
    evaluation: dict = {}
    try:
        got = client.get(f"{API}/{workflow_id}/runs/{run_id}/evaluation")
        if got.status_code < 300:
            body = got.json()
            evaluation = body if isinstance(body, dict) else {"result": body}
    except Exception as exc:
        evaluation = {"error": str(exc)}
    return run, evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Armature CI for gud eval cases")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    spec = json.loads(CASES.read_text())
    chosen = spec.get("cases") or []
    if args.case:
        chosen = [item for item in chosen if item.get("case_id") in args.case]
        missing = [item for item in args.case if item not in {row.get("case_id") for row in chosen}]
        if missing:
            raise SystemExit(f"unknown case: {', '.join(missing)}")
    key = _key()
    headers = {"Authorization": f"Bearer {key}"}
    rows = []
    with httpx.Client(headers=headers, timeout=60) as client:
        for item in chosen:
            case_id, workflow_id = item["case_id"], str(item.get("workflow_id") or "")
            if not workflow_id or workflow_id.startswith("REPLACE"):
                rows.append({"case": case_id, "workflow": workflow_id or "-", "run": "-", "status": "skipped", "score": "-", "detail": "placeholder workflow id"})
                continue
            try:
                run_id = trigger(client, workflow_id)
                run, evaluation = poll(client, workflow_id, run_id, args.timeout)
                status = _status_of(run) or "unknown"
                score = _score_of(evaluation) or _score_of(run) or "-"
                detail = _digest(evaluation) if evaluation else _digest(run)
                rows.append({"case": case_id, "workflow": workflow_id, "run": run_id, "status": status, "score": score, "detail": detail})
            except Exception as exc:
                rows.append({"case": case_id, "workflow": workflow_id, "run": "-", "status": "error", "score": "-", "detail": str(exc)[:200]})
    header = f"{'case':<24} {'status':<12} {'score':<8} run"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['case']:<24} {row['status']:<12} {str(row['score']):<8} {row['run']}")
        print(f"  {row['detail']}")
    failed = [row for row in rows if row["status"].lower() in {"failed", "failure", "error", "timeout"}]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
