# gud wire spec, version 1

This is the contract every gud implementation builds against. `docs/PLAN.md` is the design record; where this file is more specific, this file wins, and every place it departs from the plan is listed in section 24. If code and this file disagree, fix the code or change this file on purpose. The Python interfaces of the reference implementation are in `docs/INTERFACES.md`; provider setup is in `docs/TRANSPORTS.md`.

Sections 0–24 are normative. Appendix A records choices the reference implementation makes where this text leaves room (tools, the tester, the host's internals, the dashboard); it never changes what replay accepts. Where the main text says "the reference implementation", the named function in `gud.core` is normative (section 18).

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
- A message is an **envelope message** when exactly one MIME part has `filename="gud.env"` in `Content-Disposition` or `name="gud.env"` in `Content-Type`, compared exactly after RFC 2231 decoding. The search covers every non-multipart part in depth-first order, including parts inside an attached `message/rfc822`. A message with two or more such parts is ignored.
- The envelope bytes are that part's decoded body: base64 is decoded strictly after removing all whitespace; any other transfer encoding is decoded as declared. The envelope MUST start with a valid header block containing `Gud-Version: 1` (section 3). If the part does not decode, or the envelope does not, the message is ignored.
- **The human part is never read by replay.** It is for people. Senders SHOULD put in it: the free text (intent, reason, comment), the headers as `Name: value` lines, `Gud-Id: <id>`, and, for pushes, a git-style unified diff preview of the change truncated after 200 lines with a final line `[... N more lines]`.
- **Subject tag.** Every signed message's `Subject` MUST end with `[gud:<first 12 hex of its gud id>]`. This tag is how an unsigned human reply finds its target when a provider has rewritten Message-IDs (section 8.2). The rest of the subject is free.
- **Message-ID, In-Reply-To and References** are for threading in mail clients and for resolving human replies only. No gud rule references a Message-ID.
- **Layout (SHOULD).** The reference builder (`gud.transport.mime.build`) writes CRLF line endings, a quoted-printable UTF-8 text part (lines of at most 76 characters, trailing whitespace encoded), and the envelope in 76-character base64 lines. Its default boundary is `gud-` plus 40 hex derived from the content and checked not to occur in either body, so a build is deterministic given its inputs. Header values have CR and LF replaced by spaces; non-ASCII subjects and display names use RFC 2047 encoded words. A message without an envelope is a single `text/plain` part; when its text does not end in a newline, the body ends in a quoted-printable soft line break so the text round-trips exactly.

### 2.2 Unsigned human messages

A message received at the repo address that has no `gud.env` part is an **unsigned message**. Its text is the first `text/plain` part in depth-first order, decoded with its charset (an unknown charset as UTF-8, invalid bytes replaced), with CRLF turned into LF. A message with no `text/plain` part is ignored, and so is one whose text is empty. Section 8 says when it counts.

### 2.3 Version

Only envelope messages carrying `Gud-Version: 1` and unsigned messages are read. A repo is defined by its host log (section 11); there is no repo without one. Earlier versions of gud (v0: no host log, `Gud-*` trailers in the body) are not supported: a v0 message reaching a repo is an unsigned message, and has no effect unless its text is a human reply verb (section 8.3).

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
- A decoder MUST reject: a missing empty line, unsorted or duplicate names, non-canonical case, a value with a forbidden byte or leading/trailing space, a sig line that is not last, any bytes after the last record, a record whose `data` is not followed by exactly one `LF`, a `length` with a leading zero, a record line with two consecutive spaces, and an envelope without `Gud-Version: 1`. An encoder MUST refuse an envelope without `Gud-Version: 1`.
- Header values MUST be valid UTF-8. TAB is a C0 control and is rejected; C1 code points are allowed.
- The `Gud-Sig` value MUST be canonical padded base64 of exactly 64 bytes; non-canonical padding bits are rejected (otherwise two byte forms would decode alike).
- Unknown header names are allowed, kept, signed, and ignored by replay. Unknown records are ignored except in `push` and `revert`, where they make the message malformed.

These rules make encoding a bijection over the decodable set: one parsed envelope has exactly one byte form.

### 3.2 Records

Arguments that are repo paths are **percent-encoded**: every byte outside `%x21-7E`, and `%` itself, is written as `%` plus two **uppercase** hex digits. No other byte may be encoded. A decoder MUST reject lowercase hex, encodings of bytes that did not need encoding, and invalid UTF-8 after decoding. The path arguments are, by record: argument 1 of `patch`, `put`, `delete`, `mode` and `evidence`, and both arguments of `rename`. All other arguments are kept as written.

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
- `.gud/BASE`, `.gud/local` and every path under `.gud/local/` are **reserved** for working-copy metadata and MUST NOT be in any tree. (A file at `.gud/local` itself is rejected too, so a working copy can always create the directory.)

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

Senders SHOULD generate operations in this canonical way (receivers MUST accept any valid sequence), which is what the reference `gud.core.ops.diff_trees` emits:

1. Renames, sorted by `src`. A rename is detected only for an exact (mode, content) match whose destination has no file ancestor and is not a directory in the old tree.
2. **All** deletes, sorted. (Interleaving deletes per path would give an invalid sequence when a file becomes a directory or the reverse.)
3. Per remaining path in sorted order, `put`, `patch` or `mode`. A text file whose content and mode both change gets `patch` then `mode`.

Use `patch` only when both old and new bytes are valid UTF-8 with no NUL and the file exists before and after with a regular mode; use `put` for everything else (new files, binaries, symlinks, files where the patch would be larger than the file). A `patch` or `put` whose content does not fit the remaining inline budget (section 6) becomes a blob-reference `put`.

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
- Hunks MUST be in strictly increasing order of old start and MUST NOT overlap. The `new start` values are checked: each MUST equal the position the hunk lands at in the new file. For an empty new range, `new start` is the number of new-file lines before the hunk (the usual diff convention, mirroring old start).
- A lone `LF` is not an `hline`: an empty context line is written `" \n"`. Patch data MUST end with `LF`.
- A result in which a line without `LF` is followed by more lines is rejected.
- Text after the second `@@` on a hunk header is ignored.

Receivers MUST NOT reject a patch because the bytes are not UTF-8: application is byte-wise.

### 5.5 State hash

The state of a native link is its push's gud id: `sha256(signing bytes)`. Because the signing bytes contain `Gud-Parent`, states chain. The state of a carried link is defined in section 14.4. Genesis names `Gud-Parent: ZERO`.

What a signature covers: the author's intent, the operations, and the parent they named. It does **not** cover the final tree of a carried link. Replay derives that tree deterministically, and the test policy (section 15) decides when tests vouch for it.

### 5.6 Blame

- A file is **text** for blame when its bytes are valid UTF-8 and contain no NUL. Blame for a text file is one `(owner handle, link seq)` per line (5.4 line definition). Blame for any other file (binary, symlink) is one `(owner, link seq)` for the whole file.
- Genesis (link seq 0): every line and every file belongs to the genesis author. An empty text file has no blame runs. Symlinks are non-text.
- A new link: per changed file, lines equal in old and new keep their blame (alignment by a longest-common-subsequence line diff over the lines as bytes; implementations MUST use the same alignment as the reference `gud.core.ops.update_blame`, which is Python `difflib.SequenceMatcher(autojunk=False)` opcodes); inserted and replaced lines belong to the link's author. A `put` over an existing text file is aligned against the old content. A file deleted and re-created in one push gets fresh blame. A renamed file keeps its blame under the new path. A file whose content changes as a whole (non-text) belongs to the author. A mode change alone does not change blame.
- The link seq in blame also gives the line's `linked_at` through the chain, which ownership fading needs (section 13.1).

## 6. Blobs and size budget

- **Budget.** Let `M` be the smaller of the sender transport's `max_message_bytes` and the host's advertised `max_message_bytes` (section 21). The per-message envelope budget is `B = min(7 340 032, floor((M − 65 536) × 57 / 78))` bytes. An envelope larger than `B` MUST NOT be sent. (The `gud.env` part is base64 in 76-character lines each ending in CRLF, so 57 envelope bytes take 78 bytes of MIME. The two terms agree, at the 7 MiB cap, for every `M ≥ 10 109 790` bytes. An earlier draft used `× 3 / 4`, which lets a full-budget message exceed `M` below about 10.11 MB; a sender using `57 / 78` also obeys that older bound.) The reference is `gud.transport.base.envelope_budget`.
- A sender whose push would exceed `B` sends large `put` contents (and large `evidence` files) as **blob references** and sends the content as `blob` messages, chunked. The reference client starts with an inline allowance of `B − 8192 − len(intent)` and shrinks it until the signed envelope fits `B`; contents that do not fit become blob references, sent first, with `Gud-Chunk-Size = B − 2048`. Genesis and evidence files use the same path.
- **`blob` kind.** Headers: `Gud-Version`, `Gud-Kind: blob`, `Gud-Blob` (sha256 of the whole content), `Gud-Size` (total bytes), `Gud-Chunk` (`<i>/<n>`, `0 ≤ i < n`), `Gud-Chunk-Size` (bytes per chunk; every chunk except the last has exactly this size). One `data` record. Blobs need no signature; a signature, if present, is ignored. Content is the chunks concatenated in index order.
- A `blob` is malformed unless `n = ceil(Gud-Size / Gud-Chunk-Size)` and its `data` has exactly the length its index implies.
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
| `claim` | Task, Files | Revises, Release, Done, After | `text?` | yes |
| `task` | Title | Requester, Accept, Bug | `text?`, `accept?` | yes |
| `subtask` | Parent-Task, Title | After, Accept, Files | `text?`, `accept?` | yes |
| `no-change` | Task, Tip | | `text` (reason, one line, non-empty) | yes |
| `checkpoint` | Log, Link, Tree, Snapshot | | none | yes |
| `join` | Role, Address, Key | Sponsor | `text?` | yes, by `Gud-Key` |
| `notify` | Event, Event-Type | Target, Task | `text?` | yes |
| `blob` | Blob, Size, Chunk, Chunk-Size | | `data` | no |

(Header names in the table omit the `Gud-` prefix.) There is no `task-done` kind: done is derived (section 16.6). There is no `fix` kind: fix tasks are derived (section 15.5).

Validation (`gud.core.envelope.validate_kind`) checks the required headers, the format of every header the kind uses (64-hex ids, lists of ids, enums, handles, `Gud-Seen` and `Gud-Log`, keys, blob numbers) and the record order. In kinds other than `push` and `revert`, records the kind does not list are dropped before the order check (section 3.1). A missing `Gud-Sig` is not a kind error: the message fails verification (void, or `bad_sig` for a push). A `no-change` reason is "one line" when it contains no LF and no CR.

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
- `Gud-Done: yes` (claim): with `Gud-Revises` = the author's active claim, declares the claimed item's work complete (section 16.6). It combines with a scope update or with `Gud-Release`.
- `Gud-After`: list of task, subtask or claim ids that should land first. Advisory (section 16.4).
- `Gud-Title`: one-line title.
- `Gud-Requester`: handle of a human member. Default: the author if human, else the author's sponsor, else none. A value that is not a human member falls back to the default.
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

For (2), the authserv-id and `header.from` are compared case-insensitively. The topmost header with the pinned authserv-id decides even when it carries no `dmarc` result (that is a fail); any `dmarc=pass` in it with a matching `header.from` passes. Result syntax is parsed leniently, so only a value with no authserv-id at all is unparseable, and a malformed top header can never make a lower, forged one count. The reference is `gud.core.envelope.dmarc_pass`.

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
| `accept` | requester acceptance of the target task (section 16.6); no effect on a push |
| `object` | objection to the target push; the rest of the text is the reason |
| `veto` | veto of the target push |
| anything else | a comment; no chain effect |

An unsigned message with no resolvable target and no `In-Reply-To` **opens a task** (section 16.1); its author and requester are the sender.

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

All config files live in the tree and are read **at a stated link** so every member replays with the same values. A push whose resulting tree contains an unparseable config file does not link (status `bad_config`). A config file that is not a regular file (for example a symlink), and a tree without `.gud/members`, are `bad_config` too.

Common format for `.gud/members`, `.gud/owners`, `.gud/test`, `.gud/config`: UTF-8 lines; `#` starts a comment to end of line; blank lines ignored; fields separated by runs of ASCII space or tab. CR is an ordinary byte, so a file with CRLF line endings normally fails to parse. A key repeated in `.gud/test` or `.gud/config` makes the file unparseable; unknown keys are ignored in both.

### 10.1 Globs

Used by `.gud/owners`, `.gud/test` `on-paths`, `Gud-Files` and `Gud-Accept`. A glob is matched against the **whole** repo path:

- `**` as a whole segment matches zero or more segments (`a/**/b`, `**/x`, `a/**`).
- `*` matches zero or more characters other than `/`. `?` matches one character other than `/`.
- `[...]` is a character class; `[!...]` negates; ranges by code point.
- `\` escapes the next character.
- A trailing `/` means `/**`. There is no implicit match at any depth: `*.css` matches only top-level files.
- Because `**` matches zero segments, `a/**` (and `a/`) also match the path `a` itself. Character classes never match `/`.
- A leading `/`, an empty segment, an unterminated `[` or a trailing `\` is a syntax error.

**Overlap.** Two glob lists `a` and `b` overlap at tree `X` when some glob string appears in both, or some path in `X` matches a glob in `a` and a glob in `b`, or a glob in one, read as a literal path, matches a glob in the other. An invalid glob matches nothing, though the same string listed on both sides still overlaps.

### 10.2 `.gud/members`

```
# handle role address public_key sponsor
@auth-agent agent auth-agent@example.org ed25519:BASE64 @nelson
@nelson human nelson@example.com ed25519:BASE64 -
@t1 tester t1@example.org ed25519:BASE64 @nelson
@host host repo@example.org ed25519:BASE64 -
```

- Five fields per line. `address` is a bare address. `public_key` is `ed25519:...` or `-`. `sponsor` is a human member's handle or `-`; parsing checks only that it is a handle, not that it names a member.
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
- A file named `.gud/protected` has no meaning (it was the v0 form of protection).

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

`on-tip` takes `links=` and/or `time=` in any order; a missing one is off. With no `command`, no test requirement applies to any push and acceptance criteria cannot pass (section 16.6). The policy is read at the candidate's parent.

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
| `task-quiet` | quiescence before a landed, unclaimed root task is work-done (16.6) | the lease `L` |
| `checkpoint` | links between checkpoints | `1000` |
| `authserv-id` | pinned authserv-id for unsigned human mail | none (no unsigned mail counts) |

Minimums: `own-links` ≥ 0; `lease-thrash`, `checkpoint`, `stack-max` (10.4) and `on-tip links` ≥ 1. `gud host init` and `gud doctor` measure delivery latency and propose `window` and `grace` (`grace` ≥ measured p99 delivery delay; `window` ≥ 2 × `grace`). Changing them is an ordinary push (quorum, section 10.3). Unknown keys are ignored. The values used for a candidate are those at its parent; the lease `L` and `task-quiet` are read at the time of the activity they follow (section 16).

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
| `handover` | `next ed25519:<new host public key>`, and optionally `repo <new repo address>` (a bare `local@domain`) when the repo moves (section 11.6) |

- **Entry hash** = `sha256` of the entry bytes up to and including the `LF` before `sig`.
- The signature is Ed25519 over the same bytes, with the **current host key**: the `key` of the most recent `open`, or for the entries after a `handover`, its `next` key. `sig` MUST be canonical base64 of 64 bytes, and an entry MUST be canonical (re-encoding it gives the same bytes).
- `seq` starts at 0 and increases by 1. `prev` is the previous entry's hash (`ZERO` for seq 0). Entry 0 is an `open`, and `from` is forbidden at seq 0.
- An `open` at seq > 0 MUST cite the immediately preceding entry (`from = <seq−1>:<prev>`), use either the key named by a `handover` immediately before it or a locally trusted key (section 11.6), and name the same `repo`, or the new address its `handover` names. After a `handover`, the next entry MUST be that `open`.
- `time` is **strictly increasing**. For a `mail` entry the host sets `time = max(min(provider received time in ms, c), previous time + 1)`, where `c = max(host clock, previous time + 1)` is the host's clock reading at append; a provider time ahead of the host clock is clamped to it, and a burst within one millisecond or a clock step backwards never makes an entry impossible. A `tick` takes `time = c`. Received time in this spec always means this log time.
- The host appends a `mail` entry for every envelope message that decodes and every unsigned message that passes 8.1 (2) and (3) at the tip, and for nothing else. "At the tip" is the tip at the message's received time τ, after every timer due before τ (section 12.4): when replay has a timer due before τ that the log has not reached, the host first appends a `tick` at τ − 1 and replays it, then judges the message. The host stores the raw MIME before appending its entry, so a logged id always has its mail. It SHOULD NOT log one id twice (the reference host refuses to; section 4 dedupes if it happens).
- The host appends a `tick` as soon as its clock reaches the earliest time at which replay can change without new mail (window closes, `window.closing`, parks, expiries, lapses, deadlines, quiescence; `ChainDelta.next_wakeup`). While such a timer is pending (every open proposal, claim or escalation has one) it also ticks after 5 seconds without an entry; with none, at least every 60 seconds. More ticks than this are allowed. After its `handover`, a host appends nothing.

### 11.2 Serving

The mirror serves the log at `GET /api/v1/log?since=<seq>&limit=<n>` as `{"entries": ["<entry text including sig line>", ...], "head": {"seq": n, "hash": h}}` and the raw MIME of each logged message at `GET /api/v1/mail/<gud id>` (section 21).

### 11.3 What replay takes from the log

- **Only logged messages exist.** An email with no `mail` entry is invisible to replay.
- **Received time** of a message is its entry's `time`. The **order** of messages is log order.
- **Replay time.** Replay at local clock `now` uses `t = min(now, time of the last log entry)` (`now` itself on an empty log). Because log times strictly increase, any future entry has a time greater than `t`, so a decision at `τ ≤ t` never changes.
- **Gaps.** If the raw message for entry `n` is not available, replay uses `t = min(now, time(n) − 1)`.

### 11.4 Verification

A member verifies, for each new entry: format, `seq`, `prev`, strictly increasing `time`, signature under the current key, and, for `mail`, that the fetched message's gud id equals `id`. Any failure stops replay at the last good entry and is reported (`gud audit`): the verified prefix is kept and replayed, and the failure is raised with that prefix's result (`ReplayError.delta`). A served log head behind the member's verified log (rollback), or a served entry at a verified seq with a different hash (fork), stops the sync the same way. A fetched message whose gud id differs from its entry's is dropped and the entry treated as missing (the gap rule above).

### 11.5 Gud-Seen and fork evidence

- Every signed message carries `Gud-Seen`, the latest entry its sender verified. Members MUST keep every log entry they cite, with its signature.
- Replay checks each `Gud-Seen` (section 9). A mismatch makes the message void and emits a `host.fork` event naming the message and the two hashes. On its own this is a suspicion: a member can lie about what it saw, but only to void its own message.
- **Fork proof** is two entries with the same `seq`, different hashes, each validly signed by the key current at that `seq` in its own log. `gud audit` builds it from a member's retained entry and the served log. Its JSON form is `{"a": "<entry text>", "b": "<entry text>"}`. A proven fork means the host showed members different histories.

### 11.6 Host handover

- **Planned.** The old host appends `handover` with `next` = the new key, signed by the old key, and, when the repo moves to another address, `repo` = that address. The new host continues the same log: its first entry is `open` with `from` citing the handover entry, `key` = the new key, `repo` = the handover's `repo` if it has one and the current address otherwise, signed with the new key. Then a members push changes the `host` line to the new key (and address), linking like any members change. The `host` role cannot push (section 9), so a member sends it (normally the console or a human). The same host rotates its own key the same way within its log: `handover` at `max(head time + 1, now − 1)`, then `open` 1 ms later.
- **Unplanned.** If the old host cannot sign, the new host starts with `open` citing the last entry it holds in `from`. Members accept that segment only after a local, explicit trust decision (`gud host trust <key>`); replay records that decision as part of its log input. The members push recording the new key follows as above. An unplanned open cannot move the repo to another address.
- **Moving the repo.** Replay never reads `To`, and the `host` line's address is informational, so the chain, its tip and blame continue across an address change. The log's current address is that of its latest `open`. Members follow it: they send to the current address of their verified log, and point at a new host only after checking that its log continues the verified one (`gud host follow URL`). Inbox copies sent to any address the log has had count as repo mail for `gud audit`.
- Webhook and mirror signatures (section 19) are verified with the `host` member key at the tip **or** the current log key. Until the members push links, the two differ.

## 12. Replay and link rules

### 12.1 Inputs and visibility

Replay is `replay(emails, log, now)`. At decision time τ, a message is **visible** when its log time ≤ τ. Every decision at τ uses only messages visible at τ.

### 12.2 Genesis

Genesis is the earliest-logged `push` with `Gud-Parent: ZERO` that: decodes; whose operations apply to the empty tree to a tree with hash `Gud-Tree` whose config files parse; whose `.gud/members` has exactly one `host` line whose key equals the log key at the push's entry; and whose signature verifies with `Gud-Author`'s key in that tree. Genesis needs no consent and no test. `seq = 0`, `linked_at` = its received time.

### 12.3 Candidate evaluation

For the current last link `Y` and time τ, the **candidates** of `Y` are: each push or revert with `Gud-Parent = Y.state` (native), and each live push carried onto `Y` (section 14). For each candidate `c` of push `P`, evaluation gives one of: **qualified at q** (q ≤ τ), **pending** (with a reason and a park time), or **dead** (terminal). Checks in order; the first that applies decides:

1. **Intrinsic** (once, for the native candidate): malformed envelope or records → `malformed`; signature or role fails → `bad_sig`; operations fail on the named parent or the tree hash differs from `Gud-Tree` → `conflict`; resulting tree has an unparseable config file → `bad_config`. These are dead for all candidates of `P`.
2. **Author removed or key changed**: the author is not a member of `Y` with an allowed role, or the push's signature does not verify with the author's key in `Y`'s members → dead `bad_sig`. (A member who rotates keys re-sends pending pushes signed with the new key.)
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
- **Parents.** `Gud-Parent` MUST name a link state exactly; the mapping of carried ids in section 4 applies to votes, tests and claims only. A push whose `Gud-Parent` is not a link is **orphan** while that parent might still link (it is a pending candidate somewhere) and **stale** once it cannot. A push whose parent is unknown when it is logged, or that names a push which linked under a carried state, is `stale`, and so is a `ZERO`-parent push after genesis. A push logged after its named parent stopped being the tip is carried straight onto the tip; if it is not carryable it is `superseded`, by the tip.
- **Order of work at one time τ.** Replay advances through log-entry times and its own timers. At τ it applies, in this order: claim lapses due by τ, then quiescence due at τ (section 16.6); the log entry at τ; candidate evaluation, escalation updates and the walk, repeated while a link happens at τ (a new link's candidates may qualify at the same τ, so consecutive links can share `linked_at`); then `window.closing` events due at τ. "Tip at received time τ" (section 9) is the tip before anything decided at τ, so a message is never verified against a link its own arrival caused.

### 12.5 Link rules, summarised

A candidate `c` of `Y` links when:

1. **Authorship.** Its push is validly signed by a member with an allowed role at its named parent, and the author is still such a member at `Y`, with a key that verifies the push (sections 9, 12.3).
2. **Applies.** Its operations apply to the named parent with tree hash `Gud-Tree`, and, if carried, it is carryable onto `Y` (section 14).
3. **Consent.** Consent v1 holds against `Y` (section 13).
4. **Test policy.** `Y`'s `.gud/test` policy is met (section 15).
5. **Race.** It is first in the walk at the decision time (section 12.4).

## 13. Consent v1

Computed for candidate `c` of push `P` (author `a`) on parent `Y`, against `Y`'s tree, blame, members and config, at evaluation time `E(c)`.

### 13.1 Line owners (fading)

- Touched lines: lines `P`'s change removes or rewrites in `Y`'s tree (for a path changed by exactly one `patch`, its `-` lines; for any other edit of a text file, such as a `put` or several operations on one path, the lines a `difflib` diff of the before and after bytes marks replaced or deleted; for `delete`, `rename`, `mode` and a non-text `put`, every line or the whole file); plus, for lines inserted inside a Python `def` or `class` in a `.py` file (by `ast` of `Y`'s version), the line owning that `def`/`class` line. An insertion before old line `i` (0-based) is inside a `def`/`class` spanning 1-based lines `L..E` when `L ≤ i < E`; the innermost such definition counts. Unparseable Python adds nobody. The reference is `gud.core.ops.touched_lines`.
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

Consent holds when **every** applicable part holds; its time is the latest part time. Each part holds at the earliest τ meeting its condition, and consent is **monotone**: once the lazy part has held, a counting objection received later does not undo it.

- **Lazy part** (applies when `A` is non-empty): holds at the earliest τ with no blocking objection in force (13.5) and either every member of `A` has a counting approve, or τ ≥ `close(c) + G`. When `A` is empty, it holds at `E(c)`: there is no window, objections have no effect, and a human who must stop the push uses `veto`.
- **Explicit part**, for each path with an `explicit` line: every member of `D_explicit[p]` approves; if that set is empty, one approve from any voter other than `a`. No deadline. Explicit owners, like askers, are filtered to voters at `Y`.
- **Quorum part**: approves from `floor(n/2) + 1` of the **quorum voters**: voters at `Y` whose `.gud/members` line `P` does not add, remove or change (`n` is their count). `a` counts as approving, at `E(c)`, if eligible.
- **Members rule**: one approve from a human member at `Y`. A human author's own push counts as that approve (at `E(c)`). Line owners of `.gud/members` are not asked.

An approve **counts** when it targets `P` or any candidate id of `P`, is from a voter at `Y` (or a verified human, section 8), is not void, and is visible. Its time is its received time. An approve received after the same voter's objection withdraws that objection. Votes on a candidate are verified against the members of the candidate's parent `Y` at evaluation time (deviation 17).

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
  - An evidence test counts only when the evidence content is available (inline, or a complete blob), its `Gud-Tree` equals the candidate tree with the file added at mode `100644` (or the candidate tree itself when it already holds those exact bytes) and, on a candidate of the objected push, `Gud-Base-Result` is present.
  - A confirmed objection inherited by later pushes of the negotiation blocks them whatever their askers, and the vote rule is not re-checked for them. It is cleared for a push `P′` only by a revision received at or before `P′` (`P′` included), so a later clearing revision does not unblock an earlier push.
- **Veto**: from a human member at `Y`, targeting `P`, visible before `P` links: an unsigned verified reply whose first word is `veto`, or a signed `object` from a human whose `text` starts with the word `veto`. Terminal. Agents cannot veto.

### 13.6 Negotiations and rounds

- A push's **negotiation** is the set of pushes connected to it by `Gud-Revises` links (all by the same author). The root is the one with no valid `Gud-Revises`.
- For each objector `v` and negotiation `N`, an **episode** starts with a counting objection by `v` on any push in `N` received after `v`'s previous episode was answered. An objection counts toward episodes once it has counted on a candidate of its push. An episode is answered by the first answer to any of its objections (13.5: an author comment on the objection or the push, or a revision). An evidence objection still failing on a revision (first evidence test on the revision reports `fail`) starts a new episode for its objector, once for each such later push in the negotiation.
- **Rounds rule.** When any objector reaches **two episodes** in `N`, `N` is escalated (13.7). An objector escalates on purpose by objecting again after being answered.

### 13.7 Deadlock and escalation

Replay opens an **escalation** at the time τ0 when either holds:

- **Mutual**: two open (live, unlinked) pushes `P` and `Q` where `author(P)` has a blocking objection on `Q` and `author(Q)` has a blocking objection on `P`.
- **Rounds**: section 13.6.

An escalation has:

- `id = sha256("gud-escalation/1\n" + sorted push ids (negotiation roots) joined by "\n" + "\n" + reason + "\n")`, with `reason` `mutual` or `rounds`.
- **Deciders**: the requesters of the tasks named by the pushes' `Gud-Task` (resolved to the root task), plus the sponsors of the pushes' authors; if that is empty, every human voter at the tip.
- **Deadline**: `τ0 + escalate`.
- **Decision**: the first counting decider vote received in `(τ0, deadline]` (strictly after the opening, so the objection that opens a rounds escalation is not its decision): an approve of one of the escalated pushes (or any push in its negotiation) **chooses** it; an `object` or `veto` of one **declines** it (in the mutual case, the other is then chosen). Decider votes are verified at the tip.
- **Default at the deadline**, deterministic:
  - Mutual: the negotiation whose root has the earlier received time is chosen (ties by id).
  - Rounds: the push is chosen unless it has a confirmed, uncleared evidence objection, in which case it is declined.
- **Effect**: a chosen negotiation has all its objections, including later ones, made advisory from the decision time; every push of a declined negotiation is dead `declined`. Explicit, quorum and members parts still apply to the chosen push, and it still waits for its own lazy window.
- An escalation id is opened at most once, and no new escalation opens over a negotiation already in an open one.
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

A `revert` is carried by the same rule over the paths it touches (the reverted link's `files`). A push whose blobs are still incomplete is carried by the file rule on paths alone, whatever the carry mode; its tree, consent plan and `review.requested` follow once the blobs complete.

**Three-way merge** (`three_way`, also used by section 15.6 and by working copies, section 22) is byte-wise over lines (section 5.4), with `difflib` opcodes (autojunk off). An edit made identically on both sides applies once instead of conflicting. Delete against modify conflicts in both directions. Modes merge three-way. Binaries and symlinks changed on both sides conflict. A merge whose result would break 5.1 reports a conflict for that path. Conflicting paths keep `theirs`.

### 14.2 Derived tree

- File mode (and line mode when no later link touched `P`'s paths): `Y`'s tree with each path `P` touches set to `P`'s native entry, or deleted. This equals applying `P`'s operations to `Y`'s tree under the file rule.
- Line mode: `Y`'s tree with each path `P` touches replaced by its merge result.

If the derived tree breaks 5.1 or a config file does not parse, `P` is not carryable.

### 14.3 Consent on carried candidates

Consent is recomputed against `Y` (section 13). Votes on `P` and on any of its candidates carry over. A carried candidate's window open is `P`'s previous window open when its askers are a subset of the previous candidate's askers (no new owner is asked), and `max(received(P), Y.linked_at)` otherwise. Only the newly affected owners are therefore given a fresh window. The plan is computed first with the previous `E`; if its askers are not a subset, `E` becomes `max(received(P), Y.linked_at)` and the plan is recomputed once with that `E`. A push first logged after `Y` linked, whose named parent is an older link, is carried straight onto the current last link with `E = received(P)`.

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

The first `push`-scope result naming `c`'s id decides: a `fail` (or a `pass` whose tree mismatches) makes `c` dead `tests_failed`. For a carried candidate, a result naming the push id counts when its `Gud-Tree` is the candidate's tree.

### 15.2 Testers

Testers read `.gud/test` at the parent, run the command (in `image` if set) with `timeout`, and sign `test`. Several testers may run. For any (scope, target, tree) the first logged result counts. Exit status 0 within `timeout` is `pass`; anything else, including a timeout, is `fail`. A run that cannot happen at all (runner unavailable, a tree that cannot be materialised) is not a result: nothing is signed and the candidate parks (12.4). Appendix A.3 describes the reference tester.

### 15.3 Batched stacks (`every-link on`)

- A tester picks a base link `X` and up to `stack-max` pending candidates in race order, computes the tree from applying them in sequence (each carried onto the previous result by file-mode rules; a non-carryable member ends the stack), tests that tree once, and sends `test` scope `stack` with `Gud-Target = X.state`, `Gud-Stack = P1,...,Pk` (push ids) and `Gud-Tree` = the final tree hash.
- Replay checks `Gud-Tree` by recomputing the stack. A stack pass **vouches** for the candidate of `Pi` when the links from `X` are exactly `P1 … P(i−1)` (native or carried) and the candidate is `Pi` on the result. So if all `k` link in order, all `k` are vouched for by one run. Replay looks for vouching stacks at most 64 links back from the candidate's parent; a stack member must have passed its intrinsic check (12.3 step 1) and name a parent at or before the stack base.
- On a stack fail the tester bisects by testing sub-stacks; only a failing stack of one push (`k = 1`) or a failing `push`-scope test kills a candidate.

### 15.4 Tip tests and red tip

- **Tip runs.** Testers test the tip on the `on-tip` cadence (every `links` new links or every `time`, whichever comes first). These runs never block linking. The cadence is a tester duty; replay does not enforce it.
- **Verdict** of a link `L` is the first visible `tip`-scope result with `Gud-Target = L.state` and `Gud-Tree = L.tree_hash`. A result naming a link that does not exist, or with a different tree hash, is ignored.
- At τ, let `L*` be the highest-seq link with a verdict. The chain is **red** at τ when `verdict(L*) = fail`, else **green**. `last_green` is the highest-seq link with a pass verdict.
- **Red start**: the τ at which the chain turns from green to red; `red_link` is `L*` at that moment.
- **Bisect.** While red, testers test links between `last_green` and `red_link`. `first_bad` is the link right after `last_green` when its verdict is `fail` (with no green link: genesis, when its verdict is `fail`). Equivalently, the lowest-seq link `B` in `(last_green, L*]` whose verdict is `fail` and whose parent is `last_green`. It is undefined until such a verdict exists.
- Red ends when a link after `red_link` gets a `pass` verdict (`tip.green`).
- While red, pushes keep linking.

### 15.5 Fix tasks

- At red start replay derives a **fix task** with `id = sha256("gud-fix/1\n" + red_link.state + "\n")`. It cites `red_link` until `first_bad` is known, then `first_bad` and its author.
- Its requester is the sponsor of the cited link's author (none if no sponsor). Its scope is the paths the cited link touched.
- **Offer**: during `[red start, red start + fix-offer)` only a claim by the cited link's author can win it; after that, anyone's. When bisect finds `first_bad`, the task is re-cited and re-offered to that author, and its requester and scope are updated; the offer window still runs from red start.
- It is **done** when red ends; every open fix task is done when the chain turns green. It does not need subtasks, criteria or acceptance.

### 15.6 Revert and auto-revert

- A `revert` names `Gud-Revert = B.state` for a link `B` with parent `B⁻`, and `Gud-Parent = Y0` (any link at or after `B`). Its derived operations restore, for each path whose entry differs between `B⁻` and `B`, `B⁻`'s entry: if `Y0`'s entry for the path equals `B`'s, set it to `B⁻`'s (or delete it if absent there); otherwise three-way merge with base `B`'s version, mine `B⁻`'s, theirs `Y0`'s, and on conflict the revert is dead `conflict`. `Gud-Tree` must match the result. The reference is `gud.core.ops.invert_ops`.
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
- **Lease.** An active claim's `last_activity` is the latest of: the claim, any claim update from the holder (`Gud-Revises` = the active claim), any push from the holder whose `Gud-Task` is the item or a descendant, any `comment` from the holder with `Gud-Task` set to the item or a descendant. The claim **lapses** at `last_activity + L` (`claim.lapsed`), with `L` read at `last_activity`. It also ends when the item is done or the holder releases it (`Gud-Release: yes`).
- **Scope update.** A claim from the holder with `Gud-Revises` = the active claim replaces its `Gud-Files` and renews the lease. An update or release may name the active claim by its winning id or by its latest update's id. A `Gud-Release` that matches no active claim of the author is ignored.
- **Removal.** A claim ends when its holder stops being an allowed member at the tip.
- **Fix tasks** follow the offer rule in 15.5.

### 16.4 Overlap and scope

- When a claim wins or updates, replay compares its scope with every other active claim at the tip (10.1 overlap). Each overlapping pair emits `claim.overlap` to both holders, once per pair of claims. A pair where one claim's item is an ancestor of the other's never overlaps: splitting is the holder's plan. Holders settle overlaps in `comment`s: split the files (narrow scope with a claim update), sequence the work (`Gud-After`), or release.
- Scope is **advisory**. A push outside its claim's scope links normally. If a push touches paths inside another active claim's scope, replay emits `scope.exceeded` to the author and that claim's holder, except to the holder of a claim on an ancestor of the push's task.
- `Gud-After` is advisory: it orders events and agent behaviour, not linking.

### 16.5 Subtasks (self-splitting)

A `subtask` is valid when, at its received time, its author holds the active claim on `Gud-Parent-Task`. A valid subtask stays valid after that claim ends. Subtasks can be split again by the holder of a claim on them.

### 16.6 Derived done

For a task or subtask `X`, at τ:

- **Work done** at τ:
  - if `X` has valid subtasks: all of them are done;
  - otherwise, if `X` is a subtask: a link with `Gud-Task = X` exists, or an accepted `no-change` or an accepted done declaration for `X` holds (subtasks are small by construction, so the first link carrying the id is their work);
  - otherwise (a root task with no valid subtasks): an accepted **done declaration** for `X`, or an accepted `no-change` for `X`, holds, or `X` is **quiescent** at τ. A link carrying `Gud-Task = X` alone never makes a root task's work done, whoever sent it.
- **Quiescence.** A root task `X` (not a fix task) with no valid subtasks is quiescent at τ when all hold at τ: at least one link carries `Gud-Task = X`; `X` has no active claim; no live push (not linked, not dead) carries `Gud-Task = X`; and τ ≥ `A + Q`, where `A` is the latest **activity** on `X` and `Q` is `task-quiet` (10.5) read at `A`. Activity on `X` is any of, at its received time (a link at its `linked_at`): a `claim` with `Gud-Task = X` (won, void, update or release), a push with `Gud-Task = X` from a member that verifies at the tip, a `comment` with `Gud-Task = X` from a member, and a link carrying `Gud-Task = X`. Comments without `Gud-Task`, votes and tests are not activity. So work becomes done by quiescence at the latest of `A + Q`, the time the last claim on `X` ended (release or lapse), and the time the last push carrying `X` linked or died. It is the fallback for a task whose work landed through others or whose holder stopped without declaring; the holder's declaration stays the fast path. Quiescence is not sticky: until `X` is done, new activity on it (for instance a claim that wins) revokes the work-done, and it holds again `Q` after the last activity once the other conditions hold. The work-done link is the tip at that τ, and the done rules below apply unchanged (criteria on that link or later, not red).
- A **done declaration** for `X` is a `claim` with `Gud-Done: yes` and `Gud-Revises` naming the active claim on `X` (an update, or a release with `Gud-Release: yes`). It is accepted while all hold at its received time: its author holds the active claim on `X`; `X` has no valid subtasks and is not a fix task; and the author has no live push with `Gud-Task = X`. The work-done link is the tip at the declaration's received time, so a criteria pass must be on that link or a later one. A declaration is not dropped by later links. The scope update or release it carries applies whether or not the declaration is accepted.
- **Done** at the earliest τ where work is done **and** the chain is not red **and**:
  - if `X` has criteria: a tester `accept`-scope pass with `Gud-Target = X` on a link `L` with seq ≥ the link at which work became done, received ≤ τ, while not red;
  - else if `X` is a root task with a requester who is a human member: a requester acceptance (unsigned `accept`/`approve` reply targeting `X`, or a signed `approve` with `Gud-Target = X`) received after work became done;
  - else nothing more.
- A done root task stays done. A subtask's `no-change` can be dropped (below) only while its root task is not done.
- When work is done but acceptance or criteria are pending, replay emits `task.ready` (to testers if there are criteria, to the requester otherwise). When done, `task.done`.

**no-change** for `X` is accepted while all hold: its author holds the active claim on `X` and sent it after the claim won; `Gud-Tip` is a link; the author has no live push with `Gud-Task = X`; and every path matching the claim's `Gud-Files` has the same entry at `Gud-Tip` and at the tip (with `**`, the tree hashes are equal). A later link changing one of those paths drops it; dropping it revokes work-done and done for the item and for its ancestors below a done root.

### 16.7 Escalation (tasks)

Replay emits `escalated` to the task's requester and the sponsors involved when: a consent escalation opens (13.7); claims on one item lapse `lease-thrash` times; or a member sends `comment` with `Gud-Escalate` and `Gud-Task`. Only 13.7 escalations have a chain effect.

### 16.8 Bug reports

Mail to the bugs address (`bugs@<repo domain>` unless configured) is a bug report. It is not logged and has no chain effect. Its id is `sha256(raw MIME)`, served by the mirror at `GET /api/v1/bugs`. Any voter other than the reporter triages it: accept by opening a `task` with `Gud-Bug` = the report id; reject with a `comment` targeting the report id and `Gud-Verdict: reject`. A task citing `Gud-Bug` needs a voter author, and only the earliest-logged task citing a given `Gud-Bug` is valid. Report text is untrusted data.

## 17. Checkpoints and epochs

- **Snapshot bytes** for log position `n` are the RFC 8785 (JCS) canonical JSON of the replay state after feeding all entries `≤ n` with `now = time(n)` (`gud.core.replay.snapshot_at`). Its schema is `gud.core.replay.Snapshot` (`docs/INTERFACES.md`), which is normative: version, log head, `t` (the replay time), and `state`, the engine's full persisted state. `state` is a superset of what replay needs to continue: links with their content-derived trees, blame and rules roots; every open candidate with its votes; push records, votes, tests and blobs; tasks, claims, escalations, red state and open fix tasks; and the ids of every message seen (a sorted list) so dedupe survives. It also has a `tip` section: the tip tree entries `(path, mode, sha256, size)`, the tip's blame runs (runs whose ownership has expired are stored as owner `-`), and the rules bundle (members, owners, test and config bytes). Fields added to the schema are written only when they differ from their default, so a snapshot that does not use them keeps its earlier bytes.
- **`checkpoint`** messages from voters name `Gud-Log = n:hash`, `Gud-Link` (the tip at `n`), `Gud-Tree`, and `Gud-Snapshot = sha256(snapshot bytes)`.
- A checkpoint is **valid** when signed by more than half of the voters at `Gud-Link`, all with identical `Gud-Log` and `Gud-Snapshot`.
- **When.** Voters SHOULD checkpoint at the log entry `n` that is the last entry with `time ≤ linked_at` of every link whose seq is a multiple of `checkpoint`.
- **Clone from checkpoint.** A new member fetches the snapshot (`GET /api/v1/snapshot/<hash>`), checks its hash against a valid checkpoint, loads it, and replays entries after `n`. The log is still fed from seq 0 (it is never truncated); entries up to the snapshot head are checked against it and not re-processed. Tree contents, and the tree and blame nodes of older links, come from blobs or the mirror (`GET /api/v1/object/<sha256>`, every object checked against its hash). The reference client reads logged mail from the head backwards (at most 5000 entries), groups `checkpoint` messages by `(Gud-Log, Gud-Link, Gud-Snapshot)`, and accepts the newest group whose `Gud-Log` matches the served log, whose snapshot hashes to its name, whose `Gud-Link` is the snapshot's last link, and whose signers are more than half of the voters **in the snapshot's own tip rules**. That trusts the quorum the snapshot names; `gud verify --from-genesis` removes the trust.
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
| `approved` | approve id | approve, push, by | author |
| `held` | hold id | hold, push, by, closes_at | author |
| `test.passed` / `test.failed` | test id | test, scope, target, tree, tester, log (the log tail) | author(s) of the pushes covered; for `accept`, the task's holder and requester |
| `window.closing` | candidate id, closes_at | candidate, closes_at | author and askers who have not voted; emitted at `close − min(10 s, W/4)` |
| `superseded` | push id, link state | push, by_link | author |
| `push.rejected` | push id, status | push, status, reason (`bad_sig`, `malformed`, `conflict`, `bad_config`, `tests_failed`, `vetoed`, `declined`, `expired`, `stale`) | author |
| `carried` | carried state | push, candidate, onto, files | author |
| `linked` | link state | link, push, seq, author, task, files, carried_from | author; every agent; holders of claims whose scope overlaps the files; authors of live pushes that touch the same files |
| `task.opened` | task id | task, alias, title, requester, criteria | agents |
| `subtask.posted` | subtask id | subtask, alias, parent, title, after, files | agents |
| `claim.won` | claim id | claim, task, holder, files | holder; requester |
| `claim.lost` | claim id | claim, task, active_claim | the claimant |
| `claim.lapsed` | claim id, lapsed_at | claim, task, holder, lapsed_at | agents (including the former holder) |
| `claim.overlap` | claims (the sorted pair of claim ids) | claims, holders, globs | both holders |
| `scope.exceeded` | push id, claim id | push, claim, paths | author; that claim's holder |
| `task.ready` | task id, work-done link | task, link (the work-done link), needs (`criteria` / `accept`) | testers (criteria) or requester (accept) |
| `task.done` | task id | task, link, no_change list (subtask, reason) | requester; holders of claims on the task and its subtasks; every agent |
| `escalated` | escalation id, or (task, reason, at) for a task escalation | escalation, reason, pushes, positions (objections and answers), deciders, deadline, default | deciders; authors; objectors |
| `escalation.resolved` | escalation id | escalation, chosen, declined, by (`decider` handle or `default`) | deciders; authors; objectors |
| `tip.red` | red link state | red_link, last_green | agents; testers |
| `tip.bisected` | red link state, first_bad state | red_link, first_bad, author, fix_task | agents; testers; first_bad's author |
| `tip.green` | green link state | link, red_link | agents; testers |
| `fix.opened` | fix task id | task, alias, cited_link, author, offered_to, offer_until | agents |
| `host.fork` | message id, or `sha256(JCS(proof))` when there is no message | message, cited, log_hash, proof (when proven) | every member; sponsors |

An event's data never changes after it is emitted; a fact learned later is a new event (`tip.bisected` carries `first_bad` once bisect finds it, and from then on the fix task cites it). Dispatchers never re-send an id with different data.

- **Keys are data fields.** Each key part in the table is a field of `data` (`gud.core.envelope.EVENT_KEYS`), so every event carries what its id is computed from. JCS accepts integers only, with |n| < 2⁵³.
- **Extra fields.** Events MAY carry fields beyond the table (the reference adds, for example, `linked.kind`, `linked.tree_hash`, `objected.veto`, `claim.lost.why`, `host.fork.why`). Receivers ignore fields they do not know.
- **When.** `push.received` is emitted for pushes that pass the signature check on their named parent (and for orphans); other pushes get `push.rejected`. `objected`, `approved` and `held` are emitted when the vote is valid at the tip. Events of one replay step are ordered by `(at, the type order of this table, id)`.

### 19.3 Dispatch channels

The host runs the dispatcher. Each member has one delivery preference held by the host (not chain state), registered with a signed mirror request (section 21):

1. **Agent webhook.** `POST <url>` with the event JSON as the body and headers:
   - `X-Gud-Event-Id: <event id>`
   - `X-Gud-Cursor: <journal seq>`
   - `X-Gud-Time: <unix seconds>` (an integer)
   - `X-Gud-Sig: ed25519:<base64>` over the bytes `"gud-hook/1\n" + X-Gud-Time + "\n" + body`, signed with the host key.
   The body is the RFC 8785 JSON of section 19.1. The receiver verifies with the host key (11.6), rejects `|now − X-Gud-Time| > 300`, and dedupes by event id. Any 2xx is delivery and moves the member's acknowledged journal seq; nothing else does. The host posts one item per request, in journal order per member, and a failure holds back that member's later items until the retry, so journal order is kept. It retries with backoff 1, 2, 4, … seconds capped at 10 minutes, for 24 hours of continuous failure, then sends everything pending through channel 3 and acknowledges it. The acknowledged seq and retry state survive a host restart.
2. **SSE stream.** `GET /api/v1/events?member=<handle>` with mirror auth headers (section 21). Each SSE message has `id: <journal seq>`, `event: <type>`, `data: <event JSON>`. A client resumes with `Last-Event-ID` or `?after=<journal seq>`; the resume point is the larger of the two. A member may read only its own stream (a loopback caller may read any). The stream starts with a comment, sends a keepalive comment every 15 s, ends after `?max=<s>` when given, and with `?follow=0` returns the backlog and closes.
3. **Email fallback.** Members without a registration get this channel. The host sends a `notify` envelope message, signed with the host member's key, From the host member's address and To the member's address only (never CC'd to the repo), batching events for that member over at most 60 seconds from the first pending one (the reference batches 30 s), `Gud-Event` = the first event's id, the rest listed in `text`. For human members, the types of 19.4's human notifications are left out of this batch.

### 19.4 Delivery guarantees

- At least once. The host keeps a per-member **journal** (monotonic `journal seq`, starting at 1, without gaps) of every event concerning the member. An event id already in a member's journal is never appended again, so re-feeding replay after a restart, duplicate webhooks or a second publish add nothing. Events of one replay step are journaled in the order of 19.2. Delivery skips the host itself and any handle that is not a member at the tip. Agents dedupe by event id and resume from their last journal seq.
- Events for one push thread are delivered in journal order. A receiver SHOULD debounce bursts for about 1 second before acting.
- **Human notifications** (`task.ready` with `needs = accept`, `task.done`, `escalated`) go as one `notify` each To every concerned human member and Cc the repo address, so they are logged and dedupe across dispatchers: before sending, a dispatcher checks the log for a `notify` with the same `Gud-Event` (and skips events it has already sent). Replies to these messages resolve their target through the notify (8.2), so `Gud-Target` is chosen to make a plain reply do the right thing: for task and claim events, the task (so `accept` is requester acceptance); for push events, the push; for an escalation over one push, that push; for an escalation over several pushes, no target, so a bare `approve` cannot pick one by accident.

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
- If a provider exposes authentication results only as structured data, the adapter prepends one synthesized header `Authentication-Results: <adapter authserv-id>; dmarc=<result> header.from=<domain>` to the raw bytes, in the message's own line ending, before anything hashes them. Because it is topmost, a header with the same authserv-id forged inside the message is never the one 8.1 (2) reads. The adapter's authserv-id is fixed and documented, and is what the repo pins: `fake.gud.invalid` (fake transports), `primitive.gud.invalid` (Primitive), `ses.gud.invalid` (SES). Adapters that read a real header report the provider's own id (Gmail: `mx.google.com`); Postmark exposes no verdict at all (`docs/TRANSPORTS.md`).

**Cursors.** A cursor is an opaque string; none means the start of the mailbox. `list(since=c, limit)` returns messages delivered after `c`, oldest first, and the cursor of the last returned message (or `c` unchanged when nothing is new). Every message's own cursor is the position just after it, so a consumer may stop part-way through a batch. Cursors survive process restarts. Delivery is at least once: after a reconnect or a provider-side reset a message may come again; consumers dedupe by gud id (section 4). `subscribe` without a cursor delivers mail that arrives after the call (the adapter takes its head cursor before returning); with one, it first catches up from it. The callback may see one message twice. To avoid a gap, an intake subscribes first and then lists from its stored cursor. Items parsed from a provider webhook body carry no cursor; intakes persist only cursors from `list`, `wait` and `subscribe`, and run `list` catch-up on start.

### 20.2 Conformance (`tests/v1/transport/test_conformance.py`, one parameter per transport)

An adapter conforms when, against a live or containerised account:

1. **Byte-exactness**: an envelope of random bytes (including NUL, CR, lone LF, 998+ byte lines, non-UTF-8) round-trips with an identical gud id.
2. **Dedupe**: the same message delivered twice yields one gud id; the intake logs it once.
3. **Ordering**: delivery times are returned and non-decreasing in list order for mail sent in sequence ≥ 1 s apart.
4. **Wake latency**: `subscribe` fires within 5 s (API providers) or 30 s (IMAP IDLE, Gmail) of delivery; `wait` returns within the same bound.
5. **Large blobs**: a 25 MB file round-trips through the blob path within the adapter's `max_message_bytes`.
6. **Spoofed human**: an unsigned reply with a forged `From` of a human member fails 8.1.
7. **Resume**: after a forced disconnect, `list(cursor)` returns every message missed, once.
8. **Quota reporting**: `capabilities()` values match the provider's documented limits.

The checks are in `tests/v1/transport/conformance.py`; select a transport with `-k <name>`. Live runs are opt-in by environment variable (`GUD_LIVE_PRIMITIVE`, `GUD_LIVE_JMAP`, `GUD_LIVE_SES`, `GUD_LIVE_POSTMARK`, `GUD_LIVE_GMAIL`); `docs/TRANSPORTS.md` says which adapters have passed live and which only against mocks.

`gud host init --transport X` checks DNS (SPF, DKIM, DMARC), creates or validates the repo address, warns about spam filtering, sends a round trip, records the authserv-id, measures delivery latency and proposes `window` and `grace`. `gud doctor` re-runs the probe.

## 21. Hosting, mirror and auth

- **Mirror endpoints** (all JSON unless noted):
  - `GET /api/v1/info` (open): `{repo, bugs_address, host_key, log_head: {seq, hash, time}, max_message_bytes, transport}`.
  - `GET /api/v1/log?since=&limit=`: log entries (11.2). `since` is inclusive: `since=n` starts at seq `n`.
  - `GET /api/v1/mail/<gud id>`: raw MIME (`message/rfc822`) of logged mail only. `GET /api/v1/mail?since=<seq>&limit=`: `{"mail": [{"seq", "id", "raw": base64}], "next": seq|null}`, the mail entries with seq ≥ `since`; `next` is the seq to ask for next.
  - Both lists take `wait=<s>` (at most 60) to long-poll until an entry exists.
  - `GET /api/v1/object/<sha256>`: any object in the host's content-addressed store (blob content, and the tree and blame nodes a checkpoint clone fetches on demand). `GET /api/v1/snapshot/<sha256>`: snapshot bytes the host stored, and the snapshot named by any logged `checkpoint`, computed on request by replaying entries `≤ n` with `now = time(n)` and served only when it hashes to the name.
  - `GET /api/v1/events`: SSE (19.3).
  - `POST /api/v1/delivery`: body `{"webhook": url}` or `{"sse": true}` or `{"email": true}` sets the caller's delivery preference. The caller must be a member at the tip; a webhook URL must be `http(s)`.
  - `GET /api/v1/bugs`: bug reports.
  - `POST /api/v1/webhook/<provider>`: provider webhooks. Exempt from member auth; they carry the provider's own signature, checked by that transport's webhook verifier (an unknown provider gets 404, a bad signature 401, an unparseable body 400).
  - `/api/v1/admin/*` (for example `POST /api/v1/admin/handover`, body `{"key", "repo"?}`) and the dashboard's actions (Appendix A.6): loopback only.
- **Mirror auth v1.** Every request except `/api/v1/info` from outside loopback carries `X-Gud-Handle`, `X-Gud-Time` and `X-Gud-Sig = ed25519:<b64>` over `"{METHOD} {path_with_query} {time} {sha256(body)}"` (the hash of the empty string for no body; otherwise of the exact request body). `X-Gud-Time` is decimal unix seconds; clients send whole seconds, and the canonical text of a fractional time has at most 6 fractional digits with trailing zeros removed (`gud.events.dispatch.format_time`). The signed `path_with_query` is the path the server sees, including any path prefix of the host URL, and the query exactly as sent. The host accepts it when the handle is a member at the tip whose key verifies it and `|now − time| ≤ 300`. A member without a key cannot sign. A removed member loses access when the removal links. Requests from loopback are trusted and may name the member in `X-Gud-Handle`.
- **Before genesis** there are no members to verify against, so the host serves every read endpoint without a signature until genesis links (there is nothing to read but the `open` entry and unsigned mail). The genesis author needs this to read the log head it cites in `Gud-Seen`. `POST /api/v1/delivery` still needs a member.
- **Rate limit.** A host MAY limit mail from addresses that are not member addresses at the tip (the reference: 120 per sender per rolling minute). Members are never limited. A limited message is not logged; a later catch-up may offer it again.
- The host is trusted only for availability, for received times within the grace rule, and for `Authentication-Results` on unsigned mail. Everything else is verified by members.
- **Local host mode.** A host need not be a server. With `host_url = local:<dir>` a member's client is the host: on each sync it lists the repo mailbox from a stored cursor and logs exactly what 11.1 says, judging unsigned mail with its own replay. Its clock reading is `max(clock, head time + 1)`, so a burst within one millisecond can lead the wall clock by a few milliseconds rather than fail. When nothing was logged and the head is at least 1 s old, a sync appends a `tick`, so replay time follows the clock whenever a member syncs. Several members on one machine may share the directory (an `flock` serialises intake). The same directory can later be served (`gud host serve`); while it is served, local members only read it (Appendix A.5).
- **Join.** `gud join <host_url> --handle @x --address x@domain [--role agent] [--sponsor @h]` creates a key locally and sends a signed `join`. Any member answers with a membership push adding the line (`Gud-Join` cites the request; `gud admit`), which needs the members rule. A `join` whose self-signature fails is ignored. When it links, the new member clones (from a checkpoint if one exists).
- **Leaving** is a members push removing the line. The `host` line cannot be removed, nor the last human (no members change could be approved after that).
- **Key rotation** is a members push that edits only the member's `public_key` field, keeps every other line of `.gud/members` byte for byte, and is signed with the old key. The members rule needs one human approve, and a human author's own push satisfies it (13.3). **Recovery** of a lost key is the same push approved by the sponsor (or by a human, for a member without one). Replay accepts any human approve; the client enforces "the sponsor" (`gud.ops.keys.sponsor_approve` refuses another approver, and a push whose `.gud/members` change is anything but that key). A sponsor who authors the recovery push links it without a separate approve. Because 12.3 step 2 re-verifies every candidate with the author's key at `Y`, a push signed with a rotated-out key never links once the rotation has, so a rotating member re-sends its pending pushes with the new key.

## 22. Working copy

- A working copy is an ordinary directory. Its metadata lives in `.gud/BASE` (one line: the base link's state, 64 hex, LF) and `.gud/local/` (index cache, handle, transport settings, push and sync records). Neither is ever in a tree (5.1). One working copy per agent.
- **Working tree.** Files are read byte-exactly; the user executable bit gives `100755`; symlinks give `120000` with the raw target bytes. FIFOs, sockets, devices, empty symlinks and paths that 5.1 rejects (for example non-UTF-8 names) are skipped with a warning. A tracked path that `.gudignore` matches keeps its base entry: a push never changes or deletes it. Paths that differ only in case are not handled on case-insensitive filesystems.
- **Materialise** writes the tree byte-exactly with modes: files `0644`/`0755` exactly (independent of umask), symlinks as symlinks, every parent as a real directory (a symlink or file in the way is replaced). Nothing is written through a symlink or outside the directory: each file is written to a temporary name with `O_NOFOLLOW | O_EXCL` and renamed into place, after checking its blob's hash.
- **`.gudignore`** (a tree file, gitignore syntax). Only the root `.gudignore` is read, from the working copy when present, else from the base tree. Semantics follow gitignore: `#` comments, `!` negation, last match wins, a trailing `/` matches directories only, a pattern with an inner or leading `/` is anchored at the root, `**/`, `/**` and `/**/` as in git, `*`, `?`, `[...]`, and nothing under an excluded directory can be re-included. Ignored paths are never pushed. `.git` (as any path segment, in any case), `.gud/BASE` and `.gud/local` (itself and below) are always ignored. When neither the working copy nor the base tree has a `.gudignore`, the default list applies (`__pycache__/`, `*.pyc`, `*.pyo`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.tox/`, `.venv/`, `*.egg-info/`, `.coverage`, `node_modules/`, `.DS_Store`; `gud.wc.ignore.DEFAULT_GUDIGNORE`), and `gud init` writes it as the repo's `.gudignore` when the directory has none.
- **Status/diff**: compare the working copy (minus ignored paths) with the base tree; `diff` renders a git-style unified diff.
- **Push**: operations = `diff_trees(base tree, working tree)` (5.3 canonical), `Gud-Parent` = `.gud/BASE`, even when the tip has moved, so carry-forward (section 14) applies. Before signing, the client refuses, and sends nothing:
  - a push that could only be superseded: a link after the base touched a path the push touches, so it is not carryable (`carry file`, read at the tip) or would not merge (`carry line`, checked with `three_way` on those paths). It says to run `gud sync`. `--no-carry` while behind is refused the same way;
  - a push whose tree has an unparseable config file or breaks 5.1;
  - a push that adds a path matching the build-output list (`gud.wc.ignore.ARTIFACT_PATTERNS`, the default list above) under a `.gudignore` that forgets them, unless `--allow-artifacts`;
  - a push that fails the **secret scan**. It runs over the bytes of every path added or changed relative to the base (file contents and symlink targets; deleted paths are not scanned) and matches: a PEM private key header (`-----BEGIN` … `PRIVATE KEY-----`), an `ed25519:` gud private key file (a file whose whole content, apart from surrounding whitespace, is `ed25519:` and the base64 of 32 bytes; a public key alone in a file has the same form and is reported too, but keys inside `.gud/members` lines are not), `AKIA[0-9A-Z]{16}`, `ghp_[A-Za-z0-9]{36}`, `github_pat_`, `xox[baprs]-`, `sk-[A-Za-z0-9_-]{20,}`, `sk-ant-`, or `-----BEGIN OPENSSH PRIVATE KEY-----`. `--allow-secret <path>` skips one path. Every member receives every diff, so a pushed secret is leaked for good.
