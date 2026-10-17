// HTTP shapes from gud/api_contract.md. Field names match the wire format.

export type Link = {
  id: string
  seq: number
  author: string
  subject: string
  intent: string
  state: string
  tree_hash: string
  received_at: number
  task: string | null
  diffstat: { files: string[]; added: number; removed: number }
}

export type ProposalStatus =
  | "window"
  | "objected"
  | "vetoed"
  | "testing"
  | "tests_failed"
  | "needs_approval"
  | "needs_quorum"
  | "stale"
  | "bad_sig"
  | "conflict"
  | "superseded"
  | "qualified"

export type Proposal = {
  id: string
  parent: string
  author: string
  subject: string
  received_at: number
  status: ProposalStatus
  reason: string
  affected_owners: string[]
  approvals: string[]
  objections: { by: string; reason: string; id: string }[]
  window_closes_at: number | null
  protected_rule: "explicit" | "quorum" | null
  quorum_needed: number | null
  quorum_have: number | null
  test_result: "pass" | "fail" | null
  task: string | null
}

export type MemberRole = "agent" | "lead" | "tester" | "console" | "human"

export type Member = {
  handle: string
  role: MemberRole
  address: string
  fingerprint: string | null
  sponsor: string | null
  votes: boolean
  lines_owned: number
  added_by_link: string | null
  removed_by_link?: string | null
  removed_at?: number | null
}

export type BlameLine = {
  n: number
  text: string
  owner: string
  link_id: string
  orphaned: boolean
}

export type SubtaskStatus = "open" | "proposed" | "landed"

export type Subtask = {
  id: string
  assignee: string
  title: string
  status: SubtaskStatus
  link_id: string | null
}

export type Task = {
  id: string
  title: string
  description: string
  requester: string
  created_at: number
  status: "open" | "in_progress" | "done"
  subtasks: Subtask[]
  done_link: string | null
  notified_at: number | null
}

export type Activity = {
  id: string
  at: number
  kind: string
  from: string
  subject: string
  summary: string
  target: string | null
}

export type JoinRequest = {
  id: string
  handle: string
  role: string
  address: string
  public_key: string
  sponsor: string | null
  reason: string
  received_at: number
  valid: boolean
  status: "pending" | "proposed" | "member" | "rejected"
  push_id: string | null
}

export type EmailView = {
  id: string
  from: string
  to: string[]
  cc: string[]
  subject: string
  body: string
  received_at: number
  in_reply_to: string | null
  sender_verified: boolean
  kind: string | null
}

export type RepoState = {
  repo: string
  now: number
  window_seconds: number
  tip: Link | null
  links: Link[]
  proposals: Proposal[]
  members: Member[]
  removed_members: Member[]
  /** Handles whose private keys are on the API host; only these can approve from the UI. */
  local_signers?: string[]
}

export type TreeFile = {
  path: string
  lines: number
  owners: Record<string, number>
}

export type TreeResponse = { at: string; files: TreeFile[] }

export type BlameResponse = { path: string; at: string; lines: BlameLine[] }

export type DiffResponse = { id: string; diff: string }

export type ServerEvent =
  | { event: "mail"; data: Activity }
  | { event: "chain"; data: { tip: Link } }
  | { event: "proposal"; data: Proposal }
  | { event: "task"; data: Task }
  | { event: "join"; data: JoinRequest }
