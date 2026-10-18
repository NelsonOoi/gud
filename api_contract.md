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

```python
# gud/mail_remote.py: a member reading the host mirror and sending through their own account
class RemoteMailbox:                     # implements gud.types.Mailbox
    def __init__(self, host_url: str, identity: Identity, send_via: Mailbox, repo_address: str, client=None)
    # list/wait read GET {host}/api/mail (signed requests); send delegates to send_via

# gud/hostauth.py
def sign_request(identity, method: str, path_with_query: str, now: float) -> dict[str, str]   # headers
def verify_request(members: dict[str, Member], headers, method, path_with_query, now) -> str | None  # handle or None

# gud/joins.py
def join_body(identity, role, address, public_key, sponsor=None, reason="") -> str
def join_requests(chain) -> list[dict]          # JoinRequest shape from the HTTP contract
def audit(mirror: list[Email], own: list[Email], repo_address: str, skew: float) -> dict   # {missing, altered, retimed}
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
  bug: string | null;          // message id of the bug report, when this ticket came from one
}

type BugReport = {
  id: string; from: string; subject: string; description: string; received_at: number;
  status: "pending" | "accepted" | "rejected";
  task_id: string | null; verdict: "approve" | "reject" | null; review_by: string | null; reason: string;
}

type Activity = {
  id: string; at: number; kind: string;   // push | object | approve | test | task | subtask | task-done | notify | human | link
  from: string;                           // handle if known, else address
  subject: string; summary: string; target: string | null;
}

type JoinRequest = {
  id: string;                 // message id of the join email
  handle: string; role: string; address: string; public_key: string; sponsor: string | null;
  reason: string; received_at: number;
  valid: boolean;             // self-signature verifies and From == address
  status: "pending" | "proposed" | "member" | "rejected";   // proposed: a membership push cites it and is not yet a link
  push_id: string | null;     // the membership push citing it, if any
}

type RawEmail = {             // exactly gud.types.Email, as JSON (tuples become arrays)
  message_id: string; from_addr: string; to: string[]; cc: string[]; subject: string; body: string;
  received_at: number; sender_verified: boolean; in_reply_to: string | null; references: string[];
  provider_id: string | null;
}

type EmailView = {
  id: string; from: string; to: string[]; cc: string[]; subject: string; body: string;
  received_at: number; in_reply_to: string | null; sender_verified: boolean; kind: string | null;
}
```

### Endpoints

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/state` | `{ repo: string, bugs_address: string, now: number, window_seconds: number, tip: Link \| null, links: Link[], proposals: Proposal[], members: Member[], removed_members: (Member & { removed_by_link: string \| null, removed_at: number \| null })[], local_signers: string[] }` |
| GET | `/api/info` | `{ repo: string, window_seconds: number, tip_seq: number \| null }`; open to anyone, so `gud join` can find the repo address |
| GET | `/api/mail?since=<float>&limit=500` | `{ emails: RawEmail[], next_since: number \| null }` the host mirror of the repo mailbox, oldest first; page with `next_since` |
| GET | `/api/joins` | `JoinRequest[]` newest first |
| POST | `/api/joins/{id}/propose` | returns `{ push_id }`: `@console` sends the membership push for that join request (adds its line, `Gud-Join: <id>`); 409 if invalid or already a member |
| GET | `/api/links/{id}/diff` | `{ id, diff: string }` |
| GET | `/api/tree?at=<id>` | `{ at: string, files: { path: string; lines: number; owners: Record<string, number> }[] }` |
| GET | `/api/blame?path=<p>&at=<id>` | `{ path, at, lines: BlameLine[] }` |
| GET | `/api/tasks` | `Task[]` |
| GET | `/api/bugs` | `BugReport[]` oldest first. Mail to `bugs_address` that is still pending, accepted (a task cites it), or rejected |
| POST | `/api/bugs/{id}/review` | body `{ action: "approve" \| "reject", from, reason? }` returns `{ id }`. Sends the human review on the local mailbox. The lead opens the ticket after an approval; 409 if this report was already reviewed |
| GET | `/api/activity?limit=100` | `Activity[]` newest first |
| GET | `/api/thread/{id}` | `EmailView[]` oldest first (the push and every reply targeting it) |
| POST | `/api/tasks` | body `{ title, description, requester }` returns `{ id }` (sends a `task` email from `@console`) |
| POST | `/api/members` | body `{ action: "add" \| "remove", handle, role?, address?, public_key?, sponsor? }` returns `{ push_id }` (console sends a membership push; it lands by quorum) |
| POST | `/api/reply` | body `{ target, from, text }` returns `{ id }` (a human reply; only with the fake mailbox, to simulate a person emailing) |
| POST | `/api/approve` | body `{ target, as }` returns `{ id }` (signed approve from a local agent identity; demo helper) |
| GET | `/api/events` | SSE stream. Events: `mail` (`Activity`), `chain` (`{ tip: Link }`), `proposal` (`Proposal`), `task` (`Task`), `bug` (`BugReport`, when a report arrives or its status changes) |

Access: from loopback everything is open. From anywhere else, `/api/info` is open, other GET endpoints need a member signature (`X-Gud-Handle`, `X-Gud-Time`, `X-Gud-Sig`, see SPEC "Mirror access") and POST endpoints return 403. SSE also emits `join` (`JoinRequest`) events.

Everything the UI shows can be recomputed from `/api/state`, `/api/tasks` and `/api/activity`; SSE events are hints to refetch or patch in place.
