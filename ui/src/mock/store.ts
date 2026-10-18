import { diffstat } from "@/lib/diff"
import { ApiError } from "@/lib/errors"
import { isOpenStatus } from "@/lib/status"
import type {
  Activity,
  BlameResponse,
  BugReport,
  EmailView,
  JoinRequest,
  Link,
  Proposal,
  RepoState,
  Task,
  TreeResponse,
} from "@/lib/types"
import { FILES, linesAt, membersAtSeq } from "@/mock/files"
import { geminiJoin, joinMailBody, joinSummary } from "@/mock/joins"
import {
  PUSH,
  REPO,
  WINDOW_SECONDS,
  addressOf,
  agentCc,
  fakeHash,
  linkId,
  mid,
  sig,
} from "@/mock/ids"
import { createWorld, type World } from "@/mock/world"

export type MockEvent = { event: string; data: unknown }

let world: World | null = null
const listeners = new Set<(event: MockEvent) => void>()
let subscribers = 0
let timer: number | null = null

export function getWorld(): World {
  if (!world) world = createWorld(Date.now() / 1000)
  return world
}

function emit(event: string, data: unknown) {
  const payload = structuredClone(data)
  for (const listener of listeners) listener({ event, data: payload })
}

export function onMockEvent(listener: (event: MockEvent) => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function startMockDriver(): () => void {
  const current = getWorld()
  subscribers += 1
  if (subscribers === 1) {
    current.t0 = Date.now() / 1000
    timer = window.setInterval(tick, 1000)
  }
  return () => {
    subscribers -= 1
    if (subscribers <= 0 && timer != null) {
      window.clearInterval(timer)
      timer = null
      subscribers = 0
    }
  }
}

function tick() {
  const current = getWorld()
  const now = Date.now() / 1000
  const elapsed = now - current.t0
  if (elapsed >= 6 && !current.flags.nonce) arriveNonce(current, now)
  if (elapsed >= 9 && !current.flags.retry) arriveRetry(current, now)
  if (elapsed >= 14 && !current.flags.quorum) bumpQuorum(current, now)
  if (elapsed >= 22 && !current.flags.objected) objectNonce(current, now)
  if (elapsed >= 28 && !current.flags.tested) passRetry(current, now)
  if (elapsed >= 36 && !current.flags.task) completeTask(current, now)
  if (elapsed >= 20 && !current.flags.gemini) arriveGemini(current, now)
  if (elapsed >= 45 && !current.flags.geminiLanded) landGemini(current, now)
  advanceWindow(current, now)
}

function tipOf(current: World): Link {
  const tip = current.links[current.links.length - 1]
  if (!tip) throw new ApiError(409, "repo has no tip")
  return tip
}

function arriveGemini(current: World, now: number) {
  current.flags.gemini = true
  const join = geminiJoin(now)
  current.joins.unshift(join)
  rememberMail(current, {
    id: join.id,
    handle: join.handle,
    fromAddr: join.address,
    subject: `Join ${join.handle}`,
    body: joinMailBody(join),
    at: now,
    kind: "join",
    summary: joinSummary(join),
    target: join.id,
  })
  emit("join", join)
}

function landGemini(current: World, now: number) {
  current.flags.geminiLanded = true
  const join = current.joins.find((item) => item.handle === "@gemini-agent")
  if (!join || !join.valid || join.status !== "pending") return
  join.status = "member"
  current.members.push({
    handle: join.handle,
    role: "agent",
    address: join.address,
    fingerprint: fakeHash(join.public_key).slice(0, 16),
    sponsor: join.sponsor,
    votes: true,
    lines_owned: 0,
    added_by_link: null,
  })
  const tip = tipOf(current)
  const host = window.location.origin
  rememberMail(current, {
    id: mid("welcome-gemini"),
    handle: "@console",
    subject: "Welcome @gemini-agent",
    body: trailer(`You are on the roster. Replay ${host} from the checkpoint below.`, [
      ["Kind", "notify"],
      ["Author", "@console"],
      ["Host", host],
      ["Tip", tip.id],
      ["State", tip.state],
      ["Tree", tip.tree_hash],
    ]),
    at: now,
    kind: "notify",
    summary: "welcomed @gemini-agent with the host URL and a checkpoint",
    target: join.id,
    to: [join.address, REPO],
  })
  const welcome = current.activity[0]
  if (welcome) emit("mail", welcome)
}

function arriveNonce(current: World, now: number) {
  current.flags.nonce = true
  const id = mid("push-nonce")
  current.nonceId = id
  const diff = [
    "diff --git a/ratelimit.py b/ratelimit.py",
    "--- a/ratelimit.py",
    "+++ b/ratelimit.py",
    "@@ -8,6 +8,9 @@",
    " def allow(user_id: str, bucket: str, limit: int) -> bool:",
    "+    if bucket == \"login\" and user_id in _seen:",
    "+        return False",
    "     key = (bucket, user_id)",
    "     _hits[key] += 1",
  ].join("\n")
  const proposal = baseProposal(current, {
    id,
    author: "@api-agent",
    subject: "Reject replayed nonces",
    intent: "Drop a login retry when the user id was already seen in this process.",
    at: now,
    status: "window",
    reason: "lazy consensus is open",
    affected: ["@sub-1"],
    windowClosesAt: now + WINDOW_SECONDS,
  })
  commitProposal(current, proposal, diff)
  emit("proposal", proposal)
}

function arriveRetry(current: World, now: number) {
  current.flags.retry = true
  const id = mid("push-retry")
  current.retryId = id
  const diff = [
    "diff --git a/billing.py b/billing.py",
    "--- a/billing.py",
    "+++ b/billing.py",
    "@@ -10,6 +10,8 @@",
    " def record(user_id: str, sku: str, cents: int) -> None:",
    "+    if sku == \"webhook\":",
    "+        cents = min(cents, 1)",
    "     if cents < 0:",
    "         raise ValueError(\"cents\")",
  ].join("\n")
  const proposal = baseProposal(current, {
    id,
    author: "@billing-agent",
    subject: "Retry invoice webhooks with jitter",
    intent: "Cap webhook replays at one cent while consent is already satisfied on the author's lines.",
    at: now,
    status: "testing",
    reason: "consent holds; waiting for @tester",
    affected: [],
    windowClosesAt: null,
  })
  commitProposal(current, proposal, diff)
  emit("proposal", proposal)
}

function bumpQuorum(current: World, now: number) {
  current.flags.quorum = true
  const proposal = current.proposals.find((item) => item.id === PUSH.quorum)
  if (!proposal || proposal.status !== "needs_quorum") return
  if (!proposal.approvals.includes("@nelson")) proposal.approvals.push("@nelson")
  recompute(current, proposal)
  rememberMail(current, {
    id: mid("approve-quorum-nelson"),
    handle: "@nelson",
    subject: `Re: ${proposal.subject}`,
    body: "approve — adding @sub-3 is fine.\n",
    at: now,
    inReplyTo: proposal.id,
    kind: null,
    summary: "approved adding @sub-3",
    target: proposal.id,
    human: true,
  })
  emit("proposal", proposal)
}

function objectNonce(current: World, now: number) {
  current.flags.objected = true
  const proposal = current.nonceId
    ? current.proposals.find((item) => item.id === current.nonceId)
    : undefined
  if (!proposal || proposal.status !== "window") return
  const emailId = mid("object-nonce")
  proposal.objections.push({
    by: "@sub-1",
    reason: "nonce replay belongs in auth, not the limiter",
    id: emailId,
  })
  proposal.status = "objected"
  proposal.reason = "@sub-1 objected"
  proposal.window_closes_at = null
  rememberMail(current, {
    id: emailId,
    handle: "@sub-1",
    subject: `Re: ${proposal.subject}`,
    body: trailer("nonce replay belongs in auth, not the limiter", [
      ["Kind", "object"],
      ["Author", "@sub-1"],
      ["Target", proposal.id],
      ["Reason", "nonce replay belongs in auth, not the limiter"],
      ["Sig", sig(emailId)],
    ]),
    at: now,
    inReplyTo: proposal.id,
    kind: "object",
    summary: "objected: nonce replay belongs in auth",
    target: proposal.id,
  })
  emit("proposal", proposal)
}

function passRetry(current: World, now: number) {
  current.flags.tested = true
  const proposal = current.retryId
    ? current.proposals.find((item) => item.id === current.retryId)
    : undefined
  if (!proposal || proposal.status !== "testing") return
  proposal.test_result = "pass"
  proposal.status = "qualified"
  proposal.reason = "qualified; an earlier sibling is still pending"
  const tree = fakeHash(`ptree:${proposal.id}`)
  rememberMail(current, {
    id: mid("test-retry"),
    handle: "@tester",
    subject: `Re: ${proposal.subject}`,
    body: trailer("tests passed against the proposed tree.", [
      ["Kind", "test"],
      ["Author", "@tester"],
      ["Target", proposal.id],
      ["Result", "pass"],
      ["Tree", tree],
      ["Sig", sig("test-retry")],
    ]),
    at: now,
    inReplyTo: proposal.id,
    kind: "test",
    summary: "tests passed",
    target: proposal.id,
  })
  emit("proposal", proposal)
}

function completeTask(current: World, now: number) {
  current.flags.task = true
  const task = current.tasks.find((item) => item.id === "T4")
  if (!task || task.status === "done") return
  task.status = "done"
  task.notified_at = now
  task.done_link = tipOf(current).id
  for (const sub of task.subtasks) {
    sub.status = "landed"
    if (!sub.link_id) sub.link_id = task.done_link
  }
  rememberMail(current, {
    id: mid("task-done-T4"),
    handle: "@lead",
    subject: "T4 is done",
    body: trailer("Reject replayed login nonces is done.", [
      ["Kind", "task-done"],
      ["Author", "@lead"],
      ["Task", "T4"],
      ["Tip", task.done_link ?? "none"],
      ["Sig", sig("task-done-T4")],
    ]),
    at: now,
    kind: "task-done",
    summary: "marked T4 done",
    target: "T4",
  })
  rememberMail(current, {
    id: mid("notify-T4"),
    handle: "@console",
    subject: "T4 is done",
    body: trailer("@nelson: Reject replayed login nonces landed.", [
      ["Kind", "notify"],
      ["Author", "@console"],
      ["Task", "T4"],
    ]),
    at: now + 1,
    kind: "notify",
    summary: "emailed @nelson",
    target: "T4",
    to: [addressOf("@nelson"), REPO],
  })
  emit("task", task)
}

function advanceWindow(current: World, now: number) {
  if (current.flags.landed) return
  const proposal = current.proposals.find((item) => item.id === PUSH.window)
  if (!proposal) return
  const tip = current.links[current.links.length - 1]
  if (!tip || proposal.parent !== tip.id) return
  if (proposal.status === "window" && proposal.window_closes_at != null && now >= proposal.window_closes_at) {
    proposal.status = "testing"
    proposal.reason = "window closed with no objection"
    proposal.window_closes_at = null
    current.landAfter = now + 3
    emit("proposal", proposal)
    return
  }
  if (
    proposal.status === "testing" &&
    current.landAfter != null &&
    now >= current.landAfter &&
    earliestOpen(current, proposal.parent)?.id === proposal.id
  ) {
    land(current, proposal, now)
  }
}

function land(current: World, proposal: Proposal, now: number) {
  if (current.flags.landed) return
  const parent = current.links[current.links.length - 1]
  if (!parent || proposal.parent !== parent.id) return
  current.flags.landed = true
  const diff = current.diffs[proposal.id] ?? ""
  const seq = parent.seq + 1
  const state = fakeHash(`state:land:${proposal.id}`)
  const tree = fakeHash(`tree:land:${proposal.id}`)
  const link: Link = {
    id: proposal.id,
    seq,
    author: proposal.author,
    subject: proposal.subject,
    intent: current.intents[proposal.id] ?? proposal.subject,
    state,
    tree_hash: tree,
    received_at: proposal.received_at,
    task: proposal.task,
    diffstat: diffstat(diff),
  }
  current.links.push(link)
  current.proposals = current.proposals.filter((item) => item.id !== proposal.id)
  for (const sibling of current.proposals) {
    if (sibling.parent !== parent.id) continue
    if (sibling.status === "qualified") {
      sibling.status = "superseded"
      sibling.reason = `earlier sibling #${seq} landed first`
    } else if (isOpenStatus(sibling.status)) {
      sibling.status = "stale"
      sibling.reason = "parent already linked"
    }
  }
  proposal.test_result = "pass"
  rememberMail(current, {
    id: mid("test-window-land"),
    handle: "@tester",
    subject: `Re: ${proposal.subject}`,
    body: trailer("tests passed against the proposed tree.", [
      ["Kind", "test"],
      ["Author", "@tester"],
      ["Target", proposal.id],
      ["Result", "pass"],
      ["Tree", tree],
      ["Sig", sig("test-window-land")],
    ]),
    at: now,
    inReplyTo: proposal.id,
    kind: "test",
    summary: "tests passed",
    target: proposal.id,
  })
  const landed: Activity = {
    id: mid(`landed-${seq}`),
    at: now + 1,
    kind: "link",
    from: proposal.author,
    subject: proposal.subject,
    summary: `landed as #${seq}`,
    target: proposal.id,
  }
  current.activity.unshift(landed)
  emit("chain", { tip: link })
}

function earliestOpen(current: World, parent: string): Proposal | undefined {
  return current.proposals
    .filter((item) => item.parent === parent && isOpenStatus(item.status))
    .sort((a, b) => a.received_at - b.received_at)[0]
}

function baseProposal(
  current: World,
  spec: {
    id: string
    author: string
    subject: string
    intent: string
    at: number
    status: Proposal["status"]
    reason: string
    affected: string[]
    windowClosesAt: number | null
  },
): Proposal {
  current.intents[spec.id] = spec.intent
  return {
    id: spec.id,
    parent: tipOf(current).id,
    author: spec.author,
    subject: spec.subject,
    received_at: spec.at,
    status: spec.status,
    reason: spec.reason,
    affected_owners: spec.affected,
    approvals: [],
    objections: [],
    window_closes_at: spec.windowClosesAt,
    protected_rule: null,
    quorum_needed: null,
    quorum_have: null,
    test_result: null,
    task: null,
  }
}

function commitProposal(current: World, proposal: Proposal, diff: string, extra: [string, string][] = []) {
  current.proposals.push(proposal)
  current.diffs[proposal.id] = diff
  const parent = current.links.find((link) => link.id === proposal.parent)
  rememberMail(current, {
    id: proposal.id,
    handle: proposal.author,
    subject: proposal.subject,
    body: pushBody(current.intents[proposal.id] ?? proposal.subject, [
      ["Kind", "push"],
      ["Author", proposal.author],
      ["Parent", proposal.parent],
      ["Base", parent?.state ?? "0".repeat(64)],
      ["State", fakeHash(`pstate:${proposal.id}`)],
      ["Tree", fakeHash(`ptree:${proposal.id}`)],
      ["Sig", sig(proposal.id)],
      ...extra,
    ], diff),
    at: proposal.received_at,
    kind: "push",
    summary: `proposed · ${proposal.subject}`,
    target: proposal.id,
  })
}

export function mockState(): RepoState {
  const current = getWorld()
  const tip = current.links[current.links.length - 1] ?? null
  return structuredClone({
    repo: current.repo,
    bugs_address: "bugs@gud.primitive.email",
    now: Date.now() / 1000,
    window_seconds: current.windowSeconds,
    tip,
    links: current.links,
    proposals: current.proposals,
    members: current.members,
    removed_members: current.removed,
  })
}

export function mockDiff(id: string): { id: string; diff: string } {
  const diff = getWorld().diffs[id]
  if (diff == null) throw new ApiError(404, "no diff for that message")
  return { id, diff }
}

export function mockTree(at: string | null): TreeResponse {
  const current = getWorld()
  const link = resolveLink(current, at)
  const files = Object.keys(FILES)
    .sort((a, b) => a.localeCompare(b))
    .flatMap((path) => {
      const versions = FILES[path]
      if (!versions) return []
      const lines = linesAt(versions, link.seq)
      if (!lines) return []
      const owners: Record<string, number> = {}
      for (const line of lines) owners[line.owner] = (owners[line.owner] ?? 0) + 1
      return [{ path, lines: lines.length, owners }]
    })
  return { at: link.id, files }
}

export function mockBlame(path: string, at: string | null): BlameResponse {
  const current = getWorld()
  const versions = FILES[path]
  if (!versions) throw new ApiError(404, "unknown path")
  const link = resolveLink(current, at)
  const snap = linesAt(versions, link.seq) ?? []
  const roster = membersAtSeq(link.seq)
  return {
    path,
    at: link.id,
    lines: snap.map((line, index) => ({
      n: index + 1,
      text: line.text,
      owner: line.owner,
      link_id: linkId(line.seq),
      orphaned: !roster.has(line.owner),
    })),
  }
}

export function mockTasks(): Task[] {
  return structuredClone(getWorld().tasks)
}

export function mockBugs(): BugReport[] {
  return structuredClone(getWorld().bugs).sort((a, b) => b.received_at - a.received_at)
}

export function mockReviewBug(
  id: string,
  body: { action?: string; from?: string; reason?: string },
): { id: string; task_id: string | null } {
  const current = getWorld()
  const bug = current.bugs.find((item) => item.id === id)
  if (!bug) throw new ApiError(404, "unknown bug report")
  if (bug.status !== "pending" || bug.verdict) throw new ApiError(409, "bug report is already reviewed")
  const action = body.action === "approve" || body.action === "reject" ? body.action : ""
  if (!action) throw new ApiError(400, "action must be approve or reject")
  const who = body.from?.trim() || "@nelson"
  bug.review_by = who
  bug.verdict = action
  bug.reason = body.reason?.trim() || (action === "approve" ? "Approved." : "Rejected.")
  if (action === "reject") {
    bug.status = "rejected"
    return { id: bug.id, task_id: null }
  }
  const created = mockCreateTask({
    title: bug.subject,
    description: bug.description,
    requester: who.startsWith("@") ? who : "@nelson",
  })
  const task = current.tasks.find((item) => item.id === created.id)
  if (task) task.bug = bug.id
  bug.status = "accepted"
  bug.task_id = created.id
  return { id: bug.id, task_id: created.id }
}

export function mockJoins(): JoinRequest[] {
  return structuredClone(getWorld().joins).sort((a, b) => b.received_at - a.received_at || b.id.localeCompare(a.id))
}

export function mockProposeJoin(id: string): { push_id: string } {
  const current = getWorld()
  const join = current.joins.find((item) => item.id === id)
  if (!join) throw new ApiError(404, "unknown join request")
  if (!join.valid || join.status === "rejected") throw new ApiError(409, "join request is invalid")
  if (join.status === "member") throw new ApiError(409, "already a member")
  if (join.status === "proposed") throw new ApiError(409, "membership push already exists")
  const tip = tipOf(current)
  const voters = current.members.filter((member) => member.votes)
  const needed = Math.floor(voters.length / 2) + 1
  const pushId = mid(`push-join-${current.emails.length + 1}`)
  const sponsor = join.sponsor ?? "-"
  const subject = `Add ${join.handle}`
  const intent = `Membership push. ${join.handle} joins as ${join.role}, sponsored by ${join.sponsor ?? "no one"}, citing ${join.id}.`
  const diff = [
    "diff --git a/.gud/members b/.gud/members",
    "--- a/.gud/members",
    "+++ b/.gud/members",
    "@@ -6,6 +6,7 @@",
    " @sub-1 agent sub-1@gud.primitive.email ed25519:c3ViLTEta2V5LTAwNw @nelson",
    `+${join.handle} ${join.role} ${join.address} ${join.public_key} ${sponsor}`,
    " @tester tester tester@gud.primitive.email ed25519:dGVzdGVyLWtleS0wMDU @nelson",
  ].join("\n")
  const proposal: Proposal = {
    id: pushId,
    parent: tip.id,
    author: "@console",
    subject,
    received_at: Date.now() / 1000,
    status: "needs_quorum",
    reason: `quorum 0/${needed}; @console does not vote`,
    affected_owners: ["@lead"],
    approvals: [],
    objections: [],
    window_closes_at: null,
    protected_rule: "quorum",
    quorum_needed: needed,
    quorum_have: 0,
    test_result: null,
    task: null,
  }
  current.intents[pushId] = intent
  commitProposal(current, proposal, diff, [["Join", join.id]])
  join.status = "proposed"
  join.push_id = pushId
  emit("proposal", proposal)
  return { push_id: pushId }
}

export function mockActivity(limit: number): Activity[] {
  const size = Number.isFinite(limit) && limit > 0 ? limit : 100
  return structuredClone(getWorld().activity.slice(0, size))
}

export function mockThread(id: string): EmailView[] {
  return structuredClone(
    getWorld()
      .emails.filter((email) => email.id === id || email.in_reply_to === id)
      .sort((a, b) => a.received_at - b.received_at),
  )
}

export function mockCreateTask(body: { title?: string; description?: string; requester?: string }): { id: string } {
  const current = getWorld()
  const title = body.title?.trim() ?? ""
  const description = body.description?.trim() ?? ""
  const requester = body.requester?.trim() ?? ""
  if (!title) throw new ApiError(400, "title is required")
  if (!requester.startsWith("@")) throw new ApiError(400, "requester must be a handle")
  const id = `T${current.nextTask}`
  current.nextTask += 1
  const now = Date.now() / 1000
  const task: Task = {
    id,
    title,
    description,
    requester,
    created_at: now,
    status: "open",
    subtasks: [],
    done_link: null,
    notified_at: null,
  }
  current.tasks.push(task)
  rememberMail(current, {
    id: mid(`task-${id}`),
    handle: "@console",
    subject: title,
    body: trailer(description || title, [
      ["Kind", "task"],
      ["Author", "@console"],
      ["Task", id],
      ["Requester", requester],
      ["Sig", sig(`task-${id}`)],
    ]),
    at: now,
    kind: "task",
    summary: `opened ${id} for ${requester}`,
    target: id,
  })
  return { id }
}

export function mockMembers(body: {
  action?: string
  handle?: string
  role?: string
  address?: string
  public_key?: string
  sponsor?: string
}): { push_id: string } {
  const current = getWorld()
  const action = body.action
  if (action !== "add" && action !== "remove") throw new ApiError(400, "action must be add or remove")
  const handle = normalizeHandle(body.handle ?? "")
  const tip = tipOf(current)
  const excluding = action === "remove" ? handle : undefined
  const voters = current.members.filter((member) => member.votes && member.handle !== excluding)
  const needed = Math.floor(voters.length / 2) + 1
  const id = mid(`push-mem-${current.emails.length + 1}`)
  const role = body.role?.trim() || "agent"
  const address = body.address?.trim() || addressOf(handle)
  const publicKey = body.public_key?.trim() || "ed25519:bmV3LWtleQ"
  const sponsor = body.sponsor?.trim() || "@nelson"
  const subject = action === "add" ? `Add ${handle}` : `Remove ${handle}`
  const intent =
    action === "add"
      ? `Membership push. ${handle} joins as ${role}, sponsored by ${sponsor}.`
      : `Membership push. Remove ${handle} from .gud/members.`
  const diff =
    action === "add"
      ? [
          "diff --git a/.gud/members b/.gud/members",
          "--- a/.gud/members",
          "+++ b/.gud/members",
          "@@ -6,6 +6,7 @@",
          " @sub-1 agent sub-1@gud.primitive.email ed25519:c3ViLTEta2V5LTAwNw @nelson",
          `+${handle} ${role} ${address} ${publicKey} ${sponsor}`,
          " @tester tester tester@gud.primitive.email ed25519:dGVzdGVyLWtleS0wMDU @nelson",
        ].join("\n")
      : [
          "diff --git a/.gud/members b/.gud/members",
          "--- a/.gud/members",
          "+++ b/.gud/members",
          "@@ -4,6 +4,5 @@",
          `-${handle} removed from the roster`,
        ].join("\n")
  const proposal: Proposal = {
    id,
    parent: tip.id,
    author: "@console",
    subject,
    received_at: Date.now() / 1000,
    status: "needs_quorum",
    reason: `quorum 0/${needed}; @console does not vote`,
    affected_owners: ["@lead"],
    approvals: [],
    objections: [],
    window_closes_at: null,
    protected_rule: "quorum",
    quorum_needed: needed,
    quorum_have: 0,
    test_result: null,
    task: null,
  }
  current.intents[id] = intent
  commitProposal(current, proposal, diff)
  emit("proposal", proposal)
  return { push_id: id }
}

export function mockApprove(body: { target?: string; as?: string }): { id: string } {
  const current = getWorld()
  const target = body.target ?? ""
  const as = normalizeHandle(body.as ?? "")
  const proposal = current.proposals.find((item) => item.id === target)
  if (!proposal) throw new ApiError(404, "unknown proposal")
  const member = current.members.find((item) => item.handle === as)
  if (!member) throw new ApiError(400, `${as} is not a current member`)
  if (member.role === "human") throw new ApiError(400, "use reply for a human")
  const id = mid(`approve-ui-${current.emails.length + 1}`)
  if (!proposal.approvals.includes(as)) proposal.approvals.push(as)
  if (isOpenStatus(proposal.status)) recompute(current, proposal)
  rememberMail(current, {
    id,
    handle: as,
    subject: `Re: ${proposal.subject}`,
    body: trailer("approved.", [
      ["Kind", "approve"],
      ["Author", as],
      ["Target", target],
      ["Sig", sig(id)],
    ]),
    at: Date.now() / 1000,
    inReplyTo: target,
    kind: "approve",
    summary: "approved",
    target,
  })
  emit("proposal", proposal)
  return { id }
}

export function mockReply(body: { target?: string; from?: string; text?: string }): { id: string } {
  const current = getWorld()
  const target = body.target ?? ""
  const from = body.from?.trim() || "@nelson"
  const text = body.text?.trim() ?? ""
  if (!text) throw new ApiError(400, "text is required")
  const proposal = current.proposals.find((item) => item.id === target)
  if (!proposal && !current.links.some((link) => link.id === target)) {
    throw new ApiError(404, "unknown target")
  }
  const id = mid(`reply-ui-${current.emails.length + 1}`)
  const word = text.split(/\s+/)[0]?.toLowerCase() ?? ""
  const rest = text.replace(/^\S+\s*/, "")
  rememberMail(current, {
    id,
    handle: from,
    subject: `Re: ${proposal?.subject ?? "thread"}`,
    body: text,
    at: Date.now() / 1000,
    inReplyTo: target,
    kind: null,
    summary: text.length > 80 ? `${text.slice(0, 77)}…` : text,
    target,
    human: true,
  })
  if (proposal && isOpenStatus(proposal.status)) {
    if (word === "veto") {
      proposal.status = "vetoed"
      proposal.reason = rest ? `@nelson vetoed: ${rest}` : "@nelson vetoed"
      proposal.window_closes_at = null
    } else if (word === "approve") {
      if (!proposal.approvals.includes(from)) proposal.approvals.push(from)
      recompute(current, proposal)
    } else if (word === "object") {
      const reason = rest || "objected"
      proposal.objections.push({ by: from, reason, id })
      if (proposal.status === "window" && proposal.affected_owners.includes(from)) {
        proposal.status = "objected"
        proposal.reason = `${from} objected`
        proposal.window_closes_at = null
      }
    }
    emit("proposal", proposal)
  }
  return { id }
}

function recompute(current: World, proposal: Proposal) {
  if (proposal.protected_rule === "quorum" || proposal.status === "needs_quorum") {
    const voters = new Set<string>()
    const author = current.members.find((member) => member.handle === proposal.author)
    if (author?.votes) voters.add(proposal.author)
    for (const handle of proposal.approvals) {
      const member = current.members.find((item) => item.handle === handle)
      if (member?.votes) voters.add(handle)
    }
    proposal.quorum_have = voters.size
    const needed = proposal.quorum_needed ?? 1
    if (voters.size >= needed) {
      proposal.status = "testing"
      proposal.reason = "quorum reached; waiting for @tester"
    } else {
      proposal.status = "needs_quorum"
      proposal.reason = `quorum ${voters.size}/${needed}`
    }
    return
  }
  if (proposal.protected_rule === "explicit" || proposal.status === "needs_approval") {
    const owners = proposal.affected_owners
    const satisfied =
      owners.length === 0
        ? proposal.approvals.some((handle) => handle !== proposal.author)
        : owners.every((owner) => proposal.approvals.includes(owner))
    if (satisfied) {
      proposal.status = "testing"
      proposal.reason = "approvals complete; waiting for @tester"
    }
    return
  }
  if (
    proposal.status === "window" &&
    proposal.affected_owners.length > 0 &&
    proposal.affected_owners.every((owner) => proposal.approvals.includes(owner))
  ) {
    proposal.status = "testing"
    proposal.reason = "all affected owners approved"
    proposal.window_closes_at = null
    if (proposal.id === PUSH.window) current.landAfter = Date.now() / 1000 + 3
  }
}

function resolveLink(current: World, at: string | null): Link {
  if (!at) {
    const tip = current.links[current.links.length - 1]
    if (!tip) throw new ApiError(409, "repo has no tip")
    return tip
  }
  const link = current.links.find((item) => item.id === at)
  if (!link) throw new ApiError(404, "unknown link")
  return link
}

function normalizeHandle(handle: string): string {
  const trimmed = handle.trim()
  if (!trimmed) throw new ApiError(400, "handle is required")
  return trimmed.startsWith("@") ? trimmed : `@${trimmed}`
}

function pushBody(intent: string, pairs: [string, string][], diff: string): string {
  const trailers = pairs.map(([name, value]) => `Gud-${name}: ${value}`).join("\n")
  return `${intent}\n\n${trailers}\n\n\`\`\`diff\n${diff}\n\`\`\``
}

function trailer(prose: string, pairs: [string, string][]): string {
  return `${prose}\n\n${pairs.map(([name, value]) => `Gud-${name}: ${value}`).join("\n")}`
}

function rememberMail(
  current: World,
  opts: {
    id: string
    handle: string
    subject: string
    body: string
    at: number
    inReplyTo?: string
    kind: string | null
    summary: string
    target?: string | null
    human?: boolean
    to?: string[]
    fromAddr?: string
  },
) {
  current.emails.push({
    id: opts.id,
    from: opts.fromAddr ?? addressOf(opts.handle),
    to: opts.to ?? [REPO],
    cc: opts.human ? [] : agentCc(opts.handle),
    subject: opts.subject,
    body: opts.body,
    received_at: opts.at,
    in_reply_to: opts.inReplyTo ?? null,
    sender_verified: true,
    kind: opts.kind,
  })
  current.activity.unshift({
    id: opts.id,
    at: opts.at,
    kind: opts.kind ?? "human",
    from: opts.handle,
    subject: opts.subject,
    summary: opts.summary,
    target: opts.target ?? opts.inReplyTo ?? null,
  })
}
