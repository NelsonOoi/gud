# gud

**Source control where the repo is an email address.** Every push, vote, test result, task and claim is a signed email to the repo address. The host appends each one it accepts to a signed, hash-chained log. Every member replays the same mail and log and gets the same chain, the same tip and the same blame. There are no branches and no merges: a push names its parent, and if another push lands first, gud carries it forward onto the new tip when the files don't collide.

gud is built for swarms of coding agents, with humans as members too:

- **Consent from blame.** A push that rewrites lines someone else owns opens a short window for them to object. A push that touches nobody's lines links on its own. Ownership fades with time and links. `.gud/owners` adds lazy, explicit or quorum rules per path, and humans can always veto.
- **Tests from a tester member.** A tester replies to pushes and to the tip with signed results, under the repo's `.gud/test` policy: gate pushes, or test the tip, bisect a red tip, and open a fix task for whoever broke it.
- **Leaderless tasks.** Anyone opens a task. Agents claim it with a file scope and a lease, split it into subtasks, and declare it done. Replay derives done-ness, and a quiet, landed task becomes done by itself.
- **Any mail provider.** The protocol needs only what IMAP+SMTP, JMAP, API providers and the Gmail API all offer: raw MIME, a delivery time, a cursor. The envelope travels as a `gud.env` attachment, so providers can rewrite Message-IDs and bodies without breaking anything.

The contract is [SPEC.md](SPEC.md). The Python interfaces are in [docs/INTERFACES.md](docs/INTERFACES.md), provider setup in [docs/TRANSPORTS.md](docs/TRANSPORTS.md), the design record in [docs/PLAN.md](docs/PLAN.md), and measured results in [RESULTS.md](RESULTS.md).

## Install

```bash
python3.11 -m venv .venv            # Python 3.11 or later
.venv/bin/pip install -e .          # the `gud` command
.venv/bin/pip install -e '.[agent]' # plus the reference agent (Claude Agent SDK)
```

## Quickstart

This runs a whole repo on one machine over the `fake` transport, an on-disk mail system, so nothing leaves the machine. Swap `--transport fake --set root=...` for a real provider ([docs/TRANSPORTS.md](docs/TRANSPORTS.md)) to run it over email. Each member has its own `GUD_HOME` (`--home`) and handle (`--handle`, or `GUD_HANDLE`); `--json` makes any command print one JSON object.

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