- **Base fast-forward.** `.gud/BASE` moves to the tip without writing any file when the working tree already holds everything the links since the base changed (`three_way(base, working, tip)` equals the working tree), or when the tip's tree is exactly a tree this working copy pushed (recorded in `.gud/local/pushes.jsonl`). A push does this before computing its operations and again once it links, so a member's next push is computed against the link holding their previous one.
- **Sync**: three-way merge (`three_way`, 14.1) with mine = working tree, theirs = tip tree, and, per path the working tree changed, base = that path's entry at the latest link after `.gud/BASE` where it already equals the working tree (normally the member's own linked push), else the base's. Fast-forward uses the same base. On any conflict nothing is written (working files and `.gud/BASE` are untouched); the conflicts are recorded in `.gud/local/sync.json` (shown by `gud status` until a sync succeeds) and, for every path where base, mine and tip are all text, a diff3-style merge with `<<<<<<< mine` / `||||||| base` / `=======` / `>>>>>>> tip <id12>` markers is written to `.gud/local/conflicts/<path>`. On success the working copy becomes the merge and `.gud/BASE` the tip. Sync never auto-resolves.
- **Verify**: sync, then replay the mirror's log (up to the cache's length, so both see the same inputs) and mail into a fresh store, from the newest valid checkpoint the cache knows or, with `--from-genesis`, from genesis. Report differences in links (field by field), proposals, tasks, claims, escalations, red state, processed log head and, when those agree, the snapshot hash; then check that the cached tip's and base's trees hash to their links' tree hashes and every blob's bytes match its hash.
- The replay cache (`GUD_HOME/cache/<repo>/`) and object store (`GUD_HOME/objects/`) are disposable and rebuildable.

