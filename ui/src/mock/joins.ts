import type { JoinRequest } from "@/lib/types"
import { PUSH, sig } from "@/mock/ids"

export const JOIN_CLAUDE = "<join-claude@acme.primitive.email>"
export const JOIN_CODEX = "<join-codex@orbit.primitive.email>"
export const JOIN_STRANGER = "<join-stranger@mail.example>"
export const JOIN_GEMINI = "<join-gemini@lab.primitive.email>"

const KEYS = {
  claude: "ed25519:Y2xhdWRlLWFnZW50LWtleS0wMDE",
  codex: "ed25519:Y29kZXgtYWdlbnQta2V5LTAwMg",
  stranger: "ed25519:bm90LWEtcmVhbC1zaWduYXR1cmU",
  gemini: "ed25519:Z2VtaW5pLWFnZW50LWtleS0wMDM",
} as const

export function joinSummary(join: Pick<JoinRequest, "handle" | "role">): string {
  return `${join.handle} requests to join as ${join.role}`
}

export function joinMailBody(join: JoinRequest): string {
  const pairs: [string, string][] = [
    ["Kind", "join"],
    ["Author", join.handle],
    ["Role", join.role],
    ["Address", join.address],
    ["Key", join.public_key],
  ]
  if (join.sponsor) pairs.push(["Sponsor", join.sponsor])
  if (join.reason) pairs.push(["Reason", join.reason])
  pairs.push(["Sig", join.valid ? sig(join.id) : "ed25519:bm90LWEtcmVhbC1zaWduYXR1cmU"])
  const prose = join.reason || `${join.handle} requests to join.`
  return `${prose}\n\n${pairs.map(([name, value]) => `Gud-${name}: ${value}`).join("\n")}`
}

export function seedJoins(now: number): JoinRequest[] {
  return [
    {
      id: JOIN_CLAUDE,
      handle: "@claude-agent",
      role: "agent",
      address: "claude-agent@acme.primitive.email",
      public_key: KEYS.claude,
      sponsor: "@nelson",
      reason: "Review auth changes from the Acme account.",
      received_at: now - 25,
      valid: true,
      status: "pending",
      push_id: null,
    },
    {
      id: JOIN_CODEX,
      handle: "@codex-agent",
      role: "agent",
      address: "codex-agent@orbit.primitive.email",
      public_key: KEYS.codex,
      sponsor: "@nelson",
      reason: "Take API tasks from an Orbit mailbox.",
      received_at: now - 140,
      valid: true,
      status: "proposed",
      push_id: PUSH.join,
    },
    {
      id: JOIN_STRANGER,
      handle: "@stranger",
      role: "agent",
      address: "stranger@mail.example",
      public_key: KEYS.stranger,
      sponsor: null,
      reason: "Add me to the swarm.",
      received_at: now - 2 * 3600,
      valid: false,
      status: "rejected",
      push_id: null,
    },
  ]
}

export function geminiJoin(now: number): JoinRequest {
  return {
    id: JOIN_GEMINI,
    handle: "@gemini-agent",
    role: "agent",
    address: "gemini-agent@lab.primitive.email",
    public_key: KEYS.gemini,
    sponsor: "@nelson",
    reason: "Replay the host mirror and take API tasks.",
    received_at: now,
    valid: true,
    status: "pending",
    push_id: null,
  }
}
