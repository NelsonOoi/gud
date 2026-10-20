// Shapes of the v1 host's UI view API (`gud/host_view.py`, `/api/v1/view/*`). Times are integer
// unix milliseconds (protocol time, SPEC-v1 §0); ids are 64 hex.

export type Hex = string
export type Ms = number

export type Role = "agent" | "human" | "tester" | "console" | "host"

export type RepoConfig = {
  window: Ms
  grace: Ms
  skew: Ms
  own_links: number
  own_time: Ms
  lease: Ms
  escalate: Ms
  expire: Ms
  carry: "file" | "line"
  fix_offer: Ms
  lease_thrash: number
  checkpoint: number
  authserv_id: string | null
}

export type TestPolicy = {
  command: string | null
  image: string | null
  timeout: Ms
  on_paths: string[]
  on_tip_links: number | null
  on_tip_time: Ms | null
  every_link: boolean
  stack_max: number
  auto_revert: Ms | null
}

export type Diffstat = { added: number; removed: number; files: string[]; binary: string[] }

export type Link = {
  seq: number
  state: Hex
  push: Hex
  kind: "push" | "revert"
  author: string
  intent: string
  title: string
  tree_hash: Hex
  parent: Hex | null
  named_parent: Hex | null
  carried_from: Hex | null
  carried_from_seq: number | null
  revert_of: Hex | null
  received_at: Ms
  linked_at: Ms
  task: Hex | null
  task_alias: string | null
  files: string[]
  verdict: "pass" | "fail" | null
  verdict_at: Ms | null
  red_role: "first_bad" | "red_link" | "last_green" | null
  diffstat: Diffstat
}

export type FixTaskRef = {
  id: Hex
  alias: string
  status: TaskStatus
  title: string
  offered_to: string | null
  offer_until: Ms | null
  cited_link: Hex | null
  holder: string | null
}

export type RedState = {
  red: boolean
  red_link: Hex | null
  red_link_seq: number | null
  red_since: Ms | null
  last_green: Hex | null
  last_green_seq: number | null
  first_bad: Hex | null
  first_bad_seq: number | null
  first_bad_author: string | null
  first_bad_title: string | null
  fix_task: FixTaskRef | null
}

export type UiSigner = { handle: string; role: Role }

export type ViewState = {
  repo: string
  bugs_address: string | null
  host_key: string
  transport: string
  now: Ms
  t: Ms
  log_head: { seq: number; hash: Hex; time: Ms; type: string }
  processed: { seq: number; hash: Hex }
  config: RepoConfig | null
  test_policy: TestPolicy | null
  tip: Link | null
  red: RedState
  counts: { links: number; proposals_live: number; tasks_open: number; claims_active: number; escalations_open: number }
  ui: { signers: UiSigner[]; human: string | null; console: string | null; actions: boolean }
}

export type ProposalStatus =
  | "window"
  | "objected"
  | "escalated"
  | "needs_approval"
  | "needs_quorum"
  | "testing"
  | "pending_blobs"
  | "qualified"
  | "parked"
  | "orphan"
  | "carried"
  | "vetoed"
  | "tests_failed"
  | "declined"
  | "expired"
  | "superseded"
  | "stale"
  | "bad_sig"
  | "malformed"
  | "conflict"
  | "bad_config"

export type ObjectionStatus = "blocking" | "advisory" | "pending_verdict" | "confirmed" | "refuted" | "cleared" | "void"

export type Objection = {
  id: Hex
  by: string
  reason: string
  received: Ms
  kind: "reason" | "evidence" | "veto"
  evidence: { path: string; sha256: Hex; size: number } | null
  status: ObjectionStatus
  answered_by: Hex | null
  episode: number
  blocking: boolean
}

export type Revision = {
  push: Hex
  candidate: Hex
  status: ProposalStatus
  received_at: Ms
  revises: Hex | null
  current: boolean
}

