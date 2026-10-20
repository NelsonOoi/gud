import { useEffect, useState } from "react"
import { Activity, Files, GitCommitHorizontal, ListTodo, Server, Users } from "lucide-react"
import { DetailSheet } from "@/components/detail-sheet"
import { ShareRepo } from "@/components/share-repo"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { useGud } from "@/hooks/use-gud"
import { duration, relativeTime, shortHash } from "@/lib/format"
import { cn } from "@/lib/utils"
import { ActivityView } from "@/views/activity-view"
import { ChainView } from "@/views/chain-view"
import { FilesView } from "@/views/files-view"
import { HostPanel } from "@/views/host-view"
import { MembersView } from "@/views/members-view"
import { TasksView } from "@/views/tasks-view"

const VIEWS = [
  { id: "chain", label: "Chain", icon: GitCommitHorizontal },
  { id: "files", label: "Files", icon: Files },
  { id: "tasks", label: "Tasks", icon: ListTodo },
  { id: "members", label: "Members", icon: Users },
  { id: "activity", label: "Activity", icon: Activity },
  { id: "host", label: "Host", icon: Server },
] as const

type ViewId = (typeof VIEWS)[number]["id"]

function parseHash(): ViewId {
  const raw = window.location.hash.replace(/^#\/?/, "")
  return VIEWS.some((view) => view.id === raw) ? (raw as ViewId) : "chain"
}

function SidebarStatus() {
  const { state, host, needs, now, connected, mode, setOpenId } = useGud()
  if (!state) return null
  const cfg = state.config
  const agents = host?.deliveries.filter((d) => d.channel !== "email") ?? []
  return (
    <div className="grid gap-2.5 border-t border-border px-3 py-3 text-[11px]">
      {needs.member && needs.items.length > 0 ? (
        <div className="grid gap-1">
          <p className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">Needs {needs.member}</p>
          {needs.items.slice(0, 6).map((item, i) => {
            const target = String(item.push ?? item.escalation ?? item.task ?? item.claim ?? "")
            return (
              <button key={i} type="button" className="truncate text-left text-foreground/90 hover:underline" onClick={() => target && setOpenId(String(item.push ?? (item.pushes as string[] | undefined)?.[0] ?? item.task ?? target))}>
                {item.type} {item.alias ? String(item.alias) : shortHash(target, 8)}
              </button>
            )
          })}
        </div>
      ) : null}
      <div className="grid gap-0.5">
        <p className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">Agents connected</p>
        {agents.length === 0 ? (
          <p className="text-muted-foreground">none (email only)</p>
        ) : (
          agents.map((d) => (
            <p key={d.handle} className="flex items-center gap-1.5">
              <span className="size-1.5 rounded-full bg-emerald-400" />
              <span>{d.handle}</span>
              <span className="text-muted-foreground">{d.channel}</span>
            </p>
          ))
        )}
      </div>
      <div className="grid gap-0.5 font-mono text-[10px] text-muted-foreground">
        <span>
          log #{state.log_head.seq} · {relativeTime(state.log_head.time, now)}
          {mode === "live" ? (connected ? " · live" : " · …") : ""}
        </span>
        {host ? (
          <span className={host.audit.ok ? "text-emerald-300/80" : "text-red-300"}>audit {host.audit.ok ? "clean" : `exit ${host.audit.exit_status}`}</span>
        ) : null}
        {cfg ? (
          <span>
            W {duration(cfg.window)} · G {duration(cfg.grace)} · lease {duration(cfg.lease)}
          </span>
        ) : null}
        <span>{state.ui.actions && state.ui.human ? `UI signs as ${state.ui.human}` : "read-only UI"}</span>
      </div>
    </div>
  )
}

export function Shell() {
  const { state, mode } = useGud()
  const [view, setView] = useState<ViewId>(parseHash)

  useEffect(() => {
    const onHash = () => setView(parseHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  function go(next: ViewId) {
    window.location.hash = `#/${next}`
    setView(next)
  }

  const tip = state?.tip ?? null
  const red = state?.red.red ?? false
  const badges: Partial<Record<ViewId, number>> = {
    chain: state?.counts.proposals_live ?? 0,
    tasks: state?.counts.tasks_open ?? 0,
  }
  const title = VIEWS.find((item) => item.id === view)?.label ?? "Chain"

  return (
    <div className="flex h-full min-h-0 bg-background text-foreground">
      <aside className="flex h-full min-h-0 w-60 shrink-0 flex-col overflow-x-hidden overflow-y-auto border-r border-border bg-sidebar">
        <div className="min-w-0 px-3 py-3">
          <div className="flex items-baseline gap-2">
            <span className="text-base font-semibold tracking-tight">gud</span>
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">v1 host</span>
          </div>
          <p className="mt-2 truncate font-mono text-[11px] text-muted-foreground" title={state?.repo}>
            {state?.repo ?? "…"}
          </p>
          <div className="mt-1.5 flex items-center gap-1.5 font-mono text-[11px]">
            <span className={cn("size-1.5 rounded-full", red ? "bg-red-400" : "bg-emerald-400")} />
            {tip ? (
              <>
                <span>#{tip.seq}</span>
                <span className="text-muted-foreground">{shortHash(tip.state)}</span>
                {red ? <span className="text-red-300">red</span> : null}
              </>
            ) : (
              <span className="text-muted-foreground">no genesis</span>
            )}
          </div>
        </div>
        <Separator />
        <nav className="flex flex-1 flex-col gap-0.5 p-2">
          {VIEWS.map((item) => {
            const Icon = item.icon
            const active = view === item.id
            const count = badges[item.id] ?? 0
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => go(item.id)}
                className={cn(
                  "flex h-8 items-center gap-2 rounded-md px-2 text-left text-sm text-muted-foreground hover:bg-sidebar-accent hover:text-foreground",
                  active && "bg-sidebar-accent font-medium text-foreground",
                )}
              >
                <Icon className="size-4" />
                <span>{item.label}</span>
                {count > 0 ? (
                  <Badge variant="secondary" className="ml-auto h-4 px-1.5 font-mono text-[10px]">
                    {count}
                  </Badge>
                ) : null}
              </button>
            )
          })}
        </nav>
        <SidebarStatus />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-11 shrink-0 items-center gap-3 border-b border-border px-4">
          <h1 className="text-sm font-medium">{title}</h1>
          <div className="ml-auto flex items-center gap-2">
            <ShareRepo repo={state?.repo} />
            {mode === "mock" ? (
              <Badge variant="outline" className="border-amber-400/40 text-[10px] tracking-wide text-amber-200 uppercase">
                mock data
              </Badge>
            ) : (
              <Badge variant="outline" className="text-[10px] uppercase">
                live
              </Badge>
            )}
          </div>
        </header>
        <main className="min-h-0 flex-1">
          {view === "chain" ? <ChainView /> : null}
          {view === "files" ? <FilesView /> : null}
          {view === "tasks" ? <TasksView /> : null}
          {view === "members" ? <MembersView /> : null}
          {view === "activity" ? <ActivityView /> : null}
          {view === "host" ? <HostPanel /> : null}
        </main>
      </div>
      <DetailSheet />
    </div>
  )
}
