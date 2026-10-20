"""Harness pieces that need no model: stream parsing, cost, the supervisor's kill rules, both arms end
to end with a deterministic fake agent, the PR protocol, and the report."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import report
import run
import specs
from agents import AgentSpec, claude_argv, supervise
from streams import StreamStats, usage_cost

FAKE = Path(__file__).resolve().parent / "fake_agent.py"
AGENT_CMD = f"{sys.executable} {FAKE}"


# -- stream parsing and cost ----------------------------------------------------------------------

def test_usage_cost_matches_claude_code():
    # From a real probe: Claude Code reported costUSD 0.0466866 for this sonnet-5-5 usage.
    usage = {"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 19693,
             "cache_creation_input_tokens": 10676,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 10676}}
    assert usage_cost("claude-sonnet-5-5", usage) == pytest.approx(0.0466866, abs=1e-9)


def test_stream_stats_dedups_messages_and_prefers_result():
    stats = StreamStats()
    usage = {"input_tokens": 1000, "output_tokens": 100}
    for block in ({"type": "text", "text": "a"}, {"type": "tool_use", "id": "t1", "name": "Bash",
                                                    "input": {"command": "ls"}}):
        stats.feed({"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5",
                                                     "usage": usage, "content": [block]}})
    stats.feed({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                          "content": [{"type": "text", "text": "x.py"}]}]}})
    assert stats.estimated_cost() == pytest.approx((1000 * 2 + 100 * 10) / 1e6)
    assert stats.tool_calls == [{"name": "Bash", "input": {"command": "ls"}, "result": "x.py", "is_error": False}]
    assert stats.cost_source() == "estimate"
    stats.feed({"type": "result", "total_cost_usd": 0.5, "num_turns": 3, "result": "done\nESCALATE: x"})
    assert stats.cost() == 0.5 and stats.turns() == 3 and stats.cost_source() == "result"


def test_auth_failure_detected_from_recorded_livetest_events():
    """The exact events from livetest/runs/20261017-210957-claude-plain (exit 1 in 4.4 s)."""
    stats = StreamStats()
    stats.feed({"type": "assistant", "error": "authentication_failed", "message": {
        "id": "x", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0},
        "content": [{"type": "text", "text": "Failed to authenticate: OAuth session expired and could not be refreshed"}]}})
    assert stats.auth_failed()
    assert stats.cost() == 0


def test_claude_argv_is_identical_across_arms_except_mcp():
    common = dict(model="claude-sonnet-5-5", agent_budget=2.0, deny_paths=[Path("/x/hidden")])
    git = claude_argv("claude", "P", mcp_config=None, allow_mcp=None, **common)
    gud = claude_argv("claude", "P", mcp_config=Path("/m.json"), allow_mcp="gud", **common)
    assert [a for a in gud if a not in ("--mcp-config", "/m.json", "mcp__gud")] == git
    assert git[git.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    assert "Read(//x/hidden/**)" in git
    assert git[git.index("--max-budget-usd") + 1] == "2.00"


# -- supervisor ------------------------------------------------------------------------------------

def _specs(tmp_path, mode: str, n: int = 2, mcp: bool = False) -> list[AgentSpec]:
    env = dict(os.environ, FAKE_AGENT_MODE=mode)
    out = []
    for i in range(n):
        argv = [sys.executable, str(FAKE), "-p", "x"] + (["--mcp-config", "/nope.json"] if mcp else [])
        out.append(AgentSpec(handle=f"@a{i + 1}", argv=argv, cwd=tmp_path, env=env,
                             log_dir=tmp_path / f"a{i + 1}", require_mcp="gud" if mcp else None))
    return out


def test_supervisor_kills_on_run_budget(tmp_path):
    started = time.time()
    runs, outcome = supervise(_specs(tmp_path, "spend"), run_budget=0.2, agent_budget=1.0, timeout=60, poll=0.1)
    assert outcome.reason == "budget"
    assert time.time() - started < 30
    assert all(r.killed == "run_budget" for r in runs)
    assert outcome.total_cost_usd >= 0.2
    assert all((tmp_path / f"a{i}" / "stream.jsonl").stat().st_size > 0 for i in (1, 2))


def test_supervisor_kills_one_agent_on_its_budget(tmp_path):
    specs_ = _specs(tmp_path, "spend", n=1) + [
        AgentSpec("@a2", [sys.executable, str(FAKE), "-p", "x"], tmp_path,
                  dict(os.environ, FAKE_AGENT_MODE="sleep"), tmp_path / "a2")]
    runs, outcome = supervise(specs_, run_budget=10, agent_budget=0.1, timeout=4, poll=0.1)
    assert runs[0].killed == "agent_budget"
    assert runs[1].killed == "timeout" and outcome.reason == "timeout"


def test_supervisor_timeout(tmp_path):
    runs, outcome = supervise(_specs(tmp_path, "sleep"), run_budget=10, agent_budget=5, timeout=2, poll=0.1)
    assert outcome.reason == "timeout"
    assert all(r.killed == "timeout" and r.exit_code is not None for r in runs)


def test_supervisor_auth_failure_is_harness_error(tmp_path):
    runs, outcome = supervise(_specs(tmp_path, "authfail"), run_budget=10, agent_budget=5, timeout=30, poll=0.1)
    assert outcome.reason == "harness_error"
    assert any("authentication failed" in e for e in outcome.harness_errors)


def test_supervisor_mcp_not_connected_is_harness_error(tmp_path):
    runs, outcome = supervise(_specs(tmp_path, "nomcp", n=1, mcp=True), run_budget=10, agent_budget=5,
                              timeout=30, poll=0.1)
    assert outcome.reason == "harness_error"
    assert "MCP server gud is failed" in outcome.harness_errors[0]


# -- both arms end to end --------------------------------------------------------------------------

@pytest.fixture
def fake_env(monkeypatch):
    spec = specs.load("acme-site")
    monkeypatch.setenv("FAKE_FILES_DIR", str(spec.hidden / "reference"))
    monkeypatch.setenv("FAKE_AGENTS", "2")
    monkeypatch.setenv("FAKE_CONFLICT", "1")
    monkeypatch.setenv("FAKE_ESCALATE", "1")
    monkeypatch.setenv("FAKE_DEADLINE", "90")


@pytest.mark.parametrize("arm", ["git", "gud"])
def test_arm_end_to_end_with_fake_agents(arm, tmp_path, fake_env):
    out = tmp_path / "out"
    summary = run.run_one(arm_name=arm, spec_name="acme-site", n=2, trial=1, budget=5.0, timeout=150,
                          out=out, agent_cmd=AGENT_CMD, model="claude-sonnet-5-5", window=3.0,
                          keep_work=False, work_root=tmp_path)
    assert summary["outcome"] == "completed", summary.get("harness_errors")
    assert summary["hidden"]["pass_rate"] == 1.0, summary["spec_checks"]
    assert summary["final_tests_pass"] is True
    assert summary["integrations"] >= 2
    assert summary["test_runs"] >= 2
    assert summary["conflicts_total"] >= 1, summary[arm]["conflicts"]
    assert summary["conflicts_resolved"] == summary["conflicts_total"]
    assert summary["escalations"] == 1
    assert summary["hidden_access"] == []
    assert summary["cost_usd"] > 0 and summary["tokens"]["output"] > 0
    assert summary["model"] == "claude-sonnet-5-5"
    for agent in summary["agents"]:
        assert agent["exit_code"] == 0 and agent["killed"] is None
        assert agent["turns"] > 0 and agent["cost_source"] == "result"
    run_dir = out / f"{arm}-acme-site-n2-t1"
    saved = json.loads((run_dir / "summary.json").read_text())
    assert saved["integrations"] == summary["integrations"]
    assert (run_dir / "final_tree" / "pricing.html").is_file()
    assert (run_dir / "agents" / "a1" / "stream.jsonl").stat().st_size > 0
    readme = (run_dir / "final_tree" / "README.md").read_text()
    assert "Owner note from @a1" in readme and "Owner note from @a2" in readme
    if arm == "git":
        git = summary["git"]
        assert git["merges"] == 2 and git["approvals"] >= 2
        assert git["merge_rejections"].get("rebase_conflict", 0) >= 1
        assert (run_dir / "prs" / "events.jsonl").exists()
    else:
        gud = summary["gud"]
        assert gud["links"] >= 2 and gud["emails_per_link"] > 1
        assert gud["superseded"] + gud["stale"] >= 1
        assert (run_dir / "mailbox.jsonl").exists()
    text = report.report([out])
    assert f"| {arm} |" in text and "hidden-test pass rate" in text and "100%" in text
    assert not any(Path(tmp_path).glob("gudv1-*")), "temp workspaces are removed"


def test_both_arms_get_the_same_prompt_preamble(tmp_path):
    from git_arm import GitArm
    from gud_arm import GudArm

    spec = specs.load("todo-cli")
    handles = ["@a1", "@a2"]
    git_arm, gud_arm = GitArm(tmp_path / "g", spec, handles), GudArm(tmp_path / "u", spec, handles)
    git_arm.setup()
    gud_arm.setup()
    git = run.prompt_for(git_arm, spec, "@a1", handles, 1.5)
    gud = run.prompt_for(gud_arm, spec, "@a1", handles, 1.5)
    head = lambda text: text.split("# How this repo works")[0]  # noqa: E731
    assert head(git) == head(gud)
    assert spec.text in head(git)
    assert "test_hidden" not in git + gud


# -- the PR protocol -------------------------------------------------------------------------------

@pytest.fixture
def git_arm(tmp_path):
    from git_arm import GitArm
    arm = GitArm(tmp_path / "w", specs.load("todo-cli"), ["@a1", "@a2"])
    arm.setup()
    return arm


def _sh(arm, handle, command):
    env = run.agent_env(arm, handle)
    return subprocess.run(command, shell=True, cwd=arm.workdir(handle), env=env, capture_output=True, text=True)


def test_main_is_protected(git_arm):
    _sh(git_arm, "@a1", "echo x > x.txt && git add x.txt && git commit -qm x")
    proc = _sh(git_arm, "@a1", "git push origin HEAD:main")
    assert proc.returncode != 0 and "main is protected" in proc.stderr
    assert git_arm.events()[-1]["kind"] == "direct_main_push_rejected"
    assert git_arm.events()[-1]["by"] == "@a1"


def test_merge_requires_approval_and_passing_tests(git_arm):
    _sh(git_arm, "@a1", "git switch -qc bad && printf 'def test_x():\\n    assert False\\n' > tests/test_x.py"
                        " && git add -A && git commit -qm bad && git push -q -u origin bad")
    assert "opened PR1" in _sh(git_arm, "@a1", "pr open --title bad").stdout
    assert "cannot approve your own" in _sh(git_arm, "@a1", "pr approve PR1").stdout
    proc = _sh(git_arm, "@a1", "pr merge PR1")
    assert proc.returncode == 1 and "needs an approval" in proc.stdout
    _sh(git_arm, "@a2", "pr approve 1 -m ok")
    proc = _sh(git_arm, "@a1", "pr merge PR1")
    assert proc.returncode == 1 and "tests failed" in proc.stdout
    _sh(git_arm, "@a1", "git rm -q tests/test_x.py && git commit -qm fix && git push -q")
    proc = _sh(git_arm, "@a1", "pr merge PR1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[merged]" in _sh(git_arm, "@a2", "pr list --all").stdout
    m = git_arm.metrics(60, {})
    assert m["integrations"] == 1 and m["test_runs"] == 2
    assert m["git"]["merge_rejections"] == {"no_approval": 1, "tests_failed": 1}
    assert m["git"]["gate_test_failures"] == 1


def test_agent_identity_is_per_clone(git_arm):
    assert _sh(git_arm, "@a2", "git config user.name").stdout.strip() == "@a2"
    assert _sh(git_arm, "@a1", "git config user.email").stdout.strip() == "a1@agents.local"
