// The offline story, in the exact shapes of the v1 view API. Repo repo@acme.example: four links
// and a red tip (first bad #3, fix task T3 offered to @api-agent), a carried push, a negotiation that
// escalated after two rounds, an evidence objection the tester confirmed, two claims that overlap
// on app/auth.py, a pending join request, and genesis lines whose ownership has expired.

import type {
  Claim,
  DiffFile,
  DiffResponse,
  Escalation,
  GudEvent,
  HostLogEntry,
  JoinRequest,
  Link,
  Member,
  Objection,
  Proposal,
  ProposalStatus,
  RepoConfig,
  Task,
  ThreadMessage,
} from "@/lib/types"

export const REPO = "repo@acme.example"
const MIN = 60_000

export function hex(seed: string): string {
  let h1 = 0x811c9dc5
  let out = ""
  for (let round = 0; out.length < 64; round++) {
    for (let i = 0; i < seed.length; i++) h1 = Math.imul(h1 ^ seed.charCodeAt(i) ^ round, 16777619) >>> 0
    out += h1.toString(16).padStart(8, "0")
  }
  return out.slice(0, 64)
}

export type FileLine = { owner: string; seq: number; text: string }
export type MockFile = { mode: string; binary?: { size: number }; lines: FileLine[] }

export type World = {
  t0: number
  config: RepoConfig
  links: Link[]
  proposals: Proposal[]
  tasks: Task[]
  claims: Claim[]
  escalations: Escalation[]
  members: Member[]
  joins: JoinRequest[]
  files: Record<string, MockFile>
  diffs: Record<string, DiffFile[]>
  threads: Record<string, ThreadMessage[]>
  events: GudEvent[]
  log: HostLogEntry[]
  red: { red: boolean; red_since: number | null; red_link: string | null; last_green: string | null; first_bad: string | null }
  fixTask: string | null
  step: number
}

function member(handle: string, role: Member["role"], sponsor: string | null, lines = 0, live = lines): Member {
  const key = `ed25519:${btoa(hex(handle).slice(0, 32)).slice(0, 43)}=`
  return {
    handle, role, address: `${handle.slice(1)}@acme.example`, key, key_short: key.slice(8, 20), sponsor,
    votes: role === "agent" || role === "human", lines, lines_live: live, added_seq: 0,
    delivery: { channel: role === "agent" ? "webhook" : role === "tester" ? "sse" : "email",
      url: role === "agent" ? `https://agents.acme.example/${handle.slice(1)}/hook` : null,
      registered: role === "agent" || role === "tester", journal_head: 12, acked: 12 },
  }
}

function link(seq: number, author: string, title: string, at: number, files: string[], extra: Partial<Link> = {}): Link {
  const state = hex(`link-${seq}`)
  return {
    seq, state, push: extra.carried_from ? hex(`push-${seq}`) : state, kind: "push", author, intent: title, title,
    tree_hash: hex(`tree-${seq}`), parent: seq ? hex(`link-${seq - 1}`) : null,
    named_parent: seq ? hex(`link-${seq - 1}`) : null, carried_from: null, carried_from_seq: null, revert_of: null,
    received_at: at - 40_000, linked_at: at, task: null, task_alias: null, files, verdict: null, verdict_at: null,
    red_role: null, diffstat: { added: 4, removed: 1, files, binary: files.filter((f) => f.endsWith(".png")) }, ...extra,
  }
}

function objection(id: string, by: string, reason: string, at: number, extra: Partial<Objection> = {}): Objection {
  return { id: hex(id), by, reason, received: at, kind: "reason", evidence: null, status: "blocking", answered_by: null,
    episode: 1, blocking: true, ...extra }
}

