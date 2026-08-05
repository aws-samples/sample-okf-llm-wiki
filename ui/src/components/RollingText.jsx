// RollingText — the label-roll swap Claude Code desktop uses: when the value
// changes, the old text drops out below while the new one falls in from above
// (keyframes in index.css). Two deliberate behaviors:
//
// - No roll on MOUNT (gen 0 renders un-animated) — only on a change.
// - No roll on streaming GROWTH: the thinking header's reasoning preview
//   builds up word-by-word as tokens arrive ("Let me" → "Let me check" → …),
//   and rolling on every extension would strobe. A change where one value is
//   a prefix of the other (ignoring the truncation ellipsis) swaps in place;
//   only a genuine replacement animates.
//
// `className` styles the clipping container; `textClassName` styles BOTH text
// spans — required for effects like `text-shimmer` that use background-clip
// and must sit on the text element itself (a styled parent renders child
// spans transparent).

import { useEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"

const ROLL_MS = 260 // keep in sync with the okf-roll-* animation duration

const strip = (s) => (s || "").replace(/…$/, "")

function isGrowth(a, b) {
  const x = strip(a)
  const y = strip(b)
  return x.startsWith(y) || y.startsWith(x)
}

export function RollingText({ text, className, textClassName }) {
  const prevRef = useRef(text)
  const [leaving, setLeaving] = useState(null)
  // Increments only on ANIMATED swaps — keying the in-flow span on it re-runs
  // the enter animation exactly then (never on growth updates or mount).
  const [gen, setGen] = useState(0)

  useEffect(() => {
    const prev = prevRef.current
    if (prev === text) return undefined
    prevRef.current = text
    if (isGrowth(prev, text)) return undefined
    setLeaving(prev)
    setGen((g) => g + 1)
    const t = setTimeout(() => setLeaving(null), ROLL_MS)
    return () => clearTimeout(t)
  }, [text])

  return (
    <span
      className={cn(
        "relative inline-flex max-w-full overflow-hidden align-bottom",
        className
      )}
    >
      <span
        key={gen}
        className={cn(
          "inline-block max-w-full truncate",
          gen > 0 && "okf-roll-in",
          textClassName
        )}
      >
        {text}
      </span>
      {leaving != null ? (
        <span
          key={`out-${gen}`}
          aria-hidden="true"
          className={cn(
            "okf-roll-out absolute inset-0 inline-block truncate",
            textClassName
          )}
        >
          {leaving}
        </span>
      ) : null}
    </span>
  )
}
