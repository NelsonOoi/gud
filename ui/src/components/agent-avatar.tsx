import { agentColor, initials } from "@/lib/agents"
import { cn } from "@/lib/utils"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"

export function AgentAvatar({
  handle,
  className,
}: {
  handle: string
  className?: string
}) {
  const color = agentColor(handle)
  return (
    <Avatar className={cn("size-6", className)}>
      <AvatarFallback
        className="text-[10px] font-semibold"
        style={{ backgroundColor: color.bg, color: color.fg }}
      >
        {initials(handle)}
      </AvatarFallback>
    </Avatar>
  )
}

export function AgentChip({
  handle,
  muted,
  struck,
}: {
  handle: string
  muted?: boolean
  struck?: boolean
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border border-border bg-background/60 px-1.5 py-0.5 text-[11px] leading-none",
        muted && "opacity-60",
      )}
    >
      <AgentAvatar handle={handle} className="size-4" />
      <span className={cn("font-medium", struck && "line-through")}>{handle}</span>
    </span>
  )
}
