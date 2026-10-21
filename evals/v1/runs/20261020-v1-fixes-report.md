# gud v1 coordination fixes: real-agent re-run (2026-10-20)

The first v1 grid ([20261020-v1-report.md](20261020-v1-report.md)) found three coordination problems in gud v1:

1. T1 was done after its first link.
2. Agents with no claim were never woken by other agents' links.
3. N reference agents acted as one: one claimed `**` and the rest stood down.

This report re-runs the gud1 arms after the fixes, with the same harness, specs, model (`claude-sonnet-5-5`), N=4,
$1.20 run budget, 20-minute timeout and ledger. The git arm was not re-run, so its column is the first grid's
result. Each run's `summary.json` is in `runs/20261020-v1-fixes-grid/`. Raw transcripts are kept locally and are
not committed.

**Spend: $7.91 of the $12 batch cap.** The cap was enforced by `cap.py` on the shared ledger `runs/v1-spend.json`,
with `--cap-usd 26.00` (the $14.01 already spent plus $12). The $7.91 is all of it, including cells that were
discarded (below).

| | runs | $ |
| --- | ---: | ---: |
| the 10 cells in the tables below | 10 | 6.46 |
| `gud1-cc` acme-site-shared t1, first attempt: superseded after the declaration fix (`runs/20261020-v1-fixes-grid-superseded/`) | 1 | 0.67 |
| harness errors, re-run (`*-harness-error/`): four t2 cells with the API unreachable (ENOTFOUND, no turn completed), and `gud1-cc` todo-stack t1, which ran while the machine was suspended for about 18 minutes (a 1,072 s gap between host-log ticks) | 5 | 0.77 |

## What changed (gud, commits on this branch)

| problem | spec | code | tests |
| --- | --- | --- | --- |
| 1. done on the first link | §16.6: a root task with no subtasks is work-done only on its holder's **done declaration**, or a `no-change`. A declaration is a claim update or release with `Gud-Done: yes`, sent with no live push for the task. Criteria must then pass on a link at or after the declaration. Subtasks keep "first link"; a task with subtasks still follows its subtasks. §7 adds the `Gud-Done` header. §16.4 note: claims on a task and on its own subtasks don't overlap. | `core/tasks.py` (`_declare`, `_work_done`, `_nested`), `core/replay.py`, `core/envelope.py`, `core/types.py` (`declared`, `declared_by`); `gud1 claim update --done`, `gud1 release --done`; MCP `claim_update(done=true)` and `release(done=true)`, which wait for the member's own pushes to link | `tests/v1/replay/test_tasks.py`: the eval repro (a README-only link with an accept command that already passes) and the declaration rules; CLI, MCP, tester, e2e and view tests updated |
| 2. no wake-up from others' links | §19.2: `linked` and `task.done` concern every agent (`tip.*` already did) | `replay._linked_concerns`, `TaskBoard._finish`. The dispatcher caps webhook posts per member per pass (64) and re-pumps while a backlog remains. MCP `wait` also returns when its own sync sees another member's link (`tip_moved`). In the reference agent, other members' links, `tip.green` and unrelated `task.done` are *ambient*: quiet, and coalesced into one debounced board check (3 s, at most 15 s). A `task.done` that a held task depends on (`after`) wakes that task's session. | the eval repro: @a1 claims `todo.py`, @a2 holds nothing and waits for `linked`; it now returns on @a1's link. Also a human's wait ending on a tip move, dispatcher fairness, ambient coalescing (`test_runtime.py`), and dependency wake-ups |
| 3. N agents act as one | Implementation notes (coordination) 6 | Reference-agent prompt: split separable work into subtasks (files, `after`) before implementing when others are idle; post your own part first and claim it; idle agents claim open subtasks. Turn text names the idle agents. MCP (the only channel to gud1-cc; its preamble is unchanged): `tasks` lists idle agents and open subtasks; `claim` with `**` on an unsplit task while others are idle returns a `warning` naming them; refusals point at open subtasks | MCP warning, team line and refusals; scripted-backend runtime tests: a planner splits, two idle agents take the parts and T1 completes through its subtasks |
| found in this re-run | coordination notes 1 and 6 | MCP: refusals and linked pushes name the subtasks nobody holds; `release` on a claim that ended with its task says so (it used to say "not a claim"). `done` declares the *whole* task: on a root task it is refused while other members visibly work on it (their open pushes, or their comments since the claim won); `whole=true` confirms. | two regression tests in `tests/v1/mcp/test_tools.py` built from the transcripts |

