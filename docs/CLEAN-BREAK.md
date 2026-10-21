# The v1 clean break

gud v1 replaces v0 outright. v0 is deprecated. Its last commit is tagged `v0-final`. There is no v0 compatibility and no migration path: v0 mailboxes (no host log, v0 `Gud-*` trailers) no longer replay. A v0 project can start again from its files with `gud init` (a fresh v1 genesis) or from a git commit with `gud init --from-git` (`gud import`).

This file is the inventory made before anything was deleted. Each entry says what happens to a path and why. It was built from an import graph of `gud/`, `tests/`, `evals/` and `scripts/` (every `import`/`from` statement, relative imports resolved), the import closure of the v1 entry points, and a grep for entry points, `python -m` invocations, file paths and doc links.

## Method and the one finding that matters

The v1 closure is `gud.core.*`, `gud.transport.*`, `gud.wc.*`, `gud.events.*`, `gud.agent.*`, `gud.ops.*`, `gud.runners.*`, `gud.gitbridge`, `gud.host_view` and the `*_v1` modules, plus `tests/v1/` and the v1 eval harness. No v1 library module imports any v0 module. `gud/runners/instabox.py` was ported from v0 `gud/build.py` by copying, and `gud/core/ops.py` ported v0's merge by copying, so neither needs `gud/build.py` or `gud/mcp_server.py`. `gud/hostauth.py` is v0 only: v1 signs mirror requests in `gud/wc/remote.py` and verifies them in `gud/host_v1.py`.

The only v1-side code that reaches v0 is in the eval harness:

- `evals/v1/gud_arm.py` and `evals/v1/gud_tester.py` are the v0 arm (`gud.compose`, `gud.mail_fake`, `gud.memberfile`, `gud.replay`, `gud.sig`, `gud.types`, `gud.wire`, `gud.build`, `gud.config`, `gud.mailbox`, `gud.tester`).
- `evals/v1/run.py` registers that arm as `gud` (and the `both` arm set).
- `evals/v1/tests/fake_agent.py` has a v0 section (`gud.config`, `gud.mailbox`, `gud.mcp_server`), used only for the v0 arm.
- `evals/v1/tests/test_harness.py` runs the v0 arm end to end (`arm in ["git", "gud"]`) and compares its prompt with git's (`from gud_arm import GudArm`).
- `evals/v1/report.py` has rows for the v0 arm's `gud` summary block.

`tests/v1/transport/test_registry.py::test_v1_transport_does_not_import_v0` names v0 modules in a string. It is rewritten so it no longer names them: v1 modules may import only v1 modules.

## Deleted

### v0 library modules (`gud/`)

All are v0 only. None is imported by a v1 module.

| Path | What it was |
| --- | --- |
| `gud/wire.py` | v0 email rendering and parsing, v0 hashes |
| `gud/replay.py` | v0 replay |
| `gud/rules.py` | v0 affected owners, protection, quorum |
| `gud/tasks.py` | v0 task state |
| `gud/types.py` | v0 data types and mailbox interface |
| `gud/chain.py` | v0 chain over a mailbox |
| `gud/patch.py` | v0 unified diffs |
| `gud/sig.py` | v0 Ed25519 helpers |
| `gud/keys.py` | v0 local key store |
| `gud/mail_fake.py`, `gud/mail_hosted.py`, `gud/mail_primitive.py`, `gud/mail_remote.py`, `gud/mailbox.py` | v0 mailboxes (v1: `gud/transport/`) |
| `gud/webhook.py` | v0 Primitive webhook receiver (v1: `gud/host_v1.py` provider webhooks) |
| `gud/inbox.py` | v0 agent inbox gate |
| `gud/worker.py`, `gud/lead.py`, `gud/swarm.py` | v0 scripted agents and in-process swarm |
| `gud/tester.py` | v0 tester agent (the name is reused by v1, below) |
| `gud/build.py` | v0 test runners (v1: `gud/runners/`) |
| `gud/mcp_server.py` | v0 MCP server (v1: `gud/mcp_v1.py`) |
| `gud/mcp_baseline.py` | v0 git-style baseline MCP server for the scripted evals |
| `gud/mcp_app.py` | v0 live-feed MCP app (widget in `resources/`) |
| `gud/api.py` | v0 HTTP API for the old UI (v1: `gud/host_view.py`) |
| `gud/cli.py` | v0 command line (the name is reused by v1, below) |
| `gud/config.py` | v0 settings from env and `.env` |
| `gud/console.py` | v0 live view and human actions |
| `gud/bugs.py` | v0 bug mail |
| `gud/joins.py`, `gud/members.py`, `gud/memberfile.py` | v0 membership |
| `gud/merge.py` | v0 three-way merge (v1: `gud.core.ops.three_way`) |
| `gud/notify.py` | v0 task-done notices |
| `gud/open.py`, `gud/repos.py` | v0 GUI repo opening and picker |
| `gud/qa.py` | v0 browser QA (browser-use) |
| `gud/analytics.py` | v0 Armature MCP analytics |
| `gud/hostauth.py` | v0 mirror signatures |
| `gud/blame.py` | v0 blame |
| `gud/compose.py` | v0 signed mail builder |
| `gud/testkit.py` | v0 test fixtures |
| `gud/core/v0.py` | the legacy reader (SPEC-v1 §23). Deleted by decision: v0 mailboxes no longer replay. Nothing outside `tests/v1/v0/` imported it. |

