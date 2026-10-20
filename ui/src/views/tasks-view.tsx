import { useState } from "react"
import { TriangleAlert } from "lucide-react"
import { toast } from "sonner"
import { AgentAvatar, AgentChip } from "@/components/agent-avatar"
import { ClaimBadge, TaskBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Textarea } from "@/components/ui/textarea"
import { useActor, useGud } from "@/hooks/use-gud"
import { comment, openTask } from "@/lib/api"
import { ApiError } from "@/lib/errors"
import { countdown, relativeTime, shortHash } from "@/lib/format"
import type { BugReport, Claim, Overlap, Task } from "@/lib/types"
import { cn } from "@/lib/utils"

export function TasksView() {
  const { tasks, now, loading, state } = useGud()
  const byId = new Map(tasks.tasks.map((t) => [t.id, t]))
  const roots = tasks.tasks.filter((t) => !t.parent || !byId.has(t.parent)).sort((a, b) => b.opened_at - a.opened_at)
  const open = roots.filter((t) => t.status !== "done")
  const done = roots.filter((t) => t.status === "done")

  return (
    <ScrollArea className="h-full">
      <div className="mx-auto flex max-w-3xl flex-col gap-3 px-4 py-4">
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            Leaderless: any member opens a task, an agent claims it with a scope and a lease, splits it into subtasks,
            and replay derives done.
            {state?.red.red ? " The tip is red: nothing becomes done until it is green." : ""}
          </p>
          <NewTaskDialog />
        </div>
        {tasks.overlaps.length > 0 ? <Overlaps overlaps={tasks.overlaps} byId={byId} /> : null}
        {tasks.bugs.filter((b) => !b.task).map((bug) => <BugCard key={bug.id} bug={bug} />)}
        {loading && roots.length === 0 ? <p className="text-sm text-muted-foreground">Loading tasks…</p> : null}
        {open.map((t) => <TaskCard key={t.id} task={t} byId={byId} now={now} />)}
        {done.length > 0 ? <h2 className="mt-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">Done</h2> : null}
        {done.map((t) => <TaskCard key={t.id} task={t} byId={byId} now={now} />)}
        {!loading && roots.length === 0 ? <p className="text-sm text-muted-foreground">No tasks yet.</p> : null}
      </div>
    </ScrollArea>
  )
}

function Overlaps({ overlaps, byId }: { overlaps: Overlap[]; byId: Map<string, Task> }) {
  return (
    <Card size="sm" className="border-amber-400/30">
      <CardContent className="space-y-1.5">
        <p className="flex items-center gap-1.5 text-xs font-medium text-amber-200">
          <TriangleAlert className="size-3.5" /> Overlapping claims (§16.4) — holders settle it in comments
        </p>
        {overlaps.map((o) => (
          <p key={o.claims.join(":")} className="flex flex-wrap items-center gap-1.5 text-[12px]">
            <AgentChip handle={o.holders[0]} />
            <span className="font-mono text-[10px] text-muted-foreground">{byId.get(o.tasks[0])?.alias}</span>
            <span className="text-muted-foreground">and</span>
            <AgentChip handle={o.holders[1]} />
            <span className="font-mono text-[10px] text-muted-foreground">{byId.get(o.tasks[1])?.alias}</span>
            <span className="text-muted-foreground">both cover</span>
            <span className="font-mono text-[11px]">{(o.paths.length ? o.paths : o.globs).join(", ")}</span>
          </p>
        ))}
      </CardContent>
    </Card>
  )
}

function ClaimRow({ claim, now }: { claim: Claim; now: number }) {
  const lapsing = claim.status === "active" && claim.lapses_at - now < 5 * 60_000
  return (
    <div className="flex flex-wrap items-center gap-2 text-[12px]">
      <AgentAvatar handle={claim.holder} className="size-5" />
      <span className="font-medium">{claim.holder}</span>
      <ClaimBadge status={claim.status} />
      <span className="font-mono text-[11px] text-muted-foreground">{claim.files.join(", ")}</span>
      {claim.status === "active" ? (
        <span className={cn("font-mono text-[10px]", lapsing ? "text-amber-200" : "text-muted-foreground")}>
          lease {countdown(claim.lapses_at, now)}
        </span>
      ) : null}
      {claim.overlaps.length > 0 && claim.status === "active" ? (
        <Badge variant="outline" className="border-amber-400/40 text-[10px] text-amber-200">overlaps {claim.overlaps.length}</Badge>
      ) : null}
    </div>
  )
}

