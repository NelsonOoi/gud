# Two-minute video

One negotiation, shown twice. `@billing-agent` rate-limits `login` by raising. `@auth-agent`, who owns the function, says to return `LoginResult` instead. The author revises. The owner approves. The change lands.

The gud half is those emails on the chain. The GitHub half is the same edit through the scripted pull-request proxy in `evals/pr_vs_thread.py`: real git diffs, then `pr_create`, `pr_diff`, `pr_comment`, `pr_comments`, `pr_checks`, `pr_approve`, and `pr_merge`. The last card is the table that script prints. No model is called. Record it once and cut. Do not run a live model, Primitive, or InstaBox in this video.

`DEMO.md` is the longer judge demo. This cut is only the comparison.

## The card

Printed by `.venv/bin/python -m evals.pr_vs_thread` on 18 Oct 2026. Re-run it the morning you export and replace this table if a line moves.

| | Pull request | gud email |
| --- | ---: | ---: |
| Turns | 16 | 10 |
| Input tokens | 46,420 | 24,026 |
| Output tokens | 2,450 | 405 |
| Total tokens | 48,870 | 24,431 |
| Local seconds | 0.229 | 0.011 |

Author is the gap: 12 turns and 2,366 output tokens against 6 turns and 278. Reviewer is 4 turns either way. The pull-request author writes all of `auth.py` twice. The gud author pushes two small old/new edits each time.

Say this if asked, and leave it off the card. The emails themselves are heavier than the diffs and the review comment: 1,260 payload tokens against 640, because each mail repeats the diff, the trailers, and a signature. The gud tool list is also heavier per turn (1,254 tokens against 687) because the live `mcp_server` list is resent every turn. Fewer turns still win. Local seconds are git and the mailbox on this machine. They are not model time, and they are not time spent waiting on a person or on GitHub Actions. CI on the pull-request side is the one line `pytest pass 1s`.

`DEMO.md` still says 22,080 for the email side. `gud.md` says 26,272. Both are older runs. Speak 24,431.

## Shot list

About 120 seconds. Chain view on the left half, the pull-request calls on the right half, then the card full frame.

| Time | Picture | Line |
| --- | --- | --- |
| 0:00–0:08 | Title over a still of `login`. | "Same edit. One side opens a pull request. The other sends email." |
| 0:08–0:28 | Chain. The first push is open. Affected owner is `@auth-agent`. | "`@billing-agent` sends two edits. The owner of `login` is told, on that thread." |
| 0:28–0:42 | The object reply, then the revised push. | "`@auth-agent` says: don't raise, return `LoginResult`. The revision is another email." |
| 0:42–0:52 | The approve, then the link on the tip. | "The owner approves. The tester has already replied. It is in." |
| 0:52–1:18 | The sixteen calls, scrolled in order. Hold on `write` both times so the whole file is visible. | "The pull request does the same negotiation in sixteen calls. The author writes the whole file, then writes it again after the comment." |
| 1:18–1:28 | `pr_comment` with the objection, then `pr_diff` of the revised branch, then `pr_approve` and `pr_merge`. | "The review is a comment that repeats the hunk. Approve and merge are two more calls." |
| 1:28–1:48 | The card. | "Sixteen turns and 48,870 tokens. Ten turns and 24,431. The author is the whole gap." |
| 1:48–2:00 | The card stays. One cost line, then out. | "A hundred agents on GitHub Enterprise is $2,100 a month in seats. This mailbox is about a dollar per 43,000 emails." |

The objection text, both sides:

> Don't raise. login always returns LoginResult. When the bucket is empty, return LoginResult(False, None, "rate limited").

## What the pull-request half shows

These are the calls `run_pr` already walks. Put them on screen in this order. The diff text comes from a real local git repo that the script builds and deletes. The `pr_*` results are the proxy: the same text `gh pr diff` and `gh pr view` return, without opening github.com.

`@billing-agent`

1. `read` `auth.py`
2. `write` the whole raised file
3. `git_commit` "rate-limit login"
4. `git_push` to `rate-limit-login`
5. `pr_create` "Rate-limit login"
6. `pr_comments` on #17, which includes the hunk again
7. `read` the raised file
8. `write` the whole revised file
9. `git_commit` "return LoginResult when rate limited"
10. `git_push`
11. `pr_checks` → `pytest pass 1s`
12. `pr_merge` #17

`@auth-agent`

1. `pr_diff` #17, the full branch diff
2. `pr_comment` on `auth.py` with the objection
3. `pr_diff` #17 again, after the second commit
4. `pr_approve`

Tools charged on every pull-request turn: `git_status`, `git_pull`, `read`, `write`, `git_commit`, `git_push`, `resolve`, `log`, plus the seven `pr_*` tools. That list is what makes the input 46,420.

## What the gud half shows

`@billing-agent`

1. `read` `auth.py`
2. `push` two old/new edits that add `import ratelimit` and the `raise`
3. `wait` → objected, with the reason
4. `read` `auth.py` at the tip, which is still the original
5. `push` two old/new edits that return `LoginResult`
6. `wait` → landed

`@auth-agent`

1. `thread` on the first push
2. `reply` `object`
3. `thread` on the revision
4. `reply` `approve`

The chain should show four beats and then one new link: the raise proposal, the object, the revision, the approve, the tip. The tester's pass is on the thread. It is not a fifth thing to narrate.

## Before you record

1. Re-run the counter and freeze the card from that printout.

```bash
cd gud
.venv/bin/python -m evals.pr_vs_thread
```

2. Seed a fresh fake mailbox with this negotiation and open the chain on it. `pr_vs_thread` builds those emails in a temp directory and deletes them, so the page at `http://localhost:8787` will not show this thread until something writes the same five messages into the mailbox the API is reading. `scripts/demo.py` is the other swarm: rate-limit plus lockout plus a protected file plus a race. Do not film that and put this card under it.

3. Start the page only after the seed exists.

```bash
GUD_MAILBOX=fake .venv/bin/python -m gud.api
```

4. Zoom the chain so one proposal card fills the left half. The right half is a text column of the sixteen calls, not a GitHub tab and not the MCP widget.

5. Record each half as its own take. Cut on the sentences above. The chain does not have to move while you talk if the four messages are already there. Scrolling the call list is enough motion on the right.

## Leave out

The overlap benchmark in `livetest/REPORT.md`. That git side pushes to trunk and does not review, so it spends fewer tokens. It answers a different question.

`livetest/run_git.py --mode pr`. That one does use branches, a review queue, and a merge, and it calls a model. It is not this card.

A github.com window, unless the `pr_*` lines on screen are the same sixteen calls. A live pull request is a different recording, and its token count is not this table.
