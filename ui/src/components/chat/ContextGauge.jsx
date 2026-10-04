// The agent's context-window fill as a donut beside the composer's model/effort
// setting (ported from Sentry); the popover has the numbers and Compact Now.
// `context` is the runtime's reading — the newest model call's input tokens over
// the model's window, plus the auto-compaction threshold — updated after every
// call. Always on screen (grey and inert until measured); a "Compacting…" chip
// while a compaction runs. No digits in the row: the ring's colour carries it.

import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn, formatTokens } from "@/lib/utils"

// Fixed percentages, not fractions of the compaction threshold, so the colour
// changes at a number the user can name whatever the model.
const BAND_AMBER = 60
const BAND_RED = 80

function band(percent, measured) {
  // Grey means "no reading", not 0%.
  if (!measured) return "text-muted-foreground"
  if (percent >= BAND_RED) return "text-destructive"
  if (percent >= BAND_AMBER) return "text-chart-4"
  return "text-primary"
}

// The ring's filled arc is a stroke-dasharray on one circle. The `size-3.5`
// class is load-bearing: `icon-xs` clamps any svg without a size- class to 12px.
const RING = 14
const RING_STROKE = 3.5

function Donut({ fraction, className }) {
  // Half a stroke in from the edge, so the ring is not clipped by its own box.
  const radius = (RING - RING_STROKE) / 2
  const circumference = 2 * Math.PI * radius
  const filled = Math.max(0, Math.min(1, fraction)) * circumference
  const center = RING / 2
  return (
    <svg
      width={RING}
      height={RING}
      viewBox={`0 0 ${RING} ${RING}`}
      className={cn("size-3.5 shrink-0", className)}
      aria-hidden="true"
    >
      {/* The track: the whole window, in the edge grey. */}
      <circle
        cx={center}
        cy={center}
        r={radius}
        fill="none"
        stroke="currentColor"
        strokeWidth={RING_STROKE}
        className="text-edge"
      />
      {/* The fill, from 12 o'clock clockwise (hence the -90° rotation), in the
          band colour inherited from the svg. */}
      <circle
        cx={center}
        cy={center}
        r={radius}
        fill="none"
        stroke="currentColor"
        strokeWidth={RING_STROKE}
        strokeDasharray={`${filled} ${circumference - filled}`}
        strokeLinecap="butt"
        transform={`rotate(-90 ${center} ${center})`}
        className="transition-[stroke-dasharray] duration-300 ease-out motion-reduce:transition-none"
      />
    </svg>
  )
}

export function ContextGauge({
  context,
  modelName,
  onCompact,
  compacting = false,
  disabled = false,
}) {
  const [open, setOpen] = useState(false)
  const tokens = Number(context?.tokens) || 0
  const window_ = Number(context?.window) || 0
  const measured = tokens > 0 && window_ > 0
  const percent = measured
    ? Number(context.percent) || (tokens / window_) * 100
    : 0
  const thresholdTokens = Number(context?.threshold) || 0
  // Without a threshold from the runtime, fall back to 85 rather than claim
  // "compacts at 0%".
  const thresholdPercent =
    thresholdTokens && window_ ? (thresholdTokens / window_) * 100 : 85
  const tone = band(percent, measured)
  const compactions = Number(context?.compactions) || 0
  // Sub-1% readings keep a decimal rather than show "0%" beside a filled ring.
  const shown =
    percent >= 1 ? Math.round(percent) : Math.max(percent, 0.1).toFixed(1)

  if (compacting) {
    return (
      <span
        className="inline-flex items-center gap-1.5 rounded-md bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
        aria-live="polite"
      >
        <span className="inline-block size-1.5 animate-pulse rounded-full bg-primary/70" />
        Compacting…
      </span>
    )
  }

  // Inert until measured (nothing to show), sized like the button below so the
  // row does not shift when the first reading lands.
  if (!measured) {
    return (
      <span
        className="flex size-6 items-center justify-center"
        title="Context window: nothing measured yet"
        aria-label="Context window, nothing measured yet"
      >
        <Donut fraction={0} className={tone} />
      </span>
    )
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        {/* The ring alone; the digits are in the popover. */}
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className="hover:bg-foreground/5"
          title={`Context ${shown}% full, ${formatTokens(tokens)} of ${formatTokens(window_)} tokens`}
          aria-label={`Context window ${shown} percent full`}
        >
          {/* The tone is on the ring, not the button: the ghost hover recolours
              the button's text, which would change the reading. */}
          <Donut fraction={percent / 100} className={tone} />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" side="top" className="w-72 gap-0 p-2.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-sm font-medium">
            <span className="text-muted-foreground">Context</span>
            <span className="tabular-nums"> {shown}%</span>
          </span>
          {modelName ? (
            <span className="text-xs text-muted-foreground">{modelName}</span>
          ) : null}
        </div>

        {/* The same reading as the donut, with the auto-compaction threshold. */}
        <div className="mt-2">
          <div className="relative h-1.5 overflow-hidden rounded-full bg-foreground/8">
            <div
              className={cn(
                "h-full rounded-full bg-current transition-[width] duration-300 motion-reduce:transition-none",
                tone
              )}
              style={{ width: `${Math.min(percent, 100)}%` }}
            />
            <div
              className="absolute inset-y-0 w-px bg-foreground/40"
              style={{ left: `${Math.min(thresholdPercent, 100)}%` }}
            />
          </div>
          <div className="mt-1 flex items-center justify-between text-[11px] text-muted-foreground">
            <span className="tabular-nums">
              {formatTokens(tokens)} / {formatTokens(window_)} Tokens
            </span>
            <span className="tabular-nums">
              Auto-Compacts At {Math.round(thresholdPercent)}%
            </span>
          </div>
        </div>

        {compactions > 0 ? (
          <p className="mt-1.5 text-xs text-muted-foreground">
            Compacted {compactions === 1 ? "Once" : `${compactions} Times`}
          </p>
        ) : null}

        {onCompact ? (
          // Disabled while a turn runs, which the runtime would refuse.
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-2.5 w-full"
            disabled={disabled}
            onClick={() => {
              setOpen(false)
              onCompact()
            }}
          >
            Compact Now
          </Button>
        ) : null}
      </PopoverContent>
    </Popover>
  )
}
