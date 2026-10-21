import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { toast } from "sonner"
import {
  bootApi,
  getChain,
  getEscalations,
  getEvents,
  getHost,
  getMembers,
  getNeeds,
  getProposals,
  getState,
  getTasks,
  subscribe,
  type ApiMode,
} from "@/lib/api"
import { serverNow, setServerNow, shortHash } from "@/lib/format"
import type {
  Escalation,
  GudEvent,
  HostView,
  Link,
  MembersResponse,
  NeedsResponse,
  Proposal,
  TasksResponse,
  ViewState,
} from "@/lib/types"

type GudContextValue = {
  mode: ApiMode
  loading: boolean
  connected: boolean
  state: ViewState | null
  links: Link[]
  proposals: Proposal[]
  tasks: TasksResponse
  escalations: Escalation[]
  members: MembersResponse
  host: HostView | null
  needs: NeedsResponse
  events: GudEvent[]
  now: number
  refresh: () => Promise<void>
  openId: string | null
  setOpenId: (id: string | null) => void
}

const EMPTY_TASKS: TasksResponse = { tasks: [], claims: [], overlaps: [], bugs: [] }
const EMPTY_MEMBERS: MembersResponse = { members: [], removed: [], joins: [] }
const MAX_EVENTS = 2000

const GudContext = createContext<GudContextValue | null>(null)

function toastFor(event: GudEvent) {
  const d = event.data
  const s = (key: string) => (typeof d[key] === "string" ? (d[key] as string) : "")
  switch (event.type) {
    case "linked":
      toast.success(`Linked #${String(d.seq ?? "?")}`, {
        description: `${s("author")} · ${shortHash(s("link"))}${d.carried_from ? " · carried" : ""}`,
      })
      break
    case "objected":
      toast.error(`Objection by ${s("by")}`, { description: s("reason") })
      break
    case "escalated":
      toast.warning("Escalated", { description: `${s("reason")} · deciders ${(d.deciders as string[] | undefined)?.join(", ") ?? ""}` })
      break
    case "tip.red":
      toast.error("Tip is red", { description: `red link ${shortHash(s("red_link"))}` })
      break
    case "tip.green":
      toast.success("Tip is green again")
      break
    case "task.done":
      toast.success("Task done", { description: s("task").slice(0, 12) })
      break
    case "host.fork":
      toast.error("Host fork suspected", { description: s("message").slice(0, 12) })
      break
    default:
      break
  }
}

export function GudProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ApiMode>("mock")
  const [loading, setLoading] = useState(true)
  const [connected, setConnected] = useState(false)
  const [state, setState] = useState<ViewState | null>(null)
  const [links, setLinks] = useState<Link[]>([])
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [tasks, setTasks] = useState<TasksResponse>(EMPTY_TASKS)
  const [escalations, setEscalations] = useState<Escalation[]>([])
  const [members, setMembers] = useState<MembersResponse>(EMPTY_MEMBERS)
  const [host, setHost] = useState<HostView | null>(null)
  const [needs, setNeeds] = useState<NeedsResponse>({ member: null, items: [] })
  const [events, setEvents] = useState<GudEvent[]>([])
  const [now, setNow] = useState(() => Date.now())
  const [openId, setOpenId] = useState<string | null>(null)
  const refreshRef = useRef<() => Promise<void>>(async () => {})
  const seen = useRef(new Set<string>())

  const addEvents = useCallback((items: GudEvent[]) => {
    const fresh = items.filter((e) => !seen.current.has(e.id))
    if (fresh.length === 0) return false
    for (const e of fresh) seen.current.add(e.id)
    setEvents((current) => [...current, ...fresh].slice(-MAX_EVENTS))
    return true
  }, [])

  const refresh = useCallback(async () => {
    const [nextState, chain, props, nextTasks, escs, nextMembers, nextHost, nextNeeds] = await Promise.all([
      getState(),
      getChain(),
      getProposals(),
      getTasks(),
      getEscalations(),
      getMembers(),
      getHost(),
      getNeeds(),
    ])
    setServerNow(nextState.now)
    setState(nextState)
    setLinks(chain.links)
    setProposals(props.proposals)
    setTasks(nextTasks)
    setEscalations(escs.escalations)
    setMembers(nextMembers)
    setHost(nextHost)
    setNeeds(nextNeeds)
    setNow(serverNow())
  }, [])

  useEffect(() => {
    refreshRef.current = refresh
  }, [refresh])

  useEffect(() => {
    const clock = window.setInterval(() => setNow(serverNow()), 500)
    return () => window.clearInterval(clock)
  }, [])

  useEffect(() => {
    let unsubscribe = () => {}
    let cancelled = false
    let timer = 0
    let pending = false
    const schedule = (delay: number) => {
      if (pending) return
      pending = true
      timer = window.setTimeout(() => {
        pending = false
        void refreshRef.current().catch(() => {})
      }, delay)
    }
    void (async () => {
      const nextMode = await bootApi()
      if (cancelled) return
      setMode(nextMode)
      try {
        await refreshRef.current()
        const backlog = await getEvents(0, 5000)
        if (!cancelled) addEvents(backlog.events)
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Could not load gud")
      } finally {
        if (!cancelled) setLoading(false)
      }
      unsubscribe = subscribe((message) => {
        if (message.type === "open") {
          setConnected(true)
          schedule(100)
          return
        }
        if (message.type === "head") {
          schedule(1500)
          return
        }
        if (addEvents([message.event])) toastFor(message.event)
        schedule(300)
      })
    })()
    return () => {
      cancelled = true
      unsubscribe()
      window.clearTimeout(timer)
    }
  }, [addEvents])

  const value = useMemo(
    () => ({
      mode, loading, connected, state, links, proposals, tasks, escalations, members, host, needs, events, now,
      refresh, openId, setOpenId,
    }),
    [mode, loading, connected, state, links, proposals, tasks, escalations, members, host, needs, events, now,
      refresh, openId],
  )

  return <GudContext.Provider value={value}>{children}</GudContext.Provider>
}

export function useGud(): GudContextValue {
  const value = useContext(GudContext)
  if (!value) throw new Error("useGud must be used inside GudProvider")
  return value
}

/** The member the UI acts as for votes (the configured human), and for tasks (human, else console). */
export function useActor(): { human: string | null; taskAs: string | null; enabled: boolean } {
  const { state } = useGud()
  const human = state?.ui.human ?? null
  return { human, taskAs: human ?? state?.ui.console ?? null, enabled: Boolean(state?.ui.actions) }
}
