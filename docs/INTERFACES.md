# gud Python interfaces

The Python contract of the reference implementation. Wire format and rules are in `SPEC.md` (cited as §n); its Appendix A records the implementation choices behind several names below. Design record: `docs/PLAN.md`.

## Ground rules

- **Layers.** Modules import only along the dependency graph below. `gud/core/` imports nothing outside `gud.core` and the standard library (plus `cryptography`); `tests/v1/transport/test_registry.py` checks this for `gud.core` and `gud.transport`.
- Every name listed here is public and stable. Anything else is private (leading underscore).
- Types: `Hex` is 64 lowercase hex; `Ms` is integer unix milliseconds; `Handle` is `@name`. No floats for protocol time.
- Errors: each module raises its own subclass of `ValueError` (named below). Functions documented as "never raises" return a status instead.
- Determinism: every function in `gud/core/` is pure given its arguments (and the content-addressed store), never reads the clock, environment or network, and never depends on dict/set iteration order.

### Dependency graph (acyclic; an arrow means "may import")

```
core.types            -> (stdlib only)
core.envelope         -> core.types
core.objects          -> core.types
core.ops              -> core.types, core.objects
core.config           -> core.types, core.objects
core.hostlog          -> core.types, core.envelope
core.rules            -> core.types, core.envelope, core.objects, core.ops, core.config
core.tasks            -> core.types, core.envelope, core.objects, core.config
core.replay           -> all core.* above
transport.base        -> core.types
transport.mime        -> core.types, core.envelope
transport.auth        -> core.envelope (re-exports its Authentication-Results parser)
transport.<adapter>   -> transport.base, transport.mime, transport.auth, transport.httputil, transport.spool,
                         core.types, core.envelope
transport.registry    -> transport.base (adapters imported lazily by name)
events.model          -> core.types, core.envelope
events.dispatch       -> events.model, core.*, transport.*
events.intake         -> events.model, events.dispatch, core.*, transport.*
wc.review             -> core.* (only)
wc.*                  -> core.*, transport.*, events.model, events.dispatch (mirror auth)
runners.*             -> core.types, core.objects
host_view             -> core.*, events.*, transport.*, wc.review, ops.audit
host                  -> core.*, transport.*, events.*, host_view
tester, gitbridge, ops.*  -> core.*, transport.*, events.*, wc.*, runners.* (tester)
mcp                   -> core.*, transport.*, events.*, wc.*
agent.*               -> core.*, transport.*, events.*, wc.*, mcp
cli                   -> everything above
```

`core` never imports `transport`, `events` or anything above. `transport` depends only on `core.types` and `core.envelope`.

Two consequences of that rule: the `Authentication-Results` parser and the event-id helpers are **implemented in `core.envelope`** (replay needs both), and `transport.auth` / `events.model` re-export them under their documented names. Mirror auth (§21) lives in `events.dispatch`, so members (`wc.remote`) and the host share one implementation.

Packages get empty `__init__.py` files that re-export nothing. `gud.mcp` is a submodule of the `gud` package; absolute imports of the `mcp` SDK inside it resolve to the SDK, not to itself.

---

## `gud/core/types.py`

Plain data, no logic beyond trivial properties. Every dataclass is `frozen=True`.

```python
Hex = str
Ms = int
Handle = str
Mode = Literal["100644", "100755", "120000"]
Role = Literal["agent", "human", "tester", "console", "host"]
ZERO: Hex                                   # "0" * 64
MODES: frozenset[str]                       # the three Mode values
REGULAR_MODES: frozenset[str]               # {"100644", "100755"}
ROLES: frozenset[str]                       # the five Role values
VOTER_ROLES: frozenset[str]                 # {"agent", "human"}
SIGNED_KINDS: frozenset[str]                # every kind in §7 except "blob"
KINDS: frozenset[str]                       # §7 kinds

@dataclass(frozen=True)
class Record:
    name: str                               # §3.2 record name
    args: tuple[str, ...]                   # decoded (paths already percent-decoded)
    data: bytes

@dataclass(frozen=True)
class Envelope:
    headers: tuple[tuple[str, str], ...]    # canonical names, sorted, excluding Gud-Sig
    records: tuple[Record, ...]
    sig: str | None = None                  # "ed25519:<b64>" or None
    def get(self, name: str) -> str | None  # header value by canonical name
    def get_list(self, name: str) -> tuple[str, ...]   # §0 list parsing
    @property
    def kind(self) -> str | None
    @property
    def author(self) -> Handle | None
    @property
    def sent_ms(self) -> Ms | None          # Gud-Sent * 1000
    @property
    def text(self) -> str                   # the text record decoded, or ""
    def records_named(self, name: str) -> tuple[Record, ...]

@dataclass(frozen=True)
class Seen:
    seq: int
    entry_hash: Hex
    def render(self) -> str                 # "<seq>:<hash>"

@dataclass(frozen=True)
class Email:
    """One message at the repo address, as the host stored it."""
    gud_id: Hex                             # §3.3: envelope id, or sha256(raw_mime) for unsigned
    raw_mime: bytes
    message_id: str                         # Message-ID header value, "" if absent
    from_addr: str                          # bare lowercase address
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    in_reply_to: str | None
    references: tuple[str, ...]
    auth_results: tuple[str, ...]           # raw Authentication-Results values, top first
    text: str                               # first text/plain part, decoded ("" if none)
    envelope: Envelope | None               # None for unsigned mail
    received_at: Ms | None = None           # log time; None until logged
    provider_id: str | None = None
    # a message's DMARC check is core.envelope.dmarc_pass(email.auth_results, authserv_id, domain(email.from_addr))
    @property
    def signed(self) -> bool                # envelope present and kind in SIGNED_KINDS

@dataclass(frozen=True)
class Member:
    handle: Handle
    role: Role
    address: str
    public_key: str | None
    sponsor: Handle | None = None
    @property
    def votes(self) -> bool                 # role in VOTER_ROLES

@dataclass(frozen=True)
class LogEntry:
    seq: int
    prev: Hex
    time: Ms
    type: Literal["open", "mail", "tick", "handover"]
    fields: tuple[tuple[str, str], ...]     # type fields in §11.1 order
    sig: str
    hash: Hex                               # sha256 of the entry bytes before the sig line
    def field(self, name: str) -> str | None

@dataclass(frozen=True)
class Evidence:
    path: str
    sha256: Hex
    size: int
    inline: bytes | None                    # None when it is a blob reference

@dataclass(frozen=True)
class LinkState:
    """One link. Holds roots into the object store, never a tree dict."""
    state: Hex                              # push id (native) or carried state (§14.4)
    seq: int
    push: Hex                               # the push (or revert) id
    kind: Literal["push", "revert"]
    parent: Hex | None                      # None for genesis
    named_parent: Hex | None                # the push's Gud-Parent (differs when carried)
    author: Handle
    tree_root: Hex                          # objects.Tree root node id
    tree_hash: Hex                          # §5.2
    blame_root: Hex                         # objects.BlameMap root node id
    rules_root: Hex                         # objects id of the parsed-config bundle bytes (config.RepoRules)
    received_at: Ms
    linked_at: Ms
    task: Hex | None
    intent: str
    files: tuple[str, ...]                  # sorted paths touched (both sides of renames)
    carried_from: Hex | None = None         # named parent when carried
    revert_of: Hex | None = None

ProposalStatus = Literal[
    # live
    "window", "objected", "escalated", "needs_approval", "needs_quorum", "testing",
    "pending_blobs", "qualified", "parked", "orphan", "carried",
    # dead
    "vetoed", "tests_failed", "declined", "expired", "superseded", "stale",
    "bad_sig", "malformed", "conflict", "bad_config",
]
LIVE_STATUSES: frozenset[str]
DEAD_STATUSES: frozenset[str]

@dataclass(frozen=True)
class ObjectionState:
    id: Hex
    by: Handle
    reason: str
    received: Ms
    evidence: Evidence | None
    status: Literal["blocking", "advisory", "pending_verdict", "confirmed", "refuted", "cleared", "void"]
    answered_by: Hex | None                 # comment or revision id
    episode: int                            # §13.6

@dataclass(frozen=True)
class ProposalState:
    """One candidate (§12.3) or, for dead/orphan pushes, the push itself."""
    id: Hex                                 # candidate id (push id when native)
    push: Hex
    named_parent: Hex
    parent: Hex | None                      # effective parent (None for orphan/stale)
    author: Handle
    kind: Literal["push", "revert"]
    received_at: Ms
    status: ProposalStatus
    reason: str
    task: Hex | None
    revises: Hex | None
    negotiation: Hex                        # root push id of the negotiation
    files: tuple[str, ...]
    tree_hash: Hex | None                   # derived tree hash on `parent`
    window_open: Ms | None
    window_closes_at: Ms | None
    park_at: Ms | None
    line_owners: tuple[Handle, ...]
    askers: tuple[Handle, ...]
    declared: tuple[tuple[str, str, tuple[Handle, ...]], ...]   # (path, rule, owners)
    quorum_needed: int | None
    quorum_have: int | None
    members_rule: bool
    approvals: tuple[tuple[Handle, Ms], ...]
    objections: tuple[ObjectionState, ...]
    held_by: Handle | None
    test_needed: bool
    test_result: Literal["pass", "fail"] | None
    pending_blobs: tuple[Hex, ...]
    escalation: Hex | None
    qualified_at: Ms | None

@dataclass(frozen=True)
class TaskState:
    id: Hex
    alias: str                              # "T3", "T3.2"
    kind: Literal["task", "subtask", "fix"]
    title: str
    description: str
    author: Handle | None
    requester: Handle | None
    parent: Hex | None                      # parent task for subtasks
    opened_at: Ms
    accept_paths: tuple[str, ...]
    accept_cmds: tuple[str, ...]
    after: tuple[Hex, ...]
    files_hint: tuple[str, ...]
    bug: Hex | None
    status: Literal["open", "claimed", "work_done", "done"]
    claim: Hex | None                       # active claim id
    children: tuple[Hex, ...]
    work_done_at: Ms | None
    work_done_link: Hex | None
    done_at: Ms | None
    done_link: Hex | None
    no_change: tuple[tuple[Hex, str], ...]  # (subtask id, reason) for accepted no-changes
    cited_link: Hex | None = None           # fix tasks
    offered_to: Handle | None = None        # fix tasks
    offer_until: Ms | None = None           # fix tasks
    declared: Hex | None = None             # the holder's done declaration (claim with Gud-Done), §16.6
    declared_by: Handle | None = None
    work_done_by: str | None = None         # "subtasks", "link", "declared", "no_change", "quiet", "green" (fix)
    active_at: Ms | None = None             # latest activity (§16.6 quiescence)
    quiet_until: Ms | None = None           # when quiescence would make work done, while it can
    # fields added after the first snapshot are written only when set, so older snapshots keep their bytes

@dataclass(frozen=True)
class ClaimState:
    id: Hex                                 # the winning claim's id (stable across updates)
    latest: Hex                             # latest update's id
    task: Hex
    holder: Handle
    files: tuple[str, ...]
    after: tuple[Hex, ...]
    won_at: Ms
    last_activity: Ms
    lapses_at: Ms
    status: Literal["active", "lapsed", "released", "done", "removed", "void"]

@dataclass(frozen=True)
class EscalationState:
    id: Hex
    reason: Literal["mutual", "rounds"]
    pushes: tuple[Hex, ...]                 # negotiation roots
    opened_at: Ms
    deadline: Ms
    deciders: tuple[Handle, ...]
    status: Literal["open", "resolved"]
    chosen: Hex | None
    declined: tuple[Hex, ...]
    decided_by: Handle | Literal["default"] | None
    decided_at: Ms | None

@dataclass(frozen=True)
class RedState:
    red: bool
    red_link: Hex | None
    red_since: Ms | None
    last_green: Hex | None
    first_bad: Hex | None
    fix_task: Hex | None

EVENT_TYPES: tuple[str, ...]                # every type in §19.2, in that order

@dataclass(frozen=True)
class Event:
    id: Hex                                 # §19.1
    type: str
    at: Ms
    concerns: tuple[Handle, ...]            # sorted
    data: Mapping[str, Any]                 # JSON-compatible; ints, strs, lists, dicts, None, bools;
                                            # carries every EVENT_KEYS field of its type (§19.2)

@dataclass(frozen=True)
class Chain:
    """Read-only view of replay state at time t. Trees and blame via core.replay.ChainReader."""
    t: Ms
    log_head: Seen
    links: tuple[LinkState, ...]            # genesis first
    proposals: Mapping[Hex, ProposalState]  # by candidate id, plus dead/orphan pushes by push id
    tasks: Mapping[Hex, TaskState]
    claims: Mapping[Hex, ClaimState]
    escalations: Mapping[Hex, EscalationState]
    red: RedState
    @property
    def tip(self) -> LinkState | None
    def link(self, state: Hex) -> LinkState | None
    def push_status(self, push_id: Hex) -> ProposalState | None   # the push's current candidate

@dataclass(frozen=True)
class ChainDelta:
    """What one Replayer step changed."""
    t_before: Ms
    t_after: Ms
    log_head: Seen
    new_links: tuple[LinkState, ...]
    changed_proposals: tuple[ProposalState, ...]
    changed_tasks: tuple[TaskState, ...]
    changed_claims: tuple[ClaimState, ...]
    changed_escalations: tuple[EscalationState, ...]
    red: RedState | None                    # set when red state changed
    events: tuple[Event, ...]               # in (at, type order, id) order
    next_wakeup: Ms | None                  # earliest future time a time-only step can change state
    missing_mail: tuple[Hex, ...]           # logged ids whose raw mail is not available

class ObjectStoreLike(Protocol):            # implemented by core.objects stores
    def get(self, key: Hex) -> bytes: ...
    def put(self, data: bytes) -> Hex: ...
    def has(self, key: Hex) -> bool: ...
```

---

## `gud/core/envelope.py`

