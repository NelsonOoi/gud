# gud v1: a portable, email-native replacement for git for agent swarms

> **Design record.** This is the plan gud v1 was built from, kept as written. It describes v0 as it stood then (`gud/replay.py`, `gud/worker.py`, `gud/mcp_server.py`, the lead, `demo_repo/` and so on); all of that was deleted in the v1 clean break (tag `v0-final`, `docs/CLEAN-BREAK.md`). File names it gives for v1 work (`SPEC-v1.md`, `gud1`, `*_v1.py`) are now `SPEC.md`, `gud` and the unsuffixed modules. Where this plan and `SPEC.md` differ, `SPEC.md` wins; SPEC §24 lists every deviation.

## Context

gud started as a working hackathon prototype. The repo is the set of emails at a repo address, pushes are signed diffs, links are decided by replaying the mailbox (`gud/replay.py`), and consent comes from blame. It already runs live over email (the rate-limit demo, the Acme site build). v1 grows that core into a framework people can adopt, in six directions:

1. **Run on any mail provider.** `Mailbox` is already narrow (`send` / `list` / `wait` in `gud/types.py`). v1 makes the protocol depend only on what every provider offers:
   - content-hash ids instead of Message-IDs;
   - verification from standard `Authentication-Results` headers;
   - the diff carried as an attachment, so it arrives byte-exact through any SMTP path;
   - one webhook and intake path that every provider plugs into.
2. **Host real repos of any size.** Byte-exact trees with binary files, file modes and symlinks. Large files split across several emails. Incremental replay, so each new email costs milliseconds instead of a replay from genesis.
3. **Bring real agents.** The prototype's `LLMCoder` (`gud/worker.py`) makes one-shot edit and review calls inside a fixed control loop. v1 lets agents run code, hold a conversation on the thread, and work in a full working copy with the whole repo, its history and other agents' in-flight work in view. They can be any MCP client or a reference agent built on the Claude Agent SDK.
4. **Coordinate without a lead.** Agents claim work with a file scope, split tasks themselves, and finish them through protocol rules. Every signed message is verified the same way, whichever member sent it.
5. **Measure with real agents.** The prototype's gud-vs-git comparisons replay fixed edits (`evals/swarm_vs_prs.py`). v1 runs the same real agents under git and under gud, scored by hidden acceptance tests.
6. **Work alongside git.** Import a git repo, mirror every link to a git remote so CI and deploys keep working, and give humans a CLI for daily use.

**Goal:** a v1 protocol plus a reference implementation where any standards-compliant email setup can host a repo, and a swarm of real agents (bring-your-own via MCP, or a reference agent on the Claude Agent SDK) builds complete repos over email. Incoming mail (provider webhooks, or the equivalent push signal) wakes the agents it concerns within seconds, so conflicting edits are argued out on the thread and settled live inside the consent window, not left to time out. Git stays usable as a downstream mirror.

## Decisions (confirmed)

- **Break the wire format → gud v1.** Content-addressed ids and the diff carried as an attachment. Keep a read-only v0 reader so the existing demos still replay.
- **Agents:** bring-your-own via the MCP server, plus a reference agent built on the Claude Agent SDK and launched with `gud agent`.
- **Git:** `gud import` from a git repo, plus a one-way mirror of every link to a git remote.
- **Providers:** support all four kinds. IMAP+SMTP and Primitive (the existing API provider) are proven first, since they're the baseline and the webhook reference. JMAP, SES or Postmark, and the Gmail API follow (milestone 5).
- **Eval agent until the reference agent exists:** Claude Code sessions driven through the gud MCP server (`livetest/run_claude.py`, fixed in milestone 0). Milestones 0–3 use these. From milestone 4 the reference agent joins them.

### What the four "standards" are

| | Read | Send | New-mail signal | Good for | Watch out |
|---|---|---|---|---|---|
| **IMAP + SMTP** | IMAP (RFC 9051) | SMTP submission (RFC 6409) | IMAP IDLE | The universal baseline: Fastmail, Workspace, M365, self-hosted Dovecot/Postfix | Two protocols and two logins. IDLE drops, so the client must reconnect. Sent mail is a separate copy. |
| **JMAP** | JSON over HTTP (RFC 8620/8621) | same API | push via EventSource | Modern and simple. One API, stable ids, efficient sync (Fastmail, Stalwart) | Few providers support it |
| **API providers** | inbound webhook, or a list API | REST send | webhook | High send volume and cheap (Primitive, SES, Postmark, Mailgun) | Each one's API is different. Inbound needs a public webhook URL. Some rewrite Message-ID. |
| **Gmail API** | REST + `history` sync | REST send | Pub/Sub watch | People who only have Gmail | OAuth, and a send quota of about 500 a day (consumer) or 2,000 a day (Workspace): fine for humans, too low for swarm agents. Rewrites Message-ID. |

**Rule that falls out of this:** the protocol may rely only on what all four provide: send a MIME message, list and wait for messages delivered to an address, the receiving server's delivery time, and the receiving server's `Authentication-Results` header. Nothing else.

## Should members keep a local copy? Yes, as a disposable cache

