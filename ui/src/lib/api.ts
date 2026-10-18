import { extractDiff } from "@/lib/diff"
import { ApiError } from "@/lib/errors"
import type {
  Activity,
  BlameResponse,
  BugReport,
  DiffResponse,
  EmailView,
  JoinRequest,
  RawEmail,
  RepoState,
  ServerEvent,
  Task,
  TreeResponse,
} from "@/lib/types"
import {
  mockActivity,
  mockApprove,
  mockBlame,
  mockBugs,
  mockCreateTask,
  mockDiff,
  mockJoins,
  mockMail,
  mockMembers,
  mockProposeJoin,
  mockReply,
  mockReviewBug,
  mockState,
  mockTasks,
  mockThread,
  mockTree,
  onMockEvent,
  startMockDriver,
} from "@/mock/store"

export type ApiMode = "mock" | "live"

export type RepoCatalog = {
  active: string | null
  repos: string[]
  needs_token: boolean
}

let apiMode: "unset" | ApiMode = "unset"
let primed: RepoState | null = null
let primedRepos: RepoCatalog | null = null

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
  // No server falls back to mock right away. A slow one (real mail can take seconds to list)
  // is alive, so keep waiting on it instead of showing fake data.
  for (let attempt = 0; attempt < 4; attempt++) {
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 8000)
    try {
      const response = await fetch(`${baseUrl()}/api/repos`, { signal: controller.signal })
      if (!response.ok) throw new ApiError(response.status, response.statusText)
      primedRepos = (await response.json()) as RepoCatalog
      apiMode = "live"
      if (!primedRepos.needs_token) {
        try {
          const stateResponse = await fetch(`${baseUrl()}/api/state`, { signal: controller.signal })
          if (stateResponse.ok) primed = (await stateResponse.json()) as RepoState
        } catch {
          /* the page refetches state after the catalog */
        }
      }
      return "live"
    } catch {
      if (!controller.signal.aborted) break
    } finally {
      window.clearTimeout(timer)
    }
  }
  apiMode = "mock"
  return "mock"
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
  if (method === "GET" && pathname === "/api/bugs") return mockBugs() as T
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

  if (method === "GET" && pathname === "/api/joins") return mockJoins() as T
  if (method === "GET" && pathname === "/api/mail") return mockMail() as T

  const joinPropose = /^\/api\/joins\/(.+)\/propose$/.exec(pathname)
  if (method === "POST" && joinPropose?.[1]) return mockProposeJoin(decodePath(joinPropose[1])) as T

  const joinApprove = /^\/api\/joins\/(.+)\/approve$/.exec(pathname)
  if (method === "POST" && joinApprove?.[1]) return mockProposeJoin(decodePath(joinApprove[1])) as T

  const bugReview = /^\/api\/bugs\/(.+)\/review$/.exec(pathname)
  if (method === "POST" && bugReview?.[1]) {
    return mockReviewBug(decodePath(bugReview[1]), body as { action?: string; from?: string; reason?: string }) as T
  }

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
  if (method === "POST" && pathname === "/api/repos") {
    return { active: "repo@gud.local", repos: ["repo@gud.local"], needs_token: false } as T
  }
  if (method === "GET" && pathname === "/api/swarm") return { enabled: false } as T
  if (method === "POST" && pathname === "/api/swarm") return { enabled: Boolean(body.enabled) } as T
  if (method === "POST" && pathname === "/api/agents") {
    return {
      handle: "@agent",
      address: "agent@gud.local",
      public_key: "ed25519:bW9jaw==",
      private_key: "bW9ja2tleQ==",
    } as T
  }
  throw new ApiError(404, `no mock for ${method} ${pathname}`)
}

export async function getRepos(): Promise<RepoCatalog> {
  if (primedRepos) {
    const snapshot = primedRepos
    primedRepos = null
    return snapshot
  }
  return request<RepoCatalog>("/api/repos")
}

export async function selectRepo(address: string): Promise<RepoCatalog> {
  return request<RepoCatalog>("/api/repo", { method: "POST", body: JSON.stringify({ address }) })
}

export async function signInMailbox(token: string): Promise<RepoCatalog> {
  return request<RepoCatalog>("/api/mailbox", { method: "POST", body: JSON.stringify({ token }) })
}

export async function createRepo(body: { name: string; humans: string[] }): Promise<RepoCatalog> {
  return request<RepoCatalog>("/api/repos", { method: "POST", body: JSON.stringify(body) })
}

export async function getSwarm(): Promise<{ enabled: boolean }> {
  return request("/api/swarm")
}

export async function setSwarm(enabled: boolean): Promise<{ enabled: boolean }> {
  return request("/api/swarm", { method: "POST", body: JSON.stringify({ enabled }) })
}

export type ProvisionedAgent = {
  handle: string
  address: string
  public_key: string
  private_key: string
}

export async function provisionAgent(body: { handle: string; role?: string }): Promise<ProvisionedAgent> {
  return request("/api/agents", { method: "POST", body: JSON.stringify(body) })
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

export async function getBugs(): Promise<BugReport[]> {
  return request<BugReport[]>("/api/bugs")
}

export async function reviewBug(
  id: string,
  body: { action: "approve" | "reject"; from?: string; reason?: string },
): Promise<{ id: string; task_id?: string | null }> {
  return request(`/api/bugs/${encodeURIComponent(id)}/review`, {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function getActivity(limit = 200): Promise<Activity[]> {
  return request<Activity[]>(`/api/activity?limit=${limit}`)
}

export async function getMail(): Promise<RawEmail[]> {
  const emails: RawEmail[] = []
  let since: number | undefined
  for (let page = 0; page < 40; page += 1) {
    const params = new URLSearchParams({ limit: "500" })
    if (since != null) params.set("since", String(since))
    const body = await request<{ emails: RawEmail[]; next_since: number | null }>(`/api/mail?${params}`)
    emails.push(...body.emails)
    if (body.next_since == null || body.emails.length === 0 || body.next_since === since) break
    since = body.next_since
  }
  return emails
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

export async function getJoins(): Promise<JoinRequest[]> {
  try {
    return await request<JoinRequest[]>("/api/joins")
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.status === 403)) return []
    throw error
  }
}

export async function proposeJoin(id: string): Promise<{ push_id: string }> {
  return request(`/api/joins/${encodeURIComponent(id)}/propose`, { method: "POST" })
}

export async function approveJoin(id: string): Promise<{ push_id: string }> {
  return request(`/api/joins/${encodeURIComponent(id)}/approve`, { method: "POST" })
}

export function subscribe(onEvent: (event: ServerEvent) => void): () => void {
  if (apiMode !== "live") {
    const unsubscribe = onMockEvent((event) => {
      if (
        event.event === "mail" ||
        event.event === "chain" ||
        event.event === "proposal" ||
        event.event === "task" ||
        event.event === "join" ||
        event.event === "bug"
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
  const types = ["mail", "chain", "proposal", "task", "join", "bug"] as const

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
