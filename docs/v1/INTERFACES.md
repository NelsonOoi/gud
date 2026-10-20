# gud v1 Python interfaces

The contract parallel implementers code against. Wire format and rules are in `docs/v1/SPEC-v1.md` (cited as §n). Design record: `docs/v1/PLAN.md`.

## Ground rules

- **New code goes in new modules.** The v0 modules (`gud/wire.py`, `gud/types.py`, `gud/replay.py`, `gud/rules.py`, `gud/tasks.py`, `gud/patch.py`, `gud/sig.py`, `gud/mcp_server.py`, `gud/api.py`, …) stay untouched and working until the final switch-over. v1 modules MUST NOT import v0 modules, with one exception: `gud/core/v0.py` (the legacy reader adapter). v1 modules MAY copy v0 code.
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
core.v0               -> core.types, legacy gud.replay / gud.wire / gud.tasks / gud.types
transport.base        -> core.types
transport.mime        -> core.types, core.envelope
transport.auth        -> core.envelope (re-exports its Authentication-Results parser)
transport.<adapter>   -> transport.base, transport.mime, transport.auth, core.types, core.envelope
transport.registry    -> transport.base (adapters imported lazily by name)
events.model          -> core.types, core.envelope
events.dispatch       -> events.model, core.*, transport.*
events.intake         -> events.model, events.dispatch, core.*, transport.*
wc.*                  -> core.*, transport.*, events.model
host_v1               -> core.*, transport.*, events.*
tester_v1, gitbridge, ops.*  -> core.*, transport.*, events.*, wc.*
mcp_v1                -> core.*, transport.*, events.*, wc.*
agent.*               -> core.*, transport.*, events.*, wc.*, mcp_v1
cli_v1                -> everything above
```

`core` never imports `transport`, `events` or anything above. `transport` depends only on `core.types` and `core.envelope`.

Two consequences of that rule: the `Authentication-Results` parser and the event-id helpers are **implemented in `core.envelope`** (replay needs both), and `transport.auth` / `events.model` re-export them under their documented names.

Two modules are added to the requested layout because nothing listed could own them without breaking the graph: `gud/wc/remote.py` (member-side client for the host mirror) and `gud/host_v1.py` (the host process: HTTP mirror + intake + dispatch). Packages get empty `__init__.py` files that re-export nothing.

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
    # v0's sender_verified becomes core.envelope.dmarc_pass(email.auth_results, authserv_id, domain(email.from_addr))
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
    data: Mapping[str, Any]                 # JSON-compatible; ints, strs, lists, dicts, None, bools

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
    # byte-wise port of v0 mcp_server.three_way; `paths` restricts which paths are merged (others = theirs)
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
def parse_config(data: bytes | None) -> RepoConfig

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
    def handover(self, old_private: bytes, new_public: str, now: Ms) -> LogEntry
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
```

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

@dataclass(frozen=True)
class Activity:                                      # lease-renewing activity (§16.3)
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

class TaskBoard:
    """Incremental task/claim state machine. Fed in log order by core.replay."""
    def __init__(self, cfg_at: Callable[[Ms], RepoConfig], members_at: Callable[[Ms], Members]) -> None
    def add_task(self, t: TaskInput) -> list[Event]               # validates subtask split rule (§16.5)
    def add_claim(self, c: ClaimInput, tip_paths: Iterable[str]) -> list[Event]   # win / void / update / release, overlap
    def add_activity(self, a: Activity) -> None
    def add_no_change(self, n: NoChangeInput) -> list[Event]
    def on_link(self, link: LinkState, touched: Sequence[str], tip_paths: Iterable[str]) -> list[Event]  # work done, no-change drops, scope.exceeded
    def on_accept_test(self, task: Hex, link: Hex, result: Literal["pass", "fail"], at: Ms, red: bool) -> list[Event]
    def on_requester_accept(self, task: Hex, by: Handle, at: Ms) -> list[Event]
    def on_red(self, red: RedState, at: Ms, cited_author: Handle | None, touched: Sequence[str]) -> list[Event]  # opens fix task
    def on_green(self, at: Ms) -> list[Event]
    def advance(self, to: Ms) -> list[Event]          # lease lapses, offer expiry, done transitions that wait on time
    def next_time(self) -> Ms | None
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

```python
class ReplayError(ValueError): ...

@dataclass(frozen=True)
class Snapshot:
    """§17 snapshot; to_bytes() is RFC 8785 JSON."""
    version: int                                     # 1
    log_head: Seen
    t: Ms
    state: dict                                      # links, tip tree entries, live blame runs, rules bytes,
                                                     # open candidates + votes, TaskBoard.to_json(), escalations,
                                                     # red state, sorted seen ids
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
    def from_checkpoint(cls, snapshot: bytes, store: ObjectStoreLike, *, state_dir: Path | None = None) -> Replayer
    def feed(self, emails: Iterable[Email], log_entries: Sequence[LogEntry], now: Ms) -> ChainDelta
        # verify+append log entries, index emails by gud id (unlogged mail is held, not used),
        # then advance to t = replay_time(...). Idempotent for repeated inputs.
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

class ChainReader:
    """Accessors that need the object store."""
    def __init__(self, chain: Chain, store: ObjectStoreLike) -> None
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
def resolve_unsigned_target(email: Email, by_message_id: Mapping[str, Hex], ids: Sequence[Hex],
                            notify_target: Mapping[Hex, Hex]) -> Hex | None   # §8.2
def human_verb(text: str) -> tuple[Literal["approve", "accept", "object", "veto"], str] | None   # §8.3
```

