# gud

**Git is too slow for a swarm, so in gud an email thread does the job of source control.** An agent emails a diff. Whoever owns the lines it changes gets a minute to object. The tests reply on the same thread, and then the change is in.

A swarm doing real work spends a lot of its time on git: clone, pull, rebase, merge conflicts, pull requests, review, the CI queue. gud rebuilds source control for that case. The repo is the email chain, and a push is one signed diff. A push that only adds code lands as soon as the tester replies. A push that rewrites another agent's lines gives that agent a minute to object on the thread, and the revised diff lands as soon as they approve. There are no branches and no merges.

## Results

Every number below comes from a scripted run, not a model call. Tokens are counted with GPT-5's `o200k_base` tokenizer. Method and traces are in [RESULTS.md](RESULTS.md).

**One negotiation.** `@billing-agent` adds rate limiting to `login` by making it raise an error. `@auth-agent` owns that function and objects. The author revises, the owner approves, and the change lands.

| | Pull request | gud email |
| --- | ---: | ---: |
| Turns | 16 | 10 |
| Total tokens | 48,870 | ~24,400 |
| Output tokens | 2,450 | ~400 |

The author accounts for the whole gap. On the pull request, the author rewrites all of `auth.py` twice. In gud, the author pushes two small old/new edits. The reviewer takes 4 turns either way.

**The full scripted swarm** ran on a real Primitive mailbox: 8 beats, 68 emails, all passing. Here it is against the same edits sent as git pull requests:

| | Pull requests | gud email |
| --- | ---: | ---: |
| Turns | 89 | 41 |
| Total tokens | 270,215 | 110,361 |

**Cost.** Primitive charges $1 per 43,000 emails. GitHub Enterprise for 100 agents costs $2,100 a month in seats before anyone pushes.

**Why now.** AgenticFlict (2026) found that 27.7% of agent pull requests conflict on merge. Pairs of different agents conflict about twice as often as pairs of the same agent (41.7% against 19.8%). Swarm tools like SwarmGit and SwarmForge give each agent a worktree on one machine and merge through a local queue. That only works for agents you launched yourself on a shared disk.

## Sponsor tracks

| Sponsor | What it does in gud | What breaks without it |
| --- | --- | --- |
| **Primitive** | The mailbox is the repo. Every push, objection, approval, test result, task and bug report is an email to the repo address, with agents in CC. Long-polling `listEmails?wait=` replaces `git pull`, and `sender_verified` is how humans are trusted. | Storage, transport, and human identity |
| **InsForge** (InstaBox) | Every push is tested in an InstaBox VM, up to 5 at a time, and the tester agent replies to the push with a signed pass or fail. After a task is done, a browser agent in an InstaBox clicks through the site and emails any defects to `bugs@`. | The "tested" link rule, and browser QA |
| **Armature** | Analytics on the gud MCP server, plus evals that run the same multi-agent tasks against gud and against a git-backed MCP server. | The before-and-after numbers |

## How it works

### The repo is rebuilt from the mailbox

The mailbox is the only record. A local copy is a cache, and `gud rebuild` can recreate it from genesis.

1. Every push email carries a unified diff against its parent push (`Gud-Parent`), along with the parent's state hash, the new state hash, and the new tree hash.
2. A push becomes a **link** when all four of these rules hold:
   1. **Authorship.** It is signed (Ed25519) by a member listed in `.gud/members` at the parent, and sent from that member's address.
   2. **Applies.** The diff applies cleanly to the parent's tree, and the hashes match.
   3. **Consent.** See below.
   4. **Tested.** A tester member has replied that the tests pass on that exact tree.
3. Among a link's children, the earliest-received push that qualifies becomes the next link. If an earlier child is still pending, the chain waits for it.
4. Every decision uses the time the mailbox received each email, so replaying the rules gives the same chain on any machine.

There are no branches. When two agents push on the same parent, both pushes are evaluated. The one that loses is marked superseded, and its agent either pushes again on the new tip or argues on the thread.

### Consent

Consent is worked out from blame at the parent:

- **Affected owners** are the agents who own the lines a push removes or rewrites. If a push inserts into a Python function or class, that function's owner counts too.
- **Lazy consensus** is the default. A push lands after a 60-second window unless an affected owner objects on the thread. If every affected owner approves, it lands right away.
- **Explicit** consent applies to files listed in `.gud/protected`, such as `api/interfaces.py`. Every affected owner must approve.
- **Quorum** applies to `.gud/members` and `.gud/protected`. More than half of the voters must approve.
- **Veto.** If a human member replies `veto`, the push never lands.

### Members and joining

`.gud/members` is a file in the tree, so membership has a history like any other file. Adding or removing a member is a push, and it needs quorum. Once a removal lands, that member's key stops verifying, so any later pushes from them are rejected. Their old lines stay in blame, marked orphaned.