export function proposal(seed: string, author: string, title: string, received: number, cfg: RepoConfig, parentSeq: number,
  files: string[], extra: Partial<Proposal> = {}): Proposal {
  const push = hex(seed)
  const open = extra.timers?.window_open ?? received
  const close = open + cfg.window
  const status: ProposalStatus = extra.status ?? "window"
  return {
    id: push, push, kind: "push", author, title, intent: title, received_at: received, status, reason: "",
    live: true, dead: false, parent: hex(`link-${parentSeq}`), parent_seq: parentSeq, named_parent: hex(`link-${parentSeq}`),
    named_parent_seq: parentSeq, carried: false, task: null, task_alias: null, revises: null, negotiation: push, files,
    tree_hash: hex(`tree-${seed}`),
    timers: { window_open: open, window_closes_at: close, grace_until: close + cfg.grace, park_at: close + cfg.grace,
      expires_at: received + cfg.expire, window: cfg.window, grace: cfg.grace, held: false, held_by: null, qualified_at: null },
    plan: { line_owners: [], askers: [], declared: [], quorum_needed: null, quorum_have: null, members_rule: false, waiting_on: [] },
    approvals: [], objections: [], rounds: 0,
    revisions: [{ push, candidate: push, status, received_at: received, revises: null, current: true }],
    linked_revisions: [], test: { needed: false, result: null }, pending_blobs: [], escalation: null, race: null,
    blocks_tip: false, join: null, ...extra,
  }
}

function claim(seed: string, task: Task, holder: string, files: string[], won: number, cfg: RepoConfig): Claim {
  return { id: hex(seed), latest: hex(seed), task: task.id, task_alias: task.alias, holder, files, after: [], won_at: won,
    last_activity: won + 4 * MIN, lapses_at: won + 4 * MIN + cfg.lease, status: "active", overlaps: [] }
}

function task(alias: string, title: string, opened: number, extra: Partial<Task> = {}): Task {
  return {
    id: hex(`task-${alias}`), alias, kind: "task", title, description: "", author: "@nelson", requester: "@nelson",
    parent: null, parent_alias: null, children: [], opened_at: opened, status: "open", accept_paths: [], accept_cmds: [],
    has_criteria: false, after: [], files_hint: [], bug: null, claim: null, holder: null, work_done_at: null,
    work_done_link: null, done_at: null, done_link: null, no_change: [], cited_link: null, cited_seq: null, cited_author: null,
    offered_to: null, offer_until: null, waiting: null, claims: [], links: [], ...extra,
  }
}

function msg(seed: string, kind: string, author: string, at: number, text: string, headers: Record<string, string> = {},
  evidence: ThreadMessage["evidence"] = null): ThreadMessage {
  const id = hex(seed)
  return { id, seq: null, received_at: at, kind, author, from: `${author.slice(1)}@acme.example`,
    subject: `[gud] ${kind} ${text.split("\n")[0]} [gud:${id.slice(0, 12)}]`, signed: true, text, message_id: `<${id.slice(0, 16)}@acme.example>`,
    headers, evidence }
}

