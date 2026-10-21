import { shortHash } from "@/lib/format"
import type { GudEvent } from "@/lib/types"

const s = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v))
const list = (v: unknown): string => (Array.isArray(v) ? v.map(s).join(", ") : s(v))
const h = (v: unknown): string => shortHash(s(v))

/** One line per §19.2 event, in the words of `gud.mcp.describe_event`. */
export function describeEvent(e: GudEvent): { who: string; text: string; target: string | null } {
  const d = e.data
  switch (e.type) {
    case "push.received":
      return { who: s(d.author), text: `pushed ${h(d.push)} on ${h(d.parent)} · ${list(d.files)}`, target: s(d.push) }
    case "review.requested":
      return { who: s(d.owner), text: `asked to review ${h(d.push)} (${s(d.why)})`, target: s(d.push) }
    case "objected":
      return { who: s(d.by), text: `objected to ${h(d.push)}: ${s(d.reason)}${d.evidence ? " (evidence test)" : ""}`, target: s(d.push) }
    case "commented":
      return { who: s(d.by), text: `commented on ${h(d.target)}: ${s(d.text)}`, target: s(d.target) }
    case "approved":
      return { who: s(d.by), text: `approved ${h(d.push)}`, target: s(d.push) }
    case "held":
      return { who: s(d.by), text: `held ${h(d.push)}: the window is extended once`, target: s(d.push) }
    case "test.passed":
    case "test.failed":
      return { who: s(d.tester), text: `${s(d.scope)} test ${e.type === "test.passed" ? "passed" : "failed"} on ${h(d.target)}`, target: s(d.target) }
    case "window.closing":
      return { who: "", text: `window closing on ${h(d.candidate)}`, target: s(d.candidate) }
    case "superseded":
      return { who: "", text: `${h(d.push)} superseded by link ${h(d.by_link)}`, target: s(d.push) }
    case "push.rejected":
      return { who: "", text: `${h(d.push)} rejected: ${s(d.status)}${d.reason ? ` (${s(d.reason)})` : ""}`, target: s(d.push) }
    case "carried":
      return { who: "", text: `${h(d.push)} carried onto ${h(d.onto)}`, target: s(d.push) }
    case "linked":
      return { who: s(d.author), text: `linked #${s(d.seq)} ${h(d.link)}${d.carried_from ? ` (carried from ${h(d.carried_from)})` : ""} · ${list(d.files)}`, target: s(d.link) }
    case "task.opened":
      return { who: s(d.requester), text: `opened ${s(d.alias)}: ${s(d.title)}`, target: s(d.task) }
    case "subtask.posted":
      return { who: "", text: `posted ${s(d.alias)}: ${s(d.title)}`, target: s(d.subtask) }
    case "claim.won":
      return { who: s(d.holder), text: `claimed ${h(d.task)} · ${list(d.files)}`, target: s(d.task) }
    case "claim.lost":
      return { who: "", text: `claim ${h(d.claim)} lost to ${h(d.active_claim)}`, target: s(d.task) }
    case "claim.lapsed":
      return { who: s(d.holder), text: `claim on ${h(d.task)} lapsed`, target: s(d.task) }
    case "claim.overlap":
      return { who: list(d.holders), text: `claims overlap on ${list(d.globs)}`, target: null }
    case "scope.exceeded":
      return { who: "", text: `${h(d.push)} touches ${list(d.paths)} inside claim ${h(d.claim)}`, target: s(d.push) }
    case "task.ready":
      return { who: "", text: `${h(d.task)} work done; needs ${s(d.needs)}`, target: s(d.task) }
    case "task.done":
      return { who: "", text: `${h(d.task)} done at ${h(d.link)}`, target: s(d.task) }
    case "escalated":
      return { who: list(d.deciders), text: `escalated (${s(d.reason)}) to ${list(d.deciders) || "the humans"}`, target: Array.isArray(d.pushes) ? s(d.pushes[0]) : s(d.task) || null }
    case "escalation.resolved":
      return { who: s(d.by), text: `escalation resolved: chose ${h(d.chosen)}`, target: s(d.chosen) || null }
    case "tip.red":
      return { who: "", text: `tip is red at ${h(d.red_link)} (last green ${h(d.last_green)})`, target: s(d.red_link) }
    case "tip.bisected":
      return { who: s(d.author), text: `bisect: first bad ${h(d.first_bad)}`, target: s(d.first_bad) }
    case "tip.green":
      return { who: "", text: `tip is green at ${h(d.link)}`, target: s(d.link) }
    case "fix.opened":
      return { who: s(d.offered_to), text: `fix task ${s(d.alias)} cites ${h(d.cited_link)}`, target: s(d.task) }
    case "host.fork":
      return { who: "", text: `host fork suspected (${s(d.why)})`, target: s(d.message) || null }
    default:
      return { who: "", text: e.type, target: null }
  }
}
