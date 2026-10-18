import { useEffect, useState, type FormEvent } from "react"
import { Activity, Files, GitCommitHorizontal, ListTodo, Users } from "lucide-react"
import { DetailSheet } from "@/components/detail-sheet"
import { copyText, ShareRepo } from "@/components/share-repo"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Textarea } from "@/components/ui/textarea"
import { useGud } from "@/hooks/use-gud"
import { createRepo, getSwarm, provisionAgent, setSwarm, type ProvisionedAgent } from "@/lib/api"
import { shortHash } from "@/lib/format"
import { isOpenStatus } from "@/lib/status"
import { cn } from "@/lib/utils"
import { ActivityView } from "@/views/activity-view"
import { ChainView } from "@/views/chain-view"
import { FilesView } from "@/views/files-view"
import { MembersView } from "@/views/members-view"
import { TasksView } from "@/views/tasks-view"

const VIEWS = [
  { id: "chain", label: "Chain", icon: GitCommitHorizontal },
  { id: "files", label: "Files", icon: Files },
  { id: "tasks", label: "Tasks", icon: ListTodo },
  { id: "members", label: "Members", icon: Users },
  { id: "activity", label: "Activity", icon: Activity },
] as const

type ViewId = (typeof VIEWS)[number]["id"]

function MailboxSignIn() {
  const { signIn } = useGud()
  const [token, setToken] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError("")
    try {
      await signIn(token.trim())
    } catch (err) {
      setError(err instanceof Error ? err.message : "That credential was rejected")
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="mx-auto grid w-full max-w-sm gap-3 p-8">
      <h2 className="text-sm font-medium">Sign in to the mailbox</h2>
      <p className="text-sm text-muted-foreground">Paste the Primitive token for this account. It is kept on this machine.</p>
      <Input
        type="password"
        autoComplete="off"
        value={token}
        placeholder="prim_..."
        onChange={(event) => setToken(event.target.value)}
      />
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      <Button type="submit" disabled={busy || !token.trim()}>
        Sign in
      </Button>
    </form>
  )
}

const AGENT_ROLES = ["agent", "lead", "tester", "console"] as const

function parseEmails(raw: string): string[] {
  return raw.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean)
}

