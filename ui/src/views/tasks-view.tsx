import { useState } from "react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/agent-avatar"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { useGud } from "@/hooks/use-gud"
import { createTask, reviewBug } from "@/lib/api"
import { ApiError } from "@/lib/errors"
import { clockTime, relativeTime, shortId } from "@/lib/format"
import type { BugReport, Task } from "@/lib/types"
import { cn } from "@/lib/utils"

const TASK_TONE: Record<Task["status"], string> = {
  open: "border-zinc-500/40 text-zinc-300",
  in_progress: "border-amber-400/40 bg-amber-400/10 text-amber-200",
  done: "border-emerald-400/40 bg-emerald-400/10 text-emerald-200",
}

const SUB_TONE: Record<Task["subtasks"][number]["status"], string> = {
  open: "text-muted-foreground",
  proposed: "text-amber-200",
  landed: "text-emerald-200",
}

const BUG_TONE: Record<BugReport["status"], string> = {
  pending: "border-amber-400/40 bg-amber-400/10 text-amber-200",
  accepted: "border-emerald-400/40 bg-emerald-400/10 text-emerald-200",
  rejected: "border-zinc-500/40 text-zinc-300",
}

export function TasksView() {
  const { tasks, bugs, now, state, setOpenId, loading } = useGud()
  const ordered = [...tasks].sort((a, b) => b.created_at - a.created_at)
  const orderedBugs = [...bugs].sort((a, b) => b.received_at - a.received_at)
  const address = state?.bugs_address

  return (
    <ScrollArea className="h-full">
      <div className="mx-auto flex max-w-3xl flex-col gap-3 px-4 py-4">
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            {address
              ? `Email ${address} about a bug. A member reviews it, and the lead opens a ticket after that.`
              : "A human requests a task. The lead splits it. The requester is mailed when it is done."}
          </p>
          <NewTaskDialog />
        </div>
        {orderedBugs.map((bug) => (
          <BugCard key={bug.id} bug={bug} />
        ))}
        {loading && ordered.length === 0 ? <p className="text-sm text-muted-foreground">Loading tasks…</p> : null}
        {ordered.map((task) => (
          <Card key={task.id} size="sm">
            <CardHeader className="border-b">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <CardTitle className="flex items-center gap-2 text-sm">
                    <span className="font-mono text-muted-foreground">{task.id}</span>
                    {task.title}
                  </CardTitle>
                  <p className="mt-1 text-xs text-muted-foreground">{task.description}</p>
                </div>
                <Badge variant="outline" className={cn("uppercase", TASK_TONE[task.status])}>
                  {task.status.replaceAll("_", " ")}
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="space-y-2">
              <p className="text-[11px] text-muted-foreground">
                requested by {task.requester}
                {task.bug ? " · from a bug report" : ""} · {relativeTime(task.created_at, now)}
                {task.done_link ? (
                  <>
                    {" · "}
                    <button type="button" className="font-mono text-foreground hover:underline" onClick={() => setOpenId(task.done_link)}>
                      done {shortId(task.done_link)}
                    </button>
                  </>
                ) : null}
              </p>
              {task.notified_at ? (
                <p className="text-[12px] text-emerald-200">
                  requester notified at {clockTime(task.notified_at)}
                </p>
              ) : null}
              <ul className="space-y-1.5">
                {task.subtasks.length === 0 ? (
                  <li className="text-xs text-muted-foreground">No subtasks yet.</li>
                ) : (
                  task.subtasks.map((sub) => (
                    <li key={sub.id} className="flex items-center gap-2 text-sm">
                      <AgentAvatar handle={sub.assignee} className="size-5" />
                      <span className="font-mono text-[11px] text-muted-foreground">{sub.id}</span>
                      <span className="min-w-0 flex-1 truncate">{sub.title}</span>
                      <span className={cn("text-[11px] uppercase", SUB_TONE[sub.status])}>{sub.status}</span>
                      {sub.link_id ? (
                        <button
                          type="button"
                          className="font-mono text-[11px] text-muted-foreground hover:text-foreground"
                          onClick={() => setOpenId(sub.link_id)}
                        >
                          {shortId(sub.link_id)}
                        </button>
                      ) : null}
                    </li>
                  ))
                )}
              </ul>
            </CardContent>
          </Card>
        ))}
        {state && ordered.length === 0 && !loading ? (
          <p className="text-sm text-muted-foreground">No tasks yet.</p>
        ) : null}
      </div>
    </ScrollArea>
  )
}

function BugCard({ bug }: { bug: BugReport }) {
  const { state, refresh } = useGud()
  const reviewer = state?.members.find((member) => member.role === "human")?.handle ?? "@nelson"
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null)
  const waiting = bug.status === "pending" && Boolean(bug.review_by)

  async function review(action: "approve" | "reject") {
    setBusy(action)
    try {
      const result = await reviewBug(bug.id, { action, from: reviewer })
      await refresh()
      if (result.task_id) toast.success(`Opened ${result.task_id}`)
      else if (action === "approve") toast.success("Approved. The lead opens the ticket from that reply.")
      else toast.success("Rejected. No ticket was opened.")
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not review the bug report")
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card size="sm">
      <CardHeader className="border-b">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-sm">{bug.subject}</CardTitle>
            <p className="mt-1 line-clamp-3 text-xs text-muted-foreground">{bug.description}</p>
          </div>
          <Badge variant="outline" className={cn("uppercase", BUG_TONE[bug.status])}>
            {bug.status}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-[11px] text-muted-foreground">
          {bug.from}
          {bug.task_id ? ` · ticket ${bug.task_id}` : ""}
          {bug.review_by ? ` · ${bug.verdict === "reject" ? "rejected" : "reviewed"} by ${bug.review_by}` : ""}
        </p>
        {bug.status === "rejected" && bug.reason ? (
          <p className="text-xs text-muted-foreground">{bug.reason}</p>
        ) : null}
        {waiting ? (
          <p className="text-xs text-amber-200">Approved by {bug.review_by}. The lead opens the ticket from that reply.</p>
        ) : null}
        {bug.status === "pending" && !bug.review_by ? (
          <div className="flex gap-2">
            <Button size="sm" disabled={busy !== null} onClick={() => void review("approve")}>
              {busy === "approve" ? "Approving…" : "Approve"}
            </Button>
            <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void review("reject")}>
              {busy === "reject" ? "Rejecting…" : "Reject"}
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function NewTaskDialog() {
  const { state, refresh } = useGud()
  const humans = (state?.members ?? []).filter((member) => member.role === "human")
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState("")
  const [description, setDescription] = useState("")
  const [requester, setRequester] = useState(humans[0]?.handle ?? "@nelson")
  const [busy, setBusy] = useState(false)

  async function submit() {
    setBusy(true)
    try {
      const created = await createTask({ title: title.trim(), description: description.trim(), requester })
      await refresh()
      setOpen(false)
      setTitle("")
      setDescription("")
      toast.success(`Opened ${created.id}`)
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Could not open the task")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">New task</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New task</DialogTitle>
          <DialogDescription>@console will email the repo and the requester.</DialogDescription>
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
            Requester
            <Select value={requester} onValueChange={(value) => setRequester(value ?? "@nelson")}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Human member" />
              </SelectTrigger>
              <SelectContent>
                {humans.map((member) => (
                  <SelectItem key={member.handle} value={member.handle}>
                    {member.handle}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        </div>
        <DialogFooter>
          <Button disabled={busy || !title.trim() || !requester} onClick={() => void submit()}>
            Send task
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
