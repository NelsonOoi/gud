# gud wire spec, version 1

This is the contract every v1 module builds against. `docs/v1/PLAN.md` is the design record; where this file is more specific, this file wins, and every place it departs from the plan is listed in "Deviations from PLAN.md" at the end. If code and this file disagree, fix the code or change this file on purpose.

The key words MUST, MUST NOT, SHOULD and MAY are used in their RFC 2119 sense.

## 0. Conventions

- **Bytes.** Every hash input, envelope and tree entry is a byte string. Text that enters a hash is UTF-8. There is no newline normalisation anywhere: CR, CRLF, a missing final LF and NUL bytes are all preserved exactly.
- **Hashes.** `sha256(x)` is lowercase hex of SHA-256 (64 characters). `ZERO` is 64 `0` characters. The display form of any hash is its first 12 characters.
- **Time.**
  - Protocol times inside replay are **integer unix milliseconds** (`ms`). Replay MUST NOT use floating point for time.
  - `Gud-Sent` is integer unix **seconds**. Replay converts it with `sent_ms = Gud-Sent × 1000`.
  - Durations in repo config files are written with a unit and converted to ms (section 10.6).
- **Handles.** `@` followed by 1–64 characters from `[a-z0-9._-]`, the first after `@` being `[a-z0-9]`. Handles are lowercase and compared byte-wise.
- **Addresses.** Email addresses are compared case-insensitively after taking the bare `local@domain` out of any display form.
- **Lists.** A list-valued header is items joined by `,` with no spaces. A reader splits on `,`, trims ASCII spaces from each item, drops empty items, then percent-decodes each item (so `%2C` is a literal comma). Writers MUST percent-encode `%` and `,` inside items.
- **Sets in hashes and orders.** Whenever this spec says "sorted", it means ascending byte-wise order of the UTF-8 encoding, unless it says otherwise.

## 1. Concepts

- **Mailbox.** The repo is the set of emails delivered to the repo address, together with the host log (below). Nothing else is a record. A local copy of anything is a cache.
- **Host.** Whoever runs the repo address's mailbox. The host keeps the **host log**: a hash-chained list of entries, each signed with the host's Ed25519 key, that gives every accepted email a sequence number and a received time. The host serves a read-only mirror of the mail and the log. It can withhold mail, but everything it withholds or retimes is either bounded by the grace rule (section 13.4) or provable from its own signatures (section 11).
- **Member.** A line in `.gud/members` in the tree at some link. A member signs with their own Ed25519 key and sends from their own mail account. Roles: `agent`, `human`, `tester`, `console`, `host` (section 10.2).
- **Push.** A signed email of kind `push` (or `revert`) that carries change operations against a named parent link. A push's id is its **state hash**.
- **Link.** A push, or a push derived from one by carry-forward, that satisfies the link rules (section 12). The links from genesis form the chain.
- **Tip.** The last link. Its tree is the repo.
- **Candidate.** A push being considered as the next link on a given parent. A push has one candidate per parent it is considered on: on its named parent it is **native**; on a later link it is **carried** (section 14).
- **Claim.** A signed, leased statement "I am doing task X, touching these files" (section 16).
- **Task.** A unit of requested work, opened by any member or verified human. Subtasks form a tree under it. Done-ness is derived by replay.
- **Checkpoint.** A quorum-signed statement that the replay state after a given log entry has a given snapshot hash. Members may start replay from it. The mail before a checkpoint forms a closed **epoch** and may be archived (section 17).

## 2. Email shape

### 2.1 Signed and envelope messages

Every gud message except an unsigned human message is a MIME message with this structure:

```
MIME-Version: 1.0
From: <sender address>
To: <repo address>
Cc: <optional>
Subject: [gud] <kind> <summary> [gud:<id12>]
Message-ID: <...>
Date: <...>
In-Reply-To / References: <optional, threading only>
Content-Type: multipart/mixed; boundary="<b>"

--<b>
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: quoted-printable   (or 8bit, 7bit, base64)

<human part>
--<b>
Content-Type: application/octet-stream; name="gud.env"
Content-Disposition: attachment; filename="gud.env"
Content-Transfer-Encoding: base64

<base64 of the envelope bytes, lines of at most 76 characters>
--<b>--
```

Rules:

- Messages are sent **to the repo address**. `Cc` is optional and never affects the chain (section 19.5).
- A message is a **v1 envelope message** when exactly one MIME part (at any depth) has `filename="gud.env"` in `Content-Disposition` or `name="gud.env"` in `Content-Type`. A message with two or more such parts is ignored.
- The envelope bytes are that part's decoded body (base64 decoding ignores whitespace). The envelope MUST start with a valid header block containing `Gud-Version: 1` (section 3). If it does not decode, the message is ignored.
- **The human part is never read by replay.** It is for people. Senders SHOULD put in it: the free text (intent, reason, comment), the headers as `Name: value` lines, `Gud-Id: <id>`, and, for pushes, a git-style unified diff preview of the change truncated after 200 lines with a final line `[... N more lines]`.
- **Subject tag.** Every signed message's `Subject` MUST end with `[gud:<first 12 hex of its gud id>]`. This tag is how an unsigned human reply finds its target when a provider has rewritten Message-IDs (section 8.2). The rest of the subject is free.
- **Message-ID, In-Reply-To and References** are for threading in mail clients and for resolving human replies only. No gud rule references a Message-ID.

### 2.2 Unsigned human messages

A message received at the repo address that has no `gud.env` part is an **unsigned message**. Its text is the decoded body of its first `text/plain` part in MIME tree order (depth first). A message with no `text/plain` part is ignored. Section 8 says when it counts.

### 2.3 v1 detection

A repo is a v1 repo when its host serves a host log (section 11). In a v1 repo, only v1 envelope messages and unsigned messages are read. A message that carries v0 `Gud-*` trailers in its body and no `gud.env` part is treated as an unsigned message, and has no effect unless its text is a human reply verb. Mailboxes with no host log are v0 and are read only by the legacy reader (section 23).

## 3. Envelope

### 3.1 Grammar

```
envelope     = *header-line [sig-line] LF payload
header-line  = name ":" SP value LF            ; name is never Gud-Sig
sig-line     = "Gud-Sig: ed25519:" b64sig LF
name         = "Gud-" segment *("-" segment)
segment      = UPALPHA *(LOWALPHA / DIGIT)
value        = 1*vchar-utf8                    ; no CR, LF, NUL, no other C0 controls, no DEL,
                                               ; no leading or trailing SP
payload      = *record
record       = rname SP length *(SP arg) LF data LF
rname        = LOWALPHA *(LOWALPHA / DIGIT / "-")
length       = "0" / (%x31-39 *DIGIT)          ; decimal byte count of data
arg          = 1*(%x21-7E)                     ; printable ASCII, no space
data         = <length bytes, any values>
b64sig       = standard base64 with padding of the 64-byte Ed25519 signature
```

Header block rules:

- Header lines (excluding the sig line) MUST be in **strictly ascending byte order of `name`**. No name appears twice.
- Names are in **canonical case**: `Gud-` then segments that are one uppercase letter followed by lowercase letters or digits (`Gud-Parent-Task`, `Gud-Base-Result`). A decoder MUST reject any other case. (Name comparison is therefore byte comparison.)
- The sig line, when present, is the **last** line of the header block.
- The header block ends at the first empty line (a lone `LF`). Everything after it is the payload.
- A decoder MUST reject: a missing empty line, unsorted or duplicate names, non-canonical case, a value with a forbidden byte or leading/trailing space, a sig line that is not last, any bytes after the last record, a record whose `data` is not followed by exactly one `LF`, and a `length` with a leading zero.
- Unknown header names are allowed, kept, signed, and ignored by replay. Unknown records are ignored except in `push` and `revert`, where they make the message malformed.

These rules make encoding a bijection: one parsed envelope has exactly one byte form.

### 3.2 Records

Arguments that are repo paths are **percent-encoded**: every byte outside `%x21-7E`, and `%` itself, is written as `%` plus two **uppercase** hex digits. No other byte may be encoded. A decoder MUST reject lowercase hex, encodings of bytes that did not need encoding, and invalid UTF-8 after decoding.

| Record | Args | Data |
| --- | --- | --- |
| `text` | none | UTF-8 free text (must be valid UTF-8) |
| `accept` | none | UTF-8, one acceptance command per line (section 16.2) |
| `patch` | `<path>` | unified hunks (section 5.4) |
| `put` | `<path> <mode> <sha256> <size>` | file bytes inline when `length == size`; a blob reference when `length == 0` and `size > 0`. `size == 0` is an empty file. Any other `length` is malformed. |
| `delete` | `<path>` | empty |
| `rename` | `<src> <dst>` | empty |
| `mode` | `<path> <mode>` | empty |
| `evidence` | `<path> <sha256> <size>` | as `put`: inline or blob reference |
| `data` | none | one blob chunk |

Each kind (section 7) says which records it may carry and in what order. `text`, when allowed, is always first and appears at most once.

### 3.3 Signing bytes and ids

- **Signing bytes** of an envelope are its bytes with the sig line removed: the sorted header lines, the empty line, the payload.
- The **gud id** of any envelope message is `sha256(signing bytes)`. For a `push` or `revert` this is also its **state hash**. Envelope messages have no `Gud-State` header: the state is computed, never claimed.
- The gud id of an **unsigned message** is `sha256(raw MIME bytes as stored by the host)`.
- The signature is Ed25519 over the signing bytes (section 9).
- A resend of the same message MUST reuse the original envelope bytes, so it has the same id. A re-signed message with a new `Gud-Sent` is a different message.

### 3.4 Common headers

| Header | Meaning |
| --- | --- |
| `Gud-Version` | Always `1`. Required on every envelope. |
| `Gud-Kind` | The kind (section 7). Required. |
| `Gud-Author` | Signer's handle. Required on every signed kind. |
| `Gud-Sent` | Unix seconds at signing. Required on every signed kind. |
| `Gud-Seen` | `<seq>:<entry hash>`: the latest host log entry the sender had verified when signing. Required on every signed kind except `join`. |
| `Gud-Sig` | `ed25519:<base64>`. Required on every signed kind. |

## 4. References and dedupe

- Every reference between gud messages is a gud id (64 hex): `Gud-Parent`, `Gud-Target`, `Gud-Revises`, `Gud-Task`, `Gud-Parent-Task`, `Gud-After`, `Gud-Tip`, `Gud-Link`, `Gud-Revert`, `Gud-Stack`, `Gud-On`, `Gud-Join`, `Gud-Bug`. Prefixes are never valid on the wire.
- A reference to a push may name the push's own id or the id of any of its carried candidates (section 14.4). Replay maps both to the push.
- **Dedupe.** Replay keys every message by gud id. If the log has two entries for one id, the lower `seq` counts and the other is ignored. Two unsigned messages with the same `Message-ID` header value count once (the lower `seq`).

## 5. Trees, change operations, hashes

### 5.1 Trees

A tree is a finite map `path → (mode, bytes)`.

- `mode` is one of `100644` (file), `100755` (executable file), `120000` (symlink; the bytes are the link target, non-empty and with no NUL).
- A **path** is a non-empty UTF-8 string of `/`-separated segments. It has no leading or trailing `/`, no empty segment, no segment `.` or `..`, no NUL byte, and no segment equal to `.git` compared ASCII-case-insensitively.
- No path in a tree is a proper `/`-prefix of another (a file and a directory cannot share a name).
- `.gud/BASE` and every path under `.gud/local/` are **reserved** for working-copy metadata and MUST NOT be in any tree.

### 5.2 Tree hash

```
tree_hash = sha256( concat over paths p in sorted order of:
                    utf8(p) || 0x00 || ascii(mode) || 0x00 || ascii(sha256(bytes)) || 0x0A )
```

The empty tree hashes to `sha256("")`. Implementations SHOULD cache each entry's line so that a link costs one pass over the path list, not over file contents.

### 5.3 Change operations

A `push` payload is an optional `text` record followed by one or more operation records, applied **in order**, each to the tree left by the previous one. If any operation fails, the push does not apply.

| Op | Precondition | Effect |
| --- | --- | --- |
| `patch <path>` | path exists with mode `100644` or `100755`; hunks apply exactly (5.4) | bytes replaced by the patched bytes; mode kept |
| `put <path> <mode> <sha> <size>` | content available (inline, or the blob is complete); `sha256(content) == sha` and `len == size` | path set to `(mode, content)`, created or replaced. Parent "directories" MUST NOT exist as files. |
| `delete <path>` | path exists | path removed |
| `rename <src> <dst>` | `src` exists; `dst` does not | entry moved unchanged |
| `mode <path> <mode>` | path exists; old and new mode both in {`100644`, `100755`} and differ | mode changed |

