# Real-agent eval: git vs gud

Milestone 0 of [docs/PLAN.md](../../docs/PLAN.md). A swarm of real coding agents builds a complete repo from a task spec, once with git as the coordination layer and once with gud. Hidden acceptance tests, which no agent ever sees, score the final tree. Everything else is held fixed.

```bash
# one cell: 4 agents, $1.20 per run, under a hard cap across runs
.venv/bin/python evals/v1/run.py --arm gud-cc --spec todo-stack --agents 4 --budget-usd 1.2 --cap-usd 30

# all three arms, one trial each
.venv/bin/python evals/v1/run.py --arm v1 --spec acme-site-shared --agents 4 --trials 1 --budget-usd 1.2

# a grid, three cells at a time, under one hard cap
.venv/bin/python evals/v1/grid.py --specs acme-site-shared todo-stack refactor-guard \
    --arms git gud-cc gud-ref --agents 4 --trials 2 --budget-usd 1.2 --cap-usd 30 \
    --out evals/v1/runs/<stamp>-v1 --parallel 3

# aggregate any set of runs: mean (min–max) per spec, N and arm
.venv/bin/python evals/v1/report.py evals/v1/runs/<stamp>

# harness tests (no model calls; fake agents make deterministic edits)
.venv/bin/python -m pytest -q evals/v1/tests
```