function TaskCard({ task: t, byId, now, depth = 0 }: { task: Task; byId: Map<string, Task>; now: number; depth?: number }) {
  const { setOpenId } = useGud()
  const children = t.children.map((id) => byId.get(id)).filter((x): x is Task => Boolean(x))
  const active = t.claims.filter((c) => c.status === "active")
  const past = t.claims.filter((c) => c.status !== "active")
  const body = (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">
        {t.kind === "fix" ? "derived from the red tip" : `opened by ${t.author ?? "—"}`}
        {t.requester ? ` · requester ${t.requester}` : ""} · {relativeTime(t.opened_at, now)}
        {t.bug ? ` · from bug ${shortHash(t.bug, 8)}` : ""}
        {t.after.length ? ` · after ${t.after.map((a) => byId.get(a)?.alias ?? shortHash(a, 8)).join(", ")}` : ""}
      </p>
      {t.kind === "fix" ? (
        <p className="text-[12px] text-red-200">
          Cites{" "}
          <button type="button" className="font-mono hover:underline" onClick={() => t.cited_link && setOpenId(t.cited_link)}>
            #{t.cited_seq} {shortHash(t.cited_link)}
          </button>{" "}
          by {t.cited_author}.{" "}
          {t.offer_until && now < t.offer_until && !t.holder
            ? `Only ${t.offered_to} can claim it for ${countdown(t.offer_until, now)}.`
            : t.holder ? "" : "Anyone can claim it."}{" "}
          Done when the tip is green.
        </p>
      ) : null}
      {t.has_criteria ? (
        <p className="font-mono text-[11px] text-muted-foreground">
          criteria: {[...t.accept_paths, ...t.accept_cmds.map((c) => `$ ${c}`)].join(" · ")}
        </p>
      ) : null}
      {t.files_hint.length ? <p className="font-mono text-[11px] text-muted-foreground">suggested scope {t.files_hint.join(", ")}</p> : null}
      {active.map((c) => <ClaimRow key={c.id} claim={c} now={now} />)}
      {active.length === 0 && t.status !== "done" ? <p className="text-[11px] text-muted-foreground">Unclaimed.</p> : null}
      {past.length ? (
        <p className="text-[10px] text-muted-foreground">
          earlier claims: {past.map((c) => `${c.holder} (${c.status})`).join(", ")}
        </p>
      ) : null}
      {t.waiting === "acceptance" ? (
        <p className="text-[12px] text-violet-200">Work is done; waiting for {t.requester} to accept.</p>
      ) : t.waiting === "criteria" ? (
        <p className="text-[12px] text-violet-200">Work is done; waiting for a tester&apos;s acceptance run.</p>
      ) : null}
      {t.links.length ? (
        <p className="flex flex-wrap gap-1.5 font-mono text-[11px]">
          {t.links.map((l) => (
            <button key={l.state} type="button" className="text-muted-foreground hover:text-foreground" onClick={() => setOpenId(l.state)}>
              #{l.seq} {l.author}
            </button>
          ))}
        </p>
      ) : null}
      {t.no_change.length ? (
        <p className="text-[11px] text-muted-foreground">no-change: {t.no_change.map((n) => n.reason).join("; ")}</p>
      ) : null}
      {children.length ? (
        <div className="space-y-2 border-l border-border pl-3">
          {children.map((c) => <TaskCard key={c.id} task={c} byId={byId} now={now} depth={depth + 1} />)}
        </div>
      ) : null}
    </div>
  )
  if (depth > 0) {
    return (
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm">
          <button type="button" className="font-mono text-[11px] text-muted-foreground hover:underline" onClick={() => setOpenId(t.id)}>{t.alias}</button>
          <span className="min-w-0 flex-1 truncate">{t.title}</span>
          <TaskBadge status={t.status} />
        </div>
        {body}
      </div>
    )
  }
  return (
    <Card size="sm" className={cn(t.kind === "fix" && t.status !== "done" && "border-red-400/30")}>
      <CardHeader className="border-b">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-sm">
              <button type="button" className="font-mono text-muted-foreground hover:underline" onClick={() => setOpenId(t.id)}>{t.alias}</button>
              <span className="truncate">{t.title}</span>
              {t.kind === "fix" ? <Badge variant="outline" className="border-red-400/40 text-[10px] text-red-200">fix</Badge> : null}
            </CardTitle>
            {t.description ? <p className="mt-1 line-clamp-3 text-xs text-muted-foreground">{t.description}</p> : null}
          </div>
          <TaskBadge status={t.status} />
        </div>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  )
}