export function createWorld(now: number): World {
  const cfg: RepoConfig = {
    window: 90_000, grace: 30_000, skew: 120_000, own_links: 3, own_time: 60 * MIN, lease: 30 * MIN,
    escalate: 60 * MIN, expire: 7 * 24 * 60 * MIN, carry: "file", fix_offer: 5 * MIN, lease_thrash: 3, checkpoint: 1000,
    authserv_id: "mx.acme.example",
  }
  const t0 = now - 3 * 60 * MIN
  const T1 = task("T1", "Per-user rate limiting on login", t0 + 5 * MIN, {
    description: "Five failed logins per minute per user, then 429.", accept_paths: ["tests/test_auth.py"], has_criteria: true,
  })
  const T2 = task("T2", "Bill overage above the free tier", t0 + 6 * MIN, { description: "Charge 0.01 per request above 10k." })
  const T21 = task("T2.1", "Overage tests", t0 + 70 * MIN, { kind: "subtask", parent: T2.id, parent_alias: "T2", author: "@billing-agent", files_hint: ["tests/test_billing.py"] })
  T2.children = [T21.id]
  const links: Link[] = [
    link(0, "@nelson", "genesis", t0, [".gud/config", ".gud/members", "app/auth.py", "app/billing.py", "logo.png", "tests/test_auth.py"],
      { verdict: "pass", verdict_at: t0 + MIN }),
    link(1, "@auth-agent", "rate limit login per user", t0 + 80 * MIN, ["app/auth.py", "tests/test_auth.py"],
      { task: T1.id, task_alias: "T1", verdict: "pass", verdict_at: t0 + 82 * MIN, red_role: "last_green" }),
    link(2, "@billing-agent", "meter requests for billing", t0 + 95 * MIN, ["app/billing.py"],
      { task: T2.id, task_alias: "T2", carried_from: hex("link-0"), carried_from_seq: 0, named_parent: hex("link-0") }),
    link(3, "@api-agent", "docs and a new logo", t0 + 120 * MIN, ["README.md", "logo.png"],
      { verdict: "fail", verdict_at: now - 6 * MIN, red_role: "first_bad" }),
    link(4, "@auth-agent", "tidy the limiter", now - 8 * MIN, ["app/auth.py"], { task: T1.id, task_alias: "T1" }),
  ]
  const L = (seq: number) => links[seq]!
  T1.status = "work_done"
  T1.waiting = "criteria"
  T1.work_done_at = L(4).linked_at
  T1.work_done_link = L(4).state
  T1.links = [{ seq: 1, state: L(1).state, author: "@auth-agent" }, { seq: 4, state: L(4).state, author: "@auth-agent" }]
  T2.status = "claimed"
  T2.links = [{ seq: 2, state: L(2).state, author: "@billing-agent" }]
  const fix = task("T3", `fix red tip at ${L(3).state.slice(0, 12)}`, now - 6 * MIN, {
    kind: "fix", author: null, cited_link: L(3).state, cited_seq: 3, cited_author: "@api-agent", offered_to: "@api-agent",
    offer_until: now - 6 * MIN + cfg.fix_offer, files_hint: ["README.md", "logo.png"],
  })
  const c1 = claim("claim-auth", T1, "@auth-agent", ["app/auth.py", "tests/**"], t0 + 10 * MIN, cfg)
  const c2 = claim("claim-billing", T2, "@billing-agent", ["app/billing.py", "app/auth.py"], t0 + 12 * MIN, cfg)
  c1.last_activity = now - 8 * MIN
  c1.lapses_at = c1.last_activity + cfg.lease
  c2.last_activity = now - 27 * MIN
  c2.lapses_at = c2.last_activity + cfg.lease
  c1.overlaps = [c2.id]
  c2.overlaps = [c1.id]
  T1.claim = c1.id
  T1.holder = "@auth-agent"
  T1.claims = [c1]
  T2.claim = c2.id
  T2.holder = "@billing-agent"
  T2.claims = [c2]

  // a push in its window, asking the owner of the lines it rewrites
  const pa = proposal("prop-overage", "@billing-agent", "cap overage at 3x the plan", now - 25_000, cfg, 4, ["app/billing.py", "app/auth.py"], {
    task: T2.id, task_alias: "T2",
    plan: { line_owners: ["@auth-agent"], askers: ["@auth-agent"], declared: [], quorum_needed: null, quorum_have: null, members_rule: false, waiting_on: ["@auth-agent"] },
    race: 0, blocks_tip: true,
  })
  // carried: named #3, its files untouched since, so it rides onto #4 with its own window (§14)
  const pc = proposal("prop-carried", "@api-agent", "link the status page from the README", now - 9 * MIN, cfg, 4, ["README.md"], {
    carried: true, named_parent: L(3).state, named_parent_seq: 3, id: hex("cand-carried"),
    plan: { line_owners: [], askers: ["@nelson"], declared: [{ path: "README.md", rule: "lazy", owners: ["@nelson"] }], quorum_needed: null, quorum_have: null, members_rule: false, waiting_on: [] },
    approvals: [{ by: "@nelson", at: now - 8 * MIN }], status: "qualified", race: 1,
  })
  pc.timers.window_open = now - 9 * MIN
  pc.timers.window_closes_at = now - 9 * MIN + cfg.window
  // a negotiation: v1 objected, answered by v2, objected again: rounds escalation (§13.6–13.7)
  const v1 = proposal("prop-api-v1", "@api-agent", "rename /v1/login to /v2/login", now - 14 * MIN, cfg, 4, ["app/auth.py"], {
    status: "window",
    objections: [objection("obj-api-1", "@auth-agent", "clients still call /v1/login", now - 13 * MIN,
      { status: "advisory", blocking: false, answered_by: hex("prop-api-v2") })],
    plan: { line_owners: ["@auth-agent"], askers: ["@auth-agent"], declared: [], quorum_needed: null, quorum_have: null, members_rule: false, waiting_on: [] },
  })
  const v2 = proposal("prop-api-v2", "@api-agent", "rename /v1/login, keep a redirect", now - 11 * MIN, cfg, 4, ["app/auth.py"], {
    status: "escalated", revises: v1.push, negotiation: v1.push, escalation: hex("esc-1"), rounds: 2,
    objections: [objection("obj-api-2", "@auth-agent", "the redirect drops the rate-limit headers", now - 10 * MIN, { episode: 2 })],
    plan: { line_owners: ["@auth-agent"], askers: ["@auth-agent"], declared: [], quorum_needed: null, quorum_have: null, members_rule: false, waiting_on: [] },
  })
  v1.negotiation = v1.push
  v1.escalation = v2.escalation
  v1.rounds = 2
  const revs = [
    { push: v1.push, candidate: v1.id, status: v1.status, received_at: v1.received_at, revises: null, current: false },
    { push: v2.push, candidate: v2.id, status: v2.status, received_at: v2.received_at, revises: v1.push, current: true },
  ]
  v1.revisions = revs.map((r) => ({ ...r, current: r.push === v1.push }))
  v2.revisions = revs
  // an evidence objection the tester confirmed: passes on the parent, fails on the push
  const pe = proposal("prop-limit", "@console", "raise the login limit to 50", now - 4 * MIN, cfg, 4, ["app/auth.py"], {
    status: "objected",
    objections: [objection("obj-ev", "@auth-agent", "the limit test fails with 50", now - 3 * MIN,
      { kind: "evidence", status: "confirmed", evidence: { path: "tests/test_limit.py", sha256: hex("ev"), size: 412 } })],
    plan: { line_owners: ["@auth-agent"], askers: ["@auth-agent"], declared: [], quorum_needed: null, quorum_have: null, members_rule: false, waiting_on: [] },
  })
  const dead = proposal("prop-dup", "@billing-agent", "meter requests (first try)", t0 + 90 * MIN, cfg, 1, ["app/billing.py"], {
    status: "superseded", live: false, dead: true,
  })
  const esc: Escalation = {
    id: hex("esc-1"), reason: "rounds", opened_at: now - 10 * MIN, deadline: now - 10 * MIN + cfg.escalate,
    pushes: [{ root: v1.push, push: v2.push, author: "@api-agent", title: v2.title, status: "escalated", confirmed_evidence: false }],
    deciders: ["@nelson"], status: "open", chosen: null, declined: [], decided_by: null, decided_at: null,
    default: { chosen: v1.push, declined: [], why: "the push is chosen (§13.7 rounds default)" },
  }

  const members = [
    member("@api-agent", "agent", "@nelson", 9, 9),
    member("@auth-agent", "agent", "@nelson", 41, 41),
    member("@billing-agent", "agent", "@nelson", 22, 22),
    member("@console", "console", null),
    member("@host", "host", null),
    member("@nelson", "human", null, 18, 2),
    member("@t1", "tester", "@nelson"),
  ]
  const joins: JoinRequest[] = [{
    id: hex("join-codex"), handle: "@codex-agent", role: "agent", address: "codex@orbit.example", key: "ed25519:Y29kZXgtYWdlbnQta2V5LWZvci1kZW1vLW9ubHkhIQ==",
    key_short: "Y29kZXgtYWdl", sponsor: "@nelson", reason: "I can take the billing subtasks.", received_at: now - 2 * MIN,
    status: "pending", push: null,
  }]

  const files: Record<string, MockFile> = {
    "app/auth.py": { mode: "100644", lines: [
      { owner: "@nelson", seq: 0, text: "from time import time" },
      { owner: "@auth-agent", seq: 1, text: "from app.limits import Window" },
      { owner: "@nelson", seq: 0, text: "" },
      { owner: "@auth-agent", seq: 1, text: "WINDOW = Window(seconds=60, limit=5)" },
      { owner: "@nelson", seq: 0, text: "" },
      { owner: "@nelson", seq: 0, text: "def login(user, password):" },
      { owner: "@auth-agent", seq: 4, text: "    if not WINDOW.allow(user.id):" },
      { owner: "@auth-agent", seq: 1, text: "        return 429, {\"retry-after\": WINDOW.retry_after(user.id)}" },
      { owner: "@nelson", seq: 0, text: "    if not user.check(password):" },
      { owner: "@nelson", seq: 0, text: "        return 401, {}" },
      { owner: "@nelson", seq: 0, text: "    return 200, {\"session\": user.new_session()}" },
    ] },
    "app/billing.py": { mode: "100644", lines: [
      { owner: "@nelson", seq: 0, text: "FREE_TIER = 10_000" },
      { owner: "@billing-agent", seq: 2, text: "RATE = 0.01" },
      { owner: "@nelson", seq: 0, text: "" },
      { owner: "@billing-agent", seq: 2, text: "def overage(requests):" },
      { owner: "@billing-agent", seq: 2, text: "    return max(0, requests - FREE_TIER) * RATE" },
    ] },
    "tests/test_auth.py": { mode: "100644", lines: [
      { owner: "@auth-agent", seq: 1, text: "def test_sixth_login_is_limited(client):" },
      { owner: "@auth-agent", seq: 1, text: "    for _ in range(5):" },
      { owner: "@auth-agent", seq: 1, text: "        client.login(\"ann\", \"bad\")" },
      { owner: "@auth-agent", seq: 1, text: "    assert client.login(\"ann\", \"bad\").status == 429" },
    ] },
    "README.md": { mode: "100644", lines: [
      { owner: "@api-agent", seq: 3, text: "# acme" },
      { owner: "@api-agent", seq: 3, text: "" },
      { owner: "@api-agent", seq: 3, text: "Login is rate limited per user (5/min)." },
    ] },
    "logo.png": { mode: "100644", binary: { size: 18_244 }, lines: [{ owner: "@api-agent", seq: 3, text: "" }] },
    "run.sh": { mode: "100755", lines: [
      { owner: "@nelson", seq: 0, text: "#!/bin/sh" },
      { owner: "@nelson", seq: 0, text: "exec python -m app" },
    ] },
  }

  const diffs: Record<string, DiffFile[]> = {
    [L(1).state]: [{ op: "modify", path: "app/auth.py", binary: false, added: 4, removed: 0, hunks:
      "@@ -1,3 +1,6 @@\n from time import time\n+from app.limits import Window\n \n+WINDOW = Window(seconds=60, limit=5)\n+\n def login(user, password):\n" },
    { op: "add", path: "tests/test_auth.py", binary: false, added: 4, removed: 0, hunks: "@@ -0,0 +1,4 @@\n+def test_sixth_login_is_limited(client):\n+    for _ in range(5):\n+        client.login(\"ann\", \"bad\")\n+    assert client.login(\"ann\", \"bad\").status == 429\n" }],
    [L(3).state]: [
      { op: "add", path: "README.md", binary: false, added: 3, removed: 0, hunks: "@@ -0,0 +1,3 @@\n+# acme\n+\n+Login is rate limited per user (5/min).\n" },
      { op: "modify", path: "logo.png", binary: true, old_size: 9_120, new_size: 18_244, added: 0, removed: 0, hunks: "", placeholder: "binary file: 9120 → 18244 bytes (sha256 4e1f0c9d2a77)" },
      { op: "rename", path: "docs/notes.txt", src: "notes.txt", binary: false, added: 0, removed: 0, hunks: "" },
      { op: "mode", path: "run.sh", binary: false, old_mode: "100644", new_mode: "100755", added: 0, removed: 0, hunks: "" },
    ],
    [pa.push]: [{ op: "modify", path: "app/billing.py", binary: false, added: 2, removed: 1, hunks:
      "@@ -3,3 +3,4 @@\n \n def overage(requests):\n-    return max(0, requests - FREE_TIER) * RATE\n+    raw = max(0, requests - FREE_TIER) * RATE\n+    return min(raw, 3 * plan_price())\n" }],
    [v2.push]: [{ op: "modify", path: "app/auth.py", binary: false, added: 2, removed: 1, hunks:
      "@@ -5,2 +5,3 @@\n \n-def login(user, password):\n+@route(\"/v2/login\", redirect_from=\"/v1/login\")\n+def login(user, password):\n" }],
  }

  const threads: Record<string, ThreadMessage[]> = {
    [v1.push]: [
      msg("prop-api-v1", "push", "@api-agent", v1.received_at, v1.title, { parent: L(4).state }),
      msg("obj-api-1", "object", "@auth-agent", now - 13 * MIN, "clients still call /v1/login", { target: v1.push }),
      msg("prop-api-v2", "push", "@api-agent", v2.received_at, v2.title, { parent: L(4).state, revises: v1.push }),
      msg("obj-api-2", "object", "@auth-agent", now - 10 * MIN, "the redirect drops the rate-limit headers", { target: v2.push }),
    ],
    [pe.push]: [
      msg("prop-limit", "push", "@console", pe.received_at, pe.title, { parent: L(4).state }),
      msg("obj-ev", "object", "@auth-agent", now - 3 * MIN, "the limit test fails with 50", { target: pe.push },
        { path: "tests/test_limit.py", sha256: hex("ev"), size: 412 }),
      msg("test-ev", "test", "@t1", now - 2 * MIN, "evidence: base pass, candidate fail\n1 failed in 0.21s", { scope: "evidence", target: hex("obj-ev"), result: "fail", "base-result": "pass" }),
    ],
  }
  threads[v2.push] = threads[v1.push]!

  const events: GudEvent[] = []
  let n = 0
  const ev = (type: string, at: number, data: Record<string, unknown>, concerns: string[] = []) =>
    events.push({ n: ++n, id: hex(`ev-${type}-${n}`), type, at, concerns, data })
  ev("linked", t0, { link: L(0).state, push: L(0).push, seq: 0, author: "@nelson", files: L(0).files, carried_from: null })
  ev("task.opened", T1.opened_at, { task: T1.id, alias: "T1", title: T1.title, requester: "@nelson" })
  ev("task.opened", T2.opened_at, { task: T2.id, alias: "T2", title: T2.title, requester: "@nelson" })
  ev("claim.won", c1.won_at, { claim: c1.id, task: T1.id, holder: "@auth-agent", files: c1.files })
  ev("claim.won", c2.won_at, { claim: c2.id, task: T2.id, holder: "@billing-agent", files: c2.files })
  ev("claim.overlap", c2.won_at, { claims: [c1.id, c2.id], holders: ["@auth-agent", "@billing-agent"], globs: ["app/auth.py"] })
  for (const l of links.slice(1)) {
    ev("push.received", l.received_at, { push: l.push, parent: l.named_parent, author: l.author, files: l.files })
    if (l.carried_from) ev("carried", l.linked_at - 1000, { push: l.push, candidate: l.state, onto: l.parent, files: l.files })
    ev("linked", l.linked_at, { link: l.state, push: l.push, seq: l.seq, author: l.author, files: l.files, carried_from: l.carried_from })
  }
  ev("subtask.posted", T21.opened_at, { subtask: T21.id, alias: "T2.1", parent: T2.id, title: T21.title })
  ev("test.failed", now - 6 * MIN, { scope: "tip", target: L(3).state, tester: "@t1", result: "fail" })
  ev("tip.red", now - 6 * MIN, { red_link: L(3).state, last_green: L(1).state })
  ev("tip.bisected", now - 6 * MIN, { red_link: L(3).state, first_bad: L(3).state, author: "@api-agent", fix_task: fix.id })
  ev("fix.opened", now - 6 * MIN, { task: fix.id, alias: "T3", cited_link: L(3).state, author: "@api-agent", offered_to: "@api-agent" })
  ev("push.received", v1.received_at, { push: v1.push, parent: L(4).state, author: "@api-agent", files: v1.files })
  ev("objected", now - 13 * MIN, { push: v1.push, objection: hex("obj-api-1"), by: "@auth-agent", reason: "clients still call /v1/login" })
  ev("push.received", v2.received_at, { push: v2.push, parent: L(4).state, author: "@api-agent", files: v2.files })
  ev("objected", now - 10 * MIN, { push: v2.push, objection: hex("obj-api-2"), by: "@auth-agent", reason: "the redirect drops the rate-limit headers" })
  ev("escalated", now - 10 * MIN, { escalation: esc.id, reason: "rounds", pushes: [v1.push], deciders: ["@nelson"], deadline: esc.deadline })
  ev("carried", now - 8 * MIN, { push: pc.push, candidate: pc.id, onto: L(4).state, files: pc.files })
  ev("push.received", pe.received_at, { push: pe.push, parent: L(4).state, author: "@console", files: pe.files })
  ev("objected", now - 3 * MIN, { push: pe.push, objection: hex("obj-ev"), by: "@auth-agent", reason: "the limit test fails with 50", evidence: { path: "tests/test_limit.py" } })
  ev("test.failed", now - 2 * MIN, { scope: "evidence", target: hex("obj-ev"), tester: "@t1", result: "fail" })
  ev("push.received", pa.received_at, { push: pa.push, parent: L(4).state, author: "@billing-agent", files: pa.files })
  ev("review.requested", pa.received_at, { push: pa.push, candidate: pa.id, owner: "@auth-agent", why: "line" })

  const log: HostLogEntry[] = events.slice(-14).map((e, i) => ({
    seq: 60 + i, type: "mail" as const, time: e.at, hash: hex(`entry-${i}`), id: String(e.data.push ?? e.data.link ?? e.data.task ?? e.id),
    kind: e.type.startsWith("objected") ? "object" : e.type === "push.received" ? "push" : e.type.startsWith("test") ? "test" : "comment",
    author: String(e.data.author ?? e.data.by ?? e.data.tester ?? "@host"), summary: e.type,
  })).reverse()
  log.push({ seq: 0, type: "open", time: t0 - MIN, hash: hex("entry-open"), fields: { repo: REPO, key: members[4]!.key ?? "" } })

  return {
    t0, config: cfg, links: [...links].reverse(), proposals: [pa, v1, v2, pe, pc, dead], tasks: [T1, T2, T21, fix],
    claims: [c1, c2], escalations: [esc], members, joins, files, diffs, threads, events, log,
    red: { red: true, red_since: now - 6 * MIN, red_link: L(3).state, last_green: L(1).state, first_bad: L(3).state },
    fixTask: fix.id, step: 0,
  }
}

