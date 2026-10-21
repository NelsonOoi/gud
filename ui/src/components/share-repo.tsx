import { Copy } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"

export function joinCommand(origin = window.location.origin): string {
  return `gud join ${origin} --handle @x --address x@their.domain --role agent --sponsor @human`
}

export async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    toast.success("Copied")
  } catch {
    toast.error("Could not copy")
  }
}

export function ShareRepo({ repo }: { repo: string | undefined }) {
  const command = joinCommand()
  const address = repo ?? ""

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          Share repo
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Share repo</DialogTitle>
          <DialogDescription>
            An outside agent joins from their own mail account with a self-signed join request. They keep their
            private key; a member admits them with a membership push.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <CopyBlock label="Repo address" value={address} />
          <CopyBlock label="Join command" value={command} />
        </div>
      </DialogContent>
    </Dialog>
  )
}

function CopyBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span>{label}</span>
        <Button
          variant="outline"
          size="icon-sm"
          aria-label={`Copy ${label}`}
          disabled={!value}
          onClick={() => void copyText(value)}
        >
          <Copy />
        </Button>
      </div>
      <pre className="overflow-x-auto rounded-md border border-border bg-muted/40 px-2 py-1.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-foreground">
        {value || "…"}
      </pre>
    </div>
  )
}
