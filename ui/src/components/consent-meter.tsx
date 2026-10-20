import { Progress } from "@/components/ui/progress"
import { countdown, relativeTime } from "@/lib/format"
import type { Escalation, Proposal } from "@/lib/types"

function clamp(value: number): number {
  return Math.max(0, Math.min(100, value))
}

/** Consent v1 timers for one candidate: the window (W, 2W when held), then the grace G during
 * which a vote sent before the close can still arrive (§13.4), then park (§12.4). */
export function ConsentMeter({ proposal, now, escalation }: { proposal: Proposal; now: number; escalation?: Escalation }) {
  const t = proposal.timers
  const p = proposal.plan

  if (proposal.status === "escalated" && escalation) {
    const span = Math.max(escalation.deadline - escalation.opened_at, 1)
    return (
      <Meter
        label={`escalated (${escalation.reason}) · deciders ${escalation.deciders.join(", ") || "—"}`}
        detail={now < escalation.deadline ? `default in ${countdown(escalation.deadline, now)}` : "deadline passed"}
        value={clamp(((now - escalation.opened_at) / span) * 100)}
        tone="orange"
      />
    )
  }

  if (proposal.status === "needs_quorum") {
    const have = p.quorum_have ?? 0
    const needed = p.quorum_needed ?? 0
    return <Meter label="quorum" detail={`${have}/${needed}`} value={needed > 0 ? clamp((have / needed) * 100) : 0} />
  }

  if (proposal.status === "needs_approval") {
    const explicit = p.declared.filter((d) => d.rule === "explicit")
    const owners = [...new Set(explicit.flatMap((d) => d.owners))]
    const approved = new Set(proposal.approvals.map((a) => a.by))
    const label = p.members_rule && owners.length === 0 ? "members rule: a human approve" : "explicit owners"
    const needed = Math.max(owners.length, 1)
    const have = owners.length ? owners.filter((o) => approved.has(o)).length : approved.size > 0 ? 1 : 0
    return <Meter label={label} detail={`${Math.min(have, needed)}/${needed}`} value={clamp((have / needed) * 100)} />
  }

  if (t.window_open != null && t.window_closes_at != null && proposal.live) {
    const close = t.window_closes_at
    const grace = t.grace_until ?? close
    const span = Math.max(grace - t.window_open, 1)
    const value = clamp(((now - t.window_open) / span) * 100)
    let detail: string
    let label: string
    if (now < close) {
      label = t.held ? `window (held by ${t.held_by}, 2W)` : "window"
      detail = `closes in ${countdown(close, now)}`
    } else if (now < grace) {
      label = "grace: votes sent before the close still count"
      detail = countdown(grace, now)
    } else if (proposal.status === "parked") {
      label = "parked"
      detail = t.park_at ? relativeTime(t.park_at, now) : ""
    } else {
      label = proposal.status === "objected" ? "window passed, still objected" : "window passed"
      detail = t.park_at && now < t.park_at ? `parks in ${countdown(t.park_at, now)}` : "deciding"
    }
    const tone = proposal.status === "objected" ? "red" : now >= close ? "amber" : undefined
    return <Meter label={label} detail={detail} value={value} tone={tone} marker={clamp(((close - t.window_open) / span) * 100)} />
  }

  if (proposal.status === "testing") {
    return <p className="text-[11px] text-sky-200/90">Waiting for a tester&apos;s vouching pass.</p>
  }
  if (proposal.status === "qualified") {
    return <p className="text-[11px] text-emerald-200/90">Qualified. An earlier candidate on this parent can still link first.</p>
  }
  return null
}

const TONES: Record<string, string> = {
  red: "[&_[data-slot=progress-indicator]]:bg-red-400",
  amber: "[&_[data-slot=progress-indicator]]:bg-amber-400",
  orange: "[&_[data-slot=progress-indicator]]:bg-orange-400",
}

function Meter({ label, detail, value, tone, marker }: { label: string; detail: string; value: number; tone?: string; marker?: number }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span className="truncate">{label}</span>
        <span className="shrink-0 font-mono text-foreground/80">{detail}</span>
      </div>
      <div className={`relative ${tone ? TONES[tone] : ""}`}>
        <Progress value={value} />
        {marker != null && marker < 100 ? (
          <span className="absolute top-[-2px] h-[calc(100%+4px)] w-px bg-foreground/50" style={{ left: `${marker}%` }} title="window close" />
        ) : null}
      </div>
    </div>
  )
}