## 23. Archives and host migration

- **Archive format** (`gud-archive/1`). A directory, or a `.tar` with the same members: `manifest.json`, `log.txt` (every entry's exact section 11.1 text, concatenated in seq order), `mail.mbox` and `snapshot.json` (the section 17 snapshot at the log head). The mbox is mboxrd: per message a `From_` line, the raw MIME exactly as the host stored it with every line matching `>*From ` given one more `>`, an LF if the message had no final LF, and a blank line. The manifest records `{seq, id, sha256, size, time}` per message, so a reader knows whether that LF was added. Each logged id is archived once, at its first seq. Exports are deterministic (no timestamps). The exporter MAY sign the manifest (`signer`, `sig` = Ed25519 over the JCS of the manifest without `sig`); a verifier accepts the signature only from a key in the archived log's key history, or one it trusts.
- **Verifying an archive.** An envelope message's gud id does not cover its MIME wrapper, so a message matches when its bytes hash to the manifest's `sha256` and give the logged id. Verification checks member hashes; the log (11.4, including handovers; an optional anchor key must be one of the log's keys); every `mail` entry's message present, matching its recorded hash and its logged id; the mbox in log order; the manifest signature; and that replaying the archive at its log head (`now = time(head)`) reproduces the archived snapshot and tip. A message the exporter could not produce is listed as missing, and the archive then fails as incomplete.
- **Importing.** The importer names the address the continued log will have (the archived log's, or the new one its `handover` names) and is refused any other. Three cases: the archived log ends in a `handover` naming the new key (planned: the new host appends `open` citing it); the log's current key is already the new host's (a restore: nothing is appended); or neither (unplanned: allowed only with an explicit local trust decision, and `open` cites the last entry, 11.6).
- **Mail on import.** Archived mail is already logged. The new host loads its mail store from the archive instead of re-ingesting it, because a provider that adds headers would change unsigned ids. Re-delivering it to a mailbox is a byte-exact backup copy only (raw injection, where an adapter supports it). The old host logs nothing after its `handover`; mail that reaches it after the export is not in the continued log, and its senders resend it (the same envelope gives the same id, 3.3).
- **Audit.** `gud audit` compares what the host serves now with what the member holds. *Retained* entries are the member's own verified log plus any entries other members hand over. At the first seq where a retained entry differs from the served one, it tries to build a fork proof (11.5), checking each entry under the key current at that seq in its own log. A difference the signatures do not prove, or a served log shorter than what the member verified, fails the log check. Gud-Seen is checked for every signed kind except `join`, over served mail and the member's inbox copies addressed (To or Cc) to the repo; a cited seq the served log lacks is a mismatch with log hash `ZERO`. Inbox copies match by gud id, then by Message-ID (an unsigned copy whose text equals the served message is the same mail). A copy is *retimed* when `|log time − own received time| > S`, and *withheld* when the log lacks it and the log head is more than `S` past its received time. *Late votes* (`approve`/`object`/`hold` logged more than `G` after `Gud-Sent`) are reported but are not a failure (13.4 already discounts them). Exit status: 0 clean; 1 Gud-Seen mismatches or missing, altered, retimed or withheld mail; 2 a fork proof, or a served log that fails verification.

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

## Appendix A. Reference implementation notes (non-normative)

Choices the reference implementation makes where sections 0–24 leave room. None of them changes what replay accepts; a second implementation may choose differently and still interoperate. Golden vectors for the core are in `tests/v1/vectors/core.json`; scenarios for each part are in the `tests/v1/` directory named below.

### A.1 Transport adapters

Per-adapter configuration, capabilities and verification status are in `docs/TRANSPORTS.md`. The JMAP, SES, Postmark and Gmail adapters are verified against local mocks of the providers' public APIs only, not against live accounts. Scenarios: `tests/v1/transport/`.

1. **Fake receiving server.** `FakeMailbox.send` and `NetworkMailbox.send` model authenticated submission: they prepend `fake.gud.invalid; dmarc=pass` only when `From` is the sending mailbox's own address, and `dmarc=fail` otherwise. `deliver()` injects raw bytes from an outside MTA and adds nothing. `NetworkMailbox` numbers generated Message-IDs across the whole network, so two mailboxes never generate the same Message-ID in one millisecond.
2. **Structured-send providers.** A provider that builds MIME from structured fields (Primitive, Postmark) renders a raw-bytes `send` from its parsed fields: the envelope bytes, text, subject, addresses and threading headers are kept; other headers, the Message-ID and the MIME layout are the provider's. Primitive takes one `to`, so further To addresses are sent as Cc (replay never reads To or Cc). `Sent.message_id` reports the provider's rewritten Message-ID when it is predictable (Primitive: `<primitive-direct-<sent id>@<from domain>>`).
3. **Primitive DMARC.** Primitive evaluates DMARC on the organisational domain (`dmarcFromDomain`, e.g. `primitive.email` for `x.primitive.email`). The adapter writes `header.from` as the actual From domain, and keeps a `pass` only when that domain equals `dmarcFromDomain` or is a subdomain of it; otherwise it writes `fail`.
4. **IMAP.** Folders are read in their configured order (default `INBOX`, `Junk`). `Junk` (or `Spam`) falls back to the folder LIST marks `\Junk`, and a configured folder that does not exist is skipped. `list` merges folders oldest-first by INTERNALDATE while keeping UID order within each folder, so every folder's part of the cursor only moves forward. A UIDVALIDITY change rescans that folder from UID 1. `max_message_bytes` is the configured value, else the SMTP `SIZE` extension, else 10 MiB.
5. **Webhook-fed mailboxes.** SES and Postmark only push. Their adapters store what the webhook delivers in a local `Spool` and serve `list`/`wait`/`subscribe` from it; a periodic `catch_up()` fetches what the webhook missed from the provider's store (S3; Postmark's message search). The cursor is the spool sequence number. A provider id is stored once and the first copy wins, so stored bytes, and with them an unsigned message's gud id (section 3.3), never change even when a webhook copy and a catch-up copy differ (catch-up copies carry `dmarc=none`). List order is intake order. A provider that pushes out of order can therefore give a later row an earlier `received_at`; the host's clamping (section 11.1) absorbs this.
6. **Delivery time.** JMAP `receivedAt` (servers commonly give whole seconds; ties keep the server's order). Gmail `internalDate`. SES `receipt.timestamp`, or the S3 object's `LastModified` for catch-up. Postmark's inbound webhook has no timestamp, so it is the local arrival time, and the search result's `ReceivedAt` for catch-up.
7. **SES Authentication-Results.** SES reports DMARC as structured data (`receipt.dmarcVerdict.status`), so the adapter synthesizes `ses.gud.invalid; dmarc=<pass|fail|none|temperror> header.from=<From domain>` (PASS, FAIL, GRAY, PROCESSING_FAILED) and the repo pins `ses.gud.invalid`. An `amazonses.com` header that SES may write into the stored message is not relied on: it was not verified live, and a sender can forge one with that id lower down. Copies found by S3 catch-up have no verdict and get `dmarc=none`, so a human's unsigned reply that the webhook missed does not count. That is safe, but it means the webhook must be reliable for unsigned human mail.
8. **Postmark Authentication-Results.** Postmark exposes no DMARC verdict for inbound mail and adds no header of its own, so `auth_results` is `none`. On a Postmark repo no unsigned human message passes section 8.1 (2), and humans sign (section 8.3). The `authserv_id` option is for a forwarding MTA the operator controls that writes the topmost header. Pinning any other id would let a sender's forged header count.
9. **SNS trust.** The signing certificate is fetched only from `https://sns.<topic region>.amazonaws.com/…pem` (no userinfo, no port) over verified TLS. It must name `sns.amazonaws.com`, be valid now, and is pinned per URL. The `TopicArn` must be configured, because AWS signs every account's topics. SignatureVersion 1 and 2 are accepted. There is no freshness window, since SNS retries can lag by hours; a replayed notification is deduped by `mail.messageId`. A `SubscriptionConfirmation` is confirmed only after its signature verifies and its `SubscribeURL` passes the same host rule.
10. **Postmark webhook trust.** Postmark does not sign webhooks. `verify` requires HTTP Basic credentials, the documented source IPs, or both, and refuses everything when neither is configured. The source IP is passed as `remote_addr=` or the `X-Gud-Remote-Addr` header, which the webhook server sets from the socket peer. A replay with valid credentials is deduped by `MessageID`.
11. **Gmail push.** A Pub/Sub push carries only `{emailAddress, historyId}`, so `parse` returns `[]` and `accept_webhook` returns the notification and wakes every subscription and `wait`, which then list from their cursors. The OIDC token is verified (RS256 via Google's JWKS, issuer, audience, service-account email, expiry ±60 s). Without a public URL the subscription polls `history.list` every `poll_interval` seconds, and `capabilities().push` is `none` unless a topic is configured. This is polling outside catch-up, which section 20.1 discourages; it stays within the section 20.2 (4) 30 s Gmail bound.
12. **Message-ID.** JMAP submits the raw bytes and keeps the Message-ID. SES rewrites it to `<MessageId@email.amazonses.com>` (`<region>.amazonses.com` outside us-east-1). Postmark writes `<MessageID@mtasv.net>`. Gmail sets its own, which the adapter reads back after sending (`format=metadata`). Human replies to these messages resolve through the rewritten id or the subject tag (section 8.2).
13. **Cursors.** JMAP: `<c|q>|<state>|<receivedAt>|<ids at receivedAt>|<partial>`, where `c` is `Email/changes` from the state and `q` is an `Email/query` walk by `receivedAt` (used for no cursor and after `cannotCalculateChanges`). Gmail: `h:<historyId>:<skip ids>:<last internalDate>` or `s:<base historyId>:<internalDate>:<ids>`; a history 404 falls back to a walk. Both fallbacks are at least once (section 20.1).
14. **What counts as delivered.** JMAP skips Emails whose only mailboxes have role `sent` or `drafts` (the adapter's own `Email/import` copies) and reads Junk. Gmail skips drafts and messages labelled SENT without INBOX or SPAM, and reads Spam. JMAP and Gmail apply no recipient filter (the account is the repo's). SES and Postmark keep only notifications whose recipients include the address (Postmark: or a configured alias), and SES catch-up takes every object under the repo's own S3 prefix.
15. **Sizes.** JMAP: min(`maxSizeUpload`, ⌊`maxSizeAttachmentsPerEmail` × 78/57⌋ + 65 536), the MIME size whose envelope budget equals the attachment limit. SES: 40 000 000 with an S3 action, 150 000 when only an SNS action is configured (too small to host a repo: the conformance minimum is 1 MiB). Postmark: 10 000 000. Gmail: 25 000 000, with sends over 4 MB using media upload. Fake and IMAP default to 10 MiB. Each adapter refuses an oversize message before calling the provider.
16. **Quota.** SES reads `GetAccount` `SendQuota.Max24HourSend` (−1 means unlimited, reported as `None`). Gmail reports the documented 500 a day for consumer and 2 000 for Workspace accounts, because the API exposes no remaining quota. Postmark and JMAP report a configured value or `None`. Every adapter raises a `QuotaExceeded` subclass for exhausted limits (HTTP 429 after retries; JMAP `overQuota`/`rateLimit`; SES throttling and `SendingPausedException`; Postmark ErrorCode 405; Gmail rate- and daily-limit reasons, where a 403 is not retried).
17. **Conformance harnesses.** The Postmark harness has no positive control for section 20.2 (6), because nothing verifies there (item 8). The SES, Postmark and Gmail intakes hold no connection a test could drop, so `subscribe_reconnect` runs for JMAP (and IMAP) only. The SES and Postmark live harnesses read by catch-up because a test machine has no public webhook URL.

### A.2 Git interop (`gud/gitbridge.py`)

Scenarios: `tests/v1/gitbridge/`.

1. **Import source.** `gud import` (or `gud init --from-git`) reads one commit: `<ref>^{commit}` (tags are peeled). History is not imported. The genesis intent (`text` record) is `gud import: git <ref> at commit <full commit id>`. Contents are read with `git cat-file --batch`, so no filters (CRLF, `text`, `filter` attributes) apply: bytes are the blob bytes.
2. **What import skips.** Submodules (mode `160000`), paths gud cannot hold under section 5.1 (not UTF-8, a `.git` segment in any case, `.gud/BASE`, `.gud/local` and below) and symlinks with an empty or NUL target are skipped, each with a warning in `ImportPlan.warnings`. The legacy git mode `100664` is imported as `100644` with a warning. Nothing else is changed.
3. **Config files at import.** `.gud/members` always comes from the roster; a copy in the git tree is replaced (with a warning). `.gud/config`, `.gud/test` and `.gud/owners` are kept from the git tree when present, else written as defaults: `.gud/config` spells out every section 10.5 default except `authserv-id`; `.gud/test` and `.gud/owners` are comments only (so the section 10.4 defaults and no declared owners apply). The whole tree must pass section 10 (`rules_of`) or the import fails.
4. **Roster format.** One member per line, `handle role address [pubkey] [sponsor]`, `#` comments. With four fields the fourth is the sponsor when it starts with `@`, otherwise the key. A missing key is `-`. The `host` line takes the host key passed to `import_git` when it has none and must equal it otherwise. The result must parse under section 10.2, so agents, testers and the console need keys and there is exactly one host. `gud init` uses the same format: the initialising member's line gets their key when it has none, and the host line gets the host log's key.
5. **Genesis push.** `text` (the intent) then `diff_trees(empty, tree)`: one `put` per file in path order. `inline_budget` bounds inline file content only; files that do not fit become blob references and their raw contents are returned for the transport to send as `blob` messages first (section 6). The signer must be in the imported `.gud/members` with a push role and the matching key.
6. **Commit shape.** Tree = the link's tree byte for byte (modes `100644`/`100755`/`120000`, directories as git trees). Author = the link author's handle as name and their member address at the link (else at the previous link, else `<name>@gud.invalid`), dated `received_at`; committer = the mirror identity (default `gud mirror <mirror@gud.invalid>`), dated `linked_at`; both in whole seconds at `+0000`. Mirror commits are therefore deterministic: the same links and identity give the same commit ids on any machine.
7. **Commit message.** The intent with trailing whitespace removed and NUL written as `\0` (git refuses NUL), or `gud link <id12>` when empty; a blank line; then exactly `Gud-Link: <link state>` and `Gud-Tree: <tree hash>` as the last two lines. `Gud-Link` is the plan's `Gud-State` trailer under its section 4 reference name; for a carried link it is the carried state.
8. **Progress and resume.** The remote is the only progress record: the head commit of the mirror branch (default `gud`) names the last mirrored link in its trailers. A mirror resumes after that link. It stops with `MirrorDivergence`, writing nothing, when the head has no such trailers, names a link that is not in the chain, or names a tree hash different from that link's. Links are pushed one at a time with a plain (non-force) push, so a CI watching the branch sees every link; a rejected push is divergence unless the remote head is now the very commit being pushed (another mirror got there first).
9. **Input.** Links are given in chain order and must be contiguous (`parent` = the previous link's state); a mirror into an empty branch starts at genesis.

### A.3 Tester (`gud/tester.py`, `gud/runners/`)

These fix what one tester sends. Scenarios: `tests/v1/tester/`.

1. **Running the command.** The `command` line runs as `/bin/sh -c '<command> <args>'` with the materialised tree as working directory, in its own process group. Materialisation is byte-exact (section 22): modes `100644`/`100755`, symlinks created as symlinks and never followed, no newline rewriting. Test paths (`Gud-Accept` matches, the evidence path) are appended shell-quoted. With an `image`, the Docker runner runs the same line in `docker run --rm --network none -v <tree>:/work -w /work <image>`; the local runner ignores the image and says so in the log. The whole process group is killed at `timeout`, and also after a normal exit, so nothing the test started outlives the run.
2. **Honest results.** A timeout's `text` record says `timeout after <t>s`. An InstaBox `exec` that hits its 600 s cap before a longer policy timeout is not a result (section 15.2): nothing is signed. The `text` record is a header line, the runner and duration, and the output tail, at most 65 536 bytes.
3. **One result per key.** A tester never signs two results for one `(Gud-Scope, Gud-Target, Gud-Tree)`; for `accept` the key also includes `Gud-Link`, because done-ness depends on the link's seq (section 16.6). The signed envelope is persisted before it is handed to the transport; after a crash it is re-sent byte-for-byte (same gud id, section 3.3). Own results found in the log restore the memory when the state directory is lost. Runs are cached by `(tree hash, command, image, timeout, arguments)`, so one tree is run once across scopes (a passing stack's tree is usually the next tip).
4. **Which candidates.** The tester tests candidates of the tip whose evaluation says a test is needed and that have no vouching pass (`ProposalState.test_needed` and no `test_result`), including parked ones, and never pushes whose blobs are incomplete. Without `every-link`, each gets a `push`-scope test on its own (possibly carried) tree.
5. **Stack composition.** With `every-link on`, the stack is the tip's live candidates in race order (received time), parked candidates after the others, up to `stack-max`. Candidates that already have a vouching pass stay in the stack so it follows the order links will actually happen in; the stack is cut after the last candidate that still needs one. A candidate that is not carryable onto the stack so far ends it. A candidate whose file-mode overlay differs from its candidate tree (a line-mode carry) also ends it; if it is first, it gets a `push`-scope test instead. A stack whose `(base, tree)` was already answered is not re-run: its later members wait for the earlier ones to link or park.
6. **Stack bisect.** On a failing stack of `k`, the tester binary-searches prefixes `P1..Pm` on the same base (the base itself is assumed to pass) and signs every run. The smallest failing prefix `m` and the passing prefix `m−1` result: if `m = 1`, the failing stack of one kills `P1`; if `m > 1`, the pass vouches `P1..P(m−1)` and `Pm` is killed only after they link, when it heads the next stack on the new base and fails alone there.
7. **Tip cadence (deviation 18).** The reference point is the latest link with a tip verdict on the chain (any tester; its log time) or this tester's last tip run (its own clock), and before any, `(seq −1, genesis linked_at)`. The tip is due when it has no verdict and `tip.seq − ref.seq ≥ links`, or a link has landed since `ref` and `now − ref.time ≥ time`. `time` runs on the tester's clock. While red with `first_bad` known, every new tip is tested, so red ends at the first passing link (section 15.4).
8. **Bisect.** While red with `first_bad` unknown: `lo` is `last_green` (or "before genesis" when no link ever passed) and `hi` is the lowest link above `lo` with a known `fail` verdict; the tester binary-searches between them with `tip`-scope runs, one signed result per probe. A link is tested with its own `.gud/test`, or the tip's when it has no `command`. Replay then derives `first_bad` and re-cites the fix task (section 15.5).
9. **Auto-revert.** When the tester's clock passes `red_since + auto-revert` with `first_bad` known and not genesis, it sends a `revert` of `first_bad` onto the tip (operations from `invert_ops`; on a conflict, nothing). At most one per `first_bad`, unless that revert has died.
10. **Acceptance.** For a task or subtask with status `work_done` and criteria, the tester runs the criteria on the tip and signs `accept` with `Gud-Link` = the tip. `Gud-Accept` globs are matched against the tip tree (section 10.1). Each must match at least one file, otherwise the result is `fail`. The matched paths, deduplicated, go as arguments to one run of the `command`. Each `accept` line then runs as its own shell command in the same environment, and all must exit 0. With no `command` the result is `fail` ("cannot pass", section 10.4) without running anything. Nothing runs while the chain is red, or after this tester's own run on that tip failed (a pass received while red does not count). A failing item is re-run on each later tip.
11. **Evidence.** An evidence objection that is not void and has no evidence test on any candidate of the push gets one on the push's current candidate. The tester runs the `.gud/test` command at the candidate's parent with the evidence path as argument. That run uses the candidate tree with the file added at mode `100644` (or the tree as is when it already holds those exact bytes, section 13.5), which gives `Gud-Result`. When the candidate belongs to the objected push, the same run on its parent's tree with the file added gives `Gud-Base-Result`. Later pushes in the negotiation that inherit a confirmed objection are re-run the same way, once each, without `Gud-Base-Result`. The evidence bytes come from the objection's `evidence` record, or from the completed blob. A file that cannot be added (its path collides with a directory or a file's parent) gets no result.
12. **Which testers count for the tester's own memory.** When scanning the log, a tester believes `test` messages signed by itself or by a member who is a `tester` at the current tip. Replay remains the authority on what counts: this only steers what the tester runs next.
13. **Process.** `gud tester [--runner local|docker|instabox] [--parallel N] [--interval S] [--once]` runs `gud.tester.Tester` over the member's `RepoClient` (mirror or local host mode), signing as the member and sending through its own transport, with state in `GUD_HOME/tester/<handle>/`. `python -m gud.tester` takes the same member from GUD_HOME. A failing step (mirror unreachable, transport error) is logged and retried on the next pass instead of ending the tester.

### A.4 Host internals (`gud/events/`, `gud/host.py`)

Scenarios: `tests/v1/events/`, `tests/v1/host/`.

1. **Missing mail.** On start the host feeds its own log to the replay cache. Ids whose raw mail is not in the mail store are re-read from the mailbox with a full `list()` scan (`Intake.repair`). Anything still missing is reported in `Intake.missing`, and replay stays just before the first such entry (section 11.3).
2. **Intake paths.** Push intake (`subscribe`), provider webhooks and catch-up all end in one `on_received` under one lock, deduped by gud id against the log. Only `list()` cursors are persisted. Push intake is the fast path; catch-up runs on start and every 30 s as a safety net.
3. **Journal files.** Each member has one JSONL journal file under the host directory. Concerns are journaled as replay names them.
4. **Volume.** The dispatcher posts at most `max_per_pass` (64) webhook items per member per pass and runs the next pass at once while a backlog remains, so one member's backlog never delays the others.
5. **Snapshots.** The host indexes every logged `checkpoint` by `Gud-Snapshot` and computes the snapshot it names on request (`gud.core.replay.snapshot_at`, shared with the local host and members). `Replayer.chain()` on a replayer just made by `from_checkpoint` reports the snapshot's head as the processed head until the log is fed past it; `save`/`load` keep it.

### A.5 Command line, host directory and member state (`gud/cli.py`, `gud/wc/`, `gud/ops/`)

Scenarios: `tests/v1/wc/` (against the real host, `gud.host`), `tests/v1/cli/` (the `gud` command line as subprocesses), `tests/v1/ops/`.

1. **Context and output.** A command finds its repo from the working copy's `.gud/local/config.json` (`handle`, `host_url`, `repo`), else `GUD_HOME/remote.json` (written by `init`, `clone`, `join`). The replay cache is `GUD_HOME/cache/<repo>/`, shared by members who share a GUD_HOME; use one GUD_HOME per member. With `--json` every command prints one JSON object (errors as `{"error": ...}`). Exit codes: 0 ok, 1 failed or found problems (refused push, sync conflict, verify differences, doctor failures, audit findings), 2 usage errors and unavailable commands.
2. **`gud init`.** Without `--from-git`, genesis is the directory's files (minus ignored paths) plus `.gud/members` and the gitbridge defaults (A.2) for any missing `.gud/config`, `.gud/test`, `.gud/owners`; the directory becomes a working copy at genesis. Without `--host-url`, a local host is created under `GUD_HOME/host/<repo>/` using the member's transport with the repo address.
3. **One host directory, two ways to run it.** `gud host init` (and `gud init` without `--host-url`) makes a host directory: `key`, `host.json`, `transport.toml`, `log.jsonl`, `mail/`. `gud host serve --dir D` runs the real host (`gud.host.serve_dir`: intake, dispatcher, the section 21 app) on that same directory, so a repo started in local host mode continues under the same log. Both hosts store mail as `mail/<2 hex>/<id>.eml` and read flat `mail/<id>.eml` too (what `import_archive` writes). While served, the directory's `serve.lock` is held and local-mode members only read it (`LocalHost.served()`); a second `serve` refuses to start. A host directory's `transport.toml` resolves `<key>_env` from the environment like a member's, so `gud host init --set api_key_env=PRIMITIVE_API_KEY` keeps the key off disk.
4. **Commands.** Beyond the core verbs: `gud keys new|show|rotate|promote` (`rotate` sends a members push with a new key kept pending; `promote` installs it once the tip lists it), `gud admit JOIN`, `gud task list|show`, `gud object --test FILE [--test-path P]` (evidence from the working copy; `--evidence P:FILE` also works), `gud comment --verdict reject`, `gud delivery --webhook URL | --sse | --email`, `gud claim update CLAIM --done` and `gud release CLAIM --done` (the section 16.6 done declaration; both refuse while the member has an open push for the item), `gud host key|handover|follow|serve`, `gud export-archive [--host]`, `gud import-archive`, `gud audit [--evidence FILE]`, and the global `-C DIR` and `--offline`. `gud agent ...` and `gud mcp ...` run `gud.agent.runtime.main` / `gud.mcp.main` with the global `--home` and `--handle` (and `--wc` inside a working copy, `--json` for mcp) before the command's own options; unknown options are passed through only for these two. `gud blame --full` reads the same mail as without it (archived epochs are not separate yet).
5. **Ids.** `RepoClient.resolve` accepts a full id, a hex prefix of 4 or more, a `T<n>`/`T<n>.<m>` alias, `tip`, `[gud:<hex>]` (anywhere in the text, e.g. a pasted subject), `gud:<hex>`, `<id>` and a logged message's `<Message-ID>` (`Replayer.by_message_id`).
6. **Fetching mail.** New log entries are fetched in pages of 500. Mail for new entries (and older gaps, `Replayer.wanted_mail`) comes from `GET /api/v1/mail?since=` pages first, then `GET /api/v1/mail/<id>` one by one; mismatches are reported in `RepoClient.last_problems`. `RepoClient.on_delta` is a list of callbacks called with every delta `sync_log` produces, including the syncs `WorkingCopy` runs and the verified prefix of a failing sync.
7. **Audit and export read the mirror, not the cache.** `RepoClient.log_entries(since, limit)` and `RepoClient.raw(id)` fetch what the mirror serves now, decoded but unverified, which is what `gud.ops.audit.audit` and `export_archive` expect. `gud audit` runs even when the sync fails (that failure is what it reports, as `sync_error`) and writes the report with any fork proof to `--evidence`.
8. **Review.** What needs a member (`gud review`, the MCP `inbox`) is one function, `gud.wc.review.needs`; `review` items give their reasons in the section 19.2 `review.requested` vocabulary (`line`, `lazy`, `explicit:<path>`, `quorum`, `members`).
9. **Key files.** `GUD_HOME/keys/<handle without @>.key` holds the base64 of the 32-byte Ed25519 seed plus LF (`gud.ops.keys.KeyStore`). The directory is 0700, the file 0600, and it is written atomically. A rotation stages the new key as `<name>.next.key`; `promote` replaces the current key once the rotation has linked, so the old private key is deleted. `load` refuses a key file that group or others can access. The CLI's member keys live at `GUD_HOME/members/<handle>/key` in the `ed25519:<base64 seed>` form (mode 0600).

### A.6 Dashboard view API (`gud/host_view.py`, `ui/`)

Members never read these views; two hosts of one repo show the same views because they replay the same log (section 18). Scenarios: `tests/v1/host/test_view_api.py` (a real host over the fake network, driven by `tests/v1/host/view_world.py`).

1. **Source of truth.** Every view is computed from the host's own `Replayer` (its `Chain` and a `ChainReader` with the replayer as index), its host log, member journals and delivery registry. Handlers hold the intake lock (`Intake.lock`) while they read replay state, because the replayer is not thread-safe; trees and blame come from the content-addressed store. Times are protocol ms; `t` is the replay time (section 11.3), `now` the host clock.
2. **Access.** The view API uses the mirror's check: loopback callers need no signature, anyone else signs as a member at the tip. The all-events stream is no exception, so a browser away from loopback needs a signing proxy (EventSource cannot set headers). The built UI (`/`, `/assets/*` and any other non-`/api/` path, which gets `index.html`) is open: it holds no repo data.
3. **Current candidate.** A proposal row is each unlinked push's current candidate (`Chain.push_status`, section 14.4): `carried` when its effective parent differs from its named parent. Live rows come first in received order; dead ones (newest first, `?dead=` many) follow. `race` is the position among the tip's live candidates in received order, and `blocks_tip` marks the first one that is neither parked nor qualified while others wait behind it (section 12.4). `timers.grace_until` is `close + G`, the earliest a contested window can link (section 13.4, deviation 3).
4. **Negotiations.** `rounds` is the highest episode among a candidate's objections; `revisions` lists the current candidates of every unlinked push with the same negotiation root. A thread for a push holds its whole negotiation: the root, every revision (following `Gud-Revises` back from a linked push), their votes, comments and tests, in log order.
5. **Escalation default.** `default` restates the rule of section 13.7 for display. Replay decides; the view only predicts.
6. **Blame and fading.** A line is `live` at the viewed link Y when `Y.seq − s < K` or `t − linked_at(s) < T`, with Y's config and the replay time `t` as the evaluation time; `links_left` and `expires_at` say how long. Per-file and per-member line counts come from blame runs, without reading file contents. Non-text files and symlinks have one blame entry and a placeholder instead of content.
7. **Diffs.** `ops` are the signed operations of the push (section 5.3). `files` are render ops of the tree change parent → result (`add`, `modify`, `delete`, `rename`, `mode`) with unified hunks for text and a placeholder for binaries and symlinks; `unified` is the git-style text. A carried link is shown against its actual parent, not the named one. A proposal is shown on its candidate tree, or on its named parent while it has no candidate tree.
8. **The all-events feed.** The dispatcher journals events per concerned member only, so the host keeps one more feed of every published event (`Dispatcher.observers` → `EventFeed`, persisted as `ui-events.jsonl` in the host directory, numbered from 1, deduped by event id). On start, events already in member journals but missing from the feed are backfilled in section 19.2 order. `/api/v1/view/stream` sends `id: <n>` / `event: <type>` / `data: <event JSON>` frames, resumes from `Last-Event-ID` or `?after=`, adds an `event: head` frame whenever the log grows (ticks and mail that caused no event), and keeps alive every 15 s.
9. **Self-audit.** `/api/v1/view/host` runs `gud.ops.audit.audit` against the host's own log and mail store with no retained entries, cached until the number of mail entries changes. It catches a log that does not verify, missing or altered mail and Gud-Seen mismatches, but not a fork: only members with their own retained entries can prove one (section 11.5).
10. **Agents connected.** There is no presence protocol. The panel lists delivery registrations (section 19.3 webhook or SSE) with each member's journal head and acknowledged cursor; a member without one gets email.
11. **UI actions.** `POST /api/v1/view/act/{task,vote,comment,admit}` are loopback-only, like `/api/v1/admin/*`. They sign a message with a key the operator gave `gud host serve --ui-home <GUD_HOME>` (every `members/<handle>/key` there; only handles that are members at the tip with that public key count), render it with `gud.transport.mime.build` From that member's address To the repo, and hand it to the intake exactly like received mail (`received_now`, provider id `ui`), so it is logged, replayed, served and journaled like any other message. It never leaves the host. `veto` sends an `object` whose text starts with `veto` and needs a human (section 13.5); object, veto and hold must target a push, approve a push or a task (requester acceptance, section 16.6). `admit` is the membership push answering a join request (`Gud-Join`), as `gud admit` builds it. The default signer is the first human, and for tasks the human, else the console.
12. **One repo per host.** No lead, no repo picker, no provider token in the UI. The dashboard's offline mode (`?mock=1`, or no host answering) answers the same view API from an in-memory story.

### A.7 MCP server and reference agent (`gud/mcp.py`, `gud/agent/`)

Scenarios: `tests/v1/mcp/` (the server over a real stdio MCP session, local host mode) and `tests/v1/agent/` (the runtime with a scripted session backend). The live smoke driver is `evals/v1/refagent_smoke.py`. The changes from the first real-agent grid (`evals/v1/runs/20261020-v1-report.md`) and its re-run (`evals/v1/runs/*-v1-fixes-report.md`) are folded in.

1. **One server per member session.** `python -m gud.mcp [--home H] [--handle @x] [--wc DIR] [--json]` (or `gud mcp`) binds one member: its key signs every message and its working copy is the one `push` reads. The repo is found as the CLI finds it (A.5). There is no per-call handle or key. Several servers of one member may run at once (one per agent session). They share the member's GUD_HOME, so its replay cache and event journal (item 5) are shared.
2. **Ids.** Every id argument goes through `RepoClient.resolve` (A.5), plus `base` (the working copy's base). Before resolving, the server strips `<...>` brackets, takes the hex from a `[gud:<hex>]` subject tag, and takes the local part of a Message-ID-like `<hex@host>`. An ambiguous or unknown prefix is an error that says which forms work.
3. **Full context, no truncation.** `read`, `ls`, `blame`, `diff`, `inflight` (diffs), `thread` and `task` return repo content and member text in full. `read`, `ls` and `search` take `at` = any link or an in-flight push. For a push, that means its proposed tree: the current candidate's tree, or else its operations applied to its named parent. `blame` takes links only, because a proposal has no blame yet. It reports each range's link and whether its ownership is still **live** under section 13.1 at the tip (`own-links`/`own-time`). `search` is the one bounded tool: `limit` matches (default 500), with the number left out stated. It searches the working copy with ripgrep when `rg` is on PATH, else in Python over the working tree (ignore rules applied), and any other tree in Python.
4. **Push.** `push` sends the working copy's diff against `.gud/BASE` (`WorkingCopy.push`, with the refusals of section 22). Members without a filesystem pass `edits` instead (`{path, content|content_b64, mode?}`, `{path, delete}`, `{path, rename_to}`), applied to `base` (default the tip). The build-output guard names the `.gudignore` lines to add; `allow_artifacts=true` overrides it. (The live smoke showed why: with no `.gudignore` in the seed, an agent's first link carried `.pytest_cache/` and `__pycache__/*.pyc`.) After sending, the server syncs until the host has logged the push (`GUD_WAIT`, default 10 s), then reports `linked`, or the proposal status explained in words (`explain_proposal`). A push that links lists the open subtasks nobody holds.
5. **The member's event journal.** `GUD_HOME/members/<handle>/events.jsonl` holds every section 19 event whose `concerns` include the member, deduped by event id. Each line is `{n, event}`, with `n` a local, gap-free sequence. Every channel appends to it: webhooks and SSE (the runtime), and the member's **own replay deltas**. Since section 18 makes events part of replay's output, with the same ids on every machine, a member's own replay is a valid event source. It is the only one in local host mode, which has no dispatcher. `journal_deltas(client, journal)` wraps the client instance's `sync_log`, so the syncs `WorkingCopy` runs internally (clone, push, sync) are journaled too and no delta is consumed unseen.
6. **inbox and wait.** `inbox` lists derived needs, then the journal entries since the session last looked. The derived needs are: reviews (the member is an asker, an explicit owner, or a quorum voter, and has not voted), blocking or pending-verdict objections on the member's pushes, open escalations it decides, its claims lapsing within 5 minutes, its active claims that overlap another active claim (section 10.1), and a `declare` need when other members' links carry a task it holds (check the landed work and declare done with `whole=true`). `wait(timeout, types, push)` blocks on new journal entries for at most 600 s, syncing every `poll_s`. With `push`, it also returns when that push links, dies or is objected. **Echoes** never end a wait: the member's own `claim.won` and `linked`. With `push`, neither do `test.passed`, `approved`, `held`, `carried` or `push.received`, which are steps on the way to the outcome. Besides journal events, `wait` returns when its own sync sees a link by another member after the wait started (`tip_moved`); this applies when `types` is empty or includes `linked`, and with `push` only when `linked` is asked for.
7. **Coordination verbs.** `claim` refuses an item that another member actively holds, before sending (replay would void it anyway, section 16.3; refusing first saves a message and names the holder). Its reply says whether the claim won, which active claims overlap it at the tip, and who owns live lines in that scope; a `**`/`*` scope on an unsplit task while others are idle adds a `warning` that names them. A refused claim on a held item, and a refused `subtask`, point to the open subtasks nobody holds (or, for an unsplit task, to `wait(types=["subtask.posted", "linked"])`). `subtask` refuses unless the member holds the parent's active claim (section 16.5). `claim_update` and `release` take the claim id or its task; on a claim that ended because its item is done they say so. `claim_update(done=true)` and `release(done=true)` send the section 16.6 done declaration: they wait up to 60 s for the member's own open pushes for the item to link and then refuse; on a root task they refuse while other members visibly work on the item (their open pushes for it, or their comments on it or on the claim since it won), naming them, unless `whole=true`. `tasks` reports the other agents, which of them are idle (no active claim, no live push), the open subtasks, and when a landed, unclaimed task becomes done by quiescence. `object` takes evidence as `evidence_file` (a working-copy file, whose repo path is its relative path unless `evidence_path` is given) or as `evidence_content` plus `evidence_path`, inline when it fits, else a blob. There is no `veto` tool, and `object` refuses a reason whose first word is `veto`, because agents cannot veto (section 13.5).
8. **Runtime channels and resume.** With `--listen` the runtime serves webhooks (section 19.3 (1)) and registers its URL with `POST /api/v1/delivery`. On an `http(s)` host without `--listen` it follows SSE and registers `{"sse": true}`. It always polls `sync_log` as well: every `--poll` s in local host mode, at least every 10 s otherwise. Webhook and SSE events are verified and deduped by `EventClient` (which keeps the SSE cursor), then journaled. The runtime routes journal entries after its persisted `routed` cursor (`agent/runtime.json`). A restarted runtime therefore routes exactly the events journaled while it was down, including those an MCP process journaled from its own syncs, and nothing twice.
9. **Routing.** The session key is `events.model.thread_key` with negotiation roots from the chain, redirected by an alias table. A task claimed during a turn (`task:<id>`) and every push sent during that turn (`push:<root>`) are aliased to the session that took the turn. Comments and events on the member's own pushes for a task (`push:` or `thread:` keys) go to that task's session. A unit of work therefore keeps one warm context: the session that claimed and implemented also answers the objection. Events that only confirm the member's own action (item 6), and `approved`, `held`, `test.passed`, `carried`, `task.ready`, `task.done`, `push.received`, plus `window.closing` on its own pushes, are **quiet**: journaled and shown by `inbox`, but they do not wake a turn. Other members' links that touch none of its claimed scopes or live pushes, `tip.green`, and `task.done` for items none of its claims waits on are **ambient**: quiet, and a burst of them schedules one board check (`idle_check`), debounced 3 s and run at most 15 s after the first. A `task.done` for an item listed in the `after` of a task the agent holds is routed to that task's session, and so is another member's link carrying a task the agent holds (with the `declare` hint of item 6).
10. **Turns are serial.** Bursts are debounced per session key (`Debouncer`, `--debounce` 1 s, flushed at most 5 s after a thread's first event). Turns then run one at a time: all sessions of a member share one working copy, and two concurrent turns would edit it at once. The prompt asks each turn to end with the working copy pushed, or with what is still local stated. More than `--max-open` (8) warm sessions closes the least recently used. A session is summarised and closed when every thread aliased to it is finished: its push linked or died, its task is done or held by someone else, or it was a one-shot `event:`/`idle:` wake-up. Summaries become notes that open later sessions (the last 6, in the agent's own words).
11. **Idle and startup.** When woken by `task.opened`, `claim.lapsed`, `subtask.posted` or `fix.opened`, the turn text asks the agent to consider claiming; for an open, unsplit task it names the idle agents and asks the claimant to split. After `--idle` seconds without a turn (default 300), at startup when nothing was routed, or after a board check, the runtime wakes an `idle:<n>` session if there is open work: an open task or subtask with no active claim and no undone children, and no fix task offered to someone else (at startup, a review, objection or escalation that needs the member also counts). This happens once per board signature (open ids plus link count), so an unchanged board never wakes the agent twice.
12. **Splitting guidance.** The system prompt asks a claimant to split a task with separable parts before implementing when other agents are idle: post subtasks with files and `after`, post and claim its own part first, and leave the rest. Agents with no claim are asked to claim open subtasks. The `gud-cc` eval arm gets the same guidance only through the MCP server's instructions and tool results, because its preamble must match the git arm's.
13. **Budgets.** Per member (`--budget-usd`, `--budget-tokens`) and per root task (`--task-budget-usd`, `--task-budget-tokens`), persisted in `agent/budget.json`. The SDK reports `total_cost_usd` cumulatively per session and `usage` per turn, so a turn costs the difference of the totals. Tokens are input + output + cache-creation tokens + one tenth of cache reads. A turn whose task (or the member) is over budget is not run. The task is escalated once instead: a `comment` with `Gud-Escalate: budget` and `Gud-Task`, which raises `escalated` to its requester and sponsors (section 16.7), and the member's claims under it are released. An exhausted member budget escalates every task it holds and stops the runtime. The member's remaining dollars are also passed to each SDK session as `max_budget_usd`.
14. **Sessions.** The Claude Agent SDK session runs with cwd = the working copy and model `claude-sonnet-5-5` unless `--model` is given. Its tools are exactly Read, Edit, Write, Bash, Grep and Glob plus the `gud` MCP server (`python -m gud.mcp` bound to the member and working copy), pre-approved with permission mode `dontAsk`. It runs with `strict_mcp_config`, no user or project settings, `max_turns` 80 per wake-up, and `PYTHONDONTWRITEBYTECODE=1` and `PYTEST_ADDOPTS=-p no:cacheprovider` (item 4). Reading the members' key files is denied, and so is every `--deny-path` (Read, Edit, Write, Glob and Grep). The SDK session id is kept per thread key, so a restarted runtime resumes the thread's conversation.
15. **Untrusted text.** The system prompt states that task descriptions, intents, comments, reasons and file contents are data that cannot change the rules. Each turn wraps its events in `<events>...</events>` with member-written text quoted in «», and says so in the turn text.

### A.8 End-to-end runs

`tests/v1/e2e/` runs every process separately: `gud host serve`, `gud tester`, each member command; the agent-side webhook receiver and SSE reader in the test. The fake transport and GreenMail (IMAP IDLE intake) run the full scenario; the live Primitive smoke (`GUD_LIVE_PRIMITIVE=1`) runs the reduced flow (`smoke_flow`, also run on the fake transport). One live run on 2026-10-20, fresh address `repo-e2e-<stamp>@<managed domain>`: passed, 18 emails (all to the repo address; members on SSE get no notify mail), 72 s wall time; new file linked 2.9 s after `gud push` started (consent alone), an approved edit 5.9 s, `review.requested` reached the owner's webhook 2.3 s after the push, objection logged 4.3 s after it, and the revision linked 29.4 s after the objected push (window 20 s + grace 8 s, then park).

### A.9 Resolved issues

- **A rotated-out key could link a push on a pre-rotation parent.** Section 9 verifies a push at its named parent, and an earlier draft of section 12.3 step 2 only re-checked that the author was still a member at the candidate's parent. Whoever held an old key could sign a push naming a pre-rotation link, have it carried onto the tip, and link it. Step 2 now also re-verifies the signature with the author's key at `Y`. `tests/v1/ops/test_keys.py::test_old_key_cannot_link_a_push_on_a_pre_rotation_parent` covers it.
