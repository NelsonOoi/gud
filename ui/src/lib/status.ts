import type { ProposalStatus } from "@/lib/types"

export const OPEN_STATUSES: ReadonlySet<ProposalStatus> = new Set([
  "window",
  "needs_approval",
  "needs_quorum",
  "testing",
  "qualified",
])

export function isOpenStatus(status: ProposalStatus): boolean {
  return OPEN_STATUSES.has(status)
}

export const STATUS_LABEL: Record<ProposalStatus, string> = {
  window: "window",
  objected: "objected",
  vetoed: "vetoed",
  testing: "testing",
  tests_failed: "tests failed",
  needs_approval: "needs approval",
  needs_quorum: "needs quorum",
  stale: "stale",
  bad_sig: "bad sig",
  conflict: "conflict",
  superseded: "superseded",
  qualified: "qualified",
}

export const ACTIVITY_KINDS = [
  "push",
  "object",
  "approve",
  "test",
  "task",
  "subtask",
  "task-done",
  "notify",
  "human",
  "link",
] as const