```python
VERSION: str = "1"
SIG_PREFIX: str = "ed25519:"
class EnvelopeError(ValueError): ...

def canonical_name(name: str) -> str                 # "gud-parent-task" -> "Gud-Parent-Task"
def encode_path(path: str) -> str                    # §3.2 percent-encoding
def decode_path(arg: str) -> str                     # strict; raises EnvelopeError
def render_list(items: Sequence[str]) -> str         # §0 list form
def parse_list(value: str | None) -> tuple[str, ...]

def build(kind: str, *, author: Handle | None, headers: Mapping[str, str],
          records: Sequence[Record] = (), sent: int | None = None,
          seen: Seen | None = None) -> Envelope      # adds Version/Kind/Author/Sent/Seen, sorts, validates
def encode(env: Envelope) -> bytes                   # exact bytes §3.1 (sig line last if sig)
def decode(data: bytes) -> Envelope                  # strict; raises EnvelopeError
def signing_bytes(env: Envelope) -> bytes            # encode() without the sig line
def gud_id(env: Envelope) -> Hex                     # sha256(signing_bytes)
def unsigned_id(raw_mime: bytes) -> Hex              # sha256(raw_mime)

def sign(env: Envelope, private_raw: bytes) -> Envelope       # returns a copy with sig set
def verify(env: Envelope, public_key: str | None) -> bool     # never raises
def sign_bytes(private_raw: bytes, payload: bytes) -> str     # generic Ed25519 -> "ed25519:<b64>"
def verify_bytes(public_key: str | None, payload: bytes, signature: str | None) -> bool
def generate_key() -> tuple[bytes, str]              # (private raw 32 bytes, "ed25519:<b64 pub>")
def public_of(private_raw: bytes) -> str

def parse_seen(value: str) -> Seen                   # raises EnvelopeError
def now_sent(clock: Callable[[], float]) -> int      # integer unix seconds for Gud-Sent
def sha256_hex(data: bytes) -> Hex
def jcs(value: Any) -> bytes                         # RFC 8785 canonical JSON (ints only, no floats)
def validate_kind(env: Envelope) -> str | None       # §7 required headers / record order; None if ok, else reason
def subject_tag(gud_id: Hex) -> str                  # "[gud:<12 hex>]"

# Authentication-Results (§8.1); re-exported by gud.transport.auth
@dataclass(frozen=True)
class AuthResult:
    authserv_id: str
    results: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...]   # (method, result, props) e.g. ("dmarc","pass",(("header.from","x.org"),))
def parse_authentication_results(value: str) -> AuthResult | None    # RFC 8601, comments stripped; never raises
def dmarc_pass(auth_results: Sequence[str], authserv_id: str, from_domain: str) -> bool  # topmost header with that authserv-id only

# Event ids (§19.1); re-exported by gud.events.model
EVENT_KEYS: Mapping[str, tuple[str, ...]]            # per type, the data fields forming its id key (§19.2)
def event_id(type: str, key: Sequence[str | int]) -> Hex                 # sha256(jcs({"type": type, "key": key}))
def make_event(type: str, at: Ms, concerns: Iterable[Handle], data: Mapping[str, Any]) -> Event  # id from EVENT_KEYS, concerns sorted

# validators
def is_hex64(value: str) -> bool; def is_handle(value: str) -> bool
def is_public_key(value: str) -> bool; def is_signature(value: str) -> bool
```

---

## `gud/core/objects.py`

Content-addressed storage and persistent (structurally shared) trees and blame.

```python
class ObjectError(ValueError): ...

class MemoryStore:                                   # implements ObjectStoreLike
    def __init__(self) -> None
    def get(self, key: Hex) -> bytes                 # raises KeyError
    def put(self, data: bytes) -> Hex                # key = sha256(data); idempotent
    def has(self, key: Hex) -> bool

class DiskStore:                                     # GUD_HOME/objects/<2 hex>/<62 hex>, atomic writes
    def __init__(self, root: Path) -> None
    def get(self, key: Hex) -> bytes
    def put(self, data: bytes) -> Hex
    def has(self, key: Hex) -> bool

@dataclass(frozen=True)
class Entry:
    mode: Mode
    blob: Hex                                        # sha256 of the file bytes (stored in the store)
    size: int

class Tree:
    """Immutable path -> Entry map. Nodes are stored in the store; unchanged subtrees are shared.
    Node layout is private (a directory-sharded persistent map); only `root` is persisted elsewhere."""
    @classmethod
    def empty(cls, store: ObjectStoreLike) -> Tree
    @classmethod
    def load(cls, store: ObjectStoreLike, root: Hex) -> Tree
    @classmethod
    def from_files(cls, store: ObjectStoreLike, files: Mapping[str, tuple[Mode, bytes]]) -> Tree
    @property
    def root(self) -> Hex                            # node id; NOT the §5.2 tree hash
    def hash(self) -> Hex                            # §5.2 tree hash (cached per node)
    def get(self, path: str) -> Entry | None
    def read(self, path: str) -> bytes               # raises KeyError
    def set(self, path: str, mode: Mode, data: bytes) -> Tree      # stores the blob
    def set_entry(self, path: str, entry: Entry) -> Tree           # blob must already be stored
    def delete(self, path: str) -> Tree              # raises KeyError
    def items(self) -> Iterator[tuple[str, Entry]]   # sorted by path bytes
    def paths(self) -> Iterator[str]
    def __len__(self) -> int
    def __contains__(self, path: str) -> bool
    def changed_paths(self, other: Tree) -> Iterator[str]   # sorted; skips shared nodes
    def to_files(self) -> dict[str, tuple[Mode, bytes]]     # full materialisation (tip / working copy only)

def tree_hash(entries: Iterable[tuple[str, Mode, Hex]]) -> Hex   # §5.2 over (path, mode, sha256(bytes)), sorted input
def valid_path(path: str) -> bool                    # §5.1 path rules incl. reserved paths
def check_tree_shape(tree: Tree) -> str | None       # §5.1 file/dir prefix rule; None if ok

@dataclass(frozen=True)
class BlameRun:
    owner: Handle                                    # "-" for expired runs in snapshots
    seq: int                                         # link seq that wrote these lines
    length: int                                      # number of lines (1 for non-text files)

class BlameMap:
    """Immutable path -> tuple[BlameRun, ...], structurally shared like Tree; one stored object per file."""
    @classmethod
    def empty(cls, store: ObjectStoreLike) -> BlameMap
    @classmethod
    def load(cls, store: ObjectStoreLike, root: Hex) -> BlameMap
    @property
    def root(self) -> Hex
    def get(self, path: str) -> tuple[BlameRun, ...]
    def set(self, path: str, runs: Sequence[BlameRun]) -> BlameMap
    def delete(self, path: str) -> BlameMap
    def rename(self, src: str, dst: str) -> BlameMap
    def lines(self, path: str) -> Iterator[tuple[Handle, int]]   # expanded (owner, seq) per line
    def paths(self) -> Iterator[str]
    def __contains__(self, path: str) -> bool

EMPTY_DIR: Hex                                       # node id of the empty directory
# Tree.store: the ObjectStoreLike a Tree reads and writes
```

---

## `gud/core/ops.py`

Change operations, application, diffing, merging, blame update. Byte-exact (§5).

```python
class OpError(ValueError): ...                       # an op does not apply; message says which and why

@dataclass(frozen=True)
class Patch:
    path: str
    hunks: bytes                                     # §5.4 patch-data
@dataclass(frozen=True)
class Put:
    path: str
    mode: Mode
    sha256: Hex
    size: int
    data: bytes | None                               # None = blob reference
@dataclass(frozen=True)
class Delete:
    path: str
@dataclass(frozen=True)
class Rename:
    src: str
    dst: str
@dataclass(frozen=True)
class ModeOp:
    path: str
    mode: Mode

ChangeOp = Patch | Put | Delete | Rename | ModeOp

class BlobSource(Protocol):
    def blob(self, sha256: Hex) -> bytes | None: ...   # None while pending

def ops_to_records(ops: Sequence[ChangeOp]) -> tuple[Record, ...]
def records_to_ops(records: Sequence[Record]) -> tuple[ChangeOp, ...]   # raises OpError on unknown/malformed
def touched_paths(ops: Sequence[ChangeOp]) -> tuple[str, ...]           # sorted; both rename sides
def blob_refs(ops: Sequence[ChangeOp]) -> tuple[Hex, ...]               # Put.data is None

def apply_ops(tree: Tree, ops: Sequence[ChangeOp], blobs: BlobSource) -> Tree   # raises OpError; checks §5.1
def apply_patch(old: bytes, hunks: bytes) -> bytes   # §5.4 exact; raises OpError
def make_patch(old: bytes, new: bytes, context: int = 3) -> bytes        # hunks only, no headers
def is_text(data: bytes) -> bool                     # valid UTF-8 and no NUL (§5.6)
def split_lines(data: bytes) -> list[bytes]          # §5.4 line definition

def diff_trees(old: Tree, new: Tree, *, inline_budget: int) -> tuple[ChangeOp, ...]
    # §5.3 canonical ops; Put contents above the remaining budget become blob references
def render_unified(old: Tree, new: Tree, ops: Sequence[ChangeOp], max_lines: int | None = 200) -> str
    # git-style preview for the human part / `gud diff`; never used by replay
def invert_ops(base_before: Tree, base_after: Tree, onto: Tree) -> tuple[Tree, tuple[str, ...]]
    # §15.6 revert: (result tree, conflicting paths)

@dataclass(frozen=True)
class MergeResult:
    tree: Tree
    conflicts: tuple[str, ...]                       # one human-readable line per conflicting path
def three_way(base: Tree, mine: Tree, theirs: Tree, *, paths: Iterable[str] | None = None) -> MergeResult
    # §14.1 three-way merge; `paths` restricts which paths are merged (others = theirs)
def merge_file(base: bytes | None, mine: bytes | None, theirs: bytes | None) -> bytes | None
    # raises OpError on overlap; None = deleted

@dataclass(frozen=True)
class TouchedLines:
    removed: Mapping[str, tuple[int, ...]]           # 0-based old line numbers removed or rewritten, per path
    whole_file: tuple[str, ...]                      # paths touched as a whole (delete, rename, non-text put, mode)
    inserted_at: Mapping[str, tuple[int, ...]]       # old-line positions where lines were inserted (for .py enclosing defs)
def touched_lines(before: Tree, after: Tree, ops: Sequence[ChangeOp]) -> TouchedLines
def update_blame(before: Tree, before_blame: BlameMap, after: Tree, ops: Sequence[ChangeOp],
                 owner: Handle, seq: int) -> BlameMap    # §5.6 (difflib.SequenceMatcher(autojunk=False))
def genesis_blame(tree: Tree, owner: Handle) -> BlameMap
```

---

## `gud/core/config.py`

Parsers for the four config files (§10) and globs (§10.1).

```python
class ConfigError(ValueError): ...

MEMBERS, OWNERS, TEST, CONFIG = ".gud/members", ".gud/owners", ".gud/test", ".gud/config"
BUILTIN_QUORUM: tuple[str, ...] = (OWNERS, CONFIG)

class Glob:
    def __init__(self, pattern: str) -> None         # raises ConfigError on bad syntax
    pattern: str
    def match(self, path: str) -> bool
def glob_match(pattern: str, path: str) -> bool
def globs_overlap(a: Sequence[str], b: Sequence[str], paths: Iterable[str]) -> bool   # §10.1 overlap
def parse_duration(text: str) -> Ms                  # §10.6

@dataclass(frozen=True)
class Members:
    by_handle: Mapping[Handle, Member]
    def get(self, handle: Handle) -> Member | None
    def by_address(self, address: str) -> Member | None
    def voters(self) -> tuple[Handle, ...]           # sorted
    def humans(self) -> tuple[Handle, ...]
    def host(self) -> Member
def parse_members(data: bytes) -> Members            # raises ConfigError (§10.2)
def render_members(members: Members) -> bytes

@dataclass(frozen=True)
class OwnerRule:
    glob: str
    rule: Literal["lazy", "explicit", "quorum"]
    owners: tuple[Handle, ...]
@dataclass(frozen=True)
class Owners:
    rules: tuple[OwnerRule, ...]                     # file order
    def rule_for(self, path: str) -> OwnerRule | None      # last match
    def builtin_for(self, path: str) -> Literal["quorum", "members"] | None
def parse_owners(data: bytes) -> Owners

@dataclass(frozen=True)
class TestPolicy:
    command: str | None
    image: str | None
    timeout: Ms
    on_paths: tuple[str, ...]
    on_tip_links: int | None                         # None = off
    on_tip_time: Ms | None
    every_link: bool
    stack_max: int
    auto_revert: Ms | None
def parse_test(data: bytes | None) -> TestPolicy     # None -> defaults

@dataclass(frozen=True)
class RepoConfig:
    window: Ms = 60_000
    grace: Ms = 30_000
    skew: Ms = 120_000
    own_links: int = 50
    own_time: Ms = 86_400_000
    lease: Ms = 1_800_000
    escalate: Ms = 3_600_000
    expire: Ms = 604_800_000
    carry: Literal["file", "line"] = "file"
    fix_offer: Ms = 300_000
    lease_thrash: int = 3
    checkpoint: int = 1000
    authserv_id: str | None = None
    task_quiet: Ms | None = None                     # None: the lease
    @property
    def quiet(self) -> Ms                            # task-quiet, defaulting to the lease
def parse_config(data: bytes | None) -> RepoConfig
def bare_address(value: str) -> str                  # local@domain out of a display form, lowercased

@dataclass(frozen=True)
class RepoRules:
    members: Members
    owners: Owners
    test: TestPolicy
    config: RepoConfig
    def encode(self) -> bytes                        # canonical bytes (jcs-like, stored at LinkState.rules_root)
    @classmethod
    def decode(cls, data: bytes) -> RepoRules
def rules_of(tree: Tree) -> RepoRules                # raises ConfigError (push becomes bad_config)
```

---

## `gud/core/hostlog.py`

The signed append-only log (§11).

```python
class LogError(ValueError): ...

def encode_entry(entry: LogEntry) -> bytes           # text incl. sig line
def decode_entry(text: bytes) -> LogEntry            # strict; raises LogError
def entry_bytes(seq: int, prev: Hex, time: Ms, type: str, fields: Sequence[tuple[str, str]]) -> bytes  # signed part

@dataclass(frozen=True)
class ForkProof:
    a: LogEntry
    b: LogEntry
    def to_json(self) -> dict[str, str]              # {"a": text, "b": text}

class HostLog:
    """A verified log prefix. Append is host-only; extend/verify is what members do."""
    def __init__(self, entries: Sequence[LogEntry] = (), *, trusted_keys: Sequence[str] = ()) -> None
        # trusted_keys: extra keys accepted for unplanned handover (`gud host trust`), §11.6
    @property
    def head(self) -> LogEntry | None
    @property
    def key(self) -> str                             # current host key
    repo: str                                        # address of the latest open
    repos: tuple[str, ...]                           # every address the log has had
    trusted_keys: tuple[str, ...]
    def next_repo(self) -> str | None                # the address a pending handover moves to
    def entries(self, since: int = 0, limit: int | None = None) -> list[LogEntry]
    def __len__(self) -> int
    def at(self, seq: int) -> LogEntry
    def key_at(self, seq: int) -> str
    def seq_of(self, gud_id: Hex) -> int | None      # first mail entry for the id
    def mail_ids(self, since: int = 0) -> Iterator[tuple[int, Hex, Ms]]   # (seq, id, time) in order
    def extend(self, entries: Sequence[LogEntry]) -> int   # verify and append; returns count; raises LogError at first bad entry
    def verify(self) -> None                         # full re-verification; raises LogError

    # host side
    @classmethod
    def open(cls, repo: str, host_private: bytes, now: Ms) -> HostLog           # entry 0
    def append_mail(self, gud_id: Hex, provider_time: Ms, host_private: bytes, now: Ms) -> LogEntry
        # time = max(provider_time, head.time + 1), must be <= now
    def append_tick(self, host_private: bytes, now: Ms) -> LogEntry
    def handover(self, old_private: bytes, new_public: str, now: Ms, *, repo: str | None = None) -> LogEntry
        # fields: next, and repo when the repo moves (§11.6)
    def continue_after_handover(self, new_private: bytes, now: Ms) -> LogEntry  # "open" with from=

    # members
    def check_seen(self, seen: Seen, own_seq: int) -> Literal["ok", "future", "mismatch", "unknown"]
def fork_proof(a: LogEntry, b: LogEntry, key: str) -> ForkProof | None   # same seq, different hash, both valid
def replay_time(log: HostLog, now: Ms, first_missing_seq: int | None) -> Ms   # §11.3
```

