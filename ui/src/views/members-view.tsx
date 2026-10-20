import { useState } from "react"
import { Copy } from "lucide-react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/agent-avatar"
import { ConsentMeter } from "@/components/consent-meter"
import { copyText, joinCommand } from "@/components/share-repo"
import { JoinStatusBadge, RoleBadge, StatusBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useActor, useGud } from "@/hooks/use-gud"
import { admit } from "@/lib/api"
import { ApiError } from "@/lib/errors"
import { relativeTime, shortHash } from "@/lib/format"
import type { JoinRequest, Member } from "@/lib/types"
import { cn } from "@/lib/utils"

export function MembersView() {
  const { members, proposals, now, loading, refresh, setOpenId, state } = useGud()
  const actor = useActor()
  const [busy, setBusy] = useState<string | null>(null)
  const pending = proposals.filter((p) => p.live && p.plan.members_rule)

  async function doAdmit(join: JoinRequest) {
    setBusy(join.id)
    try {
      const r = await admit({ join: join.id, as: actor.human ?? undefined })
      await refresh()
      setOpenId(r.id)
      toast.success(`Membership push for ${join.handle} sent by ${r.as}`)
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not admit")
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="h-full overflow-auto">
      <div className="mx-auto flex max-w-5xl flex-col gap-4 px-4 py-4">
        <p className="text-xs text-muted-foreground">
          Members are lines in <code>.gud/members</code> at the tip. Changing them is a push that needs a human approve
          (the members rule); agents and humans vote, testers and the console do not.
        </p>
        <Card size="sm">
          <CardContent className="space-y-3">
            <div>
              <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Join requests</h2>
              <p className="mt-1 text-[11px] text-muted-foreground">
                A self-signed <code>join</code>. Admitting it is a members push citing it (<code>Gud-Join</code>).
              </p>
            </div>
            {loading && members.joins.length === 0 ? <p className="text-sm text-muted-foreground">Loading…</p> : null}
            {members.joins.length === 0 && !loading ? <EmptyJoins /> : null}
            {members.joins.map((join) => (
              <div key={join.id} className={cn("flex items-start gap-3 rounded-md border border-border px-3 py-2", join.status === "member" && "opacity-60")}>
                <AgentAvatar handle={join.handle} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{join.handle}</span>
                    {join.role ? <RoleBadge role={join.role} /> : null}
                    <JoinStatusBadge status={join.status} />
                  </div>
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    <span className="font-mono">{join.address}</span> · <span className="font-mono">{join.key_short}</span> · sponsor{" "}
                    {join.sponsor ?? "—"} · {join.received_at ? relativeTime(join.received_at, now) : ""}
                  </p>
                  {join.reason ? <p className="mt-0.5 text-[11px] text-muted-foreground">{join.reason}</p> : null}
                </div>
                {join.status === "pending" && actor.enabled && actor.human ? (
                  <Button size="sm" disabled={busy === join.id} onClick={() => void doAdmit(join)}>Admit</Button>
                ) : null}
                {join.push ? (
                  <Button size="sm" variant="outline" onClick={() => setOpenId(join.push)}>View push</Button>
                ) : null}
              </div>
            ))}
          </CardContent>
        </Card>
        {pending.length > 0 ? (
          <section className="space-y-2">
            <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Pending membership pushes</h2>
            {pending.map((p) => (
              <button key={p.id} type="button" className="block w-full text-left" onClick={() => setOpenId(p.push)}>
                <Card size="sm">
                  <CardContent className="space-y-2">
                    <div className="flex items-center gap-2">
                      <AgentAvatar handle={p.author} />
                      <span className="text-sm font-medium">{p.title}</span>
                      <StatusBadge status={p.status} />
                      <span className="ml-auto font-mono text-[11px] text-muted-foreground">{shortHash(p.push)}</span>
                    </div>
                    <ConsentMeter proposal={p} now={now} />
                  </CardContent>
                </Card>
              </button>
            ))}
          </section>
        ) : null}
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Handle</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Address</TableHead>
              <TableHead>Key</TableHead>
              <TableHead>Sponsor</TableHead>
              <TableHead>Delivery</TableHead>
              <TableHead className="text-right">Lines (live)</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.members.map((m) => (
              <MemberRow key={m.handle} member={m} signer={state?.ui.signers.some((s) => s.handle === m.handle) ?? false} />
            ))}
          </TableBody>
        </Table>
        <section className="space-y-2">
          <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Removed</h2>
          {members.removed.length === 0 ? (
            <p className="text-sm text-muted-foreground">No removed members.</p>
          ) : (
            <ul className="space-y-2">
              {members.removed.map((m) => (
                <li key={m.handle} className="flex items-center gap-3 rounded-md border border-border px-3 py-2">
                  <AgentAvatar handle={m.handle} />
                  <span className="text-sm line-through">{m.handle}</span>
                  <RoleBadge role={m.role} />
                  <span className="text-[11px] text-muted-foreground">{m.lines} lines still blamed to them (no say)</span>
                  <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setOpenId(m.removed_link)}>
                    removed in #{m.removed_seq}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}

function MemberRow({ member: m, signer }: { member: Member; signer: boolean }) {
  const d = m.delivery
  return (
    <TableRow>
      <TableCell>
        <span className="flex items-center gap-2 font-medium">
          <AgentAvatar handle={m.handle} />
          {m.handle}
          {signer ? <Badge variant="outline" className="border-pink-400/40 text-[10px] text-pink-200">UI signs</Badge> : null}
        </span>
      </TableCell>
      <TableCell>
        <span className="flex items-center gap-1.5">
          <RoleBadge role={m.role} />
          {m.votes ? <span className="text-[10px] text-muted-foreground">votes</span> : null}
        </span>
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">{m.address}</TableCell>
      <TableCell className="font-mono text-xs">
        <Tooltip>
          <TooltipTrigger asChild>
            <span>{m.key_short ?? "—"}</span>
          </TooltipTrigger>
          <TooltipContent className="font-mono">{m.key ?? "no key (unsigned mail only)"}</TooltipContent>
        </Tooltip>
      </TableCell>
      <TableCell className="text-xs">{m.sponsor ?? "—"}</TableCell>
      <TableCell className="text-xs">
        {d ? (
          <span className={cn(d.registered ? "text-foreground" : "text-muted-foreground")}>
            {d.channel}
            {d.journal_head ? <span className="ml-1 font-mono text-[10px] text-muted-foreground">{d.acked ?? 0}/{d.journal_head}</span> : null}
          </span>
        ) : "—"}
      </TableCell>
      <TableCell className="text-right font-mono text-xs">
        {m.lines}
        {m.lines_live !== m.lines ? <span className="text-muted-foreground"> ({m.lines_live})</span> : null}
      </TableCell>
    </TableRow>
  )
}

function EmptyJoins() {
  const command = joinCommand()
  return (
    <div className="space-y-2">
      <p className="text-sm text-muted-foreground">An outside agent joins from their own mail account with their own key.</p>
      <div className="flex items-start gap-2">
        <pre className="min-w-0 flex-1 overflow-x-auto rounded-md border border-border bg-muted/40 px-2 py-1.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-foreground">
          {command}
        </pre>
        <Button variant="outline" size="icon-sm" aria-label="Copy join command" onClick={() => void copyText(command)}>
          <Copy />
        </Button>
      </div>
    </div>
  )
}
