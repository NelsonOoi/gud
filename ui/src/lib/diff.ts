import type { Link } from "@/lib/types"

export function diffstat(diff: string): Link["diffstat"] {
  const files: string[] = []
  let added = 0
  let removed = 0
  for (const line of diff.split("\n")) {
    const file = /^diff --git a\/(.+) b\//.exec(line)
    if (file?.[1]) {
      files.push(file[1])
      continue
    }
    if (line.startsWith("+++") || line.startsWith("---")) continue
    if (line.startsWith("+")) added += 1
    else if (line.startsWith("-")) removed += 1
  }
  return { files, added, removed }
}

export function extractDiff(body: string): string {
  const fenced = /```diff\n([\s\S]*?)\n```/.exec(body)
  return fenced?.[1] ?? ""
}

export type DiffLineKind = "add" | "del" | "hunk" | "meta" | "ctx"

export function classifyDiffLine(line: string): DiffLineKind {
  if (
    line.startsWith("diff --git") ||
    line.startsWith("index ") ||
    line.startsWith("---") ||
    line.startsWith("+++")
  ) {
    return "meta"
  }
  if (line.startsWith("@@")) return "hunk"
  if (line.startsWith("+")) return "add"
  if (line.startsWith("-")) return "del"
  return "ctx"
}