function CreateRepoDialog() {
  const { openCreated } = useGud()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [emails, setEmails] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)

  async function submit() {
    const humans = parseEmails(emails)
    if (!name.trim() || humans.length === 0) {
      setError("Name and at least one email are required")
      return
    }
    setBusy(true)
    setError("")
    try {
      const catalog = await createRepo({ name: name.trim(), humans })
      setName("")
      setEmails("")
      setOpen(false)
      await openCreated(catalog)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the repo")
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setError("")
      }}
    >
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant="outline" className="h-7 w-full min-w-0 text-[11px]">
          New repo
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New repo</DialogTitle>
          <DialogDescription>Name the repo and list the humans who can open it.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <label className="grid gap-1 text-xs">
            Name
            <Input value={name} placeholder="name" autoComplete="off" onChange={(event) => setName(event.target.value)} />
          </label>
          <label className="grid gap-1 text-xs">
            Emails
            <Textarea
              value={emails}
              placeholder="comma or newline"
              rows={3}
              onChange={(event) => setEmails(event.target.value)}
            />
          </label>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
        </div>
        <DialogFooter>
          <Button disabled={busy || !name.trim() || parseEmails(emails).length === 0} onClick={() => void submit()}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RepoPicker() {
  const { state, mode, repos, activeRepo, needsToken, loading, refreshRepos, chooseRepo } = useGud()
  const options = activeRepo && !repos.includes(activeRepo) ? [activeRepo, ...repos] : repos
  const current = activeRepo && options.includes(activeRepo) ? activeRepo : options[0]

  if (mode !== "live") {
    return (
      <p className="mt-2 truncate font-mono text-[11px] text-muted-foreground" title={state?.repo}>
        {state?.repo ?? "…"}
      </p>
    )
  }

  return (
    <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)] gap-2">
      {needsToken ? (
        <p className="font-mono text-[11px] text-muted-foreground">sign in</p>
      ) : options.length === 0 ? (
        <p className="font-mono text-[11px] text-muted-foreground">{loading ? "…" : "no repos"}</p>
      ) : (
        <Select
          value={current}
          onOpenChange={(open) => {
            if (open) void refreshRepos()
          }}
          onValueChange={(value) => {
            if (value !== current) void chooseRepo(value)
          }}
        >
          <SelectTrigger
            className="h-7 w-full min-w-0 overflow-hidden font-mono text-[11px] *:data-[slot=select-value]:block! *:data-[slot=select-value]:min-w-0 *:data-[slot=select-value]:truncate"
            title={current}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {options.map((address) => (
              <SelectItem key={address} value={address} className="font-mono text-[11px]">
                {address}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {needsToken ? null : <CreateRepoDialog />}
    </div>
  )
}

function SwarmToggle() {
  const [enabled, setEnabled] = useState(false)
  const [ready, setReady] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    let cancelled = false
    void getSwarm()
      .then((body) => {
        if (!cancelled) setEnabled(body.enabled)
      })
      .catch(() => {
        if (!cancelled) setEnabled(false)
      })
      .finally(() => {
        if (!cancelled) setReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function toggle(next: boolean) {
    setBusy(true)
    setError("")
    try {
      const body = await setSwarm(next)
      setEnabled(body.enabled)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the swarm")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-1">
      <label className="flex items-start gap-2 text-[11px] leading-snug">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={enabled}
          disabled={busy || !ready}
          onChange={(event) => void toggle(event.target.checked)}
        />
        <span>
          <span className="font-medium">Swarm</span>
          <span className="block text-muted-foreground">Off by default. The in-process swarm is optional.</span>
        </span>
      </label>
      {error ? <p className="text-[11px] text-destructive">{error}</p> : null}
    </div>
  )
}

function provisionPrompt(issued: ProvisionedAgent, repo: string): string {
  const where = repo ? ` The repo is ${repo}.` : ""
  return `Your name is "${issued.handle}".${where} Use the key "${issued.private_key}" with gud MCP. Pass that handle, repo, and private_key on every call.`
}

function ProvisionAgent() {
  const { state } = useGud()
  const [handle, setHandle] = useState("")
  const [role, setRole] = useState<(typeof AGENT_ROLES)[number]>("agent")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [issued, setIssued] = useState<ProvisionedAgent | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!handle.trim()) {
      setError("Handle is required")
      return
    }
    setBusy(true)
    setError("")
    try {
      setIssued(await provisionAgent({ handle: handle.trim(), role }))
      setHandle("")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not provision the agent")
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="grid gap-1.5">
      <p className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">Provision agent</p>
      <Input
        value={handle}
        placeholder="@name"
        autoComplete="off"
        className="h-7 font-mono text-[11px]"
        onChange={(event) => setHandle(event.target.value)}
      />
      <Select value={role} onValueChange={(value) => setRole(value as (typeof AGENT_ROLES)[number])}>
        <SelectTrigger className="h-7 w-full text-[11px]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {AGENT_ROLES.map((item) => (
            <SelectItem key={item} value={item} className="text-[11px]">
              {item}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {error ? <p className="text-[11px] text-destructive">{error}</p> : null}
      <Button type="submit" size="sm" disabled={busy}>
        Provision
      </Button>
      {issued ? (
        <div className="grid gap-1">
          <p className="text-[10px] text-muted-foreground">Private key for {issued.handle}.</p>
          <textarea
            readOnly
            value={issued.private_key}
            className="h-14 w-full resize-none rounded-md border border-input bg-transparent p-1 font-mono text-[10px] break-all"
          />
          <p className="text-[10px] text-muted-foreground">Paste this into the chat.</p>
          <textarea
            readOnly
            value={provisionPrompt(issued, state?.repo ?? "")}
            className="h-28 w-full resize-none rounded-md border border-input bg-transparent p-1 font-mono text-[10px] break-all"
          />
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void copyText(provisionPrompt(issued, state?.repo ?? ""))}
          >
            Copy prompt
          </Button>
        </div>
      ) : null}
    </form>
  )
}

function parseHash(): ViewId {
  const raw = window.location.hash.replace(/^#\/?/, "")
  return VIEWS.some((view) => view.id === raw) ? (raw as ViewId) : "chain"
}

export function Shell() {
  const { state, mode, needsToken } = useGud()
  const [view, setView] = useState<ViewId>(parseHash)

  useEffect(() => {
    const onHash = () => setView(parseHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  function go(next: ViewId) {
    window.location.hash = `#/${next}`
    setView(next)
  }

  const tip = state?.tip ?? null
  const openCount = state?.proposals.filter((proposal) => isOpenStatus(proposal.status)).length ?? 0
  const title = VIEWS.find((item) => item.id === view)?.label ?? "Chain"

  return (
    <div className="flex h-full min-h-0 bg-background text-foreground">
      <aside className="flex h-full min-h-0 w-60 shrink-0 flex-col overflow-x-hidden overflow-y-auto border-r border-border bg-sidebar">
        <div className="min-w-0 px-3 py-3">
          <div className="flex items-baseline gap-2">
            <span className="text-base font-semibold tracking-tight">gud</span>
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">chain</span>
          </div>
          <RepoPicker />
          <div className="mt-1.5 flex items-center gap-1.5 font-mono text-[11px]">
            <span className="size-1.5 rounded-full bg-emerald-400" />
            {tip ? (
              <>
                <span>#{tip.seq}</span>
                <span className="text-muted-foreground">{shortHash(tip.state)}</span>
              </>
            ) : (
              <span className="text-muted-foreground">no tip</span>
            )}
          </div>
        </div>
        <Separator />
        <nav className="flex flex-1 flex-col gap-0.5 p-2">
          {VIEWS.map((item) => {
            const Icon = item.icon
            const active = view === item.id
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => go(item.id)}
                className={cn(
                  "flex h-8 items-center gap-2 rounded-md px-2 text-left text-sm text-muted-foreground hover:bg-sidebar-accent hover:text-foreground",
                  active && "bg-sidebar-accent font-medium text-foreground",
                )}
              >
                <Icon className="size-4" />
                <span>{item.label}</span>
                {item.id === "chain" && openCount > 0 ? (
                  <Badge variant="secondary" className="ml-auto h-4 px-1.5 font-mono text-[10px]">
                    {openCount}
                  </Badge>
                ) : null}
              </button>
            )
          })}
        </nav>
        {mode === "live" && !needsToken ? (
          <div className="grid gap-3 border-t border-border px-3 py-3">
            <SwarmToggle />
            <ProvisionAgent />
          </div>
        ) : null}
        <div className="px-3 py-3 font-mono text-[10px] text-muted-foreground">
          window {state?.window_seconds ?? "—"}s
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-11 shrink-0 items-center gap-3 border-b border-border px-4">
          <h1 className="text-sm font-medium">{title}</h1>
          <div className="ml-auto flex items-center gap-2">
            <ShareRepo repo={state?.repo} />
            {mode === "mock" ? (
              <Badge variant="outline" className="border-amber-400/40 text-[10px] tracking-wide text-amber-200 uppercase">
                mock data
              </Badge>
            ) : (
              <Badge variant="outline" className="text-[10px] uppercase">
                live
              </Badge>
            )}
          </div>
        </header>
        <main className="min-h-0 flex-1">
          {needsToken ? <MailboxSignIn /> : null}
          {!needsToken && view === "chain" ? <ChainView /> : null}
          {!needsToken && view === "files" ? <FilesView /> : null}
          {!needsToken && view === "tasks" ? <TasksView /> : null}
          {!needsToken && view === "members" ? <MembersView /> : null}
          {!needsToken && view === "activity" ? <ActivityView /> : null}
        </main>
      </div>
      <DetailSheet />
    </div>
  )
}
