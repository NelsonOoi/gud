import { useEffect, useState } from "react"
import { AgentAvatar } from "@/components/agent-avatar"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useGud } from "@/hooks/use-gud"
import { agentColor } from "@/lib/agents"
import { getBlame, getTree } from "@/lib/api"
import { shortHash, shortId } from "@/lib/format"
import type { BlameLine, TreeFile } from "@/lib/types"
import { cn } from "@/lib/utils"

export function FilesView() {
  const { state, setOpenId } = useGud()
  const repo = state?.repo ?? ""
  const links = [...(state?.links ?? [])].sort((a, b) => b.seq - a.seq)
  const tipId = state?.tip?.id ?? ""
  const linkIds = links.map((link) => link.id).join("\n")
  const [at, setAt] = useState(tipId)
  const [files, setFiles] = useState<TreeFile[]>([])
  const [path, setPath] = useState("")
  const [lines, setLines] = useState<BlameLine[]>([])
  const [loadingTree, setLoadingTree] = useState(false)
  const [loadingBlame, setLoadingBlame] = useState(false)

  useEffect(() => {
    if (!tipId) {
      setAt("")
      setFiles([])
      setPath("")
      setLines([])
      return
    }
    if (!at || !linkIds.split("\n").includes(at)) setAt(tipId)
  }, [repo, tipId, at, linkIds])

  useEffect(() => {
    if (!at) return
    let cancelled = false
    setLoadingTree(true)
    void getTree(at)
      .then((tree) => {
        if (cancelled) return
        setFiles(tree.files)
        setPath((current) => (tree.files.some((file) => file.path === current) ? current : tree.files[0]?.path ?? ""))
      })
      .finally(() => {
        if (!cancelled) setLoadingTree(false)
      })
    return () => {
      cancelled = true
    }
  }, [at])

  useEffect(() => {
    if (!at || !path) {
      setLines([])
      return
    }
    let cancelled = false
    setLoadingBlame(true)
    void getBlame(path, at)
      .then((blame) => {
        if (!cancelled) setLines(blame.lines)
      })
      .finally(() => {
        if (!cancelled) setLoadingBlame(false)
      })
    return () => {
      cancelled = true
    }
  }, [at, path])

  const selected = links.find((link) => link.id === at)

  return (
    <div className="flex h-full min-h-0">
      <div className="flex w-72 shrink-0 flex-col border-r border-border">
        <div className="border-b border-border p-2">
          {at ? <Select value={at} onValueChange={(value) => { if (value) setAt(value) }}>
            <SelectTrigger className="w-full" size="sm">
              <SelectValue placeholder="Tree at…">
                {selected ? `#${selected.seq} · ${shortHash(selected.state)}` : "Tree at…"}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {links.map((link) => (
                <SelectItem key={link.id} value={link.id}>
                  #{link.seq} · {shortHash(link.state)} · {link.subject}
                </SelectItem>
              ))}
            </SelectContent>
          </Select> : <Skeleton className="h-7 w-full" />}
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {loadingTree && files.length === 0 ? (
            <div className="space-y-2 p-3">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : (
            files.map((file) => (
              <button
                key={file.path}
                type="button"
                onClick={() => setPath(file.path)}
                className={cn(
                  "block w-full space-y-1 px-3 py-2 text-left hover:bg-muted/40",
                  path === file.path && "bg-muted/50",
                )}
              >
                <span className="flex items-baseline justify-between gap-2">
                  <span className="truncate font-mono text-[12px]">{file.path}</span>
                  <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{file.lines}</span>
                </span>
                <OwnerBar owners={file.owners} />
              </button>
            ))
          )}
        </div>
      </div>
      <div className="min-w-0 flex-1 overflow-auto">
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-background/95 px-3 py-2 backdrop-blur">
          <span className="font-mono text-xs">{path || "Select a file"}</span>
          {selected ? (
            <button
              type="button"
              className="font-mono text-[11px] text-muted-foreground hover:text-foreground"
              onClick={() => setOpenId(selected.id)}
            >
              at #{selected.seq} {shortHash(selected.state)}
            </button>
          ) : null}
        </div>
        {loadingBlame && lines.length === 0 ? (
          <div className="space-y-1 p-3">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-4 w-2/3" />
          </div>
        ) : lines.length === 0 ? (
          <p className="p-4 text-sm text-muted-foreground">This path is not in the tree at this link.</p>
        ) : (
          <div className="font-mono text-[12px] leading-5">
            {lines.map((line) => (
              <BlameRow key={line.n} line={line} onOpenLink={() => setOpenId(line.link_id)} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function OwnerBar({ owners }: { owners: Record<string, number> }) {
  const entries = Object.entries(owners).sort((a, b) => b[1] - a[1])
  const total = entries.reduce((sum, [, count]) => sum + count, 0) || 1
  return (
    <div className="flex h-1.5 overflow-hidden rounded-full bg-muted">
      {entries.map(([owner, count]) => (
        <Tooltip key={owner}>
          <TooltipTrigger asChild>
            <span
              className="h-full"
              style={{ width: `${(count / total) * 100}%`, backgroundColor: agentColor(owner).bg }}
            />
          </TooltipTrigger>
          <TooltipContent>
            {owner} · {count}
          </TooltipContent>
        </Tooltip>
      ))}
    </div>
  )
}

function BlameRow({ line, onOpenLink }: { line: BlameLine; onOpenLink: () => void }) {
  const color = agentColor(line.owner)
  return (
    <div className={cn("flex hover:bg-muted/30", line.orphaned && "opacity-50")}>
      <span className="w-10 shrink-0 select-none pr-2 text-right text-muted-foreground/70">{line.n}</span>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={onOpenLink}
            className="flex w-36 shrink-0 items-center gap-1.5 border-l-2 px-2 text-left"
            style={{ borderLeftColor: color.bg }}
          >
            <AgentAvatar handle={line.owner} className="size-4" />
            <span className={cn("truncate text-[11px]", line.orphaned && "line-through")}>{line.owner}</span>
          </button>
        </TooltipTrigger>
        <TooltipContent>
          <span className={cn(line.orphaned && "line-through")}>{line.owner}</span>
          <span className="ml-2 font-mono">{shortId(line.link_id)}</span>
          {line.orphaned ? <span className="ml-2 text-muted-foreground">orphaned</span> : null}
        </TooltipContent>
      </Tooltip>
      <pre className="min-w-0 flex-1 px-3 whitespace-pre">{line.text || " "}</pre>
    </div>
  )
}
