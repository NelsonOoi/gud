import { useEffect, useState } from "react"
import { AgentAvatar } from "@/components/agent-avatar"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useGud } from "@/hooks/use-gud"
import { agentColor } from "@/lib/agents"
import { getBlame, getFile, getTree } from "@/lib/api"
import { duration, relativeTime, shortHash } from "@/lib/format"
import type { BlameLine, BlameResponse, FileResponse, TreeFile } from "@/lib/types"
import { cn } from "@/lib/utils"

export function FilesView() {
  const { state, links, setOpenId, now } = useGud()
  const tipId = state?.tip?.state ?? ""
  const [at, setAt] = useState(tipId)
  const [files, setFiles] = useState<TreeFile[]>([])
  const [path, setPath] = useState("")
  const [blame, setBlame] = useState<BlameResponse | null>(null)
  const [file, setFile] = useState<FileResponse | null>(null)
  const [loadingTree, setLoadingTree] = useState(false)
  const [loadingBlame, setLoadingBlame] = useState(false)
  const linkIds = links.map((l) => l.state).join("\n")

  useEffect(() => {
    if (!tipId) return
    if (!at || !linkIds.split("\n").includes(at)) setAt(tipId)
  }, [tipId, at, linkIds])

  useEffect(() => {
    if (!at) return
    let cancelled = false
    setLoadingTree(true)
    void getTree(at)
      .then((tree) => {
        if (cancelled) return
        setFiles(tree.files)
        setPath((current) => (tree.files.some((f) => f.path === current) ? current : tree.files.find((f) => !f.path.startsWith("."))?.path ?? tree.files[0]?.path ?? ""))
      })
      .catch(() => setFiles([]))
      .finally(() => {
        if (!cancelled) setLoadingTree(false)
      })
    return () => {
      cancelled = true
    }
  }, [at, state?.tip?.state])

  useEffect(() => {
    if (!at || !path) {
      setBlame(null)
      setFile(null)
      return
    }
    let cancelled = false
    setLoadingBlame(true)
    void Promise.allSettled([getBlame(path, at), getFile(path, at)])
      .then(([b, f]) => {
        if (cancelled) return
        setBlame(b.status === "fulfilled" ? b.value : null)
        setFile(f.status === "fulfilled" ? f.value : null)
      })
      .finally(() => {
        if (!cancelled) setLoadingBlame(false)
      })
    return () => {
      cancelled = true
    }
  }, [at, path])

  const selected = links.find((l) => l.state === at)

  return (
    <div className="flex h-full min-h-0">
      <div className="flex w-72 shrink-0 flex-col border-r border-border">
        <div className="border-b border-border p-2">
          {at ? (
            <Select value={at} onValueChange={(value) => { if (value) setAt(value) }}>
              <SelectTrigger className="w-full" size="sm">
                <SelectValue placeholder="Tree at…">{selected ? `#${selected.seq} · ${shortHash(selected.state)}` : "Tree at…"}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {links.map((l) => (
                  <SelectItem key={l.state} value={l.state}>
                    #{l.seq} · {shortHash(l.state)} · {l.title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <Skeleton className="h-7 w-full" />
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {loadingTree && files.length === 0 ? (
            <div className="space-y-2 p-3">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : (
            files.map((f) => (
              <button
                key={f.path}
                type="button"
                onClick={() => setPath(f.path)}
                className={cn("block w-full space-y-1 px-3 py-2 text-left hover:bg-muted/40", path === f.path && "bg-muted/50")}
              >
                <span className="flex items-baseline justify-between gap-2">
                  <span className="truncate font-mono text-[12px]">{f.path}</span>
                  <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                    {f.mode === "100755" ? "x " : f.mode === "120000" ? "→ " : ""}
                    {f.binary ? `${f.size} B` : f.lines}
                  </span>
                </span>
                <OwnerBar file={f} />
              </button>
            ))
          )}
        </div>
      </div>
      <div className="min-w-0 flex-1 overflow-auto">
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-border bg-background/95 px-3 py-2 backdrop-blur">
          <span className="truncate font-mono text-xs">{path || "Select a file"}</span>
          {blame ? (
            <span className="shrink-0 text-[10px] text-muted-foreground">
              ownership fades after {blame.own_links} links and {duration(blame.own_time)} (§13.1)
            </span>
          ) : null}
          {selected ? (
            <button type="button" className="shrink-0 font-mono text-[11px] text-muted-foreground hover:text-foreground" onClick={() => setOpenId(selected.state)}>
              at #{selected.seq} {shortHash(selected.state)}
            </button>
          ) : null}
        </div>
        {loadingBlame && !blame ? (
          <div className="space-y-1 p-3">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-4/5" />
          </div>
        ) : !blame ? (
          <p className="p-4 text-sm text-muted-foreground">This path is not in the tree at this link.</p>
        ) : blame.binary ? (
          <div className="p-4 text-sm">
            <BlameRow line={blame.lines[0]!} now={now} onOpenLink={(id) => setOpenId(id)} />
            <p className="mt-3 font-mono text-[12px] text-muted-foreground italic">
              {file?.kind === "symlink" ? `symlink → ${file.target}` : file?.placeholder ?? "binary file"}
            </p>
          </div>
        ) : (
          <div className="font-mono text-[12px] leading-5">
            {blame.lines.map((line) => (
              <BlameRow key={line.n} line={line} now={now} onOpenLink={(id) => setOpenId(id)} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function OwnerBar({ file }: { file: TreeFile }) {
  const entries = Object.entries(file.owners).sort((a, b) => b[1] - a[1])
  const total = entries.reduce((sum, [, n]) => sum + n, 0) || 1
  return (
    <div className="flex h-1.5 overflow-hidden rounded-full bg-muted">
      {entries.map(([owner, n]) => {
        const live = file.live_owners[owner] ?? 0
        return (
          <Tooltip key={owner}>
            <TooltipTrigger asChild>
              <span className="flex h-full" style={{ width: `${(n / total) * 100}%` }}>
                <span className="h-full" style={{ width: `${(live / n) * 100}%`, backgroundColor: agentColor(owner).bg }} />
                <span className="h-full opacity-30" style={{ width: `${((n - live) / n) * 100}%`, backgroundColor: agentColor(owner).bg }} />
              </span>
            </TooltipTrigger>
            <TooltipContent>
              {owner} · {n} {file.binary ? "file" : "lines"}
              {live < n ? ` (${n - live} expired)` : ""}
              {file.former.includes(owner) ? " · not a member here" : ""}
            </TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}

function BlameRow({ line, now, onOpenLink }: { line: BlameLine; now: number; onOpenLink: (id: string) => void }) {
  const color = agentColor(line.owner)
  const faded = !line.live || !line.member
  return (
    <div className={cn("flex hover:bg-muted/30", faded && "text-foreground/60")}>
      <span className="w-10 shrink-0 select-none pr-2 text-right text-muted-foreground/70">{line.n}</span>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={() => line.link && onOpenLink(line.link)}
            className={cn("flex w-40 shrink-0 items-center gap-1.5 border-l-2 px-2 text-left", faded && "opacity-50")}
            style={{ borderLeftColor: line.live ? color.bg : "transparent", borderLeftStyle: line.live ? "solid" : "dashed" }}
          >
            <AgentAvatar handle={line.owner} className="size-4" />
            <span className={cn("truncate text-[11px]", !line.member && "line-through")}>{line.owner}</span>
            <span className="ml-auto text-[10px] text-muted-foreground">#{line.seq}</span>
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">
          <p>
            {line.owner} · #{line.seq} {shortHash(line.link, 8)} · {line.intent || "—"}
          </p>
          <p className="text-muted-foreground">
            {!line.member
              ? "no longer a member: no say"
              : !line.voter
                ? "not a voter: no say"
                : line.live
                  ? line.expires_at
                    ? `live: asked on a change; expires ${relativeTime(line.expires_at, now)}`
                    : `live: asked on a change for ${line.links_left} more links`
                  : "expired: blame kept, no say (§13.1)"}
          </p>
        </TooltipContent>
      </Tooltip>
      <pre className="min-w-0 flex-1 px-3 whitespace-pre">{line.text || " "}</pre>
    </div>
  )
}