The gud1-cc "How this repo works" section changed in one sentence, which describes the done rule as a fact. The
shared preamble is byte-identical to the git arm's (`test_gud1_cc_prompt_shares_the_preamble`). The full suite
passes: 1236 passed, 59 skipped (`pytest -q --deselect tests/test_mcp_baseline.py`; 1222 before, plus 14 new
tests). `evals/v1/tests`: 41 passed.

## Before and after

"Before" is `runs/20261020-v1-grid/`, two trials per cell. "After" is two trials on acme-site-shared and todo-stack
and one on refactor-guard. Each cell is mean (min–max). Every run, before and after, passed every hidden test, so
pass rate still doesn't separate anything.

### todo-stack (library + CLI + API + docs)

| | git (before) | gud1-cc before | gud1-cc after | gud1-ref before | gud1-ref after |
| --- | --- | --- | --- | --- | --- |
| hidden tests passed | 27/27 ×2 | 27/27 ×2 | 27/27 ×2 | 27/27 ×2 | 27/27 ×2 |
| wall time (s) | 184 (128–240) | 466 (369–562) | **78 (58–98)** | 92 (83–101) | 116 |
| cost | $0.69 | $0.53 | $0.77 | $0.24 | $0.90 |
| distinct authors who landed work | 3.5 | 4 | 4 | **1** | **4** |
| subtasks posted | — | 0 | **3** | 0 | **3** |
| claims won per run | — | 1 | 4 | 1 | 4 |
| claim overlaps | — | 0 | 0 | 0 | 0 |
| `wait` timeouts | — | 1–2 per cell, 300–600 s each (first report) | **0** | not recorded | 0 |
| T1 opened → done (s) | — | 58 (28–88) | 54 | 55 | 50 |
| last agent link (s) | — | 457 (365–550) | 71 (49–94) | 51 | 43 |
| T1 done only after all agent links | — | **0/2** (3 links after done) | 1/2 (one clean-up link after done) | 2/2 | 2/2 |
| how T1 became done | — | first link ×2 | all subtasks done ×2 | first link ×2 | all subtasks done ×2 |

### acme-site-shared (five pages, one template, one nav, one stylesheet)

| | git (before) | gud1-cc before | gud1-cc after | gud1-ref before | gud1-ref after |
| --- | --- | --- | --- | --- | --- |
| hidden tests passed | 28/28 ×2 | 28/28 ×2 | 28/28 ×2 | 28/28 ×2 | 28/28 ×2 |
| wall time (s) | 278 (261–295) | 247 (123–371) | **94 (84–104)** | 121 (105–137) | 123 (99–146) |
| cost | $0.52 | $0.56 | $0.69 | $0.25 | $0.38 ($0.14–$0.62) |
| distinct authors who landed work | 1.5 | 2 (1–3) | 2.5 (2–3) | 1 | 1 |
| subtasks posted | — | 0 | 0 | 0 | 1 (0–2) |
| claims won per run | — | 1 | 1 | 1 | 3 (1–5) |
| claim overlaps | — | 0 | 0 | 0 | 0 |
| `wait` timeouts | — | 1–2 per cell, 300–600 s | 2 per run, 30 s and 60 s (timeouts the agents chose) | not recorded | 0 |
| T1 opened → done (s) | — | 65 (59–70) | not done | 63 | 87 (62–111) |
| T1 done only after all agent links | — | 1/2 | — (not done) | 2/2 | 2/2 |
| how T1 became done | — | first link ×2 | **not done ×2** | first link ×2 | declared ×1, subtasks ×1 |

### refactor-guard (one trial after)

