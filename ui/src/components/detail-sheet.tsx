import { useEffect, useState } from "react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/agent-avatar"
import { ConsentMeter } from "@/components/consent-meter"
import { ChangeView } from "@/components/diff-view"
import { MessageThread } from "@/components/email-thread"
import { KindBadge, StatusBadge, TaskBadge, VerdictBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { useActor, useGud } from "@/hooks/use-gud"
import { comment, getDiff, getThread, vote } from "@/lib/api"
import { ApiError } from "@/lib/errors"
import { relativeTime, shortHash } from "@/lib/format"
import { explain } from "@/lib/status"
import type { DiffResponse, ThreadResponse } from "@/lib/types"

export function DetailSheet() {
  const { openId, setOpenId, links, proposals, tasks, escalations, now, refresh } = useGud()
  const actor = useActor()
  const [thread, setThread] = useState<ThreadResponse | null>(null)
  const [diff, setDiff] = useState<DiffResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)

  const link = links.find((l) => l.state === openId || l.push === openId) ?? null
  const proposal = link ? null : proposals.find((p) => p.id === openId || p.push === openId) ?? null
  const task = tasks.tasks.find((t) => t.id === openId) ?? null
  const escalation = proposal?.escalation ? escalations.find((e) => e.id === proposal.escalation) : undefined

  async function load(id: string, cancelled: () => boolean) {
    const [t, d] = await Promise.allSettled([getThread(id), getDiff(id)])
    if (cancelled()) return
    setThread(t.status === "fulfilled" ? t.value : null)
    setDiff(d.status === "fulfilled" ? d.value : null)
    if (t.status === "rejected" && d.status === "rejected") {
      toast.error(t.reason instanceof Error ? t.reason.message : "Could not load that id")
    }
  }

  useEffect(() => {
    if (!openId) return
    let cancelled = false
    setLoading(true)
    setText("")
    void load(openId, () => cancelled).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => {
      cancelled = true
    }
  }, [openId])

  async function act(kind: "approve" | "hold" | "object" | "veto" | "comment" | "accept") {
    if (!openId) return
    setBusy(true)
    try {
      const target = proposal?.push ?? task?.id ?? link?.push ?? openId
      const result =
        kind === "comment"
          ? await comment({ target, text: text.trim(), as: actor.human ?? undefined })
          : await vote({ kind: kind === "accept" ? "approve" : kind, target, text: text.trim() || undefined,
            as: actor.human ?? undefined })
      setText("")
      await refresh()
      await load(openId, () => false)
      toast.success(`${kind} sent as ${result.as}`, { description: result.logged ? `logged ${shortHash(result.id)}` : "not logged" })
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : `${kind} failed`)
    } finally {
      setBusy(false)
    }
  }

  const title = link?.title ?? proposal?.title ?? task?.title ?? String(thread?.head.title ?? "Message")
  const author = link?.author ?? proposal?.author ?? task?.author ?? ""
  const when = link?.linked_at ?? proposal?.received_at ?? task?.opened_at ?? null
  const live = Boolean(proposal?.live)
  const acceptable = task != null && task.waiting === "acceptance" && task.requester === actor.human

  return (
    <Sheet open={openId != null} onOpenChange={(open) => { if (!open) setOpenId(null) }}>
      <SheetContent side="right" className="w-full gap-0 overflow-hidden p-0 sm:max-w-none" style={{ width: "min(780px, 100vw)" }}>
        <SheetHeader className="border-b border-border pr-10">
          <div className="flex items-center gap-2">
            {author ? <AgentAvatar handle={author} /> : null}
            <div className="min-w-0">
              <SheetTitle className="truncate text-left">{title}</SheetTitle>
              <SheetDescription className="truncate text-left">
                {author || "—"}
                {when ? ` · ${relativeTime(when, now)}` : ""}
                {link ? ` · #${link.seq} · ${shortHash(link.state)}` : ` · ${shortHash(openId)}`}
              </SheetDescription>
            </div>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {link ? <KindBadge kind="link" /> : null}
            {proposal ? <StatusBadge status={proposal.status} /> : null}
            {task ? <TaskBadge status={task.status} /> : null}
            {link ? <VerdictBadge verdict={link.verdict} /> : null}
            {link?.carried_from ? (
              <Badge variant="outline" className="border-teal-400/40 text-[10px] text-teal-200">
                carried from #{link.carried_from_seq}
              </Badge>
            ) : null}
            {proposal?.carried ? (
              <Badge variant="outline" className="border-teal-400/40 text-[10px] text-teal-200">
                carried #{proposal.named_parent_seq} → #{proposal.parent_seq}
              </Badge>
            ) : null}
            {link?.task_alias || proposal?.task_alias ? (
              <Badge variant="outline" className="font-mono text-[10px]">{link?.task_alias ?? proposal?.task_alias}</Badge>
            ) : null}
            {link ? (
              <span className="font-mono text-[11px] text-muted-foreground">
                +{link.diffstat.added} −{link.diffstat.removed} · tree {shortHash(link.tree_hash)}
              </span>
            ) : null}
          </div>
          {proposal ? <p className="mt-2 text-xs text-muted-foreground">{explain(proposal.status) || proposal.reason}</p> : null}
          {proposal?.live ? (
            <div className="mt-2">
              <ConsentMeter proposal={proposal} now={now} escalation={escalation} />
            </div>
          ) : null}
          {link?.intent && link.intent !== link.title ? <p className="mt-2 text-xs whitespace-pre-wrap text-muted-foreground">{link.intent}</p> : null}
          {task?.description ? <p className="mt-2 text-xs whitespace-pre-wrap text-muted-foreground">{task.description}</p> : null}
        </SheetHeader>

        <Tabs defaultValue="thread" className="min-h-0 flex-1 gap-0">
          <div className="border-b border-border px-4 py-2">
            <TabsList>
              <TabsTrigger value="thread">Thread</TabsTrigger>
              <TabsTrigger value="diff" disabled={!diff}>Change</TabsTrigger>
            </TabsList>
          </div>
          <TabsContent value="thread" className="min-h-0 overflow-auto">
            {loading ? <SheetSkeleton /> : <MessageThread messages={thread?.messages ?? []} now={now} onOpen={setOpenId} />}
          </TabsContent>
          <TabsContent value="diff" className="min-h-0 overflow-auto">
            {loading ? <SheetSkeleton /> : <ChangeView diff={diff} />}
          </TabsContent>
        </Tabs>

        {actor.enabled && actor.human ? (
          <div className="space-y-2 border-t border-border p-4">
            <Textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={live ? "reason (object / veto), or a comment" : "comment"}
              rows={2}
            />
            <div className="flex flex-wrap items-center gap-2">
              {live ? (
                <>
                  <Button size="sm" disabled={busy} onClick={() => void act("approve")}>Approve</Button>
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void act("hold")}>Hold</Button>
                  <Button size="sm" variant="outline" disabled={busy || !text.trim()} onClick={() => void act("object")}>Object</Button>
                  <Button size="sm" variant="destructive" disabled={busy} onClick={() => void act("veto")}>Veto</Button>
                </>
              ) : null}
              {acceptable ? <Button size="sm" disabled={busy} onClick={() => void act("accept")}>Accept task</Button> : null}
              <Button size="sm" variant="secondary" disabled={busy || !text.trim()} onClick={() => void act("comment")}>Comment</Button>
              <span className="ml-auto text-[11px] text-muted-foreground">signed as {actor.human}</span>
            </div>
          </div>
        ) : (
          <p className="border-t border-border px-4 py-3 text-[11px] text-muted-foreground">
            Read-only. Start <code>gud host serve --ui-home &lt;a human&apos;s GUD_HOME&gt;</code> to vote from here.
          </p>
        )}
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
