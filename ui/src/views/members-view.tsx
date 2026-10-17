import { useState } from "react"
import { MoreHorizontal } from "lucide-react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/agent-avatar"
import { ConsentMeter } from "@/components/consent-meter"
import { RoleBadge, StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
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
import { postMember } from "@/lib/api"
import { ApiError } from "@/lib/errors"
import { relativeTime, shortId } from "@/lib/format"
import type { Link, Member, MemberRole } from "@/lib/types"

const ROLES: MemberRole[] = ["agent", "lead", "tester", "console", "human"]

export function MembersView() {
  const { state, now, refresh, setOpenId } = useGud()
  const members = state?.members ?? []
  const removed = state?.removed_members ?? []
  const links = state?.links ?? []
  const pending = (state?.proposals ?? []).filter(
    (proposal) => proposal.protected_rule === "quorum" && /^(Add|Remove) @/.test(proposal.subject),
  )

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
            Membership changes are pushes. They land when more than half the voters approve.
          </p>
          <AddMemberDialog
            onCreated={async (id) => {
              await refresh()
              setOpenId(id)
            }}
          />
        </div>
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
  return [...links]
    .reverse()
    .find((link) => /remove/i.test(link.subject) && link.subject.includes(member.handle))
}

function AddMemberDialog({ onCreated }: { onCreated: (id: string) => Promise<void> }) {
  const [open, setOpen] = useState(false)
  const [handle, setHandle] = useState("@sub-3")
  const [role, setRole] = useState<MemberRole>("agent")
  const [address, setAddress] = useState("sub-3@gud.primitive.email")
  const [publicKey, setPublicKey] = useState("ed25519:c3ViLTMta2V5")
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
            @console sends a membership push. It stays pending until quorum.
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
            <Input value={publicKey} onChange={(event) => setPublicKey(event.target.value)} className="font-mono" />
          </label>
          <label className="grid gap-1 text-xs">
            Sponsor
            <Input value={sponsor} onChange={(event) => setSponsor(event.target.value)} />
          </label>
        </div>
        <DialogFooter>
          <Button disabled={busy || !handle.trim()} onClick={() => void submit()}>
            Send membership push
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
