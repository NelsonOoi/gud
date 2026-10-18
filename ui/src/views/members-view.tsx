import { useState, type ComponentProps } from "react"
import { Copy, MoreHorizontal } from "lucide-react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/agent-avatar"
import { ConsentMeter } from "@/components/consent-meter"
import { copyText, joinCommand } from "@/components/share-repo"
import { JoinStatusBadge, RoleBadge, SignatureBadge, StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useGud } from "@/hooks/use-gud"
import { approveJoin, postMember } from "@/lib/api"
import { ApiError } from "@/lib/errors"
import { relativeTime, shortHash, shortId } from "@/lib/format"
import type { JoinRequest, Link, Member, MemberRole } from "@/lib/types"
import { cn } from "@/lib/utils"

const ROLES: MemberRole[] = ["agent", "lead", "tester", "console", "human"]

const INVALID_JOIN_REASON =
  "Self-signature does not verify, or From is not the requested address."

function isMemberRole(role: string): role is MemberRole {
  return (ROLES as readonly string[]).includes(role)
}

export function MembersView() {
  const { state, joins, loading, now, refresh, setOpenId } = useGud()
  const [busyJoin, setBusyJoin] = useState<string | null>(null)
  const members = state?.members ?? []
  const removed = state?.removed_members ?? []
  const links = state?.links ?? []
  const pending = (state?.proposals ?? []).filter(
    (proposal) => proposal.protected_rule === "human" && proposal.status === "needs_approval",
  )

  async function approve(id: string) {
    setBusyJoin(id)
    try {
      const result = await approveJoin(id)
      await refresh()
      setOpenId(result.push_id)
      toast.success("Approved. The member lands with no test needed.")
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not approve the join")
    } finally {
      setBusyJoin(null)
    }
  }

  async function remove(handle: string) {
    try {
      const result = await postMember({ action: "remove", handle })
      await refresh()
      setOpenId(result.push_id)
      toast.success(`Removal push sent for ${handle}`)
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not remove the member")
    }
  }

  return (
    <div className="h-full overflow-auto">
      <div className="mx-auto flex max-w-5xl flex-col gap-4 px-4 py-4">
        <div className="flex items-center justify-between">
          <p className="text-xs text-muted-foreground">
            Membership changes are pushes. They land when a human approves and the tests pass.
          </p>
          <AddMemberDialog
            onCreated={async (id) => {
              await refresh()
              setOpenId(id)
            }}
          />
        </div>
        <JoinRequests
          joins={joins}
          now={now}
          loading={loading}
          busyId={busyJoin}
          onApprove={(id) => void approve(id)}
          onOpen={setOpenId}
        />
        {pending.length > 0 ? (
          <section className="space-y-2">
            <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Pending membership</h2>
            {pending.map((proposal) => (
              <button key={proposal.id} type="button" className="block w-full text-left" onClick={() => setOpenId(proposal.id)}>
                <Card size="sm">
                  <CardContent className="space-y-2">
                    <div className="flex items-center gap-2">
                      <AgentAvatar handle={proposal.author} />
                      <span className="text-sm font-medium">{proposal.subject}</span>
                      <StatusBadge status={proposal.status} />
                      <span className="ml-auto font-mono text-[11px] text-muted-foreground">{shortId(proposal.id)}</span>
                    </div>
                    <ConsentMeter proposal={proposal} now={now} windowSeconds={state?.window_seconds ?? 60} />
                    <p className="text-[11px] text-muted-foreground">{proposal.reason}</p>
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
              <TableHead>Fingerprint</TableHead>
              <TableHead>Sponsor</TableHead>
              <TableHead>Votes</TableHead>
              <TableHead className="text-right">Lines</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.map((member) => (
              <TableRow key={member.handle}>
                <TableCell>
                  <span className="flex items-center gap-2 font-medium">
                    <AgentAvatar handle={member.handle} />
                    {member.handle}
                  </span>
                </TableCell>
                <TableCell>
                  <RoleBadge role={member.role} />
                </TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{member.address}</TableCell>
                <TableCell className="font-mono text-xs">{member.fingerprint ?? "—"}</TableCell>
                <TableCell className="text-xs">{member.sponsor ?? "—"}</TableCell>
                <TableCell className="text-xs">{member.votes ? "yes" : "no"}</TableCell>
                <TableCell className="text-right font-mono text-xs">{member.lines_owned}</TableCell>
                <TableCell>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${member.handle}`}>
                        <MoreHorizontal />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem variant="destructive" onClick={() => void remove(member.handle)}>
                        Remove
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <section className="space-y-2">
          <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Removed</h2>
          {removed.length === 0 ? (
            <p className="text-sm text-muted-foreground">No removed members.</p>
          ) : (
            <ul className="space-y-2">
              {removed.map((member) => {
                const when = removalLink(member, links)
                return (
                  <li key={member.handle} className="flex items-center gap-3 rounded-md border border-border px-3 py-2">
                    <AgentAvatar handle={member.handle} />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm line-through">{member.handle}</span>
                        <RoleBadge role={member.role} />
                      </div>
                      <p className="text-[11px] text-muted-foreground">
                        {member.lines_owned} orphaned lines
                        {when ? ` · removed ${relativeTime(when.received_at, now)} in #${when.seq}` : ""}
                      </p>
                    </div>
                    {when ? (
                      <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setOpenId(when.id)}>
                        {shortId(when.id)}
                      </Button>
                    ) : null}
                  </li>
                )
              })}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}

function removalLink(member: Member, links: Link[]): Link | undefined {
  if (member.removed_by_link) {
    const exact = links.find((link) => link.id === member.removed_by_link)
    if (exact) return exact
  }
  return [...links]
    .reverse()
    .find((link) => /remove/i.test(link.subject) && link.subject.includes(member.handle))
}

function AddMemberDialog({ onCreated }: { onCreated: (id: string) => Promise<void> }) {
  const [open, setOpen] = useState(false)
  const [handle, setHandle] = useState("@sub-3")
  const [role, setRole] = useState<MemberRole>("agent")
  const [address, setAddress] = useState("sub-3@gud.primitive.email")
  const [publicKey, setPublicKey] = useState("")
  const [sponsor, setSponsor] = useState("@nelson")
  const [busy, setBusy] = useState(false)

  async function submit() {
    setBusy(true)
    try {
      const result = await postMember({
        action: "add",
        handle: handle.trim(),
        role,
        address: address.trim(),
        public_key: publicKey.trim(),
        sponsor: sponsor.trim(),
      })
      await onCreated(result.push_id)
      setOpen(false)
      toast.success("Membership push sent")
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not add the member")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">Add member</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add member</DialogTitle>
          <DialogDescription>
            @console sends a membership push, and you approve it. The tests still have to pass.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <label className="grid gap-1 text-xs">
            Handle
            <Input value={handle} onChange={(event) => setHandle(event.target.value)} />
          </label>
          <label className="grid gap-1 text-xs">
            Role
            <Select value={role} onValueChange={(value) => setRole((value ?? "agent") as MemberRole)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ROLES.map((item) => (
                  <SelectItem key={item} value={item}>
                    {item}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <label className="grid gap-1 text-xs">
            Address
            <Input value={address} onChange={(event) => setAddress(event.target.value)} />
          </label>
          <label className="grid gap-1 text-xs">
            Public key
            <Input
              value={publicKey}
              onChange={(event) => setPublicKey(event.target.value)}
              placeholder="blank: generate a key on the server"
              className="font-mono"
            />
          </label>
          <label className="grid gap-1 text-xs">
            Sponsor
            <Input value={sponsor} onChange={(event) => setSponsor(event.target.value)} />
          </label>
        </div>
        <DialogFooter>
          <Button disabled={busy || !handle.trim()} onClick={() => void submit()}>
            Add and approve
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function JoinRequests({
  joins,
  now,
  loading,
  busyId,
  onApprove,
  onOpen,
}: {
  joins: JoinRequest[]
  now: number
  loading: boolean
  busyId: string | null
  onApprove: (id: string) => void
  onOpen: (id: string) => void
}) {
  return (
    <Card size="sm">
      <CardContent className="space-y-3">
        <div>
          <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Join requests</h2>
          <p className="mt-1 text-[11px] text-muted-foreground">
            An outside agent mails a self-signed join. You approve it. The tests still have to pass.
          </p>
        </div>
        {loading && joins.length === 0 ? (
          <p className="text-sm text-muted-foreground">Loading join requests…</p>
        ) : joins.length === 0 ? (
          <EmptyJoins />
        ) : (
          <div className="space-y-2">
            {joins.map((join) =>
              join.valid ? (
                <JoinRow key={join.id} join={join} now={now} busy={busyId === join.id} onApprove={onApprove} onOpen={onOpen} />
              ) : (
                <Tooltip key={join.id}>
                  <TooltipTrigger asChild>
                    <JoinRow join={join} now={now} busy={busyId === join.id} onApprove={onApprove} onOpen={onOpen} />
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs text-left">{INVALID_JOIN_REASON}</TooltipContent>
                </Tooltip>
              ),
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function JoinRow({
  join,
  now,
  busy,
  onApprove,
  onOpen,
  className,
  ...props
}: {
  join: JoinRequest
  now: number
  busy: boolean
  onApprove: (id: string) => void
  onOpen: (id: string) => void
} & ComponentProps<"div">) {
  const canApprove = join.valid && (join.status === "pending" || join.status === "proposed")
  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-md border border-border px-3 py-2",
        !join.valid && "cursor-help opacity-50",
        className,
      )}
      {...props}
    >
      <AgentAvatar handle={join.handle} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{join.handle}</span>
          {isMemberRole(join.role) ? (
            <RoleBadge role={join.role} />
          ) : (
            <span className="text-xs uppercase text-muted-foreground">{join.role}</span>
          )}
          <SignatureBadge valid={join.valid} />
          <JoinStatusBadge status={join.status} />
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground">
          <span className="font-mono">{join.address}</span>
          <span> · </span>
          <span className="font-mono" title={join.public_key}>
            {shortHash(join.public_key)}
          </span>
          <span> · </span>
          <span>{join.sponsor ?? "—"}</span>
          <span> · </span>
          <time className="font-mono">{relativeTime(join.received_at, now)}</time>
        </p>
        <p className="mt-0.5 text-[11px] text-muted-foreground">{join.reason || "—"}</p>
      </div>
      {canApprove ? (
        <Button size="sm" className="shrink-0" disabled={busy} onClick={() => onApprove(join.id)}>
          Approve
        </Button>
      ) : null}
      {join.status === "proposed" && join.push_id ? (
        <Button variant="outline" size="sm" className="shrink-0" onClick={() => onOpen(join.push_id ?? "")}>
          View push
        </Button>
      ) : null}
    </div>
  )
}

function EmptyJoins() {
  const command = joinCommand()
  return (
    <div className="space-y-2">
      <p className="text-sm text-muted-foreground">
        An outside agent joins from their own mail account, without the host's API key.
      </p>
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
