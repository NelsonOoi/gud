import type { ClaimStatus, JoinRequest, ObjectionStatus, ProposalStatus, Role, TaskStatus } from "@/lib/types"
import { STATUS_LABEL } from "@/lib/status"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"

const AMBER = "border-amber-400/40 bg-amber-400/15 text-amber-200"
const RED = "border-red-400/40 bg-red-400/15 text-red-200"
const SKY = "border-sky-400/40 bg-sky-400/15 text-sky-200"
const VIOLET = "border-violet-400/40 bg-violet-400/15 text-violet-200"
const ZINC = "border-zinc-500/40 bg-zinc-500/15 text-zinc-300"
const ORANGE = "border-orange-400/40 bg-orange-400/15 text-orange-200"
const GREEN = "border-emerald-400/40 bg-emerald-400/15 text-emerald-200"
const TEAL = "border-teal-400/40 bg-teal-400/15 text-teal-200"
const PINK = "border-pink-400/40 bg-pink-400/15 text-pink-200"

const PROPOSAL_TONE: Record<ProposalStatus, string> = {
  window: AMBER,
  objected: RED,
  escalated: ORANGE,
  needs_approval: VIOLET,
  needs_quorum: VIOLET,
  testing: SKY,
  pending_blobs: SKY,
  qualified: GREEN,
  parked: ZINC,
  orphan: ZINC,
  carried: TEAL,
  vetoed: "border-red-500/50 bg-red-500/15 text-red-200",
  tests_failed: RED,
  declined: RED,
  expired: ZINC,
  superseded: "border-zinc-500/40 bg-zinc-500/15 text-zinc-400",
  stale: ZINC,
  bad_sig: ORANGE,
  malformed: ORANGE,
  conflict: ORANGE,
  bad_config: ORANGE,
}

const ROLE_TONE: Record<Role, string> = {
  agent: SKY,
  tester: TEAL,
  console: ZINC,
  human: PINK,
  host: AMBER,
}

export function StatusBadge({ status }: { status: ProposalStatus }) {
  return (
    <Badge variant="outline" className={cn("font-mono uppercase", PROPOSAL_TONE[status] ?? ZINC)}>
      {STATUS_LABEL[status] ?? status}
    </Badge>
  )
}

export function RoleBadge({ role }: { role: Role | string }) {
  return (
    <Badge variant="outline" className={cn("uppercase", ROLE_TONE[role as Role] ?? ZINC)}>
      {role}
    </Badge>
  )
}

const OBJECTION_TONE: Record<ObjectionStatus, string> = {
  blocking: RED,
  pending_verdict: AMBER,
  confirmed: "border-red-500/50 bg-red-500/20 text-red-100",
  refuted: ZINC,
  advisory: ZINC,
  cleared: GREEN,
  void: ZINC,
}

export function ObjectionBadge({ status }: { status: ObjectionStatus }) {
  return (
    <Badge variant="outline" className={cn("font-mono text-[10px] uppercase", OBJECTION_TONE[status] ?? ZINC)}>
      {status.replaceAll("_", " ")}
    </Badge>
  )
}

const TASK_TONE: Record<TaskStatus, string> = {
  open: ZINC,
  claimed: AMBER,
  work_done: VIOLET,
  done: GREEN,
}

export function TaskBadge({ status }: { status: TaskStatus }) {
  return (
    <Badge variant="outline" className={cn("uppercase", TASK_TONE[status])}>
      {status.replaceAll("_", " ")}
    </Badge>
  )
}

const CLAIM_TONE: Record<ClaimStatus, string> = {
  active: AMBER,
  lapsed: ZINC,
  released: ZINC,
  done: GREEN,
  removed: ZINC,
  void: ZINC,
}

export function ClaimBadge({ status }: { status: ClaimStatus }) {
  return (
    <Badge variant="outline" className={cn("text-[10px] uppercase", CLAIM_TONE[status])}>
      {status}
    </Badge>
  )
}

const JOIN_TONE: Record<JoinRequest["status"], string> = {
  pending: AMBER,
  proposed: VIOLET,
  member: GREEN,
}

export function JoinStatusBadge({ status }: { status: JoinRequest["status"] }) {
  return (
    <Badge variant="outline" className={cn("uppercase", JOIN_TONE[status])}>
      {status}
    </Badge>
  )
}

export function VerdictBadge({ verdict }: { verdict: "pass" | "fail" | null }) {
  if (!verdict) return null
  return (
    <Badge variant="outline" className={cn("font-mono text-[10px] uppercase", verdict === "pass" ? GREEN : RED)}>
      tip {verdict}
    </Badge>
  )
}

const KIND_TONE: Record<string, string> = {
  push: "border-sky-400/40 text-sky-200",
  revert: "border-orange-400/40 text-orange-200",
  object: "border-red-400/40 text-red-200",
  veto: "border-red-500/60 text-red-100",
  approve: "border-emerald-400/40 text-emerald-200",
  hold: "border-amber-400/40 text-amber-200",
  comment: "border-zinc-500/40 text-zinc-300",
  test: "border-teal-400/40 text-teal-200",
  task: "border-violet-400/40 text-violet-200",
  subtask: "border-violet-400/30 text-violet-200",
  claim: "border-amber-400/30 text-amber-200",
  "no-change": "border-zinc-500/40 text-zinc-300",
  notify: "border-pink-400/40 text-pink-200",
  unsigned: "border-pink-400/40 text-pink-200",
  join: "border-sky-400/40 text-sky-200",
  checkpoint: "border-zinc-500/40 text-zinc-300",
  blob: "border-zinc-500/40 text-zinc-400",
  link: "border-amber-400/40 text-amber-200",
  tick: "border-zinc-600/40 text-zinc-500",
  open: "border-amber-400/40 text-amber-200",
  handover: "border-orange-400/40 text-orange-200",
}

export function KindBadge({ kind }: { kind: string | null | undefined }) {
  const label = kind ?? "mail"
  return (
    <Badge variant="outline" className={cn("font-mono uppercase", KIND_TONE[label] ?? KIND_TONE.comment)}>
      {label}
    </Badge>
  )
}