---

## `gud/core/v0.py`

Legacy reader. The only v1 module allowed to import v0 code.

```python
def is_v1_message(email: Email) -> bool                        # envelope present with Gud-Version: 1
def is_v0_mailbox(emails: Sequence["gud.types.Email"]) -> bool # no host log and v0 trailers present
@dataclass(frozen=True)
class V0Chain:
    chain: "gud.replay.Chain"                        # unchanged v0 result
    links: tuple[LinkState, ...]                     # adapted: tree_root/blame_root into `store`
    tip_state: Hex | None
def read_v0(emails: Sequence["gud.types.Email"], window_seconds: float, now: float,
            store: ObjectStoreLike) -> V0Chain       # wraps gud.replay.replay; read-only
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
    def parse(self, headers: Mapping[str, str], body: bytes) -> list[Received]: ...
```

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
def parse(raw: bytes) -> ParsedMime                  # never raises on bad bodies; raises MimeError only if headers unparseable
def to_email(raw: bytes, *, received_at: Ms | None = None, provider_id: str | None = None) -> Email | None
    # decodes the envelope, computes gud_id; None if it has >1 gud.env or an envelope that fails decode
```

## `gud/transport/auth.py`

```python
from gud.core.envelope import AuthResult, parse_authentication_results, dmarc_pass   # re-exported
def synthesize(authserv_id: str, dmarc: str, from_domain: str) -> str  # header value for adapters without raw A-R
```

## Adapters (`gud/transport/*.py`)

Each module exposes one class implementing `Mailbox` (and `WebhookVerifier` where marked), plus `from_config(cfg: Mapping[str, str]) -> Mailbox`.

| Module | Class | Notes |
| --- | --- | --- |
| `fake.py` | `FakeMailbox(root: Path, address: str, *, clock: Callable[[], float] = time.time)` | Local JSONL store shared by addresses under one root; `send` delivers raw bytes to every recipient with a strictly increasing `received_at`; `subscribe` via file watch or in-process callbacks. Also `FakeNetwork` (in-memory, deterministic clock, fault injection: delay, duplicate, drop, reorder) for protocol tests. |
| `imap_smtp.py` | `ImapSmtpMailbox(imap_host, imap_port, smtp_host, smtp_port, username, password, address, *, folders=("INBOX", "Junk"))` | IDLE with reconnect; cursor `"<folder>:<UIDVALIDITY>:<UID>"`; received time = INTERNALDATE; SMTP submission (RFC 6409). |
| `jmap.py` | `JmapMailbox(session_url, token, address)` | Email/query + Email/changes, EventSource push, EmailSubmission; cursor = JMAP state; received = `receivedAt`. |
| `primitive.py` | `PrimitiveMailbox(api_key, address, *, base_url=...)` + `WebhookVerifier` (HMAC) | Port of `gud/mail_primitive.py` minus inline-body handling; raw MIME required; synthesizes A-R if only JSON auth is exposed. |
| `ses.py` | `SesMailbox(region, credentials, address, *, inbound_bucket=None)` + `WebhookVerifier` (SNS signature) | Outbound SendRawEmail; inbound via SNS→webhook, raw MIME from S3. |
| `postmark.py` | `PostmarkMailbox(server_token, address)` + `WebhookVerifier` (basic auth) | Inbound JSON includes `RawEmail` when enabled (required). |
| `gmail.py` | `GmailMailbox(credentials_path, address)` + `WebhookVerifier` (Pub/Sub JWT) | `history.list` sync, `users.watch`; raw via `format=raw`; quota reported (humans and small swarms only). |

## `gud/transport/registry.py`

```python
Factory = Callable[[Mapping[str, str]], Mailbox]
def register(name: str, factory: Factory) -> None
def names() -> tuple[str, ...]                       # built-ins: fake, imap_smtp, jmap, primitive, ses, postmark, gmail
def open_mailbox(name: str, cfg: Mapping[str, str]) -> Mailbox   # lazy-imports the adapter module
def from_env(env: Mapping[str, str] | None = None) -> Mailbox    # GUD_TRANSPORT + GUD_<NAME>_* variables
def member_config(home: Path, handle: Handle) -> tuple[str, dict[str, str]]  # GUD_HOME/members/<handle>/transport.toml
```

---

## `gud/events/model.py`

```python
from gud.core.types import EVENT_TYPES, Event                              # re-exported
from gud.core.envelope import EVENT_KEYS, event_id, make_event            # re-exported
def to_json(event: Event, repo: str) -> bytes        # §19.1 body (jcs)
def from_json(body: bytes) -> tuple[str, Event]      # (repo, event); raises ValueError
def validate(event: Event) -> None                   # data matches the §19.2 schema for its type
THREAD_KEY: Callable[[Event], str]                   # session routing key: negotiation root, task id, or event id

@dataclass(frozen=True)
class Delivery:
    handle: Handle
    channel: Literal["webhook", "sse", "email"]
    url: str | None = None

@dataclass(frozen=True)
class JournalItem:
    cursor: int                                      # per-member monotonic journal seq
    event: Event
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

class DeliveryRegistry:
    def __init__(self, path: Path) -> None
    def set(self, delivery: Delivery) -> None
    def get(self, handle: Handle) -> Delivery        # default: email
    def all(self) -> tuple[Delivery, ...]

class Dispatcher:
    def __init__(self, journal: Journal, registry: DeliveryRegistry, *, repo: str, host_private: bytes,
                 send: Callable[[Outgoing], Sent], members: Callable[[], Members],
                 http: "httpx.Client | None" = None, clock: Callable[[], float] = time.time) -> None
    def publish(self, events: Sequence[Event]) -> None    # journal, then schedule delivery
    def pump(self) -> int                                 # one delivery pass (webhooks with backoff, email batches); returns sent count
    def sse(self, handle: Handle, after: int) -> Iterator[bytes]   # SSE frames: id/event/data, keepalive comments
    def notify_humans(self, events: Sequence[Event], log_has_event: Callable[[Hex], bool]) -> None  # §19.4 notify emails

class EventClient:
    """Member side: receive events by webhook or SSE, dedupe, persist cursor."""
    def __init__(self, state_path: Path, host_keys: Callable[[], Sequence[str]]) -> None
    def accept_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> Event | None  # None if bad sig or duplicate
    def stream(self, mirror: "RepoClient", handle: Handle) -> Iterator[Event]   # SSE with resume (Last-Event-ID)
    def seen(self, event_id: Hex) -> bool
    @property
    def cursor(self) -> int
```

## `gud/events/intake.py`

```python
class Intake:
    """Host-side single entry point for 'an email arrived' (§11, Phase 1b)."""
    def __init__(self, *, repo: str, mailbox: Mailbox, replayer: Replayer, log_path: Path,
                 host_private: bytes, mail_store: Path, dispatcher: Dispatcher,
                 clock: Callable[[], float] = time.time, rate_limit_per_sender: int = 120) -> None
    def on_received(self, item: Received) -> ChainDelta | None
        # parse (to_email), filter (§11.1 what gets logged), dedupe by gud id, append mail entry,
        # store raw MIME, Replayer.feed, Dispatcher.publish(delta.events). None if not logged.
    def on_webhook(self, provider: str, headers: Mapping[str, str], body: bytes) -> int
        # WebhookVerifier.verify + parse, then on_received for each; returns accepted count
    def catch_up(self) -> int                        # list() from the stored cursor; on start and after reconnect
    def tick(self) -> ChainDelta | None              # appends a tick entry when due (§11.1) and advances replay
    def run(self, stop: threading.Event) -> None     # subscribe + catch_up + tick loop + dispatcher.pump
    def log_entries(self, since: int, limit: int) -> list[LogEntry]
    def raw(self, gud_id: Hex) -> bytes | None
```

## `gud/host_v1.py` (added)

```python
def create_app(intake: Intake, dispatcher: Dispatcher, registry: DeliveryRegistry, *, repo: str,
               bugs: Mailbox | None = None) -> "fastapi.FastAPI"     # §21 endpoints
def verify_request(members: Members, method: str, path_with_query: str, body: bytes,
                   headers: Mapping[str, str], now: float) -> Handle | None   # mirror auth v1
def sign_request(handle: Handle, private_raw: bytes, method: str, path_with_query: str,
                 body: bytes, now: float) -> dict[str, str]
def serve(home: Path, *, transport: str, bind: str = "127.0.0.1:8787") -> None   # `gud host serve`
def host_init(home: Path, *, repo: str, transport: str, cfg: Mapping[str, str]) -> dict   # DNS checks, round trip, latency -> proposed W/G
```

---

## `gud/wc/` (working copy)

```python
# gud/wc/remote.py (added): member-side client of the host mirror
class RepoClient:
    def __init__(self, host_url: str, handle: Handle, private_raw: bytes, *, send_via: Mailbox,
                 repo_address: str, home: Path, http: "httpx.Client | None" = None) -> None
    def info(self) -> dict
    def sync_log(self) -> ChainDelta                 # fetch log + missing mail, feed the local Replayer, save
    def replayer(self) -> Replayer
    def chain(self) -> Chain
    def reader(self) -> ChainReader
    def seen(self) -> Seen                           # current Gud-Seen to sign with
    def send(self, env: Envelope, *, summary: str, text: str = "", in_reply_to: Hex | None = None,
             blobs: Sequence[bytes] = ()) -> Hex     # sends blobs first, then the message; returns gud id
    def sign_and_send(self, kind: str, headers: Mapping[str, str], records: Sequence[Record] = (),
                      *, summary: str, in_reply_to: Hex | None = None) -> Hex
    def fetch_object(self, sha256: Hex) -> bytes
    def fetch_snapshot(self, sha256: Hex) -> bytes
    def set_delivery(self, delivery: Delivery) -> None

# gud/wc/ignore.py
class IgnoreRules:
    @classmethod
    def load(cls, root: Path, tree: Tree | None = None) -> IgnoreRules   # .gudignore + built-ins (§22)
    def ignored(self, path: str, is_dir: bool = False) -> bool

# gud/wc/secrets.py
PATTERNS: tuple[tuple[str, "re.Pattern[bytes]"], ...]   # (name, regex) §22 minimum list
@dataclass(frozen=True)
class SecretHit:
    path: str
    line: int
    pattern: str
def scan(files: Mapping[str, bytes], allow: Iterable[str] = ()) -> list[SecretHit]

# gud/wc/copy.py
class WcError(RuntimeError): ...
@dataclass(frozen=True)
class Status:
    base: Hex
    tip: Hex
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    mode_changed: tuple[str, ...]
    behind: int                                      # links between base and tip
@dataclass(frozen=True)
class SyncResult:
    ok: bool
    new_base: Hex
    conflicts: tuple[str, ...]
@dataclass(frozen=True)
class PushResult:
    push: Hex
    parent: Hex
    ops: int
    blobs: int
    forecast: ConsentPlan

class WorkingCopy:
    def __init__(self, root: Path, client: RepoClient) -> None
    @classmethod
    def clone(cls, client: RepoClient, root: Path, *, at: Hex | None = None, checkpoint: bool = True) -> WorkingCopy
    @property
    def base(self) -> Hex                            # reads .gud/BASE
    def working_tree(self) -> Tree                   # files minus ignored, byte-exact, modes from the filesystem
    def status(self) -> Status
    def diff(self, paths: Sequence[str] = ()) -> str
    def push(self, intent: str, *, task: Hex | None = None, revises: Hex | None = None,
             carry: bool = True, allow_secret: Sequence[str] = ()) -> PushResult   # secret scan first; raises WcError
    def sync(self) -> SyncResult                     # three_way; writes nothing on conflict
    def verify(self, *, from_checkpoint: bool = True) -> list[str]   # differences; empty = ok
    def materialize(self, tree: Tree) -> None        # byte-exact, no writes through symlinks
```

---

## `gud/cli_v1.py`

`main(argv: list[str] | None = None) -> int`. Every subcommand is `cmd_<name>(args: argparse.Namespace) -> int`. Global flags: `--home PATH` (`GUD_HOME`), `--handle @x` (`GUD_HANDLE`), `--json`.

| Command | Arguments |
| --- | --- |
| `gud host init` | `--repo ADDR --transport NAME [--bugs ADDR]` |
| `gud host serve` | `[--bind HOST:PORT]` |
| `gud host trust` | `KEY` (§11.6 unplanned handover) |
| `gud doctor` | `[--transport NAME]` |
| `gud init` | `--repo ADDR --member-file FILE` (genesis from a roster) |
| `gud join` | `HOST_URL --handle @x --address ADDR [--role agent] [--sponsor @h] [--reason TEXT]` |
| `gud clone` | `HOST_URL [DIR] [--at STATE] [--no-checkpoint]` |
| `gud status`, `gud diff` | `[PATH...]` |
| `gud push` | `-m INTENT [--task T] [--revises PUSH] [--no-carry] [--allow-secret PATH]...` |
| `gud sync` | |
| `gud log` | `[-n N]` |
| `gud blame` | `PATH [--at STATE] [--full]` |
| `gud show` | `ID` (push, link, task, claim; aliases like `T3.1` accepted) |
| `gud verify` | `[--from-genesis]` |
| `gud review` | (pending items that need me) |
| `gud approve` / `gud object` / `gud veto` / `gud hold` | `TARGET [-m TEXT] [--evidence PATH:FILE]` (evidence on object only) |
| `gud comment` | `TARGET -m TEXT [--task T] [--escalate budget\|stuck]` |
| `gud task new` | `TITLE [-m DESC] [--accept PATHS] [--accept-cmd CMD]... [--requester @h]` |
| `gud tasks` | `[--all]` |
| `gud claim` | `TASK --files GLOBS [--after IDS]`; `gud claim update CLAIM --files GLOBS`; `gud release CLAIM` |
| `gud subtask` | `PARENT TITLE [-m DESC] [--after IDS] [--files GLOBS] [--accept ...]` |
| `gud no-change` | `TASK -m REASON` |
| `gud revert` | `LINK [-m TEXT]` |
| `gud checkpoint` | (sign a checkpoint at the recommended position) |
| `gud agent` | `--handle @x [--listen :PORT] [--model NAME] [--budget-usd N]` |
| `gud tester` | `--handle @t [--runner local\|docker\|instabox] [--parallel N]` |
| `gud import` | `GIT_DIR [--ref main] --roster FILE` |
| `gud mirror git` | `REMOTE [--branch gud]` |
| `gud export-archive` / `gud import-archive` | `OUT` / `IN --repo ADDR` |
| `gud keys rotate` | `[--host]` |
| `gud audit` | `[--evidence OUT]` |

---

## `gud/mcp_v1.py`

`build_server(tools: GudTools) -> "mcp.server.Server"`; `main(argv: list[str] | None = None) -> None`. `GudTools(client: RepoClient, wc_root: Path | None)`: one per session, its own key and working copy. Every tool returns a string (JSON when `json=True`). Ids accept full ids, 12-hex prefixes and aliases.

| Tool | Arguments |
| --- | --- |
| `clone` | `dir: str \| None = None, at: str \| None = None` |
| `status` | — |
| `sync` | — |
| `diff` | `paths: list[str] = []` |
| `push_working_copy` | `intent: str, task: str \| None = None, revises: str \| None = None, carry: bool = True` |
| `push` | `intent: str, edits: list[{path, content?, content_b64?, mode?, delete?, rename_to?}], task=None, base=None` (agents without a filesystem) |
| `read` | `path: str, at: str \| None = None` |
| `ls` | `prefix: str = "", at: str \| None = None` |
| `blame` | `path: str, at: str \| None = None` |
| `log` | `limit: int = 20` |
| `thread` | `id: str` |
| `inflight` | `paths: list[str] = []` (open proposals with diffs, claims and threads) |
| `inbox` | — (what needs me: reviews, objections on my pushes, comments, claim overlaps) |
| `wait` | `timeout: float = 60, types: list[str] = []` (next events for me) |
| `search` | `query: str, regex: bool = False, at: str \| None = None` |
| `tasks` | `all: bool = False` |
| `task_new` | `title: str, description: str = "", accept_paths: list[str] = [], accept_cmds: list[str] = []` |
| `claim` | `task: str, files: list[str], after: list[str] = []` |
| `claim_update` | `claim: str, files: list[str]` |
| `release` | `claim: str` |
| `subtask` | `parent: str, title: str, description: str = "", after: list[str] = [], files: list[str] = []` |
| `no_change` | `task: str, reason: str` |
| `approve` / `hold` | `target: str, text: str = ""` |
| `object` | `target: str, reason: str, evidence_path: str \| None = None, evidence_content: str \| None = None` |
| `comment` | `target: str, text: str, task: str \| None = None` |
| `forecast` | `paths: list[str]` (owners and rules a change would need) |

---

## `gud/tester_v1.py`

```python
class Runner(Protocol):
    def run(self, files: Mapping[str, tuple[Mode, bytes]], command: str, *, image: str | None,
            timeout_ms: int, extra_args: Sequence[str] = ()) -> RunResult: ...
@dataclass(frozen=True)
class RunResult:
    passed: bool
    log: str
    seconds: float
    runner: str
def runner_from_legacy(kind: Literal["local", "docker", "instabox"]) -> Runner   # wraps gud.build runners (copied, not imported)

class Tester:
    def __init__(self, client: RepoClient, runner: Runner, *, parallel: int = 1,
                 clock: Callable[[], float] = time.time) -> None
    def step(self) -> list[Hex]                      # one pass; returns ids of test messages sent
    def run_forever(self, events: Iterator[Event]) -> None
    # individual duties (each sends one `test`, returns its id or None)
    def test_push(self, candidate: Hex) -> Hex | None                    # on-paths / every-link single
    def test_stack(self, base: Hex, pushes: Sequence[Hex]) -> Hex | None # §15.3, bisects on fail
    def test_tip(self, link: Hex) -> Hex | None                          # on-tip cadence
    def bisect(self) -> list[Hex]                                        # §15.4
    def test_evidence(self, objection: Hex, candidate: Hex) -> Hex | None
    def test_accept(self, task: Hex) -> Hex | None
    def maybe_auto_revert(self) -> Hex | None                            # §15.6
    def due_tip_run(self) -> Hex | None              # cadence decision from TestPolicy.on_tip_*
```

---

## `gud/agent/` (reference agent)

```python
# gud/agent/budget.py
@dataclass
class Budget:
    usd: float | None
    tokens: int | None
    def charge(self, usd: float, tokens: int) -> None
    @property
    def exhausted(self) -> bool

# gud/agent/prompt.py
SYSTEM_PROMPT: str                                   # one general prompt; email content is untrusted data
def render_event(event: Event, chain: Chain) -> str  # the user turn for one (debounced) batch of events
def render_batch(events: Sequence[Event], chain: Chain) -> str

# gud/agent/sessions.py
class Session(Protocol):
    def send(self, text: str) -> "SessionTurn": ...
    def close(self, summary: bool = True) -> str: ...
class SessionManager:
    """One warm session per thread key (events.model.THREAD_KEY)."""
    def __init__(self, factory: Callable[[str], Session], *, debounce_s: float = 1.0, max_open: int = 8) -> None
    def route(self, events: Sequence[Event]) -> None
    def close_finished(self, chain: Chain) -> None   # summarise and close sessions whose push linked or died

# gud/agent/runtime.py
class AgentRuntime:
    def __init__(self, client: RepoClient, wc: WorkingCopy, *, model: str = "claude-sonnet-5-5",
                 budget: Budget, listen: str | None = None) -> None
    def run(self, stop: threading.Event) -> None     # webhook receiver (listen) or SSE; polling fallback
    def on_events(self, events: Sequence[Event]) -> None
    def claude_session(self, thread_key: str) -> Session   # Claude Agent SDK; cwd = working copy; tools = native + mcp_v1
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

```python
# gud/ops/archive.py
def export_archive(intake_or_client: "Intake | RepoClient", out: Path) -> Path   # mbox + log.txt + snapshot(s)
def import_archive(archive: Path, *, repo: str, host_private: bytes, mailbox: Mailbox) -> HostLog
    # new host continues the log after a planned handover (§11.6)

# gud/ops/keys.py
def rotate_member_key(client: RepoClient, *, new_private: bytes | None = None) -> tuple[Hex, bytes]  # members push; (push id, new key)
def revoke_member(client: RepoClient, handle: Handle, reason: str) -> Hex
def rotate_host_key(intake: Intake, new_private: bytes) -> tuple[LogEntry, Hex]   # handover entry + members push

# gud/ops/audit.py
@dataclass(frozen=True)
class AuditReport:
    log_ok: bool
    log_error: str | None
    seen_mismatches: tuple[tuple[Hex, Seen, Hex], ...]   # (message, cited, log hash)
    fork_proofs: tuple[ForkProof, ...]
    late_votes: tuple[tuple[Hex, Ms], ...]           # received − sent > G
    missing_mail: tuple[Hex, ...]
    @property
    def ok(self) -> bool
def audit(client: RepoClient, *, retained: Sequence[LogEntry] = ()) -> AuditReport
```

---

## Test obligations (for implementers)

- `tests/v1/` holds the v1 suite; v0 tests stay as they are.
- Every `gud/core/` module ships with unit tests; `core.replay` additionally with:
  - **shuffle determinism**: the same emails and log in any feed order and batching give identical `Chain`, `Snapshot.hash()` and event ids, and `Replayer` equals `replay()`;
  - the protocol tests listed under "Verification" in `PLAN.md` (host log, consent v1, test policy, carry-forward).
- Every adapter passes `tests/transport_conformance.py` (§20.2); `FakeMailbox` and `FakeNetwork` run it in CI.

## Addendum: public additions from the core foundations (a330f30)

These names are public and part of the contract:

- `gud.core.envelope`: `is_hex64`, `is_handle`, `is_public_key`, `is_signature`.
- `gud.core.config`: `bare_address`.
- `gud.core.types`: `MODES`, `REGULAR_MODES`, `ROLES`. `TestPolicy` fields have defaults.
- `gud.core.objects`: `EMPTY_DIR`, `Tree.store`, `BlameMap.paths()`, `BlameMap.__contains__`.
- `gud.core.hostlog`: `HostLog.repo`, `HostLog.entries(since=0, limit=None)`.

Event data carries every event-id key. That adds `approved.approve`, `held.hold`, `test.*.test`, `claim.lapsed.lapsed_at` and `task.ready.link`. See "Implementation notes (core)" at the end of SPEC-v1.md, which binds every later module.

## Addendum: transport layer (gud/transport)

Public additions and changes, all backward compatible with the sections above. See "Implementation notes (transport)" in SPEC-v1.md and `docs/v1/TRANSPORTS.md`.

- **`Mailbox.subscribe(callback, *, since: str | None = None)`**: the optional `since` catches up from a cursor first. Without it the subscription starts at the head.
- `gud.transport.base`: `PushKind`, `AuthResultsKind`, `ENVELOPE_CAP`, `MIME_OVERHEAD`, `envelope_budget(max_message_bytes, host_max_message_bytes=None) -> int` (§6 budget, 57/78 erratum), `now_ms(clock)`, `ThreadSubscription(target, *, name)` (a `Subscription` running `target(stop_event)` on a daemon thread), `safe_call(callback, item)`, and the string-config helpers `cfg_bool`, `cfg_int`, `cfg_list` used by every `from_config`. `Mailbox` is `runtime_checkable`.
- `gud.transport.mime`: `ENV_FILENAME`, `ParsedMime.has_text` (a field with default `False`), `make_message_id(from_addr)`, `qp_encode(text)`, `b64_lines(data)`, `is_envelope_part(part)`, `outgoing_from_raw(raw) -> Outgoing` (for structured-send providers), `recipients(raw) -> tuple[str, ...]`. `to_email` also returns `None` for an unsigned message without a `text/plain` part (§2.2).
- `gud.transport.auth`: `prepend(raw, value) -> bytes` (prepend one `Authentication-Results` line in the message's own line ending), `sender_verified(auth_results, from_addr, authserv_id) -> bool` (§8.1(2) for one message). `synthesize` raises `ValueError` for an authserv-id or domain that is not a token. `auth` imports only `core.envelope`.
- `gud.transport.registry`: `BUILTINS`. A built-in whose module does not exist yet (jmap, ses, postmark, gmail) raises `TransportError("... not implemented yet")`. `member_config` merges top-level keys with a table named after the transport and resolves `<key>_env` from the environment.
- Every built-in adapter also has `head() -> str | None`, the cursor just after the newest message (not part of the protocol; the conformance suite uses it when present).
- `gud.transport.fake`: `FAKE_AUTHSERV_ID = "fake.gud.invalid"`, `DEFAULT_MAX_MESSAGE_BYTES` (10 MiB). `FakeMailbox(root, address, *, clock, max_message_bytes, authserv_id, poll_interval)` plus `head()`, `deliver(raw, recipients=None, *, received_at=None)` (inject from an outside MTA). `Faults(delay_ms, jitter_ms, duplicate, drop, seed)`. `FakeNetwork(*, start_ms, faults, max_message_bytes, authserv_id)` with `now_ms`, `clock()`, `mailbox(address) -> NetworkMailbox`, `advance(ms)`, `advance_to(ms)`, `next_due()`, `pending`, `set_faults`, `submit(raw, recipients)`. `NetworkMailbox` adds `deliver(raw, recipients=None)` and `head()`; its `wait` advances virtual time instead of sleeping.
- `gud.transport.imap_smtp`: constructor keywords `imap_tls`/`smtp_tls` (`"ssl" | "starttls" | "none"`), `smtp_username`, `smtp_password`, `max_message_bytes`, `authserv_id` (the provider's, reported in capabilities), `send_quota_per_day`, `preserves_message_id`, `idle_poll`, `timeout`, `ssl_context`, `clock`. Helpers `encode_cursor`, `decode_cursor`, `parse_internaldate`.
- `gud.transport.primitive`: `PrimitiveMailbox(api_key, address, *, base_url, client, webhook_secret, authserv_id, subscribe_wait, send_quota_per_day, max_message_bytes, clock, sleep)` implements `Mailbox` and `WebhookVerifier`. It adds `accept_webhook(headers, body, now=None) -> list[Received]` (verify, parse, hand to subscribers), `fetch_webhook_secret()` and `head()`. Also exported: `AUTHSERV_ID = "primitive.gud.invalid"`, `MAX_MESSAGE_BYTES`, `PrimitiveError(TransportError)`, `PrimitiveQuotaError(PrimitiveError, QuotaExceeded)`, `signature_header`, `verify_signature`, `dmarc_for`, `unwrap_webhook`, `event_name`, `encode_cursor`, `decode_cursor`, `parse_time`.
- The conformance suite lives at `tests/v1/transport/conformance.py` (checks) and `tests/v1/transport/test_conformance.py` (transports), replacing the `tests/transport_conformance.py` named under "Test obligations".

## Addendum: public additions from the replay engine

All additions are backward compatible (new optional fields default, new keyword arguments are optional). See "Implementation notes (replay)" in SPEC-v1.md.

- `gud.core.rules`:
  - `ConsentPlan.humans: tuple[Handle, ...] = ()` (human voters at Y, for the members rule), `ConsentPlan.to_json()`, `ConsentPlan.from_json(d)`.
  - `Vote.target: Hex | None = None` (the negotiation root a decider vote is about; used by `decide_escalation`).
  - `ConsentInputs.push: Hex | None = None`, `ConsentInputs.negotiation: Hex | None = None` (select verdicts on the push's candidates and match escalation `chosen`/`declined`).
  - `ConsentError`, `INF`, `BLOCKING_STATUSES`, `is_confirmed(verdict) -> bool`, `first_verdict(verdicts, objection, push, until) -> EvidenceVerdict | None`.
  - `decide_escalation` counts decider votes received in `(opened_at, deadline]`.
- `gud.core.tasks`:
  - `NoChangeInput.paths_match: bool = True`, `NoChangeInput.has_live_push: bool = False` (tree and live-push checks are the engine's).
  - `TaskError`; `TaskBoard.take_changes() -> tuple[list[Hex], list[Hex]]`, `has(task)`, `task(id)`, `claim(id)`, `holders(task)`, `escalate(task, reason, at, by) -> Event`.
- `gud.core.replay`:
  - `ReplayError(message, delta=None)`; `.delta` is the `ChainDelta` of the verified prefix when `feed` stops at a bad log entry.
  - `Replayer.from_checkpoint(snapshot, store, *, state_dir=None, trusted_host_keys=())`.
  - `Replayer.tip`, `Replayer.last_error`, `Replayer.valid_checkpoints() -> list[dict]` (§17 quorum groups `{log, link, snapshot, signers}`).
  - `ChainReader(chain, store, index=None)`; `index` (a `Replayer`, as returned by `Replayer.reader()`) enables `email`, `thread`, `candidate_tree`, `ops_of` and `stack_tree`; without it they return None/empty (`stack_tree` raises).
  - `resolve_unsigned_target(email, by_message_id, ids, notify_target)`: `ids` may also be a mapping from 12-hex prefixes to id lists.
  - The `ChainDelta` contract is documented in the module docstring of `gud/core/replay.py`; `ChainDelta.events` is complete for §19.2.
- `gud.core.v0`: `V0Chain.now: float = 0.0` and `V0Chain.view() -> Chain` (the v1 `Chain` view: links and adapted proposals; no tasks, claims or log).

## Addendum: wc and cli

Public additions and changes in `gud/wc/` and `gud/cli_v1.py`, all backward compatible with the sections above (new keyword arguments are optional, new dataclass fields have defaults). See "Implementation notes (wc/cli)" in SPEC-v1.md.

- **`gud.wc.remote`**
  - `RemoteError(RuntimeError)`: mirror unreachable or refusing, or served data that fails verification (including host log rollback/fork).
  - Mirror auth v1, client side (the host may import or mirror these; the bytes are §21's): `HANDLE_HEADER`, `TIME_HEADER`, `SIG_HEADER`, `MAX_SKEW = 300`, `format_time(now) -> str`, `request_bytes(method, path_with_query, time_token, body=b"") -> bytes`, `sign_request(handle, private_raw, method, path_with_query, body, now) -> dict[str, str]`, `verify_request(members, method, path_with_query, body, headers, now, max_skew=300) -> Handle | None`.
  - `Mirror` (Protocol): `info()`, `log(since, limit)`, `mail_batch(since, limit)`, `mail(id)`, `object(sha)`, `snapshot(sha)`, `delivery(body)`, `pull()` (the §21 calls as plain methods; `None` for 404). `HttpMirror(base_url, handle, private_raw, *, http=None, clock=time.time, timeout=30.0)` implements it over HTTP.
  - `LocalHost(root, *, mailbox=None, clock=time.time, store=None)` and `LocalHost.create(root, *, repo, transport, cfg, host_private=None, bugs=None, objects=None, clock=time.time, mailbox=None)`: local host mode (intake `pull()`, the Mirror calls, `host_key`, `mailbox()`, `delivery(body, handle=None)`).
  - `open_mirror(host_url, handle, private_raw, *, http=None, clock=time.time, mailbox=None) -> Mirror` (`local:<dir>` or `http(s)://`).
  - `FetchingStore(local, fetch)`: an `ObjectStoreLike` that fetches missing objects by hash.
  - `snapshot_at(entries, mail, seq, store, trusted_host_keys=()) -> bytes` (§17 snapshot bytes at a log position).
  - Keys and config: `encode_private_key`, `decode_private_key`, `key_path(home, handle)`, `save_key(home, handle, raw, *, name="key", overwrite=False)`, `load_key(home, handle, *, name="key")`, `repo_slug(repo)`, `write_transport_toml(path, name, cfg)`, `now_ms(clock)`.
  - `RepoClient.__init__` gains keywords `clock=time.time`, `mirror: Mirror | None = None`, `trusted_host_keys=()`; `send_via` may be `None` (read-only). Attributes `host_url`, `handle`, `repo`, `home`, `clock`, `mirror`, `cache_dir`, `objects`, `store`, `trusted_host_keys`, `last_problems`.
  - `RepoClient` methods added: `bootstrap(*, checkpoint=True) -> "cache" | "checkpoint" | "genesis"`, `rebuild(*, from_checkpoint=True, upto=None, store=None) -> Replayer`, `snapshot_at(seq) -> bytes`, `recommended_checkpoint() -> int | None`, `budget() -> int`, `build_signed(kind, headers, records=(), *, key=None, author=None, seen=True) -> Envelope`, `member(handle=None) -> Member | None`, `resolve(ref) -> Hex` (full id, hex prefix ≥ 4, `T3`/`T3.1` alias, `tip`), `now_ms()`, `reset_cache()`, `trust_host_key(key)`.
  - `RepoClient.sign_and_send` gains keywords `text=""` (human-part preview) and `blobs=()`. `set_delivery` takes a `Delivery`-like object (`channel`, `url`) or a §21 body mapping.
- **`gud.wc.ignore`**: `GUDIGNORE`, `IgnoreRules.parse(text)`, `IgnoreRules.patterns`, `builtin_ignored(path)`.
- **`gud.wc.secrets`**: pattern names are `pem-private-key`, `openssh-private-key`, `gud-private-key`, `aws-access-key-id`, `github-token`, `github-pat`, `slack-token`, `anthropic-key`, `api-key`.
- **`gud.wc.copy`**
  - `SecretsFound(WcError)` with `.hits`.
  - `Status` gains `conflicts: tuple[str, ...] = ()`, `pushes: tuple[tuple[Hex, str], ...] = ()`, property `clean`, `to_json()`. `SyncResult` gains `written: tuple[str, ...] = ()`, `markers: tuple[str, ...] = ()`, `to_json()`. `PushResult` gains `files: tuple[str, ...] = ()`, `to_json()`; `forecast` may be `None`.
  - `WorkingCopy.adopt(client, root, base) -> WorkingCopy` (an existing directory after `gud init`), `WorkingCopy.fast_forward() -> bool`, `WorkingCopy.warnings`.
  - Module functions: `build_push(client, parent, before, after, intent, *, kind="push", headers=None) -> (Envelope, blobs, ops)`, `create_genesis(client, files, intent="genesis") -> Hex`, `push_tree(client, parent, after, intent, *, headers=None) -> Hex`, `verify_cache(client, *, from_checkpoint=True, base=None) -> list[str]`, `conflict_markers(base, mine, theirs, tip_label="tip") -> bytes`, `find_root(start) -> Path | None`, `read_local_config(root)`, `write_local_config(root, data)`, constants `BASE_FILE`, `LOCAL_DIR`.
- **`gud.cli_v1`**: console entry point `gud1 = "gud.cli_v1:main"` (`gud` stays v0). `build_parser()`. Every command is `cmd_<name>` (nested ones `cmd_<group>_<name>`, e.g. `cmd_task_new`, `cmd_host_init`, `cmd_keys_rotate`, `cmd_mirror_git`). Commands added to the table above: `keys new [--address A] [--transport N] [--set K=V]... [--force]`, `keys show`, `keys promote`, `admit JOIN [-m TEXT]`, `task list [--all]`, `task show ID`. Arguments added: global `-C DIR` and `--offline`; `init [DIR] [--from-git GIT_DIR] [--ref R] [--host-url URL] [--transport N] [--set K=V]... [-m INTENT]`; `import ... [--dir D] [--host-url URL]`; `join ... [--transport N] [--set K=V]...`; `host init ... [--set K=V]... [--dir D]`; `doctor [--transport N] [--set K=V]... [--no-send] [--timeout S]`; `object|veto ... [--test FILE [--test-path P]]`; `comment ... [--verdict reject]`; `checkpoint [--at-log N]`; `mirror git ... [--workdir D]`.
- The §21 host test double used by the wc tests is `tests/v1/wc/host_double.py` (`HostDouble`, `InProcess`, `Clock`); it is not product code.
