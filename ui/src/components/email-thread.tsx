import { AgentAvatar } from "@/components/agent-avatar"
import { KindBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { clockTime, presentEmail, relativeTime } from "@/lib/format"
import type { EmailView } from "@/lib/types"

export function EmailThread({
  emails,
  now,
  handleFor,
}: {
  emails: EmailView[]
  now: number
  handleFor: (address: string) => string
}) {
  if (emails.length === 0) {
    return <p className="px-4 py-6 text-sm text-muted-foreground">No messages in this thread.</p>
  }
  return (
    <div className="space-y-3 px-4 py-3">
      {emails.map((email) => {
        const handle = handleFor(email.from)
        const { prose, trailers } = presentEmail(email.body)
        return (
          <article key={email.id} className="rounded-lg border border-border bg-card/60 px-3 py-2.5">
            <header className="flex items-center gap-2">
              <AgentAvatar handle={handle.startsWith("@") ? handle : "@?"} />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-sm font-medium">{handle}</span>
                  <KindBadge kind={email.kind} />
                  {email.sender_verified ? (
                    <Badge variant="outline" className="border-emerald-400/30 text-[10px] text-emerald-200">
                      verified
                    </Badge>
                  ) : (
                    <Badge variant="outline" className="border-red-400/40 text-[10px] text-red-200">
                      unverified
                    </Badge>
                  )}
                </div>
                <p className="truncate font-mono text-[10px] text-muted-foreground">{email.from}</p>
              </div>
              <time
                className="ml-auto shrink-0 text-right text-[11px] text-muted-foreground"
                dateTime={new Date(email.received_at * 1000).toISOString()}
                title={clockTime(email.received_at)}
              >
                {relativeTime(email.received_at, now)}
              </time>
            </header>
            {prose ? (
              <p className="mt-2 text-[13px] leading-relaxed whitespace-pre-wrap">{prose}</p>
            ) : null}
            {trailers ? (
              <pre className="mt-2 overflow-x-auto font-mono text-[11px] leading-5 text-muted-foreground whitespace-pre">
                {trailers}
              </pre>
            ) : null}
          </article>
        )
      })}
    </div>
  )
}
