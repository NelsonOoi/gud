// Time is integer unix milliseconds everywhere (protocol time). `serverNow()` is the host clock,
// estimated from the offset of the last `state.now` the host sent.

let offsetMs = 0

export function setServerNow(serverNowMs: number) {
  offsetMs = serverNowMs - Date.now()
}

export function serverNow(): number {
  return Date.now() + offsetMs
}

export function shortHash(hash: string | null | undefined, n = 12): string {
  return (hash ?? "").slice(0, n)
}

export function shortKey(key: string | null | undefined): string {
  return (key ?? "").replace(/^ed25519:/, "").slice(0, 12)
}

function span(ms: number): string {
  const s = Math.round(Math.abs(ms) / 1000)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  if (s < 86400 * 2) return `${Math.round(s / 3600)}h`
  return `${Math.round(s / 86400)}d`
}

export function relativeTime(at: number, now: number): string {
  const delta = now - at
  if (Math.abs(delta) < 5000) return "just now"
  return delta < 0 ? `in ${span(delta)}` : `${span(delta)} ago`
}

export function clockTime(at: number): string {
  return new Date(at).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })
}

/** "1m 05s" until `at`, or "0s" once it has passed. */
export function countdown(at: number, now: number): string {
  const left = Math.max(0, Math.ceil((at - now) / 1000))
  const h = Math.floor(left / 3600)
  const m = Math.floor((left % 3600) / 60)
  const s = left % 60
  if (h > 0) return `${h}h ${m.toString().padStart(2, "0")}m`
  if (m > 0) return `${m}m ${s.toString().padStart(2, "0")}s`
  return `${s}s`
}

export function duration(ms: number | null | undefined): string {
  if (ms == null) return "—"
  if (ms % 86_400_000 === 0 && ms >= 86_400_000) return `${ms / 86_400_000}d`
  if (ms % 3_600_000 === 0 && ms >= 3_600_000) return `${ms / 3_600_000}h`
  if (ms % 60_000 === 0 && ms >= 60_000) return `${ms / 60_000}m`
  if (ms % 1000 === 0) return `${ms / 1000}s`
  return `${ms}ms`
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`
}
