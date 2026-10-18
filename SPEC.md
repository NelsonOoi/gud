# gud wire spec

This is the contract every module builds against. If code and this file disagree, fix the code or change this file on purpose.

## Concepts

- **Mailbox.** The repo is the set of emails delivered to the repo address (`GUD_REPO_ADDRESS`). Emails are stored by Primitive in production, or by `gud.mail_fake.FileMailbox` locally. Nothing else is a record.
- **Host.** Whoever owns the repo address's mailbox. The host serves a read-only mirror of that mailbox to members (see "Hosting and member accounts"). The host can withhold or retime mail but cannot forge it, because everything that changes the chain is signed.
- **Member.** A line in `.gud/members` in the tree at some state. Agents sign; humans are trusted by verified sender address. Each member uses **their own** mail account and credentials; nobody shares an API key.
- **Push.** An email with `Gud-Kind: push` carrying a unified diff against its parent push.
- **Link.** A push that satisfies the four link rules. The chain of links from genesis is the history.
- **Tip.** The last link. Its tree is the repo.

## Email shape

All gud emails are sent **to the repo address**. The agent members the email concerns are in `Cc` (see "CC list" under `.gud/members`). `In-Reply-To` and `References` follow normal threading.

Body layout, in order:

1. Free text (the intent, or a human's reply).
2. A blank line, then **trailers**: one per line, `Gud-<Name>: <value>`. Trailer names are case-insensitive on parse and rendered in the canonical case shown below.
3. Optionally, a blank line and exactly one fenced block starting with a line of three backticks plus `diff`, and ending with a line of three backticks. That block is the diff, byte-exact.

Parsing rules (`gud.wire.parse_body`):

- The diff is the content of the **last** fenced `diff` block, excluding the fence lines.
- Trailers are lines outside the fence matching `^Gud-[A-Za-z-]+:\s?(.*)$`. Later duplicates overwrite earlier ones.
- Free text is everything before the first trailer line, stripped.
- A message without a `Gud-Kind` trailer is a **human reply** (see below).

## Kinds and trailers

| Kind | Required trailers | Optional | Signed |
| --- | --- | --- | --- |
| `push` | Author, Parent, Base, State, Tree, Sig | Task, Consent | yes |
| `object` | Author, Target, Reason, Sig | | yes |
| `approve` | Author, Target, Sig | | yes |
| `test` | Author, Target, Result (`pass`/`fail`), Tree, Sig | | yes |
| `task` | Author, Task, Requester, Sig | | yes |
| `subtask` | Author, Task, Parent-Task, Assignee, Sig | After | yes |
| `task-done` | Author, Task, Tip, Sig | | yes |
| `no-change` | Author, Task, Tip, Reason, Sig | | yes |
| `notify` | Author | Task, Target | no (informational, never affects the chain) |
| `join` | Author, Role, Address, Key, Sig | Sponsor, Reason | yes, by `Key` itself (no chain effect; input to a membership push) |

Field meanings:

- `Gud-Author`: member handle, always with a leading `@`.
- `Gud-Parent`: message id of the parent push, with angle brackets. Genesis uses `none`.
- `Gud-Base`: state hash of the parent (64 hex). Genesis uses 64 zeros.
- `Gud-State`: state hash after this push (64 hex).
- `Gud-Tree`: tree hash after this push (64 hex).
- `Gud-Consent`: comma-separated message ids of approvals the author is citing.
- `Gud-Target`: message id of the push being objected to, approved, or tested.
- `Gud-Result`: `pass` or `fail`.
- `Gud-Task`: task id like `T7`, or subtask id like `T7.1`.
- `Gud-Tip`: on `task-done`, the tip the task finished on. On `no-change`, the link the assignee checked.
- `Gud-Reason`: one line. On `object`, why. On `no-change`, why the subtask needs nothing.
- `Gud-Sig`: `ed25519:<base64 signature>`.
- `Gud-Role`, `Gud-Address`, `Gud-Key`, `Gud-Sponsor` (join only): the requested member line. `Gud-Author` is the requested handle. `Gud-Key` is the requester's public key, and `Gud-Sig` must verify with it.
- `Gud-Join` (optional on a membership push): message id of the join request it admits.

## Human replies

A human member replies in plain text, threaded to the push (`In-Reply-To` is the target message id). The first word of the free text decides the meaning, case-insensitively:

- `approve` means approve.
- `veto` means veto.
- `object` means objection; the rest of the text is the reason.
- Anything else is a comment (it can carry a condition for agents, but it has no chain effect).

A human reply counts only if the mailbox marks it `sender_verified` and its `From` matches a `human` member's address at the target's parent state.

## Hashes

All hashes are lowercase hex sha256. Implemented in `gud.wire`.

- **Tree hash**: for each path in sorted order, feed `path + "\0" + sha256(content_utf8) + "\n"`. Empty tree hashes the empty string.
- **State hash**: sha256 of `parent_state + "\n" + author + "\n" + ",".join(sorted(consent_ids)) + "\n"` followed by the diff bytes (UTF-8).
- Display form is the first 12 characters.

Tree contents are UTF-8 text, and every file ends with exactly one trailing newline (`gud.wire.normalize`).

## Signatures

Ed25519 (`gud.sig`). The signed bytes (`gud.wire.signing_bytes`) are:

```
<every Gud-* trailer except Gud-Sig, as "Name: value\n", sorted by Name>
--
<diff text, or empty>
```

Agents sign with their private key. Verification uses the public key from `.gud/members` at the **parent** state (for a push) or at the **target's parent** state (for object/approve/test).

## `.gud/members`

One member per line; `#` starts a comment; fields are whitespace-separated:

```
handle role address public_key sponsor
@auth-agent agent auth-agent@gud.primitive.email ed25519:BASE64 @nelson
@nelson human nelson@example.com - -
```

- `role`: `agent`, `lead`, `tester`, `console`, or `human`.
- `public_key`: `ed25519:<base64 raw 32 bytes>`, or `-` for humans.
- `sponsor`: the human handle CC'd when someone asks this agent's owner a question, or `-`.
- **Voters** for quorum: roles `agent`, `lead`, `human`. Testers and the console do not vote.
- **CC list**: only the non-human members an email concerns, never the sender. CC never affects the chain (replay reads the repo mailbox), it only decides whose inbox wakes up.
  - `subtask`: the assignee.
  - `push`: testers, the lead, and the push's affected owners.
  - `approve`, `object`, `test`: the push's author and the lead.
  - `task`: the lead.
  - `task-done`: the subtask assignees.
  - `no-change`: the lead.
  - Anything else (genesis, membership pushes): every non-human member.

## `.gud/protected`

One rule per line: `<glob> <rule>`, rule is `explicit` or `quorum`. Globs use `fnmatch` against repo-relative paths. `.gud/members` and `.gud/protected` are always `quorum`, even if unlisted.

## Link rules

A push P with parent X is a **link** when all hold:

1. **Authorship.** `Gud-Author` is an agent-type member (any role except `human`) in X's members; `Gud-Sig` verifies with that member's key; `From` equals that member's address; `sender_verified` is true.
2. **Applies.** `Gud-Base` equals X's state hash, the diff applies cleanly to X's tree, and the resulting state and tree hashes equal `Gud-State` and `Gud-Tree`.
3. **Consent** (below).
4. **Tested.** There is a `test` email from a `tester` member at X targeting P with `Result: pass` and a matching `Tree`. A `fail` result means P never qualifies.

Then, among X's children that qualify, the one with the **earliest received time** is X's next link. If an earlier-received child is still pending (it might qualify later), X has no next link yet, with one limit: a child waiting on people (`needs_approval`, `needs_quorum`, or `objected`) holds its later siblings back only until `received + W`. After that it is **parked**: it can still qualify, but a later sibling may link first, and then it is superseded and must be re-pushed on the new tip.

The next link is decided at the earliest moment the rule above picks one, using only mail received by that moment. Mail that arrives later never changes an existing link.

## Consent

Computed against X's tree and X's blame.

- **Affected owners**: owners of every line P removes or rewrites, plus, for lines inserted inside a Python function or class, the owner of that symbol's `def`/`class` line. Excludes P's author and anyone who is not a voter in X's members.
- **Protected**: any touched path matching an `explicit` or `quorum` rule. The strictest rule wins (`quorum` > `explicit` > none).
- **Veto**: a verified human `veto` reply targeting P, received before P qualifies, blocks P forever.

Rule by case:

- **Unprotected, no affected owners**: consent holds.
- **Unprotected, with affected owners (lazy consensus)**: consent holds at `received(P) + W` if no affected owner sent an `object` (or human `object` reply) targeting P with received time before `received(P) + W`. It holds immediately if every affected owner has approved (an `approve` targeting P, or an approval cited in `Gud-Consent`).
- **`explicit`**: every affected owner approved. Objections are irrelevant; missing approvals keep it pending. If there are no affected owners (for example a new file under a protected glob), one approval from any voter other than the author is required.
- **`quorum`**: approvals from more than half of X's voters (`len(voters) // 2 + 1`), excluding any voter whose membership line this push adds, removes or changes. The author counts as approving if they are an eligible voter.

Consent holds at a time: the window close, the last required approval's received time, or the push's own received time. A push **qualifies** at the later of its consent time and its passing test's received time.

`W` is `GUD_WINDOW_SECONDS` (default 60). Everything is judged on mailbox received times, so rebuild is deterministic once windows have closed.

## Tasks

- `task` from `@console` (or any member) opens task `T<n>`. Subject is the title; free text is the description; `Gud-Requester` is the human handle.
- A human member opens a task by mailing the repo address in plain text (no `Gud-Kind`, no `Gud-Sig`, not a reply) when `sender_verified` is set and `From` matches their address. The lead sends the signed `task` itself — title from the subject, description from the body with trailer-shaped lines neutralized, `Gud-Requester` that human, `Gud-Source` the source message id — and splits it with no review. Mail from anyone else or an unverified sender is ignored (they use the bugs address). The lead never opens a second task for the same source, including after a restart (a task already cites `Gud-Source`) and while its own send is still in flight.
- `subtask` from the lead assigns `T<n>.<m>` to `Gud-Assignee`. Its free text carries the parent task's title and description, so the assignee works from the email alone. `Gud-After: T<n>.<a>, ...` lists subtasks that must land first; the assignee starts only after they have (a landed link or an accepted `no-change`).
- A subtask is **done** when a link carries `Gud-Task: T<n>.<m>`, or when a `no-change` from its current assignee is accepted.
- `no-change` is how the assignee says the tip already satisfies the subtask. It is signed by that assignee. `Gud-Task` is the subtask id, `Gud-Tip` is the link they checked, and `Gud-Reason` is one line (the free text is the same line). Anyone else is ignored, including a former assignee. It is accepted only when all of these hold:
  - `Gud-Author` is the current assignee, `From` is that member's address, `sender_verified` is set, and `Gud-Sig` verifies with their key at the cited link (or at the tip, if they joined later).
  - It was received at or after the current assignment.
  - `Gud-Tip` is a link on the chain.
  - The assignee has no in-flight push for this subtask (`window`, `testing`, `needs_approval`, `needs_quorum`, or `qualified`). A landed link still wins over `no-change`.
  - The subtask's files are unchanged between that link and the current tip. Those files are the `Files:` paths plus any other path named in the subtask title or description that exists in the cited tree. When the subtask names none, the whole tree counts, and any later change drops the `no-change`. A later link that does not touch the named files leaves it in force. A later link that changes one of them (including creating a named file that was absent) drops it, and the subtask is open again until a new link lands or a new `no-change` cites a tip that still matches. The latest accepted `no-change` supplies the reason.
- A task is **done** when the lead sends `task-done`; the requester is then notified by email (`notify` kind, To the human's address, CC repo). The `task-done` text and that notice name each subtask that needed no change, with its reason.
- `Gud-Bug` (optional on a `task`): message id of the bug report the ticket was opened from.

## Bug reports

Mail to `bugs@<domain>` (`GUD_BUGS_ADDRESS`, otherwise `bugs@` plus the repo domain) is a bug report when it is not a reply and not itself a signed gud email or a `notify`. A line in the body that only looks like a trailer stays part of the report. The subject is the title and the body is the report. The sender does not have to be a member. The report is not a task yet.

A report stays pending until a voter other than the sender reviews it. With no model reviewer, the lead mails the sponsor once (`notify`, `Gud-Ask: review`, `Gud-Target` the report) and waits. A review is a reply to that request or to the report, or an `approve` / `object` whose `Gud-Target` is the report. The first word of a plain reply is `approve` or `accept` to accept, and `reject` or `object` to reject. The earliest review wins. The sender cannot accept their own report.

Accepting opens a `task`. Its description is the report, `Gud-Requester` is the sender when they are a human member and otherwise the lead's sponsor, and `Gud-Bug` cites the report. The lead then splits that task like any other. Rejecting opens no task. Either way the lead mails the sender once.

With an LLM lead, the model may cast that review. If it cannot decide, the report stays pending. On the local mailbox, `POST /api/bugs/{id}/review` sends the human review.

## Agents

Agents are driven by their own inbox; the repo mailbox is for deciding links.

- **Wake on mail.** Each agent long-polls its own address and keeps only mail that also went to the repo address (anything else is not gud mail). Mail addressed to it is what it acts on: a worker takes the subtasks assigned to it, the tester takes pushes, authors take votes and test results on their pushes.
- **Replay before acting.** Before pushing, voting or testing, an agent replays the repo mailbox to pick the tip and see which links exist. With nothing new in its inbox, it still replays now and then (every few seconds while it has work in flight, less often when idle) to catch windows closing and missed mail.
- **Remember what it sent.** Real mail reaches the repo seconds after sending, so an agent remembers its own pushes and votes and does not repeat them while they are in transit.
- **Rebase.** A push that loses its race is superseded. The author first tries a 3-way merge of its edit onto the new tip, and asks its model to redo the edit only on a real conflict.
- **Nothing to change.** An assignee whose edit does not change the tip sends `no-change` instead of an empty push. A coder says so in its reply (`{"files": {}, "done": "reason"}`). If the coder instead changes nothing, the worker signs `no-change` the next time it sees that same tip, and does not ask again. A failed coder call is not a no-change.
- **Reviews.** A reviewer that cannot reach a verdict (model error, unusable answer) casts no vote, rather than approving.

## Hosting and member accounts

Requirement: members never share the host's mail API key.

**Identity is the signature, not the account.** Every message that affects the chain is signed with the member's own Ed25519 key, and replay verifies it against `.gud/members`. Which mail account sent it does not matter, as long as `From` equals the member's address and the receiving mailbox marks it `sender_verified`. So a member may send from:

- their own Primitive account, at their own address (recommended for outside agents; any address under `primitive.email` can mail any other), or
- a Primitive **agent connection** the host creates for them at an address on the host's domain. The member claims the invitation and gets a credential bound to that one address; the host's org key is never handed over. (Org API keys cannot create connections; a human creates them in Primitive.)

**Reading: the host's mailbox is the reference copy.** Each member's inbox gets a CC of the mail that concerns it, but inboxes disagree on received times, humans reply only to the repo address, and late joiners lack history. So replay always runs over the **repo mailbox as served by the host**:

- `GET /api/mail?since=<float>` on the host returns the repo mailbox's raw emails (every `Email` field, including `received_at` and `sender_verified`), oldest first. See `api_contract.md`.
- A member reads it with `gud.mail_remote.RemoteMailbox(host_url, identity, send_via=<their own Mailbox>)`. `list`/`wait` read the host mirror; `send` goes through the member's own mailbox (their Primitive key or connection credential), To the repo address, CC the agent members the email concerns, exactly as today.
- Members verify every signature locally, so the host cannot forge pushes, approvals, objections or tests. The host is trusted only for received times and for `sender_verified` on unsigned human replies.

**Mirror access.** Requests to the mirror (and to every read endpoint) from outside the host's loopback interface must be signed by a current member:

- Headers `X-Gud-Handle`, `X-Gud-Time` (unix seconds), `X-Gud-Sig` = `ed25519:<b64>` over the bytes `"{METHOD} {path_with_query} {time}"`.
- The host accepts it if the handle is a member at the tip with a public key that verifies it, and `|now - time| <= 300`. A removed member loses mirror access when the removal links.
- Mutating demo endpoints (`POST /api/tasks`, `/api/members`, `/api/reply`, `/api/approve`) accept only loopback requests.

**Audit.** `gud audit` compares the host mirror with the member's own inbox: every gud email in the member's inbox that was sent to the repo address must appear in the mirror with the same body, and the mirror's received time must be within `GUD_AUDIT_SKEW` (default 120 s) of the member's copy. It reports missing, altered, and retimed messages, and exits non-zero if any.

**Joining.**

1. The newcomer runs `gud join <host_url> --handle @x --address x@their.domain [--role agent] [--sponsor @h]`. This creates a key locally (never sent anywhere) and sends a signed `join` email from their own account to the repo address.
2. Any member (in the demo, `@console` via the UI) answers with a membership push that adds the requested line, cites `Gud-Join`, and lands by quorum like any other `.gud/members` change. A `join` with a bad self-signature, or whose `From` is not `Gud-Address`, is ignored.
3. When it lands, the welcome email (To the new address) carries the host URL and a checkpoint (tip id, state hash, tree hash, seq). The new member replays the mirror from genesis and checks it reaches that checkpoint.

**Leaving.** A removal push revokes the key at the chain level. For a connected agent the host also excludes the address from Primitive's agent network.

**Config per member:** `GUD_HOST_URL` (absent means "I am the host"), `GUD_HANDLE`, `GUD_ADDRESS`, `GUD_REPO_ADDRESS`, and their own `PRIMITIVE_API_KEY` (or connection credential). The private key lives at `GUD_HOME/keys/<handle>.key`.

## Transport interfaces

Defined in `gud/types.py`:

- `Mailbox.send(...) -> Email` delivers one email to `to` and every `cc` address, and returns the stored copy.
- `Mailbox.list(recipient=None, since=None) -> list[Email]` returns delivered emails, oldest first.
- `Mailbox.wait(recipient, since, timeout) -> list[Email]` long-polls for new mail.
- `Runner.run(tree) -> RunResult` runs the test suite on a tree.

Implementations: `gud.mail_fake.FileMailbox` (local, JSONL), `gud.mail_primitive.PrimitiveMailbox` (the host, or a member sending from their own account), `gud.mail_remote.RemoteMailbox` (a member reading the host mirror); `gud.build.LocalRunner`, `gud.build.InstaBoxRunner`.
