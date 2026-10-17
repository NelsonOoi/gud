# gud interfaces

Two contracts: the **Python core API** (what every module calls) and the **HTTP API** (what the UI and MCP server call). Wire format and rules are in `SPEC.md`.

## Python core API

Owned by the core module set (`patch`, `blame`, `rules`, `replay`, `chain`). Everything else imports these names and nothing deeper.

```python
# gud/patch.py
def diff_trees(old: Tree, new: Tree) -> str                      # done (Phase 0)
def apply_diff(tree: Tree, diff: str) -> Tree                      # raises PatchError
def diffstat(diff: str) -> dict                                    # {"files": [...], "added": int, "removed": int}

# gud/blame.py
def genesis_blame(tree: Tree, owner: str, link_id: str) -> Blame
def update_blame(old_tree: Tree, old_blame: Blame, new_tree: Tree, owner: str, link_id: str) -> Blame
def removed_owners(old_tree: Tree, old_blame: Blame, diff: str) -> set[str]       # owners of removed/rewritten lines
def enclosing_owners(old_tree: Tree, old_blame: Blame, diff: str) -> set[str]     # def/class owners around inserts (.py only)

# gud/rules.py
def affected_owners(parent: LinkState, diff: str, author: str) -> list[str]   # SPEC "Affected owners", sorted
def protection(parent: LinkState, diff: str) -> str | None                     # None | "explicit" | "quorum"
def quorum_voters(parent: LinkState, new_tree: Tree) -> list[str]              # voters minus add/removed subjects

# gud/replay.py
@dataclass
class Chain:
    links: list[LinkState]                 # genesis first
    proposals: dict[str, ProposalState]    # pending or rejected pushes whose parent is a link
    by_id: dict[str, LinkState]
    emails: list[Email]                    # every repo email, oldest first
    @property
    def tip(self) -> LinkState | None

def replay(emails: list[Email], window: float, now: float) -> Chain

# gud/chain.py
class Repo:
    def __init__(self, mailbox: Mailbox, window: float, clock=time.time)
    def refresh(self) -> Chain           # replays repo mail; may cache, must equal a fresh replay
    def tip(self) -> LinkState
    def tree(self, at: str | None = None) -> Tree
    def blame(self, path: str, at: str | None = None) -> list[tuple[str, str, str]]   # (owner, link_id, text)
    def thread(self, message_id: str) -> list[Email]
```

Helpers already written (Phase 0): `gud.wire`, `gud.sig`, `gud.compose`, `gud.memberfile`, `gud.mail_fake`, `gud.config`, `gud.testkit`.

## HTTP API

Served by `gud/api.py` (FastAPI) on `http://localhost:8787`. All JSON. Message ids are passed URL-encoded (they contain `<`, `>`, `@`). Timestamps are unix seconds (float).

### Shapes

```ts
type Link = {
  id: string; seq: number; author: string; subject: string; intent: string;
  state: string; tree_hash: string; received_at: number; task: string | null;
  diffstat: { files: string[]; added: number; removed: number };
}

type Proposal = {
  id: string; parent: string; author: string; subject: string; received_at: number;
  status: "window" | "objected" | "vetoed" | "testing" | "tests_failed" | "needs_approval"
        | "needs_quorum" | "stale" | "bad_sig" | "conflict" | "superseded" | "qualified";
  reason: string; affected_owners: string[]; approvals: string[];
  objections: { by: string; reason: string; id: string }[];
  window_closes_at: number | null; protected_rule: "explicit" | "quorum" | null;
  quorum_needed: number | null; quorum_have: number | null;
  test_result: "pass" | "fail" | null; task: string | null;
}

type Member = {
  handle: string; role: "agent" | "lead" | "tester" | "console" | "human";
  address: string; fingerprint: string | null; sponsor: string | null;
  votes: boolean; lines_owned: number;
  added_by_link: string | null;       // link that added this member
}

type BlameLine = { n: number; text: string; owner: string; link_id: string; orphaned: boolean }

type Task = {
  id: string; title: string; description: string; requester: string; created_at: number;
  status: "open" | "in_progress" | "done";
  subtasks: { id: string; assignee: string; title: string; status: "open" | "proposed" | "landed"; link_id: string | null }[];
  done_link: string | null; notified_at: number | null;
}

type Activity = {
  id: string; at: number; kind: string;   // push | object | approve | test | task | subtask | task-done | notify | human | link
  from: string;                           // handle if known, else address
  subject: string; summary: string; target: string | null;
}

type EmailView = {
  id: string; from: string; to: string[]; cc: string[]; subject: string; body: string;
  received_at: number; in_reply_to: string | null; sender_verified: boolean; kind: string | null;
}
```

### Endpoints

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/state` | `{ repo: string, now: number, window_seconds: number, tip: Link \| null, links: Link[], proposals: Proposal[], members: Member[], removed_members: Member[] }` |
| GET | `/api/links/{id}/diff` | `{ id, diff: string }` |
| GET | `/api/tree?at=<id>` | `{ at: string, files: { path: string; lines: number; owners: Record<string, number> }[] }` |
| GET | `/api/blame?path=<p>&at=<id>` | `{ path, at, lines: BlameLine[] }` |
| GET | `/api/tasks` | `Task[]` |
| GET | `/api/activity?limit=100` | `Activity[]` newest first |
| GET | `/api/thread/{id}` | `EmailView[]` oldest first (the push and every reply targeting it) |
| POST | `/api/tasks` | body `{ title, description, requester }` returns `{ id }` (sends a `task` email from `@console`) |
| POST | `/api/members` | body `{ action: "add" \| "remove", handle, role?, address?, public_key?, sponsor? }` returns `{ push_id }` (console sends a membership push; it lands by quorum) |
| POST | `/api/reply` | body `{ target, from, text }` returns `{ id }` (a human reply; only with the fake mailbox, to simulate a person emailing) |
| POST | `/api/approve` | body `{ target, as }` returns `{ id }` (signed approve from a local agent identity; demo helper) |
| GET | `/api/events` | SSE stream. Events: `mail` (`Activity`), `chain` (`{ tip: Link }`), `proposal` (`Proposal`), `task` (`Task`) |

Everything the UI shows can be recomputed from `/api/state`, `/api/tasks` and `/api/activity`; SSE events are hints to refetch or patch in place.
