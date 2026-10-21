"""The gud v1 arms with no model: gud-cc (fake Claude Code on gud.mcp) and gud-ref (the real reference
agent runtime with fake sessions), the replay and transcript analysis, and the spending cap."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import report
import run
import specs

FAKE = Path(__file__).resolve().parent / "fake_agent.py"
AGENT_CMD = f"{sys.executable} {FAKE}"
REF_CMD = [sys.executable, str(Path(__file__).resolve().parent / "fake_refagent.py")]


@pytest.fixture
def fake_env(monkeypatch):
    spec = specs.load("acme-site")
    monkeypatch.setenv("FAKE_FILES_DIR", str(spec.hidden / "reference"))
    monkeypatch.setenv("FAKE_AGENTS", "2")
    monkeypatch.setenv("FAKE_CONFLICT", "1")
    monkeypatch.setenv("FAKE_DEADLINE", "90")


@pytest.mark.parametrize("arm", ["gud-cc", "gud-ref"])
def test_gud_arm_end_to_end_with_fake_agents(arm, tmp_path, fake_env):
    out = tmp_path / "out"
    summary = run.run_one(arm_name=arm, spec_name="acme-site", n=2, trial=1, budget=5.0, timeout=150,
                          out=out, agent_cmd=AGENT_CMD, model="claude-sonnet-5-5", window=3.0,
                          keep_work=False, work_root=tmp_path, ref_cmd=REF_CMD, ref_settle=6.0, drain_s=40)
    assert summary["outcome"] == "completed", summary.get("harness_errors")
    assert summary["hidden"]["pass_rate"] == 1.0, summary["spec_checks"]
    assert summary["final_tests_pass"] is True
    assert ".gud/members" not in summary["final_files"] and ".gudignore" in summary["final_files"]
    assert summary["integrations"] >= 2
    g = summary["gud"]
    assert g["links"] == summary["integrations"] and g["links_per_min"] > 0
    # both agents touch README.md from the same base: the later push is superseded (both were sent before
    # either linked) or refused by the working copy before sending (sync first); either way it is pushed again
    ana = summary["analysis"]
    raced = g["superseded"] >= 1
    assert raced or ana["summary"]["stale_base_refusals"] >= 1, (g["push_list"], ana["summary"])
    if raced:
        assert g["colliding_pushes"] >= 2, g["push_list"]
        assert summary["conflicts_total"] >= 1
    assert summary["conflicts_resolved"] == summary["conflicts_total"]
    # one claim on T1 wins; the other agent's claim is refused by the tool (T1 is held) and never sent
    assert g["claims"]["won"] == 1
    assert g["task"]["alias"] == "T1" and g["task"]["status"] in ("work_done", "done")
    # tip policy: the tester tests tips after links (no push-scope gating), and the acceptance check
    assert g["test_runs_by_scope"].get("tip", 0) >= 1 and "push" not in g["test_runs_by_scope"]
    assert g["test_runs"] == summary["test_runs"] and g["test_runs_per_link"] is not None
    assert g["red_tip"]["episodes"] == 0 and g["red_tip"]["red_at_end"] is False
    assert g["emails_by_kind"]["task"] == 1 and g["emails_by_kind"]["claim"] >= 1
    readme = (out / f"{arm}-acme-site-n2-t1" / "final_tree" / "README.md").read_text()
    assert "Owner note from @a1" in readme and "Owner note from @a2" in readme
    # transcript analysis
    for handle in ("@a1", "@a2"):
        a = ana["agents"][handle]
        assert "README.md" in a["files_read"]
        assert a["gud_tools"]["inflight"] == 1 and a["gud_tools"]["claim"] == 1
    colliding = [c for a in ana["agents"].values() for c in a["colliding_pushes"]]
    assert all(c["inflight_before"] and c["found_in_transcript"] for c in colliding)
    assert colliding or ana["summary"]["push_refusals"]
    holder = next(h for h, a in g["per_agent"].items() if a["claims_won"])
    other = "@a2" if holder == "@a1" else "@a1"
    assert ana["agents"][holder]["reads_outside_claims"] == []
    assert ana["agents"][other]["claim_scopes"] == [] and ana["agents"][other]["reads_outside_claims"] == ["README.md"]
    assert ana["summary"]["colliding_with_inflight_before"] == ana["summary"]["colliding_pushes"]
    assert summary["hidden_access"] == []
    assert summary["cost_usd"] > 0
    for agent in summary["agents"]:
        assert agent["exit_code"] is not None and agent["turns"] > 0
    run_dir = out / f"{arm}-acme-site-n2-t1"
    assert (run_dir / "host-log.jsonl").stat().st_size > 0
    assert "tester @tester running" in (run_dir / "tester.log").read_text()
    saved = json.loads((run_dir / "summary.json").read_text())
    assert "_pushes_full" not in saved["gud"]
    text = report.report([out])
    assert f"| {arm} |" in text and "claims won" in text
    assert not any(Path(tmp_path).glob("gudv1-*")), "temp workspaces are removed"


def test_gud_arm_repo_config_takes_lease_and_task_quiet(tmp_path):
    from gud.core.config import parse_config
    from gud_arm import GudCCArm

    spec = specs.load("acme-site")
    short = GudCCArm(tmp_path / "s", spec, ["@a1"], lease="2m", task_quiet="2m").config_bytes()
    assert parse_config(short).lease == parse_config(short).quiet == 120_000
    default = GudCCArm(tmp_path / "d", spec, ["@a1"]).config_bytes()
    assert b"lease 10m\n" in default and b"task-quiet" not in default      # the arm's defaults are unchanged
    assert parse_config(default).quiet == 600_000
    arm = run.make_arm("gud-ref", tmp_path / "r", spec, ["@a1"], 20.0, "tip", {"lease": "2m"})
    assert b"lease 2m\n" in arm.config_bytes()


def test_gud_cc_prompt_shares_the_preamble(tmp_path):
    from git_arm import GitArm
    from gud_arm import GudCCArm

    spec = specs.load("todo-stack")
    handles = ["@a1", "@a2"]
    git_arm, cc = GitArm(tmp_path / "g", spec, handles), GudCCArm(tmp_path / "u", spec, handles)
    git_arm.setup()
    cc.setup()
    try:
        _check_preamble(git_arm, cc, spec, handles)
    finally:
        cc.close()                                       # the host serve process


def _check_preamble(git_arm, cc, spec, handles):
    head = lambda text: text.split("# How this repo works")[0]  # noqa: E731
    git = run.prompt_for(git_arm, spec, "@a1", handles, 1.5)
    gud = run.prompt_for(cc, spec, "@a1", handles, 1.5)
    assert head(git) == head(gud)
    assert spec.text in head(git)
    assert "test_hidden" not in git + gud
    assert "subtask" in gud and "inflight" in gud and "@a2" in gud
    assert "todolib.py" not in cc.instructions("@a1"), "nothing pre-splits the work"
    mcp = json.loads(cc.mcp_config("@a1").read_text())["mcpServers"]["gud"]
    assert mcp["args"][:2] == ["-m", "gud.cli"] and "mcp" in mcp["args"] and mcp["env"]["PYTHONPATH"] == str(run.REPO)
    assert (cc.workdir("@a1") / ".gud" / "BASE").is_file() and (cc.workdir("@a1") / "README.md").is_file()


def test_gud_ref_auth_failure_is_harness_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "authfail")
    summary = run.run_one(arm_name="gud-ref", spec_name="todo-cli", n=1, trial=1, budget=1.0, timeout=60,
                          out=tmp_path / "out", agent_cmd=AGENT_CMD, model=None, window=3.0, keep_work=False,
                          work_root=tmp_path, ref_cmd=REF_CMD, ref_settle=5.0)
    assert summary["outcome"] == "harness_error"
    assert any("authentication" in e for e in summary["harness_errors"])


def test_cap_ledger_enforces_the_cap(tmp_path):
    from cap import CapLedger

    ledger = CapLedger(tmp_path / "spend.json", cap_usd=2.0)
    assert ledger.reserve("a", 1.2) == 1.2
    assert ledger.reserve("b", 1.2) == 0.8          # only what is left under the cap
    assert ledger.reserve("c", 1.0) == 0.0          # nothing left while a and b are in flight
    ledger.commit("a", 0.3)                         # a spent less than it reserved: 0.9 comes back
    assert ledger.reserve("c", 1.0) == 0.9
    ledger.commit("b", 0.8)
    ledger.commit("c", 0.9)
    state = ledger.state()
    assert state["spent_usd"] == 2.0 and state["remaining_usd"] == 0.0 and state["reserved_usd"] == 0.0


def test_run_one_skips_when_the_cap_is_reached(tmp_path):
    from cap import CapLedger

    ledger = CapLedger(tmp_path / "spend.json", cap_usd=1.0)
    ledger.commit("earlier", 1.0)
    summary = run.run_one(arm_name="git", spec_name="todo-cli", n=2, trial=1, budget=1.0, timeout=30,
                          out=tmp_path / "out", agent_cmd=AGENT_CMD, model=None, window=3.0, keep_work=False,
                          work_root=tmp_path, ledger=ledger)
    assert summary["outcome"] == "skipped_cap"


def test_run_one_commits_spend_to_the_ledger(tmp_path, fake_env):
    from cap import CapLedger

    ledger = CapLedger(tmp_path / "spend.json", cap_usd=10.0)
    summary = run.run_one(arm_name="git", spec_name="acme-site", n=2, trial=1, budget=5.0, timeout=150,
                          out=tmp_path / "out", agent_cmd=AGENT_CMD, model=None, window=3.0, keep_work=False,
                          work_root=tmp_path, ledger=ledger)
    assert summary["outcome"] == "completed"
    state = ledger.state()
    assert state["reserved_usd"] == 0 and state["spent_usd"] == pytest.approx(summary["cost_usd"], abs=1e-4)
    assert summary["analysis"]["agents"]["@a1"]["files_changed"], "from origin.git's commits"
