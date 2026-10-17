export const REPO = "repo@gud.primitive.email"
export const WINDOW_SECONDS = 90

export function mid(slug: string): string {
  return `<${slug}@gud.primitive.email>`
}

export function linkId(seq: number): string {
  return mid(`link-${String(seq).padStart(3, "0")}`)
}

export const PUSH = {
  window: mid("push-window"),
  approval: mid("push-approval"),
  quorum: mid("push-quorum"),
  testing: mid("push-testing"),
  qualified: mid("push-qualified"),
  objected: mid("push-objected"),
  vetoed: mid("push-vetoed"),
  failed: mid("push-failed"),
  superseded: mid("push-superseded"),
  stale: mid("push-stale"),
  conflict: mid("push-conflict"),
  badsig: mid("push-badsig"),
} as const

export const ADDR: Record<string, string> = {
  "@lead": "lead@gud.primitive.email",
  "@auth-agent": "auth-agent@gud.primitive.email",
  "@billing-agent": "billing-agent@gud.primitive.email",
  "@api-agent": "api-agent@gud.primitive.email",
  "@sub-1": "sub-1@gud.primitive.email",
  "@sub-2": "sub-2@gud.primitive.email",
  "@tester": "tester@gud.primitive.email",
  "@console": "console@gud.primitive.email",
  "@nelson": "nelson@example.com",
}

export function fakeHash(seed: string): string {
  let a = 2166136261
  let b = 2166136261 ^ 0x9e3779b9
  for (let i = 0; i < seed.length; i++) {
    const c = seed.charCodeAt(i)
    a = Math.imul(a ^ c, 16777619)
    b = Math.imul(b ^ (c << 1), 2246822519)
  }
  let out = ""
  for (let i = 0; i < 8; i++) {
    a = Math.imul(a ^ (a >>> 16), 2246822507) >>> 0
    b = Math.imul(b ^ (b >>> 13), 3266489909) >>> 0
    const mixed = (a ^ b ^ (Math.imul(i + 1, 0x9e3779b9) >>> 0)) >>> 0
    out += mixed.toString(16).padStart(8, "0")
  }
  return out.slice(0, 64)
}

export function sig(seed: string): string {
  return `ed25519:${fakeHash(seed).slice(0, 44)}`
}

export function addressOf(handle: string): string {
  return ADDR[handle] ?? `${handle.replace(/^@/, "")}@gud.primitive.email`
}

export function agentCc(except?: string): string[] {
  return Object.entries(ADDR)
    .filter(([handle, addr]) => handle !== "@nelson" && handle !== except && !addr.endsWith("@example.com"))
    .map(([, addr]) => addr)
}
