import type { ProposalStatus } from "@/lib/types"

export const LIVE_STATUSES: ReadonlySet<ProposalStatus> = new Set([
  "window",
  "objected",
  "escalated",
  "needs_approval",
  "needs_quorum",
  "testing",
  "pending_blobs",
  "qualified",
  "parked",
  "orphan",
  "carried",
])

export function isLive(status: ProposalStatus): boolean {
  return LIVE_STATUSES.has(status)
}

export const STATUS_LABEL: Record<ProposalStatus, string> = {
  window: "window",
  objected: "objected",
  escalated: "escalated",
  needs_approval: "needs approval",
  needs_quorum: "needs quorum",
  testing: "testing",
  pending_blobs: "blobs pending",
  qualified: "qualified",
  parked: "parked",
  orphan: "orphan",
  carried: "carried",
  vetoed: "vetoed",
  tests_failed: "tests failed",
  declined: "declined",
  expired: "expired",
  superseded: "superseded",
  stale: "stale",
  bad_sig: "bad sig",
  malformed: "malformed",
  conflict: "conflict",
  bad_config: "bad config",
}

/** What a proposal is waiting for, in words (mirrors `gud.mcp.explain_proposal`). */
export function explain(status: ProposalStatus): string {
  switch (status) {
    case "window":
      return "Lazy consent: links when the window and grace pass unless an asker objects."
    case "objected":
      return "Blocked by an objection. The author answers with a comment or a revision."
    case "escalated":
      return "Escalated to the requester and sponsors; the default applies at the deadline."
    case "needs_approval":
      return "Needs an explicit approve."
    case "needs_quorum":
      return "Needs a quorum of voters."
    case "testing":
      return "Waiting for a tester's vouching pass."
    case "pending_blobs":
      return "Waiting for its blob messages."
    case "qualified":
      return "Qualified: links when no earlier candidate holds it back."
    case "parked":
      return "Parked: it no longer holds later candidates back and can still link."
    case "orphan":
      return "Its parent has not linked yet."
    case "carried":
      return "Carried onto a newer link."
    case "superseded":
      return "Another push linked first and touched the same files."
    case "stale":
      return "Its parent can no longer link."
    default:
      return ""
  }
}

export const EVENT_GROUPS: Record<string, string> = {
  "push.received": "push",
  "review.requested": "review",
  objected: "objection",
  commented: "comment",
  approved: "vote",
  held: "vote",
  "test.passed": "test",
  "test.failed": "test",
  "window.closing": "push",
  superseded: "push",
  "push.rejected": "push",
  carried: "push",
  linked: "link",
  "task.opened": "task",
  "subtask.posted": "task",
  "claim.won": "claim",
  "claim.lost": "claim",
  "claim.lapsed": "claim",
  "claim.overlap": "claim",
  "scope.exceeded": "claim",
  "task.ready": "task",
  "task.done": "task",
  escalated: "escalation",
  "escalation.resolved": "escalation",
  "tip.red": "tip",
  "tip.bisected": "tip",
  "tip.green": "tip",
  "fix.opened": "tip",
  "host.fork": "host",
}
