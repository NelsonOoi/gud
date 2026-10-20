import { useMemo, useState } from "react"
import { ChevronRightIcon, Scale } from "lucide-react"
import { AgentAvatar, AgentChip } from "@/components/agent-avatar"
import { ConsentMeter } from "@/components/consent-meter"
import { RedBanner } from "@/components/red-banner"
import { ObjectionBadge, StatusBadge, VerdictBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { useGud } from "@/hooks/use-gud"
import { countdown, relativeTime, shortHash } from "@/lib/format"
import { STATUS_LABEL } from "@/lib/status"
import type { Escalation, Link, Proposal } from "@/lib/types"
import { cn } from "@/lib/utils"

export function ChainView() {
  const { state, links, proposals, escalations, now, loading, openId, setOpenId } = useGud()
  const [showDead, setShowDead] = useState(false)

  // one card per negotiation (§13.6): its newest live push, with the rest as its history
  const negotiations = useMemo(() => {
    const live = proposals.filter((p) => p.live)
    const groups = new Map<string, Proposal[]>()
    for (const p of live) groups.set(p.negotiation, [...(groups.get(p.negotiation) ?? []), p])
    return [...groups.values()]
      .map((items) => [...items].sort((a, b) => b.received_at - a.received_at))
      .sort((a, b) => (a[a.length - 1]?.received_at ?? 0) - (b[b.length - 1]?.received_at ?? 0))
  }, [proposals])
  const dead = proposals.filter((p) => p.dead)
  const openEsc = escalations.filter((e) => e.status === "open")
  const escById = new Map(escalations.map((e) => [e.id, e]))

  if (loading && !state) {
    return (
      <div className="space-y-3 p-4">
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    )
  }

  return (
    <ScrollArea className="h-full">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-4">
        <RedBanner />
        {openEsc.length > 0 ? (
          <section className="space-y-2">
            <SectionHead title="Escalations" count={openEsc.length} />
            {openEsc.map((e) => (
              <EscalationCard key={e.id} escalation={e} now={now} onOpen={setOpenId} />
            ))}
          </section>
        ) : null}
        <section className="space-y-3">
          <SectionHead title="Proposals" count={negotiations.length} />
          {negotiations.length === 0 ? <p className="text-sm text-muted-foreground">No live proposals.</p> : null}
          {negotiations.map((items) => {
            const head = items[0]!
            return (
              <ProposalCard
                key={head.negotiation}
                proposal={head}
                history={items.slice(1)}
                now={now}
                escalation={head.escalation ? escById.get(head.escalation) : undefined}
                selected={openId === head.id || openId === head.push}
                onOpen={setOpenId}
              />
            )
          })}
          {dead.length > 0 ? (
            <div>
              <button
                type="button"
                className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
                onClick={() => setShowDead((v) => !v)}
              >
                <ChevronRightIcon className={cn("size-3 transition-transform", showDead && "rotate-90")} />
                {dead.length} rejected or superseded
              </button>
              {showDead ? (
                <ul className="mt-1 space-y-1">
                  {dead.map((p) => (
                    <li key={p.id}>
                      <button type="button" onClick={() => setOpenId(p.push)} className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-muted/40">
                        <AgentAvatar handle={p.author} className="size-5" />
                        <span className="min-w-0 flex-1 truncate text-[12px]">{p.title}</span>
                        <StatusBadge status={p.status} />
                        <span className="font-mono text-[10px] text-muted-foreground">{relativeTime(p.received_at, now)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
        </section>
        <section className="space-y-1">
          <SectionHead title="Links" count={state?.counts.links ?? links.length} />
          {links.map((link) => (
            <LinkRow key={link.state} link={link} now={now} selected={openId === link.state} onOpen={() => setOpenId(link.state)} />
          ))}
        </section>
      </div>
    </ScrollArea>
  )
}

function SectionHead({ title, count }: { title: string; count: number }) {
  return (
    <header className="mb-1 flex items-baseline justify-between">
      <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h2>
      <span className="font-mono text-[11px] text-muted-foreground">{count}</span>
    </header>
  )
}

function EscalationCard({ escalation: e, now, onOpen }: { escalation: Escalation; now: number; onOpen: (id: string) => void }) {
  const chosen = e.default.chosen
  return (
    <Card size="sm" className="border-orange-400/30">
      <CardContent className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Scale className="size-4 text-orange-300" />
          <span className="text-sm font-medium">{e.reason === "mutual" ? "Mutual objections" : "Objected twice (rounds)"}</span>
          <Badge variant="outline" className="border-orange-400/40 font-mono text-[10px] text-orange-200">
            {now < e.deadline ? `deadline in ${countdown(e.deadline, now)}` : "deadline passed"}
          </Badge>
          <span className="ml-auto font-mono text-[10px] text-muted-foreground">{shortHash(e.id)}</span>
        </div>
        <ul className="space-y-1">
          {e.pushes.map((p) => (
            <li key={p.root} className="flex items-center gap-2 text-[12px]">
              {p.author ? <AgentAvatar handle={p.author} className="size-5" /> : null}
              <button type="button" className="min-w-0 flex-1 truncate text-left hover:underline" onClick={() => onOpen(p.push)}>
                {p.title}
              </button>
              {p.confirmed_evidence ? (
                <Badge variant="outline" className="border-red-400/40 text-[10px] text-red-200">confirmed evidence</Badge>
              ) : null}
              {p.status ? <span className="font-mono text-[10px] text-muted-foreground">{STATUS_LABEL[p.status as Proposal["status"]] ?? p.status}</span> : null}
            </li>
          ))}
        </ul>
        <p className="text-[11px] text-muted-foreground">
          Deciders {e.deciders.map((d) => <AgentChip key={d} handle={d} />)} — the first decider vote chooses or declines.
        </p>
        <p className="text-[11px] text-orange-200/90">
          Default at the deadline: {chosen ? `link ${e.pushes.find((p) => p.root === chosen)?.title ?? shortHash(chosen)}` : "decline"}
          {e.default.declined.length && chosen ? `, decline ${e.default.declined.length}` : ""} — {e.default.why}
        </p>
      </CardContent>
    </Card>
  )
}

function ProposalCard({
  proposal: p,
  history,
  now,
  escalation,
  selected,
  onOpen,
}: {
  proposal: Proposal
  history: Proposal[]
  now: number
  escalation?: Escalation
  selected: boolean
  onOpen: (id: string) => void
}) {
  const approved = new Set(p.approvals.map((a) => a.by))
  const objectors = new Set(p.objections.map((o) => o.by))
  return (
    <Card size="sm" className={cn("transition-colors", selected && "ring-1 ring-ring")}>
      <CardContent className="space-y-2.5">
        <button type="button" onClick={() => onOpen(p.push)} className="flex w-full items-start gap-2 text-left">
          <AgentAvatar handle={p.author} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-sm font-medium hover:underline">{p.title}</span>
              <StatusBadge status={p.status} />
              {p.task_alias ? <Badge variant="outline" className="font-mono text-[10px]">{p.task_alias}</Badge> : null}
              {p.carried ? (
                <Badge variant="outline" className="border-teal-400/40 text-[10px] text-teal-200">
                  carried #{p.named_parent_seq} → #{p.parent_seq}
                </Badge>
              ) : null}
              {p.blocks_tip ? <Badge variant="outline" className="border-amber-400/40 text-[10px] text-amber-200">holds the race</Badge> : null}
              {p.timers.held ? <Badge variant="outline" className="border-amber-400/40 text-[10px] text-amber-200">held by {p.timers.held_by}</Badge> : null}
              {p.test.needed || p.test.result ? (
                <Badge variant="outline" className={cn("text-[10px]", p.test.result === "fail" ? "border-red-400/40 text-red-200" : p.test.result === "pass" ? "border-emerald-400/40 text-emerald-200" : "border-sky-400/40 text-sky-200")}>
                  tests {p.test.result ?? "needed"}
                </Badge>
              ) : null}
              {p.rounds > 1 ? <Badge variant="outline" className="border-orange-400/40 text-[10px] text-orange-200">round {p.rounds}</Badge> : null}
            </div>
            <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">
              {p.author} · {relativeTime(p.received_at, now)} · on #{p.parent_seq ?? "?"} · {p.files.join(", ")}
            </p>
          </div>
        </button>
        <ConsentMeter proposal={p} now={now} escalation={escalation} />
        {p.plan.askers.length > 0 || p.plan.members_rule || p.plan.declared.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1">
            <span className="mr-1 text-[10px] tracking-wide text-muted-foreground uppercase">asked</span>
            {p.plan.askers.map((h) => (
              <span key={h} className="inline-flex items-center gap-1">
                <AgentChip handle={h} muted={approved.has(h)} />
                <span className={cn("text-[10px]", approved.has(h) ? "text-emerald-300" : objectors.has(h) ? "text-red-300" : "text-muted-foreground")}>
                  {approved.has(h) ? "approved" : objectors.has(h) ? "objected" : p.plan.line_owners.includes(h) ? "owns lines" : "lazy owner"}
                </span>
              </span>
            ))}
            {p.plan.declared.filter((d) => d.rule !== "lazy").map((d) => (
              <Badge key={`${d.path}:${d.rule}`} variant="outline" className="font-mono text-[10px]">
                {d.rule} {d.path}
              </Badge>
            ))}
            {p.plan.members_rule ? <Badge variant="outline" className="text-[10px]">members rule</Badge> : null}
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground">No live owner is asked: no window, objections have no effect (a human can veto).</p>
        )}
        {p.approvals.filter((a) => !p.plan.askers.includes(a.by)).length > 0 ? (
          <div className="flex flex-wrap items-center gap-1">
            <span className="mr-1 text-[10px] tracking-wide text-muted-foreground uppercase">approvals</span>
            {p.approvals.filter((a) => !p.plan.askers.includes(a.by)).map((a) => <AgentChip key={a.by} handle={a.by} />)}
          </div>
        ) : null}
        {p.objections.length > 0 ? (
          <ul className="space-y-1.5 border-l-2 border-red-400/30 pl-2">
            {p.objections.map((o) => (
              <li key={o.id} className="text-[12px]">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium">{o.by}</span>
                  <Badge variant="outline" className="text-[10px] uppercase">{o.kind}</Badge>
                  <ObjectionBadge status={o.status} />
                  <span className="text-[10px] text-muted-foreground">round {o.episode}</span>
                  {o.answered_by ? (
                    <button type="button" className="font-mono text-[10px] text-muted-foreground hover:text-foreground" onClick={() => onOpen(o.answered_by!)}>
                      answered by {shortHash(o.answered_by, 8)}
                    </button>
                  ) : null}
                </div>
                <p className={cn("mt-0.5", o.blocking ? "text-red-300" : "text-muted-foreground")}>{o.reason}</p>
                {o.evidence ? (
                  <p className="font-mono text-[10px] text-red-200/80">
                    evidence {o.evidence.path} ·{" "}
                    {o.status === "confirmed"
                      ? "tester: passes on the parent, fails on the push"
                      : o.status === "refuted"
                        ? "tester refuted it: treated as a reason"
                        : o.status === "cleared"
                          ? "cleared by a revision holding the test"
                          : "waiting for a tester's verdict"}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
        {history.length > 0 || p.revisions.length > 1 ? (
          <div className="flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground">
            <span className="tracking-wide uppercase">negotiation</span>
            {p.revisions.map((r, i) => (
              <button
                key={r.push}
                type="button"
                onClick={() => onOpen(r.push)}
                className={cn("rounded border border-border px-1.5 py-0.5 font-mono hover:bg-muted/40", r.current && "border-foreground/40 text-foreground")}
              >
                v{i + 1} {STATUS_LABEL[r.status]}
              </button>
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function LinkRow({ link, now, selected, onOpen }: { link: Link; now: number; selected: boolean; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn("flex w-full items-start gap-3 rounded-md px-2 py-2 text-left hover:bg-muted/40", selected && "bg-muted/50",
        link.red_role === "first_bad" && "bg-red-500/5")}
    >
      <span className="w-8 shrink-0 pt-0.5 font-mono text-xs text-muted-foreground">#{link.seq}</span>
      <AgentAvatar handle={link.author} />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm">{link.title}</span>
          {link.task_alias ? <Badge variant="outline" className="font-mono text-[10px]">{link.task_alias}</Badge> : null}
          {link.kind === "revert" ? <Badge variant="outline" className="border-orange-400/40 text-[10px] text-orange-200">revert</Badge> : null}
          {link.carried_from ? (
            <Badge variant="outline" className="border-teal-400/40 text-[10px] text-teal-200">carried from #{link.carried_from_seq}</Badge>
          ) : null}
          <VerdictBadge verdict={link.verdict} />
          {link.red_role === "first_bad" ? <Badge variant="outline" className="border-red-500/50 text-[10px] text-red-200">first bad</Badge> : null}
          {link.red_role === "last_green" ? <Badge variant="outline" className="border-emerald-400/40 text-[10px] text-emerald-200">last green</Badge> : null}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[11px] text-muted-foreground">
          <span>{link.author}</span>
          <span>{shortHash(link.state)}</span>
          <span className="text-emerald-300/90">+{link.diffstat.added}</span>
          <span className="text-red-300/90">−{link.diffstat.removed}</span>
          <span className="truncate">{link.files.join(", ")}</span>
          <span title={`received ${relativeTime(link.received_at, now)}`}>{relativeTime(link.linked_at, now)}</span>
        </span>
      </span>
    </button>
  )
}
