import { OctagonAlert } from "lucide-react"
import { useGud } from "@/hooks/use-gud"
import { countdown, relativeTime, shortHash } from "@/lib/format"

/** §15.4–15.5: the chain is red. Names the red link, the last green link, `first_bad` once bisect
 * finds it, and the derived fix task with its offer window. */
export function RedBanner() {
  const { state, now, setOpenId } = useGud()
  const red = state?.red
  if (!red?.red) return null
  const fix = red.fix_task
  return (
    <div className="flex items-start gap-3 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2.5 text-[12px]">
      <OctagonAlert className="mt-0.5 size-4 shrink-0 text-red-300" />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm font-medium text-red-100">
          Tip is red{red.red_since ? ` since ${relativeTime(red.red_since, now)}` : ""}. Pushes keep linking; tasks cannot finish.
        </p>
        <p className="font-mono text-[11px] text-red-200/90">
          {red.red_link ? (
            <button type="button" className="hover:underline" onClick={() => setOpenId(red.red_link)}>
              red link #{red.red_link_seq} {shortHash(red.red_link)}
            </button>
          ) : null}
          {red.last_green ? (
            <>
              {" · "}
              <button type="button" className="hover:underline" onClick={() => setOpenId(red.last_green)}>
                last green #{red.last_green_seq}
              </button>
            </>
          ) : " · no green link yet"}
          {" · "}
          {red.first_bad ? (
            <button type="button" className="font-semibold text-red-100 hover:underline" onClick={() => setOpenId(red.first_bad)}>
              first bad #{red.first_bad_seq} by {red.first_bad_author}: {red.first_bad_title}
            </button>
          ) : (
            <span>bisecting: first bad not known yet</span>
          )}
        </p>
        {fix ? (
          <p className="text-[11px] text-red-200/80">
            <button type="button" className="font-mono text-red-100 hover:underline" onClick={() => setOpenId(fix.id)}>
              {fix.alias}
            </button>{" "}
            {fix.title} ·{" "}
            {fix.holder
              ? `claimed by ${fix.holder}`
              : fix.offer_until && now < fix.offer_until
                ? `offered to ${fix.offered_to} for ${countdown(fix.offer_until, now)}, then to anyone`
                : "open to anyone"}
          </p>
        ) : null}
      </div>
    </div>
  )
}
