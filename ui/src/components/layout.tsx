import { useEffect, useState } from "react"
import { Activity, Files, GitCommitHorizontal, ListTodo, Users } from "lucide-react"
import { DetailSheet } from "@/components/detail-sheet"
import { ShareRepo } from "@/components/share-repo"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { useGud } from "@/hooks/use-gud"
import { shortHash } from "@/lib/format"
import { isOpenStatus } from "@/lib/status"
import { cn } from "@/lib/utils"
import { ActivityView } from "@/views/activity-view"
import { ChainView } from "@/views/chain-view"
import { FilesView } from "@/views/files-view"
import { MembersView } from "@/views/members-view"
import { TasksView } from "@/views/tasks-view"

const VIEWS = [
  { id: "chain", label: "Chain", icon: GitCommitHorizontal },
  { id: "files", label: "Files", icon: Files },
  { id: "tasks", label: "Tasks", icon: ListTodo },
  { id: "members", label: "Members", icon: Users },
  { id: "activity", label: "Activity", icon: Activity },
] as const

type ViewId = (typeof VIEWS)[number]["id"]

function parseHash(): ViewId {
  const raw = window.location.hash.replace(/^#\/?/, "")
  return VIEWS.some((view) => view.id === raw) ? (raw as ViewId) : "chain"
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
  const openCount = state?.proposals.filter((proposal) => isOpenStatus(proposal.status)).length ?? 0
  const title = VIEWS.find((item) => item.id === view)?.label ?? "Chain"

  return (
    <div className="flex h-full min-h-0 bg-background text-foreground">
      <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-sidebar">
        <div className="px-3 py-3">
          <div className="flex items-baseline gap-2">
            <span className="text-base font-semibold tracking-tight">gud</span>
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">chain</span>
          </div>
          <p className="mt-2 truncate font-mono text-[11px] text-muted-foreground" title={state?.repo}>
            {state?.repo ?? "…"}
          </p>
          <div className="mt-1.5 flex items-center gap-1.5 font-mono text-[11px]">
            <span className="size-1.5 rounded-full bg-emerald-400" />
            {tip ? (
              <>
                <span>#{tip.seq}</span>
                <span className="text-muted-foreground">{shortHash(tip.state)}</span>
              </>
            ) : (
              <span className="text-muted-foreground">no tip</span>
            )}
          </div>
        </div>
        <Separator />
        <nav className="flex flex-1 flex-col gap-0.5 p-2">
          {VIEWS.map((item) => {
            const Icon = item.icon
            const active = view === item.id
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
                {item.id === "chain" && openCount > 0 ? (
                  <Badge variant="secondary" className="ml-auto h-4 px-1.5 font-mono text-[10px]">
                    {openCount}
                  </Badge>
                ) : null}
              </button>
            )
          })}
        </nav>
        <div className="px-3 py-3 font-mono text-[10px] text-muted-foreground">
          window {state?.window_seconds ?? "—"}s
        </div>
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
        </main>
      </div>
      <DetailSheet />
    </div>
  )
}