---

## `gud/core/rules.py`

Consent v1 (§13). Pure functions over a parent link's state.

```python
@dataclass(frozen=True)
class ParentView:
    """What consent needs from the parent link Y."""
    seq: int
    linked_at: Ms
    tree: Tree
    blame: BlameMap
    rules: RepoRules
    link_time: Callable[[int], Ms]                   # link seq -> linked_at (for fading)

@dataclass(frozen=True)
class ConsentPlan:
    line_owners: tuple[Handle, ...]
    lazy_declared: tuple[Handle, ...]
    explicit: tuple[tuple[str, tuple[Handle, ...]], ...]   # (path, owners minus author)
    askers: tuple[Handle, ...]                       # A, voters only, sorted
    eligible_objectors: tuple[Handle, ...]           # A ∪ human voters
    quorum_voters: tuple[Handle, ...] | None         # None when no quorum part
    quorum_needed: int | None
    members_rule: bool
    declared: tuple[tuple[str, str, tuple[Handle, ...]], ...]  # for display

def live(seq: int, parent: ParentView, eval_time: Ms) -> bool        # §13.1 fading
def consent_plan(parent: ParentView, before: Tree, after: Tree, ops: Sequence[ChangeOp],
                 author: Handle, eval_time: Ms) -> ConsentPlan

@dataclass(frozen=True)
class Vote:
    id: Hex
    kind: Literal["approve", "object", "hold", "veto"]
    by: Handle
    received: Ms
    sent: Ms | None                                  # None for unsigned
    evidence: Evidence | None = None
    reason: str = ""

def vote_counts(vote: Vote, close: Ms, cfg: RepoConfig) -> bool     # §13.4 vote rule (send time + grace)
def future_skewed(vote: Vote, cfg: RepoConfig) -> bool              # §9

@dataclass(frozen=True)
class Answer:
    id: Hex                                          # comment or revision push id
    at: Ms
    answers: Hex | None                              # objection id, or None = answers all before `at`

@dataclass(frozen=True)
class EvidenceVerdict:
    objection: Hex
    on: Hex                                          # candidate id
    on_push: Hex
    base_result: Literal["pass", "fail"] | None
    result: Literal["pass", "fail"]
    received: Ms
    contains_file: bool                              # candidate tree has the evidence file byte-exactly

@dataclass(frozen=True)
class ConsentInputs:
    plan: ConsentPlan
    author: Handle
    window_open: Ms
    votes: Sequence[Vote]                            # all votes on the push and its candidates, verified
    answers: Sequence[Answer]
    verdicts: Sequence[EvidenceVerdict]
    negotiation_objections: Sequence[ObjectionState] # evidence objections inherited from earlier revisions
    escalation: EscalationState | None
    cfg: RepoConfig

@dataclass(frozen=True)
class ConsentResult:
    holds_at: Ms | None
    status: Literal["ok", "window", "objected", "escalated", "needs_approval", "needs_quorum", "vetoed", "declined"]
    reason: str
    close: Ms                                        # window close incl. hold
    held_by: Handle | None
    objections: tuple[ObjectionState, ...]
    approvals: tuple[tuple[Handle, Ms], ...]
    quorum_have: int | None
    next_change: Ms | None                           # next time the result can change without new mail

def evaluate_consent(inp: ConsentInputs, at: Ms) -> ConsentResult   # §13.3–13.5 at decision time `at`

@dataclass(frozen=True)
class OpenPush:
    push: Hex
    negotiation: Hex
    author: Handle
    received: Ms
    task: Hex | None
    objections: tuple[ObjectionState, ...]
    episodes: Mapping[Handle, int]

def detect_escalations(open_pushes: Sequence[OpenPush], at: Ms) -> tuple[tuple[Literal["mutual", "rounds"], tuple[Hex, ...]], ...]
def escalation_id(reason: str, roots: Sequence[Hex]) -> Hex       # §13.7
def decide_escalation(esc: EscalationState, decider_votes: Sequence[Vote],
                      evidence_uncleared: Mapping[Hex, bool], received: Mapping[Hex, Ms], at: Ms) -> EscalationState
    # counts decider votes received in (opened_at, deadline]
```