function BugCard({ bug }: { bug: BugReport }) {
  const { refresh } = useGud()
  const actor = useActor()
  const [busy, setBusy] = useState(false)

  async function accept() {
    setBusy(true)
    try {
      const r = await openTask({ title: bug.subject || "bug report", description: bug.text, bug: bug.id, as: actor.human ?? undefined })
      await refresh()
      toast.success(`Task opened from the bug report`, { description: shortHash(r.id) })
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not open the task")
    } finally {
      setBusy(false)
    }
  }

  async function reject() {
    setBusy(true)
    try {
      await comment({ target: bug.id, text: "not a bug", verdict: "reject", as: actor.human ?? undefined })
      await refresh()
      toast.success("Rejected")
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not reject")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card size="sm" className="border-amber-400/20">
      <CardContent className="space-y-2">
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="border-amber-400/40 text-[10px] text-amber-200 uppercase">bug report</Badge>
          <span className="text-sm font-medium">{bug.subject}</span>
          <span className="ml-auto text-[11px] text-muted-foreground">{bug.from}</span>
        </div>
        <p className="line-clamp-3 text-xs text-muted-foreground">{bug.text}</p>
        {actor.enabled && actor.human ? (
          <div className="flex gap-2">
            <Button size="sm" disabled={busy} onClick={() => void accept()}>Open task</Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void reject()}>Reject</Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function NewTaskDialog() {
  const { refresh, members } = useGud()
  const actor = useActor()
  const humans = members.members.filter((m) => m.role === "human")
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState("")
  const [description, setDescription] = useState("")
  const [requester, setRequester] = useState("")
  const [accept, setAccept] = useState("")
  const [busy, setBusy] = useState(false)

  async function submit() {
    setBusy(true)
    try {
      const created = await openTask({
        title: title.trim(),
        description: description.trim(),
        requester: requester.trim() || undefined,
        accept: accept.split(/[\s,]+/).filter(Boolean),
        as: actor.taskAs ?? undefined,
      })
      await refresh()
      setOpen(false)
      setTitle("")
      setDescription("")
      setAccept("")
      toast.success(`Task opened by ${created.as}`, { description: shortHash(created.id) })
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not open the task")
    } finally {
      setBusy(false)
    }
  }

  if (!actor.enabled || !actor.taskAs) return null
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">New task</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New task</DialogTitle>
          <DialogDescription>A signed task from {actor.taskAs}. Agents get task.opened and claim it.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <label className="grid gap-1 text-xs">
            Title
            <Input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Add per-user rate limiting" />
          </label>
          <label className="grid gap-1 text-xs">
            Description
            <Textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={4} />
          </label>
          <label className="grid gap-1 text-xs">
            Acceptance tests (paths or globs, optional)
            <Input value={accept} onChange={(event) => setAccept(event.target.value)} placeholder="tests/test_rate.py" className="font-mono" />
          </label>
          <label className="grid gap-1 text-xs">
            Requester (a human; default: the author if human)
            <Input value={requester} onChange={(event) => setRequester(event.target.value)} placeholder={humans[0]?.handle ?? "@human"} className="font-mono" />
          </label>
        </div>
        <DialogFooter>
          <Button disabled={busy || !title.trim()} onClick={() => void submit()}>Open task</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