| | git (before) | gud1-cc before | gud1-cc after | gud1-ref before | gud1-ref after |
| --- | --- | --- | --- | --- | --- |
| hidden tests passed (incl. 3 guard tests) | 23/23 ×2 | 23/23 ×2 | 23/23 | 23/23 ×2 | 23/23 |
| wall time (s) | 99 (71–127) | 482 (328–636) | **67** | 129 (107–152) | 122 |
| cost | $0.54 | $0.40 | $0.51 | $0.28 | $0.47 |
| distinct authors who landed work | 1 | 1 | 1 | 1 | 1 |
| subtasks / claims won / overlaps | — | 0 / 1 / 0 | 0 / 1 / 0 | 0 / 1 / 0 | 0 / 2 / 0 |
| `wait` timeouts | — | 1–2, up to 600 s | 0 | not recorded | 0 |
| how T1 became done | — | first link ×2 | **not done** (holder never declared) | first link ×2 | `no-change` from the holder after the work linked |
| context check (guard read, guard tests pass) | yes ×2 | yes ×2 | yes (all 4 read it) | yes ×2 | yes (all 4 read it) |

## What the numbers show

1. **Leaderless splitting happens where the task has separable parts.** On todo-stack both gud1 arms split T1 in
   all four runs. In each run the first claimant claimed T1 for the library only and posted three subtasks (CLI,
   HTTP API, docs). Then three other agents each claimed one
   subtask. Four distinct authors landed four links, with no claim overlaps and no colliding pushes. Before, the
   reference arm was one agent doing everything (1 author, 0 subtasks in every run). On acme-site-shared and
   refactor-guard the claimant mostly judged the task one piece: claimed `**` (reference arm) or claimed its part
   without subtasks (Claude Code arm). The `**` warning didn't change that. In one acme reference run, two
   subtasks were posted, but the T1 claimant pushed the whole site, and the subtask holders closed them with
   `no-change`.
2. **The 300–600 s stalls are gone.** No `wait` hit a long timeout in any of the 10 runs. The only timeouts were
   four short waits (30 s and 60 s, as the agents asked) in the two acme-site-shared gud1-cc runs. gud1-cc wall time fell from 466 → 78 s on
   todo-stack, 247 → 94 s on acme-site-shared and 482 → 67 s on refactor-guard. On todo-stack and
   acme-site-shared it is now well below the git arm's 184 s and 278 s. Cost per run rose ($0.53 → $0.77 on
   todo-stack), because agents that used to sit in a dead `wait` now get woken and spend tokens.
3. **"Done" no longer comes before the work.** Before the fix, gud1-cc todo-stack was done at 28–88 s while links
   kept landing until 365–550 s (3 links after done). After it, every run that reached done did so through the
   subtasks or through a declaration, after all the subtask work had linked. The exception is one clean-up link
   to `api.py` 39 s after done, which tidied a part that had already landed. The cost is that **T1 is not always
   done any more**. In all three gud1-cc runs on unsplit tasks the work landed, but nobody holding T1 declared
   it done. The holder had claimed only its own files, or another agent had done the work. In the superseded acme
   run, the holder declared done 29 s in, meaning "my part". The MCP guard now refuses that.
4. **The reference arm costs more now that four agents work.** It cost $0.90 against $0.24 on todo-stack, while
   wall time stayed about the same (116 s against 92 s). Each agent is also woken by `tip.red`, `fix.opened` and
   `subtask.posted`. Both todo-stack reference runs had one red-tip episode. The CLI linked 5–6 s before the
   library it imports, and the tip was green again 8–10 s later, when the library landed. These are the first red tips in any v1 grid run. The fix-task flow ran end to end: offer, bisect and green.

## Gud bugs found in this re-run (fixed, with regression tests)

