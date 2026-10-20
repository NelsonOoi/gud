import type { ReactNode } from "react"
import { Badge } from "@/components/ui/badge"
import { classifyDiffLine } from "@/lib/diff"
import type { DiffResponse, SignedOp } from "@/lib/types"
import { cn } from "@/lib/utils"

const KEYWORDS = new Set([
  "def",
  "class",
  "return",
  "if",
  "elif",
  "else",
  "raise",
  "import",
  "from",
  "not",
  "and",
  "or",
  "in",
  "is",
  "None",
  "True",
  "False",
  "async",
  "await",
  "with",
  "as",
  "try",
  "except",
  "pass",
  "for",
  "while",
  "lambda",
])

function highlight(code: string): ReactNode[] {
  const nodes: ReactNode[] = []
  const pattern =
    /(#.*$)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|\b(\d+)\b|\b([A-Za-z_][A-Za-z0-9_]*)\b/g
  let last = 0
  let key = 0
  for (const match of code.matchAll(pattern)) {
    const index = match.index ?? 0
    if (index > last) nodes.push(code.slice(last, index))
    const full = match[0]
    const word = match[4]
    if (match[1]) nodes.push(<span key={key} className="text-zinc-500">{full}</span>)
    else if (match[2]) nodes.push(<span key={key} className="text-amber-200">{full}</span>)
    else if (match[3]) nodes.push(<span key={key} className="text-sky-300">{full}</span>)
    else if (word && KEYWORDS.has(word)) nodes.push(<span key={key} className="text-violet-300">{full}</span>)
    else nodes.push(full)
    key += 1
    last = index + full.length
  }
  if (last < code.length) nodes.push(code.slice(last))
  return nodes
}

const ROW: Record<string, string> = {
  add: "bg-emerald-500/10 text-emerald-100",
  del: "bg-red-500/10 text-red-100",
  hunk: "bg-sky-500/10 text-sky-200",
  meta: "text-muted-foreground",
  ctx: "text-foreground/90",
}

export function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return <p className="px-4 py-6 text-sm text-muted-foreground">No change to show.</p>
  }
  const lines = diff.replace(/\n$/, "").split("\n")
  return (
    <div className="overflow-x-auto font-mono text-[12px] leading-5">
      {lines.map((line, index) => {
        const kind = classifyDiffLine(line)
        const prefix = kind === "add" || kind === "del" ? line.slice(0, 1) : ""
        const code = kind === "add" || kind === "del" ? line.slice(1) : line
        return (
          <div key={index} className={cn("flex whitespace-pre px-3", ROW[kind])}>
            <span className="w-4 shrink-0 select-none opacity-70">{prefix}</span>
            <span>{kind === "add" || kind === "del" || kind === "ctx" ? highlight(code) : code}</span>
          </div>
        )
      })}
    </div>
  )
}

const OP_TONE: Record<string, string> = {
  add: "border-emerald-400/40 text-emerald-200",
  modify: "border-sky-400/40 text-sky-200",
  delete: "border-red-400/40 text-red-200",
  rename: "border-violet-400/40 text-violet-200",
  mode: "border-amber-400/40 text-amber-200",
}

function signedOp(op: SignedOp): string {
  switch (op.op) {
    case "rename":
      return `rename ${op.src} → ${op.dst}`
    case "put":
      return `put ${op.path} ${op.mode} ${op.size} B${op.inline ? "" : " (blob)"}`
    case "mode":
      return `mode ${op.path} ${op.mode}`
    default:
      return `${op.op} ${op.path}`
  }
}

/** A link's or proposal's change as render ops: one block per path, binaries and symlinks as a
 * placeholder line, plus the signed operations the push carried (§5.3). */
export function ChangeView({ diff }: { diff: DiffResponse | null }) {
  if (!diff) return <p className="px-4 py-6 text-sm text-muted-foreground">No change to show.</p>
  return (
    <div className="space-y-3 py-3">
      {!diff.available ? (
        <p className="px-4 text-sm text-muted-foreground">Tree not available yet: {diff.why}.</p>
      ) : null}
      {diff.files.map((file) => (
        <section key={`${file.op}:${file.path}`} className="overflow-hidden border-y border-border/70">
          <header className="flex flex-wrap items-center gap-2 bg-muted/30 px-3 py-1.5 font-mono text-[12px]">
            <Badge variant="outline" className={cn("font-mono text-[10px] uppercase", OP_TONE[file.op])}>
              {file.op}
            </Badge>
            <span className="truncate">{file.src ? `${file.src} → ${file.path}` : file.path}</span>
            {file.old_mode && file.new_mode && file.old_mode !== file.new_mode ? (
              <span className="text-amber-200/90">
                {file.old_mode} → {file.new_mode}
              </span>
            ) : null}
            {file.added || file.removed ? (
              <span className="ml-auto">
                <span className="text-emerald-300/90">+{file.added}</span>{" "}
                <span className="text-red-300/90">−{file.removed}</span>
              </span>
            ) : null}
          </header>
          {file.placeholder ? (
            <p className="px-3 py-2 font-mono text-[12px] text-muted-foreground italic">{file.placeholder}</p>
          ) : file.hunks ? (
            <DiffView diff={file.hunks} />
          ) : null}
        </section>
      ))}
      {diff.ops.length > 0 ? (
        <details className="px-4 text-[11px] text-muted-foreground">
          <summary className="cursor-pointer">Signed operations ({diff.ops.length})</summary>
          <ul className="mt-1 space-y-0.5 font-mono">
            {diff.ops.map((op, i) => (
              <li key={i}>{signedOp(op)}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  )
}
