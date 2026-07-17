// The agent avatar: a 3×3 grid of tiny dots — the SAME grain size as the dots in
// the effort slider (~2px on a 6px pitch), all uniform in size. While the agent
// answers, each dot LIGHTS UP on its OWN schedule and to its OWN color shade of
// the effort fade — so they twinkle randomly, never all the same color at once.
// When the agent is done the animation freezes and each dot HOLDS its current
// color. Transparent background, pure CSS.

import { cn } from "@/lib/utils"

// Per-dot: a starting PHASE (0..1, where in the glow cycle the dot sits), a
// slightly varied cycle duration, and a peak fade mix % (how "Smarter"/dark its
// brightest color reaches). The phase becomes a NEGATIVE animation-delay
// (-phase*dur) so the dot is frozen mid-cycle even before it runs — this is what
// makes a NON-animating avatar (a turn loaded from history, or a finished turn)
// show a scattered, settled set of colors instead of every dot stuck faint/"off"
// at phase 0. When active it still twinkles from that phase. Phases spread across
// the range (none at 0/1, which are the faint troughs) so the resting grid reads
// as lit-but-varied. No two dots share phase+peak, so it twinkles, not pulses.
const DOTS = [
  { phase: 0.16, dur: 1.25, peak: 92 },
  { phase: 0.62, dur: 1.6, peak: 62 },
  { phase: 0.38, dur: 1.35, peak: 80 },
  { phase: 0.82, dur: 1.5, peak: 70 },
  { phase: 0.5, dur: 1.7, peak: 96 },
  { phase: 0.28, dur: 1.3, peak: 55 },
  { phase: 0.72, dur: 1.45, peak: 85 },
  { phase: 0.44, dur: 1.55, peak: 66 },
  { phase: 0.9, dur: 1.4, peak: 90 },
]

export function AgentAvatar({ active = false, className }) {
  return (
    <span
      className={cn("okf-agent-dots", active && "is-active", className)}
      aria-hidden="true"
    >
      {DOTS.map((d, i) => (
        <span
          key={i}
          style={{
            // Negative delay = start `phase` of the way into the cycle. Paused,
            // the dot freezes there → varied resting colors, not a flat "off".
            animationDelay: `${(-(d.phase * d.dur)).toFixed(3)}s`,
            animationDuration: `${d.dur}s`,
            "--okf-dot-peak": `${d.peak}%`,
          }}
        />
      ))}
    </span>
  )
}