- **Refusals pointed the wrong way, and finished claims said "not a claim".** In gud1-cc todo-stack (the first,
  suspended attempt), @a3 was refused on the held subtask T1.2. It was told to wait for @a4 "to split it", and it
  never noticed the open CLI subtask T1.1. @a4 then called `release` on its finished subtask and got "T1.2 is not
  a claim". Both agents stopped with T1.1 unclaimed, so 10/27 hidden tests passed at the timeout (the cell is
  discarded as a harness error, because the machine was suspended during it). Commit `41198b7`: refusals and
  linked pushes now list the open subtasks nobody holds, and a claim that ended with its task says so. The re-run
  of that cell landed four authors through three subtasks in 58 s.
- **`release(done=true)` read as "my part is done".** In gud1-cc acme-site-shared t1, @a2 claimed T1 for two pages
  and pushed them. It called `release(done=true)` while @a1's comment on its claim said @a1 was building the nav
  and three more pages. T1 was declared done at 29 s with most of the site missing, and @a2 then commented that it
  had done so "by mistake". Commit `80a7909`: on a root task, `done` is refused while other members have open
  pushes for it or have commented on it or on the claim since it won. The refusal says to release without done,
  or to split the task, and `whole=true` confirms. Only the affected cell was re-run ($0.71): the holder released
  without done, and T1 stayed open after all the work landed (row "not done" above).

## Still unresolved

- **Nobody declares an unsplit task done** when the work is spread across agents who don't hold T1, which is the
  usual gud1-cc pattern on acme-site-shared and refactor-guard. The claim lapses after `lease` (10 min). Then
  anyone can claim T1 and declare it, but in these runs the agents stopped first. Possible next steps: an MCP hint
  in `wait`/`push` results to T1's holder when others' links carry `Gud-Task=T1`, or a requester-side "accept the
  tip" path for an unclaimed task.
- **Splitting depends on the model's judgement.** Five of the eight runs on the two separable specs split T1: all four
  todo-stack runs and one of four acme-site-shared runs, where the claimant then pushed the whole site itself. On
  acme-site-shared both arms still mostly put one agent on the shared template, nav and stylesheet.
- **The cost of the extra wake-ups is not yet tuned.** Every reference agent takes a turn for `tip.red`,
  `fix.opened` and every `subtask.posted` batch, and about half of those turns end with "nothing for me".
- **Cost of turns in flight when the harness stops is lost.** In gud1-ref acme t2, three agents were still in
  their first turn, waiting for subtasks that never came, when the run settled at 99 s. Their spend was never
  reported, so that run's $0.14 is a lower bound.
- **Small samples.** There are two trials per cell, one on refactor-guard, and pass rates are at the ceiling.
  Four of the 10 cells after the fixes ran before the two re-run fixes `41198b7` and `80a7909`:
  gud1-ref todo t1, acme t1 and refactor t1, and gud1-cc refactor t1. The rest ran after `41198b7`, and the
  gud1-cc acme t1 re-run ran after `80a7909`.

## Full tables (`report.py runs/20261020-v1-fixes-grid`)

### acme-site-shared, N=4

