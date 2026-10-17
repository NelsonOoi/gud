let offsetSec = 0

export function setServerNow(serverNowSec: number) {
  offsetSec = serverNowSec - Date.now() / 1000
}

export function serverNow(): number {
  return Date.now() / 1000 + offsetSec
}

export function shortHash(hash: string, n = 12): string {
  return hash.slice(0, n)
}

export function shortId(id: string): string {
  const inner = id.replace(/^</, "").replace(/>$/, "")
  const slug = inner.split("@")[0]
  return slug || inner
}

export function relativeTime(unix: number, now: number): string {
  const delta = Math.round(now - unix)
  const s = Math.abs(delta)
  if (s < 5) return "just now"
  const label =
    s < 60
      ? `${s}s`
      : s < 3600
        ? `${Math.round(s / 60)}m`
        : s < 86400 * 2
          ? `${Math.round(s / 3600)}h`
          : `${Math.round(s / 86400)}d`
  return delta < 0 ? `in ${label}` : `${label} ago`
}

export function clockTime(unix: number): string {
  return new Date(unix * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

export function countdown(closesAt: number, now: number): string {
  const left = Math.max(0, Math.ceil(closesAt - now))
  const m = Math.floor(left / 60)
  const s = left % 60
  if (m <= 0) return `${s}s`
  return `${m}m ${s.toString().padStart(2, "0")}s`
}

export function presentEmail(body: string): { prose: string; trailers: string } {
  const without = body.replace(/\n?```diff\n[\s\S]*\n```\s*$/, "").trim()
  const idx = without.search(/^Gud-/m)
  if (idx < 0) return { prose: without, trailers: "" }
  return {
    prose: without.slice(0, idx).trim(),
    trailers: without.slice(idx).trim(),
  }
}