`run.py` options: `--arm git|gud-cc|gud-ref|v1` (`v1` is all three), `--spec <name>`, `--agents N`, `--trials K`, `--trial-start`, `--budget-usd X` (per run; each agent gets X/N), `--timeout S` (wall clock per run), `--out DIR` (default `evals/v1/runs/<stamp>/`), `--model` (default `claude-sonnet-5-5`; `''` lets the CLI choose and the model is recorded from the session's init event), `--window` (gud consent window, default 20 s), `--gud-test-policy tip|gate`, `--gud-lease`, `--gud-task-quiet`, `--wait-done`, `--cap-usd`/`--ledger`, `--keep-work`, `--work-root`, `--agent-cmd` and `--ref-cmd` (swap the agent binary; the tests use `tests/fake_agent.py` and `tests/fake_refagent.py`).

> **Arm names in older runs.** Runs and reports written before the v1 clean break call the gud arms `gud1-cc` and `gud1-ref` and their summary block `gud1`; `report.py` reads both. The milestone-0 smoke runs (`runs/20261020-smoke-*`) have an arm called `gud`: that was **v0** gud, now deleted.

## What is the same in every arm

| | every arm |
| --- | --- |
| Agent | Claude Code, headless: `claude -p PROMPT --output-format stream-json --verbose` (`gud-ref`: the reference agent, below) |
| Model | `claude-sonnet-5-5` (recorded per agent from the stream's init event) |
| Built-in tools | `--tools Bash,Read,Edit,Write,Glob,Grep`, pre-approved with `--allowedTools`, `--permission-mode dontAsk` (anything else is denied, nobody is prompted) |
| Isolation | `--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence`: no user settings, hooks, plugins, skills or MCP servers leak in |
| Prompt | Identical preamble (role, peers, task text, ground rules, budget, `ESCALATE:` convention); only the "How this repo works" section differs |
| Budget | Per agent `budget/N`, passed to Claude Code as `--max-budget-usd` **and** enforced by the harness (below) |
| Tests | `python -m pytest -q -x`, with the same venv Python: the git merge gate, or the gud tester |
| Workspace | A temp directory outside this repo; PATH has the venv's `python` (with pytest) first |
| Start | The spec's seed tree |

## The arms

**git** (`git_arm.py`, `prtool.py`)

- One bare repo on local disk (`origin.git`, reached by path). Each agent has its own clone and its own git identity (a private `GIT_CONFIG_GLOBAL`: `user.name=@aK`, `pull.rebase=true`).
- `main` is protected by a pre-receive hook. The only way in is the local PR tool `pr` on every agent's PATH: `pr open`, `pr list`, `pr show`, `pr approve`, `pr comment`, `pr merge`, `pr close`, `pr wait`.
- `pr merge ID` holds one repo-wide lock and lands the PR only if:
  1. it has an approval from an agent other than its author (approvals persist across later pushes, as on GitHub by default);
  2. the branch rebases cleanly onto the current main (otherwise the agent rebases locally, resolves, and force-pushes);
  3. `python -m pytest -q -x` passes on the rebased tree.

  It then fast-forwards main. Every PR action is appended to `prs/events.jsonl`.

**gud** (`gud_arm.py`): a real host (`gud host serve`) and a real tester (`gud tester`) over one fake-transport mail root, driven through the `gud` command line exactly as `tests/v1/e2e/scenario.py` drives it.

| | `gud-cc` | `gud-ref` |
| --- | --- | --- |
| Agent | Claude Code headless, the exact git-arm invocation (model, `--tools`, budget, isolation) plus `--mcp-config` | `python -m gud.agent.runtime` (Claude Agent SDK), one process per member, its own system prompt (`gud/agent/prompt.py`) |
| Prompt | The git arm's preamble word for word; only "How this repo works" differs | The task on the board (spec text plus the "hidden tests check this text" rule); no preamble |
| Working copy | `gud clone` per agent, the agent's cwd | the same |
| gud tools | `python -m gud.cli --home H --handle @aK mcp --wc WC` (the gud MCP server) | the runtime starts the same server per session |
| Stop | the session ends itself (or budget/timeout) | no live push and no agent activity for 90 s once something linked, 30 s after T1 is done, or budget/timeout |

Members are `@a1..@aN` (agents), `@tester`, and `@nelson` (human requester and sponsor, who signs genesis and the task and never acts again). The task `T1` is opened with the spec text and the acceptance check `python -m pytest -q` (`accept` record). Nothing splits the work: there is no lead, and the prompt says only that other agents work on the same repo. `.gud/config`: `window 20s`, `grace 5s`, `lease 10m`, `escalate 3m`, `fix-offer 2m` (and `task-quiet` with `--gud-task-quiet`). `.gud/test` (`--gud-test-policy`): `tip` (default; no push waits for tests, the tester tests the tip after every link, a red tip is bisected and opens a fix task) or `gate` (every push needs a pass, like the git merge gate). After the agents stop, the harness waits for open consent windows to close (`drain`), and with `--wait-done` keeps the host and tester running until T1 is done.

**Spend cap.** `--cap-usd` with `--ledger` (default `runs/spend.json`): `cap.py` keeps one ledger across runs and processes; every run reserves its budget before it starts and commits what it spent, so concurrent cells can never exceed the cap together (a cell gets a smaller budget, or `outcome: skipped_cap`). Inside a run the supervisor kills every agent at the run budget; the reference agents are capped per member by the SDK's `max_budget_usd` and the harness stops them at the run budget.

## Specs and hidden tests

| spec | what collides | hidden tests |
| --- | --- | --- |
| `acme-site` | (no forced collision) the Acme Notes marketing site | 21: files, doctype/title/stylesheet on every page, identical nav, hero, cards, CTA, pricing table, mailto, about story, CSS, media query, local links |
| `todo-cli` | (no forced collision) `todo.py` with argparse subcommands persisting to JSON | 15: exact output lines, ids, errors and exit codes, `--help`, `--file` / `TODO_FILE` |
| `kv-api` | (no forced collision) `kv_server.py` on `http.server` with PUT/GET/DELETE, listing and TTL | 17: status codes and bodies, TTL expiry, 400/404/405, separate stores, `--port` |
| `acme-site-shared` | Five pages through one template (`build.py`), one nav file (`components/nav.html`) and one stylesheet: every page needs a nav link, CSS and `aria-current` support in `build.py` | 28: build runs, five pages, titles, one stylesheet, nav order/texts/`aria-current`, one nav source, per-page content, CSS selectors, media query, links, `site/` not committed |
| `todo-stack` | `todolib.py` (API left to the agents) used by `cli.py` and `api.py`, plus docs that must name every public library function: every part depends on the library's API | 27: CLI and HTTP behaviour, shared store, the same library error message in both front ends, front ends do no file I/O (AST), docs cover the public API, commands and endpoints |
| `refactor-guard` | A seeded notes app; the obvious unicode-slug change in `notes/slug.py` makes the `NOTE_FILE` guard in `notes/cleanup.py` (never named by the task) treat non-ASCII notes as debris, so `gc` deletes them | 23, three of which are the guard (`meta.json`); `hidden/refactor-guard/naive/` is the obvious fix and fails exactly those three while passing the visible tests |

Seeds (`specs/<spec>/seed/`) are the starting trees. Each spec has a reference solution in `hidden/<spec>/reference/`. The unit tests check that the hidden tests pass on the reference and fail on the bare seed, so a low score means the agents' tree is wrong, not the test. The harness copies the final tree into a temp dir and runs the hidden tests there, only after every agent has stopped.

Agents cannot read the hidden tests through normal means. Their workspaces are outside the repo, and the harness passes deny rules for the eval directory to Read/Edit/Write/Glob/Grep. Bash could still read any path, so every tool call is also audited for the hidden-test path (`hidden_access` in the summary).

## Budget, time and harness errors

`agents.supervise` reads each agent's stream-json as it arrives:

- **Live cost** is the sum over assistant messages of `usage` × list price, deduplicated by message id (Claude Code emits one event per content block with the same usage). Sonnet 5.5 prices reproduce Claude Code's own `costUSD` exactly. When the `result` event arrives, its `total_cost_usd` replaces the estimate; it also covers Claude Code's small background Haiku call.
- An agent is killed when its cost reaches `budget/N`. Every agent is killed when the run total reaches `--budget-usd`, or at `--timeout`.
- **Harness errors are not agent results.** An authentication failure, or a required MCP server that does not report `connected`, kills the run and marks it `outcome: harness_error`. `claude auth status` is also checked before starting. Both failure modes were seen in an earlier harness, where an expired login and an MCP server that refused its options each looked like an agent result.

## Metrics (`summary.json`)

Per agent: `turns`, `tokens` (input, output, cache read, cache creation; `ledger` for the reference agent), `cost_usd` (+ `cost_source`), `wall_s`, `exit_code`, `killed`, `tool_counts`, `local_test_runs`, `escalated`, `final_text`.

Per run: `outcome`, `hidden` (passed/total/pass_rate), `spec_checks` (each hidden test), `final_tests_pass` (the repo's own suite on the final tree), `wall_s`, `cost_usd`, `tokens`, `escalations`, `hidden_access`, and these normalized coordination numbers:

| field | git | gud |
| --- | --- | --- |
| `integrations`, `integrations_per_min` | PR merges into main | links (excluding genesis) |
| `test_runs`, `test_runs_per_integration` | merge-gate pytest runs | tester results |
| `conflicts_total`, `conflicts_resolved` | merge-gate rebase conflicts + `CONFLICT` lines in agents' own git output; resolved if the PR (or, for local conflicts, the agent) merged afterwards | pushes that ended superseded, stale, conflict or objected; resolved if the same author landed a later push |

Arm details are under `git` (PRs, approvals, comments, merge rejections by reason, local conflicts, push rejections, direct-to-main attempts) and `gud`. The `gud` block comes from a fresh replay of the run's host log and mail (`analyze.gud_metrics`): links and links/min; pushes by end (`linked`, `linked_carried`, `superseded`, rejected statuses); carried links; colliding pushes; test runs by scope (`tip`, `accept`, `push`, `evidence`) and per link; claims (won, lost, updates, releases, lapsed, final scopes); claim overlaps and how each was settled (split by claim update, released, settled in comments, commented then collided, ignored); comments by target; objections (reason vs evidence, answered by revision or comment); consent escalations and who resolved them (a decider or `default` at the deadline) and task escalations; red-tip episodes, time to green and fix tasks; T1's status.

From the transcripts (`analyze.transcript_analysis`, every arm, git included, under `analysis`): files each agent read (Read, gud `read`, Grep on a file, `cat`/`head`/`sed`/... in Bash), files it changed, reads outside its changes, reads outside its claimed scope (gud), its gud tool counts, for each colliding push whether it called `inflight` or `comment` before it, pushes the working copy refused because the base was stale, and for refactor-guard who read the guard file (`context_check`). Pushes collide when they share a file, neither is in the other's base, and they were open together or the later one was built on a base older than the earlier one's link.

Run artifacts: `agents/<aK>/{prompt.txt,stream.jsonl,tools.json,stderr.log}`, `final_tree/`, `hidden_tests.log`, `final_tests.log`, `argv.json`, and `prs/` + `main.log` (git) or the host directory's log and mail (gud). Only `summary.json` files and reports are committed.

## Results (2026-10-20)

All runs used `claude-sonnet-5-5`, N=4 and a $1.20 run budget, unless noted. Every run in every arm passed every hidden test, so pass rate does not separate the arms at this N.

- **Milestone-0 smoke** ([runs/20261020-smoke-report.md](runs/20261020-smoke-report.md)): N=2, one trial, git against **v0** gud on acme-site, todo-cli and kv-api. It showed that the harness works end to end; nothing more.
- **First v1 grid** ([runs/20261020-v1-report.md](runs/20261020-v1-report.md)): the three coordination-forcing specs × git, gud-cc and gud-ref × 2 trials, $8.01. The context check passed in every arm. gud-ref runs were one agent claiming `**` and doing the whole task. gud-cc agents coordinated in comments around a single claim, never with subtasks or claim updates, and their wall time was inflated by `wait` calls that no event ever ended. Objections, escalations and red tips never occurred. The report lists the gud issues found, with repros.
- **Coordination fixes re-run** ([runs/20261020-v1-fixes-report.md](runs/20261020-v1-fixes-report.md)): the gud arms after three fixes: done is declared (SPEC §16.6), every agent follows the tip (§19.2), and claimants split before they build. $7.91 of a $12 batch cap. On todo-stack, both gud arms split T1 into three subtasks in every run, and four authors landed work. No `wait` stalled. gud-cc wall time fell from 466 s (369–562) to 78 s (58–98). One gap stayed open: on the unsplit specs, gud-cc runs often ended with all the work linked and T1 never declared done.
- **Done by quiescence** (`runs/20261020-v1-quiet/`, summaries only): the three gud-cc cells where T1 was never done, re-run with SPEC §16.6 quiescence. T1 became done in all three (acme-site-shared 150 s and 124 s, refactor-guard 130 s; $1.60 together).

## Limitations of the comparison

- **Small samples.** One or two trials per cell at N=4. These runs show that the harness works and where gud's coordination broke; they do not rank the systems. The plan's cells are N=4/8/16 with at least 3 trials each.
- **The git arm was not re-run** after the gud fixes; its column is the first grid's.
- **Different review policies, by design.** Git needs one approval from another agent before any merge. gud only asks the owners of rewritten lines, by lazy consensus in a 20 s window, so new files land on consent alone. With a small N a git PR can stall if the peer stops before reviewing it. The prompt asks agents to check for pending reviews before they stop.
- **Local transports only.** git over a file path and gud over the fake mail root have no network latency, authentication or email delivery delay. Live email timings for Primitive and IMAP are in `docs/TRANSPORTS.md` and the live end-to-end smoke in SPEC Appendix A.8.
- **Cost is Claude Code's list-price figure.** The account is a subscription, so this is not a bill. A killed agent's cost is the usage estimate, which misses its background Haiku call (well under $0.01).
- **The conflict metric is approximate for git.** Local conflicts are counted from `CONFLICT` lines in Bash output and counted as resolved if that agent merged afterwards. Stream-json carries no per-call timestamps.
- **Hidden tests are protected by isolation and an audit, not a sandbox.** Bash can read any path.
