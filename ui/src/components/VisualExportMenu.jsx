// The hover kebab every visual carries — canvas tiles (chart, kpi, table,
// pivot) and inline chat charts alike: Copy to clipboard / Download SVG /
// Download PNG. The menu knows nothing about HOW a visual renders; the caller
// hands it `getPng` — a hook resolving {dataUrl, width, height} (postMessage
// round-trip for sandboxed chart frames, DOM capture for HTML tiles; see
// lib/visualExport.js) — and all three actions derive from that one PNG.
//
// Visibility is the CALLER's affair (opacity-0 → group-hover reveal on its
// own container), passed via className; data-[state=open] keeps the trigger
// shown while its portaled menu is up and the pointer has wandered off.

import { CopyIcon, DownloadIcon, EllipsisIcon, Loader2Icon } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  copyPngToClipboard,
  downloadDataUrl,
  downloadText,
  exportFilename,
  pngToSvg,
} from "@/lib/visualExport"
import { cn } from "@/lib/utils"

export function VisualExportMenu({ title, getPng, className }) {
  const [busy, setBusy] = useState(false)
  const name = exportFilename(title)
  const run = (work, okMessage) => {
    setBusy(true)
    work()
      .then(() => okMessage && toast.success(okMessage))
      .catch((err) => toast.error(err?.message || "Export failed"))
      .finally(() => setBusy(false))
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          disabled={busy}
          aria-label="Export visual"
          // Excluded from DOM captures — at capture time the pointer is still
          // hovering the tile, so the revealed kebab would land in the image.
          data-export-exclude=""
          className={cn(
            "size-6 text-muted-foreground data-[state=open]:opacity-100",
            busy && "opacity-100",
            className
          )}
        >
          {busy ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <EllipsisIcon className="size-3.5" />
          )}
        </Button>
      </DropdownMenuTrigger>
      {/* The kit content pins itself to the TRIGGER's width — fine for the
          wide Actions button, but this trigger is a 24px kebab, so the labels
          wrapped to two lines. w-auto sizes to the widest item instead. */}
      <DropdownMenuContent align="end" className="w-auto">
        {/* Copy + SVG export TRANSPARENT (they get pasted/embedded onto other
            surfaces); the PNG download keeps the theme surface — it's shared
            as-is, and a bare dark-mode chart is near-white ink on nothing.
            Chart frames honor the bg option; DOM-captured tiles ignore it
            (a table without its card surface reads broken). */}
        <DropdownMenuItem
          className="text-xs"
          // copyPngToClipboard builds its ClipboardItem synchronously in this
          // gesture (Safari requirement) — don't wrap it behind an await.
          onSelect={() =>
            run(
              () => copyPngToClipboard(() => getPng({ bg: "transparent" })),
              "Copied to clipboard"
            )
          }
        >
          <CopyIcon className="size-3.5" /> Copy to clipboard
        </DropdownMenuItem>
        <DropdownMenuItem
          className="text-xs"
          onSelect={() =>
            run(() =>
              Promise.resolve()
                .then(() => getPng({ bg: "transparent" }))
                .then((png) => downloadText(pngToSvg(png), `${name}.svg`, "image/svg+xml"))
            )
          }
        >
          <DownloadIcon className="size-3.5" /> Download SVG
        </DropdownMenuItem>
        <DropdownMenuItem
          className="text-xs"
          onSelect={() =>
            run(() =>
              Promise.resolve()
                .then(() => getPng())
                .then((png) => downloadDataUrl(png.dataUrl, `${name}.png`))
            )
          }
        >
          <DownloadIcon className="size-3.5" /> Download PNG
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
