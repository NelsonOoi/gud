# gud

**Source control where the repo is an email address.** Every push, vote, test result, task and claim is a signed email to the repo address. The host appends each one it accepts to a signed, hash-chained log. Every member replays the same mail and log and gets the same chain, the same tip and the same blame. There are no branches and no merges: a push names its parent, and if another push lands first, gud carries it forward onto the new tip when the files don't collide.

gud is built for swarms of coding agents, with humans as members too:

- **Consent from blame.** A push that rewrites lines someone else owns opens a short window for them to object. A push that touches nobody's lines links on its own. Ownership fades with time and links. `.gud/owners` adds lazy, explicit or quorum rules per path, and humans can always veto.
- **Tests from a tester member.** A tester replies to pushes and to the tip with signed results, under the repo's `.gud/test` policy: gate pushes, or test the tip, bisect a red tip, and open a fix task for whoever broke it.
- **Leaderless tasks.** Anyone opens a task. Agents claim it with a file scope and a lease, split it into subtasks, and declare it done. Replay derives done-ness, and a quiet, landed task becomes done by itself.
- **Fewer tokens spent on coordination.** Agents never clone, branch, rebase, resolve merge conflicts or manage pull requests. A push is a small diff, and a push that loses a race is carried forward instead of redone. In real-agent runs, gud agents wrote **16–45% fewer output tokens** than git agents for the same passing result, and threw away less work ([Token savings](#token-savings)).
- **Any mail provider.** The protocol needs only what IMAP+SMTP, JMAP, API providers and the Gmail API all offer: raw MIME, a delivery time, a cursor. The envelope travels as a `gud.env` attachment, so providers can rewrite Message-IDs and bodies without breaking anything.

The contract is [SPEC.md](SPEC.md). The Python interfaces are in [docs/INTERFACES.md](docs/INTERFACES.md), provider setup in [docs/TRANSPORTS.md](docs/TRANSPORTS.md), the design record in [docs/PLAN.md](docs/PLAN.md), and measured results in [RESULTS.md](RESULTS.md).

## Token savings

With git, a coding agent spends a large share of its tokens on plumbing: cloning, pulling, branching, rebasing, resolving conflict markers, opening and reviewing pull requests, and redoing work that lost a merge race. gud removes that plumbing:

- **No clone and no pull.** The agent works in a local working copy that `sync` keeps current. There's no branch to manage.
- **A push is one small diff.** `push` sends the working copy's change against its base. There's no PR to write, update or rebase.
- **Lost races are carried, not redone.** If another push lands first and the files don't overlap, gud carries the push onto the new tip itself.
- **Conflicts are settled where they happen.** A push that rewrites someone's recent lines gets a short consent window on the thread, instead of a conflict file and a retry loop.
- **Collisions are caught before anyone codes.** Claims declare their files, so overlapping work shows up as `claim.overlap` before any tokens go into it.
- **Tests aren't a per-push gate.** The tester checks the tip and bisects if it turns red, so agents don't wait on CI and re-push.

**Measured** with real Claude Code agents, N=4, the same model, tools, prompt preamble and budget in both arms ([RESULTS.md](RESULTS.md#real-agent-evals)). Every run in both arms passed every hidden test.

| Spec | Output tokens per run: git | Output tokens per run: gud (current v1) | Change |
| --- | ---: | ---: | ---: |
| todo-stack (library + CLI + HTTP API + docs) | 22,104 | 17,465 | **−21%** |
| acme-site-shared (five pages, shared nav and styles) | 17,723 | 12,322 | **−30%** |
| refactor-guard (feature plus an unnamed guard) | 20,129 | 14,949 | **−26%** |

- **Every cell wrote fewer output tokens.** The table is the current v1 batch. In the first grid, before the coordination fixes, the savings were 16–45%, so every cell of both batches wrote fewer. Output tokens are the code, messages and tool calls the model writes.
- **Less work thrown away.** git runs left 1.5–4 duplicate PRs unmerged; gud-cc left 0–2.5 superseded or stale pushes in the first grid, and 0 on todo-stack after the fixes.
- **Cost per run was about the same.** gud agents take more turns to read the task board and each other's in-flight diffs, so their input and context-read tokens were similar or higher, and cost per run landed in the same range ($0.40–0.77 against $0.52–0.69).

These are small samples: two trials per cell, local transports. The full tables are in [evals/v1/runs/](evals/v1/runs/).

## Install

gud installs from source. The `gud` package on PyPI is a different project, so don't `pip install gud`.

```bash
git clone https://github.com/NelsonOoi/gud.git && cd gud
python3.11 -m venv .venv && source .venv/bin/activate   # Python 3.11 or later
pip install -e .            # the `gud` command
pip install -e '.[agent]'   # plus the reference agent (Claude Agent SDK)
```

The `gud` command lives in `.venv/bin`, so activate the venv in each new shell. For a global command instead, run `pipx install 'git+https://github.com/NelsonOoi/gud.git'`. The web dashboard is served from a source checkout (`ui/dist`, built with `cd ui && npm ci && npm run build`), or from any path given in `GUD_UI_DIST`.

## Use gud with your own agents

Any agent that can call MCP tools or run a shell command can work in a gud repo. Pick how it connects:

| Your agent | Connect it with | You get |
| --- | --- | --- |
| Claude Code, Codex, Cursor, or any other MCP client | `gud mcp`, the gud MCP server | 28 tools: tasks, claims, push, review, inflight, history. The agent edits files with its own tools. |
| Your own loop on the Claude Agent SDK, or any other framework | `gud mcp` as an MCP server inside your loop | the same tools; you run the loop |
| A script, or an agent that only has a shell | the `gud` CLI with `--json` | every command, with one JSON object of output |
| Nothing yet | `gud agent`, the reference agent | a complete agent that wakes on events, claims, splits, codes, reviews and answers objections |

### 1. Give each agent its own identity

Each agent is a member of the repo, with its own key, its own mail address and its own working copy. To add an agent to an existing repo (the [Quickstart](#quickstart) below builds a local one to try this on):

```bash
# On the agent's machine: a key and mail account, a join request, then (once admitted) a working copy.
gud --home ~/.gud-alice --handle @alice keys new --address alice@example.org --transport imap_smtp --set ...
gud --home ~/.gud-alice --handle @alice join https://host.example.org --address alice@example.org --role agent --sponsor @nelson

# A human member admits the request: from the CLI, or with the Admit button on the dashboard's Members view.
gud --home ~/.gud-nelson --handle @nelson admit <join id>

gud --home ~/.gud-alice --handle @alice clone https://host.example.org ~/work/alice-wc
```

Give each agent its own `--home` and working copy, even when they share a machine.

### 2. Connect an MCP client

The server is one process per agent, bound to that agent's key and working copy:

```bash
gud --home ~/.gud-alice --handle @alice mcp --wc ~/work/alice-wc
```

Use the full path to `gud` (for example `~/gud/.venv/bin/gud`) if it isn't on the client's `PATH`.

**Claude Code:**

```bash
claude mcp add gud -- gud --home ~/.gud-alice --handle @alice mcp --wc ~/work/alice-wc
# add --scope project to write it into .mcp.json for the whole team
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.gud]
command = "gud"
args = ["--home", "/Users/you/.gud-alice", "--handle", "@alice", "mcp", "--wc", "/Users/you/work/alice-wc"]
```

**Cursor** (`.cursor/mcp.json`), or any client that reads an `mcpServers` file:

```json
{ "mcpServers": { "gud": {
    "command": "gud",
    "args": ["--home", "/Users/you/.gud-alice", "--handle", "@alice", "mcp", "--wc", "/Users/you/work/alice-wc"] } } }
```

**Your own loop on the Claude Agent SDK:**

```python
from claude_agent_sdk import ClaudeAgentOptions, query

options = ClaudeAgentOptions(
    cwd="/Users/you/work/alice-wc",
    mcp_servers={"gud": {"command": "gud",
                         "args": ["--home", "/Users/you/.gud-alice", "--handle", "@alice", "mcp"]}},
    allowed_tools=["Read", "Edit", "Write", "Bash", "Grep", "Glob", "mcp__gud"],
)
async for message in query(prompt="Pick up the next open task in this gud repo and see it through.", options=options):
    ...
```

The server sends its own instructions to the client: how to claim, split, push, wait and review. A short prompt is enough.

### 3. The loop an agent follows

1. `tasks`: read the whole board, including open subtasks and idle agents.
2. `claim` a task, listing the files it will touch. If other agents are free and the task has separate parts, post `subtask`s with their files and dependencies, and claim one.
3. `inflight`: see what other agents are changing right now. Then explore the repo, edit the working copy, and run the tests locally.
4. `push`, then `wait(push=…)` until it links. If someone objects, answer with a `comment` or a revised push.
5. When `inbox` or `wait` says a push needs your review, read it with `thread` or `inflight`, then `approve`, `object` (with a reason, or a failing test as evidence) or `comment`.
6. When the whole task is in, `claim_update(done=true)`.

With the CLI it's the same loop: `gud tasks`, `gud claim T1 --files 'src/**'`, `gud sync`, `gud push -m …`, `gud review`, `gud approve <id>`, `gud claim update <claim> --done`. Add `--json` to any of them.

### 4. Wake the agent when something needs it

The host sends each member the events that concern it: a push touching its lines, an objection to its push, a claim overlap, a new link, a red tip. Choose a delivery channel per member:

```bash
gud --home ~/.gud-alice --handle @alice delivery --webhook https://my-orchestrator.example.org/gud/alice
gud --home ~/.gud-alice --handle @alice delivery --sse      # stream from GET /api/v1/events?member=@alice
gud --home ~/.gud-alice --handle @alice delivery --email    # a batched notify email
```

Webhook deliveries carry `X-Gud-Event-Id`, `X-Gud-Time` and `X-Gud-Sig`, the host's Ed25519 signature over the body and time. `gud.events.dispatch.verify_hook` checks it, and `EventClient` adds dedupe and a saved cursor. Deliveries are at least once, in order, and resume from the last event after a restart. That's enough to start a fresh agent session, or resume one, the moment someone objects to its change. Inside a long-running session, the MCP `wait` tool does the same job.

### 5. Mix agents, vendors and machines

Agents only talk through mail and the host's mirror, so a Claude Code session on a laptop, a Codex agent in CI and a reference agent on a server can share one repo. Each signs with its own key and sends from its own mail account. Humans take part from the CLI, the dashboard, or by replying `approve`, `object` or `veto` to the email.

## Quickstart

A local test run: this runs a whole repo on one machine over the `fake` transport, an on-disk mail system, so nothing leaves the machine. Swap `--transport fake --set root=...` for a real provider ([Run on your email provider](#run-on-your-email-provider)) to run it over email. Each member has its own `GUD_HOME` (`--home`) and handle (`--handle`, or `GUD_HANDLE`); `--json` makes any command print one JSON object.

```bash
MAIL=$PWD/mail; REPO=repo@example.org

# 1. A host for the repo address, served over HTTP (the mirror, intake, events and the dashboard).
gud --home homes/host --handle @host host init --repo $REPO --transport fake --set root=$MAIL --dir hostdir
gud --home homes/host --handle @host host serve --dir hostdir --bind 127.0.0.1:8787 &

# 2. A key and a mail account for every member.
for who in nelson alice tester; do
  gud --home homes/$who --handle @$who keys new --address $who@example.org --transport fake --set root=$MAIL
done
gud --home homes/alice --handle @alice keys show          # prints the public key for the roster

# 3. Genesis from a directory and a roster: handle role address [public key] [sponsor].
cat > roster <<EOF
@nelson human  nelson@example.org
@alice  agent  alice@example.org   ed25519:<alice's key>  @nelson
@tester tester tester@example.org  ed25519:<tester's key> @nelson
@host   host   $REPO
EOF
mkdir -p myproject/.gud && echo 'command python -m pytest -q' > myproject/.gud/test
cd myproject && gud --home ../homes/nelson --handle @nelson init --repo $REPO --member-file ../roster \
    --host-url http://127.0.0.1:8787 -m "first commit" && cd ..

# 4. An agent clones, edits and pushes. A push that touches nobody's lines links on its own.
gud --home homes/alice --handle @alice clone http://127.0.0.1:8787 alice-wc
cd alice-wc && echo 'def mul(a, b): return a * b' > mul.py
gud --home ../homes/alice --handle @alice push -m "add mul"     # push ... on <base>: 1 ops; linked
gud --home ../homes/alice --handle @alice log
gud --home ../homes/alice --handle @alice sync                  # three-way merge of the tip; never auto-resolves
cd ..

# 5. The tester runs the repo's test policy and signs results.
gud --home homes/tester --handle @tester clone http://127.0.0.1:8787 tester-wc
cd tester-wc && gud --home ../homes/tester --handle @tester tester --runner local &   # or docker, instabox
cd ..

# 6. Agents: the reference agent, or any MCP client (Claude Code, Codex, Cursor) on the gud MCP server.
cd alice-wc
gud --home ../homes/alice --handle @alice agent --budget-usd 2       # needs the [agent] extra and Claude auth
gud --home ../homes/alice --handle @alice mcp                        # stdio MCP server bound to @alice and this working copy
```

Other commands: `task new|list|show`, `claim`, `release`, `subtask`, `approve`, `object` (with `--test FILE` for an evidence objection), `veto`, `hold`, `comment`, `review`, `blame`, `verify --from-genesis`, `join` / `admit`, `delivery --webhook|--sse|--email`, `mirror git`, `import` (genesis from a git commit), `audit`, `export-archive` / `import-archive` and `host handover`. `gud --help` lists them all; `gud init` without `--host-url` makes a local host instead (`local:<dir>`), with no server at all.

**Dashboard.** `cd ui && npm ci && npm run build`, then `gud host serve` also serves the dashboard at `/`: the chain, proposals with their consent plans and countdowns, tasks and claims, blame with fading ownership, escalations, the red tip, members, the host log with its self-audit, and every event live. With `--ui-home <a human's GUD_HOME>` the page can also approve, object, veto and open tasks as that human, from loopback only. See [ui/README.md](ui/README.md).

## Run on your email provider

A repo needs one mail account for the **repo address**, which the host reads, and one for **each member's own address**, which that member sends from. Each can be on a different provider. Transport settings go in `--set KEY=VALUE`; any key ending in `_env` names an environment variable that is read when the mailbox opens, so passwords and API keys never land in a config file.

**1. The host, on the repo address.**

```bash
export REPO_IMAP_PASSWORD=...
gud --home homes/host --handle @host host init --repo repo@yourdomain.com --dir hostdir \
  --transport imap_smtp \
  --set imap_host=imap.fastmail.com --set smtp_host=smtp.fastmail.com \
  --set username=repo@yourdomain.com --set password_env=REPO_IMAP_PASSWORD \
  --set authserv_id=mx.messagingengine.com
gud --home homes/host --handle @host host serve --dir hostdir --bind 0.0.0.0:8787
```

On Primitive, use `--transport primitive --set api_key_env=PRIMITIVE_API_KEY` instead. When it can log in, `host init` also probes the mailbox and reports its size limit, push mode and authserv-id. `gud doctor` (step 3) shows a login failure explicitly.

**2. Each member, on their own address.**

```bash
export ALICE_PASSWORD=...
gud --home ~/.gud-alice --handle @alice keys new --address alice@yourdomain.com \
  --transport imap_smtp --set imap_host=imap.fastmail.com --set smtp_host=smtp.fastmail.com \
  --set username=alice@yourdomain.com --set password_env=ALICE_PASSWORD
```

This creates the key and writes `~/.gud-alice/members/@alice/transport.toml`. Then put the member in the genesis roster, or have them `join` and a sponsor `admit` them ([Use gud with your own agents](#use-gud-with-your-own-agents)).

**3. Check the round trip and set the timing.**

```bash
gud --home ~/.gud-alice --handle @alice doctor
```

`doctor` sends a message to the member's own address, times how long it takes to come back, and proposes a consent window and grace for `.gud/config`. Commit those as an ordinary push, so every member replays with the same values. Providers that deliver slowly (SMTP greylisting, retries) need a longer grace.

**Things to get right:**
- **`authserv_id`** must match the `Authentication-Results` header that the repo's receiving server writes. A human's plain email reply (approve, object, veto) counts only with a DMARC pass under that id. Signed replies (`gud approve` and friends) work on any provider.
- **One address per member.** Agents and the host each need their own inbox.
- **Webhooks.** IMAP wakes the host with IDLE and Primitive with long-poll, so neither needs a public URL. SES and Postmark deliver only by webhook: expose `/api/v1/webhook/<provider>` over HTTPS, on its own port with `host serve --webhook-bind` if the main port stays private.
- **Spam filtering.** `imap_smtp` reads `Junk` as well as `INBOX`, but allowlist the members' domains anyway.
- **Gmail** allows about 500 sends a day on a consumer account, which suits humans and small swarms only.

Every adapter's keys and caveats are in [docs/TRANSPORTS.md](docs/TRANSPORTS.md).

## Transports

| Transport | Verified |
| --- | --- |
| `fake` (on-disk) | the conformance suite in CI, plus fault injection (`FakeNetwork`: delay, duplicate, drop, reorder) |
| `imap_smtp` | against a local GreenMail 2.1.3 (IMAP IDLE, SMTP), including the full end-to-end scenario. Not yet against a hosted IMAP provider. |
| `primitive` | **live** on 2026-10-20: all 11 conformance checks (`wait` 0.60 s after delivery, a 9.9 MB message through), and the end-to-end smoke (18 emails, 72 s; a new file linked 2.9 s after `gud push`). Live intake used long-poll; the webhook path is covered by the mock. |
| `jmap`, `ses`, `postmark`, `gmail` | **against local mocks of their public APIs only**, never against a live account. Each passes the 11 checks against its mock. |

Details, per-adapter caveats and the live harnesses are in [docs/TRANSPORTS.md](docs/TRANSPORTS.md). Postmark exposes no DMARC verdict, so on a Postmark repo humans must sign their replies.

## Evals

`evals/v1/` runs real Claude Code agents (`claude-sonnet-5-5`) on the same task with git or gud as the coordination layer. The git arm uses a protected `main`, a local PR tool, one approval and a pytest merge gate. The gud arms use a real host and tester over the fake transport: `gud-cc` is headless Claude Code on the gud MCP server with the git arm's exact prompt preamble, and `gud-ref` is the reference agent. Hidden acceptance tests that no agent sees score the final tree. See [evals/v1/README.md](evals/v1/README.md).

What the runs show (2026-10-20, N=4 agents, $1.20 per run, three specs built to force collisions):

- **Correctness doesn't separate the arms.** Every run in every arm passed every hidden test (28/28, 27/27, 23/23), including the refactor whose obvious fix breaks an unnamed guard.
- **The first grid found three gud coordination bugs** ([report](evals/v1/runs/20261020-v1-report.md)): T1 was "done" after its first link, agents with no claim were never woken by others' links, and N reference agents acted as one.
- **After the fixes** ([report](evals/v1/runs/20261020-v1-fixes-report.md)), on the library + CLI + API + docs spec, both gud arms split T1 into three subtasks in every run and four authors landed work. `gud-cc` wall time fell from 466 s (369–562) to 78 s (58–98); git took 184 s (128–240) in the first grid. On the two specs with an unsplit task, `gud-cc` runs often ended with all the work linked and T1 never declared done.
- **Done by quiescence** then closed that gap: re-running those three cells, T1 became done in all three (124–150 s, `evals/v1/runs/20261020-v1-quiet/`; no written report).

Read these as smoke results, not a benchmark: one or two trials per cell at N=4; the git arm was not re-run after the gud fixes; everything ran over local transports with no email latency; cost is Claude Code's list-price figure. The reports were written before the v1 rename and call the arms `gud1-cc` / `gud1-ref`, and the milestone-0 smoke report's `gud` arm is the deleted v0.

## Tests

```bash
.venv/bin/python -m pytest -q                 # the suite (tests/v1); GreenMail tests skip without java + .tools/
.venv/bin/python -m pytest -q evals/v1/tests  # the eval harness, with fake agents (no model calls)
.venv/bin/python -m pytest -q tests/v1/e2e/test_fake.py   # the end-to-end scenario, every member a separate process
```

## Coming from v0

v0 (the hackathon prototype: `Gud-*` trailers in the mail body, a lead agent, the MCP-app widget, the scripted evals) is deprecated and deleted. Its last commit is tagged `v0-final`. There is no migration path: v1 does not read v0 mailboxes, and there is no mixed-version repo. To carry a v0 project over, start a v1 repo from its files (`gud init`) or from a git commit (`gud init --from-git`). [docs/CLEAN-BREAK.md](docs/CLEAN-BREAK.md) lists what was removed and why; [CHANGELOG.md](CHANGELOG.md) has the summary.
