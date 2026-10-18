import { diffstat } from "@/lib/diff"
import type {
  Activity,
  BugReport,
  EmailView,
  JoinRequest,
  Link,
  Member,
  MemberRole,
  Proposal,
  ProposalStatus,
  Task,
} from "@/lib/types"
import { FILES, linesAt } from "@/mock/files"
import { buildDiffs } from "@/mock/diffs"
import { JOIN_CODEX, joinMailBody, joinSummary, seedJoins } from "@/mock/joins"
import {
  ADDR,
  REPO,
  WINDOW_SECONDS,
  addressOf,
  agentCc,
  fakeHash,
  linkId,
  mid,
  sig,
  PUSH,
} from "@/mock/ids"

export type World = {
  repo: string
  windowSeconds: number
  links: Link[]
  proposals: Proposal[]
  members: Member[]
  removed: Member[]
  joins: JoinRequest[]
  tasks: Task[]
  bugs: BugReport[]
  emails: EmailView[]
  diffs: Record<string, string>
  activity: Activity[]
  intents: Record<string, string>
  nextTask: number
  t0: number
  flags: {
    nonce: boolean
    retry: boolean
    quorum: boolean
    objected: boolean
    tested: boolean
    task: boolean
    landed: boolean
    gemini: boolean
    geminiLanded: boolean
  }
  nonceId: string | null
  retryId: string | null
  /** When the gallery window proposal may be tested and landed. */
  landAfter: number | null
}

const ZERO = "0".repeat(64)

type LinkSpec = {
  seq: number
  author: string
  subject: string
  intent: string
  task: string | null
  ago: number
}

type ProposalSpec = {
  id: string
  parentSeq: number
  author: string
  subject: string
  intent: string
  ago: number
  status: ProposalStatus
  reason: string
  affected: string[]
  approvals: string[]
  objections: { by: string; reason: string; slug: string }[]
  window: boolean
  rule: "explicit" | "quorum" | null
  quorumHave: number | null
  quorumNeeded: number | null
  test: "pass" | "fail" | null
  badSig?: boolean
  joinId?: string
}

function lineCounts(seq: number): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const versions of Object.values(FILES)) {
    const lines = linesAt(versions, seq)
    if (!lines) continue
    for (const line of lines) counts[line.owner] = (counts[line.owner] ?? 0) + 1
  }
  return counts
}

function pushBody(intent: string, pairs: [string, string][], diff: string): string {
  const trailers = pairs.map(([name, value]) => `Gud-${name}: ${value}`).join("\n")
  return `${intent}\n\n${trailers}\n\n\`\`\`diff\n${diff}\n\`\`\``
}

function trailerBody(prose: string, pairs: [string, string][]): string {
  return `${prose}\n\n${pairs.map(([name, value]) => `Gud-${name}: ${value}`).join("\n")}`
}