export type Proposal = {
  id: Hex
  push: Hex
  kind: "push" | "revert"
  author: string
  title: string
  intent: string
  received_at: Ms
  status: ProposalStatus
  reason: string
  live: boolean
  dead: boolean
  parent: Hex | null
  parent_seq: number | null
  named_parent: Hex
  named_parent_seq: number | null
  carried: boolean
  task: Hex | null
  task_alias: string | null
  revises: Hex | null
  negotiation: Hex
  files: string[]
  tree_hash: Hex | null
  timers: {
    window_open: Ms | null
    window_closes_at: Ms | null
    grace_until: Ms | null
    park_at: Ms | null
    expires_at: Ms | null
    window: Ms | null
    grace: Ms | null
    held: boolean
    held_by: string | null
    qualified_at: Ms | null
  }
  plan: {
    line_owners: string[]
    askers: string[]
    declared: { path: string; rule: string; owners: string[] }[]
    quorum_needed: number | null
    quorum_have: number | null
    members_rule: boolean
    waiting_on: string[]
  }
  approvals: { by: string; at: Ms }[]
  objections: Objection[]
  rounds: number
  revisions: Revision[]
  linked_revisions: { push: Hex; seq: number; state: Hex }[]
  test: { needed: boolean; result: "pass" | "fail" | null }
  pending_blobs: Hex[]
  escalation: Hex | null
  race: number | null
  blocks_tip: boolean
  join: Hex | null
}

export type ProposalsResponse = { proposals: Proposal[]; live: number; dead_shown: number }

export type TaskStatus = "open" | "claimed" | "work_done" | "done"

export type ClaimStatus = "active" | "lapsed" | "released" | "done" | "removed" | "void"

export type Claim = {
  id: Hex
  latest: Hex
  task: Hex
  task_alias: string | null
  holder: string
  files: string[]
  after: Hex[]
  won_at: Ms
  last_activity: Ms
  lapses_at: Ms
  status: ClaimStatus
  overlaps: Hex[]
}

export type Task = {
  id: Hex
  alias: string
  kind: "task" | "subtask" | "fix"
  title: string
  description: string
  author: string | null
  requester: string | null
  parent: Hex | null
  parent_alias: string | null
  children: Hex[]
  opened_at: Ms
  status: TaskStatus
  accept_paths: string[]
  accept_cmds: string[]
  has_criteria: boolean
  after: Hex[]
  files_hint: string[]
  bug: Hex | null
  claim: Hex | null
  holder: string | null
  work_done_at: Ms | null
  work_done_link: Hex | null
  done_at: Ms | null
  done_link: Hex | null
  no_change: { subtask: Hex; reason: string }[]
  cited_link: Hex | null
  cited_seq: number | null
  cited_author: string | null
  offered_to: string | null
  offer_until: Ms | null
  waiting: "acceptance" | "criteria" | null
  claims: Claim[]
  links: { seq: number; state: Hex; author: string }[]
}

export type Overlap = { claims: [Hex, Hex]; holders: [string, string]; tasks: [Hex, Hex]; globs: string[]; paths: string[] }

export type BugReport = {
  id: Hex
  from: string
  subject: string
  text: string
  received_at: Ms
  task: Hex | null
  task_alias: string | null
}

export type TasksResponse = { tasks: Task[]; claims: Claim[]; overlaps: Overlap[]; bugs: BugReport[] }

export type Escalation = {
  id: Hex
  reason: "mutual" | "rounds"
  pushes: { root: Hex; push: Hex; author: string | null; title: string; status: string | null; confirmed_evidence: boolean }[]
  opened_at: Ms
  deadline: Ms
  deciders: string[]
  status: "open" | "resolved"
  chosen: Hex | null
  declined: Hex[]
  decided_by: string | null
  decided_at: Ms | null
  default: { chosen: Hex | null; declined: Hex[]; why: string }
}

export type Delivery = {
  channel: "webhook" | "sse" | "email"
  url: string | null
  registered: boolean
  journal_head: number | null
  acked: number | null
}

export type Member = {
  handle: string
  role: Role
  address: string
  key: string | null
  key_short: string | null
  sponsor: string | null
  votes: boolean
  lines: number
  lines_live: number
  added_seq: number | null
  delivery: Delivery | null
}

export type RemovedMember = { handle: string; role: Role; address: string; removed_seq: number; removed_link: Hex; lines: number }