The resulting tree MUST satisfy 5.1. A push with zero operations is malformed (use `no-change`).

Senders SHOULD generate operations in this canonical way (receivers MUST accept any valid sequence): renames first, sorted by `src`; then, per path in sorted order, `delete`, `put`, `patch` or `mode`. Use `patch` only when both old and new bytes are valid UTF-8 with no NUL and the file exists before and after with a regular mode; use `put` for everything else (new files, binaries, symlinks, files where the patch would be larger than the file).

### 5.4 Patch data

```
patch-data = 1*hunk
hunk       = "@@ -" start ["," count] " +" start ["," count] " @@" *(%x00-09 / %x0B-FF) LF *hline
hline      = (" " / "-" / "+") *(%x00-09 / %x0B-FF) LF [nonl]
nonl       = "\" *(%x00-09 / %x0B-FF) LF          ; conventionally "\ No newline at end of file"
start      = 1*DIGIT
count      = 1*DIGIT                               ; omitted means 1
```

Semantics, byte-wise and with no fuzz:

- A file's **lines** are its bytes split after each `LF`; each line keeps its `LF`. The last line may lack an `LF`. CR is an ordinary byte.
- An `hline` stands for the line made of the bytes after its first character, plus `LF`, unless it is followed by a `nonl` line, in which case it stands for those bytes with no `LF`.
- Old lines (` ` and `-`) of a hunk MUST equal the file's lines starting at line `old start` (1-based; when `old count` is 0, the hunk inserts after line `old start`, and `0` means at the beginning). New lines (` ` and `+`) replace them. Counts MUST match the numbers of old and new lines.
- Hunks MUST be in increasing order of old start and MUST NOT overlap. The `new start` values are checked: each MUST equal the position the hunk lands at in the new file.
- Text after the second `@@` on a hunk header is ignored.

Receivers MUST NOT reject a patch because the bytes are not UTF-8: application is byte-wise.

### 5.5 State hash

The state of a native link is its push's gud id: `sha256(signing bytes)`. Because the signing bytes contain `Gud-Parent`, states chain. The state of a carried link is defined in section 14.4. Genesis names `Gud-Parent: ZERO`.

What a signature covers: the author's intent, the operations, and the parent they named. It does **not** cover the final tree of a carried link. Replay derives that tree deterministically, and the test policy (section 15) decides when tests vouch for it.

### 5.6 Blame

- A file is **text** for blame when its bytes are valid UTF-8 and contain no NUL. Blame for a text file is one `(owner handle, link seq)` per line (5.4 line definition). Blame for any other file (binary, symlink) is one `(owner, link seq)` for the whole file.
- Genesis: every line and every file belongs to the genesis author.
- A new link: per changed file, lines equal in old and new keep their blame (alignment by a longest-common-subsequence line diff over the lines as bytes; implementations MUST use the same alignment as the reference `gud.core.ops.update_blame`, which is Python `difflib.SequenceMatcher(autojunk=False)` opcodes); inserted and replaced lines belong to the link's author. A renamed file keeps its blame under the new path. A file whose content changes as a whole (non-text) belongs to the author. A mode change alone does not change blame.
- The link seq in blame also gives the line's `linked_at` through the chain, which ownership fading needs (section 13.1).

## 6. Blobs and size budget

- **Budget.** Let `M` be the smaller of the sender transport's `max_message_bytes` and the host's advertised `max_message_bytes` (section 21). The per-message envelope budget is `B = min(7 340 032, floor((M − 65 536) × 3 / 4))` bytes. An envelope larger than `B` MUST NOT be sent.
- A sender whose push would exceed `B` sends large `put` contents (and large `evidence` files) as **blob references** and sends the content as `blob` messages, chunked.
- **`blob` kind.** Headers: `Gud-Version`, `Gud-Kind: blob`, `Gud-Blob` (sha256 of the whole content), `Gud-Size` (total bytes), `Gud-Chunk` (`<i>/<n>`, `0 ≤ i < n`), `Gud-Chunk-Size` (bytes per chunk; every chunk except the last has exactly this size). One `data` record. Blobs need no signature; a signature, if present, is ignored. Content is the chunks concatenated in index order.
- A blob is **complete at time τ** when all `n` chunks with consistent `Gud-Blob`, `Gud-Size` and `Gud-Chunk-Size` are visible at τ and the concatenation hashes to `Gud-Blob`. Chunks that disagree with the first-logged chunk of the same `Gud-Blob` are ignored.
- A push whose blob references are not all complete is **`pending_blobs`** (section 12.3). Senders SHOULD send every blob before the push that references it.

## 7. Kinds

Signed kinds require the common headers of 3.4. "Records" lists the allowed records in their required order (`?` optional, `+` one or more).

| Kind | Required headers | Optional headers | Records | Signed |
| --- | --- | --- | --- | --- |
| `push` | Parent, Tree | Task, Revises, Carry, Join | `text?`, ops `+` | yes |
| `revert` | Parent, Revert, Tree | Task | `text?` | yes |
| `object` | Target | | `text` (reason, non-empty), `evidence?` | yes |
| `approve` | Target | | `text?` | yes |
| `hold` | Target | | `text?` | yes |
| `comment` | Target | Task, Escalate, Verdict | `text` (non-empty) | yes |
| `test` | Scope, Target, Tree, Result | Stack, On, Base-Result, Link | `text?` (log tail, ≤ 65 536 bytes) | yes |
| `claim` | Task, Files | Revises, Release, After | `text?` | yes |
| `task` | Title | Requester, Accept, Bug | `text?`, `accept?` | yes |
| `subtask` | Parent-Task, Title | After, Accept, Files | `text?`, `accept?` | yes |
| `no-change` | Task, Tip | | `text` (reason, one line, non-empty) | yes |
| `checkpoint` | Log, Link, Tree, Snapshot | | none | yes |
| `join` | Role, Address, Key | Sponsor | `text?` | yes, by `Gud-Key` |
| `notify` | Event, Event-Type | Target, Task | `text?` | yes |
| `blob` | Blob, Size, Chunk, Chunk-Size | | `data` | no |

(Header names in the table omit the `Gud-` prefix.) There is no `task-done` kind: done is derived (section 16.6). There is no `fix` kind: fix tasks are derived (section 15.5).

Field meanings:

