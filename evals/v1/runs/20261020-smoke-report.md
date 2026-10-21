### acme-site, N=2

| | git | gud |
| --- | --- | --- |
| trials | 1 | 1 |
| outcome | completed ×1 | completed ×1 |
| hidden-test pass rate | 100% | 100% |
| hidden tests passed | 21 / 21 | 21 / 21 |
| repo's own tests pass on final tree | True ×1 | True ×1 |
| wall time | 60 s | 202 s |
| cost (run) | $0.17 | $0.19 |
| cost per agent | $0.08 ($0.08–$0.08) | $0.09 ($0.07–$0.12) |
| tokens per agent (all kinds) | 92,333 (86,963–97,703) | 119,052 (84,370–153,735) |
| output tokens per agent | 2,770 (2,761–2,779) | 3,432 (2,143–4,722) |
| turns per agent | 8 (7–9) | 10 (8–12) |
| agent wall time | 59 s (59 s–60 s) | 191 s (181 s–201 s) |
| agents killed (budget/timeout) | 0 | 0 |
| integrations (links / merges) | 1 | 2 |
| integrations per min | 1.00 | 0.59 |
| test runs (tester / merge gate) | 1 | 3 |
| test runs per integration | 1.00 | 1.50 |
| agent-run local test runs | 3 | 0 |
| conflicts | 0 | 0 |
| conflicts resolved | 0 | 0 |
| escalations | 0 | 0 |
| emails per link (gud) | — | 3.5 |
| superseded / stale (gud) | — | 0 |
| objections (gud) | — | 0 |
| PRs opened (git) | 2 | — |
| merge rejections (git) | 0 | — |
| hidden-test access attempts | 0 | 0 |

Model: claude-sonnet-5-5. Total spend in this table: $0.35.

### kv-api, N=2

| | git | gud |
| --- | --- | --- |
| trials | 1 | 1 |
| outcome | completed ×1 | completed ×1 |
| hidden-test pass rate | 100% | 100% |
| hidden tests passed | 17 / 17 | 17 / 17 |
| repo's own tests pass on final tree | True ×1 | True ×1 |
| wall time | 186 s | 100 s |
| cost (run) | $0.19 | $0.21 |
| cost per agent | $0.10 ($0.08–$0.11) | $0.10 ($0.08–$0.12) |
| tokens per agent (all kinds) | 116,261 (97,235–135,287) | 104,564 (102,878–106,251) |
| output tokens per agent | 3,752 (2,990–4,513) | 4,207 (2,841–5,573) |
| turns per agent | 9.5 (8–11) | 8.5 (8–9) |
| agent wall time | 185 s (184 s–186 s) | 98 s (97 s–99 s) |
| agents killed (budget/timeout) | 0 | 0 |
| integrations (links / merges) | 2 | 2 |
| integrations per min | 0.65 | 1.20 |
| test runs (tester / merge gate) | 2 | 2 |
| test runs per integration | 1.00 | 1.00 |
| agent-run local test runs | 6 | 1 |
| conflicts | 1 | 0 |
| conflicts resolved | 1 | 0 |
| escalations | 0 | 0 |
| emails per link (gud) | — | 2.5 |
| superseded / stale (gud) | — | 0 |
| objections (gud) | — | 0 |
| PRs opened (git) | 2 | — |
| merge rejections (git) | 0 | — |
| hidden-test access attempts | 0 | 0 |

Model: claude-sonnet-5-5. Total spend in this table: $0.40.

### todo-cli, N=2

| | git | gud |
| --- | --- | --- |
| trials | 1 | 1 |
| outcome | completed ×1 | completed ×1 |
| hidden-test pass rate | 100% | 100% |
| hidden tests passed | 15 / 15 | 15 / 15 |
| repo's own tests pass on final tree | True ×1 | True ×1 |
| wall time | 30 s | 220 s |
| cost (run) | $0.15 | $0.15 |
| cost per agent | $0.07 ($0.07–$0.08) | $0.07 ($0.06–$0.09) |
| tokens per agent (all kinds) | 69,473 (66,815–72,131) | 87,601 (69,075–106,127) |
| output tokens per agent | 2,544 (2,525–2,564) | 2,698 (1,623–3,773) |
| turns per agent | 6 (6–6) | 7 (6–8) |
| agent wall time | 30 s (30 s–30 s) | 210 s (202 s–218 s) |
| agents killed (budget/timeout) | 0 | 0 |
| integrations (links / merges) | 1 | 2 |
| integrations per min | 2.02 | 0.55 |
| test runs (tester / merge gate) | 1 | 2 |
| test runs per integration | 1.00 | 1.00 |
| agent-run local test runs | 3 | 0 |
| conflicts | 0 | 1 |
| conflicts resolved | 0 | 1 |
| escalations | 0 | 0 |
| emails per link (gud) | — | 3.0 |
| superseded / stale (gud) | — | 1 |
| objections (gud) | — | 0 |
| PRs opened (git) | 2 | — |
| merge rejections (git) | 0 | — |
| hidden-test access attempts | 0 | 0 |

Model: claude-sonnet-5-5. Total spend in this table: $0.30.

- Each cell is mean (min–max) over trials; per-agent rows pool every agent in every trial.
- Integrations are gud links (excluding genesis) or git PR merges into main.
- Test runs are the gud tester's replies or the git merge gate's pytest runs; both run `python -m pytest -q -x`.
- gud conflicts are pushes that ended superseded, stale, conflict or objected; git conflicts are merge-gate rebase conflicts plus `CONFLICT` lines in agents' own git output. Resolved means the same author landed later.
- Escalations are final messages containing `ESCALATE:`. Cost is Claude Code's reported total_cost_usd (list price), or an estimate from usage for a killed agent.