export function diffFor(world: World, id: string): DiffResponse | null {
  const link = world.links.find((l) => l.state === id || l.push === id)
  const prop = world.proposals.find((p) => p.id === id || p.push === id)
  const key = link?.state ?? prop?.push
  if (!key) return null
  const files = world.diffs[key] ?? (link ?? prop)!.files.map((path) => ({
    op: "modify" as const, path, binary: path.endsWith(".png"), added: 1, removed: 1,
    hunks: path.endsWith(".png") ? "" : `@@ -1,1 +1,1 @@\n-old line\n+new line\n`,
    placeholder: path.endsWith(".png") ? "binary file" : undefined,
  }))
  const unified = files.map((f) => `diff --git a/${f.path} b/${f.path}\n${f.placeholder ? "Binary files differ\n" : `--- a/${f.path}\n+++ b/${f.path}\n${f.hunks}`}`).join("")
  return {
    kind: link ? "link" : "proposal", push: (link ?? prop)!.push, author: (link ?? prop)!.author, available: true,
    ops: files.map((f) => (f.op === "rename" ? { op: "rename", src: f.src ?? "", dst: f.path } : f.op === "mode"
      ? { op: "mode", path: f.path, mode: f.new_mode ?? "100755" } : { op: "patch", path: f.path })),
    files, unified, seq: link?.seq, status: prop?.status, carried_from: link?.carried_from ?? null,
  }
}
