import { useMemo, useState } from "react"
import {
  Bell,
  CircleCheck,
  FlaskConical,
  GitCommitHorizontal,
  Link2,
  ListTodo,
  Mail,
  OctagonX,
  Split,
  UserRound,
  type LucideIcon,
} from "lucide-react"
import { AgentAvatar } from "@/components/agent-avatar"
import { KindBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ScrollArea } from "@/components/ui/scroll-area"
import { useGud } from "@/hooks/use-gud"
import { relativeTime } from "@/lib/format"
import { ACTIVITY_KINDS } from "@/lib/status"
import { cn } from "@/lib/utils"

const ICONS: Record<string, LucideIcon> = {
  push: GitCommitHorizontal,
  object: OctagonX,
  approve: CircleCheck,
  test: FlaskConical,
  task: ListTodo,
  subtask: Split,
  "task-done": CircleCheck,
  notify: Bell,
  human: UserRound,
  link: Link2,
}

export function ActivityView() {
  const { activity, now, setOpenId, loading } = useGud()
  const [kind, setKind] = useState("all")
  const rows = useMemo(
    () => (kind === "all" ? activity : activity.filter((item) => item.kind === kind)),
    [activity, kind],
  )

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <p className="text-xs text-muted-foreground">{rows.length} messages</p>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm">
              {kind === "all" ? "All kinds" : kind}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>Filter by kind</DropdownMenuLabel>
            <DropdownMenuRadioGroup value={kind} onValueChange={setKind}>
              <DropdownMenuRadioItem value="all">All kinds</DropdownMenuRadioItem>
              {ACTIVITY_KINDS.map((item) => (
                <DropdownMenuRadioItem key={item} value={item}>
                  {item}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <ul>
          {loading && rows.length === 0 ? (
            <li className="px-4 py-6 text-sm text-muted-foreground">Loading activity…</li>
          ) : null}
          {rows.map((item) => {
            const Icon = ICONS[item.kind] ?? Mail
            const clickable = Boolean(item.target)
            return (
              <li key={item.id} className="border-b border-border/70">
                <button
                  type="button"
                  disabled={!clickable}
                  onClick={() => {
                    if (item.target) setOpenId(item.target)
                  }}
                  className={cn(
                    "flex w-full items-start gap-3 px-4 py-2.5 text-left",
                    clickable && "hover:bg-muted/40",
                  )}
                >
                  <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  <AgentAvatar handle={item.from.startsWith("@") ? item.from : "@?"} className="size-5" />
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium">{item.from}</span>
                      <KindBadge kind={item.kind} />
                      <span className="truncate text-sm text-muted-foreground">{item.subject}</span>
                    </span>
                    <span className="mt-0.5 block truncate text-[12px] text-muted-foreground">{item.summary}</span>
                  </span>
                  <time className="shrink-0 font-mono text-[11px] text-muted-foreground">
                    {relativeTime(item.at, now)}
                  </time>
                </button>
              </li>
            )
          })}
          {rows.length === 0 && !loading ? (
            <li className="px-4 py-6 text-sm text-muted-foreground">Nothing in this filter.</li>
          ) : null}
        </ul>
      </ScrollArea>
    </div>
  )
}
