import { useEffect, useMemo, useState } from "react"
import { ChevronRightIcon } from "lucide-react"
import { AgentAvatar, AgentChip } from "@/components/agent-avatar"
import { ConsentMeter } from "@/components/consent-meter"
import { DiffView } from "@/components/diff-view"
import { StatusBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { useGud } from "@/hooks/use-gud"
import { getMail } from "@/lib/api"
import { diffstat, extractDiff } from "@/lib/diff"
import { presentEmail, relativeTime, shortHash, shortId } from "@/lib/format"
import { isOpenStatus } from "@/lib/status"
import type { Link, Proposal, RawEmail } from "@/lib/types"
import { cn } from "@/lib/utils"

export function ChainView() {
  const [mode, setMode] = useState<"chain" | "emails">("chain")

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-2">
        <p className="text-xs text-muted-foreground">
          {mode === "chain" ? "Proposals and links" : "Newest first"}
        </p>
        <div className="flex gap-1">
          <Button type="button" size="sm" variant={mode === "chain" ? "secondary" : "outline"} onClick={() => setMode("chain")}>
            Chain
          </Button>
          <Button type="button" size="sm" variant={mode === "emails" ? "secondary" : "outline"} onClick={() => setMode("emails")}>
            Emails
          </Button>
        </div>
      </div>
      {mode === "chain" ? <ParsedChain /> : <EmailChain />}
    </div>
  )
}

