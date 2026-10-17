import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { toast } from "sonner"
import { bootApi, getActivity, getState, getTasks, subscribe, type ApiMode } from "@/lib/api"
import { serverNow, setServerNow } from "@/lib/format"
import type { Activity, RepoState, Task } from "@/lib/types"

type GudContextValue = {
  mode: ApiMode
  loading: boolean
  state: RepoState | null
  tasks: Task[]
  activity: Activity[]
  now: number
  refresh: () => Promise<void>
  openId: string | null
  setOpenId: (id: string | null) => void
}

const GudContext = createContext<GudContextValue | null>(null)

export function GudProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ApiMode>("mock")
  const [loading, setLoading] = useState(true)
  const [state, setState] = useState<RepoState | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [activity, setActivity] = useState<Activity[]>([])
  const [now, setNow] = useState(() => Date.now() / 1000)
  const [openId, setOpenId] = useState<string | null>(null)
  const refreshRef = useRef<() => Promise<void>>(async () => {})

  const refresh = useCallback(async () => {
    const [nextState, nextTasks, nextActivity] = await Promise.all([
      getState(),
      getTasks(),
      getActivity(200),
    ])
    setServerNow(nextState.now)
    setState(nextState)
    setTasks(nextTasks)
    setActivity(nextActivity)
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
    const schedule = () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        void refreshRef.current()
      }, 250)
    }
    void (async () => {
      const nextMode = await bootApi()
      if (cancelled) return
      setMode(nextMode)
      unsubscribe = subscribe((event) => {
        if (event.event === "chain") {
          toast.success("Link landed", { description: event.data.tip.subject })
        } else if (event.event === "proposal" && event.data.status === "objected") {
          toast.error("Objection", { description: `${event.data.subject} — ${event.data.reason}` })
        } else if (event.event === "task" && event.data.status === "done") {
          toast.success("Task done", { description: event.data.title })
        }
        schedule()
      })
      try {
        await refreshRef.current()
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Could not load gud")
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
      unsubscribe()
      window.clearTimeout(timer)
    }
  }, [])

  const value = useMemo(
    () => ({ mode, loading, state, tasks, activity, now, refresh, openId, setOpenId }),
    [mode, loading, state, tasks, activity, now, refresh, openId],
  )

  return <GudContext.Provider value={value}>{children}</GudContext.Provider>
}

export function useGud(): GudContextValue {
  const value = useContext(GudContext)
  if (!value) throw new Error("useGud must be used inside GudProvider")
  return value
}
