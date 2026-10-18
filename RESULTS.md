# Results

18 Oct 2026. Two scripted comparisons. No model was called in either. Tokenizer is GPT-5 `o200k_base`. A turn resends the system prompt, the tool list, and the conversation so far, then the model writes one tool call.

`livetest/REPORT.md` is a different experiment: a model, on a site plan, with git pushing to trunk and skipping review. Those token totals are not these.

## One edit to login

`python -m evals.pr_vs_thread`, run again after the first print. `@billing-agent` rate-limits `login` by raising. `@auth-agent` objects: return `LoginResult(False, None, "rate limited")`. The author revises. The owner approves. The change lands.

The pull-request side is local git plus the scripted `pr_create`, `pr_diff`, `pr_comment`, `pr_comments`, `pr_checks`, `pr_approve`, and `pr_merge` results. The email side is a fake mailbox in a temp directory, not Primitive.

| | Pull request | gud email |
| --- | ---: | ---: |
| Turns | 16 | 10 |
| Input tokens | 46,420 | 23,984 |
| Output tokens | 2,450 | 407 |
| Total tokens | 48,870 | 24,391 |
| Payload tokens | 640 | 1,244 |
| Tool-list tokens / turn | 687 | 1,254 |
| Local seconds | 0.206 | 0.009 |

The author is the gap: 12 turns and 2,366 output tokens against 6 turns and 278. The reviewer is 4 turns either way. The pull-request author writes all of `auth.py` twice. The gud author pushes two small old/new edits. The emails themselves are the heavier artifact (1,254 payload tokens against 640) because each mail repeats the diff, the trailers, and a signature. The gud tool list is also heavier per turn. Fewer turns still win.

The email total moves a little on every run because message ids and signatures are new. This table matches the traces in `results/login-pull-request.md` and `results/login-gud.md`. An earlier print was 24,477, and the one before that was 24,431 (`VIDEO.md` still has that print). `DEMO.md` says 22,080 and `gud.md` says 26,272. Both are older. The pull-request total stayed 48,870.

Local seconds are git and the mailbox on this machine. They are not model time, and they are not time waiting on a person or on GitHub Actions. CI on the pull-request side is the line `pytest pass 1s`.

## Full scripted swarm, Primitive against pull requests

The swarm is `scripts/demo.py` with no `--llm`, mailbox `primitive`, runner `local`, repo `repo-swarm-20261018@lean-ridge.primitive.email`. The first process sent genesis and checked the mailbox before Primitive had delivered it, so it stopped. The message was there a moment later. The second process finished in 56.0 seconds. All eight beats passed. Tip #10, state `89e7c09e3566`, 68 emails.

`python -m evals.swarm_vs_prs` replays those same scripted edits as local git pull requests. A pull request does not merge without an approval and a passing pytest. The lockout gets a change request and a second commit on the same branch. Membership waits for `@nelson`. After `@sub-4` is removed, their push is rejected. Nine clones. The gud column of the token table is the tool transcript for those steps, not a tokenization of the 68 emails. The 56.0 seconds is the Primitive run. This process did not send the mail.

| | Pull requests | gud email |
| --- | ---: | ---: |
| Wall seconds | 17.4 | 56.0 |
| Of which clone | 0.9 | 0 |
| Turns | 89 | 41 |
| Input tokens | 257,829 | 107,588 |
| Output tokens | 12,386 | 2,773 |
| Total tokens | 270,215 | 110,361 |

Pull requests opened 10, merged 10. Approvals 10. Review comments 1. Commits on those branches 11. Rebases 1. Content conflicts 0. Pytest runs 12, failures 0. Rejected pushes 1.

The 17.4 seconds is local git. The 56.0 seconds includes Primitive. That gap is the network. Git never hit a content conflict: the four subtasks touched different lines, so the only rebase was the second race branch sitting behind `main`. The lockout was a review comment, not a conflict file.

The Primitive mailbox, counted by kind:

| Kind | Count |
| --- | ---: |
| push | 20 |
| test | 17 |
| approve | 10 |
| notify | 6 |
| subtask | 6 |
| task | 3 |
| task-done | 3 |
| plain | 2 |
| object | 1 |
| **emails** | **68** |

The live swarm pushed 20 times because several agents shared a parent and re-pushed. The pull-request queue merged in order and only rebased once, so it has 11 commits on the branches.

## Traces

One file per side. Every turn is there, with the arguments and the result.

| File | What it lists |
| --- | --- |
| `results/login-pull-request.md` | 16 turns, the one edit as a pull request |
| `results/login-gud.md` | 10 turns, the same edit as gud tool calls |
| `results/swarm-pull-requests.md` | 89 turns, the swarm as pull requests |
| `results/swarm-gud.md` | 41 turns, the tool transcript behind the swarm token column |
| `results/swarm-primitive.md` | 68 emails from the Primitive run, in mailbox order |

## Re-run

```bash
cd gud
.venv/bin/python -m evals.pr_vs_thread
.venv/bin/python -m evals.swarm_vs_prs
.venv/bin/python -m evals.dump_traces
```
