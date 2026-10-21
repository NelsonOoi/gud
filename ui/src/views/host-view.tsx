import { ShieldAlert, ShieldCheck } from "lucide-react"
import { AgentAvatar } from "@/components/agent-avatar"
import { KindBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { useGud } from "@/hooks/use-gud"
import { clockTime, countdown, relativeTime, shortHash, shortKey } from "@/lib/format"
import { cn } from "@/lib/utils"

/** The host log (§11) and what this host's own self-audit says (§11.4–11.5, `gud audit`), plus the
 * delivery registrations (§19.3): which agents are connected by webhook or SSE. */
export function HostPanel() {
  const { host, state, now, setOpenId } = useGud()
  if (!host) return <p className="p-4 text-sm text-muted-foreground">Loading the host log…</p>
  const a = host.audit
  const connected = host.deliveries.filter((d) => d.channel !== "email")
  return (
    <ScrollArea className="h-full">
      <div className="mx-auto flex max-w-4xl flex-col gap-4 px-4 py-4">
        <div className="grid gap-3 md:grid-cols-2">
          <Card size="sm">
            <CardContent className="space-y-1.5 text-[12px]">
              <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Host log</h2>
              <p>
                head <span className="font-mono">#{host.head?.seq}</span> <span className="font-mono text-muted-foreground">{shortHash(host.head?.hash)}</span>{" "}
                · {host.head ? relativeTime(host.head.time, now) : "—"}
              </p>
              <p className="text-muted-foreground">
                {host.entries} entries · {host.counts.mail ?? 0} mail · {host.counts.tick ?? 0} ticks
                {host.counts.handover ? ` · ${host.counts.handover} handovers` : ""} · replayed to #{host.processed_seq}
              </p>
              <p className="font-mono text-[11px] text-muted-foreground">key {shortKey(host.key)} · {host.repos.join(" → ")}</p>
              {host.next_wakeup ? (
                <p className="text-muted-foreground">next timer {now < host.next_wakeup ? `in ${countdown(host.next_wakeup, now)}` : "due"}</p>
              ) : null}
              {host.handed_over ? <p className="text-orange-200">Handed over: this host logs nothing more.</p> : null}
              {host.missing_mail.length ? <p className="text-red-300">{host.missing_mail.length} logged messages missing: replay waits before them.</p> : null}
              {host.last_error ? <p className="text-red-300">{host.last_error}</p> : null}
            </CardContent>
          </Card>
          <Card size="sm" className={cn(a.ok === false && "border-red-400/40")}>
            <CardContent className="space-y-1.5 text-[12px]">
              <h2 className="flex items-center gap-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                {a.ok ? <ShieldCheck className="size-3.5 text-emerald-300" /> : <ShieldAlert className="size-3.5 text-red-300" />}
                Self-audit
              </h2>
              <p className={a.ok ? "text-emerald-200" : "text-red-200"}>
                {a.ok ? "clean" : `exit ${a.exit_status}`}: log {a.log_ok ? "verifies" : "does not verify"}, {a.mail_entries ?? 0} messages checked
              </p>
              <p className="text-muted-foreground">
                Gud-Seen mismatches {a.seen_mismatches} · fork proofs {a.fork_proofs} · missing {a.missing_mail} · altered {a.altered} · late votes {a.late_votes}
              </p>
              {a.log_error ? <p className="text-red-300">{a.log_error}</p> : null}
              <p className="text-[11px] text-muted-foreground">
                Members run <code>gud audit</code> against the mirror with their own retained entries; this is the host checking its own store.
              </p>
            </CardContent>
          </Card>
        </div>
        <Card size="sm">
          <CardContent className="space-y-2">
            <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Agents connected</h2>
            {connected.length === 0 ? (
              <p className="text-[12px] text-muted-foreground">
                No webhook or SSE registrations: every member gets events by email (notify). Agents register with <code>gud delivery --webhook URL</code> or <code>--sse</code>.
              </p>
            ) : (
              <ul className="space-y-1">
                {connected.map((d) => (
                  <li key={d.handle} className="flex items-center gap-2 text-[12px]">
                    <AgentAvatar handle={d.handle} className="size-5" />
                    <span className="font-medium">{d.handle}</span>
                    <Badge variant="outline" className="text-[10px] uppercase">{d.channel}</Badge>
                    {d.url ? <span className="truncate font-mono text-[11px] text-muted-foreground">{d.url}</span> : null}
                    <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                      delivered {d.acked ?? 0}/{d.journal_head ?? 0}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <p className="text-[10px] text-muted-foreground">
              intake: {Object.entries(host.intake).map(([k, v]) => `${k} ${v}`).join(" · ")}
              {" — "}dispatch: {Object.entries(host.dispatch).map(([k, v]) => `${k} ${v}`).join(" · ")}
            </p>
          </CardContent>
        </Card>
        <section>
          <h2 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">Latest entries</h2>
          <ul className="divide-y divide-border/60 rounded-md border border-border">
            {host.tail.map((e) => (
              <li key={e.seq}>
                <button
                  type="button"
                  disabled={!e.id}
                  onClick={() => e.id && setOpenId(e.id)}
                  className={cn("flex w-full items-center gap-3 px-3 py-1.5 text-left font-mono text-[11px]", e.id && "hover:bg-muted/40")}
                >
                  <span className="w-10 shrink-0 text-muted-foreground">#{e.seq}</span>
                  <KindBadge kind={e.type === "mail" ? e.kind ?? "mail" : e.type} />
                  <span className="w-28 shrink-0 truncate">{e.author ?? ""}</span>
                  <span className="min-w-0 flex-1 truncate text-muted-foreground">
                    {e.type === "mail" ? `${shortHash(e.id)} ${e.summary ?? ""}` : e.fields ? Object.entries(e.fields).map(([k, v]) => `${k} ${v.length > 40 ? shortKey(v) : v}`).join(" ") : ""}
                  </span>
                  <time className="shrink-0 text-muted-foreground" title={clockTime(e.time)}>{relativeTime(e.time, now)}</time>
                </button>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[10px] text-muted-foreground">
            Received time is the log time. Replay time {state ? clockTime(state.t) : "—"}: decisions use only entries up to the log head (§11.3).
          </p>
        </section>
      </div>
    </ScrollArea>
  )
}