Nobody shares the host's API key. A member proves who they are with their own Ed25519 signature, not with a mail account. An outside agent runs `gud join <host> --handle @x --address x@their.primitive.email`. That creates a key locally and emails a signed join request. A current member proposes the membership push, and it lands by quorum. The new member then gets a welcome email with the host URL and a checkpoint (tip, state hash, tree hash) to verify by replaying the mailbox.

The host serves a read-only mirror of the mailbox, and only current members can read it, by signing their requests. The host can't forge pushes, approvals, or test results, because every member checks the signatures. `gud audit` compares the mirror with the member's own inbox, which catches mail that was withheld or retimed.

### Tasks and bugs

A human sends a task through the console. The lead agent splits it into subtasks (`T7.1`, `T7.2`, and so on), each assigned to a subagent by email. A subtask is done when a link carries its id. The whole task is done when the lead sends `task-done`, and the person who asked gets one email.

After that, browser QA serves the tip and mails each defect it finds to `bugs@`. A voter other than the reporter accepts or rejects each report on its thread, and an accepted report becomes a new task.

## Isn't this just `git send-email`?

The Linux kernel does take patches by email, and that is where the idea came from. The difference is what decides whether a patch lands:

- **Kernel:** a maintainer reads the patch and runs `git am`. The maintainer's tree is the truth, and the email is only a proposal.
- **gud:** nobody applies anything by hand. The rules above decide, and anyone can replay them. Consent comes from the owners of the lines being changed, not from one maintainer, and the test result is part of the thread.

gud also works between agents that share nothing. A Cursor agent, a Claude agent, and a stranger's agent can all push to the same repo without accounts on each other's git host.

## Architecture

| Component | What it does | Code |
| --- | --- | --- |
| Wire format | Body text, `Gud-*` trailers, and one fenced diff, plus hashes and signing bytes | `gud/wire.py`, `gud/sig.py` |
| Replay | Applies the link rules to the mailbox to produce links, blame, and pending proposals | `gud/replay.py`, `gud/blame.py`, `gud/rules.py` |
| Mailbox | Primitive in production, a JSONL file locally | `gud/mail_primitive.py`, `gud/mail_fake.py` |
| Tester | Watches for pushes, runs the tests, and replies with a signed result | `gud/tester.py`, `gud/build.py` (local or InstaBox) |
| Browser QA and bugs | Clicks through each finished tip and reviews bug reports on their threads | `gud/qa.py`, `gud/bugs.py` |
| Agent MCP server | `status`, `read`, `ls`, `blame`, `push`, `wait`, `rebase`, `reply`, `log`, `thread`, `tasks` | `gud/mcp_server.py`, with Armature analytics |
| Live-feed MCP app | A widget in Claude showing the chain, tasks, and bugs, plus one-click approve, object, or veto | `gud/mcp_app.py`, [docs/mcp-app.md](docs/mcp-app.md) |
| Git baseline | The same tasks done through pull, commit, and push | `gud/mcp_baseline.py`, `evals/` |
| Swarm | A lead plus subagents driven by an LLM, with a scripted demo mode | `gud/swarm.py`, `gud/worker.py`, `gud/lead.py` |
| API and UI | REST and server-sent events covering the chain, blame, tasks, members, and activity | `gud/api.py`, `ui/` (Vite, React, Tailwind, shadcn) |

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pytest -q

# the scripted swarm on a local mailbox (add --llm to have a model drive the agents)
.venv/bin/python scripts/demo.py --fast

# the pull-request vs email comparison
.venv/bin/python -m evals.pr_vs_thread
```

By default it runs fully locally, with `GUD_MAILBOX=fake` and `GUD_RUNNER=local`. To use the real services, put `PRIMITIVE_API_KEY`, `INSTABOX_API_KEY`, and `ARMATURE_API_KEY` in `.env`.

## More

- [DEMO.md](DEMO.md): the three-minute judge demo, beat by beat, with the cost card.
- [RESULTS.md](RESULTS.md): how every number above was measured.
- [SPEC.md](SPEC.md): wire format, link rules, consent.
- [api_contract.md](api_contract.md): the Python core API and the HTTP API.
- [docs/mcp-app.md](docs/mcp-app.md): running the live-feed MCP app in Claude, Codex, and Cursor.

## Known limitations

- **The coder sees only the files the lead names.** A subtask passes the coder at most 8 files, each cut off at 12,000 characters. The worker already holds the whole tip from replay, but the model can't ask for another file or for the rest of a cut one.
- **Inkbox (iMessage notifications and a key vault) is not wired in yet.** The notifier has a place for it to plug in (`gud/notify.py`).
