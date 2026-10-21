import { AgentAvatar } from "@/components/agent-avatar"
import { KindBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { clockTime, relativeTime, shortHash } from "@/lib/format"
import type { ThreadMessage } from "@/lib/types"

const SHOWN_HEADERS = ["target", "task", "revises", "scope", "result", "base-result", "on", "escalate", "verdict",
  "files", "title", "parent-task", "release", "join"]

function headerValue(value: string): string {
  return /^[0-9a-f]{64}$/.test(value) ? shortHash(value) : value
}

/** A thread from `/api/v1/view/thread/<id>`: every message in log order, with the gud headers
 * that tie it together (target, revises, task) and the evidence file of an objection. */
export function MessageThread({ messages, now, onOpen }: { messages: ThreadMessage[]; now: number; onOpen?: (id: string) => void }) {
  if (messages.length === 0) {
    return <p className="px-4 py-6 text-sm text-muted-foreground">No messages in this thread.</p>
  }
  return (
    <div className="space-y-3 px-4 py-3">
      {messages.map((m) => {
        const who = m.author ?? m.from
        const veto = m.kind === "object" && /^\s*veto\b/i.test(m.text)
        return (
          <article key={m.id} className="rounded-lg border border-border bg-card/60 px-3 py-2.5">
            <header className="flex items-center gap-2">
              <AgentAvatar handle={who.startsWith("@") ? who : "@?"} />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-sm font-medium">{who}</span>
                  <KindBadge kind={veto ? "veto" : m.kind} />
                  {m.signed ? (
                    <Badge variant="outline" className="border-emerald-400/30 text-[10px] text-emerald-200">signed</Badge>
                  ) : (
                    <Badge variant="outline" className="border-pink-400/40 text-[10px] text-pink-200">unsigned · dmarc</Badge>
                  )}
                  {m.seq != null ? <span className="font-mono text-[10px] text-muted-foreground">log #{m.seq}</span> : null}
                </div>
                <p className="truncate font-mono text-[10px] text-muted-foreground">
                  {m.from} · {shortHash(m.id)}
                </p>
              </div>
              {m.received_at ? (
                <time className="ml-auto shrink-0 text-right text-[11px] text-muted-foreground" title={clockTime(m.received_at)}>
                  {relativeTime(m.received_at, now)}
                </time>
              ) : null}
            </header>
            {m.text.trim() ? <p className="mt-2 text-[13px] leading-relaxed whitespace-pre-wrap">{m.text.trim()}</p> : null}
            {m.evidence ? (
              <p className="mt-2 rounded border border-red-400/30 bg-red-400/5 px-2 py-1 font-mono text-[11px] text-red-200">
                evidence test {m.evidence.path} · {m.evidence.size} B · {shortHash(m.evidence.sha256)}
              </p>
            ) : null}
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-muted-foreground">
              {SHOWN_HEADERS.filter((name) => m.headers[name]).map((name) => {
                const value = m.headers[name] ?? ""
                const hex = /^[0-9a-f]{64}$/.test(value)
                return (
                  <span key={name}>
                    {name}{" "}
                    {hex && onOpen ? (
                      <button type="button" className="text-foreground/80 hover:underline" onClick={() => onOpen(value)}>
                        {headerValue(value)}
                      </button>
                    ) : (
                      <span className="text-foreground/80">{headerValue(value)}</span>
                    )}
                  </span>
                )
              })}
            </div>
          </article>
        )
      })}
    </div>
  )
}