Also public: `ConsentPlan.humans: tuple[Handle, ...] = ()` (human voters at Y, for the members rule), `ConsentPlan.to_json()`, `ConsentPlan.from_json(d)`; `Vote.target: Hex | None = None` (the negotiation root a decider vote is about); `ConsentInputs.push: Hex | None = None` and `ConsentInputs.negotiation: Hex | None = None` (select verdicts on the push's candidates and match escalation `chosen`/`declined`); `ConsentError`, `INF`, `BLOCKING_STATUSES`, `is_confirmed(verdict) -> bool`, `first_verdict(verdicts, objection, push, until) -> EvidenceVerdict | None`.

---

## `gud/core/tasks.py`

Leaderless tasks, claims, leases, overlap, derived done, fix tasks (§15.5, §16).

```python
@dataclass(frozen=True)
class TaskInput:                                     # one decoded, verified task/subtask/unsigned-task message
    id: Hex
    kind: Literal["task", "subtask"]
    author: Handle | None
    received: Ms
    title: str
    description: str
    requester: Handle | None
    parent: Hex | None
    accept_paths: tuple[str, ...]
    accept_cmds: tuple[str, ...]
    after: tuple[Hex, ...]
    files_hint: tuple[str, ...]
    bug: Hex | None

@dataclass(frozen=True)
class ClaimInput:
    id: Hex
    author: Handle
    received: Ms
    task: Hex
    files: tuple[str, ...]
    revises: Hex | None
    release: bool
    after: tuple[Hex, ...]
    done: bool = False                               # Gud-Done: yes (§16.6 done declaration)
    has_live_push: bool = False                      # engine: the author has a live push for the item

@dataclass(frozen=True)
class Activity:                                      # lease-renewing activity (§16.3), quiescence activity (§16.6)
    by: Handle
    task: Hex
    at: Ms

@dataclass(frozen=True)
class NoChangeInput:
    id: Hex
    author: Handle
    received: Ms
    task: Hex
    tip: Hex
    reason: str
    paths_match: bool = True                         # engine: the claim's paths are unchanged at the tip
    has_live_push: bool = False                      # engine: the author has a live push for the item

class TaskError(ValueError): ...

class TaskBoard:
    """Incremental task/claim state machine. Fed in log order by core.replay."""
    def __init__(self, cfg_at: Callable[[Ms], RepoConfig], members_at: Callable[[Ms], Members]) -> None
    def add_task(self, t: TaskInput) -> list[Event]               # validates subtask split rule (§16.5)
    def add_claim(self, c: ClaimInput, tip_paths: Iterable[str]) -> list[Event]   # win / void / update / release, overlap
    def add_activity(self, a: Activity, push: Hex | None = None) -> list[Event]
        # lease renewal from the holder; quiescence activity from anyone; `push` stays open until on_push_end or its link
    def add_no_change(self, n: NoChangeInput) -> list[Event]
    def on_link(self, link: LinkState, touched: Sequence[str], tip_paths: Iterable[str]) -> list[Event]  # work done, no-change drops, scope.exceeded
    def on_push_end(self, push: Hex, task: Hex | None, at: Ms) -> list[Event]   # a push carrying the task died
    def on_accept_test(self, task: Hex, link: Hex, result: Literal["pass", "fail"], at: Ms, red: bool) -> list[Event]
    def on_requester_accept(self, task: Hex, by: Handle, at: Ms) -> list[Event]
    def on_red(self, red: RedState, at: Ms, cited_author: Handle | None, touched: Sequence[str]) -> list[Event]  # opens fix task
    def on_green(self, at: Ms) -> list[Event]
    def advance(self, to: Ms) -> list[Event]          # lease lapses, quiescence, offer expiry, done transitions that wait on time
    def next_time(self) -> Ms | None                  # earliest lapse or quiescence time (part of ChainDelta.next_wakeup)
    def take_changes(self) -> tuple[list[Hex], list[Hex]]   # (changed task ids, changed claim ids) since the last call
    def has(self, task: Hex) -> bool
    def task(self, id: Hex) -> TaskState | None
    def claim(self, id: Hex) -> ClaimState | None
    def holders(self, task: Hex) -> list[Handle]
    def escalate(self, task: Hex, reason: str, at: Ms, by: Handle | None) -> Event
    def tasks(self) -> Mapping[Hex, TaskState]
    def claims(self) -> Mapping[Hex, ClaimState]
    def active_claim(self, task: Hex) -> ClaimState | None
    def claims_overlapping(self, paths: Sequence[str], tip_paths: Iterable[str]) -> tuple[ClaimState, ...]
    def requester_of(self, task: Hex) -> Handle | None            # resolves to the root task
    def root_of(self, task: Hex) -> Hex
    def to_json(self) -> dict                         # snapshot part
    @classmethod
    def from_json(cls, data: dict, cfg_at, members_at) -> TaskBoard

def fix_task_id(red_link: Hex) -> Hex                 # §15.5
def aliases(tasks: Sequence[TaskState]) -> Mapping[Hex, str]   # T<n> / T<n>.<m>
def scope_matches(files: Sequence[str], path: str) -> bool
```

---

## `gud/core/replay.py`

The incremental engine plus a from-scratch reference replay. `replay()` and `Replayer` MUST agree on every input (property-tested with shuffled feeds).

The `ChainDelta` contract is documented in the module docstring of `gud/core/replay.py`; `ChainDelta.events` is complete for §19.2.

```python
class ReplayError(ValueError):
    def __init__(self, message: str, delta: ChainDelta | None = None) -> None
    delta: ChainDelta | None                         # the verified prefix's delta when feed stops at a bad entry (§11.4)

@dataclass(frozen=True)
class Snapshot:
    """§17 snapshot; to_bytes() is RFC 8785 JSON."""
    version: int                                     # 1
    log_head: Seen
    t: Ms                                            # the replay time
    state: dict                                      # the engine's full persisted state (§17): links, open candidates,
                                                     # push records, votes, tests, blobs, TaskBoard.to_json(),
                                                     # escalations, red state, sorted seen ids, plus a `tip` section
                                                     # (tip entries, blame runs with expired owners as "-", rules)
    def to_bytes(self) -> bytes
    @classmethod
    def from_bytes(cls, data: bytes) -> Snapshot
    def hash(self) -> Hex

class Replayer:
    """Persisted incremental replay. One per (repo, member). Not thread-safe; callers serialise."""
    def __init__(self, store: ObjectStoreLike, *, state_dir: Path | None = None,
                 trusted_host_keys: Sequence[str] = ()) -> None
    @classmethod
    def load(cls, state_dir: Path, store: ObjectStoreLike) -> Replayer       # resumes from the last save
    @classmethod
    def from_checkpoint(cls, snapshot: bytes, store: ObjectStoreLike, *, state_dir: Path | None = None,
                        trusted_host_keys: Sequence[str] = ()) -> Replayer
        # needs the tree and blame nodes in the store; is fed the full log, and entries up to the snapshot head
        # are checked against it, not re-processed. chain() reports the snapshot head until then; save/load keep it.
    def feed(self, emails: Iterable[Email], log_entries: Sequence[LogEntry], now: Ms) -> ChainDelta
        # verify+append log entries, index emails by gud id (unlogged mail is held, not used),
        # then advance to t = replay_time(...). Idempotent for repeated inputs. On a bad entry: keeps and
        # replays the verified prefix, then raises ReplayError(delta=...).
    @property
    def tip(self) -> LinkState | None
    @property
    def last_error(self) -> str | None
    def valid_checkpoints(self) -> list[dict]         # §17 quorum groups {log, link, snapshot, signers}
    def by_message_id(self, message_id: str) -> Hex | None
    def advance(self, now: Ms) -> ChainDelta          # time-only step (window closes, lapses, deadlines)
    def chain(self) -> Chain
    def reader(self) -> ChainReader
    def log(self) -> HostLog
    def snapshot(self) -> Snapshot                    # state after the last processed log entry
    def save(self) -> None                            # atomic write of state_dir/state.json + head
    def wanted_mail(self) -> tuple[Hex, ...]          # logged ids whose raw mail is missing

def replay(emails: Iterable[Email], log_entries: Sequence[LogEntry], now: Ms, *,
           store: ObjectStoreLike | None = None, checkpoint: bytes | None = None,
           trusted_host_keys: Sequence[str] = ()) -> Chain
    # reference full replay (§12–§16); pure function of its arguments
def replay_events(emails: Iterable[Email], log_entries: Sequence[LogEntry], now: Ms, **kw) -> tuple[Event, ...]

def snapshot_at(entries: Sequence[LogEntry], mail: Callable[[Hex], Email | None], seq: int, store: ObjectStoreLike,
                trusted_host_keys: Sequence[str] = ()) -> bytes
    # §17 snapshot bytes at a log position (replay entries <= seq with now = time(seq)); raises ReplayError.
    # Shared by the host, the local host and members (gud.wc.remote.snapshot_at wraps it, raising RemoteError).

class ChainReader:
    """Accessors that need the object store. With `index` (a Replayer, as Replayer.reader() passes it),
    email, thread, candidate_tree, ops_of, stack_tree, tip_verdicts and joins work; without it they return
    None/empty (stack_tree raises)."""
    def __init__(self, chain: Chain, store: ObjectStoreLike, index: Replayer | None = None) -> None
    def tip_verdicts(self) -> dict[Hex, tuple[str, Ms]]   # link state -> counting tip-scope result
    def joins(self) -> tuple[Hex, ...]                    # logged join requests
    def tree(self, at: Hex | None = None) -> Tree     # at = link state; None = tip
    def files(self, at: Hex | None = None) -> dict[str, tuple[Mode, bytes]]
    def blame(self, path: str, at: Hex | None = None) -> list[tuple[Handle, int, bytes]]   # (owner, link seq, line)
    def rules(self, at: Hex | None = None) -> RepoRules
    def members(self, at: Hex | None = None) -> Members
    def candidate_tree(self, candidate: Hex) -> Tree | None
    def ops_of(self, push: Hex) -> tuple[ChangeOp, ...]
    def email(self, gud_id: Hex) -> Email | None
    def thread(self, gud_id: Hex) -> list[Email]      # the message and every message targeting it (transitively), log order
    def forecast(self, parent: Hex, ops: Sequence[ChangeOp], author: Handle, at: Ms) -> ConsentPlan  # for agents before pushing

# building blocks exposed for tests and the tester
def carryable(parent_chain: Sequence[LinkState], push_ops: Sequence[ChangeOp], push_files: Sequence[str],
              mode: Literal["file", "line"], reader: ChainReader, named_parent: Hex, onto: Hex) -> Tree | None   # §14.1–14.2
def carried_state(push: Hex, onto: Hex) -> Hex       # §14.4
def stack_tree(reader: ChainReader, base: Hex, pushes: Sequence[Hex]) -> tuple[Tree, int]  # (tree, how many applied) §15.3
def red_state(links: Sequence[LinkState], verdicts: Mapping[Hex, tuple[Literal["pass", "fail"], Ms]], at: Ms) -> RedState  # §15.4
def test_required(policy: TestPolicy, files: Sequence[str]) -> bool   # §15.1
def resolve_unsigned_target(email: Email, by_message_id: Mapping[str, Hex], ids: Sequence[Hex] | Mapping[str, list[Hex]],
                            notify_target: Mapping[Hex, Hex]) -> Hex | None   # §8.2; ids may map 12-hex prefixes to ids
def human_verb(text: str) -> tuple[Literal["approve", "accept", "object", "veto"], str] | None   # §8.3
```

---

## `gud/transport/base.py`

```python
class TransportError(RuntimeError): ...
class QuotaExceeded(TransportError): ...

@dataclass(frozen=True)
class Capabilities:
    name: str                                        # "imap_smtp", "jmap", "primitive", ...
    max_message_bytes: int
    push: Literal["webhook", "idle", "eventsource", "pubsub", "none"]
    preserves_message_id: bool
    auth_results: Literal["header", "synthesized", "none"]
    authserv_id: str | None                          # fixed id when synthesized
    send_quota_per_day: int | None
    received_time_resolution_ms: int
    raw_mime: bool                                   # must be True to host a repo

@dataclass(frozen=True)
class Outgoing:
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    subject: str = ""
    text: str = ""                                   # human part
    envelope: bytes | None = None                    # gud.env attachment bytes
    message_id: str | None = None                    # adapter generates if None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()

@dataclass(frozen=True)
class Received:
    raw_mime: bytes                                  # as received, incl. Authentication-Results
    received_at: Ms                                  # receiving server's delivery time
    provider_id: str
    cursor: str                                      # resumable position after this message

@dataclass(frozen=True)
class Sent:
    provider_id: str | None
    message_id: str                                  # as the provider will deliver it (rewritten if known)
    accepted_at: Ms

class Subscription(Protocol):
    def close(self) -> None: ...

class Mailbox(Protocol):
    address: str                                     # the address this mailbox reads
    def capabilities(self) -> Capabilities: ...
    def send(self, message: Outgoing | bytes) -> Sent: ...        # raw bytes are sent unchanged
    def list(self, *, since: str | None = None, limit: int = 500) -> tuple[list[Received], str | None]: ...
    def wait(self, *, since: str | None, timeout: float) -> list[Received]: ...
    def subscribe(self, callback: Callable[[Received], None], *, since: str | None = None) -> Subscription: ...
        # push intake; reconnects internally and catches up with list(); callback may be called twice for one message
        # without `since`: from the head (mail arriving after the call); with `since`: catch up from that cursor first
    def close(self) -> None: ...

class WebhookVerifier(Protocol):                     # adapters with webhook intake implement this
    def verify(self, headers: Mapping[str, str], body: bytes, now: Ms) -> bool: ...
    def parse(self, headers: Mapping[str, str], body: bytes) -> list[Received]: ...   # items have cursor == ""
```

Also: `PushKind`, `AuthResultsKind`, `ENVELOPE_CAP`, `MIME_OVERHEAD`, `envelope_budget(max_message_bytes, host_max_message_bytes=None) -> int` (§6 budget), `now_ms(clock)`, `ThreadSubscription(target, *, name)` (a `Subscription` running `target(stop_event)` on a daemon thread), `safe_call(callback, item)`, and the string-config helpers `cfg_bool`, `cfg_int`, `cfg_list` used by every `from_config`. `Mailbox` is `runtime_checkable`. Every built-in adapter also has `head() -> str | None`, the cursor just after the newest message (not part of the protocol; the conformance suite uses it when present).

## `gud/transport/mime.py`

```python
class MimeError(ValueError): ...
PREVIEW_LINES: int = 200

def build(message: Outgoing, *, date: str | None = None, boundary: str | None = None) -> bytes
    # §2.1 layout; envelope base64 in 76-char lines; text/plain quoted-printable
def render_human_part(env: Envelope, gud_id: Hex, preview: str = "") -> str     # §2.1 SHOULD content
def subject_for(env: Envelope, gud_id: Hex, summary: str) -> str               # "[gud] <kind> <summary> [gud:<12>]"

@dataclass(frozen=True)
class ParsedMime:
    headers: tuple[tuple[str, str], ...]             # top-level, decoded, in order
    message_id: str
    from_addr: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    in_reply_to: str | None
    references: tuple[str, ...]
    auth_results: tuple[str, ...]
    text: str
    envelope_bytes: bytes | None
    envelope_parts: int                              # number of gud.env parts found
    has_text: bool = False                           # a non-gud.env text/plain part exists (§2.2)
def parse(raw: bytes) -> ParsedMime                  # never raises on bad bodies; raises MimeError only if headers unparseable
def to_email(raw: bytes, *, received_at: Ms | None = None, provider_id: str | None = None) -> Email | None
    # decodes the envelope, computes gud_id; None if it has >1 gud.env, an envelope that fails decode,
    # or (unsigned) no text/plain part

ENV_FILENAME: str                                    # "gud.env"
def make_message_id(from_addr: str) -> str
def qp_encode(text: str) -> bytes; def b64_lines(data: bytes) -> bytes
def is_envelope_part(part) -> bool
def outgoing_from_raw(raw: bytes) -> Outgoing        # for structured-send providers
def recipients(raw: bytes) -> tuple[str, ...]
```

## `gud/transport/auth.py`

Imports only `core.envelope`.

```python
from gud.core.envelope import AuthResult, parse_authentication_results, dmarc_pass   # re-exported
def synthesize(authserv_id: str, dmarc: str, from_domain: str) -> str  # header value for adapters without raw A-R;
                                                                       # ValueError for an id or domain that is not a token
def prepend(raw: bytes, value: str) -> bytes        # one Authentication-Results line, in the message's own line ending
def sender_verified(auth_results: Sequence[str], from_addr: str, authserv_id: str | None) -> bool   # §8.1 (2)
```

## `gud/transport/httputil.py`, `gud/transport/spool.py`

```python
# httputil
RETRY_STATUSES; def retry_delay(resp, attempt) -> float; def parse_iso_ms(value) -> Ms
def iso_ms(ms, fraction=True) -> str; def header(headers, name) -> str | None
# spool: the local store behind the SES and Postmark adapters (SPEC Appendix A.1)
class Spool:
    def __init__(self, root: Path, *, poll_interval: float = 0.05) -> None
    def ingest(self, raw: bytes, received_at: Ms, provider_id: str, *, source: str = "") -> Received | None  # one copy per provider id
    def list(...); def head(); def wait(since, timeout, on_idle=None, idle_every=0); def has(provider_id); def ids()
def parse_cursor(cursor: str | None) -> int
```

## Adapters (`gud/transport/*.py`)

Each module exposes one class implementing `Mailbox` (and `WebhookVerifier` where marked), plus `from_config(cfg: Mapping[str, str]) -> Mailbox`.

| Module | Class | Notes |
| --- | --- | --- |
| `fake.py` | `FakeMailbox(root: Path, address: str, *, clock: Callable[[], float] = time.time)` | Local JSONL store shared by addresses under one root; `send` delivers raw bytes to every recipient with a strictly increasing `received_at`; `subscribe` via file watch or in-process callbacks. Also `FakeNetwork` (in-memory, deterministic clock, fault injection: delay, duplicate, drop, reorder) for protocol tests. |
| `imap_smtp.py` | `ImapSmtpMailbox(imap_host, imap_port, smtp_host, smtp_port, username, password, address, *, folders=("INBOX", "Junk"))` | IDLE with reconnect; cursor `"<folder>:<UIDVALIDITY>:<UID>"`; received time = INTERNALDATE; SMTP submission (RFC 6409). |
| `jmap.py` | `JmapMailbox(session_url, token, address)` | Email/query + Email/changes, EventSource push, upload + Email/import + EmailSubmission; cursor = JMAP state (+ receivedAt position); received = `receivedAt`. |
| `primitive.py` | `PrimitiveMailbox(api_key, address, *, base_url=...)` + `WebhookVerifier` (HMAC) | Reads raw MIME only (never the parsed body); synthesizes A-R if only JSON auth is exposed. |
| `ses.py` | `SesMailbox(region, credentials, address, *, inbound_bucket=None, topic_arns=())` + `WebhookVerifier` (SNS signature) | Outbound SESv2 SendEmail (Raw); inbound via SNS→webhook, raw MIME inline or from S3; local spool. |
| `postmark.py` | `PostmarkMailbox(server_token, address)` + `WebhookVerifier` (basic auth) | Inbound JSON includes `RawEmail` when enabled (required). |
| `gmail.py` | `GmailMailbox(credentials_path, address)` + `WebhookVerifier` (Pub/Sub JWT) | `history.list` sync, `users.watch`; raw via `format=raw`; quota reported (humans and small swarms only). |

Full constructors and the other public names:

- `gud.transport.fake`: `FAKE_AUTHSERV_ID = "fake.gud.invalid"`, `DEFAULT_MAX_MESSAGE_BYTES` (10 MiB). `FakeMailbox(root, address, *, clock, max_message_bytes, authserv_id, poll_interval)` plus `head()`, `deliver(raw, recipients=None, *, received_at=None)` (inject from an outside MTA). `Faults(delay_ms, jitter_ms, duplicate, drop, seed)`. `FakeNetwork(*, start_ms, faults, max_message_bytes, authserv_id)` with `now_ms`, `clock()`, `mailbox(address) -> NetworkMailbox`, `advance(ms)`, `advance_to(ms)`, `next_due()`, `pending`, `set_faults`, `submit(raw, recipients)`. `NetworkMailbox` adds `deliver(raw, recipients=None)` and `head()`; its `wait` advances virtual time instead of sleeping, and its generated Message-IDs are numbered across the whole network.
- `gud.transport.imap_smtp`: constructor keywords `imap_tls`/`smtp_tls` (`"ssl" | "starttls" | "none"`), `smtp_username`, `smtp_password`, `max_message_bytes`, `authserv_id` (the provider's, reported in capabilities), `send_quota_per_day`, `preserves_message_id`, `idle_poll`, `timeout`, `ssl_context`, `clock`. Helpers `encode_cursor`, `decode_cursor`, `parse_internaldate`.
- `gud.transport.primitive`: `PrimitiveMailbox(api_key, address, *, base_url, client, webhook_secret, authserv_id, subscribe_wait, send_quota_per_day, max_message_bytes, clock, sleep)` implements `Mailbox` and `WebhookVerifier`, plus `accept_webhook(headers, body, now=None) -> list[Received]` (verify, parse, hand to subscribers), `fetch_webhook_secret()` and `head()`. Also `AUTHSERV_ID = "primitive.gud.invalid"`, `MAX_MESSAGE_BYTES`, `PrimitiveError(TransportError)`, `PrimitiveQuotaError(PrimitiveError, QuotaExceeded)`, `signature_header`, `verify_signature`, `dmarc_for`, `unwrap_webhook`, `event_name`, `encode_cursor`, `decode_cursor`, `parse_time`. The Primitive API reference is `docs/primitive-openapi.json`.
- `gud.transport.jmap`: `JmapMailbox(session_url, token, address, *, username, password, client, account_id, authserv_id, max_message_bytes, send_quota_per_day, received_time_resolution_ms=1000, preserves_message_id=True, ping=15, poll_interval=10, clock, sleep)` implements `Mailbox`, plus `session()`, `mailbox_roles()`, `head()`. Also `CORE`, `MAIL`, `SUBMISSION`, `JmapError(type_, description, status)` (`.type`), `JmapQuotaError`, `encode_cursor`, `decode_cursor`, `expand` (URL templates), `iter_sse`.
- `gud.transport.ses`: `SesMailbox(region, credentials, address, *, inbound_bucket, inbound_prefix, topic_arns, store, client, ses_endpoint, s3_endpoint, authserv_id, max_message_bytes, send_quota_per_day, configuration_set, catchup_interval=60, confirm_subscriptions=True, clock, sleep)` implements `Mailbox` and `WebhookVerifier` (SNS), plus `accept_webhook(headers, body, now=None) -> list[Received]` (verify; confirm subscriptions; store), `confirm(msg)`, `catch_up() -> int`, `head()`. `topic_arns` is required for SNS intake; received mail is kept in a `Spool` (`store`). Also `AUTHSERV_ID = "ses.gud.invalid"`, `MAX_MESSAGE_BYTES`, `SNS_CONTENT_MAX`, `AwsCredentials(access_key_id, secret_access_key, session_token=None)` (`.from_env()`), `sigv4_headers(method, url, *, region, service, credentials, body, headers, amz_date, unsigned_payload)`, `SnsVerifier(client, topic_arns)`, `sns_string_to_sign`, `check_sns_url`, `message_id_domain(region)`, `SesError`, `SesQuotaError`, `SnsVerificationError`.
- `gud.transport.postmark`: `PostmarkMailbox(server_token, address, *, base_url, client, store, message_stream="outbound", webhook_username, webhook_password, allowed_ips, inbound_aliases, authserv_id=None, max_message_bytes, send_quota_per_day, preserves_message_id=False, catchup_interval=60, clock, sleep)` implements `Mailbox` and `WebhookVerifier`; `verify` takes an extra keyword `remote_addr=None` (default: the `X-Gud-Remote-Addr` header). Plus `accept_webhook(headers, body, now=None, *, remote_addr=None) -> list[Received]`, `catch_up() -> int`, `head()`. Also `WEBHOOK_IPS`, `REMOTE_ADDR_HEADER`, `MAX_MESSAGE_BYTES`, `MESSAGE_ID_DOMAIN`, `basic_auth_header`, `PostmarkError(code, message, status)`, `PostmarkQuotaError`.
- `gud.transport.gmail`: `GmailMailbox(credentials_path=None, address, *, oauth, client, token_cache, account_type="auto", send_quota_per_day, max_message_bytes, pubsub_topic, push_audience, push_service_account, poll_interval=10, preserves_message_id=False, report_message_id=True, api_base, upload_base, clock, sleep)` implements `Mailbox` and `WebhookVerifier` (Pub/Sub JWT; `parse` returns `[]`, since a push carries no mail), plus `accept_webhook(headers, body, now=None) -> dict` (the notification; wakes subscriptions), `watch()`, `notification(body)`, `head()`. Also `AUTHSERV_ID = "mx.google.com"`, `CONSUMER_SEND_PER_DAY = 500`, `WORKSPACE_SEND_PER_DAY = 2000`, `SCOPES`, `OAuthClient(client_id, client_secret, refresh_token, token_uri)` (`.from_file(path)`), `TokenCache`, `GoogleJwtVerifier(client, *, audience, service_account, jwks_uri, clock, leeway)`, `consent_url`, `authorize_installed_app(client_id, client_secret, *, scopes, login_hint, open_browser, client, timeout) -> refresh token`, `delivered(labels)`, `encode_cursor`, `decode_cursor`, `GmailError`, `GmailQuotaError`, `GmailAuthError`, `JwtError`.

## `gud/transport/registry.py`

```python
Factory = Callable[[Mapping[str, str]], Mailbox]
def register(name: str, factory: Factory) -> None
def names() -> tuple[str, ...]                       # built-ins: fake, imap_smtp, jmap, primitive, ses, postmark, gmail
def open_mailbox(name: str, cfg: Mapping[str, str]) -> Mailbox   # lazy-imports the adapter module
def from_env(env: Mapping[str, str] | None = None) -> Mailbox    # GUD_TRANSPORT + GUD_<NAME>_* variables
def member_config(home: Path, handle: Handle) -> tuple[str, dict[str, str]]  # GUD_HOME/members/<handle>/transport.toml
    # merges top-level keys with a table named after the transport; resolves <key>_env from the environment
def read_transport_toml(path: Path) -> tuple[str, dict[str, str]]   # the same, for a host directory's transport.toml
BUILTINS: tuple[str, ...]                            # every built-in is implemented
```

---

## `gud/events/model.py`

```python
from gud.core.types import EVENT_TYPES, Event                              # re-exported
from gud.core.envelope import EVENT_KEYS, event_id, make_event            # re-exported
def to_json(event: Event, repo: str) -> bytes        # §19.1 body (jcs)
def from_json(body: bytes) -> tuple[str, Event]      # (repo, event); raises ValueError
def validate(event: Event) -> None                   # data fields, JSON shape, sorted handle concerns, id from key
def thread_key(event: Event, roots: Mapping[Hex, Hex] | None = None) -> str
    # session routing key: negotiation root, task id, or event id; roots maps push ids to negotiation roots
THREAD_KEY = thread_key
DATA_FIELDS: Mapping[str, tuple[str, ...]]           # the §19.2 data fields of each type
HUMAN_NOTIFY_TYPES: frozenset[str]                   # §19.4 human notifications
def event_dict(event: Event, repo: str | None = None) -> dict; def event_from_dict(obj: Mapping) -> Event
def recipients(event: Event, members: Members) -> tuple[Member, ...]   # concerned handles still members; never the host

@dataclass(frozen=True)
class Delivery:
    handle: Handle
    channel: Literal["webhook", "sse", "email"]
    url: str | None = None

@dataclass(frozen=True)
class JournalItem:
    cursor: int                                      # per-member monotonic journal seq
    event: Event
    handle: Handle = ""
```

## `gud/events/dispatch.py`

```python
HOOK_HEADERS = ("X-Gud-Event-Id", "X-Gud-Cursor", "X-Gud-Time", "X-Gud-Sig")
def hook_bytes(time_token: str, body: bytes) -> bytes        # b"gud-hook/1\n" + time + b"\n" + body
def sign_hook(body: bytes, host_private: bytes, now: float) -> dict[str, str]   # minus id/cursor
def verify_hook(headers: Mapping[str, str], body: bytes, host_keys: Sequence[str], now: float) -> bool  # ±300 s

class Journal:
    """Durable per-member event journal (GUD_HOME/host/journal/<handle>.jsonl)."""
    def __init__(self, root: Path) -> None
    def append(self, events: Sequence[Event]) -> list[JournalItem]      # one item per concerned member; dedupes by event id
    def since(self, handle: Handle, after: int, limit: int = 500) -> list[JournalItem]
    def head(self, handle: Handle) -> int
    def handles(self) -> tuple[Handle, ...]
    def has(self, handle: Handle, event_id: Hex) -> bool

# mirror auth v1 (§21), shared by members (gud.wc.remote) and the host (gud.host re-exports them)
AUTH_HANDLE, AUTH_TIME, AUTH_SIG = "X-Gud-Handle", "X-Gud-Time", "X-Gud-Sig"; HOOK_PREFIX; MAX_SKEW_S = 300
def format_time(now: float) -> str                   # whole seconds without fraction, else up to 6 digits
def request_bytes(method: str, path_with_query: str, time_token: str, body: bytes = b"") -> bytes
def sign_request(handle: Handle, private_raw: bytes, method: str, path_with_query: str,
                 body: bytes, now: float) -> dict[str, str]
def verify_request(members: Members, method: str, path_with_query: str, body: bytes,
                   headers: Mapping[str, str], now: float, max_skew: int = 300) -> Handle | None

def notify_message(events, *, members, host_private, seen, to, cc=(), sent) -> Outgoing   # §19.3 (3) / §19.4
def parse_sse(lines) -> Iterator[tuple[int | None, str]]   # (id, data)

class DeliveryRegistry:
    def __init__(self, path: Path) -> None
    def set(self, delivery: Delivery) -> None
    def get(self, handle: Handle) -> Delivery        # default: email
    def all(self) -> tuple[Delivery, ...]

class Dispatcher:
    def __init__(self, journal: Journal, registry: DeliveryRegistry, *, repo: str, host_private: bytes,
                 send: Callable[[Outgoing], Sent], members: Callable[[], Members],
                 http: "httpx.Client | None" = None, clock: Callable[[], float] = time.time,
                 seen=None, state_path: Path | None = None, email_batch_s: float = 30.0, retry_cap_s: float = 600.0,
                 retry_for_s: float = 86_400.0, hook_timeout_s: float = 5.0, on_delivered=None) -> None
    observers: list[Callable[[Sequence[Event]], None]]    # called with every non-empty publish batch before journaling
    def publish(self, events: Sequence[Event]) -> list[JournalItem]   # journal, then schedule delivery
    def pump(self) -> int                                 # one delivery pass (webhooks with backoff, email batches); returns sent count
    def sse(self, handle: Handle, after: int, *, follow: bool = True, keepalive_s: float = 15.0,
            max_s: float | None = None) -> Iterator[bytes]    # SSE frames: id/event/data, keepalive comments
    def notify_humans(self, events: Sequence[Event], log_has_event: Callable[[Hex], bool]) -> None  # queues §19.4 notifies; pump sends
    def acked(self, handle: Handle) -> int; def wake(self) -> None
    def run(self, stop: threading.Event) -> None; def start(self, stop: threading.Event) -> threading.Thread
    stats: dict

class EventClient:
    """Member side: receive events by webhook or SSE, dedupe, persist cursor."""
    def __init__(self, state_path: Path, host_keys: Callable[[], Sequence[str]], *, repo: str | None = None,
                 max_seen: int = 100_000) -> None
    def accept_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> Event | None  # None if bad sig or duplicate
    def accept(self, event: Event, cursor: int) -> bool
    def stream(self, mirror, handle: Handle, *, follow: bool = True, timeout: float | None = None,
               clock=time.time) -> Iterator[Event]   # SSE with resume; mirror: host_url, private_raw, optional http
    def seen(self, event_id: Hex) -> bool
    @property
    def cursor(self) -> int
    rejected: int; duplicates: int

class Debouncer:                                      # agent-side burst batching per session thread (§19.4)
    def __init__(self, flush, *, delay_s: float = 1.0, max_delay_s: float = 5.0, key=thread_key,
                 clock=time.monotonic) -> None
    def add(self, events); def poll(self, now=None); def flush_all(self); pending: int; def next_due(self)
```

## `gud/events/intake.py`

```python
class Intake:
    """Host-side single entry point for 'an email arrived' (§11, Phase 1b)."""
    def __init__(self, *, repo: str, mailbox: Mailbox, replayer: Replayer, log_path: Path,
                 host_private: bytes, mail_store: Path, dispatcher: Dispatcher,
                 clock: Callable[[], float] = time.time, rate_limit_per_sender: int = 120,
                 state_path: Path | None = None, verifiers=None, tick_idle_ms: int = 5_000, tick_max_ms: int = 60_000,
                 catch_up_interval_s: float = 30.0, save_interval_s: float = 5.0, on_hop=None) -> None
        # verifiers None + a mailbox that is a WebhookVerifier: the mailbox is registered under its transport name;
        # on_hop(name, gud_id, perf_counter) reports the hops received, logged, replayed, journaled
    def on_received(self, item: Received) -> ChainDelta | None
        # parse (to_email), filter (§11.1 what gets logged), dedupe by gud id, store raw MIME, append mail entry,
        # Replayer.feed, Dispatcher.publish(delta.events). None if not logged.
    def on_webhook(self, provider: str, headers: Mapping[str, str], body: bytes) -> int
        # WebhookVerifier.verify + parse, then on_received for each; returns accepted count.
        # KeyError for an unknown provider; WebhookRejected (a PermissionError) for a bad signature
    def catch_up(self) -> int                        # list() from the stored cursor; on start and after reconnect
    def tick(self, *, force: bool = False) -> ChainDelta | None   # appends a tick entry when due (§11.1) and advances replay
    def run(self, stop: threading.Event) -> None     # subscribe + catch_up + tick loop + dispatcher.pump
    def log_entries(self, since: int, limit: int) -> list[LogEntry]
    def raw(self, gud_id: Hex) -> bytes | None
    def register_verifier(self, provider: str, verifier: WebhookVerifier) -> None
    def next_tick_in(self) -> float | None; def repair(self) -> int; def save(self) -> None
    def head(self); def seen(self) -> Seen; def mail_since(self, since: int, limit: int); def wait_log(self, since, timeout)
    def object(self, sha256: Hex) -> bytes | None; def rules(self); def members(self); def host_keys(self)
    def log_has_event(self, event_id: Hex) -> bool
    def snapshot(self) -> tuple[Hex, bytes]; def snapshot_bytes(self, sha256: Hex) -> bytes | None  # incl. those named by logged checkpoints
    def handover(self, new_public: str, *, repo: str | None = None) -> LogEntry
    log: HostLog; stats: dict; missing: tuple[Hex, ...]; next_wakeup: Ms | None; last_error: str | None
    lock: threading.RLock                            # for readers of replay state on other threads

class IntakeError(ValueError): ...
class WebhookRejected(PermissionError): ...
def received_now(raw: bytes, clock=time.time, provider_id: str = "inject") -> Received   # direct injection (dashboard actions)
```

## `gud/host.py`

The host process: §21 HTTP mirror + intake + dispatch, and the dashboard (`gud/host_view.py`). `verify_request`, `sign_request`, `format_time` and `request_bytes` are re-exported from `gud.events.dispatch`; `load_signers` from `gud.host_view`.

```python
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
def create_app(intake: Intake, dispatcher: Dispatcher, registry: DeliveryRegistry, *, repo: str,
               bugs: Mailbox | None = None, loopback_hosts=None, clock=None, sse_keepalive_s: float = 15.0,
               feed: EventFeed | None = None, ui_signers: Mapping[Handle, bytes] | None = None,
               ui_dist: Path | None | bool = None) -> "fastapi.FastAPI"
    # §21 endpoints (reads open before genesis), POST /api/v1/admin/handover (loopback; {"key", "repo"?}),
    # the view API, and the built UI at / (ui_dist=False: no page)
def create_webhook_app(intake: Intake) -> "fastapi.FastAPI"   # only the provider webhook route, for a tunnelled port
class Host:                                          # intake, dispatcher, registry, replayer, mailbox, feed, ui_signers
    def app(self) -> "fastapi.FastAPI"; def save(self) -> None
def open_host(home: Path, *, repo: str, mailbox: Mailbox, host_private: bytes, bugs: Mailbox | None = None,
              clock=time.time, http=None, store=None, persist_replay: bool = True, dispatcher_kw=None,
              intake_kw=None, base: Path | None = None, ui_signers=None) -> Host
    # files under base (default home/host): log.jsonl, mail/, journal/, delivery.json, intake.json, replay/,
    # snapshots/, ui-events.jsonl; objects under home/objects
def run_host(home: Path, *, repo: str, mailbox: Mailbox, host_private: bytes, bind: str = "127.0.0.1:8787",
             webhook_bind: str | None = None, bugs=None, stop=None, log_level: str = "warning", base=None,
             store=None, on_ready=None, ui_signers=None, ui_dist=None) -> None
def serve_dir(root: Path, *, bind: str = "127.0.0.1:8787", webhook_bind: str | None = None, stop=None,
              log_level: str = "warning", on_ready=None, ui_signers=None, ui_dist=None) -> None
    # `gud host serve --dir D`: a host directory (key, host.json, transport.toml, log.jsonl, mail/); holds serve.lock
def serve(home: Path, *, transport: str, bind: str = "127.0.0.1:8787") -> None
    # older entry: reads home/host/host.key (64 hex) and home/host/transport.toml, else GUD_<TRANSPORT>_* variables
```

`gud host init` (DNS checks, round trip, latency, proposed `window`/`grace`) is implemented in the CLI.

## `gud/host_view.py`

The host's dashboard API (SPEC Appendix A.6). Read-only views of the host's own replay, plus loopback-only actions.

```python
class EventFeed:
    def __init__(self, path: Path | None, journal: Journal | None = None) -> None   # JSONL; backfills from journals
    def add(self, events: Sequence[Event]) -> int     # new ones only (dedupe by id); wakes waiters
    def since(self, after: int, limit: int = 500) -> list[tuple[int, Event]]     # numbered from 1
    def head(self) -> int
    def wait(self, after: int, timeout: float) -> bool
def load_signers(homes: Iterable[Path]) -> dict[Handle, bytes]   # every members/<handle>/key ("ed25519:<b64 seed>")
def default_ui_dist() -> Path | None                             # $GUD_UI_DIST, else ui/dist of the checkout, if built
class HostView:
    def __init__(self, intake, *, repo, dispatcher=None, registry=None, feed=None, bugs=None, signers=None, clock=None)
    # one method per endpoint below (state, chain, proposals, tasks, escalations, members, tree, file, blame, diff,
    # thread, needs, host, events, stream) and act_task / act_vote / act_comment / act_admit
def add_view_routes(app, view: HostView, *, caller, is_loopback, sse_keepalive_s=15.0) -> None
def add_static_routes(app, dist: Path | None) -> None            # "/" and non-/api paths; index.html fallback
```

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/v1/view/state` | `{repo, bugs_address, host_key, transport, now, t, log_head{seq,hash,time,type}, processed{seq,hash}, config, test_policy, tip: Link, red, counts{links, proposals_live, tasks_open, claims_active, escalations_open}, ui{signers[{handle,role}], human, console, actions}}`; `red` = `RedState` + `red_link_seq`, `last_green_seq`, `first_bad_seq`, `first_bad_author`, `first_bad_title`, `fix_task{id, alias, status, title, offered_to, offer_until, cited_link, holder}` |
| GET | `/api/v1/view/chain?limit=200&before=<seq>` | `{links, total, next_before}`, newest first; Link = `{seq, state, push, kind, author, intent, title, tree_hash, parent, named_parent, carried_from, carried_from_seq, revert_of, received_at, linked_at, task, task_alias, files, verdict, verdict_at, red_role, diffstat{added, removed, files, binary}}` (`verdict`: the counting tip-scope result; `red_role`: `first_bad`, `red_link`, `last_green` or null) |
| GET | `/api/v1/view/proposals?dead=50` | `{proposals, live, dead_shown}`; each `{id, push, kind, author, title, intent, received_at, status, reason, live, dead, parent, parent_seq, named_parent, named_parent_seq, carried, task, task_alias, revises, negotiation, files, tree_hash, timers{window_open, window_closes_at, grace_until, park_at, expires_at, window, grace, held, held_by, qualified_at}, plan{line_owners, askers, declared[{path, rule, owners}], quorum_needed, quorum_have, members_rule, waiting_on}, approvals[{by, at}], objections[{id, by, reason, received, kind, evidence{path, sha256, size}, status, answered_by, episode, blocking}], rounds, revisions[{push, candidate, status, received_at, revises, current}], linked_revisions, test{needed, result}, pending_blobs, escalation, race, blocks_tip, join}` (`kind` of an objection: `reason`, `evidence` or `veto`) |
| GET | `/api/v1/view/tasks` | `{tasks, claims, overlaps, bugs}`: `TaskState` fields + `parent_alias, holder, has_criteria, cited_seq, cited_author, waiting (acceptance / criteria / null), claims, links`; `ClaimState` + `task_alias, overlaps`; overlaps `[{claims, holders, tasks, globs, paths}]`; bugs `[{id, from, subject, text, received_at, task, task_alias}]` |
| GET | `/api/v1/view/escalations` | `{escalations}`: `EscalationState` + `pushes[{root, push, author, title, status, confirmed_evidence}]`, `default{chosen, declined, why}` |
| GET | `/api/v1/view/members` | `{members: [{handle, role, address, key, key_short, sponsor, votes, lines, lines_live, added_seq, delivery{channel, url, registered, journal_head, acked}}], removed: [{handle, role, address, removed_seq, removed_link, lines}], joins: [{id, handle, role, address, key, key_short, sponsor, reason, received_at, status (pending / proposed / member), push}]}` |
| GET | `/api/v1/view/tree?at=` | `{at{kind (link / proposal), state, seq or push, status}, files: [{path, mode, size, sha256, binary, lines, owners{handle: n}, live_owners{handle: n}, former}]}` |
| GET | `/api/v1/view/file?path=&at=` | `{path, at, mode, size, sha256, kind (text / binary / symlink), text, truncated, lines, target, placeholder}` |
| GET | `/api/v1/view/blame?path=&at=` | `{path, at, binary, own_links, own_time, t, lines: [{n, text, owner, seq, link, intent, live, voter, member, expires_at, links_left}]}` (links only; 400 for a proposal) |
| GET | `/api/v1/view/diff/<id>` | `{kind (link / proposal), push, author, available, why, ops (the signed §5.3 ops), files: [{op (add / modify / delete / rename / mode), path, src, binary, old_mode, new_mode, old_size, new_size, added, removed, hunks, placeholder}], unified, ...}` |
| GET | `/api/v1/view/thread/<id>` | `{head{id, type (link / proposal / task / claim / escalation / message), ...}, messages: [{id, seq, received_at, kind, author, from, subject, signed, text, message_id, headers{target, task, revises, ...}, evidence}]}` |
| GET | `/api/v1/view/needs?member=` | `{member, items}` from `gud.wc.review.needs` (default: the first human UI signer) |
| GET | `/api/v1/view/host?limit=50` | `{repo, key, repos, head, processed_seq, entries, counts, tail[{seq, type, time, hash, id, kind, author, summary, fields}], handed_over, missing_mail, last_error, next_wakeup, intake, dispatch, deliveries[{handle, channel, url, journal_head, acked}], feed_head, audit{ok, exit_status, log_ok, log_error, checked_at, mail_entries, seen_mismatches, fork_proofs, late_votes, missing_mail, altered, details}}` |
| GET | `/api/v1/view/events?after=&limit=` | `{events: [{n, id, type, at, concerns, data}], head}` |
| GET | `/api/v1/view/stream?after=&follow=&max=` | SSE: `id` / `event` / `data` per feed item (§19.1 JSON with `repo`), `event: head` frames, keepalive comments; `Last-Event-ID` resumes |
| POST | `/api/v1/view/act/task` | loopback; `{title, description?, requester?, accept?, accept_cmds?, bug?, as?}` → `{id, kind, as, logged, events}` |
| POST | `/api/v1/view/act/vote` | loopback; `{kind (approve / object / veto / hold), target, text?, as?}` |
| POST | `/api/v1/view/act/comment` | loopback; `{target, text, task?, verdict? ("reject"), as?}` |
| POST | `/api/v1/view/act/admit` | loopback; `{join, text?, as?}` → also `member` |

Ids in paths and queries: a full id, a hex prefix of 4 or more (links, pushes, candidates, tasks, claims, escalations, then logged messages), a task alias (`T3`, `T3.1`) or `tip`. Errors: 400 malformed or ambiguous, 401 unsigned from outside loopback, 403 an action from outside loopback or a signer that may not do it, 404 unknown, 409 no usable signer or no genesis.

---

## `gud/wc/` (working copy)

```python
# gud/wc/remote.py: member-side client of the host mirror (§21), and local host mode
class RepoClient:
    def __init__(self, host_url: str, handle: Handle, private_raw: bytes, *, send_via: Mailbox | None,
                 repo_address: str, home: Path, http: "httpx.Client | None" = None, clock=time.time,
                 mirror: "Mirror | None" = None, trusted_host_keys: Sequence[str] = ()) -> None
        # send_via None: read-only. host_url "local:<dir>" is local host mode (§21), "http(s)://..." a mirror
    def info(self) -> dict
    def sync_log(self) -> ChainDelta                 # fetch log + missing mail, feed the local Replayer, save
    def replayer(self) -> Replayer
    def chain(self) -> Chain
    def reader(self) -> ChainReader
    def seen(self) -> Seen                           # current Gud-Seen to sign with
    def send(self, env: Envelope, *, summary: str, text: str = "", in_reply_to: Hex | None = None,
             blobs: Sequence[bytes] = ()) -> Hex     # sends blobs first, then the message; returns gud id
    def sign_and_send(self, kind: str, headers: Mapping[str, str], records: Sequence[Record] = (),
                      *, summary: str, in_reply_to: Hex | None = None, text: str = "", blobs=()) -> Hex
    def fetch_object(self, sha256: Hex) -> bytes
    def fetch_snapshot(self, sha256: Hex) -> bytes
    def set_delivery(self, delivery: "Delivery | Mapping") -> None   # a Delivery-like (channel, url) or a §21 body
    def bootstrap(self, *, checkpoint: bool = True) -> Literal["cache", "checkpoint", "genesis"]
    def rebuild(self, *, from_checkpoint: bool = True, upto=None, store=None) -> Replayer
    def snapshot_at(self, seq: int) -> bytes; def recommended_checkpoint(self) -> int | None
    def budget(self) -> int                          # §6 B for this sender and host
    def build_signed(self, kind, headers, records=(), *, key=None, author=None, seen=True) -> Envelope
    def member(self, handle: Handle | None = None) -> Member | None
    def resolve(self, ref: str) -> Hex               # full id, hex prefix >= 4, T3/T3.1, tip, [gud:<hex>], gud:<hex>,
                                                     # <id>, a logged message's <Message-ID>
    def log_entries(self, since: int, limit: int) -> list[LogEntry]   # what the mirror serves now (unverified)
    def raw(self, gud_id: Hex) -> bytes | None
    def now_ms(self) -> Ms; def reset_cache(self) -> None; def trust_host_key(self, key: str) -> None
    on_delta: list[Callable[[ChainDelta], None]]     # called with every delta sync_log produces
    host_url; handle; repo; repo_address; home; clock; mirror; cache_dir; objects; store; trusted_host_keys
    private_raw; http; last_problems
    # repo is the address the cache is named after; repo_address the current address of the verified log (§11.6)

class RemoteError(RuntimeError): ...                 # mirror unreachable or refusing; served data that fails
                                                     # verification (including host log rollback or fork)
class Mirror(Protocol):                              # the §21 calls as plain methods; None for 404
    def info(self); def log(self, since, limit); def mail_batch(self, since, limit); def mail(self, id)
    def object(self, sha); def snapshot(self, sha); def delivery(self, body); def pull(self)
class HttpMirror:                                    # implements Mirror over HTTP, signing every request but /info
    def __init__(self, base_url, handle, private_raw, *, http=None, clock=time.time, timeout=30.0) -> None
class LocalHost:                                     # local host mode (§21): intake pull() + the Mirror calls
    def __init__(self, root: Path, *, mailbox=None, clock=time.time, store=None) -> None
    @classmethod
    def create(cls, root, *, repo, transport, cfg, host_private=None, bugs=None, objects=None, clock=time.time,
               mailbox=None) -> LocalHost
    @classmethod
    def install(cls, root, *, log, transport, cfg, host_private, bugs=None, objects=None) -> LocalHost
    host_key; def mailbox(self); def delivery(self, body, handle=None)
    def handover(self, new_public: str, *, repo: str | None = None) -> LogEntry
    def served(self) -> bool                         # a `gud host serve` holds this directory
def open_mirror(host_url, handle, private_raw, *, http=None, clock=time.time, mailbox=None) -> Mirror
def client_from_home(home, handle=None, *, host_url=None, repo=None, key=None, clock=time.time) -> RepoClient
class FetchingStore:                                 # ObjectStoreLike fetching missing objects by hash (checked)
    def __init__(self, local, fetch) -> None
def snapshot_at(entries, mail, seq, store, trusted_host_keys=()) -> bytes   # wraps core.replay.snapshot_at
# mirror auth: HANDLE_HEADER, TIME_HEADER, SIG_HEADER, MAX_SKEW = 300, format_time, request_bytes, sign_request,
# verify_request are gud.events.dispatch's
# keys and config
def encode_private_key(raw: bytes) -> str; def decode_private_key(text: str) -> bytes
def key_path(home, handle) -> Path; def save_key(home, handle, raw, *, name="key", overwrite=False)
def load_key(home, handle, *, name="key") -> bytes; def repo_slug(repo: str) -> str
def write_transport_toml(path, name, cfg) -> None; def now_ms(clock) -> Ms

# gud/wc/review.py (imports core only): what needs a member (`gud review`, the MCP inbox)
LEASE_WARN_MS = 300_000; WHY_TEXT: Mapping[str, str]
def needs(chain, me, *, members, now, tip_paths=None, lease_warn_ms=LEASE_WARN_MS) -> list[dict]
def needs_of(client: RepoClient) -> list[dict]
def why_codes(prop, me, member) -> list[str]         # reasons in the review.requested vocabulary
def why_text(code: str) -> str; def my_claims(chain, me) -> list[ClaimState]
def declare_hints(chain, me) -> list[dict]; def declare_text(item: dict) -> str   # the `declare` need (§16.6)

# gud/wc/ignore.py
GUDIGNORE = ".gudignore"
DEFAULT_GUDIGNORE: str                               # §22 default list, used when neither side has a .gudignore
ARTIFACT_PATTERNS: tuple[str, ...]                   # build output a push refuses without --allow-artifacts
def artifacts(paths) -> list[str]; def ignore_lines(paths) -> list[str]; def builtin_ignored(path: str) -> bool
class IgnoreRules:
    @classmethod
    def load(cls, root: Path, tree: Tree | None = None) -> IgnoreRules   # .gudignore + built-ins (§22); defaults if none
    @classmethod
    def parse(cls, text: str) -> IgnoreRules
    patterns: tuple
    def ignored(self, path: str, is_dir: bool = False) -> bool

# gud/wc/secrets.py
PATTERNS: tuple[tuple[str, "re.Pattern[bytes]"], ...]   # (name, regex) §22 list: pem-private-key, openssh-private-key,
    # gud-private-key, aws-access-key-id, github-token, github-pat, slack-token, anthropic-key, api-key
@dataclass(frozen=True)
class SecretHit:
    path: str
    line: int
    pattern: str
def scan(files: Mapping[str, bytes], allow: Iterable[str] = ()) -> list[SecretHit]

# gud/wc/copy.py
BASE_FILE = ".gud/BASE"; LOCAL_DIR = ".gud/local"
class WcError(RuntimeError): ...
class SecretsFound(WcError): ...                     # .hits
class ArtifactsFound(WcError): ...                   # .paths
@dataclass(frozen=True)
class Status:
    base: Hex
    tip: Hex
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    mode_changed: tuple[str, ...]
    behind: int                                      # links between base and tip
    conflicts: tuple[str, ...] = ()                  # recorded by the last failed sync
    pushes: tuple[tuple[Hex, str], ...] = ()
    @property
    def clean(self) -> bool
    def to_json(self) -> dict
@dataclass(frozen=True)
class SyncResult:
    ok: bool
    new_base: Hex
    conflicts: tuple[str, ...]
    written: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()                    # .gud/local/conflicts/<path> files written
    def to_json(self) -> dict
@dataclass(frozen=True)
class PushResult:
    push: Hex
    parent: Hex
    ops: int
    blobs: int
    forecast: ConsentPlan | None
    files: tuple[str, ...] = ()
    def to_json(self) -> dict

class WorkingCopy:
    def __init__(self, root: Path, client: RepoClient) -> None
    @classmethod
    def clone(cls, client: RepoClient, root: Path, *, at: Hex | None = None, checkpoint: bool = True) -> WorkingCopy
    @classmethod
    def adopt(cls, client: RepoClient, root: Path, base: Hex) -> WorkingCopy   # an existing directory after `gud init`
    @property
    def base(self) -> Hex                            # reads .gud/BASE
    def working_tree(self) -> Tree                   # files minus ignored, byte-exact, modes from the filesystem
    def status(self) -> Status
    def diff(self, paths: Sequence[str] = ()) -> str
    def push(self, intent: str, *, task: Hex | None = None, revises: Hex | None = None, carry: bool = True,
             allow_secret: Sequence[str] = (), allow_artifacts: bool = False) -> PushResult
        # §22 refusals first (behind on touched paths, config, artifacts, secrets); raises WcError
    def sync(self) -> SyncResult                     # three_way; writes nothing on conflict
    def fast_forward(self) -> bool                   # §22 base fast-forward
    def verify(self, *, from_checkpoint: bool = True) -> list[str]   # differences; empty = ok
    def materialize(self, tree: Tree) -> None        # byte-exact, no writes through symlinks
    warnings: list[str]

def build_push(client, parent, before, after, intent, *, kind="push", headers=None) -> tuple[Envelope, list[bytes], tuple]
def create_genesis(client, files, intent="genesis") -> Hex
def push_tree(client, parent, after, intent, *, headers=None) -> Hex
def verify_cache(client, *, from_checkpoint=True, base=None) -> list[str]
def conflict_markers(base: bytes, mine: bytes, theirs: bytes, tip_label: str = "tip") -> bytes
def find_root(start: Path) -> Path | None; def read_local_config(root) -> dict; def write_local_config(root, data)
```

The wc tests run against the real host (`gud.host.open_host` over a `FakeMailbox` root, `tests/v1/wc/conftest.py`); there is no host test double.

---

## `gud/cli.py`

The `gud` console script (`gud = "gud.cli:main"`). `main(argv: list[str] | None = None) -> int`; `build_parser()`. Every subcommand is `cmd_<name>(args: argparse.Namespace) -> int` (nested ones `cmd_<group>_<name>`, e.g. `cmd_task_new`, `cmd_host_init`, `cmd_keys_rotate`, `cmd_mirror_git`). Global flags: `--home PATH` (`GUD_HOME`), `--handle @x` (`GUD_HANDLE`), `--json`, `-C DIR`, `--offline`. Context, output and exit codes: SPEC Appendix A.5.

| Command | Arguments |
| --- | --- |
| `gud keys new` / `show` / `rotate` / `promote` | `new [--address A] [--transport N] [--set K=V]... [--force]`; `rotate` sends a members push with a pending key, `promote` installs it once the tip lists it |
| `gud host init` | `--repo ADDR --transport NAME [--bugs ADDR] [--set K=V]... [--dir D]` |
| `gud host serve` | `[--dir D] [--repo R] [--bind HOST:PORT] [--webhook-bind HOST:PORT] [--ui-home GUD_HOME]... [--ui-dist DIR] [--no-ui] [--no-ui-actions]`; the ready line reports `ui`, `ui_dist` and `ui_signers` |
| `gud host key` / `handover` / `line` / `follow` / `trust` | `key --dir D` (new host); `handover KEY [--new-repo ADDR] [--dir D]` (old host, not while served); `line` (members push for the host line); `follow URL`; `trust KEY` (§11.6 unplanned handover) |
| `gud delivery` | `--webhook URL \| --sse \| --email` (§19.3) |
| `gud doctor` | `[--transport N] [--set K=V]... [--no-send] [--timeout S]` |
| `gud init` | `[DIR] --repo ADDR --member-file FILE [--from-git GIT_DIR] [--ref R] [--host-url URL] [--transport N] [--set K=V]... [-m INTENT]` |
| `gud import` | `GIT_DIR [--ref main] --member-file FILE [--dir D] [--host-url URL]` (same as `init --from-git`) |
| `gud join` | `HOST_URL --handle @x --address ADDR [--role agent] [--sponsor @h] [--reason TEXT] [--transport N] [--set K=V]...` |
| `gud admit` | `JOIN [-m TEXT]` (the membership push answering a join request) |
| `gud clone` | `HOST_URL [DIR] [--at STATE] [--no-checkpoint]` |
| `gud status`, `gud diff` | `[PATH...]` |
| `gud push` | `-m INTENT [--task T] [--revises PUSH] [--no-carry] [--allow-secret PATH]... [--allow-artifacts]` |
| `gud sync` | |
| `gud log` | `[-n N]` |
| `gud blame` | `PATH [--at STATE] [--full]` |
| `gud show` | `ID` (push, link, task, claim, message; aliases like `T3.1` accepted) |
| `gud verify` | `[--from-genesis]` |
| `gud review` | (pending items that need me) |
| `gud approve` / `gud object` / `gud veto` / `gud hold` | `TARGET [-m TEXT]`; `object`/`veto` also `[--test FILE [--test-path P]]` or `--evidence P:FILE` |
| `gud comment` | `TARGET -m TEXT [--task T] [--escalate budget\|stuck] [--verdict reject]` |
| `gud task new` / `list` / `show` | `new TITLE [-m DESC] [--accept PATHS] [--accept-cmd CMD]... [--requester @h]`; `list [--all]`; `show ID` |
| `gud tasks` | `[--all]` |
| `gud claim` | `TASK --files GLOBS [--after IDS]`; `gud claim update CLAIM --files GLOBS \| --done`; `gud release CLAIM [--done]` |
| `gud subtask` | `PARENT TITLE [-m DESC] [--after IDS] [--files GLOBS] [--accept ...]` |
| `gud no-change` | `TASK -m REASON` |
| `gud revert` | `LINK [-m TEXT]` |
| `gud checkpoint` | `[--at-log N]` (sign a checkpoint at the recommended position) |
| `gud mirror git` | `REMOTE [--branch gud] [--workdir D]` |
| `gud agent` / `gud mcp` | run `gud.agent.runtime.main` / `gud.mcp.main` with the global `--home`, `--handle` (and `--wc` inside a working copy, `--json` for mcp) followed by the command's own options, which pass through |
| `gud tester` | `[--runner local\|docker\|instabox] [--parallel N] [--interval S] [--once] [--host-url URL] [--repo R]` |
| `gud export-archive` / `gud import-archive` | `OUT [--host [--dir D]]` / `ARCHIVE [--repo ADDR] [--dir D] --transport N [--set K=V]... [--trust-unplanned]` |
| `gud audit` | `[--evidence FILE]`; exits with the report's status |

Tests: `tests/v1/procs.py` (subprocess helpers: `gud`, `Proc`, `serve`, `wait_for`); the CLI suite runs in local host mode and against `gud host serve`.

---

## `gud/mcp.py`

One server per member session (SPEC Appendix A.7). Does not import `gud.cli`.

```python
TOOL_NAMES: tuple[str, ...]          # status clone sync diff push verify inflight tasks task task_new claim claim_update
                                     # release subtask no_change comment approve object hold inbox wait
                                     # read ls blame log thread search forecast
INSTRUCTIONS: str
class ToolError(Exception)
class GudTools:
    def __init__(self, client: RepoClient, wc_root: Path | str | None, *, json: bool = False,
                 events: MemberEvents | None = None, log_wait_s: float | None = None,   # default $GUD_WAIT or 10
                 poll_s: float = 1.0, sleep=time.sleep) -> None
    me: Handle; wc_root: Path | None      # every tool returns a str: text, or JSON when json=True ({"error": ...} on failure)
class MemberEvents:                       # GUD_HOME/members/<handle>/events.jsonl
    def __init__(self, home: Path, handle: Handle) -> None
    def record(self, events) -> list[tuple[int, Event]]   # only events concerning the member; dedupe by id
    def head(self) -> int; def since(self, n: int) -> list[tuple[int, Event]]; def all(self)
def journal_deltas(client: RepoClient, journal: MemberEvents) -> None   # wrap client.sync_log so every delta is journaled
def open_member(home=None, handle=None, wc=None, *, clock=time.time) -> tuple[RepoClient, Path | None]
def describe_event(event: Event, chain: Chain | None = None, me: Handle | None = None) -> str
def explain_proposal(prop: ProposalState, now: Ms) -> str
def is_echo(event: Event, me: Handle) -> bool                      # own claim.won / linked
ARTIFACT_PATTERNS: tuple[str, ...]; def artifacts(paths) -> list[str]
def build_server(tools: GudTools) -> "mcp.server.mcpserver.MCPServer"   # FastMCP on mcp < 2
def main(argv: list[str] | None = None) -> None   # python -m gud.mcp [--home H] [--handle @x] [--wc DIR] [--json]
```

Ids accept full ids, hex prefixes of 4 or more, aliases, `tip`, `base`, `<...>`, `[gud:<hex>]` and Message-ID-like `<hex@host>`.

| Tool | Arguments |
| --- | --- |
| `clone` | `dir: str \| None = None, at: str \| None = None` |
| `status` | — |
| `sync` | — |
| `verify` | — |
| `diff` | `paths: list[str] = []` |
| `push` | `intent: str, task=None, revises=None, carry: bool = True, edits=None, base=None, allow_artifacts: bool = False`: the working copy's diff, or `edits` (`{path, content \| content_b64, mode?}`, `{path, delete: true}`, `{path, rename_to}`) for members without a filesystem |
| `read` | `path: str, at: str \| None = None` (a link or an in-flight push) |
| `ls` | `prefix: str = "", at: str \| None = None` |
| `blame` | `path: str, at: str \| None = None` (links only) |
| `log` | `limit: int = 20, path: str \| None = None` |
| `thread` | `id: str` |
| `inflight` | `paths: list[str] = [], include_mine: bool = False` (open proposals with diffs, claims and threads) |
| `inbox` | — (what needs me: reviews, objections on my pushes, escalations, lapsing and overlapping claims, `declare` hints; then new journal entries) |
| `wait` | `timeout: float = 60, types: list[str] = [], push: str \| None = None` |
| `search` | `query: str, regex: bool = False, at: str \| None = None, ignore_case: bool = False, glob: str \| None = None, limit: int = 500` |
| `tasks` | `all: bool = False` |
| `task` | `id: str` |
| `task_new` | `title: str, description: str = "", accept_paths: list[str] = [], accept_cmds: list[str] = []` |
| `claim` | `task: str, files: list[str], after: list[str] = [], note: str = ""` |
| `claim_update` | `claim: str, files: list[str] \| None = None, done: bool = False, whole: bool = False` |
| `release` | `claim: str, note: str = "", done: bool = False, whole: bool = False` |
| `subtask` | `parent: str, title: str, description: str = "", after: list[str] = [], files: list[str] = [], accept_paths: list[str] = [], accept_cmds: list[str] = []` |
| `no_change` | `task: str, reason: str` |
| `approve` / `hold` | `target: str, text: str = ""` |
| `object` | `target: str, reason: str, evidence_path: str \| None = None, evidence_content: str \| None = None, evidence_file: str \| None = None` (no `veto` tool) |
| `comment` | `target: str, text: str, task: str \| None = None, escalate: str \| None = None` |
| `forecast` | `paths: list[str]` (owners and rules a change would need) |

---

## `gud/tester.py` and `gud/runners/`

The `Runner` protocol and `RunResult` live in `gud.runners.base`; `gud.tester` re-exports `Runner`, `RunResult`, `RunnerUnavailable`, `RunnerError`, `LocalRunner` and `DockerRunner`. Behaviour: SPEC §15.2 and Appendix A.3.

```python
# gud/runners/base.py (may import core.types and core.objects)
class Runner(Protocol):
    def run(self, files: Mapping[str, tuple[Mode, bytes]], command: str, *, image: str | None,
            timeout_ms: int, extra_args: Sequence[str] = ()) -> RunResult: ...
@dataclass(frozen=True)
class RunResult:
    passed: bool
    log: str
    seconds: float
    runner: str
    timed_out: bool = False
    exit_code: int | None = None
class RunnerUnavailable(RuntimeError): ...           # nothing is signed for such a run
class RunnerError(ValueError): ...
LOG_LIMIT = 65536
def materialize(root, files) -> None                 # byte-exact; symlinks made as symlinks, never written through
def tree_tarball(files) -> bytes                     # stable gzipped tar with modes and symlinks
def shell_command(command, extra_args) -> str; def child_env(extra_path=()) -> dict
def run_process(argv, *, cwd, timeout_ms, env=None, on_timeout=None) -> tuple[int, bytes, bool, float]
    # (exit, output, timed_out, seconds): runs in a new session, kills the process group on timeout and after exit
def tail(data, limit) -> str; def result_log(...) -> str

# gud/runners/local.py, docker.py, instabox.py
class LocalRunner:  def __init__(self, *, shell="/bin/sh", extra_path=(), tmp_root=None); runs: int
class DockerRunner: def __init__(self, *, docker="docker", network="none", user="auto", fallback=None, extra_args=(),
                                 tmp_root=None); def available(self); def argv(self, workdir, command, image, name, extra_args)
    # a policy without an image runs on `fallback` (local by default)
class InstaBoxRunner: def __init__(self, api_key=None, template="universal", snapshot=None, *, api_url, max_machines=5,
                                   workdir, after="pool" | "destroy", bootstrap=DEFAULT_BOOTSTRAP)
                      def close(self); def test_command(self, command, image, extra_args)
class InstaBoxError(RuntimeError): ...

# gud/tester.py
def make_runner(kind: Literal["local", "docker", "instabox"] = "local", **kw) -> Runner
@dataclass(frozen=True)
class TesterIdentity:
    handle: Handle
    private_key: bytes
    @property
    def public_key(self) -> str
class TesterClient(Protocol):                        # the subset of gud.wc.remote.RepoClient the tester uses
    def sync_log(self) -> ChainDelta; def replayer(self) -> Replayer; def seen(self) -> Seen
    def send(self, env: Envelope, *, summary: str, text: str = "") -> Hex
class LocalClient:                                   # implements TesterClient over plain callables
    def __init__(self, replayer, fetch, send, *, now=None) -> None   # fetch() -> (emails, log entries)
def mailbox_sender(mailbox, *, from_addr: str, repo: str) -> Callable   # mails envelopes to the repo (§2.1 layout)

class Tester:
    def __init__(self, client: TesterClient, runner: Runner, *, identity: TesterIdentity, parallel: int = 1,
                 clock: Callable[[], float] = time.time, state_dir: Path | None = None) -> None
        # state_dir/tester.json: sent results (persisted before sending), run cache, tip-cadence reference,
        # auto-revert ids. Without it, the log scan alone gives idempotence for results that reached the log.
    def step(self) -> list[Hex]                      # one pass; re-sends persisted-but-unsent results; returns ids sent
    def run_forever(self, events: Iterator[Event] | None = None, *, interval: float = 5.0, stop=None) -> None
        # one step per event, or every `interval` s; survives RuntimeError/OSError/ValueError from a step
    # individual duties (each sends one `test`, returns its id or None)
    def test_push(self, candidate: Hex) -> Hex | None                    # on-paths / every-link single
    def test_stack(self, base: Hex, pushes: Sequence[Hex]) -> Hex | None # §15.3; bisects prefixes on fail, signs each probe
    def test_tip(self, link: Hex) -> Hex | None                          # on-tip cadence
    def bisect(self) -> list[Hex]                                        # §15.4
    def test_evidence(self, objection: Hex, candidate: Hex) -> Hex | None
    def test_accept(self, task: Hex) -> Hex | None
    def maybe_auto_revert(self) -> Hex | None                            # §15.6
    def due_tip_run(self) -> Hex | None              # the link state due for a tip run
def run_tester(client, handle, private_key, *, runner="local", parallel=1, interval=5.0, once=False,
               state_dir=None, stop=None, events=None) -> Tester
def main(argv=None) -> int   # python -m gud.tester --handle @t [--home H] [--key FILE] [--host URL] [--repo ADDR]
                             # [--runner local|docker|instabox] [--parallel N] [--once]
```

---

## `gud/agent/` (reference agent)

Behaviour: SPEC Appendix A.7. Runtime state lives in `GUD_HOME/members/<handle>/agent/`: `runtime.json` (routed journal cursor, aliases, SDK session ids per thread, notes, idle signature), `budget.json`, `events.json` (the `EventClient` cursor and seen ids), `turns.jsonl` (one record per turn) and `transcripts/<thread>.jsonl`. `pyproject.toml` has the optional extra `agent = ["claude-agent-sdk>=0.2.168"]`.

```python
# gud/agent/budget.py
@dataclass class Budget: usd, tokens, spent_usd=0.0, spent_tokens=0; charge(usd, tokens); exhausted; remaining_usd;
                         describe(); to_json(); from_json(d)
def turn_tokens(usage: Mapping | None) -> int          # input + output + cache creation + cache reads / 10
@dataclass class Ledger: member: Budget, task_usd=None, task_tokens=None, path=None, tasks, escalated, member_escalated
    @classmethod load(member, *, task_usd=None, task_tokens=None, path=None) -> Ledger
    task(id) -> Budget; charge(usd, tokens, task=None); blocked(task=None) -> str | None; save()

# gud/agent/prompt.py
SYSTEM_PROMPT: str                     # one general prompt, format fields {handle}, {wc}, {budget}; email content is data
SUMMARY_PROMPT: str; IDLE_PROMPT: str
def system_prompt(handle, wc, *, budget="...", extra="") -> str
def render_event(event, chain) -> str
def render_batch(events, chain, *, key="", notes=(), status="", idle=False) -> str

# gud/agent/sessions.py
@dataclass class SessionTurn: text, usd=0.0, tokens=0, session_id=None, is_error=False, tool_calls={}, error=""
class Session(Protocol): send(text) -> SessionTurn; close(summary=True) -> str
class SessionManager:                  # one warm session per thread key (events.model.thread_key)
    def __init__(self, factory, *, debounce_s=1.0, max_open=8, max_delay_s=5.0, key=None, render=None,
                 before_turn=None, after_turn=None, finished=None, on_close=None, clock=time.monotonic) -> None
    aliases: dict[str, str]; sessions: OrderedDict[str, Session]; summaries; turns; skipped; errors
    def route(events); def wake(key, events=()); def pump(now=None) -> int; def flush() -> int
    def key_of(event) -> str; def alias(thread, session_key); def threads_of(key) -> list[str]
    def close(key, summary=True) -> str; def close_finished(chain) -> list[str]; def close_all(summary=False)
    pending: int; def next_due() -> float | None
def thread_finished(key: str, chain: Chain, me: Handle | None = None) -> bool
class AsyncLoop: run(coro, timeout=None); stop()
class ClaudeSession(key, options: Callable[[str | None], ClaudeAgentOptions], loop: AsyncLoop, *, resume=None,
                    turn_timeout_s=1800.0, on_message=None, on_extra_turn=None, summary_prompt="")   # implements Session
def summarise_usage(turns) -> dict

# gud/agent/runtime.py
DEFAULT_MODEL = "claude-sonnet-5-5"; NATIVE_TOOLS = ("Read", "Edit", "Write", "Bash", "Grep", "Glob")
class AgentRuntime:
    def __init__(self, client, wc, *, model=DEFAULT_MODEL, budget: Budget, listen=None, task_budget: Budget | None = None,
                 session_factory=None, state_dir=None, debounce_s=1.0, max_open=8, poll_s=2.0, idle_s=300.0,
                 advertise=None, max_turns=80, turn_timeout_s=1800.0, extra_prompt="", deny_paths=(),
                 mcp_env=None, clock=time.time, monotonic=time.monotonic, log=None) -> None
    def run(self, stop: threading.Event) -> None       # webhook receiver (listen) or SSE; polling as well
    def on_events(self, events) -> None                # journal, then route new journal entries
    def claude_session(self, thread_key: str) -> Session   # Claude Agent SDK; cwd = working copy; tools = native + gud.mcp
    def step(self, *, poll=True) -> int                # one loop pass; turns run
    def accept_webhook(self, headers, body) -> int     # HTTP status
    def idle_check(self, *, force=False, startup=False) -> bool
    def quiet(self, event) -> bool; def options(self, resume=None) -> ClaudeAgentOptions; def mcp_server_config(self) -> dict
    ledger: Ledger; journal: MemberEvents; events: EventClient; manager: SessionManager; stopped_reason: str | None
def main(argv: list[str] | None = None) -> int   # gud agent --handle @x [--home H] [--wc DIR] [--listen [HOST]:PORT]
    # [--advertise URL] [--model M] [--budget-usd N] [--budget-tokens N] [--task-budget-usd N] [--task-budget-tokens N]
    # [--debounce S] [--max-open N] [--poll S] [--idle S] [--max-turns N] [--deny-path P]...
```

---

## `gud/gitbridge.py`

```python
class GitBridgeError(ValueError): ...
class MirrorDivergence(GitBridgeError): ...          # remote branch does not continue the chain; nothing written

DEFAULT_CONFIG: bytes; DEFAULT_TEST: bytes; DEFAULT_OWNERS: bytes   # written when the git tree has none

@dataclass(frozen=True)
class ImportPlan:
    files: Mapping[str, tuple[Mode, bytes]]          # byte-exact from the git tree, incl. .gud/members from the roster
    source_commit: str
    intent: str                                      # "gud import: git <ref> at commit <id>"
    ref: str = "main"
    warnings: tuple[str, ...] = ()                   # skipped submodules / paths, replaced .gud/members, ...
    members: Members | None = None                   # parsed .gud/members (draft for review)
    def tree(self, store: ObjectStoreLike) -> Tree
def parse_roster(data: bytes, host_key: str) -> bytes   # roster lines "handle role address [pubkey] [sponsor]" -> .gud/members
def import_git(git_dir: Path, *, ref: str = "main", roster: Path, host_key: str, git: str = "git") -> ImportPlan
def genesis_from_plan(plan: ImportPlan, author: Handle, private_raw: bytes, seen: Seen,
                      store: ObjectStoreLike, inline_budget: int, *, sent: int | None = None
                      ) -> tuple[Envelope, list[bytes]]  # (signed genesis push, raw contents of its blob-reference puts)

class GitMirror:
    """One-way, append-only, never force-pushes. Progress = the remote head's Gud-Link trailer."""
    def __init__(self, remote: str, workdir: Path, store: ObjectStoreLike, *, branch: str = "gud",
                 committer: tuple[str, str] = ("gud mirror", "mirror@gud.invalid"),
                 client: RepoClient | None = None, git: str = "git") -> None
    def mirror(self, links: Iterable[LinkState] | Chain) -> int   # chain order, genesis first; returns commits pushed
    def run_once(self) -> int                        # client.sync_log(), then mirror(client.chain().links)
    def run_forever(self, stop: threading.Event, *, interval: float = 5.0) -> None   # stops on MirrorDivergence
    def last_mirrored(self) -> Hex | None            # link state named by the remote head, None if no branch
def commit_message(link: LinkState) -> str           # intent + "\n\nGud-Link: <state>\nGud-Tree: <tree hash>\n"
def parse_trailers(message: str) -> tuple[Hex, Hex] | None   # (state, tree hash) from a mirror commit message
```

`mirror` is the engine; it needs only links and the object store holding their trees, so it works with `Chain.links` from any replay. `run_once`/`run_forever` are the `gud mirror git` loop over a `RepoClient`.

---


## `gud/ops/` (security and operations)

Behaviour: SPEC §21 (keys), §23 (archives, audit). Ops relies on small structural surfaces rather than `Intake` and `RepoClient`: `Intake.log_entries`/`raw` satisfy `ArchiveSource`, and `RepoClient` needs `handle`, `reader()`, `sign_and_send(...)` and, for audit, `log_entries(since, limit)`/`raw(gud_id)` that fetch from the mirror (`GET /api/v1/log`, `GET /api/v1/mail/<id>`).

```python
# gud/ops/archive.py
FORMAT = "gud-archive/1"; MANIFEST, LOG_FILE, MBOX_FILE, SNAPSHOT_FILE
class ArchiveError(ValueError)
class ArchiveSource(Protocol): log_entries(since, limit) -> list[LogEntry]; raw(gud_id) -> bytes | None
def read_log(source) -> list[LogEntry]               # paged log_entries, or a client's local replayer().log()
def read_mail(source, gud_id) -> bytes | None        # raw(), or reader().email(id).raw_mime
def export_archive(source, out: Path, *, signer: bytes | None = None, trusted_keys=()) -> Path   # dir or .tar
def verify_archive(path, *, host_key: str | None = None, trusted_keys=(), replay: bool = True) -> ArchiveReport  # never raises
@dataclass(frozen=True) class ArchiveReport: problems, log_ok, log_error, entries, messages, missing, altered,
                                             out_of_order, signer, snapshot, tip; ok (property)
@dataclass(frozen=True) class Archive: manifest, entries, mail (id -> raw, log order), snapshot
def load_archive(path, *, trusted_keys=(), host_key=None) -> Archive          # verifies; raises ArchiveError
def plan_handover(log: HostLog, old_private: bytes, new_public: str, now: Ms, *, repo: str | None = None) -> LogEntry
def import_archive(archive, *, repo: str | None, host_private, mailbox=None, now=None, mail_store=None, log_path=None,
                   trust_unplanned=False) -> HostLog
    # repo None: the archived log's address, or its handover's new one; mailbox (optional) must offer deliver(raw);
    # a log_path ending in .jsonl gets the hosts' own format
def encode_log(entries) -> bytes; decode_log(data) -> list[LogEntry]
def mbox_encode(messages: Iterable[tuple[bytes, Ms]]) -> bytes; mbox_blocks(data) -> list[bytes]

# gud/ops/keys.py
class KeysError(ValueError); KEYS_DIR = "keys"; PUSH_ROLES
class KeyStore(home): path(handle, *, staged=False), has, save(handle, private, *, overwrite=False, staged=False),
                      generate(handle, *, overwrite=False) -> (private, public), load(handle, *, staged=False),
                      public, stage(handle, private=None), promote(handle) -> public, discard_staged, remove
def decode_private(text) -> bytes
def edit_members(data: bytes, handle, *, key=None, remove=False, address=None) -> bytes   # line-preserving; result must parse
def members_push(tree, parent, new_members, *, author, intent, private_raw, seen, sent, join=None) -> Envelope
def members_push_parts(tree, parent, new_members, *, intent, join=None) -> (headers, records)
class MemberClient(Protocol): handle; reader(); sign_and_send(kind, headers, records=(), *, summary, in_reply_to=None) -> Hex
def current_key(reader, handle, at=None) -> str | None
def rotate_member_key(client, *, new_private=None, keystore=None) -> tuple[Hex, bytes]   # members push; stages the new key
def recover_member_key(client, handle, new_public, *, reason="") -> Hex                  # lost-key push
def sponsor_approve(client, push, handle) -> Hex                                         # checked sponsor approve (§21)
def revoke_member(client, handle, reason) -> Hex
def host_key_push(client, new_public, *, reason="", address=None) -> Hex                 # host line members push
def rotate_host_key(log: HostLog, old_private, new_private, *, now: Ms, client=None) -> tuple[LogEntry, LogEntry, Hex | None]
    # (handover, open, push id or None); the host role cannot push (§9), so the members push needs a member client

# gud/ops/audit.py
VOTE_KINDS
@dataclass(frozen=True) class AuditReport: log_ok, log_error, seen_mismatches, fork_proofs, late_votes, missing_mail,
    altered: tuple[(Hex, why)] = (), retimed: tuple[(Hex, own_ms, log_ms)] = (), withheld: tuple[Hex] = ()
    ok, exit_status (0 clean / 1 discrepancies / 2 fork proof or bad log), to_json()
def audit(mirror, *, retained: Sequence[LogEntry] | None = None, inbox=(), trusted_keys=(), config: RepoConfig | None = None) -> AuditReport
    # retained=None means the client's local verified log; inbox items: Received, raw bytes, or (raw, received_ms)
```

---

## Test obligations (for implementers)

- `tests/v1/` holds the suite (`python -m pytest -q` runs it; `-m slow` adds the replay benchmark).
- Every `gud/core/` module ships with unit tests and golden vectors (`tests/v1/vectors/core.json`); `core.replay` additionally with:
  - **shuffle determinism**: the same emails and log in any feed order and batching give identical `Chain`, `Snapshot.hash()` and event ids, and `Replayer` equals `replay()`;
  - the protocol tests listed under "Verification" in `docs/PLAN.md` (host log, consent v1, test policy, carry-forward).
- Every adapter passes the transport conformance suite (§20.2): the checks are `tests/v1/transport/conformance.py`, one pytest parameter per transport in `tests/v1/transport/test_conformance.py`; `FakeMailbox` and `FakeNetwork` run it in CI, live accounts opt in by environment variable.
- `tests/v1/e2e/` runs the whole system as separate processes (fake transport, GreenMail, and the live Primitive smoke).