### Tests

| Path | Why |
| --- | --- |
| `tests/test_*.py` (25 files) | every top-level test imports only v0 modules (or runs `scripts/demo.py`) |
| `tests/v1/v0/` | tests of the legacy reader (2 tests) |

### Scripts, demo data and v0 run artefacts

| Path | Why |
| --- | --- |
| `scripts/demo.py` | v0 scripted demo (imports 18 v0 modules) |
| `scripts/run_local.sh` | starts the v0 API (`gud.api`) and the Vite dev server; v1 serves the built dashboard at `/` from `gud host serve` |
| `demo_repo/` | used only by v0 (`scripts/demo.py`, `gud/cli.py`, `tests/test_swarm.py`, `tests/test_build.py`, `evals/pr_vs_thread.py`, `evals/swarm_vs_prs.py`, `docs/mcp-app.md`). No v1 eval spec uses its content; `evals/v1/specs/` have their own seeds. `docs/v1/PLAN.md` mentions it only as history. |
| `.gud-home-mcp/mailbox.jsonl`, `.gud-home-primitive-swarm/repo.txt` | tracked v0 mailbox artefacts of the scripted evals |
| `results/` | traces of the v0 scripted comparisons (written by `evals/dump_traces.py`) |

### Evals

| Path | Why |
| --- | --- |
| `evals/README.md`, `evals/run_local.py`, `evals/pr_vs_thread.py`, `evals/swarm_vs_prs.py`, `evals/dump_traces.py`, `evals/armature_ci.py`, `evals/armature_cases.json`, `evals/cases/`, `evals/results/` | the scripted v0 evals: v0 MCP servers, the v0 git baseline, Armature CI cases for them, and their results |
| `evals/v1/gud_arm.py`, `evals/v1/gud_tester.py` | the v0 arm of the v1 eval harness |
| v0 sections of `evals/v1/run.py`, `evals/v1/report.py`, `evals/v1/tests/fake_agent.py`, `evals/v1/tests/test_harness.py` | the `gud` (v0) arm, the `both` arm set, the v0 `gud` summary rows, the v0 fake agent, and the v0 arm's tests |

### v0 widget and docs

| Path | Why |
| --- | --- |
| `resources/` | the MCP-app widget of `gud/mcp_app.py` |
| `package.json`, `package-lock.json` (repo root) | build only that widget (`vite build --config resources/vite.config.ts`). The v1 dashboard has its own `ui/package.json`. The root `node_modules/` is untracked. |
| `api_contract.md` | v0 Python core API and HTTP API |
| `docs/mcp-app.md` | testing the v0 MCP app |
| `RESULTS.md` | the v0 scripted comparisons (replaced by a v1 `RESULTS.md` afterwards) |
| `VIDEO.md` | a video script for the v0 comparison |
| `SPEC.md` | the v0 wire spec, replaced by the v1 spec |
| `README.md`, `DEMO.md` | v0 content; rewritten for v1. `DEMO.md` was an ignored, untracked file in the main checkout; the v1 one is tracked (its `.gitignore` line is removed), so move the old local copy aside before checking this branch out there. |

### Dependencies (`pyproject.toml`)

| Dependency | Why |
| --- | --- |
| `sse-starlette` | only `gud/api.py` used it; the v1 host streams SSE with FastAPI's `StreamingResponse` |
| `armature-mcp-analytics` | only `gud/analytics.py` |
| extra `app` (`mcp-use`) | only `gud/mcp_app.py` |
| extra `qa` (`browser-use`) | only `gud/qa.py` |
| console script `gud1` | v1 is now `gud` |

### Untracked local state (not in git)