| | gud1-cc | gud1-ref |
| --- | --- | --- |
| trials | 2 | 2 |
| outcome | completed ×2 | completed ×2 |
| hidden-test pass rate | 100% (100%–100%) | 100% (100%–100%) |
| hidden tests passed | 28 (28–28) / 28 | 28 (28–28) / 28 |
| repo's own tests pass on final tree | True ×2 | True ×2 |
| wall time | 94 s (84 s–104 s) | 123 s (99 s–146 s) |
| cost (run) | $0.69 ($0.67–$0.71) | $0.38 ($0.14–$0.62) |
| cost per agent | $0.17 ($0.12–$0.23) | $0.10 ($0.00–$0.19) |
| tokens per agent (all kinds) | 253,942 (135,488–371,555) | — |
| ledger tokens per agent (gud1-ref) | — | 29,533 (0–64,707) |
| output tokens per agent | 3,080 (1,786–5,425) | — |
| turns per agent | 16 (10–21) | 11.8 (1–26) |
| agent wall time | 63 s (20 s–103 s) | 121 s (97 s–145 s) |
| agents killed (budget/timeout) | 0 (0–0) | 0 (0–0) |
| integrations (links / merges) | 2.5 (2–3) | 1 (1–1) |
| integrations per min | 1.58 (1.42–1.73) | 0.51 (0.41–0.61) |
| test runs (tester / merge gate) | 3.5 (3–4) | 3 (3–3) |
| test runs per integration | 1.42 (1.33–1.50) | 3.00 (3.00–3.00) |
| agent-run local test runs | 6 (6–6) | 3 (1–5) |
| conflicts | 1 (1–1) | 0.5 (0–1) |
| conflicts resolved | 0 (0–0) | 0 (0–0) |
| escalations | 0 (0–0) | 0 (0–0) |
| emails per link (gud) | — | — |
| superseded / stale (gud) | — | — |
| objections (gud) | — | — |
| PRs opened (git) | — | — |
| merge rejections (git) | — | — |
| hidden-test access attempts | 0 (0–0) | 0 (0–0) |
| links per min (gud1) | 1.58 (1.42–1.73) | 0.51 (0.41–0.61) |
| test runs per link (gud1) | 1.42 (1.33–1.50) | 3.00 (3.00–3.00) |
| pushes (gud1) | 3.5 (3–4) | 1.5 (1–2) |
| carried links (gud1) | 0 (0–0) | 0 (0–0) |
| superseded pushes (gud1) | 1 (1–1) | 0.5 (0–1) |
| colliding pushes (gud1) | 2 (2–2) | 1 (0–2) |
| pushes refused before sending: stale base (gud1) | 1.5 (1–2) | 0 (0–0) |
| claims won (gud1) | 1 (1–1) | 3 (1–5) |
| claim updates / releases (gud1) | 0 (0–0) / 0.5 (0–1) | 0.5 (0–1) / 1 (0–2) |
| subtasks posted (gud1) | 0 (0–0) | 1 (0–2) |
| claim overlaps (gud1) | 0 (0–0) | 0 (0–0) |
| overlap settlement (gud1) | — | — |
| comments (gud1) | 4 (4–4) | 0.5 (0–1) |
| objections reason / evidence (gud1) | 0 (0–0) / 0 (0–0) | 0 (0–0) / 0 (0–0) |
| consent escalations (gud1) | 0 (0–0) | 0 (0–0) |
| escalations resolved by (gud1) | — | — |
| task escalations (gud1) | 0 (0–0) | 0 (0–0) |
| red-tip episodes (gud1) | 0 (0–0) | 0 (0–0) |
| median time to green (gud1) | — | — |
| red at end (gud1) | False ×2 | False ×2 |
| T1 status at end (gud1) | open ×1, claimed ×1 | done ×2 |
| files read per agent | 6.4 (5.5–7.2) | 7.2 (4.8–9.8) |
| reads outside own changes per agent | 3.9 (3.0–4.8) | 4.4 (3.5–5.2) |
| reads outside claimed scope per agent (gud1) | 5.9 (5.0–6.8) | 4.0 (3.0–5.0) |
| colliding pushes with inflight before (gud1) | 1.5 (1–2) of 2 (2–2) | 0 (0–0) of 1 (0–2) |
| colliding pushes with comment before (gud1) | 2 (2–2) | 0 (0–0) |
| inflight / comment calls (gud1) | 5 (3–7) / 4 (4–4) | 0 (0–0) / 0.5 (0–1) |
| guard file read by an agent (context check) | — | — |
| guard tests passed (context check) | — | — |

Model: claude-sonnet-5-5. Total spend in this table: $2.14.

### refactor-guard, N=4

