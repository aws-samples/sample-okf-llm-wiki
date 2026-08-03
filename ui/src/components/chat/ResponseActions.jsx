// The action bar under a completed AI response: copy the answer, inspect its token
// usage, and give like/dislike feedback. Rendered only for finished turns that
// produced text (no bar while streaming or on a tool-only/empty turn). Policy
// flags need no button here — they arrive mid-turn as shield timeline steps
// when the run opted in (the composer's Policy feature).
//
// Feedback is LOCAL toggle state (mutually exclusive, click again to clear) — the
// chat has no feedback sink yet, so this is a UI affordance; wiring it to a
// backend later just means lifting `feedback` up + a POST. Copy uses the same
// CopyButton primitive as the code viewer.

import { GaugeIcon, ThumbsDownIcon, ThumbsUpIcon } from "lucide-react"
import { memo, useCallback, useState } from "react"

import { Button } from "@/components/ui/button"
import { CopyButton } from "@/components/ui/copy-button"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"

// Compact token counts (1.2K / 3.4M), matching the harvest usage popover.
function fmtTokens(n) {
  if (!n) return "0"
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}K`
  return String(n)
}

function UsageStat({ label, value }) {
  return (
    <div
      className="rounded-md border bg-muted/40 px-2 py-1"
      title={`${(value || 0).toLocaleString()} tokens`}
    >
      <div className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </div>
      <div className="text-sm font-semibold tabular-nums">
        {fmtTokens(value || 0)}
      </div>
    </div>
  )
}

// Input-side composition: fresh input vs cache reads vs cache writes, as a
// shaded bar + legend (the same visual language as the harvest usage popover).
const INPUT_PARTS = [
  { key: "input_tokens", label: "Fresh input", swatch: "bg-primary" },
  { key: "cache_read_input_tokens", label: "Cache read", swatch: "bg-primary/60" },
  { key: "cache_creation_input_tokens", label: "Cache write", swatch: "bg-primary/30" },
]

// The turn's token usage, from the stream's terminal end chunk. A quiet gauge
// button in the action bar; the popover shows Input/Output tiles plus the
// input-side cache composition. Absent stats (history-loaded or errored
// turns) hide the button entirely.
function UsageAction({ stats }) {
  const total = INPUT_PARTS.reduce((s, p) => s + (stats[p.key] || 0), 0)
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Token usage for this turn"
          className="size-7 rounded-md text-muted-foreground hover:text-foreground"
        >
          <GaugeIcon className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 p-3">
        <p className="text-sm font-medium">Tokens this turn</p>
        <div className="mt-2 grid grid-cols-2 gap-2">
          <UsageStat label="Input" value={total} />
          <UsageStat label="Output" value={stats.output_tokens} />
        </div>
        {total > 0 ? (
          <div className="mt-3 flex flex-col gap-1.5">
            <div className="flex h-1.5 overflow-hidden rounded-full bg-muted">
              {INPUT_PARTS.map(({ key, swatch }) =>
                stats[key] ? (
                  <div
                    key={key}
                    className={swatch}
                    style={{ width: `${(stats[key] / total) * 100}%` }}
                  />
                ) : null
              )}
            </div>
            {INPUT_PARTS.map(({ key, label, swatch }) => (
              <div
                key={key}
                className="flex items-center justify-between text-xs"
              >
                <span className="flex items-center gap-1.5 text-muted-foreground">
                  <span className={cn("size-2 rounded-full", swatch)} />
                  {label}
                </span>
                <span className="font-mono tabular-nums">
                  {stats[key] ? fmtTokens(stats[key]) : "—"}
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  )
}

export const ResponseActions = memo(function ResponseActions({
  text,
  stats,
}) {
  const [feedback, setFeedback] = useState(null) // "up" | "down" | null
  const copy = (text || "").trim()

  const vote = useCallback(
    (v) => setFeedback((cur) => (cur === v ? null : v)),
    []
  )

  return (
    // One flat row with a single gap: every action sits the same distance from
    // its neighbors (the segmented ButtonGroup + extra margin read as uneven).
    <div className="mt-1 flex items-center gap-1">
      <CopyButton text={copy} label="Copy response" />
      {stats ? <UsageAction stats={stats} /> : null}
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Good response"
          aria-pressed={feedback === "up"}
          onClick={() => vote("up")}
          className={cn(
            "size-7 rounded-md text-muted-foreground hover:text-foreground",
            feedback === "up" && "text-primary hover:text-primary"
          )}
        >
          <ThumbsUpIcon
            className={cn("size-3.5", feedback === "up" && "fill-current")}
          />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Bad response"
          aria-pressed={feedback === "down"}
          onClick={() => vote("down")}
          className={cn(
            "size-7 rounded-md text-muted-foreground hover:text-foreground",
            feedback === "down" && "text-destructive hover:text-destructive"
          )}
        >
          <ThumbsDownIcon
            className={cn("size-3.5", feedback === "down" && "fill-current")}
          />
        </Button>
    </div>
  )
})
