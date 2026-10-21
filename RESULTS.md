# gud v1 results

Measured during the v1 build, 2026-10-20. Each section names its source. Live email runs used Primitive. Local runs used the fake transport (an on-disk mail system) and GreenMail 2.1.3 (a real IMAP+SMTP server).

## Test suite

| | Tests |
| --- | ---: |
| Full suite (`python -m pytest -q`) | **1022 passed**, 58 skipped (live-provider variants that need credentials) |
| Eval harness (`evals/v1/tests`) | 40 passed |
| Scale benchmark (`-m slow`) | passes (see "Replay at scale") |

The suite covers the protocol, replay, consent and tasks; every transport against its conformance checks; events and the host; the working copy and CLI; the tester; the MCP server and reference agent; the git bridge; ops; the view API; and end-to-end runs across separate processes.

## Transports

All 11 conformance checks (SPEC §20.2) pass on every adapter. Details are in [docs/TRANSPORTS.md](docs/TRANSPORTS.md).

| Transport | Where | `wait` after delivery | push intake | 25 MiB blob |
| --- | --- | --- | --- | --- |
| **primitive** | **live** | **0.60 s** | **0.56 s** (long-poll) | 4 chunks; a 9.9 MB message delivered intact |
| imap_smtp | GreenMail | 0.11 s | 0.27 s (IDLE); reconnect 0.98 s | 4 chunks |
| fake | local | 0.004 s | 0.005 s | 4 chunks |
| jmap | API mock | 0.35 s | 0.36 s (EventSource) | 4 chunks |
| ses | API mock | 0.005 s | 0.004 s (SNS) | 4 chunks |
| postmark | API mock | 0.002 s | 0.003 s (webhook) | 4 chunks |
| gmail | API mock | 0.008 s | 0.005 s (Pub/Sub) | 4 chunks |

On Primitive, every envelope arrived byte-exact: hostile bytes, a 7 MiB envelope, dedupe, ordering, and resume after a restart. Primitive refused the forged-From submission at send time.

## End to end, across separate processes

One scenario runs a host, members, a tester and the reference agent, each as its own process. It covers:
- a new file and an edit to owned lines;
- a race;
- an objection that carries a failing test, and the revision that answers it;
- a red tip, its fix task, and the return to green;
- a task that completes only on green;
- `verify --from-genesis` on every member;
- a git mirror checked with `git fsck`.

| Transport | Result | New file linked | Review request at the owner's webhook | Other |
| --- | --- | --- | --- | --- |
| fake | pass, ~65 s | 0.9 s | 0.6 s | race linked 10.9 s; red → green 8.9 s; 12 links mirrored to git |
| GreenMail IMAP+SMTP (IDLE) | pass, 124 s, 35 emails | 2.1 s | 1.0 s | red → green 19.3 s |
| **Primitive, live** | **pass, 72 s, 18 emails** | **2.9 s** | **2.3 s** | approved edit 5.9 s; objected push revised and linked in 29.4 s (20 s window + 8 s grace) |

Host migration to a new repo address also passes end to end: the handover, members following it, the host-line push, and pushing afterwards.

## Event pipeline

Each hop, from mail reaching the repo to the concerned agent's webhook:

| Path | received → logged | logged → replay delta | delta → journal | journal → agent | Total |
| --- | --- | --- | --- | --- | --- |
| fake | 0.9 ms | 3.7 ms | 0.2 ms | 14.7 ms | **~21 ms** |
| GreenMail IMAP IDLE (median of 5) | 1.7 ms | 2.1 ms | 0.3 ms | 14.7 ms | **64 ms** (including 44 ms SMTP → IDLE) |
| Primitive webhook (mock HTTP) | | | | | 19–33 ms |

The target was under 5 s.

## Replay at scale

The benchmark replays 10,000 links over a 2,000-file tree:

| | Result |
| --- | --- |
| One new email, fed incrementally | **5.0 ms** |
| Memory retained | **125 MiB** (against ≥ 1.9 GiB if every link kept its own tree) |
| Full incremental replay | 75 s (7.5 ms per link) |

Replay is deterministic in every case tested: shuffled delivery order, random batch splits, restart from saved state, and resume from a checkpoint.

## Real-agent evals

Real Claude Code agents (`claude-sonnet-5-5`, N=4, $1.20 per run) worked on three specs built to make them collide. Each arm uses one coordination layer:
- **git:** a protected `main`, PRs, one approval and a pytest merge gate.
- **gud-cc:** headless Claude Code on the gud MCP server, with the git arm's exact prompt preamble.
- **gud-ref:** the reference agent.

Hidden acceptance tests that no agent sees score the final tree. Sources: [evals/v1/runs/20261020-v1-report.md](evals/v1/runs/20261020-v1-report.md), [evals/v1/runs/20261020-v1-fixes-report.md](evals/v1/runs/20261020-v1-fixes-report.md) and `evals/v1/runs/20261020-v1-quiet/`.

**Every run in every arm passed every hidden test**: 28/28, 27/27 and 23/23. That includes the refactor whose obvious fix breaks a guard in a file the task never names. In every arm, 3–4 agents read that file.

### Wall time and cost (current v1)

| Spec | git | gud-cc | gud-ref |
| --- | --- | --- | --- |
| todo-stack: library + CLI + HTTP API + docs | 184 s (128–240), $0.69 | **78 s (58–98)**, $0.77 | 116 s, $0.90 |
| acme-site-shared: five pages sharing nav and styles | 278 s (261–295), $0.52 | **94 s (84–104)**, $0.69 | 123 s (99–146), $0.38 |
| refactor-guard: feature plus an unnamed guard | 99 s (71–127), $0.54 | **67 s**, $0.51 | 122 s, $0.47 |

### Coordination on todo-stack (current v1)

| | git | gud-cc | gud-ref |
| --- | --- | --- | --- |
| Authors who landed work | 3.5 | **4** | **4** |
| Subtasks posted | — | 3 | 3 |
| Claims won per run | — | 4 | 4 |
| Work that never landed | 1–2 PRs | 0 | 0 |

In every todo-stack run, the first claimant took the library and posted the CLI, HTTP API and docs as subtasks. Three other agents each claimed one. They landed four links with no colliding pushes.

### Task completion

T1 became done only after its work had landed, in all three runs re-run with done-by-quiescence (SPEC §16.6). Each of the three paths fired:

| Run | How T1 became done | Last link → done |
| --- | --- | --- |
| acme-site-shared t1 | the holder declared it | 1.9 s |
| acme-site-shared t2 | the holder declared it after the "others landed work" hint | 3.8 s |
| refactor-guard t1 | quiescence, after the holder's agent budget ran out | 120 s (exactly `task-quiet`) |

### How v1 got here

The first v1 grid led to three protocol improvements, which a second grid measured:
- **Done is declared.** A task finishes when its claim holder declares it, then passes acceptance on green.
- **Every agent follows the tip.** Each link reaches every agent, and `wait` returns when the tip moves.
- **Claimants split first.** A claimant splits separable work into subtasks before building.

With those, gud-cc on todo-stack went from 466 s to 78 s, and gud-ref went from one author to four.

### Scope

- Two trials per cell (one on refactor-guard) at N=4.
- Local transports.
- The git arm comes from the first grid.
- Cost is Claude Code's list-price figure, on a subscription account.
- Total live eval spend across all batches was about $25.

These numbers show how the systems behave on these specs. A ranking needs the larger grid in the plan: N=4/8/16 with at least 3 trials each.