`.venv-app/` (the v0 MCP app's virtualenv) and `.gud-home-app/` are ignored, untracked directories in the main checkout, so no commit can delete them. Their `.gitignore` lines are removed; delete the directories by hand (`rm -rf .venv-app .gud-home-app`). The same goes for the other v0 `GUD_HOME`s there (`.gud-home`, `.gud-home-live`, `.gud-home-site*`, untracked).

## Kept

| Path | Why |
| --- | --- |
| `gud/core/` (minus `v0.py`) | the v1 core. Imports only `gud.core.*` and the standard library. |
| `gud/transport/`, `gud/events/`, `gud/wc/`, `gud/agent/`, `gud/ops/`, `gud/runners/` | v1 |
| `gud/gitbridge.py`, `gud/host_view.py` | v1 |
| `tests/v1/` (minus `v0/`) | the v1 suite. Kept at `tests/v1/` so test ids, docs and eval notes stay valid; there is no other suite now. |
| `evals/v1/` (git arm, gud v1 arms, specs, hidden tests, runs, reports) | the agent evals. The directory keeps its name, which every report and summary path uses. |
| `evals/v1/runs/` | historical results, unchanged. Reports and summaries written before the rename keep the arm names `gud1-cc`/`gud1-ref` and the `gud1` summary block (see Renamed). |
| `docs/primitive-openapi.json` | the Primitive API reference. Unreferenced by code, but it documents the provider that `gud/transport/primitive.py` talks to. |
| `ui/` | the v1 dashboard, already on `gud/host_view.py` |
| `fastapi`, `uvicorn`, `httpx`, `cryptography`, `mcp`, `pytest`, extra `agent` (`claude-agent-sdk`) | used by v1 (host, transports, crypto, MCP server, tester, reference agent) |

## Renamed

| From | To | Why |
| --- | --- | --- |
| `gud/cli_v1.py` | `gud/cli.py` | v1 is the only CLI; console script `gud = gud.cli:main` |
| `gud/mcp_v1.py` | `gud/mcp.py` | `gud.mcp` is a submodule of the `gud` package, so it cannot shadow the installed `mcp` package: absolute imports inside it (`from mcp.server...`) still resolve to the SDK. Checked in the verification. |
| `gud/tester_v1.py` | `gud/tester.py` | |
| `gud/host_v1.py` | `gud/host.py` | |
| `docs/v1/SPEC-v1.md` | `SPEC.md` | the contract, with the implementation notes folded in |
| `docs/v1/INTERFACES.md` | `docs/INTERFACES.md` | addenda merged into the module sections |
| `docs/v1/TRANSPORTS.md`, `docs/v1/PLAN.md` | `docs/TRANSPORTS.md`, `docs/PLAN.md` | PLAN is marked as the v1 design record |
| `docs/v1/CLEAN-BREAK.md` | `docs/CLEAN-BREAK.md` | this file, once `docs/v1/` is empty |
| eval arms `gud1-cc`, `gud1-ref` | `gud-cc`, `gud-ref` | `evals/v1/gud1_arm.py` becomes `evals/v1/gud_arm.py`, `Gud1CCArm`/`Gud1RefArm` become `GudCCArm`/`GudRefArm`, the summary block `gud1` becomes `gud`, `analyze.gud1_metrics` becomes `analyze.gud_metrics`, `evals/v1/tests/test_gud1.py` becomes `test_gud.py`. `report.py` still reads historical summaries (their `gud1` block). |

## Kept, though it might look like it should be gone

| What | Why |
| --- | --- |
| v0 module names in `docs/PLAN.md` | PLAN is the v1 design record and describes v0 as it was; a note at its top says so. |
| v0 and `gud1-*` names in `evals/v1/runs/` | historical summaries and reports, left as written (the smoke runs' `gud` arm is v0). `evals/v1/README.md` explains the names. |
| `gud1-cc` in two regression docstrings (`tests/v1/mcp/test_tools.py`, `tests/v1/replay/test_tasks.py`) | they name the historical eval runs the regressions came from |
| `tests/v1/` and `evals/v1/` | there is no other suite or eval now, but every report, summary path, test id and SPEC reference uses these paths |
| the word "v1" in docstrings and the wire (`Gud-Version: 1`, `/api/v1/`, "gud v1 MCP server") | it names the wire version, not a module generation |
| `gudv1-*` temp-directory prefix of eval runs | an eval-internal name the harness tests check |
| "v0" in SPEC §24 deviations and §2.3 | history of why the protocol is shaped as it is, and the statement that v0 is not read |
| `docs/primitive-openapi.json` | the provider reference for `gud/transport/primitive.py` |
