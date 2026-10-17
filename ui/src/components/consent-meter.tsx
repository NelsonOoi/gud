import { countdown } from "@/lib/format"
import type { Proposal } from "@/lib/types"
import { Progress } from "@/components/ui/progress"

function clamp(value: number): number {
  return Math.max(0, Math.min(100, value))
}

export function ConsentMeter({
  proposal,
  now,
  windowSeconds,
}: {
  proposal: Proposal
  now: number
  windowSeconds: number
}) {
  if (proposal.status === "window" && proposal.window_closes_at != null) {
    const close = proposal.window_closes_at
    const start = close - windowSeconds
    const span = Math.max(windowSeconds, 1)
    const value = clamp(((now - start) / span) * 100)
    return (
      <Meter
        label="lazy consensus"
        detail={countdown(close, now)}
        value={value}
      />
    )
  }

  if (proposal.protected_rule === "quorum" || proposal.status === "needs_quorum") {
    const have = proposal.quorum_have ?? 0
    const needed = proposal.quorum_needed ?? 0
    const value = needed > 0 ? clamp((have / needed) * 100) : 0
    return <Meter label="quorum" detail={`${have}/${needed}`} value={value} />
  }

  if (proposal.protected_rule === "explicit" || proposal.status === "needs_approval") {
    const owners = proposal.affected_owners
    const have =
      owners.length === 0
        ? proposal.approvals.filter((handle) => handle !== proposal.author).length
        : owners.filter((owner) => proposal.approvals.includes(owner)).length
    const needed = Math.max(owners.length, 1)
    return (
      <Meter
        label="owner approvals"
        detail={`${Math.min(have, needed)}/${needed}`}
        value={clamp((have / needed) * 100)}
      />
    )
  }

  if (proposal.status === "testing") {
    return <p className="text-[11px] text-sky-200/90">Waiting for a tester to reply pass.</p>
  }

  if (proposal.status === "qualified") {
    return (
      <p className="text-[11px] text-emerald-200/90">
        Qualified. An earlier sibling of this parent can still land first.
      </p>
    )
  }

  return null
}

function Meter({ label, detail, value }: { label: string; detail: string; value: number }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{label}</span>
        <span className="font-mono text-foreground/80">{detail}</span>
      </div>
      <Progress value={value} />
    </div>
  )
}
