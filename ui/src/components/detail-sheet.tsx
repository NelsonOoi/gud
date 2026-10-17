import { useEffect, useMemo, useState } from "react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/agent-avatar"
import { DiffView } from "@/components/diff-view"
import { EmailThread } from "@/components/email-thread"
import { KindBadge, StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { useGud } from "@/hooks/use-gud"
import { getDiff, getThread, postApprove, postReply } from "@/lib/api"
import { ApiError } from "@/lib/errors"
import { relativeTime, shortHash, shortId } from "@/lib/format"
import { isOpenStatus } from "@/lib/status"
import type { EmailView, Member } from "@/lib/types"

export function DetailSheet() {
  const { openId, setOpenId, state, now, refresh } = useGud()
  const [emails, setEmails] = useState<EmailView[]>([])
  const [diff, setDiff] = useState("")
  const [loading, setLoading] = useState(false)
  const [actor, setActor] = useState("")
  const [reply, setReply] = useState("")
  const [busy, setBusy] = useState(false)

  const proposal = state?.proposals.find((item) => item.id === openId) ?? null
  const link = state?.links.find((item) => item.id === openId) ?? null
  const agents = useMemo(
    () =>
      (state?.members ?? []).filter((member) => member.role === "agent" || member.role === "lead"),
    [state?.members],
  )

  useEffect(() => {
    if (!openId) return
    let cancelled = false
    setLoading(true)
    void (async () => {
      const [threadResult, diffResult] = await Promise.allSettled([getThread(openId), getDiff(openId)])
      if (cancelled) return
      if (threadResult.status === "fulfilled") setEmails(threadResult.value)
      else {
        setEmails([])
        toast.error(threadResult.reason instanceof Error ? threadResult.reason.message : "Could not load the thread")
      }
      if (diffResult.status === "fulfilled") setDiff(diffResult.value)
      else setDiff("")
      setLoading(false)
    })()
    return () => {
      cancelled = true
    }
  }, [openId])

  useEffect(() => {
    const first = agents[0]?.handle
    if (first && !agents.some((member) => member.handle === actor)) setActor(first)
  }, [agents, actor])

  const handleFor = (address: string) => {
    const roster = [...(state?.members ?? []), ...(state?.removed_members ?? [])]
    return roster.find((member: Member) => member.address === address)?.handle ?? address
  }

  async function reloadThread() {
    if (!openId) return
    const [thread, nextDiff] = await Promise.all([getThread(openId), getDiff(openId)])
    setEmails(thread)
    setDiff(nextDiff)
  }

  async function approve() {
    if (!openId || !actor) return
    setBusy(true)
    try {
      await postApprove({ target: openId, as: actor })
      await refresh()
      await reloadThread()
      toast.success(`Approved as ${actor}`)
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Approve failed")
    } finally {
      setBusy(false)
    }
  }

  async function sendReply() {
    if (!openId || !reply.trim()) return
    setBusy(true)
    try {
      await postReply({ target: openId, from: "@nelson", text: reply.trim() })
      setReply("")
      await refresh()
      await reloadThread()
      toast.success("Reply posted as @nelson")
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Reply failed")
    } finally {
      setBusy(false)
    }
  }

  const title = proposal?.subject ?? link?.subject ?? "Message"
  const author = proposal?.author ?? link?.author ?? ""
  const received = proposal?.received_at ?? link?.received_at ?? 0
  const pending = proposal != null && isOpenStatus(proposal.status)

  return (
    <Sheet open={openId != null} onOpenChange={(open) => { if (!open) setOpenId(null) }}>
      <SheetContent
        side="right"
        className="w-full gap-0 overflow-hidden p-0 sm:max-w-none"
        style={{ width: "min(760px, 100vw)" }}
      >
        <SheetHeader className="border-b border-border pr-10">
          <div className="flex items-center gap-2">
            {author ? <AgentAvatar handle={author} /> : null}
            <div className="min-w-0">
              <SheetTitle className="truncate text-left">{title}</SheetTitle>
              <SheetDescription className="truncate text-left">
                {author} · {received ? relativeTime(received, now) : ""}
                {link ? ` · #${link.seq} · ${shortHash(link.state)}` : ` · ${shortId(openId ?? "")}`}
              </SheetDescription>
            </div>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {proposal ? <StatusBadge status={proposal.status} /> : <KindBadge kind="link" />}
            {link?.task ? <KindBadge kind={link.task} /> : null}
            {proposal?.task ? <KindBadge kind={proposal.task} /> : null}
            {link ? (
              <span className="font-mono text-[11px] text-muted-foreground">
                +{link.diffstat.added} −{link.diffstat.removed}
              </span>
            ) : null}
          </div>
          {proposal?.reason ? (
            <p className="mt-2 text-xs text-muted-foreground">{proposal.reason}</p>
          ) : null}
          {link?.intent ? <p className="mt-2 text-xs text-muted-foreground">{link.intent}</p> : null}
        </SheetHeader>

        <Tabs defaultValue="thread" className="min-h-0 flex-1 gap-0">
          <div className="border-b border-border px-4 py-2">
            <TabsList>
              <TabsTrigger value="thread">Thread</TabsTrigger>
              <TabsTrigger value="diff">Diff</TabsTrigger>
            </TabsList>
          </div>
          <TabsContent value="thread" className="min-h-0 overflow-auto">
            {loading ? <SheetSkeleton /> : <EmailThread emails={emails} now={now} handleFor={handleFor} />}
          </TabsContent>
          <TabsContent value="diff" className="min-h-0 overflow-auto">
            {loading ? <SheetSkeleton /> : <DiffView diff={diff} />}
          </TabsContent>
        </Tabs>

        {pending ? (
          <div className="space-y-3 border-t border-border p-4">
            <div className="flex items-center gap-2">
              <Select value={actor} onValueChange={(value) => setActor(value ?? "")}>
                <SelectTrigger className="w-48" size="sm">
                  <SelectValue placeholder="Approve as…" />
                </SelectTrigger>
                <SelectContent>
                  {agents.map((member) => (
                    <SelectItem key={member.handle} value={member.handle}>
                      {member.handle}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button size="sm" disabled={!actor || busy} onClick={() => void approve()}>
                Approve
              </Button>
            </div>
            <Textarea
              value={reply}
              onChange={(event) => setReply(event.target.value)}
              placeholder="approve, object &lt;reason&gt;, or veto &lt;reason&gt;"
              rows={3}
            />
            <div className="flex items-center justify-between gap-3">
              <p className="text-[11px] text-muted-foreground">
                Reply as @nelson. veto blocks the push. object counts when @nelson owns an affected line.
                approve counts toward quorum or owner consent.
              </p>
              <Button size="sm" variant="secondary" disabled={busy || !reply.trim()} onClick={() => void sendReply()}>
                Reply as @nelson
              </Button>
            </div>
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  )
}

function SheetSkeleton() {
  return (
    <div className="space-y-3 p-4">
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-24 w-full" />
    </div>
  )
}