**Recommendation:** every member, agent or human, works in a **local working copy**. It is never a source of truth. Two kinds of local state, both rebuildable from the mailbox (this keeps the README's rule that "a local copy is a cache"):

1. **Replay cache** (`GUD_HOME/cache/`): the verified chain state, persisted and updated incrementally from the last email processed. Without it, every agent replays from genesis on every check, which is what limits repo size and history length today.
2. **Working copy** (a normal directory with `.gud/BASE` holding the base link's state hash): what agents and humans edit, build and test in.

**Why yes:**
- Real agents need a real directory to run code, tests and builds and to use their native file tools. The current MCP "read file / push whole file" loop is slower, costs more tokens and is more error-prone, and it's part of why LLMCoder edits went wrong in the live runs.
- Big repos can't be re-sent or re-replayed for every action.
- Members can keep working offline or through an email outage.

**Rules that keep it safe:**
- A push is always `diff(working copy, base link)`, signed against that base. The working copy is never sent as state.
- `gud sync` moves local edits onto the new tip with the existing 3-way merge (`three_way` in `gud/mcp_server.py`) and stops on conflict. It never auto-resolves.
- `gud verify` rebuilds the cache from genesis (or from a checkpoint) and diffs it against the local state. It's the existing `gud rebuild`, extended.
- `.gudignore` plus a pre-push secret scan. Every member receives every diff, so a secret pushed once is leaked for good.
- One working copy per agent, the same idea as a git worktree, so agents on one machine never share a directory.
- **Clone cost stays low:** a working copy is materialised from the replay cache, and a new member can start from a signed checkpoint (Phase 2) instead of replaying the full history.

## Scalability review (self-critique)

"Break the wire" here means the **wire format**: the exact layout of a gud email (trailers, how the diff and signature are encoded, what ids reference). v1 emails won't be readable by v0 code, and the reverse. It changes no infrastructure. The only cost is that old demo mailboxes need the small v0 reader.

Measured on the existing design, against "a swarm builds a complete repo":

| # | Bottleneck | Evidence | Severity | Fix in this plan |
|---|---|---|---|---|
| 1 | **One tip, and every push tested on its own exact tree.** At most one link per (test time + email round trip). With N agents on the same tip, one wins and N−1 rebase and re-test, so test runs grow like N². | Run B2: 11 pushes and 7 test runs for 4 subtasks. Rate-limit run: 12 dead proposals for 4 links. | **Critical.** Adding agents makes it slower. | **Phase 2a**: carry-forward, and tests set by policy instead of required on every link |
| 2 | **Replay keeps a full tree and full blame for every link** (`Chain.links`, `LinkState.tree`/`.blame`). Memory grows as links × repo size. | `gud/replay.py:26`, `types.py:62` | **Critical** for real repos | Phase 2: structural sharing |
| 3 | The repo mailbox grows forever. IMAP folders, provider list APIs and the replay intake all slow down past around 10⁵–10⁶ messages. | — | High | Epochs and archive, after checkpoints |
| 4 | One lead plans, assigns and accepts everything, and its signature isn't verified. | `gud/lead.py`, `replay.py:220` | High: a bottleneck and a single point of failure | **Leaderless coordination** (Phase 0b) |
| 4b | Agents see only the files the lead named, truncated. | `worker.py:1037` | High: wrong edits and retries | Full repo context (Phase 4) |
| 5 | Email volume per landed link is about 8 messages (B2: 32 emails for 4 links), plus per-address send limits. | `summary.json` | Medium. Fine on API providers, blocking on Gmail. | Dispatch replaces CC; the test policy cuts test emails to tip runs and acceptance runs |
| 6 | The consent window `W` sets a floor on latency for any push touching another owner's lines. | `SPEC.md` Consent | Medium | Live dispatch makes approvals fast. Ownership fades over time (Phase 0d), so fewer pushes need consent at all. `W` is set per transport (Phase 0c), and `hold` caps the worst case. |
| 9 | **Consent can deadlock.** Blame ownership never expires, a bare objection blocks forever, and two pushes that object to each other both stall. | Run A timed out on exactly this | **Critical** for unattended swarms | **Phase 0d**: consent v1 |
| 10 | **The host is trusted for every received time**, and the audit relies on CC copies, which the plan makes optional. | `SPEC.md` Hosting, `gud audit` | High | **Phase 0c**: signed host log and send-time grace |
| 7 | Warm agent session per thread: tokens grow with the number of threads × context. | — | Medium (cost) | Per-task budgets, and sessions summarised and closed once linked |
| 8 | The plan's scope: four transports, agents, git and events all at once. | — | High (delivery risk) | Re-sequenced milestones below. Prove throughput before adding transports. |

### Phase 2a: throughput (fixes #1)

- **Carry-forward in replay.** When push P loses its race to sibling S, replay **derives** P′ = P rebased onto S, so the agent doesn't re-push. Replay computes this from the same inputs on every machine, so it's deterministic.
  - **File-level by default:** a push is carried only if none of the links that landed in between touched any of P's *files*. That avoids most clashes in logic between edits to different lines. Line-level carry is an opt-in repo setting.
  - **`Gud-Carry: none`** lets an author opt out for order-sensitive changes such as migrations.
  - **What a signature covers (stated in the spec):** an author's signature covers their intent and their change operations against the parent they named. It does *not* cover the final tree. Replay derives the final tree deterministically, and the test policy below decides when tests vouch for it.
  - Consent is recomputed against S's blame (Phase 0d rules). Only *newly* affected owners are asked.
  - The derived link records `carried-from: <P state>`, and blame keeps the record. The author's agent gets a `carried` event, so it can check the result and push a follow-up.
- **Tests are set by policy, not required on every link.** Link rule 4 ("tested") is replaced by a policy in `.gud/test`. The policy is read at the parent state, so replay stays deterministic.
  - `on-paths: <globs>`: only pushes that touch these paths need a passing `test` to link. This is the strict mode for risky areas (migrations, auth). It's empty by default.
  - `on-accept` (always on): a task's `Gud-Accept` criteria must pass on the tip before the task is done (Phase 0b).
  - `on-tip: every N links | every T minutes`: the tester tests the current tip in the background. The default is every 5 links or 10 minutes. These runs don't block anything from linking.
  - `every-link`: v0's behaviour, for repos that want it, run as **batched stacks**. The tester tests `tip + P1 + … + Pk` once and signs one `test` for the stack's tree. If it passes, all k link; if it fails, the tester bisects. Testers can run several stacks in parallel, and `InstaBoxRunner` already parallelises.
- **A failing tip doesn't stop work.** When a tip test fails:
  - Replay marks the tip **red**. Pushes keep linking.
  - The tester bisects back to the last green link, and replay records the first bad link.
  - A `fix` task is opened automatically. It cites the bad link and its author, and anyone can claim it (Phase 0b), with the bad link's author offered it first.
  - Optional `auto-revert: T`: if the tip is still red after T, replay accepts a signed revert of the bad link as a normal push. No agent has to write one.
  - A task can't be done while the tip is red.
- **Agents still test locally before pushing.** They have working copies, but nothing on the chain claims they did. That's what the tester is for.
- **Target**, measured with real agents in the milestone 0 harness (see milestone 1b): at N = 16, links/min does not fall as N goes 4 → 16, and there are fewer test runs than links. The v0 baseline comes from milestone 0, not from an estimate. Also track how often the tip goes red and how long it stays red (median time to green).

### Additions to other phases

- **Phase 2, structural sharing (#2).** A content-addressed object store (blobs and tree nodes) under `GUD_HOME/objects`.
  - A link holds a root hash, not a dict.
  - Blame is stored as per-file incremental deltas.
  - Full trees and blame are materialised only for the tip and the working copy.
- **Phase 2, epochs (#3).** Every K links, or every checkpoint, the host files the closed epoch's mail into an archive folder or label. Replay starts at the latest checkpoint and only reads the archive for `verify` and `blame --full`.
- **Leaderless (#4):** see Phase 0b. Coordination is a protocol rule, not an agent's job, so it scales with the number of agents rather than with one lead's throughput.

## Architecture changes, by phase

### Phase 0: write the v1 protocol (`SPEC.md` → `SPEC-v1.md`)

Spec first. Code is built against it, as today.

- **Content-addressed ids.** A push's id is its `Gud-State` (that hash already exists). `Gud-Parent` and `Gud-Target` hold state hashes. A vote's or test result's id is `sha256(signing bytes)`. Message-ID and `In-Reply-To` stay, but only for human threading. Replay dedupes by gud id, so a message delivered twice (CC copies, provider resends) counts once.
- **MIME layout.** `multipart/mixed`:
  - a `text/plain` part for humans: intent, trailers, and a diff preview truncated at N lines
  - an attachment `gud.env`: the canonical envelope (trailers plus change bytes), base64-encoded.
  The signature covers the envelope bytes, and replay reads only the envelope. This survives re-wrapping, quoted-printable, CRLF conversion and the 998-character line limit.
- **Changes, not only text diffs.** A push carries a list of operations: `patch` (unified diff for text), `put` (whole file by content hash, for binaries or big files), `delete`, `rename`, and `mode` (executable bit, symlink). Trees become `path → (mode, bytes)`. `normalize()` is dropped. Tree hash: `path\0mode\0sha256(bytes)\n`.
- **Size limits.** Each transport reports `max_message_bytes`. Blobs over the per-message budget (7 MB by default) go out as `blob` emails, chunked and content-addressed. A push references them by hash, and replay treats a push as "pending blobs" until they've all arrived.
- **Received time** is the receiving server's delivery time at the repo mailbox: IMAP INTERNALDATE, JMAP `receivedAt`, the provider's `received_at`, Gmail `internalDate`. If a gud id has several copies, the earliest one counts. The host is the only one who assigns received times, and the signed host log in Phase 0c makes that accountable.
- **Signed send time.** Every signed message carries `Gud-Sent: <unix seconds>`, covered by the signature. Votes use it, with a grace period (Phase 0c).
- **Sender verification, simplified.**
  - Signed kinds: the Ed25519 signature *is* the identity. Drop the `From` and `sender_verified` requirement from link rule 1. That removes the hardest provider dependency.
  - Unsigned human replies (`approve`, `veto`, `object`): require DMARC-aligned pass in an `Authentication-Results` header (RFC 8601) whose authserv-id is pinned in the host config.
  - Humans can optionally sign replies with a human key through `gud approve`, which works on any provider.
- **New kinds:**
  - `comment`: signed, free-text, threaded, no effect on the chain. Lets agents genuinely converse: a reviewer asks, the author answers, and the thread goes into both agents' context.
  - `hold`: signed, extends a push's consent window once while a negotiation is still running (see Phase 1b). `Gud-Revises` links a revision to the push it replaces.
  - `checkpoint`: `(seq, state, tree)` co-signed by a quorum of voters, for fast clones.
  - `blob`: chunked file content, as above.
  - `claim`: see Phase 0b.
  - `fix`: a task kind opened automatically by replay when the tip goes red (Phase 2a).
  - `revert`: a push that undoes one named link exactly. Replay accepts it under `auto-revert`.
- **Link rule 4 changes.** "Tested" becomes "meets the `.gud/test` policy at the parent" (Phase 2a). With the default policy, consent alone decides most links.
- **Consent rules change** (Phase 0d), and the host log rules are new (Phase 0c).
- `Gud-Version: 1` on every message. The v0 reader is selected by its absence.

### Phase 0c: signed host log and send-time grace (spec, plus a new `gud/hostlog.py`)

This has the same shape as a blockchain, a hash-chained signed log, without mining or a consensus protocol. It works like Certificate Transparency: one host puts mail in order, and members can catch it cheating.

- **The log.** For every email the host accepts at the repo address, it appends an entry: `(seq, gud id, received time, previous entry's hash)`, signed with a host Ed25519 key. The host's public key is in `.gud/members` (a new role, `host`). The mirror serves the log next to the mail (`GET /api/log?since=`).
  - Replay takes received times **only from log entries**. An email with no log entry doesn't exist as far as the chain is concerned.
  - The host can't change a received time or reorder mail later without breaking the hash chain.
- **Members cross-check each other.** Every signed message carries `Gud-Seen: <seq>:<entry hash>`, the latest log entry its sender has verified.
  - Replay checks that each `Gud-Seen` is on the log it's reading.
  - If the host shows different members different histories, their `Gud-Seen` values eventually conflict. Each member holds a host-signed entry, which together prove the fork.
  - `gud audit` checks this, and no longer needs CC copies.
- **Votes count by signed send time, with a grace period.** An `object`, `approve` or `hold` counts toward a consent window if its `Gud-Sent` is before the window closes **and** its log received time is within `G` of the close.
  - This covers mail that's slow in transit (SMTP greylisting, retries) as well as a host holding a vote back. So "settled live" works on IMAP/SMTP as long as `G` is longer than the provider's normal delay.
  - A vote with a `Gud-Sent` in the future (beyond a skew of S seconds) is ignored.
  - Cost: a contested push links up to `G` later, and senders' clocks are trusted to within `G`.
- **`W` and `G` are set per transport.** `gud host init` and `gud doctor` measure delivery latency (p99) and write `W` and `G` into `.gud/config`, which is in the tree. Changing them is an ordinary push, so every member replays with the same values.
- **Host migration** becomes verifiable: the new host's log begins with an entry citing the old host's last entry hash, and the old host signs the handover. A members push records the new host key.

### Phase 0d: consent v1 (spec, plus `gud/rules.py`)

These rules replace v0's: blame ownership that never expires, objections that block forever, and deadlocks no one resolves.

- **Ownership fades.** A line's author can block changes to it only for a limited time: the last K links, or T hours, whichever is longer. Both are set in `.gud/config`, with defaults of K = 50 and T = 24h. After that the line is open to anyone, under lazy consensus and the test policy. Blame still records who wrote it.
- **Declared owners.** A `.gud/owners` file maps path globs to members, like CODEOWNERS, for areas someone really is responsible for.
  - Declared owners always have a say on their paths, however old the lines are.
  - Each glob has a rule: `lazy` (can object within `W`), `explicit` (must approve) or `quorum`. This file replaces `.gud/protected`, and `.gud/owners` itself is always `quorum`.
- **Objections need evidence to block for good:**
  - An objection that carries a **failing test** (a test file, sent as a `blob`, that the push breaks) blocks until a revision passes it. The tester runs it. It counts as evidence only if it passes on the parent and fails on the push. If the push is accepted, the test is added to the tree.
  - An objection with only a **reason** blocks once: the author must answer with a revision (`Gud-Revises`) or a `comment`. After that the objection becomes advisory, and the push links when the window closes, unless the objector escalates.
- **Deadlock rule.** Replay escalates automatically when either of these happens:
  - two open pushes object to each other
  - one push has been objected to in two rounds.

  Both positions go in one email to the task's requester and sponsor. The requester's `approve` on one push decides. If there's no answer within the escalation timeout, the earlier-received push wins, by the same rule as a race.
- **Escalations never wait without a deadline.** Every escalation has a timeout and a deterministic default outcome. Unattended swarms can't stall the way run A did.

  The claim system in Phase 0b reduces how often these rules are needed, because overlapping work is caught before anyone writes code.

### Phase 0b: leaderless coordination (spec plus `gud/tasks.py`, `gud/replay.py`)

Nothing in the protocol needs a lead. "Lead" stops being a role. A planner is just an agent that chooses to post subtasks.

- **Tasks** can be opened by any member or verified human, as now. A task may carry acceptance criteria: `Gud-Accept`, holding test paths or commands, and spec checks.
- **`claim`** is a new signed kind: "I'm taking task or subtask X". Replay settles competing claims the same way it settles races: the earliest received claim wins, deterministically.
  - A claim is a **lease**. If the claimant sends no push, `comment` or renewal within `L` (configurable), the claim lapses and anyone can claim the work.
  - That replaces the lead's reassignment of stale subtasks.
- **A claim declares its file scope:** `Gud-Files: styles.css, index.html`, globs allowed.
  - **Overlapping claims are caught before coding.** If a new claim's scope overlaps an active claim's, both holders get a `claim.overlap` event (Phase 1b). They settle it in `comment`s: split the files, sequence the work with `Gud-After`, or have the later claim narrow its scope with a signed claim update.
  - **The scope is advisory.** A push outside its claim's scope still links normally, but the holders of overlapping claims get a `scope.exceeded` event.
  - The scope also feeds the consent forecast: when an agent claims, it's told which declared owners and recent authors its change is likely to need.
- **Self-splitting.** The holder of a claim may post `subtask`s under it, with `Gud-After` for dependencies, which already exists. Other agents claim those.
  - Big tasks break down recursively, with no central planner.
  - A split counts only while its author holds the parent claim.
- **Done.** A subtask is done when a link carries its id, or when an accepted `no-change` covers it (exists).
  - A task is done when all its subtasks are done **and** its `Gud-Accept` criteria pass on the tip, which a tester reports as a `test` targeting the task.
  - If there are no criteria, the requester replies `accept`.
  - Replay derives `task-done`, so no agent has to send it. The requester notification goes from whichever member's dispatcher observes it first, deduped by task id.
- **Escalation** (objected 2×, lease thrash, budget exhausted) goes straight to the task's requester and the sponsor, via a dispatch event.
- **Bug triage** already lets any voter other than the reporter review. Drop the lead-only path in `gud/bugs.py`.
- **Remove** the unchecked-lead-signature exception (`replay.py:220`). Every signed kind is verified against `.gud/members`.
- **Migration:** the scripted demo can keep a "planner" agent that claims the task and splits it, as `ScriptedPlanner` does now. It becomes an ordinary member.

### Phase 1: portable transport layer

- `gud/types.py`: keep `send` / `list` / `wait` and add `capabilities()`, which reports max size, push vs poll, whether Message-ID is preserved, `Authentication-Results` availability and send quota. `Email` gains `raw_mime: bytes` and `auth_results: str`, and drops the provider-specific `sender_verified` (that becomes a computed property).
- New `gud/transport/`, one module per adapter, each a pure `Mailbox`:
  - `imap_smtp.py`: imaplib/`imapclient` with IDLE and reconnect, plus smtplib submission. Uses UIDVALIDITY+UID as the cursor, and reads the junk folder too.
  - `jmap.py`: Email/query and Email/changes, EventSource push, and EmailSubmission.
  - `primitive.py`: move `gud/mail_primitive.py` here, minus the inline-body assumptions.
  - `ses.py` or `postmark.py`: outbound REST plus inbound webhook. Reuses the `HostedMailbox` + `webhook.py` pattern (cache fed by webhook, `catch_up` on start).
  - `gmail.py`: OAuth, `history.list` sync, Pub/Sub or polling.
- Shared parsing: `gud/transport/mime.py` (build and parse the v1 MIME layout) and `gud/transport/auth.py` (an `Authentication-Results` parser, generalised from `_auth_verified` in `mail_primitive.py`).
- `gud/mailbox.py`: an adapter registry keyed by `GUD_TRANSPORT`, with config per member, so every member can use a different provider.
- **Conformance suite** (`tests/transport_conformance.py`), run against every adapter. It checks round-trip byte-exactness of the envelope, dedupe, received-time ordering, `wait` wake-up latency, the large-blob path, and verification of a spoofed human `From` (must fail).
- `gud host init --transport X`: checks DNS (SPF/DKIM/DMARC), creates or validates the repo address, warns about spam filtering, sends a round trip, and records the authserv-id. `gud doctor` re-runs the conformance probe against a live account.
- **Every adapter has a push intake path**, normalised to one internal "email arrived" signal:
  - API providers: inbound webhook.
  - JMAP: EventSource.
  - IMAP: IDLE.
  - Gmail: a Pub/Sub watch.
  Polling is the fallback and runs only for catch-up after a reconnect. Phase 1b turns that signal into agent wake-ups.
- **CC fan-out becomes optional.** Agents are woken by the host's event dispatch (Phase 1b), not by CC copies landing in their inboxes. That's about 10× fewer sends, which makes quota-limited providers workable. CC stays as a fallback for members who only have an inbox.

### Phase 1b: live event pipeline (webhook in → agent woken → reply out)

The core loop: an email arriving at the repo triggers the right agent within seconds, and its reply goes out as email, so conflicting edits are argued out and settled inside the consent window instead of when the window times out. Today the pieces exist but aren't joined: `gud/webhook.py` (intake for one provider), `/api/events` in `gud/api.py` (SSE for the UI, which re-replays the chain to find changes), and the polling `Inbox` / `Gate` in `gud/inbox.py`.

- **Intake** (`gud/events/intake.py`): the host's single entry point for "an email arrived". It's fed by each adapter's push path from Phase 1, and generalises `webhook.py`:
  - per-provider signature checks (HMAC for Primitive, SNS signatures for SES, Postmark basic auth, a Pub/Sub JWT for Gmail)
  - dedupe by gud id, because webhooks are delivered at least once
  - append to the cache, then an incremental replay step (Phase 2), which returns *what changed*.
- **Chain events** (`gud/events/model.py`): typed results of that replay step:
  - `push.received`, with affected owners forecast from blame
  - `review.requested`, `objected`, `commented`, `approved`
  - `test.passed` / `test.failed`, `superseded`, `window.closing` (T−N seconds), `linked`
  - `task.opened`, `subtask.posted`, `claim.won`, `claim.lapsed`, `claim.overlap`, `scope.exceeded`, `task.done`, `escalated`
  - `carried`, `tip.red` (with the bad link once bisect finds it), `tip.green`, `fix.opened`
  - `host.fork` (two members' `Gud-Seen` disagree), sent to every member and the sponsors
  Each one names the members it concerns, using today's CC rules from `SPEC.md` with "the lead" replaced by "the claim holder". Open-work events (`task.opened`, `subtask.posted`, `claim.lapsed`) go to every agent member, so anyone can claim the work.
- **Dispatch** (`gud/events/dispatch.py`) to each concerned member, through one of:
  1. **Agent webhook:** an HTTP POST to the member's registered URL, signed by the host (Ed25519 host key over the body and a timestamp, verified like `hostauth.py`).
  2. **SSE stream** from the host mirror, for agents behind NAT. It reuses `/api/events`, but filters by member and needs signed mirror auth.
  3. **CC email** as the fallback.

  Delivery is at least once with retries and an event id, so agents dedupe. On reconnect, an agent catches up from its last event id.
- **Negotiation sessions** in the agent runtime (Phase 4). Events for the same push thread go to one warm agent session per agent, so the author and owner keep their context across turns. The flow:
  1. The owner gets `review.requested` and replies `object` with a reason, or a `comment` asking a question.
  2. The author gets `objected` or `commented` in the same live session and answers or pushes a revision.
  3. The owner approves, and it links.

  Events that land while a session is mid-turn are batched over a short debounce (about 1s).
- **Protocol support for live settlement** (added to Phase 0):
  - `comment`: signed, threaded, no chain effect. This is the conversation channel.
  - `hold`: the affected owner extends the consent window once, by `W`. It stops a live negotiation from being cut off by the timer. It's signed, at most one per owner per push, and replay applies it deterministically.
  - A revision cites the push it replaces (`Gud-Revises: <state>`), so the thread and the dashboard show one negotiation, not unrelated pushes.
- **Latency target:** under 5s from an email being received at the repo to the concerned agent's session starting, over an API-provider webhook. Measure it every hop, and add it to `summary.json` in livetest (`negotiation.py` already tracks objection timing).

### Phase 2: core repo model at scale

- `gud/wire.py`, `gud/patch.py`, `gud/sig.py`: v1 envelope, change ops, byte trees, modes, renames, the new tree hash.
- `gud/replay.py`: incremental replay. Persist the evaluated chain, keep open windows as pending, and re-evaluate only children whose inputs changed. The existing `cache` dict inside `replay()` becomes a persisted store. Add checkpoint verification and starting a replay from a checkpoint.
- **Blame and consent for binaries:** the owner of a binary file is whoever last wrote it. A `put` over someone else's file needs that owner's consent.
- **A v0 → v1 reader shim** in `gud/replay.py`, so `demo_repo` and the 18 Oct Primitive mailboxes still replay.

### Phase 3: working copy and human CLI (`gud/cli.py`)

- `gud clone <repo-address> [dir]`: join or verify, replay (from a checkpoint if there is one), and materialise. Generalises `export` in `mcp_server.py`.
- `gud status`, `gud diff`, `gud push -m "intent" [--task T3.1]`, `gud sync`, `gud log`, `gud blame`, `gud verify`.
- Reviewing and tasks: `gud review` (pending items that need me), `gud approve`, `gud object`, `gud veto`, `gud comment`, `gud task new`, `gud tasks`.
- `.gudignore` and the secret scan on push.

### Phase 4: real agents

- **Full repo context, by default.** No file allowlist and no truncation. `_relevant` and `_edit_prompt` (`worker.py:1037`) go away. Every agent gets:
  - **The whole repo**, as a working copy. It reads, searches and runs whatever it decides it needs, with its native tools.
  - **History and ownership:** `log`, `blame`, and `read` of any path at any link. It can read the thread behind any line ("why is this here?", answered by the link's email and its replies).
  - **In-flight work:** `inflight` lists open proposals from other agents, with their diffs, claims and threads. An agent can see a colliding edit before it pushes and coordinate in a `comment`.
  - **The task board:** every task, claim, subtask and acceptance criterion, not only its own assignment.
  - **Big repos:** a local search index (ripgrep, plus optional embeddings) over the working copy. Context is something the agent pulls when it needs it, not something pushed to it.
- **MCP server** (`gud/mcp_server.py`): make it working-copy aware.
  - New tools: `clone`, `sync`, `push_working_copy`, `comment`, `claim`, `inflight`, `inbox` (what needs me), `wait`, `search`.
  - Keep `read` / `ls` / `blame` (any path, any link) for agents with no filesystem.
  - Each session gets its own key and working copy.
  - Fix and turn `livetest/run_claude.py` into a CI test.
- **Reference agent** (`gud/agent/`, `gud agent --handle @x`; `gud tester --handle @t` runs the tester):
  - It receives Phase 1b events, either as a webhook receiver (`gud agent --listen :PORT`, registered with the host on join) or as an SSE subscriber. Events go to the warm negotiation session for that thread, or start a new Claude Agent SDK session. Polling the mirror is the fallback.
  - The session has the working copy as its working directory, native Read/Edit/Bash tools, the gud MCP tools, and one general prompt.
  - Defaults: `claude-sonnet-5-5`, overridable per member. Other vendors' agents join through MCP.
  - **One general agent, not a set of fixed roles.** On a wake event, the same agent decides what to do:
    - Claim open work it can do, splitting big tasks into subtasks if it holds the claim.
    - Sync, explore the repo, implement, run tests locally, and push.
    - Review pushes where it has a say under consent v1 (lines it wrote recently, or paths it's a declared owner of): read the diff and thread, run the code if needed, and reply approve, object (with a failing test where it can) or comment.
    - Answer comments and objections on its own pushes.
  - Context carries across a thread, so a reviewer no longer judges each revision from scratch.
  - **Budgets** (tokens and dollars) apply per member and per task. When a budget runs out, the work is escalated.
- **Tester** (`gud tester`, a separate process, not a model). It reads `.gud/test`, a new file giving the command, image, timeout and test policy (Phase 2a), so tests are no longer hard-coded to pytest in `gud/build.py`.
  - Runners: Local, Docker, InstaBox (exists).
  - It runs:
    - `on-paths` pushes, and batched stacks in `every-link` mode
    - background tip runs, and bisects when a tip run fails
    - task acceptance criteria (Phase 0b)
    - failing-test objections (Phase 0d)
  - Multiple tester members are allowed.
- **Scripted agents are for protocol unit tests only:** determinism, consent edge cases, and storage and replay microbenchmarks, labelled as such. They're never used to claim how agents behave, how fast they are, or how they compare with git. Retire `LLMCoder` once the reference agent passes the e2e suite.
- **Safety:** treat all email content as untrusted data in prompts. Agents only act on signed mail from current members. Anything from `bugs@` goes through triage first, as today.

### Phase 5: git interop (`gud/gitbridge.py`)

- `gud import <git-dir> [--ref main]`: builds a byte-exact genesis tree, plus `.gud/members` from a roster file. History isn't imported; record the source commit in the genesis intent.
- `gud mirror git <remote>`: a member process that turns each new link into a commit. Author = handle, message = intent + `Gud-State` trailer, parent = the previous mirrored commit. Push-only and append-only, never force. CI, deploys and GitHub readers then work unchanged.

### Phase 6: security and operations

- **Key management:** key rotation as a members push, recovery through sponsor approval, and revocation, which exists today. The host key rotates the same way, recorded in the log (Phase 0c).
- **Host migration and backup:** `gud export-archive` (mbox plus the signed host log) and `gud import-archive` to a new address, using the signed handover in Phase 0c. `gud audit` (extended to check the log and `Gud-Seen`) runs as a periodic check.
- **Abuse:** unsigned mail from non-members costs at most a parse, so set a rate limit on the replay intake. Put a per-sender cap on `bugs@`.
- **Later:** optional body encryption to member keys for private repos. Until then, document that email is plaintext between providers apart from opportunistic TLS.

## Critical files

- **Rewrite:**
  - `SPEC.md` → `SPEC-v1.md`
  - `gud/wire.py`, `gud/types.py`, `gud/patch.py`, `gud/replay.py`, `gud/mailbox.py`
- **Move or generalise:**
  - `gud/mail_primitive.py` → `gud/transport/primitive.py`
  - `gud/webhook.py` and `gud/mail_hosted.py`, which become the generic webhook host
  - `gud/mail_fake.py`, which becomes v1 MIME
- **New:**
  - `gud/transport/{imap_smtp,jmap,ses,gmail,mime,auth}.py`
  - `gud/events/{intake,model,dispatch}.py`. These absorb `gud/webhook.py`, plus the change detection in `/api/events` in `gud/api.py`.
  - `gud/agent/`, `gud/gitbridge.py`
  - `tests/transport_conformance.py`
- **Extend:**
  - `gud/cli.py`, `gud/mcp_server.py`
  - `gud/build.py` (`.gud/test` policy, bisect)
  - `gud/tasks.py` (claims with file scope, leases, derived done, `fix` tasks)
  - `gud/rules.py` (consent v1: fading ownership, `.gud/owners`, evidence objections, deadlock escalation)
  - `gud/cli.py` `cmd_audit` (log and `Gud-Seen` checks)
- **New:** `gud/hostlog.py` (the signed append-only log: append, verify, detect forks, hand over to a new host)
- **Shrink:**
  - `gud/lead.py` becomes an optional planner agent.
  - `gud/worker.py`: `LLMCoder`, `_relevant` and `_edit_prompt` are removed.
  - `gud/bugs.py`: the lead-only triage path is removed.
- **Reuse as is:**
  - `three_way` in `mcp_server.py`
  - `RemoteMailbox` in `mail_remote.py` and signed mirror access in `hostauth.py`
  - `cmd_audit`, `affected_owners` and consent rules in `rules.py`
  - `ScriptedPlanner` in `lead.py`, only as an ordinary planner member in protocol unit tests (Phase 0b migration)

## Milestones and exit criteria

Re-sequenced so the scaling risks are retired first, and transports are added only after the core scales.

0. **Real-agent eval harness first** (`evals/`, replacing the scripted `swarm_vs_prs`).
   - **Same agent, same model, same tools, same task spec, same budget.** The only variable is the coordination layer:
     - **git:** a worktree per agent, PRs, review and CI before merge
     - **gud:** a working copy per agent, the email chain.
   - **Tasks:** a set of complete-repo specs (the Acme site, a Python CLI with tests, a small web API), each with hidden acceptance tests the agents never see.
   - **Reported metrics:**
     - hidden-test pass rate and spec checks
     - wall time and dollars
     - tokens per agent
     - conflicts and how each was resolved
     - human escalations
     - links/min, test runs per link, emails per link.
   - **Scale:** N = 4, 8 and 16 real agents, with ≥ 3 trials per cell. Reported as mean and range, the same as `livetest/compare.py`.
   - **Baseline:** run it against v0 now. The gud arm uses the eval agent (Claude Code via MCP), so `livetest/run_claude.py` is fixed here first.
   - A separate scripted microbenchmark (100–500 agents) measures only replay, storage and memory.
1. **v1 protocol and fake transport,** including the host log (0c), consent v1 (0d) and the test policy (2a). The existing test suite has been ported to v1. Determinism test: the same email set and host log replay to the same tip in shuffled delivery order.
1a. **Leaderless coordination (Phase 0b).** Claims with file scope, overlap events, leases, self-splitting and derived `task-done` all work, and the lead-signature exception is removed. A swarm of eval agents with no planner member finishes the Acme spec. In the transcripts, overlapping claims are settled in `comment`s before anyone pushes.
1b. **Throughput and storage (Phase 2a, plus Phase 2's incremental replay and structural sharing).**
   - With eval agents: at N = 16, links/min does not fall as N goes 4 → 16, and there are fewer test runs than links. Under the default test policy, the median time from a red tip back to green is under 10 minutes, and no task finishes on a red tip.
   - Scripted microbenchmark: replay over 10k links of a 50 MB repo uses memory bounded by the object store, not links × tree. An incremental replay step over 10k emails takes under a second, which the event pipeline in 2b depends on.
2. **IMAP+SMTP adapter, and Primitive moved to `gud/transport/`.** Both pass conformance: IMAP+SMTP against a local Dovecot/Postfix (Docker) and one hosted provider (Fastmail or Workspace). The Acme eval spec runs end to end on each, with eval agents and no planner.
2b. **Live event pipeline.** The Primitive webhook intake and IMAP IDLE produce chain events, and dispatch goes to agent webhooks and SSE. Eval agents pass the live-settlement scenario (see Verification). Every hop is under 5s on Primitive, and the consent window is set for IMAP's measured delivery time.
3. **Working copy and human CLI (Phase 3).** A human can clone, edit, sync, push and approve from the terminal. Eval agents use `gud clone` working copies instead of MCP `export`.
4. **Reference agent and MCP v1.**
   - A leaderless swarm of general reference agents plus a tester builds each eval spec. On the hidden tests, it matches or beats the git arm of milestone 0.
   - Bring-your-own Claude Code and Codex sessions join the same repo through MCP, with full repo context.
5. **JMAP, SES/Postmark and Gmail adapters.** Each passes conformance. Gmail is documented as for humans and small swarms only.
6. **Git import and mirror, security and ops.** A mirrored GitHub repo runs CI on every link.

## Verification

- **Unit:** `.venv/bin/pytest -q` on the v1 suite, plus the v0 reader replaying `results/swarm-primitive.md`'s mailbox to its recorded tip (`89e7c09e3566`).
- **Conformance:** `pytest tests/transport_conformance.py --transport <x>` against each adapter. Dockerised Dovecot/Postfix and Stalwart (JMAP) run in CI; hosted accounts run nightly.
- **Real-agent evals** (milestone 0 harness): gud vs git with the same agents and hidden acceptance tests, at N = 4, 8 and 16, with ≥ 3 trials. They run nightly with a budget cap, and a regression against the previous release fails.
- **Scripted microbenchmark** at 100 and 500 agents, for replay and storage only.
- **Context check:** in eval transcripts, agents read files outside their claimed area when they need to. The test is to seed a task whose fix needs a guard in an unrelated file. Count `inflight` and `comment` use before colliding pushes.
- **Carry-forward determinism:** shuffle delivery order with fixed received times. The derived links and tips must be identical across runs and machines. A file-level overlap must *not* be carried, and `Gud-Carry: none` must be honoured.
- **Host log (protocol tests):**
  - A host that rewrites one received time breaks verification.
  - A host that shows two members different orders is caught as `host.fork` from their `Gud-Seen` values.
  - A vote held back by up to `G` still counts. One held back for longer than `G`, or with a future `Gud-Sent`, doesn't.
- **Consent v1 (protocol tests):**
  - An expired-ownership line links without asking its author.
  - A declared owner still has a say.
  - A reason-only objection blocks once and is advisory after a revision. A failing-test objection blocks until a revision passes the test.
  - Two pushes that object to each other escalate, and resolve to the requester's choice or to the default by the timeout.
- **Test policy (protocol tests):**
  - A red tip opens a `fix` task naming the first bad link found by bisect.
  - `auto-revert` lands the revert after T.
  - A task can't be done on a red tip.
  - `on-paths` pushes don't link without a pass.
- **Cross-provider determinism** (a protocol test, so scripted is fine): send the same scripted email set through two transports and compare tips and blame.
- **End to end:** `livetest/run_live.py` (moved onto the eval agent, and the reference agent from milestone 4) per transport, with no planner member, checking `summary.json` outcome `done`, final tests passing, and spec checks on the final tree (for example, pricing is a real `<table>`).
- **Live settlement:** a spec whose natural implementations collide on shared files (one stylesheet and the nav on every page), with no scripted clashing demands like `overlap-4`'s. Real agents run it over webhooks, and again over IMAP/SMTP with `W` and `G` from `gud doctor`. It passes when:
  - overlapping claims are settled in comments before coding, or
  - every objected push is revised and linked, or escalated and resolved by the deadlock rule, before its window (plus `G`) closes.

  There must be no unresolved stalls. Report per-hop latency: received → event → session start → reply sent → reply received. Repeat with webhooks off (polling only) to show the difference.
- **Event delivery:** replay duplicated and out-of-order webhooks, and kill and restart an agent mid-negotiation. It must resume from the last event id without double-replying.
- **Git:** `gud mirror git` to a scratch remote, then `git log` and `git fsck` on the mirror, and check that the tree at each commit hashes to the link's `Gud-Tree`.