| | gud1-cc | gud1-ref |
| --- | --- | --- |
| trials | 1 | 1 |
| outcome | completed ×1 | completed ×1 |
| hidden-test pass rate | 100% | 100% |
| hidden tests passed | 23 / 23 | 23 / 23 |
| repo's own tests pass on final tree | True ×1 | True ×1 |
| wall time | 67 s | 122 s |
| cost (run) | $0.51 | $0.47 |
| cost per agent | $0.13 ($0.08–$0.17) | $0.12 ($0.05–$0.18) |
| tokens per agent (all kinds) | 163,199 (112,714–243,751) | — |
| ledger tokens per agent (gud1-ref) | — | 36,066 (14,348–57,706) |
| output tokens per agent | 3,737 (1,263–5,272) | — |
| turns per agent | 11.2 (9–16) | 13 (6–20) |
| agent wall time | 57 s (38 s–66 s) | 121 s (121 s–121 s) |
| agents killed (budget/timeout) | 0 | 0 |
| integrations (links / merges) | 1 | 1 |
| integrations per min | 0.90 | 0.49 |
| test runs (tester / merge gate) | 2 | 3 |
| test runs per integration | 2.00 | 3.00 |
| agent-run local test runs | 5 | 4 |
| conflicts | 1 | 1 |
| conflicts resolved | 0 | 0 |
| escalations | 0 | 0 |
| emails per link (gud) | — | — |
| superseded / stale (gud) | — | — |
| objections (gud) | — | — |
| PRs opened (git) | — | — |
| merge rejections (git) | — | — |
| hidden-test access attempts | 0 | 0 |
| links per min (gud1) | 0.90 | 0.49 |
| test runs per link (gud1) | 2.00 | 3.00 |
| pushes (gud1) | 2 | 2 |
| carried links (gud1) | 0 | 0 |
| superseded pushes (gud1) | 1 | 1 |
| colliding pushes (gud1) | 2 | 2 |
| pushes refused before sending: stale base (gud1) | 0 | 0 |
| claims won (gud1) | 1 | 2 |
| claim updates / releases (gud1) | 0 / 0 | 0 / 1 |
| subtasks posted (gud1) | 0 | 0 |
| claim overlaps (gud1) | 0 | 0 |
| overlap settlement (gud1) | — | — |
| comments (gud1) | 1 | 2 |
| objections reason / evidence (gud1) | 0 / 0 | 0 / 0 |
| consent escalations (gud1) | 0 | 0 |
| escalations resolved by (gud1) | — | — |
| task escalations (gud1) | 0 | 0 |
| red-tip episodes (gud1) | 0 | 0 |
| median time to green (gud1) | — | — |
| red at end (gud1) | False ×1 | False ×1 |
| T1 status at end (gud1) | claimed ×1 | done ×1 |
| files read per agent | 6.2 | 6.2 |
| reads outside own changes per agent | 3.8 | 3.8 |
| reads outside claimed scope per agent (gud1) | 4.2 | 4.2 |
| colliding pushes with inflight before (gud1) | 1 of 2 | 0 of 2 |
| colliding pushes with comment before (gud1) | 0 | 0 |
| inflight / comment calls (gud1) | 4 / 1 | 0 / 2 |
| guard file read by an agent (context check) | True ×1 | True ×1 |
| guard tests passed (context check) | 3 | 3 |

Model: claude-sonnet-5-5. Total spend in this table: $0.99.

### todo-stack, N=4