- `Gud-Parent` (push, revert): state of the parent link. Genesis uses `ZERO`.
- `Gud-Tree` (push, revert): tree hash after applying the operations to the named parent's tree. A mismatch makes the push `conflict`.
- `Gud-Task`: id of the task or subtask this message is for.
- `Gud-Revises`: on `push`, the id of an earlier push by the same author that this one replaces (it joins that push's negotiation, section 13.6). On `claim`, the id of the same author's earlier claim that this one updates. A `Gud-Revises` naming another author's message is ignored (the message still counts, without the link).
- `Gud-Carry: none` (push): opt out of carry-forward. Any other value is malformed.
- `Gud-Join` (push): the join request this membership push admits. Informational.
- `Gud-Revert` (revert): the state of the link being undone. The operations are derived (section 15.6).
- `Gud-Target`: what the message is about. `object`, `approve`, `hold`: a push (or candidate) id; `approve` may also name a task id (requester acceptance, section 16.6) or a push in an escalation. `comment`: any gud id (push, objection, comment, task, claim, unsigned message) or a bug report id. `test`: see `Gud-Scope`.
- `Gud-Scope` (test): `push`, `stack`, `tip`, `accept` or `evidence`.
  - `push`: `Gud-Target` is a candidate id; `Gud-Tree` is that candidate's tree hash.
  - `stack`: `Gud-Target` is the base link state; `Gud-Stack` lists push ids in the order applied; `Gud-Tree` is the tree after applying all of them (section 15.3).
  - `tip`: `Gud-Target` is a link state; `Gud-Tree` is that link's tree hash.
  - `accept`: `Gud-Target` is a task or subtask id; `Gud-Link` is the link tested; `Gud-Tree` is its tree hash.
  - `evidence`: `Gud-Target` is an objection id; `Gud-On` is the candidate the evidence was run against; `Gud-Tree` is that candidate's tree hash with the evidence file added; `Gud-Base-Result` (`pass`/`fail`) is the run on the candidate's parent tree with the evidence file added (required when `Gud-On` is a candidate of the objected push itself).
- `Gud-Result`: `pass` or `fail`.
- `Gud-Files` (claim, subtask): list of globs (section 10.1). `Gud-Files` on a subtask is a suggested scope only.
- `Gud-Release: yes` (claim): releases the claim named in `Gud-Revises`.
- `Gud-After`: list of task, subtask or claim ids that should land first. Advisory (section 16.4).
- `Gud-Title`: one-line title.
- `Gud-Requester`: handle of a human member. Default: the author if human, else the author's sponsor, else none.
- `Gud-Accept`: list of repo paths or globs of test files that must pass (section 16.2).
- `Gud-Bug`: id of the bug report this task came from (section 16.8).
- `Gud-Escalate` (comment): `budget` or `stuck`. Raises an `escalated` event for the task in `Gud-Task` (section 16.7).
- `Gud-Verdict` (comment): `reject`, used for bug triage only.
- `Gud-Tip` (no-change): the link the author checked.
- `Gud-Log` (checkpoint): `<seq>:<entry hash>` of the log entry the snapshot is taken after. `Gud-Link`: the tip state at that point. `Gud-Snapshot`: sha256 of the snapshot bytes (section 17).
- `Gud-Role`, `Gud-Address`, `Gud-Key`, `Gud-Sponsor` (join): the requested member line; `Gud-Author` is the requested handle.
- `Gud-Event`, `Gud-Event-Type` (notify): the event id and type this notification carries (section 19). Notify never affects the chain.

## 8. Unsigned human messages

### 8.1 When they count

An unsigned message counts only when **all** hold:

1. It is in the host log (section 11).
2. `.gud/config` at the relevant state sets `authserv-id`, and the **topmost** `Authentication-Results` header (RFC 8601) of the raw message whose authserv-id equals it reports `dmarc=pass` with `header.from` equal (case-insensitively) to the domain of `From`. Other `Authentication-Results` headers are ignored.
3. `From` equals the address of a member with role `human` at the relevant state.

"Relevant state" is the target's parent for a vote on a push, and the tip at the message's received time otherwise.

### 8.2 Target resolution

The target of an unsigned message is the first that resolves, in this order:

1. `In-Reply-To`, then each `References` value from last to first, matched against the `Message-ID` of a logged message.
2. The last `[gud:<12 hex>]` tag in `Subject`, matched against logged gud ids by prefix. A prefix that matches more than one id does not resolve.

If the resolved message is a `notify`, the target is its `Gud-Target` (or its `Gud-Task` if it has no target).

### 8.3 Meaning

The first word of the text decides, case-insensitively, after stripping leading whitespace and these characters from the word: `.,!?;:"'` `` ` `` `()[]{}*_`.

| First word | Meaning |
| --- | --- |
| `approve` | approve the target push, or accept the target task (same as `accept`) |
| `accept` | requester acceptance of the target task (section 16.6) |
| `object` | objection to the target push; the rest of the text is the reason |
| `veto` | veto of the target push |
| anything else | a comment; no chain effect |

An unsigned message with no resolvable target and no `In-Reply-To` **opens a task** (section 16.1).

Unsigned messages have no `Gud-Sent`. For the window rules they count when received by `close + G` (section 13.4).

Humans MAY instead sign any kind with a human key listed in `.gud/members` (for example through `gud approve`); signed human messages follow the signed rules and need no `Authentication-Results`.

## 9. Signatures and verification

- Ed25519 (RFC 8032). Public keys are written `ed25519:<base64 of the 32 raw bytes>`; signatures `ed25519:<base64 of the 64 raw bytes>`.
- **Every signed kind is verified** against the public key of `Gud-Author` in `.gud/members` at the state below. There are no exceptions for any role. `From`, the sending account and `Authentication-Results` are **not** consulted for signed kinds.
- The author MUST be a member at that state, with a key, and with a role allowed for the kind.

| Kind | Members state used | Allowed roles |
| --- | --- | --- |
| `push` | the named parent (genesis: its own resulting tree) | agent, human, console, tester |
| `revert` | the named parent | agent, human, console, tester |
| `object`, `approve`, `hold` on a push | the parent of the candidate being evaluated | voters (agent, human) |
| `approve` on a task | tip at received time | human (the requester) |
| `test` | tip at received time | tester |
| `comment` | tip at received time | any except host |
| `claim`, `no-change` | tip at received time | agent, human |
| `task`, `subtask` | tip at received time | any except host and tester |
| `checkpoint` | the tip named in `Gud-Link` | voters |
| `notify` | tip at received time | any |
| `join` | none: verified with `Gud-Key` | n/a |

"Tip at received time τ" means the last link whose `linked_at ≤ τ`. A message that fails verification is **void**: it is ignored, and if it is a push it gets status `bad_sig`.

- **Gud-Seen check.** For every signed message with `Gud-Seen: s:h` logged at seq `n`: `s < n` and the log entry at seq `s` has hash `h`. Otherwise the message is void and replay emits `host.fork` (section 11.5).
- **Future skew.** A vote (`object`, `approve`, `hold`) whose `sent_ms > received + S` is void (S from `.gud/config`).

## 10. Repo config files

All config files live in the tree and are read **at a stated link** so every member replays with the same values. A push whose resulting tree contains an unparseable config file does not link (status `bad_config`).

Common format for `.gud/members`, `.gud/owners`, `.gud/test`, `.gud/config`: UTF-8 lines; `#` starts a comment to end of line; blank lines ignored; fields separated by runs of ASCII space or tab.

### 10.1 Globs

Used by `.gud/owners`, `.gud/test` `on-paths`, `Gud-Files` and `Gud-Accept`. A glob is matched against the **whole** repo path:

- `**` as a whole segment matches zero or more segments (`a/**/b`, `**/x`, `a/**`).
- `*` matches zero or more characters other than `/`. `?` matches one character other than `/`.
- `[...]` is a character class; `[!...]` negates; ranges by code point.
- `\` escapes the next character.
- A trailing `/` means `/**`. There is no implicit match at any depth: `*.css` matches only top-level files.

**Overlap.** Two glob lists `a` and `b` overlap at tree `X` when some glob string appears in both, or some path in `X` matches a glob in `a` and a glob in `b`, or a glob in one, read as a literal path, matches a glob in the other.

### 10.2 `.gud/members`

```
# handle role address public_key sponsor
@auth-agent agent auth-agent@example.org ed25519:BASE64 @nelson
@nelson human nelson@example.com ed25519:BASE64 -
@t1 tester t1@example.org ed25519:BASE64 @nelson
@host host repo@example.org ed25519:BASE64 -
```

- Five fields per line. `public_key` is `ed25519:...` or `-`. `sponsor` is a human handle or `-`.
- Roles: `agent`, `human`, `tester`, `console`, `host`. There is no `lead` role. A file containing `lead` is unparseable.
- Roles `agent`, `tester`, `console`, `host` MUST have a key. `human` MAY.
- **Voters**: `agent` and `human`. Testers, the console and the host do not vote.
- Exactly one `host` line. Its key is the host's key for webhooks and mirror responses (section 19); the host log carries its own key history (section 11.6).
- Duplicate handles or duplicate addresses make the file unparseable.

### 10.3 `.gud/owners`

```
# glob rule owners...
src/auth/** explicit @alice @bob
docs/** lazy @carol
migrations/** quorum
```

- `rule` is `lazy`, `explicit` or `quorum`. `lazy` and `explicit` lines need at least one owner handle; `quorum` lines ignore owners. Handles that are not members are ignored.
- For each path, the **last** matching line applies (CODEOWNERS semantics). No match means no declared owners.
- **Built-in rules**, applied in addition to any matching line (both must be satisfied):
  - `.gud/owners` and `.gud/config`: `quorum`.
  - `.gud/members`: the **members rule** (section 13.3).
- `.gud/owners` replaces v0's `.gud/protected`, which has no meaning in v1.

### 10.4 `.gud/test`

```
command pytest -q
image python:3.12-slim
timeout 10m
on-paths migrations/**,src/auth/**
on-tip links=5 time=10m
every-link off
stack-max 8
auto-revert 30m
```

| Key | Value | Default |
| --- | --- | --- |
| `command` | shell command, rest of line | none |
| `image` | container image | none (local runner) |
| `timeout` | duration | `10m` |
| `on-paths` | glob list | empty |
| `on-tip` | `links=<n> time=<duration>`, or `off` | `links=5 time=10m` |
| `every-link` | `on` / `off` | `off` |
| `stack-max` | integer ≥ 1 | `8` |
| `auto-revert` | duration or `off` | `off` |

With no `command`, no test requirement applies to any push and acceptance criteria cannot pass (section 16.6). The policy is read at the candidate's parent.

### 10.5 `.gud/config`

| Key | Meaning | Default |
| --- | --- | --- |
| `window` | consent window `W` | `60s` |
| `grace` | send-time grace `G` | `30s` |
| `skew` | future-skew limit `S` | `120s` |
| `own-links` | ownership fade `K` (links) | `50` |
| `own-time` | ownership fade `T` (time) | `24h` |
| `lease` | claim lease `L` | `30m` |
| `escalate` | escalation timeout | `1h` |
| `expire` | push expiry | `7d` |
| `carry` | `file` or `line` | `file` |
| `fix-offer` | fix-task offer window | `5m` |
| `lease-thrash` | lapses before escalation | `3` |
| `checkpoint` | links between checkpoints | `1000` |
| `authserv-id` | pinned authserv-id for unsigned human mail | none (no unsigned mail counts) |

`gud host init` and `gud doctor` measure delivery latency and propose `window` and `grace` (`grace` ≥ measured p99 delivery delay; `window` ≥ 2 × `grace`). Changing them is an ordinary push (quorum, section 10.3). Unknown keys are ignored. The values used for a candidate are those at its parent.

### 10.6 Durations

`<integer>[ms|s|m|h|d]`, unit default `s`. Converted to integer ms.

## 11. Host log

### 11.1 Entries

An entry is UTF-8 text lines `key value` in this exact order, followed by a signature line:

```
gud-log 1
seq <n>
prev <64 hex>
time <ms>
type <open|mail|tick|handover>
<type fields>
sig ed25519:<base64>
```

Type fields, in this order:

| Type | Fields |
| --- | --- |
| `open` | `repo <repo address>`, `key ed25519:<host public key>`, and for a continued log `from <seq>:<hash>` of the predecessor's handover entry |
| `mail` | `id <gud id>` |
| `tick` | none |
| `handover` | `next ed25519:<new host public key>` |

- **Entry hash** = `sha256` of the entry bytes up to and including the `LF` before `sig`.
- The signature is Ed25519 over the same bytes, with the **current host key**: the `key` of the most recent `open`, or for the entries after a `handover`, its `next` key.
- `seq` starts at 0 and increases by 1. `prev` is the previous entry's hash (`ZERO` for seq 0). Entry 0 is an `open`.
- `time` is **strictly increasing**. For a `mail` entry the host sets `time = max(provider received time in ms, previous time + 1)`, and `time` MUST NOT exceed the host's clock at append. Received time in this spec always means this log time.
- The host appends a `mail` entry for every v1 envelope message that decodes and every unsigned message that passes 8.1 (2) and (3) at the tip, and for nothing else. It SHOULD NOT log one id twice.
- The host appends a `tick` whenever no entry has been appended for 5 seconds while any proposal, claim or escalation is open, and at least every 60 seconds otherwise.

### 11.2 Serving

The mirror serves the log at `GET /api/v1/log?since=<seq>&limit=<n>` as `{"entries": ["<entry text including sig line>", ...], "head": {"seq": n, "hash": h}}` and the raw MIME of each logged message at `GET /api/v1/mail/<gud id>` (section 21).

### 11.3 What replay takes from the log

- **Only logged messages exist.** An email with no `mail` entry is invisible to replay.
- **Received time** of a message is its entry's `time`. The **order** of messages is log order.
- **Replay time.** Replay at local clock `now` uses `t = min(now, time of the last log entry)`. Because log times strictly increase, any future entry has a time greater than `t`, so a decision at `τ ≤ t` never changes.
- **Gaps.** If the raw message for entry `n` is not available, replay uses `t = min(now, time(n) − 1)`.

### 11.4 Verification

A member verifies, for each new entry: format, `seq`, `prev`, strictly increasing `time`, signature under the current key, and, for `mail`, that the fetched message's gud id equals `id`. Any failure stops replay at the last good entry and is reported (`gud audit`).

### 11.5 Gud-Seen and fork evidence

- Every signed message carries `Gud-Seen`, the latest entry its sender verified. Members MUST keep every log entry they cite, with its signature.
- Replay checks each `Gud-Seen` (section 9). A mismatch makes the message void and emits a `host.fork` event naming the message and the two hashes. On its own this is a suspicion: a member can lie about what it saw, but only to void its own message.
- **Fork proof** is two entries with the same `seq`, different hashes, each validly signed by the key current at that `seq` in its own log. `gud audit` builds it from a member's retained entry and the served log. Its JSON form is `{"a": "<entry text>", "b": "<entry text>"}`. A proven fork means the host showed members different histories.

### 11.6 Host handover

- **Planned.** The old host appends `handover` with `next` = the new key, signed by the old key. The new host continues the same log: its first entry is `open` with `from` citing the handover entry, `key` = the new key, signed with the new key. Then a members push changes the `host` line to the new key, linking like any members change.
- **Unplanned.** If the old host cannot sign, the new host starts with `open` citing the last entry it holds in `from`. Members accept that segment only after a local, explicit trust decision (`gud host trust <key>`); replay records that decision as part of its log input. The members push recording the new key follows as above.
- Webhook and mirror signatures (section 19) are verified with the `host` member key at the tip **or** the current log key.

## 12. Replay and link rules

### 12.1 Inputs and visibility

Replay is `replay(emails, log, now)`. At decision time τ, a message is **visible** when its log time ≤ τ. Every decision at τ uses only messages visible at τ.

### 12.2 Genesis

Genesis is the earliest-logged `push` with `Gud-Parent: ZERO` that: decodes; whose operations apply to the empty tree to a tree with hash `Gud-Tree` whose config files parse; whose `.gud/members` has exactly one `host` line whose key equals the log key at the push's entry; and whose signature verifies with `Gud-Author`'s key in that tree. Genesis needs no consent and no test. `seq = 0`, `linked_at` = its received time.

### 12.3 Candidate evaluation

For the current last link `Y` and time τ, the **candidates** of `Y` are: each push or revert with `Gud-Parent = Y.state` (native), and each live push carried onto `Y` (section 14). For each candidate `c` of push `P`, evaluation gives one of: **qualified at q** (q ≤ τ), **pending** (with a reason and a park time), or **dead** (terminal). Checks in order; the first that applies decides:

1. **Intrinsic** (once, for the native candidate): malformed envelope or records → `malformed`; signature or role fails → `bad_sig`; operations fail on the named parent or the tree hash differs from `Gud-Tree` → `conflict`; resulting tree has an unparseable config file → `bad_config`. These are dead for all candidates of `P`.
2. **Author removed**: the author is not a member of `Y` with an allowed role → dead `bad_sig`.
3. **Expired**: τ ≥ `received(P) + expire` → dead `expired`.
4. **Veto**: a counting `veto` on `P` (section 13.5) visible at τ → dead `vetoed`.
5. **Declined**: an escalation decided against `P` (section 13.7) → dead `declined`.
6. **Blobs**: a blob reference not complete at τ → pending `pending_blobs`.
7. **Tests**: if the test policy requires a test for `c` (section 15.1): a failing `push`-scope test for `c` → dead `tests_failed`; no vouching pass yet → pending `testing`.
8. **Consent** (section 13): not yet held → pending `window`, `objected`, `escalated`, `needs_approval` or `needs_quorum`.
9. Otherwise **qualified** at `q = max(consent time, vouching test time, blob completion time)`.

A `revert` under auto-revert skips 7 and 8 (section 15.6).

### 12.4 Race, parking, linking

- **Order.** Candidates of `Y` are ordered by their push's received time (log order). An auto-revert candidate (section 15.6) ranks first.
- **Park time.** A pending candidate has `park(c) = E(c) + W + hold(c) + G`, plus the `.gud/test` `timeout` when it is `testing`, where `E(c)` is its window open time (section 13.4) and `hold(c)` is `W` if a counting hold exists, else 0. At τ ≥ `park(c)` the candidate is **parked**: it can still qualify later, but it no longer holds later candidates back.
- **Walk at τ.** Go through the candidates in order, skipping dead and parked ones. The first remaining candidate decides: if it is qualified, it **links**; if it is pending, nothing links at τ.
- **Decision times.** The next link of `Y` is decided at the **earliest** τ in `[Y.linked_at, t]` at which the walk links a candidate, over the finite set of times where the walk can change: every visible message's received time, every candidate's window close + G, park time, expiry and escalation deadline, and every blob completion. The link's `linked_at` is that τ.
- **Effect on others.** When `c` links as `Y′`, every other live candidate of `Y` becomes a carried candidate of `Y′` if it is carryable (section 14), and is otherwise **superseded** (dead for this chain). Dead stay dead.
- A push whose `Gud-Parent` is not a link is **orphan** while that parent might still link (it is a pending candidate somewhere) and **stale** once it cannot.

### 12.5 Link rules, summarised

A candidate `c` of `Y` links when:

1. **Authorship.** Its push is validly signed by a member with an allowed role at its named parent, and the author is still such a member at `Y` (sections 9, 12.3).
2. **Applies.** Its operations apply to the named parent with tree hash `Gud-Tree`, and, if carried, it is carryable onto `Y` (section 14).
3. **Consent.** Consent v1 holds against `Y` (section 13).
4. **Test policy.** `Y`'s `.gud/test` policy is met (section 15).
5. **Race.** It is first in the walk at the decision time (section 12.4).

## 13. Consent v1

Computed for candidate `c` of push `P` (author `a`) on parent `Y`, against `Y`'s tree, blame, members and config, at evaluation time `E(c)`.

### 13.1 Line owners (fading)

- Touched lines: lines `P`'s change removes or rewrites in `Y`'s tree (for a `patch`, the `-` lines; for `put` over an existing text file, the lines a blame diff marks replaced or deleted; for `delete`, `rename` and a non-text `put`, every line or the whole file); plus, for lines inserted inside a Python `def` or `class` in a `.py` file (by `ast` of `Y`'s version), the line owning that `def`/`class` line.
- A blame entry `(owner, seq s)` is **live** at `Y` when `Y.seq − s < K` **or** `E(c) − linked_at(s) < T`. Expired ownership gives no say. Blame still records the owner.
- **Line owners** `O` = owners of live touched lines, minus `a`, minus non-voters at `Y`.

### 13.2 Declared owners and rules

For each touched path (both sides of a rename), take the last matching `.gud/owners` line at `Y` and the built-ins (10.3). Collect:

- `D_lazy`: owners from `lazy` lines, minus `a`.
- `D_explicit[p]`: per path, owners from its `explicit` line, minus `a`.
- `quorum`: whether any touched path has a `quorum` line or built-in.
- `members`: whether `.gud/members` is touched.

**Askers** `A = O ∪ D_lazy ∪ (⋃ D_explicit)`, voters only. **Eligible objectors** = `A` ∪ human voters at `Y`.

### 13.3 Requirements

Consent holds when **every** applicable part holds; its time is the latest part time.

- **Lazy part** (applies when `A` is non-empty): holds at the earliest τ with no blocking objection in force (13.5) and either every member of `A` has a counting approve, or τ ≥ `close(c) + G`. When `A` is empty, it holds at `E(c)`: there is no window, objections have no effect, and a human who must stop the push uses `veto`.
- **Explicit part**, for each path with an `explicit` line: every member of `D_explicit[p]` approves; if that set is empty, one approve from any voter other than `a`. No deadline.
- **Quorum part**: approves from `floor(n/2) + 1` of the **quorum voters**: voters at `Y` whose `.gud/members` line `P` does not add, remove or change (`n` is their count). `a` counts as approving if eligible.
- **Members rule**: one approve from a human member at `Y`. Line owners of `.gud/members` are not asked.

An approve **counts** when it targets `P` or any candidate id of `P`, is from a voter at `Y` (or a verified human, section 8), is not void, and is visible. Its time is its received time. An approve received after the same voter's objection withdraws that objection.

### 13.4 Windows, holds, grace

- **Window open** `E(c)`: `received(P)` for a native candidate and for a carried candidate that adds no asker (14.3); otherwise `max(received(P), Y.linked_at)`.
- **Window close** `close(c) = E(c) + W`, extended to `E(c) + 2W` if a counting hold exists.
- **Hold.** A `hold` from an eligible objector, targeting `P` or a candidate of `P`, counts if it meets the vote rule below against `E(c) + W`. The first counting hold extends the window once; later holds have no effect.
- **Vote rule (signed send time with grace).** A signed `object` or `hold` counts toward window close `C` when `sent_ms ≤ C` and `received ≤ C + G`, and it is not future-skewed (section 9). An unsigned human objection counts when `received ≤ C + G`.

### 13.5 Objections

An objection counts when it is from an eligible objector, targets `P` or a candidate of `P`, meets the vote rule, and is not void. A counting objection is **blocking** until one of:

- **Reason-only objection** (no `evidence` record): blocks **once**. It becomes advisory when the author answers, by either a `comment` from `a` targeting the objection id or `P` received after the objection, or a revision (a push from `a` with `Gud-Revises` in `P`'s negotiation, 13.6) received after the objection. It also becomes advisory when an escalation chooses `P`.
- **Evidence objection** (with `evidence <path> <sha> <size>`): a test file the objector claims passes on the parent and fails on the push.
  - Testers run it: with the file added at `path` to the parent tree (`Gud-Base-Result`) and to the candidate tree (`Gud-Result`), and send `test` scope `evidence`.
  - **Confirmed** when the first evidence test on a candidate of `P` reports base `pass` and result `fail`. A confirmed objection blocks `P` and every later push in its negotiation until a revision `R` clears it: `R`'s tree contains the evidence file at `path` with exactly those bytes, and the first evidence test with `Gud-On` = a candidate of `R` reports `pass`. So the test lands in the tree with the revision.
  - **Refuted** when that first test reports base `fail` or result `pass`. A refuted objection is treated as reason-only.
  - While no verdict exists, it blocks; if no verdict is visible by `close(c) + G + escalate`, it is treated as reason-only.
- **Veto**: from a human member at `Y`, targeting `P`, visible before `P` links: an unsigned verified reply whose first word is `veto`, or a signed `object` from a human whose `text` starts with the word `veto`. Terminal. Agents cannot veto.

### 13.6 Negotiations and rounds

- A push's **negotiation** is the set of pushes connected to it by `Gud-Revises` links (all by the same author). The root is the one with no valid `Gud-Revises`.
- For each objector `v` and negotiation `N`, an **episode** starts with a counting objection by `v` on any push in `N` received after `v`'s previous episode was answered. An evidence objection still failing on a revision (first evidence test on the revision reports `fail`) starts a new episode for its objector.
- **Rounds rule.** When any objector reaches **two episodes** in `N`, `N` is escalated (13.7). An objector escalates on purpose by objecting again after being answered.

### 13.7 Deadlock and escalation

Replay opens an **escalation** at the time τ0 when either holds:

- **Mutual**: two open (live, unlinked) pushes `P` and `Q` where `author(P)` has a blocking objection on `Q` and `author(Q)` has a blocking objection on `P`.
- **Rounds**: section 13.6.

An escalation has:

- `id = sha256("gud-escalation/1\n" + sorted push ids (negotiation roots) joined by "\n" + "\n" + reason + "\n")`, with `reason` `mutual` or `rounds`.
- **Deciders**: the requesters of the tasks named by the pushes' `Gud-Task` (resolved to the root task), plus the sponsors of the pushes' authors; if that is empty, every human voter at the tip.
- **Deadline**: `τ0 + escalate`.
- **Decision**: the first counting decider vote received in `[τ0, deadline]`: an approve of one of the escalated pushes (or any push in its negotiation) **chooses** it; an `object` or `veto` of one **declines** it (in the mutual case, the other is then chosen).
- **Default at the deadline**, deterministic:
  - Mutual: the push with the earlier received time is chosen.
  - Rounds: the push is chosen unless it has a confirmed, uncleared evidence objection, in which case it is declined.
- **Effect**: a chosen push has every objection in the escalation made advisory from the decision time; other escalated pushes are declined (dead `declined`). Explicit, quorum and members parts still apply to the chosen push.
- Candidates waiting on an escalation are `escalated` and park like any people-wait (12.4).

### 13.8 Binary files

The owner of a non-text file is its blame entry: whoever last wrote it. `put`, `delete`, `rename` or `mode` over a file with a live owner makes that owner a line owner.

## 14. Carry-forward

### 14.1 Carryable

A push `P` with named parent `X` (a link) is **carryable onto** a later link `Y` (X an ancestor of `Y`) at τ when:

- `P` is live: not dead for any reason in 12.3 steps 1–5, and not linked.
- `P` has no `Gud-Carry: none`.
- **File mode** (`carry file`, the default, read at `Y`): no link after `X` up to and including `Y` touched any path `P` touches (both sides of renames; `P`'s blob references count as their paths).
- **Line mode** (`carry line`): for each path `P` touches, a three-way merge with base `X`'s version, mine `P`'s result, theirs `Y`'s version (`gud.core.ops.three_way`) has no conflict. Paths not touched by links after `X` merge trivially.

A `revert` is carried by the same rule over the paths it touches.

### 14.2 Derived tree

- File mode: apply `P`'s operations to `Y`'s tree.
- Line mode: `Y`'s tree with each path `P` touches replaced by its merge result.

If the derived tree breaks 5.1 or a config file does not parse, `P` is not carryable.

### 14.3 Consent on carried candidates

Consent is recomputed against `Y` (section 13). Votes on `P` and on any of its candidates carry over. A carried candidate's window open is `P`'s previous window open when its askers are a subset of the previous candidate's askers (no new owner is asked), and `max(received(P), Y.linked_at)` otherwise. Only the newly affected owners are therefore given a fresh window. A push first logged after `Y` linked, whose named parent is an older link, is carried straight onto the current last link with `E = received(P)`.

### 14.4 Ids of carried candidates

- `carried_state(P, Y) = sha256("gud-carry/1\n" + P.id + "\n" + Y.state + "\n")`.
- When a carried candidate links, the link's state is that value. The link records `push = P.id`, `carried_from = X.state` (the named parent), and its author is `P`'s author. Blame attributes its lines to `P`'s author.
- Tests, votes and claims may reference either `P.id` or a carried state.

### 14.5 Events

On creating a carried candidate, replay emits `carried` to the author. A push that loses and is not carryable is `superseded`; replay emits `superseded`.

## 15. Test policy, stacks, red tip

### 15.1 Requirement

For candidate `c` on parent `Y`, with `Y`'s `.gud/test` having a `command`:

- `on-paths`: if `c` touches a path matching `on-paths`, `c` needs a vouching pass.
- `every-link on`: every candidate needs a vouching pass.
- Otherwise no test is needed to link. A candidate whose only touched path is `.gud/members` never needs a test.

A **vouching pass** is the first visible tester result that either:

- is a `push`-scope `test` with `Gud-Target` = `c`'s id, `Gud-Tree` = `c`'s tree hash, `Gud-Result: pass`; or
- is a `stack`-scope pass that vouches for `c` (15.3).

The first `push`-scope result for `c` decides: a `fail` (or a `pass` whose tree mismatches) makes `c` dead `tests_failed`.

### 15.2 Testers

Testers read `.gud/test` at the parent, run the command (in `image` if set) with `timeout`, and sign `test`. Several testers may run. For any (scope, target, tree) the first logged result counts.

### 15.3 Batched stacks (`every-link on`)

- A tester picks a base link `X` and up to `stack-max` pending candidates in race order, computes the tree from applying them in sequence (each carried onto the previous result by file-mode rules; a non-carryable member ends the stack), tests that tree once, and sends `test` scope `stack` with `Gud-Target = X.state`, `Gud-Stack = P1,...,Pk` (push ids) and `Gud-Tree` = the final tree hash.
- Replay checks `Gud-Tree` by recomputing the stack. A stack pass **vouches** for the candidate of `Pi` when the links from `X` are exactly `P1 … P(i−1)` (native or carried) and the candidate is `Pi` on the result. So if all `k` link in order, all `k` are vouched for by one run.
- On a stack fail the tester bisects by testing sub-stacks; only a failing stack of one push (`k = 1`) or a failing `push`-scope test kills a candidate.

### 15.4 Tip tests and red tip

- **Tip runs.** Testers test the tip on the `on-tip` cadence (every `links` new links or every `time`, whichever comes first). These runs never block linking. The cadence is a tester duty; replay does not enforce it.
- **Verdict** of a link `L` is the first visible `tip`-scope result with `Gud-Target = L.state` and `Gud-Tree = L.tree_hash`.
- At τ, let `L*` be the highest-seq link with a verdict. The chain is **red** at τ when `verdict(L*) = fail`, else **green**. `last_green` is the highest-seq link with a pass verdict.
- **Red start**: the τ at which the chain turns from green to red; `red_link` is `L*` at that moment.
- **Bisect.** While red, testers test links between `last_green` and `red_link`. `first_bad` is the lowest-seq link `B` in `(last_green, L*]` whose verdict is `fail` and whose parent's verdict is `pass` (or whose parent is `last_green`). It is undefined until such verdicts exist.
- Red ends when a link after `red_link` gets a `pass` verdict (`tip.green`).
- While red, pushes keep linking.

### 15.5 Fix tasks

- At red start replay derives a **fix task** with `id = sha256("gud-fix/1\n" + red_link.state + "\n")`. It cites `red_link` until `first_bad` is known, then `first_bad` and its author.
- Its requester is the sponsor of the cited link's author (none if no sponsor). Its scope is the paths the cited link touched.
- **Offer**: during `[red start, red start + fix-offer)` only a claim by the cited link's author can win it; after that, anyone's.
- It is **done** when red ends. It does not need subtasks, criteria or acceptance.

### 15.6 Revert and auto-revert

- A `revert` names `Gud-Revert = B.state` for a link `B` with parent `B⁻`, and `Gud-Parent = Y0` (any link at or after `B`). Its derived operations restore, for each path `B` touched, `B⁻`'s entry: if `Y0`'s entry for the path equals `B`'s, set it to `B⁻`'s (or delete it if absent there); otherwise three-way merge with base `B`'s version, mine `B⁻`'s, theirs `Y0`'s, and on conflict the revert is dead `conflict`. `Gud-Tree` must match the result.
- A revert is otherwise an ordinary push (consent and test policy apply), carried by 14.1.
- **Auto-revert.** With `auto-revert T` in `.gud/test` at the candidate's parent: a revert candidate whose `Gud-Revert` is the current `first_bad`, visible at τ ≥ red start + `T`, while the chain is still red at τ, skips consent and test policy, ranks first among candidates, and qualifies at τ. Any member allowed to push may send it; testers SHOULD send it when the timer fires.

### 15.7 Red tip and tasks

A task or subtask cannot become done while the chain is red (section 16.6).

## 16. Tasks and claims (leaderless)

Nothing needs a lead. Planning is just a member posting subtasks under a claim it holds.

### 16.1 Opening tasks

- A signed `task` from any allowed member (9) opens a task. Its id is the message's gud id.
- An unsigned message counting under 8.1, with no resolvable target and no `In-Reply-To`, opens a task: title = `Subject` with leading `Re:`/`Fwd:` and `[gud]` removed, description = its text, requester = the sender. Its id is the message's gud id. It has no acceptance criteria.
- **Display aliases.** Replay numbers tasks `T1, T2, …` in order of their opening time (received time, or red start for fix tasks), and subtasks `T<n>.<m>` in order of their received time under their parent. Aliases are for people and tools; the wire uses ids.

### 16.2 Acceptance criteria

A task or subtask has criteria if it has `Gud-Accept` (test file paths or globs, run as arguments to the `.gud/test` command) or an `accept` record (one shell command per line, each must exit 0 on the tested tree in the test environment). Spec checks are expressed as such commands.

### 16.3 Claims

- A `claim` names `Gud-Task` (a task or subtask) and `Gud-Files` (its scope; `**` for everything).
- **Settlement.** A claim **wins** when, at its received time, the item is open (not done) and has no active claim. Otherwise it is **void** (`claim.lost`). Among simultaneous claims, log order decides.
- **Lease.** An active claim's `last_activity` is the latest of: the claim, any claim update from the holder (`Gud-Revises` = the active claim), any push from the holder whose `Gud-Task` is the item or a descendant, any `comment` from the holder with `Gud-Task` set to the item or a descendant. The claim **lapses** at `last_activity + L` (`claim.lapsed`). It also ends when the item is done or the holder releases it (`Gud-Release: yes`).
- **Scope update.** A claim from the holder with `Gud-Revises` = the active claim replaces its `Gud-Files` and renews the lease.
- **Removal.** A claim ends when its holder stops being an allowed member at the tip.
- **Fix tasks** follow the offer rule in 15.5.

### 16.4 Overlap and scope

- When a claim wins or updates, replay compares its scope with every other active claim at the tip (10.1 overlap). Each overlapping pair emits `claim.overlap` to both holders. Holders settle it in `comment`s: split the files (narrow scope with a claim update), sequence the work (`Gud-After`), or release.
- Scope is **advisory**. A push outside its claim's scope links normally. If a push touches paths inside another active claim's scope, replay emits `scope.exceeded` to the author and that claim's holder.
- `Gud-After` is advisory: it orders events and agent behaviour, not linking.

### 16.5 Subtasks (self-splitting)

A `subtask` is valid when, at its received time, its author holds the active claim on `Gud-Parent-Task`. A valid subtask stays valid after that claim ends. Subtasks can be split again by the holder of a claim on them.

### 16.6 Derived done

For a task or subtask `X`, at τ:

- **Work done** at τ: if `X` has valid subtasks, all of them are done; otherwise a link with `Gud-Task = X` exists, or an accepted `no-change` for `X` holds.
- **Done** at the earliest τ where work is done **and** the chain is not red **and**:
  - if `X` has criteria: a tester `accept`-scope pass with `Gud-Target = X` on a link `L` with seq ≥ the link at which work became done, received ≤ τ, while not red;
  - else if `X` is a root task with a requester who is a human member: a requester acceptance (unsigned `accept`/`approve` reply targeting `X`, or a signed `approve` with `Gud-Target = X`) received after work became done;
  - else nothing more.
- A done root task stays done. A subtask's `no-change` can be dropped (below) only while its root task is not done.
- When work is done but acceptance or criteria are pending, replay emits `task.ready` (to testers if there are criteria, to the requester otherwise). When done, `task.done`.

**no-change** for `X` is accepted while all hold: its author holds the active claim on `X` and sent it after the claim won; `Gud-Tip` is a link; the author has no live push with `Gud-Task = X`; and every path matching the claim's `Gud-Files` has the same entry at `Gud-Tip` and at the tip (with `**`, the tree hashes are equal). A later link changing one of those paths drops it.

### 16.7 Escalation (tasks)

Replay emits `escalated` to the task's requester and the sponsors involved when: a consent escalation opens (13.7); claims on one item lapse `lease-thrash` times; or a member sends `comment` with `Gud-Escalate` and `Gud-Task`. Only 13.7 escalations have a chain effect.

### 16.8 Bug reports

Mail to the bugs address (`bugs@<repo domain>` unless configured) is a bug report. It is not logged and has no chain effect. Its id is `sha256(raw MIME)`, served by the mirror at `GET /api/v1/bugs`. Any voter other than the reporter triages it: accept by opening a `task` with `Gud-Bug` = the report id; reject with a `comment` targeting the report id and `Gud-Verdict: reject`. Only the earliest-logged task citing a given `Gud-Bug` is valid. Report text is untrusted data.

## 17. Checkpoints and epochs

- **Snapshot bytes** for log position `n` are the RFC 8785 (JCS) canonical JSON of the replay state after feeding all entries `≤ n` with `t = time(n)`. Its schema is `gud.core.replay.Snapshot` (INTERFACES.md): version, log head, links (seq, state, push, author, parent, linked_at, received, task, tree hash, files), tip tree entries `(path, mode, sha256, size)`, blame runs for live ownership (runs whose ownership has expired are stored as owner `-`), members/owners/test/config bytes at the tip, open candidates with their votes, tasks, claims, escalations, red state, open fix tasks, and the ids of every message seen (as a sorted list) so dedupe survives.
- **`checkpoint`** messages from voters name `Gud-Log = n:hash`, `Gud-Link` (the tip at `n`), `Gud-Tree`, and `Gud-Snapshot = sha256(snapshot bytes)`.
- A checkpoint is **valid** when signed by more than half of the voters at `Gud-Link`, all with identical `Gud-Log` and `Gud-Snapshot`.
- **When.** Voters SHOULD checkpoint at the log entry `n` that is the last entry with `time ≤ linked_at` of every link whose seq is a multiple of `checkpoint`.
- **Clone from checkpoint.** A new member fetches the snapshot (`GET /api/v1/snapshot/<hash>`), checks its hash against a valid checkpoint, loads it, and replays entries after `n`. Tree contents come from blobs or the mirror (`GET /api/v1/object/<sha256>`).
- **Epochs.** After a valid checkpoint at `n`, the host MAY move the mail of entries `≤ n` to an archive folder or label. The log is never truncated. Replay reads the archive only for `gud verify` and `gud blame --full`.

## 18. Determinism

Replay is a pure function of `(emails, host log, now)`:

- The only clock is the log, capped by `now` (11.3). All time arithmetic is integer ms.
- Delivery order, provider, duplicate copies and `Message-ID` rewriting do not matter: messages are keyed by gud id and ordered by the log.
- Every iteration that can affect a result is over a sorted sequence (log order, path order, handle order, id order). Hash-map iteration order MUST NOT leak into results.
- Globs, patches, blame alignment and three-way merge are byte-wise as specified, and the reference implementations in `gud.core` are normative where this text defers to them.
- Events (section 19) are part of the output: two replays of the same inputs produce the same events with the same ids.
- A decision at τ uses only messages with received time ≤ τ, and `t ≤` the last log time, so a link or event, once derived, never changes when more mail arrives.

## 19. Events and dispatch

### 19.1 Event shape

```json
{"id": "<64 hex>", "type": "linked", "at": 1760000000123, "repo": "repo@example.org",
 "concerns": ["@a", "@b"], "data": { ... }}
```

- `at` is the decision time τ in ms at which replay derived the fact.
- `id = sha256(JCS({"type": type, "key": key}))` where `key` is the per-type list below. Every dispatcher derives the same id.
- `concerns` is sorted. It never includes the member who caused the event (the sender of the message), except where stated.

### 19.2 Event types

"Agents" means every member with role `agent`; "testers" every `tester`; "holders" claim holders.

| Type | Key | Data | Concerns |
| --- | --- | --- | --- |
| `push.received` | push id | push, parent, author, task, files, forecast {line_owners, declared, rules, quorum_needed}, window_closes_at, needs_test | testers if a test is needed; holders of claims on the push's task and of claims whose scope the push touches |
| `review.requested` | candidate id, owner | push, candidate, owner, why (`line`/`lazy`/`explicit`/`quorum`/`members`), closes_at | that owner |
| `objected` | objection id | push, objection, by, reason, evidence (path, sha) | author; holders of the push's task claim; testers if evidence |
| `commented` | comment id | target, comment, by, text | author of the target; every author of a message in the target's thread |
| `approved` | approve id | push, by | author |
| `held` | hold id | push, by, closes_at | author |
| `test.passed` / `test.failed` | test id | scope, target, tree, tester, log tail | author(s) of the pushes covered; for `accept`, the task's holder and requester |
| `window.closing` | candidate id, close | candidate, closes_at | author and askers who have not voted; emitted at `close − min(10 s, W/4)` |
| `superseded` | push id, link state | push, by_link | author |
| `push.rejected` | push id, status | push, status, reason (`bad_sig`, `malformed`, `conflict`, `bad_config`, `tests_failed`, `vetoed`, `declined`, `expired`, `stale`) | author |
| `carried` | carried state | push, candidate, onto, files | author |
| `linked` | link state | link, push, seq, author, task, files, carried_from | author; holders of claims whose scope overlaps the files; authors of live pushes that touch the same files |
| `task.opened` | task id | task, alias, title, requester, criteria | agents |
| `subtask.posted` | subtask id | subtask, alias, parent, title, after, files | agents |
| `claim.won` | claim id | claim, task, holder, files | holder; requester |
| `claim.lost` | claim id | claim, task, active_claim | the claimant |
| `claim.lapsed` | claim id, lapse time | claim, task, holder | agents (including the former holder) |
| `claim.overlap` | sorted pair of claim ids | claims, holders, globs | both holders |
| `scope.exceeded` | push id, claim id | push, claim, paths | author; that claim's holder |
| `task.ready` | task id, work-done link | task, needs (`criteria` / `accept`) | testers (criteria) or requester (accept) |
| `task.done` | task id | task, link, no_change list (subtask, reason) | requester; holders of claims on the task and its subtasks |
| `escalated` | escalation id or (task id, reason, time) | escalation, reason, pushes, positions (objections and answers), deciders, deadline, default | deciders; authors; objectors |
| `escalation.resolved` | escalation id | escalation, chosen, declined, by (`decider` handle or `default`) | deciders; authors; objectors |
| `tip.red` | red link state | red_link, last_green | agents; testers |
| `tip.bisected` | red link state, first_bad state | red_link, first_bad, author, fix task | agents; testers; first_bad's author |
| `tip.green` | green link state | link, red_link | agents; testers |
| `fix.opened` | fix task id | task, alias, cited link, author, offered_to, offer_until | agents |
| `host.fork` | message id, or sha256 of fork proof | message, cited, log_hash, proof (when proven) | every member; sponsors |

An event's data never changes after it is emitted; a fact learned later is a new event (`tip.bisected` carries `first_bad` once bisect finds it, and from then on the fix task cites it). Dispatchers never re-send an id with different data.

### 19.3 Dispatch channels

The host runs the dispatcher. Each member has one delivery preference held by the host (not chain state), registered with a signed mirror request (section 21):

1. **Agent webhook.** `POST <url>` with the event JSON as the body and headers:
   - `X-Gud-Event-Id: <event id>`
   - `X-Gud-Cursor: <journal seq>`
   - `X-Gud-Time: <unix seconds>`
   - `X-Gud-Sig: ed25519:<base64>` over the bytes `"gud-hook/1\n" + X-Gud-Time + "\n" + body`, signed with the host key.
   The receiver verifies with the host key (11.6), rejects `|now − X-Gud-Time| > 300`, and dedupes by event id. Any 2xx is delivery. Otherwise the host retries with backoff 1, 2, 4, … seconds capped at 10 minutes, for 24 hours, then falls back to channel 3.
2. **SSE stream.** `GET /api/v1/events?member=<handle>` with mirror auth headers (section 21). Each SSE message has `id: <journal seq>`, `event: <type>`, `data: <event JSON>`. A client resumes with `Last-Event-ID` or `?after=<journal seq>`.
3. **Email fallback.** The host sends a `notify` envelope message To the member's address (never CC'd to the repo), batching events for that member over at most 60 seconds, `Gud-Event` = the first event's id, the rest listed in `text`.

### 19.4 Delivery guarantees

- At least once. The host keeps a per-member **journal** (monotonic `journal seq`) of every event concerning the member. Agents dedupe by event id and resume from their last journal seq.
- Events for one push thread are delivered in journal order. A receiver SHOULD debounce bursts for about 1 second before acting.
- **Human notifications** (`task.ready` accept requests, `task.done`, `escalated`) go to human members as `notify` messages To the human and Cc the repo address, so they are logged and dedupe across dispatchers: before sending, a dispatcher checks the log for a `notify` with the same `Gud-Event`. Replies to these messages resolve their target through the notify (8.2).

### 19.5 CC

`Cc` on any message is optional and never read by replay. Members who only have an inbox are served by 19.3 (3). Senders MAY Cc members an email concerns.

## 20. Transport contract

### 20.1 What every provider adapter provides

The protocol relies only on what IMAP+SMTP, JMAP, API providers and the Gmail API all provide. An adapter MUST provide:

- `send`: submit one RFC 5322 message (raw bytes, or a structured message the adapter renders with `gud.transport.mime`) to `To` and `Cc`. Envelope attachment bytes MUST arrive unaltered.
- `list(since cursor)`: messages delivered to an address, oldest first, each as **raw RFC 5322 bytes as received** (including the receiving server's `Authentication-Results`), the receiving server's **delivery time** (IMAP INTERNALDATE, JMAP `receivedAt`, provider `received_at`, Gmail `internalDate`), a provider id, and an opaque resumable cursor.
- `wait(cursor, timeout)`: block until new mail or timeout.
- `subscribe(callback)`: the push intake path (webhook, IDLE, EventSource or Pub/Sub), normalised to one "mail arrived" call. Polling is used only for catch-up after reconnect.
- `capabilities()`: `max_message_bytes`, push kind (`webhook`, `idle`, `eventsource`, `pubsub`, `none`), whether `Message-ID` is preserved, whether raw `Authentication-Results` is available, send quota per day, received-time resolution.
- If a provider exposes authentication results only as structured data, the adapter prepends one synthesized header `Authentication-Results: <adapter authserv-id>; dmarc=<result> header.from=<domain>` to the raw bytes before anything hashes them. The adapter's authserv-id is fixed and documented, and is what the repo pins.

### 20.2 Conformance (`tests/transport_conformance.py --transport <x>`)

An adapter conforms when, against a live or containerised account:

1. **Byte-exactness**: an envelope of random bytes (including NUL, CR, lone LF, 998+ byte lines, non-UTF-8) round-trips with an identical gud id.
2. **Dedupe**: the same message delivered twice yields one gud id; the intake logs it once.
3. **Ordering**: delivery times are returned and non-decreasing in list order for mail sent in sequence ≥ 1 s apart.
4. **Wake latency**: `subscribe` fires within 5 s (API providers) or 30 s (IMAP IDLE, Gmail) of delivery; `wait` returns within the same bound.
5. **Large blobs**: a 25 MB file round-trips through the blob path within the adapter's `max_message_bytes`.
6. **Spoofed human**: an unsigned reply with a forged `From` of a human member fails 8.1.
7. **Resume**: after a forced disconnect, `list(cursor)` returns every message missed, once.
8. **Quota reporting**: `capabilities()` values match the provider's documented limits.

`gud host init --transport X` checks DNS (SPF, DKIM, DMARC), creates or validates the repo address, warns about spam filtering, sends a round trip, records the authserv-id, measures delivery latency and proposes `window` and `grace`. `gud doctor` re-runs the probe.

## 21. Hosting, mirror and auth

- **Mirror endpoints** (all JSON unless noted):
  - `GET /api/v1/info` (open): `{repo, bugs_address, host_key, log_head: {seq, hash, time}, max_message_bytes, transport}`.
  - `GET /api/v1/log?since=&limit=`: log entries (11.2).
  - `GET /api/v1/mail/<gud id>`: raw MIME (`message/rfc822`). `GET /api/v1/mail?since=<seq>&limit=`: `{"mail": [{"seq", "id", "raw": base64}], "next": seq|null}`.
  - `GET /api/v1/object/<sha256>`: blob content. `GET /api/v1/snapshot/<sha256>`: snapshot bytes.
  - `GET /api/v1/events`: SSE (19.3).
  - `POST /api/v1/delivery`: body `{"webhook": url}` or `{"sse": true}` or `{"email": true}` sets the caller's delivery preference.
  - `GET /api/v1/bugs`: bug reports.
- **Mirror auth v1.** Every request except `/api/v1/info` from outside loopback carries `X-Gud-Handle`, `X-Gud-Time` (unix seconds, the `hostauth.format_time` form), and `X-Gud-Sig = ed25519:<b64>` over `"{METHOD} {path_with_query} {time} {sha256(body)}"` (the hash of the empty string for no body). The host accepts it when the handle is a member at the tip whose key verifies it and `|now − time| ≤ 300`. A removed member loses access when the removal links.
- The host is trusted only for availability, for received times within the grace rule, and for `Authentication-Results` on unsigned mail. Everything else is verified by members.
- **Join.** `gud join <host_url> --handle @x --address x@domain [--role agent] [--sponsor @h]` creates a key locally and sends a signed `join`. Any member answers with a membership push adding the line (`Gud-Join` cites the request), which needs the members rule. A `join` whose self-signature fails is ignored. When it links, the new member clones (from a checkpoint if one exists).
- **Leaving** is a members push removing the line. **Key rotation** is a members push changing the key; recovery of a lost key is the same push approved by the sponsor (members rule).

## 22. Working copy

- A working copy is an ordinary directory. Its metadata lives in `.gud/BASE` (one line: the base link's state, 64 hex, LF) and `.gud/local/` (index cache, handle, transport settings). Neither is ever in a tree (5.1). One working copy per agent.
- **Materialise** writes the base tree byte-exactly with modes; symlinks are created as symlinks; no file is written through a symlink or outside the directory.
- **`.gudignore`** (a tree file, gitignore syntax: `#` comments, `!` negation, trailing `/` for directories, `*`, `**`, `?`, `[...]`, patterns with a `/` anchored at the root). Ignored paths are never pushed. `.git/`, `.gud/BASE` and `.gud/local/` are always ignored.
- **Status/diff**: compare the working copy (minus ignored paths) with the base tree; `diff` renders a git-style unified diff.
- **Push**: operations = `diff_trees(base tree, working tree)` (5.3 canonical), `Gud-Parent` = `.gud/BASE`. Before sending, the **secret scan** runs over every added or changed file's bytes and refuses the push if any matches: a PEM private key header (`-----BEGIN` … `PRIVATE KEY-----`), an `ed25519:` gud private key file, `AKIA[0-9A-Z]{16}`, `ghp_[A-Za-z0-9]{36}`, `github_pat_`, `xox[baprs]-`, `sk-[A-Za-z0-9_-]{20,}`, `sk-ant-`, or `-----BEGIN OPENSSH PRIVATE KEY-----`. `--allow-secret <path>` overrides one path. Every member receives every diff, so a pushed secret is leaked for good.
- **Sync**: three-way merge (`three_way`) with base = base tree, mine = working tree, theirs = tip tree. On any conflict nothing is written and the conflicts are reported. On success the working copy becomes the merge and `.gud/BASE` the tip. Sync never auto-resolves.
- **Verify**: rebuild replay state from genesis (or from a valid checkpoint) and compare with the cache; report any difference.
- The replay cache (`GUD_HOME/cache/<repo>/`) and object store (`GUD_HOME/objects/`) are disposable and rebuildable.

## 23. v0 compatibility

- Messages without a `gud.env` part carrying `Gud-Version: 1` are not v1 messages. A mailbox with no host log is a v0 mailbox and is read only by the legacy reader (`gud.core.v0`), which wraps the v0 `gud.replay.replay` unchanged and presents its result through the v1 chain view (read-only). v0 rules (including the lead exception) apply only there.
- v1 replay never reads v0 trailers. There is no mixed-version repo; `gud import` (git) or a fresh genesis starts a v1 repo, and a v0 repo can be carried over by building a v1 genesis from its tip tree.
- v0 headers dropped in v1: `Gud-Base`, `Gud-State` (computed), `Gud-Consent`, `Gud-Assignee`, `Gud-Reason` (now a `text` record), `Gud-Source`, `Gud-Ask`. The `task-done` kind and the `lead` role are gone.

## 24. Deviations from PLAN.md

Each item resolves something the plan left open, inconsistent or unimplementable as written.

1. **No `Gud-State` header; state = id.** The plan says a push's id "is its `Gud-State`". v1 computes it as `sha256(signing bytes)` instead of carrying it as a claimed header, so the push id and state hash are the same value by construction and other kinds use the same formula. `Gud-Base` and `Gud-Consent` are dropped (Parent is the state; approvals are simply messages targeting the push).
2. **Host log has `tick` entries, strictly increasing times, and replay time is capped at the log head.** Without this a member whose clock is ahead of the host's log could close a window before a vote the host received in time was logged, so "later mail never changes a link" would fail. The host clamps each received time to `previous + 1 ms`; ties cannot occur, and race order is log order.
3. **Lazy consent holds at `close + G`, not `close`.** With the grace rule, a vote sent before `close` can arrive up to `G` later; deciding at `close` would let it rewrite a link. So every push with askers links at `close + G` at the earliest (unless all askers approve first). The plan's "contested push links up to G later" is read as "any push with askers".
4. **Holds extend a window once per push.** The plan says both "extends a push's consent window once" and "at most one per owner per push". v1 uses the first counting hold only (window becomes `2W`), which also bounds the worst case.
5. **Gud-Seen alone is not fork proof.** A member can write any `Gud-Seen`. A mismatch voids that message and raises `host.fork` as a suspicion; proof needs two host-signed entries for the same seq, which members keep (11.5).
6. **Task ids are content-addressed.** Leaderless members cannot coordinate sequential `T<n>` ids without collisions, so the wire uses gud ids and replay assigns `T<n>` / `T<n>.<m>` aliases in log order.
7. **Evidence test enters the tree through the revision.** "If the push is accepted, the test is added to the tree" would require replay to change a signed tree. v1 instead requires a clearing revision to contain the evidence file byte-exactly, so the test lands with the revision.
8. **Default for single-push (rounds) escalations.** The plan's timeout default ("earlier-received push wins") only fits the two-push case. For a push objected in two rounds the default is: it links unless it has a confirmed uncleared evidence objection, in which case it is declined.
9. **Push expiry (`expire`, default 7d).** Carry-forward plus parking would otherwise keep an unapprovable push alive forever, and replay cost would grow with it.
10. **Carry-forward is general.** The plan describes carrying P onto the sibling S it lost to. v1 carries any live push from its named parent onto any later link while the file rule holds, including pushes that arrive after their parent was superseded, so agents never need to rebase disjoint work.
11. **Unsigned human mail opens tasks directly.** v0 relied on the lead to re-sign it. With no lead, replay derives the task from the verified unsigned message.
12. **Bug reports are not chain objects.** Triage by "any voter other than the reporter" is expressed as opening a `task` with `Gud-Bug` (accept) or a `comment` with `Gud-Verdict: reject`; the bugs mailbox is not logged.
13. **`.gud/config` is built-in quorum**, like `.gud/owners`. It holds `W`, `G`, `K`, `T` and the pinned authserv-id; letting one agent change them under lazy consent would undo consent v1.
14. **Email fallback is host-sent `notify`, not sender CC.** A sender cannot know which members lack a webhook or SSE; the dispatcher can. CC remains optional and unread.
15. **`Gud-Accept` is split** into a header of test paths and an `accept` record of commands, because commands do not fit a one-line comma-separated header. "Spec checks" must be expressed as commands; the tester is not a model.
16. **Mirror auth v1 signs a body hash**, because v1 has signed POST endpoints (delivery registration); v0's signed bytes did not cover a body.
17. **Votes on carried candidates are checked against members at the candidate's parent**, not the push's named parent, so a member removed in between loses their say. The push's own signature is still checked at its named parent.
18. **`on-tip` cadence is a tester duty, not a replay rule**; replay only consumes tip verdicts. Enforcing a cadence in replay would make linking depend on tester liveness, which the plan says tip runs must not do.
19. **Checkpoints sign a canonical snapshot hash.** `(seq, state, tree)` alone is not enough to start replay: consent needs live blame, and tasks, claims and open proposals span checkpoints. The snapshot (RFC 8785 JSON of the replay state) is what quorum attests.
20. **Tests don't need a parking rule of their own beyond `timeout`.** A candidate waiting for a test parks at `E + W + G + timeout`, so a dead tester cannot freeze the repo (v0 let `testing` block siblings indefinitely).

## Implementation notes (core)

Choices made by the reference `gud/core` modules where this spec left room. Each is the simplest reading consistent with the text above; golden vectors are in `tests/v1/vectors/core.json`.

1. **Envelope codec (§3).** `decode` requires `Gud-Version: 1` (§2.1) and `encode` refuses envelopes without it, so the bijection holds over exactly the decodable set. Header values must be valid UTF-8; TAB is a C0 control and is rejected; C1 code points are allowed. The `Gud-Sig` value must be canonical padded base64 of exactly 64 bytes (non-canonical padding bits are rejected, or two byte forms would decode alike). Path arguments are, by record name: argument 1 of `patch`, `put`, `delete`, `mode`, `evidence`, and both arguments of `rename`; all other arguments are kept as written. A record line with two consecutive spaces is malformed.
2. **Kind rules (§7).** `validate_kind` checks required headers, the formats of the headers a kind uses (64-hex ids, lists of ids, enums, handles, `Gud-Seen`/`Gud-Log`, keys, blob numbers) and record order. In kinds other than `push`/`revert`, records the kind does not list are dropped before the order check. A missing `Gud-Sig` is not a kind error: it fails verification (void, or `bad_sig` for a push). A `no-change` reason is "one line" when it contains no LF and no CR. A `blob` is malformed unless `n = ceil(Gud-Size / Gud-Chunk-Size)` and the chunk has exactly its expected length.
3. **Patches (§5.4).** Hunks must be in strictly increasing order of old start. A lone LF is not an `hline` (empty context lines are written `" \n"`). Patch data must end with LF. A result in which a line without LF is followed by more lines is rejected. For an empty new range, `new start` is the number of new-file lines before the hunk (the usual diff convention, mirroring old start).
4. **Canonical operations (§5.3).** `diff_trees` emits renames (sorted by `src`), then **all** deletes (sorted), then `put`/`patch`/`mode` per path in sorted order. Interleaving deletes per path would produce an invalid sequence when a file becomes a directory or the reverse. Renames are detected only for an exact (mode, content) match whose destination has no file ancestor and is not a directory in the old tree. A text file whose content and mode both change gets `patch` then `mode`. A patch or `put` content that does not fit the remaining inline budget becomes a blob-reference `put`.
5. **Reserved paths (§5.1).** A file at `.gud/local` itself is rejected too, not only paths under `.gud/local/`, so a working copy can always create the directory.
6. **Three-way merge.** `three_way` is a byte-wise port of v0's merge with these differences: an edit made identically on both sides applies once instead of conflicting; delete-versus-modify conflicts in both directions (v0 crashed when the tip deleted); modes merge three-way; binaries and symlinks changed on both sides conflict; a merge whose result would break §5.1 reports a conflict for that path. Conflicting paths keep `theirs`. `invert_ops` (§15.6) works on the paths whose entries differ between `B⁻` and `B`.
7. **Touched lines (§13.1).** For a path changed by exactly one `patch`, the removed lines are its `-` lines. For any other edit (a `put`, or several ops on one path) they come from a `difflib` diff of the before and after bytes. `inserted_at` lists the old positions of pure insertions (a run of `+` lines with no `-` line). A `mode` change counts as a whole-file touch (§13.8).
8. **Blame (§5.6).** An empty text file has no runs. Symlinks are non-text. A file deleted and re-created in one push gets fresh blame; a `put` over an existing text file is aligned against the old content. Genesis is link seq 0.
9. **Globs (§10.1).** Since `**` matches zero segments, `a/**` (and `a/`) also match the path `a` itself. A leading `/`, an empty segment, an unterminated `[` or a trailing `\` is a syntax error. Character classes never match `/`. In `globs_overlap`, an invalid glob matches nothing, though the same string listed on both sides still overlaps.
10. **Config files (§10).** CR is an ordinary byte, so a CRLF file normally fails to parse. A repeated key in `.gud/test` or `.gud/config` makes the file unparseable. Unknown keys are ignored in `.gud/test` as in `.gud/config`. `on-tip` takes `links=` and/or `time=` in any order; a missing one is off. The members `address` field must be a bare address. `sponsor` is checked as a handle only, not for membership. A config file that is not a regular file (for example a symlink), or a missing `.gud/members`, is `bad_config`. Minimums: `own-links` ≥ 0; `lease-thrash`, `checkpoint`, `stack-max` and `on-tip links` ≥ 1.
11. **Host log (§11).** `from` is forbidden at seq 0. An `open` at seq > 0 must name the same repo, cite the immediately preceding entry (`from = <seq−1>:<prev>`), and use either the key named by a `handover` immediately before it or a locally trusted key (§11.6). After a `handover`, the next entry must be that `open`. Entries must be canonical (re-encoding gives the same bytes), and `sig` must be canonical base64 of 64 bytes. The reference host refuses to log one id twice. A `tick` takes `time = now`, which must exceed the previous time. `replay_time` on an empty log is `now`.
12. **Authentication-Results (§8.1).** The authserv-id is compared case-insensitively, as is `header.from`. The topmost header with the pinned authserv-id decides even when it carries no `dmarc` result (that is a fail). Any `dmarc=pass` in it with a matching `header.from` passes. The parser is lenient about result syntax, so only a value with no authserv-id at all is unparseable; a malformed top header cannot make a lower, forged one count.
13. **Event ids (§19.1).** `EVENT_KEYS` names data fields, so each key part must be in `data`. This adds fields to §19.2: `approved.approve`, `held.hold`, `test.passed/test.failed.test`, `claim.lapsed.lapsed_at`, `task.ready.link`. `window.closing` keys on `(candidate, closes_at)`. `claim.overlap` keys on the sorted `claims`. An `escalated` event with no escalation id keys on `(task, reason, at)`. A `host.fork` event with no message keys on `sha256(JCS(proof))`. JCS accepts integers only, with |n| < 2⁵³.

## Implementation notes (gitbridge)

Choices made by `gud/gitbridge.py` (PLAN Phase 5) where the text above and the plan left room.

1. **Import source.** `gud import` reads one commit: `<ref>^{commit}` (tags are peeled). History is not imported. The genesis intent (`text` record) is `gud import: git <ref> at commit <full commit id>`. Contents are read with `git cat-file --batch`, so no filters (CRLF, `text`, `filter` attributes) apply: bytes are the blob bytes.
2. **What import skips.** Submodules (mode `160000`), paths gud cannot hold under §5.1 (not UTF-8, a `.git` segment in any case, `.gud/BASE`, `.gud/local` and below) and symlinks with an empty or NUL target are skipped, each with a warning in `ImportPlan.warnings`. The legacy git mode `100664` is imported as `100644` with a warning. Nothing else is changed.
3. **Config files at import.** `.gud/members` always comes from the roster; a copy in the git tree is replaced (with a warning). `.gud/config`, `.gud/test` and `.gud/owners` are kept from the git tree when present, else written as defaults: `.gud/config` spells out every §10.5 default except `authserv-id`; `.gud/test` and `.gud/owners` are comments only (so the §10.4 defaults and no declared owners apply). The whole tree must pass §10 (`rules_of`) or the import fails.
4. **Roster format.** One member per line, `handle role address [pubkey] [sponsor]`, `#` comments. With four fields the fourth is the sponsor when it starts with `@`, otherwise the key. A missing key is `-`. The `host` line takes the host key passed to `import_git` when it has none and must equal it otherwise. The result must parse under §10.2, so agents, testers and the console need keys and there is exactly one host.
5. **Genesis push.** `text` (the intent) then `diff_trees(empty, tree)`: one `put` per file in path order. `inline_budget` bounds inline file content only; files that do not fit become blob references and their raw contents are returned for the transport to send as `blob` messages first (§6). The signer must be in the imported `.gud/members` with a push role and the matching key.
6. **Commit shape.** Tree = the link's tree byte for byte (modes `100644`/`100755`/`120000`, directories as git trees). Author = the link author's handle as name and their member address at the link (else at the previous link, else `<name>@gud.invalid`), dated `received_at`; committer = the mirror identity (default `gud mirror <mirror@gud.invalid>`), dated `linked_at`; both in whole seconds at `+0000`. Mirror commits are therefore deterministic: the same links and identity give the same commit ids on any machine.
7. **Commit message.** The intent with trailing whitespace removed and NUL written as `\0` (git refuses NUL), or `gud link <id12>` when empty; a blank line; then exactly `Gud-Link: <link state>` and `Gud-Tree: <tree hash>` as the last two lines. `Gud-Link` is the plan's `Gud-State` trailer under its §4 reference name; for a carried link it is the carried state.
8. **Progress and resume.** The remote is the only progress record: the head commit of the mirror branch (default `gud`) names the last mirrored link in its trailers. A mirror resumes after that link. It stops with `MirrorDivergence`, writing nothing, when the head has no such trailers, names a link that is not in the chain, or names a tree hash different from that link's. Links are pushed one at a time with a plain (non-force) push, so a CI watching the branch sees every link; a rejected push is divergence unless the remote head is now the very commit being pushed (another mirror got there first).
9. **Input.** Links are given in chain order and must be contiguous (`parent` = the previous link's state); a mirror into an empty branch starts at genesis.

## Implementation notes (transport)

Choices made by `gud/transport/` where §2, §6, §8 and §20 left room. Per-adapter configuration, capabilities and verification status are in `docs/v1/TRANSPORTS.md`.

1. **Envelope budget (§6), erratum.** §6 gives `B = min(7 340 032, floor((M − 65 536) × 3 / 4))`, but the `gud.env` part is base64 in 76-character lines each ending in CRLF, so 57 envelope bytes take 78 bytes of MIME, not 76. With `3/4`, a full-budget message can exceed `M` for any `M` below about 10.11 MB. That range includes 9.86–10.11 MB, where the 7 MiB cap already binds: a 7 MiB envelope is 10 044 254 bytes of base64 lines. `gud.transport.base.envelope_budget` uses `floor((M − 65 536) × 57 / 78)`. That is never larger than the §6 value, so a sender using it still obeys the §6 MUST. The two agree (both give the 7 MiB cap) for every `M ≥ 10 109 790` bytes, which includes every adapter shipped so far (10 MiB = 10 485 760 for fake and the IMAP default).
2. **Cursors (§20.1).** A cursor is an opaque string; `None` means the start of the mailbox. `list(since=c, limit)` returns messages delivered after `c`, oldest first, and the cursor of the last returned message (or `c` unchanged when nothing is new). Every `Received.cursor` is the position just after that message, so a consumer may stop part-way through a batch. Cursors survive process restarts. Delivery is at least once: after a reconnect or a provider-side reset a message may come again (IMAP: a UIDVALIDITY change rescans that folder from UID 1). Consumers dedupe by gud id (§4).
3. **`subscribe(callback, *, since=None)`.** The keyword is an addition to the INTERFACES signature. Without `since`, the subscription delivers mail that arrives after the call (the adapter takes its head cursor synchronously, before returning); with `since` it first catches up from that cursor. The callback runs on a background thread, may see one message twice, and an exception in it never stops the loop. To avoid a gap, an intake should subscribe first and then `list()` from its stored cursor.
4. **Webhook items are not resumable.** A `Received` produced from a webhook body (`WebhookVerifier.parse`) has `cursor == ""`. Intakes persist only cursors from `list()`/`wait()`/`subscribe()` and run `list()` catch-up on start, as §20.1 says.
5. **MIME layout (§2.1).** `gud.transport.mime.build` writes CRLF line endings, a quoted-printable UTF-8 text part (lines ≤ 76, trailing whitespace encoded), and base64 in 76-character lines. The default boundary is `gud-` plus 40 hex derived from the content and checked not to occur in either body, so a build is deterministic given its inputs. Header values have CR and LF replaced by spaces (no header injection). Non-ASCII subjects and display names use RFC 2047 encoded words. An `Outgoing` without an envelope is a single-part `text/plain` message; when its text does not end in a newline, the body ends in a QP soft line break so the text round-trips exactly.
6. **Finding `gud.env` (§2.1).** A part is a `gud.env` part when its `Content-Disposition` `filename` or its `Content-Type` `name`, after RFC 2231 decoding, is exactly `gud.env`. The walk covers every non-multipart part in depth-first order, including parts inside an attached `message/rfc822` ("at any depth"). A base64 body is decoded strictly after removing all whitespace. Any other transfer encoding is decoded as declared. A part that fails to decode makes the message ignored (`to_email` returns `None`), as does a second `gud.env` part.
7. **Unsigned text (§2.2).** The text is the first `text/plain` part in depth-first order that is not a `gud.env` part, decoded with its charset (unknown charsets as UTF-8, invalid bytes replaced), with CRLF turned into LF. `ParsedMime.has_text` (an added field) says whether such a part exists. `to_email` returns `None` for an unsigned message without one, since §2.2 says it is ignored. The gud id is still `sha256` of the raw bytes exactly as stored.
8. **Synthesized `Authentication-Results` (§20.1).** Fixed authserv-ids: `fake.gud.invalid` (fake transports) and `primitive.gud.invalid` (Primitive). The header is prepended, using the message's own line ending, before anything hashes the bytes. Because the adapter's header is topmost, a header with the same authserv-id forged inside the message is never the one §8.1(2) reads. Primitive evaluates DMARC on the organisational domain (`dmarcFromDomain`, e.g. `primitive.email` for `x.primitive.email`). The adapter writes `header.from` as the actual From domain, and keeps a `pass` only when that domain equals `dmarcFromDomain` or is a subdomain of it; otherwise it writes `fail`.
9. **Fake receiving server.** `FakeMailbox.send` and `NetworkMailbox.send` model authenticated submission: they prepend `fake.gud.invalid; dmarc=pass` only when `From` is the sending mailbox's own address, and `dmarc=fail` otherwise. `deliver()` injects raw bytes from an outside MTA and adds nothing.
10. **Structured-send providers.** A provider that builds MIME from structured fields (Primitive, Postmark) renders a raw-bytes `send` from its parsed fields: the envelope bytes, text, subject, addresses and threading headers are kept; other headers, the Message-ID and the MIME layout are the provider's. Primitive takes one `to`, so further To addresses are sent as Cc (replay never reads To or Cc). `Sent.message_id` reports the provider's rewritten Message-ID when it is predictable (Primitive: `<primitive-direct-<sent id>@<from domain>>`).
11. **IMAP specifics.** Folders are read in their configured order (default `INBOX`, `Junk`). `Junk` (or `Spam`) falls back to the folder LIST marks `\Junk`, and a configured folder that does not exist is skipped. `list` merges folders oldest-first by INTERNALDATE while keeping UID order within each folder, so every folder's part of the cursor only moves forward. `max_message_bytes` is the configured value, else the SMTP `SIZE` extension, else 10 MiB.
12. **Conformance location.** §20.2 names `tests/transport_conformance.py --transport <x>`. The suite lives in `tests/v1/transport/conformance.py` (the checks) and `tests/v1/transport/test_conformance.py` (one pytest parameter per transport). Select a transport with `-k <name>`. The live Primitive run needs `GUD_LIVE_PRIMITIVE=1`.

## Implementation notes (adapters)

Choices made by `gud/transport/{jmap,ses,postmark,gmail,spool}.py` where §2, §6, §8 and §20 and the transport notes above left room. Setup, capabilities and verification status are in `docs/v1/TRANSPORTS.md`. These four adapters are verified against local mocks of the providers' public APIs only, not against live accounts.

1. **Webhook-fed mailboxes (§20.1 `list`).** SES and Postmark only push. Their adapters store what the webhook delivers in a local `Spool` and serve `list`/`wait`/`subscribe` from it; a periodic `catch_up()` fetches what the webhook missed from the provider's store (S3; Postmark's message search). The cursor is the spool sequence number. A provider id is stored once and the first copy wins, so stored bytes, and with them an unsigned message's gud id (§3.3), never change even when a webhook copy and a catch-up copy differ (catch-up copies carry `dmarc=none`, note 3). List order is intake order. A provider that pushes out of order can therefore give a later row an earlier `received_at`; the host's clamping (§11.1) absorbs this.
2. **Delivery time (§20.1).** JMAP `receivedAt` (servers commonly give whole seconds; ties keep the server's order). Gmail `internalDate`. SES `receipt.timestamp`, or the S3 object's `LastModified` for catch-up. Postmark's inbound webhook has no timestamp, so it is the local arrival time, and the search result's `ReceivedAt` for catch-up.
3. **SES Authentication-Results.** SES reports DMARC as structured data (`receipt.dmarcVerdict.status`), so the adapter synthesizes `ses.gud.invalid; dmarc=<pass|fail|none|temperror> header.from=<From domain>` (PASS, FAIL, GRAY, PROCESSING_FAILED) and the repo pins `ses.gud.invalid` (§20.1 last bullet). An `amazonses.com` header that SES may write into the stored message is not relied on: it was not verified live, and a sender can forge one with that id lower down. Copies found by S3 catch-up have no verdict and get `dmarc=none`, so a human's unsigned reply that the webhook missed does not count. That is safe, but it means the webhook must be reliable for unsigned human mail.
4. **Postmark Authentication-Results.** Postmark exposes no DMARC verdict for inbound mail and adds no header of its own, so `auth_results` is `none`. On a Postmark repo no unsigned human message passes §8.1(2), and humans sign (§8.3). The `authserv_id` option is for a forwarding MTA the operator controls that writes the topmost header. Pinning any other id would let a sender's forged header count.
5. **SNS trust.** The signing certificate is fetched only from `https://sns.<topic region>.amazonaws.com/…pem` (no userinfo, no port) over verified TLS. It must name `sns.amazonaws.com`, be valid now, and is pinned per URL. The `TopicArn` must be configured, because AWS signs every account's topics. SignatureVersion 1 and 2 are accepted. There is no freshness window, since SNS retries can lag by hours; a replayed notification is deduped by `mail.messageId` (note 1). A `SubscriptionConfirmation` is confirmed only after its signature verifies and its `SubscribeURL` passes the same host rule.
6. **Postmark webhook trust.** Postmark does not sign webhooks. `verify` requires HTTP Basic credentials, the documented source IPs, or both, and refuses everything when neither is configured. The source IP is passed as `remote_addr=` or the `X-Gud-Remote-Addr` header, which the webhook server sets from the socket peer. This is an extension of `WebhookVerifier.verify`. A replay with valid credentials is deduped by `MessageID`.
7. **Gmail push (§20.1 `subscribe`).** A Pub/Sub push carries only `{emailAddress, historyId}`, so `parse` returns `[]` and `accept_webhook` returns the notification and wakes every subscription and `wait`, which then list from their cursors. The OIDC token is verified (RS256 via Google's JWKS, issuer, audience, service-account email, expiry ±60 s). Without a public URL the subscription polls `history.list` every `poll_interval` seconds, and `capabilities().push` is `none` unless a topic is configured. This is polling outside catch-up, which §20.1 discourages; it stays within the §20.2(4) 30 s Gmail bound.
8. **Message-ID (§20.1 `Sent.message_id`).** JMAP submits the raw bytes and keeps the Message-ID. SES rewrites it to `<MessageId@email.amazonses.com>` (`<region>.amazonses.com` outside us-east-1). Postmark writes `<MessageID@mtasv.net>`. Gmail sets its own, which the adapter reads back after sending (`format=metadata`). Human replies to these messages resolve through the rewritten id or the subject tag (§8.2).
9. **Cursors.** JMAP: `<c|q>|<state>|<receivedAt>|<ids at receivedAt>|<partial>`, where `c` is `Email/changes` from the state and `q` is an `Email/query` walk by `receivedAt` (used for `since=None` and after `cannotCalculateChanges`). Gmail: `h:<historyId>:<skip ids>:<last internalDate>` or `s:<base historyId>:<internalDate>:<ids>`; a history 404 falls back to a walk. Both fallbacks are at least once (§20.1 cursor note).
10. **What counts as delivered.** JMAP skips Emails whose only mailboxes have role `sent` or `drafts` (the adapter's own `Email/import` copies) and reads Junk. Gmail skips drafts and messages labelled SENT without INBOX or SPAM, and reads Spam. JMAP and Gmail apply no recipient filter (the account is the repo's). SES and Postmark keep only notifications whose recipients include the address (Postmark: or a configured alias), and SES catch-up takes every object under the repo's own S3 prefix.
11. **Sizes (§6 `max_message_bytes`).** JMAP: min(`maxSizeUpload`, ⌊`maxSizeAttachmentsPerEmail` × 78/57⌋ + 65 536), the MIME size whose envelope budget equals the attachment limit. SES: 40 000 000 with an S3 action, 150 000 when only an SNS action is configured (too small to host a repo: the conformance minimum is 1 MiB). Postmark: 10 000 000. Gmail: 25 000 000, with sends over 4 MB using media upload. Each adapter refuses an oversize message before calling the provider.
12. **Quota (§20.2(8)).** SES reads `GetAccount` `SendQuota.Max24HourSend` (−1 means unlimited, reported as `None`). Gmail reports the documented 500 a day for consumer and 2 000 for Workspace accounts, because the API exposes no remaining quota. Postmark and JMAP report a configured value or `None`. Every adapter raises a `QuotaExceeded` subclass for exhausted limits (HTTP 429 after retries; JMAP `overQuota`/`rateLimit`; SES throttling and `SendingPausedException`; Postmark ErrorCode 405; Gmail rate- and daily-limit reasons, where a 403 is not retried).
13. **Conformance harnesses.** The Postmark harness has no positive control for §20.2(6), because nothing verifies there (note 4). The SES, Postmark and Gmail intakes hold no connection a test could drop, so `subscribe_reconnect` runs for JMAP (and IMAP) only. Live harnesses (`GUD_LIVE_JMAP`, `GUD_LIVE_SES`, `GUD_LIVE_POSTMARK`, `GUD_LIVE_GMAIL`) skip by default. The SES and Postmark live harnesses read by catch-up because a test machine has no public webhook URL.
