// Client of the v1 host's UI view API (`/api/v1/view/*`, served by `gud host serve`). Offline (no
// host, or `?mock=1`) every call is answered by the in-memory v1 story in `@/mock/store`.

import { ApiError } from "@/lib/errors"
import type {
  ActResult,
  BlameResponse,
  DiffResponse,
  EventsResponse,
  FileResponse,
  GudEvent,
  HostView,
  MembersResponse,
  NeedsResponse,
  ProposalsResponse,
  TasksResponse,
  ThreadResponse,
  TreeResponse,
  ViewState,
  Escalation,
  Link,
} from "@/lib/types"
import { mockRequest, onMockEvent, startMockDriver } from "@/mock/store"

export type ApiMode = "mock" | "live"

/** §19.2 event types, plus the stream's own `head` frame (the host log grew). */
export const EVENT_TYPES = [
  "push.received", "review.requested", "objected", "commented", "approved", "held", "test.passed", "test.failed",
  "window.closing", "superseded", "push.rejected", "carried", "linked", "task.opened", "subtask.posted",
  "claim.won", "claim.lost", "claim.lapsed", "claim.overlap", "scope.exceeded", "task.ready", "task.done",
  "escalated", "escalation.resolved", "tip.red", "tip.bisected", "tip.green", "fix.opened", "host.fork",
] as const

let apiMode: "unset" | ApiMode = "unset"
let primed: ViewState | null = null

export function currentMode(): ApiMode | "unset" {
  return apiMode
}

function baseUrl(): string {
  return import.meta.env.VITE_GUD_API ?? ""
}

export async function bootApi(): Promise<ApiMode> {
  if (apiMode !== "unset") return apiMode
  const params = new URLSearchParams(window.location.search)
  if (params.get("mock") === "1") {
    apiMode = "mock"
    return "mock"
  }
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 8000)
  try {
    const response = await fetch(`${baseUrl()}/api/v1/view/state`, { signal: controller.signal })
    if (response.status === 401 || response.status === 403) {
      // a real host that wants a signed member request (§21): stay live and show its error
      apiMode = "live"
      return apiMode
    }
    if (!response.ok) throw new ApiError(response.status, response.statusText)
    primed = (await response.json()) as ViewState
    apiMode = "live"
  } catch {
    apiMode = "mock"
  } finally {
    window.clearTimeout(timer)
  }
  return apiMode
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (apiMode !== "live") return mockRequest<T>(path, init)
  const response = await fetch(`${baseUrl()}${path}`, {
    ...init,
    headers: { ...(init?.body ? { "content-type": "application/json" } : {}), ...init?.headers },
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === "string" && body.detail) detail = body.detail
    } catch {
      /* not json */
    }
    throw new ApiError(response.status, detail || `request failed (${response.status})`)
  }
  return (await response.json()) as T
}

const enc = encodeURIComponent

export async function getState(): Promise<ViewState> {
  if (primed) {
    const snapshot = primed
    primed = null
    return snapshot
  }
  return request<ViewState>("/api/v1/view/state")
}

export const getChain = (limit = 300) =>
  request<{ links: Link[]; total: number; next_before: number | null }>(`/api/v1/view/chain?limit=${limit}`)
export const getProposals = (dead = 30) => request<ProposalsResponse>(`/api/v1/view/proposals?dead=${dead}`)
export const getTasks = () => request<TasksResponse>("/api/v1/view/tasks")
export const getEscalations = () => request<{ escalations: Escalation[] }>("/api/v1/view/escalations")
export const getMembers = () => request<MembersResponse>("/api/v1/view/members")
export const getHost = (limit = 60) => request<HostView>(`/api/v1/view/host?limit=${limit}`)
export const getEvents = (after = 0, limit = 1000) =>
  request<EventsResponse>(`/api/v1/view/events?after=${after}&limit=${limit}`)
export const getNeeds = (member?: string) =>
  request<NeedsResponse>(`/api/v1/view/needs${member ? `?member=${enc(member)}` : ""}`)
export const getTree = (at?: string) => request<TreeResponse>(`/api/v1/view/tree${at ? `?at=${enc(at)}` : ""}`)
export const getFile = (path: string, at?: string) =>
  request<FileResponse>(`/api/v1/view/file?path=${enc(path)}${at ? `&at=${enc(at)}` : ""}`)
export const getBlame = (path: string, at?: string) =>
  request<BlameResponse>(`/api/v1/view/blame?path=${enc(path)}${at ? `&at=${enc(at)}` : ""}`)
export const getDiff = (id: string) => request<DiffResponse>(`/api/v1/view/diff/${enc(id)}`)
export const getThread = (id: string) => request<ThreadResponse>(`/api/v1/view/thread/${enc(id)}`)

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) })
}

export const openTask = (body: { title: string; description?: string; requester?: string; accept?: string[];
  accept_cmds?: string[]; bug?: string; as?: string }) => post<ActResult>("/api/v1/view/act/task", body)
export const vote = (body: { kind: "approve" | "object" | "veto" | "hold"; target: string; text?: string; as?: string }) =>
  post<ActResult>("/api/v1/view/act/vote", body)
export const comment = (body: { target: string; text: string; task?: string; verdict?: "reject"; as?: string }) =>
  post<ActResult>("/api/v1/view/act/comment", body)
export const admit = (body: { join: string; text?: string; as?: string }) =>
  post<ActResult & { member: { handle: string; role: string } }>("/api/v1/view/act/admit", body)

export type StreamMessage = { type: "event"; event: GudEvent } | { type: "head"; seq: number; time: number } |
  { type: "open" }

/** The all-events stream (SSE). Resumes from the last id it saw after a reconnect. */
export function subscribe(onMessage: (message: StreamMessage) => void): () => void {
  if (apiMode !== "live") {
    const off = onMockEvent((event) => onMessage(event))
    const stop = startMockDriver()
    return () => {
      off()
      stop()
    }
  }
  let stopped = false
  let source: EventSource | null = null
  let delay = 1000
  let last = 0

  const connect = () => {
    if (stopped) return
    source = new EventSource(`${baseUrl()}/api/v1/view/stream${last ? `?after=${last}` : ""}`)
    source.onopen = () => {
      delay = 1000
      onMessage({ type: "open" })
    }
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (raw) => {
        const message = raw as MessageEvent<string>
        try {
          const body = JSON.parse(message.data) as Omit<GudEvent, "n">
          const n = Number(message.lastEventId) || 0
          last = Math.max(last, n)
          onMessage({ type: "event", event: { ...body, n } })
        } catch {
          /* ignore a malformed frame */
        }
      })
    }
    source.addEventListener("head", (raw) => {
      try {
        const body = JSON.parse((raw as MessageEvent<string>).data) as { seq: number; time: number }
        onMessage({ type: "head", seq: body.seq, time: body.time })
      } catch {
        /* ignore */
      }
    })
    source.onerror = () => {
      source?.close()
      if (stopped) return
      window.setTimeout(connect, delay)
      delay = Math.min(delay * 2, 15_000)
    }
  }

  connect()
  return () => {
    stopped = true
    source?.close()
  }
}
