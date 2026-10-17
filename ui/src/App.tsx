import { Shell } from "@/components/layout"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { GudProvider } from "@/hooks/use-gud"

export default function App() {
  return (
    <TooltipProvider>
      <GudProvider>
        <Shell />
        <Toaster theme="dark" position="bottom-right" />
      </GudProvider>
    </TooltipProvider>
  )
}
