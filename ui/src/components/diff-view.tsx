import type { ReactNode } from "react"
import { classifyDiffLine } from "@/lib/diff"
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
    return <p className="px-4 py-6 text-sm text-muted-foreground">No diff on this message.</p>
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