export function createWorld(now: number): World {
  const diffs = buildDiffs()
  const emails: EmailView[] = []
  const activity: Activity[] = []
  const intents: Record<string, string> = {}

  const linkSpecs: LinkSpec[] = [
    {
      seq: 1,
      author: "@lead",
      subject: "Initialize the billing service",
      intent:
        "Genesis tree for repo@gud.primitive.email. Members, protected paths, and stubs for auth, billing, and HTTP.",
      task: null,
      ago: 6 * 3600,
    },
    {
      seq: 2,
      author: "@auth-agent",
      subject: "Add password login",
      intent: "Replace the auth stub with PBKDF2 password hashes and a login() that issues a bearer token.",
      task: "T1.1",
      ago: 5.2 * 3600,
    },
    {
      seq: 3,
      author: "@billing-agent",
      subject: "Record usage events",
      intent: "Append billable usage onto an in-process ledger. Negative amounts are rejected.",
      task: "T2.1",
      ago: 4.4 * 3600,
    },
    {
      seq: 4,
      author: "@api-agent",
      subject: "Expose POST /v1/login",
      intent: "Route /v1/login through auth.login and map AuthError to 401.",
      task: null,
      ago: 3.7 * 3600,
    },
    {
      seq: 5,
      author: "@auth-agent",
      subject: "Hash passwords with scrypt",
      intent: "Swap PBKDF2 for scrypt and compare digests in constant time.",
      task: "T1.2",
      ago: 3.1 * 3600,
    },
    {
      seq: 6,
      author: "@billing-agent",
      subject: "Roll usage into monthly invoices",
      intent: "Sum the ledger per user. The grace window is the caller's concern.",
      task: "T2.2",
      ago: 2.5 * 3600,
    },
    {
      seq: 7,
      author: "@lead",
      subject: "Admit @sub-1 and @sub-2",
      intent: "Membership push. @sub-1 and @sub-2 join the swarm, sponsored by @nelson.",
      task: null,
      ago: 2 * 3600,
    },
    {
      seq: 8,
      author: "@sub-1",
      subject: "Rate-limit login per user",
      intent: "Fixed window of 60s. auth.login calls allow() and raises RateLimited past the burst.",
      task: "T3.1",
      ago: 90 * 60,
    },
    {
      seq: 9,
      author: "@sub-2",
      subject: "Bill overage when a user is throttled",
      intent: "Each blocked login past the grace window appends an overage line at 2 cents.",
      task: "T3.2",
      ago: 70 * 60,
    },
    {
      seq: 10,
      author: "@api-agent",
      subject: "Return 429 and Retry-After",
      intent: "Map RateLimited to 429 with retry_after=60. Add GET-style /v1/invoice.",
      task: "T3.3",
      ago: 50 * 60,
    },
    {
      seq: 11,
      author: "@auth-agent",
      subject: "Exempt service accounts from the limiter",
      intent: "user ids with the svc_ prefix skip the login bucket and still receive a token.",
      task: null,
      ago: 35 * 60,
    },
    {
      seq: 12,
      author: "@lead",
      subject: "Remove @sub-2 from members",
      intent:
        "Membership push. @sub-2 is off the swarm. Lines they wrote stay in the tree and show up as orphaned.",
      task: null,
      ago: 18 * 60,
    },
  ]

  const links: Link[] = []
  let parentState = ZERO
  for (const spec of linkSpecs) {
    const id = linkId(spec.seq)
    const diff = diffs[id] ?? ""
    const state = fakeHash(`state:${spec.seq}`)
    const tree = fakeHash(`tree:${spec.seq}`)
    const received = now - spec.ago
    const link: Link = {
      id,
      seq: spec.seq,
      author: spec.author,
      subject: spec.subject,
      intent: spec.intent,
      state,
      tree_hash: tree,
      received_at: received,
      task: spec.task,
      diffstat: diffstat(diff),
    }
    links.push(link)
    intents[id] = spec.intent

    const pairs: [string, string][] = [
      ["Kind", "push"],
      ["Author", spec.author],
      ["Parent", spec.seq === 1 ? "none" : linkId(spec.seq - 1)],
      ["Base", parentState],
      ["State", state],
      ["Tree", tree],
    ]
    if (spec.task) pairs.push(["Task", spec.task])
    pairs.push(["Sig", sig(id)])
    addMail(emails, activity, {
      id,
      handle: spec.author,
      subject: spec.subject,
      body: pushBody(spec.intent, pairs, diff),
      at: received,
      kind: "push",
      summary: `pushed ${specFiles(diff)}`,
      target: id,
    })

    if (spec.seq === 7) {
      addApprovals(emails, activity, id, ["@nelson", "@auth-agent"], received, spec.subject)
    }
    if (spec.seq === 12) {
      addApprovals(
        emails,
        activity,
        id,
        ["@nelson", "@api-agent", "@billing-agent"],
        received,
        spec.subject,
      )
    }

    const testAt = received + 40
    addMail(emails, activity, {
      id: mid(`test-${spec.seq}`),
      handle: "@tester",
      subject: `Re: ${spec.subject}`,
      body: trailerBody("tests passed against the proposed tree.", [
        ["Kind", "test"],
        ["Author", "@tester"],
        ["Target", id],
        ["Result", "pass"],
        ["Tree", tree],
        ["Sig", sig(`test-${spec.seq}`)],
      ]),
      at: testAt,
      inReplyTo: id,
      kind: "test",
      summary: "tests passed",
      target: id,
    })
    activity.push({
      id: mid(`landed-${spec.seq}`),
      at: testAt + 1,
      kind: "link",
      from: spec.author,
      subject: spec.subject,
      summary: `landed as #${spec.seq}`,
      target: id,
    })
    parentState = state
  }

  const proposalSpecs: ProposalSpec[] = [
    {
      id: PUSH.window,
      parentSeq: 12,
      author: "@sub-1",
      subject: "Tighten the overage grace period",
      intent: "Cut GRACE_SECONDS from one hour to thirty minutes so throttle overage starts sooner.",
      ago: 12,
      status: "window",
      reason: "lazy consensus is open",
      affected: ["@billing-agent"],
      approvals: [],
      objections: [],
      window: true,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: null,
    },
    {
      id: PUSH.approval,
      parentSeq: 12,
      author: "@billing-agent",
      subject: "Log auth failures into the billing audit trail",
      intent: "auth.py is explicit-protected. Record a zero-cent auth_failure line when verify_password fails.",
      ago: 10,
      status: "needs_approval",
      reason: "explicit rule on auth.py; waiting on @auth-agent",
      affected: ["@auth-agent", "@sub-1"],
      approvals: ["@sub-1"],
      objections: [],
      window: false,
      rule: "explicit",
      quorumHave: null,
      quorumNeeded: null,
      test: null,
    },
    {
      id: PUSH.quorum,
      parentSeq: 12,
      author: "@lead",
      subject: "Add @sub-3 to the swarm",
      intent: "Membership push. @sub-3 joins as an agent, sponsored by @nelson. Lands by quorum.",
      ago: 8,
      status: "needs_quorum",
      reason: "quorum 2/4, counting the author's vote",
      affected: ["@lead"],
      approvals: ["@api-agent"],
      objections: [],
      window: false,
      rule: "quorum",
      quorumHave: 2,
      quorumNeeded: 4,
      test: null,
    },
    {
      id: PUSH.join,
      parentSeq: 12,
      author: "@console",
      subject: "Add @codex-agent",
      intent: "Membership push citing the join from @codex-agent. Lands by quorum.",
      ago: 90,
      status: "needs_quorum",
      reason: "quorum 3/5",
      affected: ["@lead"],
      approvals: ["@lead", "@auth-agent", "@nelson"],
      objections: [],
      window: false,
      rule: "quorum",
      quorumHave: 3,
      quorumNeeded: 5,
      test: null,
      joinId: JOIN_CODEX,
    },
    {
      id: PUSH.testing,
      parentSeq: 12,
      author: "@api-agent",
      subject: "Stamp X-Request-Id on handler logs",
      intent: "Consent already holds (only the author's lines). Waiting on @tester.",
      ago: 6,
      status: "testing",
      reason: "consent holds; waiting for @tester",
      affected: [],
      approvals: [],
      objections: [],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: null,
    },
    {
      id: PUSH.qualified,
      parentSeq: 12,
      author: "@billing-agent",
      subject: "Round overage up to the cent",
      intent: "Tests passed and consent holds, but an earlier sibling of this parent is still in its window.",
      ago: 4,
      status: "qualified",
      reason: "qualified; an earlier sibling is still pending",
      affected: [],
      approvals: [],
      objections: [],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: "pass",
    },
    {
      id: PUSH.objected,
      parentSeq: 11,
      author: "@api-agent",
      subject: "Raise the login burst to 100/min",
      intent: "Default the limiter to 100. @sub-1 owns allow() and objected inside the window.",
      ago: 34 * 60,
      status: "objected",
      reason: "@sub-1 objected",
      affected: ["@sub-1"],
      approvals: [],
      objections: [
        {
          by: "@sub-1",
          reason: "100/min will fill the session table",
          slug: "object-burst",
        },
      ],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: null,
    },
    {
      id: PUSH.vetoed,
      parentSeq: 10,
      author: "@billing-agent",
      subject: "Waive overage charges behind a flag",
      intent: "Short-circuit charge_overage when WAIVE_OVERAGE is set.",
      ago: 49 * 60,
      status: "vetoed",
      reason: "@nelson vetoed: do not waive revenue",
      affected: ["@sub-2"],
      approvals: [],
      objections: [],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: null,
    },
    {
      id: PUSH.failed,
      parentSeq: 11,
      author: "@api-agent",
      subject: "Return 204 when the limiter trips",
      intent: "Treat throttle as an empty success. The suite disagreed.",
      ago: 32 * 60,
      status: "tests_failed",
      reason: "test_login_throttle: expected 429, got 204",
      affected: [],
      approvals: [],
      objections: [],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: "fail",
    },
    {
      id: PUSH.superseded,
      parentSeq: 11,
      author: "@billing-agent",
      subject: "Prorate overage to the cent",
      intent: "Scale the overage by the remaining grace window. Qualified after #12 had already landed.",
      ago: 17 * 60,
      status: "superseded",
      reason: "earlier sibling #12 landed first",
      affected: [],
      approvals: [],
      objections: [],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: "pass",
    },
    {
      id: PUSH.stale,
      parentSeq: 8,
      author: "@auth-agent",
      subject: "Cache scrypt results for the request",
      intent: "Memoize hash_password. The parent already had a successor by the time this was read.",
      ago: 80 * 60,
      status: "stale",
      reason: "parent already has a successor",
      affected: ["@auth-agent"],
      approvals: [],
      objections: [],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: null,
    },
    {
      id: PUSH.conflict,
      parentSeq: 9,
      author: "@sub-1",
      subject: "Rename allow() to permit()",
      intent: "The hunk context no longer matches ratelimit.py.",
      ago: 65 * 60,
      status: "conflict",
      reason: "hunk failed at ratelimit.py:12",
      affected: [],
      approvals: [],
      objections: [],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: null,
    },
    {
      id: PUSH.badsig,
      parentSeq: 10,
      author: "@billing-agent",
      subject: "Adjust invoice rounding",
      intent: "Optional note on record(). The signature does not verify under the parent's key.",
      ago: 48 * 60,
      status: "bad_sig",
      reason: "ed25519 signature did not verify for @billing-agent",
      affected: [],
      approvals: [],
      objections: [],
      window: false,
      rule: null,
      quorumHave: null,
      quorumNeeded: null,
      test: null,
      badSig: true,
    },
  ]

  const proposals: Proposal[] = proposalSpecs.map((spec) => {
    const parent = linkId(spec.parentSeq)
    const parentLink = links.find((link) => link.id === parent)
    const received = now - spec.ago
    const diff = diffs[spec.id] ?? ""
    intents[spec.id] = spec.intent
    const state = fakeHash(`pstate:${spec.id}`)
    const tree = fakeHash(`ptree:${spec.id}`)
    const proposal: Proposal = {
      id: spec.id,
      parent,
      author: spec.author,
      subject: spec.subject,
      received_at: received,
      status: spec.status,
      reason: spec.reason,
      affected_owners: spec.affected,
      approvals: spec.approvals,
      objections: spec.objections.map((obj) => ({
        by: obj.by,
        reason: obj.reason,
        id: mid(obj.slug),
      })),
      window_closes_at: spec.window ? received + WINDOW_SECONDS : null,
      protected_rule: spec.rule,
      quorum_needed: spec.quorumNeeded,
      quorum_have: spec.quorumHave,
      test_result: spec.test,
      task: null,
    }
    const pairs: [string, string][] = [
      ["Kind", "push"],
      ["Author", spec.author],
      ["Parent", parent],
      ["Base", parentLink?.state ?? ZERO],
      ["State", state],
      ["Tree", tree],
      ["Sig", spec.badSig ? "ed25519:bm90LWEtcmVhbC1zaWduYXR1cmU" : sig(spec.id)],
    ]
    if (spec.joinId) pairs.push(["Join", spec.joinId])
    addMail(emails, activity, {
      id: spec.id,
      handle: spec.author,
      subject: spec.subject,
      body: pushBody(spec.intent, pairs, diff),
      at: received,
      kind: "push",
      summary: `proposed · ${spec.status.replaceAll("_", " ")}`,
      target: spec.id,
    })
    for (const handle of spec.approvals) {
      addMail(emails, activity, {
        id: mid(`approve-${spec.id.slice(1, 12)}-${handle.replace("@", "")}`),
        handle,
        subject: `Re: ${spec.subject}`,
        body: trailerBody("approved.", [
          ["Kind", "approve"],
          ["Author", handle],
          ["Target", spec.id],
          ["Sig", sig(`approve-${spec.id}-${handle}`)],
        ]),
        at: received + 2,
        inReplyTo: spec.id,
        kind: "approve",
        summary: "approved",
        target: spec.id,
      })
    }
    for (const obj of spec.objections) {
      addMail(emails, activity, {
        id: mid(obj.slug),
        handle: obj.by,
        subject: `Re: ${spec.subject}`,
        body: trailerBody(obj.reason, [
          ["Kind", "object"],
          ["Author", obj.by],
          ["Target", spec.id],
          ["Reason", obj.reason],
          ["Sig", sig(obj.slug)],
        ]),
        at: received + 5,
        inReplyTo: spec.id,
        kind: "object",
        summary: obj.reason,
        target: spec.id,
      })
    }
    if (spec.status === "vetoed") {
      addMail(emails, activity, {
        id: mid("veto-waive"),
        handle: "@nelson",
        subject: `Re: ${spec.subject}`,
        body: "veto this waives revenue. Ship the charge, or don't ship the flag.",
        at: received + 8,
        inReplyTo: spec.id,
        kind: null,
        summary: "vetoed the waive-overage flag",
        target: spec.id,
        human: true,
      })
    }
    if (spec.test) {
      const pass = spec.test === "pass"
      addMail(emails, activity, {
        id: mid(`test-${spec.id.slice(1, 14)}`),
        handle: "@tester",
        subject: `Re: ${spec.subject}`,
        body: trailerBody(
          pass ? "tests passed against the proposed tree." : "test_login_throttle failed: expected 429, got 204.",
          [
            ["Kind", "test"],
            ["Author", "@tester"],
            ["Target", spec.id],
            ["Result", spec.test],
            ["Tree", tree],
            ["Sig", sig(`test-${spec.id}`)],
          ],
        ),
        at: received + 12,
        inReplyTo: spec.id,
        kind: "test",
        summary: pass ? "tests passed" : "tests failed: expected 429, got 204",
        target: spec.id,
      })
    }
    return proposal
  })

  addMail(emails, activity, {
    id: mid("human-login-shape"),
    handle: "@nelson",
    subject: "Re: Expose POST /v1/login",
    body: "please keep the 401 body stable. clients already key off error=unauthorized.",
    at: now - 3.6 * 3600,
    inReplyTo: linkId(4),
    kind: null,
    summary: "asked to keep the 401 body stable",
    target: linkId(4),
    human: true,
  })

  const tasks = buildTasks(now)
  addTaskMail(emails, activity, tasks)

  const joins = seedJoins(now)
  for (const join of joins) {
    addMail(emails, activity, {
      id: join.id,
      handle: join.handle,
      fromAddr: join.address,
      subject: `Join ${join.handle}`,
      body: joinMailBody(join),
      at: join.received_at,
      kind: "join",
      summary: joinSummary(join),
      target: join.id,
    })
  }

  activity.sort((a, b) => b.at - a.at || a.id.localeCompare(b.id))

  const counts = lineCounts(12)
  const members = buildMembers(counts)
  const removed = members.filter((member) => member.handle === "@sub-2")
  const active = members.filter((member) => member.handle !== "@sub-2")

  return {
    repo: REPO,
    windowSeconds: WINDOW_SECONDS,
    links,
    proposals,
    members: active,
    removed,
    joins,
    tasks,
    bugs: [
      {
        id: mid("bug-pricing-table"),
        from: "sam@acme.example",
        subject: "Pricing page has no comparison table",
        description: "Pro should be $8/mo and Team $15/user/mo, in a comparison table. The page is three cards.",
        received_at: now - 3600,
        status: "pending",
        task_id: null,
        verdict: null,
        review_by: null,
        reason: "",
      },
    ],
    emails,
    diffs,
    activity,
    intents,
    nextTask: 6,
    t0: now,
    flags: {
      nonce: false,
      retry: false,
      quorum: false,
      objected: false,
      tested: false,
      task: false,
      landed: false,
      gemini: false,
      geminiLanded: false,
    },
    nonceId: null,
    retryId: null,
    landAfter: null,
  }
}

