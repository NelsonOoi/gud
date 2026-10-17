const KNOWN: Record<string, string> = {
  "@lead": "#e8b931",
  "@auth-agent": "#5b8def",
  "@billing-agent": "#3dbe7a",
  "@api-agent": "#b07cff",
  "@sub-1": "#3ec6e0",
  "@sub-2": "#ff7a90",
  "@sub-3": "#ff9f6b",
  "@sub-4": "#d6e35a",
  "@tester": "#2fbfb0",
  "@console": "#a1a1aa",
  "@nelson": "#f27bd4",
  "@claude-agent": "#d97757",
  "@codex-agent": "#10a37f",
  "@gemini-agent": "#4b8bf5",
  "@stranger": "#71717a",
}

const FALLBACK = [
  "#fb7185",
  "#38bdf8",
  "#a3e635",
  "#fbbf24",
  "#c084fc",
  "#34d399",
  "#f472b6",
  "#60a5fa",
  "#facc15",
  "#22d3ee",
]

export type AgentColor = { bg: string; fg: string }

function foreground(hex: string): string {
  const n = Number.parseInt(hex.slice(1), 16)
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  const y = 0.299 * r + 0.587 * g + 0.114 * b
  return y > 165 ? "#1a1a1a" : "#ffffff"
}

export function agentColor(handle: string): AgentColor {
  const known = KNOWN[handle]
  if (known) return { bg: known, fg: foreground(known) }
  let h = 0
  for (let i = 0; i < handle.length; i++) h = (h * 33 + handle.charCodeAt(i)) >>> 0
  const bg = FALLBACK[h % FALLBACK.length] ?? "#a1a1aa"
  return { bg, fg: foreground(bg) }
}

export function initials(handle: string): string {
  const raw = handle.replace(/^@/, "")
  const parts = raw.split(/[^A-Za-z0-9]+/).filter(Boolean)
  if (parts.length >= 2) {
    const a = parts[0]?.[0] ?? ""
    const b = parts[1]?.[0] ?? ""
    return (a + b).toUpperCase()
  }
  const numbered = /^([A-Za-z]+)-?(\d+)$/.exec(raw)
  if (numbered?.[1] && numbered[2]) return (numbered[1][0] + numbered[2]).toUpperCase()
  return raw.slice(0, 2).toUpperCase()
}