| | gud1-cc | gud1-ref |
| --- | --- | --- |
| trials | 2 | 2 |
| outcome | completed ×2 | completed ×2 |
| hidden-test pass rate | 100% (100%–100%) | 100% (100%–100%) |
| hidden tests passed | 27 (27–27) / 27 | 27 (27–27) / 27 |
| repo's own tests pass on final tree | True ×2 | True ×2 |
| wall time | 78 s (58 s–98 s) | 116 s (116 s–116 s) |
| cost (run) | $0.77 ($0.74–$0.80) | $0.90 ($0.85–$0.95) |
| cost per agent | $0.19 ($0.16–$0.23) | $0.22 ($0.17–$0.26) |
| tokens per agent (all kinds) | 328,608 (277,349–496,299) | — |
| ledger tokens per agent (gud1-ref) | — | 67,215 (45,674–75,954) |
| output tokens per agent | 4,366 (2,896–6,740) | — |
| turns per agent | 17.5 (15–25) | 24.9 (18–30) |
| agent wall time | 55 s (41 s–97 s) | 115 s (115 s–115 s) |
| agents killed (budget/timeout) | 0 (0–0) | 0 (0–0) |
| integrations (links / merges) | 4.5 (4–5) | 4 (4–4) |
| integrations per min | 3.61 (3.06–4.16) | 2.07 (2.07–2.08) |
| test runs (tester / merge gate) | 6 (5–7) | 5.5 (5–6) |
| test runs per integration | 1.32 (1.25–1.40) | 1.38 (1.25–1.50) |
| agent-run local test runs | 7.5 (6–9) | 8 (8–8) |
| conflicts | 0 (0–0) | 0 (0–0) |
| conflicts resolved | 0 (0–0) | 0 (0–0) |
| escalations | 0 (0–0) | 0 (0–0) |
| emails per link (gud) | — | — |
| superseded / stale (gud) | — | — |
| objections (gud) | — | — |
| PRs opened (git) | — | — |
| merge rejections (git) | — | — |
| hidden-test access attempts | 0 (0–0) | 0 (0–0) |
| links per min (gud1) | 3.61 (3.06–4.16) | 2.07 (2.07–2.08) |
| test runs per link (gud1) | 1.32 (1.25–1.40) | 1.38 (1.25–1.50) |
| pushes (gud1) | 4.5 (4–5) | 4 (4–4) |
| carried links (gud1) | 2 (2–2) | 2.5 (2–3) |
| superseded pushes (gud1) | 0 (0–0) | 0 (0–0) |
| colliding pushes (gud1) | 0 (0–0) | 0 (0–0) |
| pushes refused before sending: stale base (gud1) | 0 (0–0) | 0 (0–0) |
| claims won (gud1) | 4 (4–4) | 4 (4–4) |
| claim updates / releases (gud1) | 0 (0–0) / 0 (0–0) | 0 (0–0) / 0 (0–0) |
| subtasks posted (gud1) | 3 (3–3) | 3 (3–3) |
| claim overlaps (gud1) | 0 (0–0) | 0 (0–0) |
| overlap settlement (gud1) | — | — |
| comments (gud1) | 1 (1–1) | 0 (0–0) |
| objections reason / evidence (gud1) | 0 (0–0) / 0 (0–0) | 0 (0–0) / 0 (0–0) |
| consent escalations (gud1) | 0 (0–0) | 0 (0–0) |
| escalations resolved by (gud1) | — | — |
| task escalations (gud1) | 0 (0–0) | 0 (0–0) |
| red-tip episodes (gud1) | 0 (0–0) | 1 (1–1) |
| median time to green (gud1) | — | 9 s (8 s–10 s) |
| red at end (gud1) | False ×2 | False ×2 |
| T1 status at end (gud1) | done ×2 | done ×2 |
| files read per agent | 1.6 (0.5–2.8) | 2.1 (2.0–2.2) |
| reads outside own changes per agent | 0.6 (0.0–1.2) | 0.1 (0.0–0.2) |
| reads outside claimed scope per agent (gud1) | 0.8 (0.0–1.5) | 0.1 (0.0–0.2) |
| colliding pushes with inflight before (gud1) | 0 (0–0) of 0 (0–0) | 0 (0–0) of 0 (0–0) |
| colliding pushes with comment before (gud1) | 0 (0–0) | 0 (0–0) |
| inflight / comment calls (gud1) | 3.5 (3–4) / 1 (1–1) | 0.5 (0–1) / 0 (0–0) |
| guard file read by an agent (context check) | — | — |
| guard tests passed (context check) | — | — |

Model: claude-sonnet-5-5. Total spend in this table: $3.33.

- Each cell is mean (min–max) over trials; per-agent rows pool every agent in every trial.
- Integrations are gud links (excluding genesis) or git PR merges into main.
- Test runs are the gud tester's replies or the git merge gate's pytest runs; both run `python -m pytest -q -x`.
- gud conflicts are pushes that ended superseded, stale, conflict or objected; git conflicts are merge-gate rebase conflicts plus `CONFLICT` lines in agents' own git output. Resolved means the same author landed later.
- Escalations are final messages containing `ESCALATE:`. Cost is Claude Code's reported total_cost_usd (list price), or an estimate from usage for a killed agent.