function specFiles(diff: string): string {
  const files = diffstat(diff).files
  if (files.length === 0) return "a diff"
  if (files.length === 1) return files[0] ?? "a diff"
  return `${files.length} files`
}

function addApprovals(
  emails: EmailView[],
  activity: Activity[],
  target: string,
  handles: string[],
  received: number,
  subject: string,
) {
  handles.forEach((handle, index) => {
    addMail(emails, activity, {
      id: mid(`approve-${target.replace(/[<>@.]/g, "").slice(0, 18)}-${handle.replace("@", "")}`),
      handle,
      subject: `Re: ${subject}`,
      body: trailerBody("approved.", [
        ["Kind", "approve"],
        ["Author", handle],
        ["Target", target],
        ["Sig", sig(`approve-${target}-${handle}`)],
      ]),
      at: received + 12 + index * 4,
      inReplyTo: target,
      kind: "approve",
      summary: "approved",
      target,
    })
  })
}

type MailOpts = {
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
}

function addMail(emails: EmailView[], activity: Activity[], opts: MailOpts) {
  emails.push({
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
  activity.push({
    id: opts.id,
    at: opts.at,
    kind: opts.kind ?? "human",
    from: opts.handle,
    subject: opts.subject,
    summary: opts.summary,
    target: opts.target ?? opts.inReplyTo ?? null,
  })
}

function buildMembers(counts: Record<string, number>): Member[] {
  const rows: { handle: string; role: MemberRole; sponsor: string | null; added: number }[] = [
    { handle: "@lead", role: "lead", sponsor: "@nelson", added: 1 },
    { handle: "@auth-agent", role: "agent", sponsor: "@nelson", added: 1 },
    { handle: "@billing-agent", role: "agent", sponsor: "@nelson", added: 1 },
    { handle: "@api-agent", role: "agent", sponsor: "@nelson", added: 1 },
    { handle: "@sub-1", role: "agent", sponsor: "@nelson", added: 7 },
    { handle: "@sub-2", role: "agent", sponsor: "@nelson", added: 7 },
    { handle: "@tester", role: "tester", sponsor: "@nelson", added: 1 },
    { handle: "@console", role: "console", sponsor: "@nelson", added: 1 },
    { handle: "@nelson", role: "human", sponsor: null, added: 1 },
  ]
  return rows.map((row) => ({
    handle: row.handle,
    role: row.role,
    address: ADDR[row.handle] ?? addressOf(row.handle),
    fingerprint: row.role === "human" ? null : fakeHash(`fp:${row.handle}`).slice(0, 16),
    sponsor: row.sponsor,
    votes: row.role === "agent" || row.role === "lead" || row.role === "human",
    lines_owned: counts[row.handle] ?? 0,
    added_by_link: linkId(row.added),
  }))
}

function buildTasks(now: number): Task[] {
  return [
    {
      id: "T1",
      title: "Add password login",
      description: "Replace the auth stub with a real password check and a session token.",
      requester: "@nelson",
      created_at: now - 5.6 * 3600,
      status: "done",
      subtasks: [
        {
          id: "T1.1",
          assignee: "@auth-agent",
          title: "Add password login",
          status: "landed",
          link_id: linkId(2),
        },
        {
          id: "T1.2",
          assignee: "@auth-agent",
          title: "Hash passwords with scrypt",
          status: "landed",
          link_id: linkId(5),
        },
      ],
      done_link: linkId(5),
      notified_at: now - 3 * 3600,
    },
    {
      id: "T2",
      title: "Track billable usage",
      description: "Record usage events and roll them into a per-user invoice total.",
      requester: "@nelson",
      created_at: now - 4.6 * 3600,
      status: "done",
      subtasks: [
        {
          id: "T2.1",
          assignee: "@billing-agent",
          title: "Record usage events",
          status: "landed",
          link_id: linkId(3),
        },
        {
          id: "T2.2",
          assignee: "@billing-agent",
          title: "Roll usage into monthly invoices",
          status: "landed",
          link_id: linkId(6),
        },
      ],
      done_link: linkId(6),
      notified_at: now - 2.4 * 3600,
    },
    {
      id: "T3",
      title: "Add per-user rate limiting to login and bill overage",
      description:
        "Login gets a per-user fixed window. Throttled requests past the grace window become overage on the invoice, and the API returns 429.",
      requester: "@nelson",
      created_at: now - 110 * 60,
      status: "done",
      subtasks: [
        {
          id: "T3.1",
          assignee: "@sub-1",
          title: "Rate-limit login per user",
          status: "landed",
          link_id: linkId(8),
        },
        {
          id: "T3.2",
          assignee: "@sub-2",
          title: "Bill overage when a user is throttled",
          status: "landed",
          link_id: linkId(9),
        },
        {
          id: "T3.3",
          assignee: "@api-agent",
          title: "Return 429 and Retry-After",
          status: "landed",
          link_id: linkId(10),
        },
      ],
      done_link: linkId(10),
      notified_at: now - 45 * 60,
    },
    {
      id: "T4",
      title: "Reject replayed login nonces",
      description: "Session tokens should carry a nonce window so a replayed login body cannot mint a second token.",
      requester: "@nelson",
      created_at: now - 25 * 60,
      status: "in_progress",
      subtasks: [
        {
          id: "T4.1",
          assignee: "@auth-agent",
          title: "Track a nonce window on session tokens",
          status: "open",
          link_id: null,
        },
        {
          id: "T4.2",
          assignee: "@api-agent",
          title: "Surface nonce mismatches as 401",
          status: "proposed",
          link_id: null,
        },
      ],
      done_link: null,
      notified_at: null,
    },
    {
      id: "T5",
      title: "Document the limiter for support",
      description: "Write the runbook for 429s, the grace window, and overage lines.",
      requester: "@nelson",
      created_at: now - 8 * 60,
      status: "open",
      subtasks: [
        {
          id: "T5.1",
          assignee: "@sub-1",
          title: "Write the runbook",
          status: "open",
          link_id: null,
        },
      ],
      done_link: null,
      notified_at: null,
    },
  ]
}

function addTaskMail(emails: EmailView[], activity: Activity[], tasks: Task[]) {
  for (const task of tasks) {
    addMail(emails, activity, {
      id: mid(`task-${task.id}`),
      handle: "@console",
      subject: task.title,
      body: trailerBody(task.description, [
        ["Kind", "task"],
        ["Author", "@console"],
        ["Task", task.id],
        ["Requester", task.requester],
        ["Sig", sig(`task-${task.id}`)],
      ]),
      at: task.created_at,
      kind: "task",
      summary: `opened ${task.id} for ${task.requester}`,
      target: task.id,
    })
    task.subtasks.forEach((sub, index) => {
      addMail(emails, activity, {
        id: mid(`subtask-${sub.id}`),
        handle: "@lead",
        subject: sub.title,
        body: trailerBody(sub.title, [
          ["Kind", "subtask"],
          ["Author", "@lead"],
          ["Task", sub.id],
          ["Parent-Task", task.id],
          ["Assignee", sub.assignee],
          ["Sig", sig(`subtask-${sub.id}`)],
        ]),
        at: task.created_at + 30 + index * 15,
        kind: "subtask",
        summary: `assigned ${sub.id} to ${sub.assignee}`,
        target: sub.id,
      })
    })
    if (task.status === "done" && task.notified_at) {
      addMail(emails, activity, {
        id: mid(`task-done-${task.id}`),
        handle: "@lead",
        subject: `${task.id} is done`,
        body: trailerBody(`${task.title} is done.`, [
          ["Kind", "task-done"],
          ["Author", "@lead"],
          ["Task", task.id],
          ["Tip", linkId(task.id === "T1" ? 5 : task.id === "T2" ? 6 : 10)],
          ["Sig", sig(`done-${task.id}`)],
        ]),
        at: task.notified_at - 5,
        kind: "task-done",
        summary: `marked ${task.id} done`,
        target: task.id,
      })
      addMail(emails, activity, {
        id: mid(`notify-${task.id}`),
        handle: "@console",
        subject: `${task.id} is done`,
        body: trailerBody(`${task.requester}: ${task.title} landed.`, [
          ["Kind", "notify"],
          ["Author", "@console"],
          ["Task", task.id],
        ]),
        at: task.notified_at,
        kind: "notify",
        summary: `emailed ${task.requester}`,
        target: task.id,
        to: [addressOf(task.requester), REPO],
      })
    }
  }
}
