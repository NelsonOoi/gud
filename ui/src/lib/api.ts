import { extractDiff } from "@/lib/diff"
import { ApiError } from "@/lib/errors"
import type {
  Activity,
  BlameResponse,
  DiffResponse,
  EmailView,
  RepoState,
  ServerEvent,
  Task,
  TreeResponse,
} from "@/lib/types"
import {
  mockActivity,
  mockApprove,
  mockBlame,
  mockCreateTask,
  mockDiff,
  mockMembers,
  mockReply,
  mockState,
  mockTasks,
  mockThread,
  mockTree,
  onMockEvent,
  startMockDriver,
} from "@/mock/store"

export type ApiMode = "mock" | "live"

let apiMode: "unset" | ApiMode = "unset"
let primed: RepoState | null = null

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
  const timer = window.setTimeout(() => controller.abort(), 1500)
  try {
    const response = await fetch(`${baseUrl()}/api/state`, { signal: controller.signal })
    if (!response.ok) throw new ApiError(response.status, response.statusText)
    primed = (await response.json()) as RepoState
    apiMode = "live"
    return "live"
  } catch {
    apiMode = "mock"
    return "mock"
  } finally {
    window.clearTimeout(timer)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (apiMode !== "live") return mockDispatch<T>(path, init)
  const response = await fetch(`${baseUrl()}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { "content-type": "application/json" } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === "string" && body.detail) detail = body.detail
    } catch {
      /* ignore non-json errors */
    }
    throw new ApiError(response.status, detail || `request failed (${response.status})`)
  }
  return (await response.json()) as T
}

function decodePath(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

function mockDispatch<T>(path: string, init?: RequestInit): T {
  const url = new URL(path, "http://gud.local")
  const method = (init?.method ?? "GET").toUpperCase()
  const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {}
  const pathname = url.pathname

  if (method === "GET" && pathname === "/api/state") return mockState() as T
  if (method === "GET" && pathname === "/api/tasks") return mockTasks() as T
  if (method === "GET" && pathname === "/api/activity") {
    return mockActivity(Number(url.searchParams.get("limit") ?? "100")) as T
  }
  if (method === "GET" && pathname === "/api/tree") {
    return mockTree(url.searchParams.get("at")) as T
  }
  if (method === "GET" && pathname === "/api/blame") {
    return mockBlame(url.searchParams.get("path") ?? "", url.searchParams.get("at")) as T
  }

  const diff = /^\/api\/links\/(.+)\/diff$/.exec(pathname)
  if (method === "GET" && diff?.[1]) return mockDiff(decodePath(diff[1])) as T

  const thread = /^\/api\/thread\/(.+)$/.exec(pathname)
  if (method === "GET" && thread?.[1]) return mockThread(decodePath(thread[1])) as T

  if (method === "POST" && pathname === "/api/tasks") {
    return mockCreateTask(body as { title?: string; description?: string; requester?: string }) as T
  }
  if (method === "POST" && pathname === "/api/members") {
    return mockMembers(
      body as {
        action?: string
        handle?: string
        role?: string
        address?: string
        public_key?: string
        sponsor?: string
      },
    ) as T
  }
  if (method === "POST" && pathname === "/api/approve") {
    return mockApprove(body as { target?: string; as?: string }) as T
  }
  if (method === "POST" && pathname === "/api/reply") {
    return mockReply(body as { target?: string; from?: string; text?: string }) as T
  }
  throw new ApiError(404, `no mock for ${method} ${pathname}`)
}

export async function getState(): Promise<RepoState> {
  if (primed) {
    const snapshot = primed
    primed = null
    return snapshot
  }
  return request<RepoState>("/api/state")
}

export async function getTasks(): Promise<Task[]> {
  return request<Task[]>("/api/tasks")
}

export async function getActivity(limit = 200): Promise<Activity[]> {
  return request<Activity[]>(`/api/activity?limit=${limit}`)
}

export async function getTree(at: string): Promise<TreeResponse> {
  return request<TreeResponse>(`/api/tree?at=${encodeURIComponent(at)}`)
}

export async function getBlame(path: string, at: string): Promise<BlameResponse> {
  const query = new URLSearchParams({ path, at })
  return request<BlameResponse>(`/api/blame?${query.toString()}`)
}

export async function getThread(id: string): Promise<EmailView[]> {
  return request<EmailView[]>(`/api/thread/${encodeURIComponent(id)}`)
}

export async function getDiff(id: string): Promise<string> {
  try {
    const response = await request<DiffResponse>(`/api/links/${encodeURIComponent(id)}/diff`)
    return response.diff
  } catch (error) {
    try {
      const thread = await getThread(id)
      const push = thread.find((email) => email.kind === "push") ?? thread[0]
      const diff = extractDiff(push?.body ?? "")
      if (diff) return diff
    } catch {
      /* keep the original error */
    }
    throw error
  }
}

export async function createTask(body: {
  title: string
  description: string
  requester: string
}): Promise<{ id: string }> {
  return request("/api/tasks", { method: "POST", body: JSON.stringify(body) })
}

export async function postMember(body: {
  action: "add" | "remove"
  handle: string
  role?: string
  address?: string
  public_key?: string
  sponsor?: string
}): Promise<{ push_id: string }> {
  return request("/api/members", { method: "POST", body: JSON.stringify(body) })
}

export async function postApprove(body: { target: string; as: string }): Promise<{ id: string }> {
  return request("/api/approve", { method: "POST", body: JSON.stringify(body) })
}

export async function postReply(body: {
  target: string
  from: string
  text: string
}): Promise<{ id: string }> {
  return request("/api/reply", { method: "POST", body: JSON.stringify(body) })
}

export function subscribe(onEvent: (event: ServerEvent) => void): () => void {
  if (apiMode !== "live") {
    const unsubscribe = onMockEvent((event) => {
      if (
        event.event === "mail" ||
        event.event === "chain" ||
        event.event === "proposal" ||
        event.event === "task"
      ) {
        onEvent(event as ServerEvent)
      }
    })
    const stop = startMockDriver()
    return () => {
      unsubscribe()
      stop()
    }
  }

  let stopped = false
  let source: EventSource | null = null
  let delay = 1000
  const types = ["mail", "chain", "proposal", "task"] as const

  const connect = () => {
    if (stopped) return
    source = new EventSource(`${baseUrl()}/api/events`)
    for (const type of types) {
      source.addEventListener(type, (event) => {
        delay = 1000
        const message = event as MessageEvent<string>
        let data: unknown = null
        try {
          data = JSON.parse(message.data)
        } catch {
          data = null
        }
        onEvent({ event: type, data } as ServerEvent)
      })
    }
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
