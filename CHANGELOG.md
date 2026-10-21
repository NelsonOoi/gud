# Changelog

## 1.0.0: the v1 clean break

gud v1 replaces v0 outright. v0 is deprecated; its last commit is tagged `v0-final`. There is no compatibility layer and no migration path: v0 mailboxes (no host log, `Gud-*` trailers in the body) no longer replay. Start a v1 repo from a v0 project's files with `gud init`, or from a git commit with `gud init --from-git`. The full inventory, with a reason for every path, is [docs/CLEAN-BREAK.md](docs/CLEAN-BREAK.md).

### The protocol (`SPEC.md`)

- Wire spec version 1 is now `SPEC.md`, the only spec. The accumulated "Implementation notes" are folded into the normative text where they changed behaviour (envelope codec, patch rules, the §6 budget with its `57/78` base64 factor, host log rules, event keys and data names, ordering at one decision time, consent, evidence, escalation, carry-forward, tasks, done by declaration and by quiescence, dispatch, mirror endpoints and auth, local host mode, working copies, archives and audit). The rest is Appendix A, non-normative.
- §2.3: only `Gud-Version: 1` envelopes and unsigned messages are read; a repo is defined by its host log. §23 (v0 compatibility) is replaced by archives and host migration.

### Removed

- The legacy reader `gud/core/v0.py` (v0 mailboxes replayed through the v1 chain view).
- All v0 modules: `wire`, `replay`, `rules`, `tasks`, `types`, `chain`, `patch`, `sig`, `keys`, the `mail_*` mailboxes and `mailbox`, `webhook`, `inbox`, `worker`, `lead`, `swarm`, the v0 `tester`, `build`, `mcp_server`, `mcp_baseline`, `mcp_app`, `api`, the v0 `cli`, `config`, `console`, `bugs`, `joins`, `members`, `memberfile`, `merge`, `notify`, `open`, `repos`, `qa`, `analytics`, `hostauth`, `blame`, `compose`, `testkit`.
- The v0 tests (`tests/test_*.py`, `tests/v1/v0/`), `scripts/` (`demo.py`, `run_local.sh`), `demo_repo/`, the scripted evals and their results (`evals/*.py`, `evals/cases/`, `evals/results/`, `results/`), the v0 arm of the agent evals (`evals/v1/gud_arm.py`, `gud_tester.py`), the MCP-app widget (`resources/`, the root `package.json`), and the v0 docs (`api_contract.md`, `docs/mcp-app.md`, `RESULTS.md`, `VIDEO.md`, the v0 `SPEC.md`).
- Dependencies only v0 used: `sse-starlette`, `armature-mcp-analytics`, and the `app` (`mcp-use`) and `qa` (`browser-use`) extras.

### Renamed

- The console script `gud1` is now `gud` (`gud.cli:main`).
- `gud/cli_v1.py` → `gud/cli.py`, `gud/mcp_v1.py` → `gud/mcp.py`, `gud/tester_v1.py` → `gud/tester.py`, `gud/host_v1.py` → `gud/host.py`. Run them as `python -m gud.cli`, `python -m gud.mcp`, `python -m gud.tester`.
- `docs/v1/SPEC-v1.md` → `SPEC.md`; `docs/v1/INTERFACES.md` → `docs/INTERFACES.md` (addenda merged into the module sections); `docs/v1/TRANSPORTS.md` → `docs/TRANSPORTS.md`; `docs/v1/PLAN.md` → `docs/PLAN.md` (kept as the v1 design record).
- Eval arms `gud1-cc` / `gud1-ref` → `gud-cc` / `gud-ref` (`evals/v1/gud_arm.py`; the summary block `gud1` → `gud`). Reports and summaries written before the rename keep the old names, and `evals/v1/report.py` still reads them.

### Changed

- `tests/v1/transport/test_registry.py` checks import boundaries with an allowlist: `gud.core` imports only `gud.core`, and `gud.transport` only `gud.core` and `gud.transport`.
- `gud.tester.runner_from_legacy` (an alias of `make_runner`) is gone.
- `README.md` and `DEMO.md` are rewritten for v1; `DEMO.md` is tracked.
