# gud wire spec

This is the contract every module builds against. If code and this file disagree, fix the code or change this file on purpose.

## Concepts

- **Mailbox.** The repo is the set of emails delivered to the repo address (`GUD_REPO_ADDRESS`). Emails are stored by Primitive in production, or by `gud.mail_fake.FileMailbox` locally. Nothing else is a record.
- **Member.** A line in `.gud/members` in the tree at some state. Agents sign; humans are trusted by verified sender address.
- **Push.** An email with `Gud-Kind: push` carrying a unified diff against its parent push.
- **Link.** A push that satisfies the four link rules. The chain of links from genesis is the history.
- **Tip.** The last link. Its tree is the repo.

## Email shape

All gud emails are sent **to the repo address**. Agent members' addresses are in `Cc`. `In-Reply-To` and `References` follow normal threading.

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
| `subtask` | Author, Task, Parent-Task, Assignee, Sig | | yes |
| `task-done` | Author, Task, Tip, Sig | | yes |
| `notify` | Author | Task, Target | no (informational, never affects the chain) |

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
- `Gud-Sig`: `ed25519:<base64 signature>`.

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
- **CC list** for pushes: every non-human member's address except the sender's.

## `.gud/protected`

One rule per line: `<glob> <rule>`, rule is `explicit` or `quorum`. Globs use `fnmatch` against repo-relative paths. `.gud/members` and `.gud/protected` are always `quorum`, even if unlisted.

## Link rules

A push P with parent X is a **link** when all hold:

1. **Authorship.** `Gud-Author` is an agent-type member (any role except `human`) in X's members; `Gud-Sig` verifies with that member's key; `From` equals that member's address; `sender_verified` is true.
2. **Applies.** `Gud-Base` equals X's state hash, the diff applies cleanly to X's tree, and the resulting state and tree hashes equal `Gud-State` and `Gud-Tree`.
3. **Consent** (below).
4. **Tested.** There is a `test` email from a `tester` member at X targeting P with `Result: pass` and a matching `Tree`. A `fail` result means P never qualifies.

Then, among X's children that qualify, the one with the **earliest received time** is X's next link. If an earlier-received child is still pending (it might qualify later), X has no next link yet.

## Consent

Computed against X's tree and X's blame.

- **Affected owners**: owners of every line P removes or rewrites, plus, for lines inserted inside a Python function or class, the owner of that symbol's `def`/`class` line. Excludes P's author and anyone who is not a voter in X's members.
- **Protected**: any touched path matching an `explicit` or `quorum` rule. The strictest rule wins (`quorum` > `explicit` > none).
- **Veto**: a verified human `veto` reply targeting P, received before P qualifies, blocks P forever.

Rule by case:

- **Unprotected, no affected owners**: consent holds.
- **Unprotected, with affected owners (lazy consensus)**: consent holds at `received(P) + W` if no affected owner sent an `object` (or human `object` reply) targeting P with received time before `received(P) + W`. It holds immediately if every affected owner has approved (an `approve` targeting P, or an approval cited in `Gud-Consent`).
- **`explicit`**: every affected owner approved. Objections are irrelevant; missing approvals keep it pending.
- **`quorum`**: approvals from more than half of X's voters, excluding any voter whose membership line this push adds or removes.

`W` is `GUD_WINDOW_SECONDS` (default 60). Everything is judged on mailbox received times, so rebuild is deterministic once windows have closed.

## Tasks

- `task` from `@console` (or any member) opens task `T<n>`. Subject is the title; free text is the description; `Gud-Requester` is the human handle.
- `subtask` from the lead assigns `T<n>.<m>` to `Gud-Assignee`.
- A subtask is **done** when a link carries `Gud-Task: T<n>.<m>`.
- A task is **done** when the lead sends `task-done`; the requester is then notified by email (`notify` kind, To the human's address, CC repo).

## Transport interfaces

Defined in `gud/types.py`:

- `Mailbox.send(...) -> Email` delivers one email to `to` and every `cc` address, and returns the stored copy.
- `Mailbox.list(recipient=None, since=None) -> list[Email]` returns delivered emails, oldest first.
- `Mailbox.wait(recipient, since, timeout) -> list[Email]` long-polls for new mail.
- `Runner.run(tree) -> RunResult` runs the test suite on a tree.

Implementations: `gud.mail_fake.FileMailbox` (local, JSONL), `gud.mail_primitive.PrimitiveMailbox` (production); `gud.build.LocalRunner`, `gud.build.InstaBoxRunner`.
