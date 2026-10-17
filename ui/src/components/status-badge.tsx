import type { MemberRole, ProposalStatus } from "@/lib/types"
import { STATUS_LABEL } from "@/lib/status"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"

const PROPOSAL_TONE: Record<ProposalStatus, string> = {
  window: "border-amber-400/40 bg-amber-400/15 text-amber-200",
  objected: "border-red-400/40 bg-red-400/15 text-red-200",
  vetoed: "border-red-500/50 bg-red-500/15 text-red-200",
  testing: "border-sky-400/40 bg-sky-400/15 text-sky-200",
  tests_failed: "border-red-400/40 bg-red-400/15 text-red-200",
  needs_approval: "border-violet-400/40 bg-violet-400/15 text-violet-200",
  needs_quorum: "border-violet-400/40 bg-violet-400/15 text-violet-200",
  stale: "border-zinc-500/40 bg-zinc-500/15 text-zinc-300",
  bad_sig: "border-orange-400/40 bg-orange-400/15 text-orange-200",
  conflict: "border-orange-400/40 bg-orange-400/15 text-orange-200",
  superseded: "border-zinc-500/40 bg-zinc-500/15 text-zinc-400",
  qualified: "border-emerald-400/40 bg-emerald-400/15 text-emerald-200",
}

const ROLE_TONE: Record<MemberRole, string> = {
  lead: "border-amber-400/40 bg-amber-400/15 text-amber-200",
  agent: "border-sky-400/40 bg-sky-400/15 text-sky-200",
  tester: "border-teal-400/40 bg-teal-400/15 text-teal-200",
  console: "border-zinc-500/40 bg-zinc-500/15 text-zinc-300",
  human: "border-pink-400/40 bg-pink-400/15 text-pink-200",
}

export function StatusBadge({ status }: { status: ProposalStatus }) {
  return (
    <Badge variant="outline" className={cn("font-mono uppercase", PROPOSAL_TONE[status])}>
      {STATUS_LABEL[status]}
    </Badge>
  )
}

export function RoleBadge({ role }: { role: MemberRole }) {
  return (
    <Badge variant="outline" className={cn("uppercase", ROLE_TONE[role])}>
      {role}
    </Badge>
  )
}

const KIND_TONE: Record<string, string> = {
  push: "border-sky-400/40 text-sky-200",
  object: "border-red-400/40 text-red-200",
  approve: "border-emerald-400/40 text-emerald-200",
  test: "border-teal-400/40 text-teal-200",
  task: "border-violet-400/40 text-violet-200",
  subtask: "border-violet-400/30 text-violet-200",
  "task-done": "border-emerald-400/40 text-emerald-200",
  notify: "border-pink-400/40 text-pink-200",
  human: "border-pink-400/40 text-pink-200",
  link: "border-amber-400/40 text-amber-200",
  reply: "border-zinc-500/40 text-zinc-300",
}

export function KindBadge({ kind }: { kind: string | null }) {
  const label = kind ?? "reply"
  return (
    <Badge variant="outline" className={cn("font-mono uppercase", KIND_TONE[label] ?? KIND_TONE.reply)}>
      {label}
    </Badge>
  )
}