function EmailChain() {
  const { now, state } = useGud()
  const [emails, setEmails] = useState<RawEmail[] | null>(null)
  const [error, setError] = useState("")
  const ordered = useMemo(
    () =>
      emails == null
        ? null
        : [...emails].sort((a, b) => b.received_at - a.received_at || b.message_id.localeCompare(a.message_id)),
    [emails],
  )

  useEffect(() => {
    let cancelled = false
    void getMail()
      .then((rows) => {
        if (!cancelled) {
          setEmails(rows)
          setError("")
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setEmails([])
          setError(err instanceof Error ? err.message : "Could not load the mailbox")
        }
      })
    return () => {
      cancelled = true
    }
  }, [state])

  return (
    <ScrollArea className="min-h-0 flex-1">
      {ordered == null ? (
        <div className="space-y-3 p-4">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : error ? (
        <p className="px-4 py-6 text-sm text-destructive">{error}</p>
      ) : ordered.length === 0 ? (
        <p className="px-4 py-6 text-sm text-muted-foreground">No emails.</p>
      ) : (
        <ol>
          {ordered.map((email) => (
            <EmailRow key={email.message_id} email={email} now={now} />
          ))}
        </ol>
      )}
    </ScrollArea>
  )
}

function EmailRow({ email, now }: { email: RawEmail; now: number }) {
  const [open, setOpen] = useState(false)
  const prose = presentEmail(email.body).prose
  const diff = extractDiff(email.body)
  const stat = diff ? diffstat(diff) : null
  const to = email.to.join(", ")
  const cc = email.cc.join(", ")
  return (
    <li className="border-b border-border/70 px-4 py-3">
      <header className="flex items-baseline gap-3">
        <span className="min-w-0 truncate font-mono text-[12px]">{email.from_addr}</span>
        <time
          className="ml-auto shrink-0 font-mono text-[11px] text-muted-foreground"
          dateTime={new Date(email.received_at * 1000).toISOString()}
        >
          {relativeTime(email.received_at, now)}
        </time>
      </header>
      <p className="mt-1 text-sm">{email.subject}</p>
      <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">
        To {to || "—"}
        {cc ? ` · Cc ${cc}` : ""}
        {email.in_reply_to ? ` · reply to ${shortId(email.in_reply_to)}` : ""}
      </p>
      {prose ? <p className="mt-2 text-[13px] leading-relaxed whitespace-pre-wrap">{prose}</p> : null}
      {diff && stat ? (
        <div className="mt-2">
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            className="inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
          >
            <ChevronRightIcon className={cn("size-3 transition-transform", open && "rotate-90")} />
            {open ? "Hide diff" : "Diff"}
            <span className="text-emerald-300/90">+{stat.added}</span>
            <span className="text-red-300/90">−{stat.removed}</span>
          </button>
          {open ? (
            <div className="mt-2 overflow-hidden rounded-md border border-border">
              <DiffView diff={diff} />
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  )
}

function ParsedChain() {
  const { state, now, loading, openId, setOpenId } = useGud()

  const groups = useMemo(() => {
    const proposals = state?.proposals ?? []
    const links = state?.links ?? []
    const byParent = new Map<string, Proposal[]>()
    for (const proposal of proposals) {
      const list = byParent.get(proposal.parent) ?? []
      list.push(proposal)
      byParent.set(proposal.parent, list)
    }
    const linkById = new Map(links.map((link) => [link.id, link]))
    return [...byParent.entries()]
      .map(([parent, items]) => ({
        parent,
        link: linkById.get(parent) ?? null,
        items: [...items].sort((a, b) => b.received_at - a.received_at),
      }))
      .sort((a, b) => (b.link?.seq ?? -1) - (a.link?.seq ?? -1))
  }, [state])

  const links = useMemo(
    () => [...(state?.links ?? [])].sort((a, b) => b.seq - a.seq),
    [state],
  )

  if (loading && !state) {
    return (
      <div className="space-y-3 p-4">
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    )
  }

  const tipId = state?.tip?.id

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-4">
        <section className="space-y-4">
          <header className="flex items-baseline justify-between">
            <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Proposals</h2>
            <span className="font-mono text-[11px] text-muted-foreground">{state?.proposals.length ?? 0}</span>
          </header>
          {groups.length === 0 ? (
            <p className="text-sm text-muted-foreground">No proposals.</p>
          ) : (
            groups.map((group) => {
              const open = group.items
                .filter((proposal) => isOpenStatus(proposal.status))
                .sort((a, b) => a.received_at - b.received_at)
              const blocker = group.parent === tipId ? open[0] : undefined
              return (
                <div key={group.parent} className="space-y-2">
                  <div className="flex items-baseline gap-2 px-1 font-mono text-[11px] text-muted-foreground">
                    <span>{group.link ? `on #${group.link.seq}` : "on parent"}</span>
                    <span>{group.link ? shortHash(group.link.state) : shortHash(group.parent, 18)}</span>
                    <span className="truncate">{group.link?.subject}</span>
                  </div>
                  {group.items.map((proposal) => (
                    <ProposalCard
                      key={proposal.id}
                      proposal={proposal}
                      now={now}
                      windowSeconds={state?.window_seconds ?? 60}
                      selected={openId === proposal.id}
                      blocksTip={blocker?.id === proposal.id}
                      onOpen={() => setOpenId(proposal.id)}
                    />
                  ))}
                </div>
              )
            })
          )}
        </section>
        <section className="space-y-1">
          <header className="mb-2 flex items-baseline justify-between">
            <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Links</h2>
            <span className="font-mono text-[11px] text-muted-foreground">{links.length}</span>
          </header>
          {links.map((link) => (
            <LinkRow
              key={link.id}
              link={link}
              now={now}
              selected={openId === link.id}
              onOpen={() => setOpenId(link.id)}
            />
          ))}
        </section>
      </div>
    </ScrollArea>
  )
}

function ProposalCard({
  proposal,
  now,
  windowSeconds,
  selected,
  blocksTip,
  onOpen,
}: {
  proposal: Proposal
  now: number
  windowSeconds: number
  selected: boolean
  blocksTip: boolean
  onOpen: () => void
}) {
  return (
    <button type="button" onClick={onOpen} className="block w-full text-left">
      <Card size="sm" className={cn("transition-colors hover:bg-muted/30", selected && "ring-1 ring-ring")}>
        <CardContent className="space-y-2.5">
          <div className="flex items-start gap-2">
            <AgentAvatar handle={proposal.author} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="text-sm font-medium">{proposal.subject}</span>
                <StatusBadge status={proposal.status} />
                {proposal.task ? (
                  <Badge variant="outline" className="font-mono text-[10px]">
                    {proposal.task}
                  </Badge>
                ) : null}
                {blocksTip ? (
                  <Badge variant="outline" className="border-amber-400/40 text-[10px] text-amber-200">
                    blocks tip
                  </Badge>
                ) : null}
                {proposal.test_result ? (
                  <Badge
                    variant="outline"
                    className={cn(
                      "text-[10px]",
                      proposal.test_result === "pass"
                        ? "border-emerald-400/40 text-emerald-200"
                        : "border-red-400/40 text-red-200",
                    )}
                  >
                    tests {proposal.test_result}
                  </Badge>
                ) : null}
              </div>
              <p className="mt-0.5 text-[11px] text-muted-foreground">
                {proposal.author} · {relativeTime(proposal.received_at, now)}
                {proposal.protected_rule ? ` · ${proposal.protected_rule}` : ""}
              </p>
            </div>
          </div>
          <ConsentMeter proposal={proposal} now={now} windowSeconds={windowSeconds} />
          {proposal.reason ? <p className="text-[11px] text-muted-foreground">{proposal.reason}</p> : null}
          {proposal.affected_owners.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1">
              <span className="mr-1 text-[10px] tracking-wide text-muted-foreground uppercase">owners</span>
              {proposal.affected_owners.map((owner) => (
                <AgentChip key={owner} handle={owner} muted={proposal.approvals.includes(owner)} />
              ))}
            </div>
          ) : null}
          {proposal.approvals.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1">
              <span className="mr-1 text-[10px] tracking-wide text-muted-foreground uppercase">approvals</span>
              {proposal.approvals.map((owner) => (
                <AgentChip key={owner} handle={owner} />
              ))}
            </div>
          ) : null}
          {proposal.objections.map((objection) => (
            <p key={objection.id} className="text-[12px] text-red-300">
              {objection.by}: {objection.reason}
            </p>
          ))}
        </CardContent>
      </Card>
    </button>
  )
}

function LinkRow({
  link,
  now,
  selected,
  onOpen,
}: {
  link: Link
  now: number
  selected: boolean
  onOpen: () => void
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        "flex w-full items-start gap-3 rounded-md px-2 py-2 text-left hover:bg-muted/40",
        selected && "bg-muted/50",
      )}
    >
      <span className="w-8 shrink-0 pt-0.5 font-mono text-xs text-muted-foreground">#{link.seq}</span>
      <AgentAvatar handle={link.author} />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm">{link.subject}</span>
          {link.task ? (
            <Badge variant="outline" className="font-mono text-[10px]">
              {link.task}
            </Badge>
          ) : null}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[11px] text-muted-foreground">
          <span>{link.author}</span>
          <span>{shortHash(link.state)}</span>
          <span className="text-emerald-300/90">+{link.diffstat.added}</span>
          <span className="text-red-300/90">−{link.diffstat.removed}</span>
          <span className="truncate">{link.diffstat.files.join(", ")}</span>
          <span>{relativeTime(link.received_at, now)}</span>
        </span>
      </span>
    </button>
  )
}
