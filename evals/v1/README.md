# v1 real-agent eval: git vs gud

Milestone 0 of [docs/v1/PLAN.md](../../docs/v1/PLAN.md). A swarm of real coding agents builds a complete repo
from a task spec, once with git as the coordination layer and once with gud. Hidden acceptance tests,
which no agent ever sees, score the final tree. Everything else is held fixed.

```bash
# both arms, one trial each, 2 agents, $4 per run, 25 minutes per run
.venv/bin/python evals/v1/run.py --arm both --spec acme-site --agents 2 --trials 1 --budget-usd 4 --timeout 1500

# aggregate any set of runs: mean (min–max) per spec, N and arm
.venv/bin/python evals/v1/report.py evals/v1/runs/<stamp>

# harness tests (no model calls; a fake agent makes deterministic edits)
.venv/bin/pytest -q evals/v1/tests
```

`run.py` options: `--arm git|gud|both`, `--spec acme-site|todo-cli|kv-api`, `--agents N`, `--trials K`,
`--budget-usd X` (per run; each agent gets X/N), `--timeout S` (wall clock per run), `--out DIR`
(default `evals/v1/runs/<stamp>/`), `--model` (default `claude-sonnet-5-5`; `''` lets the CLI choose and the
model is recorded from the session's init event), `--window` (gud consent window, default 30 s),
`--keep-work`, `--agent-cmd` (swap the agent binary; the tests use `tests/fake_agent.py`).

## What is the same in both arms

| | both arms |
| --- | --- |
| Agent | Claude Code, headless: `claude -p PROMPT --output-format stream-json --verbose` |
| Model | `claude-sonnet-5-5` (recorded per agent from the stream's init event) |
| Built-in tools | `--tools Bash,Read,Edit,Write,Glob,Grep`, pre-approved with `--allowedTools`, `--permission-mode dontAsk` (anything else is denied, nobody is prompted) |
| Isolation | `--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence`: no user settings, hooks, plugins, skills or MCP servers leak in |
| Prompt | Identical preamble (role, peers, task text, ground rules, budget, `ESCALATE:` convention); only the "How this repo works" section differs |
| Budget | Per agent `budget/N`, passed to Claude Code as `--max-budget-usd` **and** enforced by the harness (below) |
| Tests | `python -m pytest -q -x` gates every integration (gud tester / git merge gate), with the same venv Python |
| Workspace | A temp directory outside this repo; PATH has the venv's `python` (with pytest) first |
| Start | The same seed tree: a README stub and one trivial passing test |

## What differs: the coordination layer

**git** (`git_arm.py`, `prtool.py`)

- One bare repo on local disk (`origin.git`, reached by path). Each agent has its own clone and its own git
  identity (a private `GIT_CONFIG_GLOBAL`: `user.name=@aK`, `pull.rebase=true`).
- `main` is protected by a pre-receive hook. The only way in is the local PR tool `pr` on every agent's PATH:
  `pr open`, `pr list`, `pr show`, `pr approve`, `pr comment`, `pr merge`, `pr close`, `pr wait`.
- `pr merge ID` holds one repo-wide lock and lands the PR only if:
  1. it has an approval from an agent other than its author (approvals persist across later pushes, as on
     GitHub by default);
  2. the branch rebases cleanly onto the current main (otherwise the agent rebases locally, resolves, and
     force-pushes);
  3. `python -m pytest -q -x` passes on the rebased tree.

  It then fast-forwards main. Every PR action is appended to `prs/events.jsonl`.

**gud** (`gud_arm.py`, `gud_tester.py`)

- Today's v0 gud over the fake mailbox (`GUD_MAILBOX=fake`, a JSONL file).
- Members are `@a1..@aN` (agents, each with its own Ed25519 key), `@tester`, `@seed` and `@nelson`.
  `@seed` is a console role: it signs genesis and the task email but does not vote, so seed lines need
  nobody's consent. `@nelson` is the human requester and never acts.
- Agents use the gud MCP server (`python -m gud.mcp_server`) with their handle and key in the prompt. Their
  working directory is an empty scratch dir; they `export` the tip (plus pending edits) into it to run tests.
- A separate tester process (`gud_tester.py`) runs `gud.tester.Tester` with the local runner and replies to
  every push. It is wrapped so that one failed step is logged and retried instead of stopping the tester.
- **No lead.** The task goes out as one signed `task` email (`T1`), which agents see with `tasks`. In v0 an
  agent cannot claim, split or comment on work, so coordination is limited to push intents, `status`/`wait`
  and approve/object replies. Milestone 1a (leaderless coordination) is what changes this.

## Specs and hidden tests

| spec | task | hidden tests (`hidden/<spec>/test_hidden.py`) |
| --- | --- | --- |
| `acme-site` | The Acme Notes marketing site (the `livetest/run_live.py` task text) | 21: files exist, doctype/title/stylesheet on every page, nav links every page and is identical across pages, hero, three cards, CTA to pricing outside the nav, a real `<table>` with Free/Pro/Team and $8 / $15/user/mo, mailto link, about story, CSS for each component, a ≤700px media query, local links resolve |
| `todo-cli` | `todo.py` with argparse subcommands add/list/done/remove persisting to JSON | 15: exact output lines, ids, done/remove, unknown id → stderr and exit 1, usage errors exit 2, `--help`, `--file` / `TODO_FILE` / `./todos.json`, argparse |
| `kv-api` | `kv_server.py` on `http.server` with PUT/GET/DELETE, key listing and TTL | 17: status codes and JSON bodies, 201 vs 200, 204 delete, sorted key list, TTL expiry, PUT clears an old TTL, 400 on bad bodies, 404/405 elsewhere, separate stores per server, the `--port` CLI |

Seeds (`specs/<spec>/seed/`) hold only a README stub and `tests/test_smoke.py`. Each spec has a reference
solution in `hidden/<spec>/reference/`. The unit tests check that the hidden tests pass on the reference and
fail on the bare seed, so a low score means the agents' tree is wrong, not the test. The harness copies the
final tree into a temp dir and runs the hidden tests there, only after every agent has stopped.

Agents cannot read the hidden tests through normal means. Their workspaces are outside the repo, and the
harness passes deny rules for the eval directory to Read/Edit/Write/Glob/Grep. Bash could still read any
path, so every tool call is also audited for the hidden-test path (`hidden_access` in the summary).

## Budget, time and harness errors

`agents.supervise` reads each agent's stream-json as it arrives:

- **Live cost** is the sum over assistant messages of `usage` × list price, deduplicated by message id (Claude
  Code emits one event per content block with the same usage). Sonnet 5.5 prices reproduce Claude Code's own
  `costUSD` exactly. When the `result` event arrives, its `total_cost_usd` replaces the estimate; it also
  covers Claude Code's small background Haiku call.
- An agent is killed when its cost reaches `budget/N`. Every agent is killed when the run total reaches
  `--budget-usd`, or at `--timeout`.
- **Harness errors are not agent results.** An authentication failure, or a required MCP server that does not
  report `connected`, kills the run and marks it `outcome: harness_error`. `claude auth status` is also
  checked before starting.

### Why `livetest/run_claude.py` failed (exit 1 in 4.4 s)

`livetest/runs/20261017-210957-claude-plain/claude.jsonl` holds one synthetic assistant message,
`"Failed to authenticate: OAuth session expired and could not be refreshed"` (`error: authentication_failed`,
0 tokens, $0, 78 ms), and then the CLI exited 1. The Claude Code login had expired. Nothing in gud or the
prompt failed. The old harness counted this as a failed agent run.

A second, silent bug sat behind it. That run's `mcp.json` launched `python -m gud.mcp_server --handle @auth-agent`,
and the current server rejects `--handle` and exits 2. Re-running that exact invocation today (auth now
valid) gives `mcp_servers: [{"name": "gud", "status": "failed"}]`. Claude then answers "the gud MCP server
failed to connect" and exits **0**. The same call without `--handle` connects and `status` works.

The new harness handles both. Auth errors become a harness error at the first event, MCP status is checked
from the init event, and the MCP config never passes `--handle`.

## Metrics (`summary.json`)

Per agent: `turns`, `tokens` (input, output, cache read, cache creation), `cost_usd` (+ `cost_source`),
`wall_s`, `exit_code`, `killed`, `tool_counts`, `local_test_runs`, `escalated`, `final_text`.

Per run: `outcome`, `hidden` (passed/total/pass_rate), `spec_checks` (each hidden test), `final_tests_pass`
(the repo's own suite on the final tree), `wall_s`, `cost_usd`, `tokens`, `escalations`, `hidden_access`, and
these normalized coordination numbers:

| field | git | gud |
| --- | --- | --- |
| `integrations`, `integrations_per_min` | PR merges into main | links (excluding genesis) |
| `test_runs`, `test_runs_per_integration` | merge-gate pytest runs | tester replies |
| `conflicts_total`, `conflicts_resolved` | merge-gate rebase conflicts + `CONFLICT` lines in agents' own git output; resolved if the PR (or, for local conflicts, the agent) merged afterwards | pushes that ended superseded, stale, conflict or objected; resolved if the same author landed a later push |

Arm details are under `git` (PRs, approvals, comments, merge rejections by reason, local conflicts, push
rejections, direct-to-main attempts) and `gud` (links/min, emails and emails per link, emails by kind,
proposal statuses, objections, approvals, `rebase` tool conflicts, push tool errors, link list).

Run artifacts: `agents/<aK>/{prompt.txt,stream.jsonl,tools.json,stderr.log}`, `final_tree/`,
`hidden_tests.log`, `final_tests.log`, `argv.json`, and `prs/` + `main.log` (git) or `mailbox.jsonl` +
`tester.log` (gud).

## Milestone-0 smoke runs (2026-10-20)

These runs used N=2 agents, one trial per arm and `claude-sonnet-5-5`, with $4 per run for acme-site and $3
for the others. No agent hit a budget or the timeout, and no run had a harness error. Full tables are in
[runs/20261020-smoke-report.md](runs/20261020-smoke-report.md). Live runs cost $1.05 in total.

| spec | arm | hidden tests | wall | cost | integrations | test runs | conflicts |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| acme-site | git | 21/21 | 60 s | $0.17 | 1 merge (2 PRs opened, 1 left redundant) | 1 | 0 |
| acme-site | gud | 21/21 | 202 s | $0.19 | 2 links | 3 (1 failed) | 0 |
| todo-cli | git | 15/15 | 30 s | $0.15 | 1 merge (both agents wrote todo.py, one PR left open) | 1 | 0 |
| todo-cli | gud | 15/15 | 220 s | $0.15 | 2 links | 2 | 1 superseded, then re-pushed as tests only |
| kv-api | git | 17/17 | 186 s | $0.19 | 2 merges | 2 | 1 local rebase conflict, resolved |
| kv-api | gud | 17/17 | 100 s | $0.21 | 2 links | 2 | 0 (one push refused: file already existed) |

What these runs show, and no more:

- The harness runs end to end in both arms on all three specs.
- Agents in both arms split the work by announcing it: in PR titles for git, in push intents for gud.
- Every final tree passed every hidden test.

At N=2 and one trial, the wall-time and cost differences are noise-level. The gud wall times are inflated by
the `wait` id quirk below: in the acme-site gud run, every `wait` call ran to its timeout, and one `rebase`
returned `unknown push`. In these runs gud agents rarely ran tests themselves and relied on the tester instead
(one `export` + pytest in six gud sessions, against 12 local pytest runs in the git sessions).

## Limitations of the comparison

- **The two arms have different communication channels.** Git agents can comment freely on PRs. v0 gud agents
  can only write push intents and approve/object, and they have no claim or split tool. That is today's v0,
  measured as it is, not a design choice of this eval.
- **Different review policies, by design.** Git needs one approval from another agent before any merge. gud
  only asks the owners of rewritten lines, by lazy consensus in a 30 s window, so new files land on a passing
  test alone. With N=2 a git PR can stall if the peer stops before reviewing it. The prompt asks agents to
  check for pending reviews before they stop.
- **Different editing ergonomics.** Git agents edit files in their clone. gud agents edit through MCP old/new
  edits and use `export` to run tests. This is part of each coordination layer, but it means the comparison
  measures tool ergonomics as well as protocol.
- **Local transports only.** git over a file path and gud over the fake JSONL mailbox have no network
  latency, authentication or email delivery delay. Live email timings for Primitive and IMAP are in the transport docs.
- **Cost is Claude Code's list-price figure.** The account is a subscription, so this is not a bill. A killed
  agent's cost is the usage estimate, which misses its background Haiku call (well under $0.01).
- **Small samples.** The milestone-0 smoke runs are N=2 with one trial per arm. They show that the harness
  works, not which system is better. The plan's cells are N=4/8/16 with at least 3 trials each.
- **The conflict metric is approximate for git.** Local conflicts are counted from `CONFLICT` lines in Bash
  output and counted as resolved if that agent merged afterwards. Stream-json carries no per-call timestamps.
- **Hidden tests are protected by isolation and an audit, not a sandbox.** Bash can read any path.
- **gud v0 quirks seen in transcripts:** `wait` needs the push id with its angle brackets (`<...@gud.local>`).
  Without them it doesn't match and waits out its timeout, even after the push has landed.
- `gud.mail_fake.FileMailbox` does not take the lock while reading and releases it before the file is closed.
  A multi-process stress test (4 writers × 25 sends, bodies from 3 KB to 26 KB, 2 readers) found no torn
  lines, so gud core was not changed.
