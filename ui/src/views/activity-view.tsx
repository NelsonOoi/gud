import { useMemo, useState } from "react"
import {
  CircleCheck,
  ClipboardList,
  FlaskConical,
  GitCommitHorizontal,
  Hand,
  Link2,
  Mail,
  MessageSquare,
  OctagonX,
  Scale,
  Server,
  Siren,
  Eye,
  type LucideIcon,
} from "lucide-react"
import { AgentAvatar } from "@/components/agent-avatar"
import { Badge } from "@/components/ui/badge"
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
import { describeEvent } from "@/lib/describe"
import { clockTime, relativeTime } from "@/lib/format"
import { EVENT_GROUPS } from "@/lib/status"
import { cn } from "@/lib/utils"

const ICONS: Record<string, LucideIcon> = {
  push: GitCommitHorizontal,
  review: Eye,
  objection: OctagonX,
  comment: MessageSquare,
  vote: CircleCheck,
  test: FlaskConical,
  link: Link2,
  task: ClipboardList,
  claim: Hand,
  escalation: Scale,
  tip: Siren,
  host: Server,
}

const GROUPS = ["push", "review", "objection", "comment", "vote", "test", "link", "task", "claim", "escalation", "tip", "host"]

export function ActivityView() {
  const { events, now, setOpenId, loading, connected, mode } = useGud()
  const [group, setGroup] = useState("all")
  const rows = useMemo(() => {
    const all = [...events].reverse()
    return group === "all" ? all : all.filter((e) => EVENT_GROUPS[e.type] === group)
  }, [events, group])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <p className="text-xs text-muted-foreground">
          {rows.length} events · every §19 event the host derived, newest first
          {mode === "live" ? (connected ? " · live" : " · reconnecting…") : ""}
        </p>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm">{group === "all" ? "All events" : group}</Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>Filter</DropdownMenuLabel>
            <DropdownMenuRadioGroup value={group} onValueChange={setGroup}>
              <DropdownMenuRadioItem value="all">All events</DropdownMenuRadioItem>
              {GROUPS.map((g) => (
                <DropdownMenuRadioItem key={g} value={g}>{g}</DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <ul>
          {loading && rows.length === 0 ? <li className="px-4 py-6 text-sm text-muted-foreground">Loading activity…</li> : null}
          {rows.map((e) => {
            const g = EVENT_GROUPS[e.type] ?? "push"
            const Icon = ICONS[g] ?? Mail
            const { who, text, target } = describeEvent(e)
            const first = who.split(", ")[0] ?? ""
            return (
              <li key={e.id} className="border-b border-border/70">
                <button
                  type="button"
                  disabled={!target}
                  onClick={() => target && setOpenId(target)}
                  className={cn("flex w-full items-start gap-3 px-4 py-2 text-left", target && "hover:bg-muted/40")}
                >
                  <Icon className={cn("mt-0.5 size-4 shrink-0 text-muted-foreground", g === "tip" && "text-red-300", g === "objection" && "text-red-300")} />
                  {first.startsWith("@") ? <AgentAvatar handle={first} className="size-5" /> : <span className="size-5" />}
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      {who ? <span className="text-sm font-medium">{who}</span> : null}
                      <Badge variant="outline" className="font-mono text-[10px]">{e.type}</Badge>
                    </span>
                    <span className="mt-0.5 block truncate text-[12px] text-muted-foreground">{text}</span>
                  </span>
                  <time className="shrink-0 font-mono text-[11px] text-muted-foreground" title={clockTime(e.at)}>
                    {relativeTime(e.at, now)}
                  </time>
                </button>
              </li>
            )
          })}
          {rows.length === 0 && !loading ? <li className="px-4 py-6 text-sm text-muted-foreground">Nothing in this filter.</li> : null}
        </ul>
      </ScrollArea>
    </div>
  )
}