export type JoinRequest = {
  id: Hex
  handle: string
  role: string | null
  address: string | null
  key: string | null
  key_short: string | null
  sponsor: string | null
  reason: string
  received_at: Ms | null
  status: "pending" | "proposed" | "member"
  push: Hex | null
}

export type MembersResponse = { members: Member[]; removed: RemovedMember[]; joins: JoinRequest[] }

export type TreeAt = { kind: "link"; state: Hex; seq: number } | { kind: "proposal"; state: Hex; push: Hex; status: string }

export type TreeFile = {
  path: string
  mode: string
  size: number
  sha256: Hex
  binary: boolean
  lines: number | null
  owners: Record<string, number>
  live_owners: Record<string, number>
  former: string[]
}

export type TreeResponse = { at: TreeAt; files: TreeFile[] }

export type BlameLine = {
  n: number
  text: string
  owner: string
  seq: number
  link: Hex | null
  intent: string
  live: boolean
  voter: boolean
  member: boolean
  expires_at: Ms | null
  links_left: number
}

export type BlameResponse = { path: string; at: TreeAt; binary: boolean; own_links: number; own_time: Ms; t: Ms; lines: BlameLine[] }

export type FileResponse = {
  path: string
  mode: string
  size: number
  sha256: Hex
  kind: "text" | "binary" | "symlink"
  text?: string
  target?: string
  placeholder?: string
}

export type DiffFile = {
  op: "add" | "modify" | "delete" | "rename" | "mode"
  path: string
  src?: string
  binary: boolean
  old_mode?: string | null
  new_mode?: string | null
  old_size?: number | null
  new_size?: number | null
  added: number
  removed: number
  hunks: string
  placeholder?: string
}

export type SignedOp =
  | { op: "patch"; path: string }
  | { op: "put"; path: string; mode: string; sha256: Hex; size: number; inline: boolean }
  | { op: "delete"; path: string }
  | { op: "rename"; src: string; dst: string }
  | { op: "mode"; path: string; mode: string }

export type DiffResponse = {
  kind: "link" | "proposal"
  push: Hex
  author: string
  available: boolean
  why?: string
  ops: SignedOp[]
  files: DiffFile[]
  unified: string
  seq?: number
  status?: string
  carried_from?: Hex | null
}

export type ThreadMessage = {
  id: Hex
  seq: number | null
  received_at: Ms | null
  kind: string
  author: string | null
  from: string
  subject: string
  signed: boolean
  text: string
  message_id: string
  headers: Record<string, string>
  evidence: { path: string; sha256: Hex; size: number } | null
}

export type ThreadResponse = { head: { id: Hex; type: string; [key: string]: unknown }; messages: ThreadMessage[] }

export type GudEvent = {
  n: number
  id: Hex
  type: string
  at: Ms
  concerns: string[]
  data: Record<string, unknown>
}

export type EventsResponse = { events: GudEvent[]; head: number }

export type NeedItem = { type: string; [key: string]: unknown }

export type NeedsResponse = { member: string | null; items: NeedItem[] }

export type HostLogEntry = {
  seq: number
  type: "open" | "mail" | "tick" | "handover"
  time: Ms
  hash: Hex
  id?: Hex
  kind?: string | null
  author?: string | null
  summary?: string
  missing?: boolean
  fields?: Record<string, string>
}

export type HostView = {
  repo: string
  key: string
  repos: string[]
  head: { seq: number; hash: Hex; time: Ms; type: string } | null
  processed_seq: number
  entries: number
  counts: Record<string, number>
  tail: HostLogEntry[]
  handed_over: boolean
  missing_mail: Hex[]
  last_error: string | null
  next_wakeup: Ms | null
  intake: Record<string, number>
  dispatch: Record<string, number>
  deliveries: { handle: string; channel: string; url: string | null; journal_head: number | null; acked: number | null }[]
  feed_head: number | null
  audit: {
    ok: boolean | null
    exit_status: number | null
    log_ok: boolean | null
    log_error: string | null
    checked_at: Ms | null
    mail_entries: number | null
    seen_mismatches: number
    fork_proofs: number
    late_votes: number
    missing_mail: number
    altered: number
  }
}

export type ActResult = { id: Hex; kind: string; as: string; logged: boolean; events: string[] }
