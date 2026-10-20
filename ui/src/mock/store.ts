// The offline host: answers `/api/v1/view/*` from the v1 story in `world.ts`, applies the UI's
// actions (as @nelson), and drives a short timeline so the dashboard moves: an approval lands the
// windowed push, the tester passes the new tip (green again, the fix task is done), and a new push
// opens a window.

import { ApiError } from "@/lib/errors"
import type {
  ActResult,
  BlameResponse,
  FileResponse,
  GudEvent,
  HostView,
  Link,
  NeedsResponse,
  Proposal,
  RedState,
  Task,
  ThreadMessage,
  TreeFile,
  TreeResponse,
  ViewState,
} from "@/lib/types"
import { createWorld, diffFor, hex, proposal, REPO, type World } from "@/mock/world"

export type MockMessage = { type: "event"; event: GudEvent } | { type: "head"; seq: number; time: number } | { type: "open" }

let world: World | null = null
const listeners = new Set<(message: MockMessage) => void>()
const HUMAN = "@nelson"

function getWorld(): World {
  world ??= createWorld(Date.now())
  return world
}

export function onMockEvent(listener: (message: MockMessage) => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function emit(type: string, data: Record<string, unknown>, concerns: string[] = []): GudEvent {
  const w = getWorld()
  const event: GudEvent = { n: w.events.length + 1, id: hex(`live-${type}-${w.events.length}-${Date.now()}`), type, at: Date.now(), concerns, data }
  w.events.push(event)
  const head = (w.log[0]?.seq ?? 0) + 1
  w.log.unshift({ seq: head, type: "mail", time: event.at, hash: hex(`entry-live-${head}`), id: String(data.push ?? data.link ?? data.task ?? event.id),
    kind: type, author: String(data.author ?? data.by ?? data.tester ?? "@host"), summary: type })
  for (const listener of listeners) listener({ type: "event", event })
  return event
}

function tip(w: World): Link {
  return w.links[0]!
}

function redState(w: World): RedState {
  const by = (state: string | null) => w.links.find((l) => l.state === state) ?? null
  const fb = by(w.red.first_bad)
  const fix = w.tasks.find((t) => t.id === w.fixTask) ?? null
  return {
    red: w.red.red, red_link: w.red.red_link, red_link_seq: by(w.red.red_link)?.seq ?? null, red_since: w.red.red_since,
    last_green: w.red.last_green, last_green_seq: by(w.red.last_green)?.seq ?? null, first_bad: w.red.first_bad,
    first_bad_seq: fb?.seq ?? null, first_bad_author: fb?.author ?? null, first_bad_title: fb?.title ?? null,
    fix_task: fix && w.red.red ? { id: fix.id, alias: fix.alias, status: fix.status, title: fix.title, offered_to: fix.offered_to,
      offer_until: fix.offer_until, cited_link: fix.cited_link, holder: fix.holder } : null,
  }
}

function state(): ViewState {
  const w = getWorld()
  const now = Date.now()
  return {
    repo: REPO, bugs_address: `bugs@acme.example`, host_key: w.members.find((m) => m.role === "host")?.key ?? "", transport: "mock",
    now, t: now, log_head: { seq: w.log[0]?.seq ?? 0, hash: w.log[0]?.hash ?? "", time: w.log[0]?.time ?? now, type: "mail" },
    processed: { seq: w.log[0]?.seq ?? 0, hash: w.log[0]?.hash ?? "" },
    config: w.config,
    test_policy: { command: "pytest -q", image: "python:3.12-slim", timeout: 600_000, on_paths: ["app/auth.py"], on_tip_links: 1,
      on_tip_time: 600_000, every_link: false, stack_max: 8, auto_revert: null },
    tip: tip(w), red: redState(w),
    counts: { links: w.links.length, proposals_live: w.proposals.filter((p) => p.live).length,
      tasks_open: w.tasks.filter((t) => t.status !== "done").length, claims_active: w.claims.filter((c) => c.status === "active").length,
      escalations_open: w.escalations.filter((e) => e.status === "open").length },
    ui: { signers: [{ handle: HUMAN, role: "human" }], human: HUMAN, console: null, actions: true },
  }
}

function tree(at: string | null): TreeResponse {
  const w = getWorld()
  const link = w.links.find((l) => l.state === at) ?? tip(w)
  const files: TreeFile[] = Object.entries(w.files).map(([path, f]) => {
    const owners: Record<string, number> = {}
    const live: Record<string, number> = {}
    for (const line of f.lines) {
      owners[line.owner] = (owners[line.owner] ?? 0) + 1
      if (isLive(w, line.seq)) live[line.owner] = (live[line.owner] ?? 0) + 1
    }
    return { path, mode: f.mode, size: f.binary?.size ?? f.lines.reduce((n, l) => n + l.text.length + 1, 0), sha256: hex(path),
      binary: Boolean(f.binary), lines: f.binary ? null : f.lines.length, owners, live_owners: live, former: [] }
  })
  return { at: { kind: "link", state: link.state, seq: link.seq }, files }
}

function isLive(w: World, seq: number): boolean {
  const t = tip(w)
  if (t.seq - seq < w.config.own_links) return true
  const written = w.links.find((l) => l.seq === seq)?.linked_at ?? 0
  return Date.now() - written < w.config.own_time
}

function blame(path: string, at: string | null): BlameResponse {
  const w = getWorld()
  const f = w.files[path]
  const link = w.links.find((l) => l.state === at) ?? tip(w)
  if (!f) throw new ApiError(404, `${path} is not in the tree`)
  return {
    path, at: { kind: "link", state: link.state, seq: link.seq }, binary: Boolean(f.binary), own_links: w.config.own_links,
    own_time: w.config.own_time, t: Date.now(),
    lines: f.lines.map((line, i) => {
      const src = w.links.find((l) => l.seq === line.seq)
      const live = isLive(w, line.seq)
      return {
        n: i + 1, text: f.binary ? `(binary file, ${f.binary.size} bytes)` : line.text, owner: line.owner, seq: line.seq,
        link: src?.state ?? null, intent: src?.title ?? "", live, voter: true, member: true,
        expires_at: live && link.seq - line.seq >= w.config.own_links ? (src?.linked_at ?? 0) + w.config.own_time : null,
        links_left: Math.max(0, w.config.own_links - (link.seq - line.seq)),
      }
    }),
  }
}

function file(path: string): FileResponse {
  const w = getWorld()
  const f = w.files[path]
  if (!f) throw new ApiError(404, `${path} is not in that tree`)
  if (f.binary) return { path, mode: f.mode, size: f.binary.size, sha256: hex(path), kind: "binary", placeholder: `binary file, ${f.binary.size} bytes` }
  return { path, mode: f.mode, size: 0, sha256: hex(path), kind: "text", text: f.lines.map((l) => l.text).join("\n") + "\n" }
}

function thread(id: string): { head: { id: string; type: string }; messages: ThreadMessage[] } {
  const w = getWorld()
  const prop = w.proposals.find((p) => p.id === id || p.push === id)
  const link = w.links.find((l) => l.state === id || l.push === id)
  const task = w.tasks.find((t) => t.id === id)
  const key = prop?.negotiation ?? prop?.push ?? link?.push ?? id
  let messages = w.threads[key] ?? w.threads[id]
  if (!messages) {
    const author = prop?.author ?? link?.author ?? task?.author ?? HUMAN
    const title = prop?.title ?? link?.title ?? task?.title ?? "message"
    messages = [{ id, seq: null, received_at: prop?.received_at ?? link?.received_at ?? task?.opened_at ?? Date.now(),
      kind: task ? (task.kind === "subtask" ? "subtask" : "task") : "push", author, from: `${author.slice(1)}@acme.example`,
      subject: `[gud] ${title}`, signed: true, text: task?.description || title, message_id: "", headers: {}, evidence: null }]
  }
  return { head: { id, type: prop ? "proposal" : link ? "link" : task ? "task" : "message" }, messages }
}

function host(): HostView {
  const w = getWorld()
  const head = w.log[0]
  return {
    repo: REPO, key: w.members.find((m) => m.role === "host")?.key ?? "", repos: [REPO],
    head: head ? { seq: head.seq, hash: head.hash, time: head.time, type: head.type } : null, processed_seq: head?.seq ?? 0,
    entries: (head?.seq ?? 0) + 1, counts: { open: 1, mail: Math.max(0, (head?.seq ?? 0) - 9), tick: 9 }, tail: w.log.slice(0, 40),
    handed_over: false, missing_mail: [], last_error: null, next_wakeup: Date.now() + 20_000,
    intake: { received: 71, logged: 70, duplicates: 1, ignored: 0, rate_limited: 0, ticks: 9, webhooks: 0 },
    dispatch: { webhook: 64, email: 3, notify: 2, failures: 0 },
    deliveries: w.members.filter((m) => m.delivery?.registered).map((m) => ({ handle: m.handle, channel: m.delivery!.channel,
      url: m.delivery!.url, journal_head: m.delivery!.journal_head, acked: m.delivery!.acked })),
    feed_head: w.events.length,
    audit: { ok: true, exit_status: 0, log_ok: true, log_error: null, checked_at: Date.now(), mail_entries: 70, seen_mismatches: 0,
      fork_proofs: 0, late_votes: 1, missing_mail: 0, altered: 0 },
  }
}

function needs(): NeedsResponse {
  const w = getWorld()
  const items: NeedsResponse["items"] = []
  for (const e of w.escalations) if (e.status === "open") items.push({ type: "escalation", escalation: e.id, pushes: e.pushes.map((p) => p.root), deadline: e.deadline })
  for (const t of w.tasks) if (t.waiting === "acceptance" && t.requester === HUMAN) items.push({ type: "accept", task: t.id, alias: t.alias })
  for (const p of w.proposals) if (p.live && p.plan.askers.includes(HUMAN) && !p.approvals.some((a) => a.by === HUMAN)) items.push({ type: "review", push: p.push })
  return { member: HUMAN, items }
}

function linkProposal(w: World, p: Proposal) {
  const prev = tip(w)
  const seq = prev.seq + 1
  const state = p.carried ? hex(`carried-${p.push}`) : p.push
  const l: Link = {
    seq, state, push: p.push, kind: p.kind, author: p.author, intent: p.intent, title: p.title, tree_hash: hex(`tree-l${seq}`),
    parent: prev.state, named_parent: p.named_parent, carried_from: p.carried ? p.named_parent : null,
    carried_from_seq: p.carried ? p.named_parent_seq : null, revert_of: null, received_at: p.received_at, linked_at: Date.now(),
    task: p.task, task_alias: p.task_alias, files: p.files, verdict: null, verdict_at: null, red_role: null,
    diffstat: { added: 2, removed: 1, files: p.files, binary: [] },
  }
  w.links.unshift(l)
  w.proposals = w.proposals.filter((x) => x.push !== p.push)
  for (const other of w.proposals) {
    if (!other.live) continue
    other.parent = l.state
    other.parent_seq = l.seq
    other.carried = other.named_parent !== other.parent
    other.race = other.race != null ? Math.max(0, other.race - 1) : null
    other.blocks_tip = other.race === 0 && other.status !== "qualified"
  }
  if (w.diffs[p.push]) w.diffs[l.state] = w.diffs[p.push]!
  emit("linked", { link: l.state, push: l.push, seq, author: l.author, files: l.files, carried_from: l.carried_from }, [l.author])
}

function act(kind: string, body: Record<string, unknown>): ActResult {
  const w = getWorld()
  const id = hex(`act-${kind}-${Date.now()}`)
  const target = String(body.target ?? body.join ?? "")
  const p = w.proposals.find((x) => x.push === target || x.id === target)
  const t = w.tasks.find((x) => x.id === target)
  const text = String(body.text ?? "").trim()
  const types: string[] = []
  const say = (type: string, data: Record<string, unknown>) => types.push(emit(type, data).type)
  if (kind === "task") {
    const n = w.tasks.filter((x) => x.kind !== "subtask").length + 1
    const created: Task = { ...w.tasks[0]!, id, alias: `T${n}`, kind: "task", title: String(body.title ?? "task"), description: String(body.description ?? ""),
      author: HUMAN, requester: HUMAN, opened_at: Date.now(), status: "open", children: [], claim: null, holder: null, claims: [], links: [],
      waiting: null, work_done_at: null, work_done_link: null, accept_paths: (body.accept as string[] | undefined) ?? [], accept_cmds: [],
      has_criteria: Boolean((body.accept as string[] | undefined)?.length), cited_link: null, cited_seq: null, cited_author: null,
      offered_to: null, offer_until: null, bug: (body.bug as string | undefined) ?? null }
    w.tasks.push(created)
    say("task.opened", { task: id, alias: created.alias, title: created.title, requester: HUMAN })
  } else if (kind === "vote" && body.kind === "approve" && t) {
    t.status = "done"
    t.waiting = null
    t.done_at = Date.now()
    say("task.done", { task: t.id, link: tip(w).state })
  } else if (kind === "vote" && p) {
    if (body.kind === "approve") {
      p.approvals.push({ by: HUMAN, at: Date.now() })
      say("approved", { push: p.push, by: HUMAN, approve: id })
      if (p.escalation) {
        const esc = w.escalations.find((e) => e.id === p.escalation)
        if (esc) {
          esc.status = "resolved"
          esc.chosen = p.negotiation
          esc.decided_by = HUMAN
          esc.decided_at = Date.now()
          say("escalation.resolved", { escalation: esc.id, chosen: p.negotiation, by: HUMAN })
          for (const o of w.proposals.filter((x) => x.negotiation === p.negotiation)) {
            for (const ob of o.objections) {
              ob.status = "advisory"
              ob.blocking = false
            }
            o.status = "qualified"
            o.escalation = null
          }
          linkProposal(w, p)
        }
      }
    } else if (body.kind === "hold") {
      p.timers.held = true
      p.timers.held_by = HUMAN
      if (p.timers.window_closes_at && p.timers.window) {
        p.timers.window_closes_at += p.timers.window
        p.timers.grace_until = (p.timers.grace_until ?? 0) + p.timers.window
        p.timers.park_at = p.timers.grace_until
      }
      say("held", { push: p.push, by: HUMAN, hold: id })
    } else if (body.kind === "veto") {
      p.status = "vetoed"
      p.live = false
      p.dead = true
      say("objected", { push: p.push, by: HUMAN, reason: `veto ${text}`.trim(), veto: true })
      say("push.rejected", { push: p.push, status: "vetoed" })
    } else {
      p.objections.push({ id, by: HUMAN, reason: text, received: Date.now(), kind: "reason", evidence: null, status: "blocking",
        answered_by: null, episode: 1, blocking: true })
      p.status = "objected"
      say("objected", { push: p.push, by: HUMAN, reason: text })
    }
  } else if (kind === "comment") {
    say("commented", { target, by: HUMAN, text, comment: id })
  } else if (kind === "admit") {
    const j = w.joins.find((x) => x.id === target)
    if (!j) throw new ApiError(404, "no such join")
    const push = proposal(`admit-${j.handle}`, HUMAN, `admit ${j.handle} (${j.role})`, Date.now(), w.config, tip(w).seq, [".gud/members"], {
      status: "qualified", join: j.id, plan: { line_owners: [], askers: [], declared: [], quorum_needed: null, quorum_have: null, members_rule: true, waiting_on: [] },
    })
    j.status = "proposed"
    j.push = push.push
    w.proposals.push(push)
    say("push.received", { push: push.push, author: HUMAN, files: push.files })
    window.setTimeout(() => {
      linkProposal(w, push)
      j.status = "member"
      w.members.push({ ...w.members[0]!, handle: j.handle, address: j.address ?? "", key: j.key, key_short: j.key_short, sponsor: j.sponsor,
        lines: 0, lines_live: 0, added_seq: tip(w).seq, delivery: null })
    }, 1500)
    return { id: push.push, kind: "push", as: HUMAN, logged: true, events: types, member: { handle: j.handle, role: j.role ?? "agent" } } as ActResult
  } else {
    throw new ApiError(404, "nothing to act on")
  }
  return { id, kind: kind === "vote" ? String(body.kind) : kind, as: HUMAN, logged: true, events: types }
}

function decode(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export async function mockRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const w = getWorld()
  const url = new URL(path, "http://gud.local")
  const method = (init?.method ?? "GET").toUpperCase()
  const q = (name: string) => url.searchParams.get(name)
  const p = url.pathname
  if (method === "POST") {
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {}
    const m = /^\/api\/v1\/view\/act\/(task|vote|comment|admit)$/.exec(p)
    if (m?.[1]) return act(m[1], body) as T
    throw new ApiError(404, `no mock for POST ${p}`)
  }
  switch (p) {
    case "/api/v1/view/state":
      return state() as T
    case "/api/v1/view/chain":
      return { links: w.links, total: w.links.length, next_before: null } as T
    case "/api/v1/view/proposals":
      return { proposals: w.proposals, live: w.proposals.filter((x) => x.live).length, dead_shown: w.proposals.filter((x) => x.dead).length } as T
    case "/api/v1/view/tasks":
      return {
        tasks: w.tasks, claims: w.claims,
        overlaps: [{ claims: [w.claims[0]!.id, w.claims[1]!.id], holders: [w.claims[0]!.holder, w.claims[1]!.holder],
          tasks: [w.claims[0]!.task, w.claims[1]!.task], globs: ["app/auth.py"], paths: ["app/auth.py"] }],
        bugs: [{ id: hex("bug-1"), from: "ann@customer.example", subject: "Login says 429 after one try", text: "Since this morning the first wrong password locks me out.",
          received_at: Date.now() - 30 * 60_000, task: null, task_alias: null }],
      } as T
    case "/api/v1/view/escalations":
      return { escalations: w.escalations } as T
    case "/api/v1/view/members":
      return { members: w.members, removed: [{ handle: "@sub-2", role: "agent", address: "sub-2@acme.example", removed_seq: 2,
        removed_link: w.links.find((l) => l.seq === 2)?.state ?? "", lines: 0 }], joins: w.joins } as T
    case "/api/v1/view/host":
      return host() as T
    case "/api/v1/view/needs":
      return needs() as T
    case "/api/v1/view/events": {
      const after = Number(q("after") ?? 0)
      return { events: w.events.slice(after), head: w.events.length } as T
    }
    case "/api/v1/view/tree":
      return tree(q("at")) as T
    case "/api/v1/view/file":
      return file(q("path") ?? "") as T
    case "/api/v1/view/blame":
      return blame(q("path") ?? "", q("at")) as T
    default:
      break
  }
  const diff = /^\/api\/v1\/view\/diff\/(.+)$/.exec(p)
  if (diff?.[1]) {
    const d = diffFor(w, decode(diff[1]))
    if (!d) throw new ApiError(404, "not a link or a push")
    return d as T
  }
  const th = /^\/api\/v1\/view\/thread\/(.+)$/.exec(p)
  if (th?.[1]) return thread(decode(th[1])) as T
  throw new ApiError(404, `no mock for ${method} ${p}`)
}

/** A short scripted timeline so the offline dashboard moves. */
export function startMockDriver(): () => void {
  const timers: number[] = []
  const at = (ms: number, fn: () => void) => timers.push(window.setTimeout(fn, ms))
  for (const listener of listeners) listener({ type: "open" })
  at(12_000, () => {
    const w = getWorld()
    const p = w.proposals.find((x) => x.title.startsWith("cap overage"))
    if (!p || !p.live) return
    p.approvals.push({ by: "@auth-agent", at: Date.now() })
    p.plan.waiting_on = []
    emit("approved", { push: p.push, by: "@auth-agent" }, [p.author])
    p.status = "qualified"
    linkProposal(w, p)
  })
  at(22_000, () => {
    const w = getWorld()
    const t = tip(w)
    t.verdict = "pass"
    t.verdict_at = Date.now()
    emit("test.passed", { scope: "tip", target: t.state, tester: "@t1", result: "pass" })
    w.red.red = false
    const fix = w.tasks.find((x) => x.id === w.fixTask)
    if (fix) {
      fix.status = "done"
      fix.done_at = Date.now()
    }
    emit("tip.green", { link: t.state, red_link: w.red.red_link })
  })
  at(34_000, () => {
    const w = getWorld()
    const p = proposal(`prop-live-${Date.now()}`, "@auth-agent", "log 429s with the user id", Date.now(), w.config, tip(w).seq, ["app/auth.py"], {
      plan: { line_owners: ["@nelson"], askers: ["@nelson"], declared: [], quorum_needed: null, quorum_have: null, members_rule: false, waiting_on: [HUMAN] },
    })
    w.proposals.unshift(p)
    emit("push.received", { push: p.push, author: p.author, files: p.files })
    emit("review.requested", { push: p.push, owner: HUMAN, why: "line" })
  })
  return () => {
    for (const id of timers) window.clearTimeout(id)
  }
}
