// The composer's effort slider (ported from Sentry): stepped, with a detent
// feel. Dragging meets some resistance leaving the current level, and once the
// pointer passes halfway the bar springs into the next one. The value itself only
// ever moves a whole level. Radix tracks the pointer continuously (a fine `step`);
// what it is handed back is the animated position, so the fill and the bar follow
// the spring, not the pointer. Styled by .okf-effort-slider (index.css).

import { useCallback, useEffect, useRef, useState } from "react"
import { Slider as SliderPrimitive } from "radix-ui"

// How far, in levels, the bar gives as the pointer nears halfway to the next level.
const GIVE = 0.18
// A spring a little under critical damping: a quick snap with a barely visible settle.
const STIFFNESS = 520
const DAMPING = 2 * Math.sqrt(STIFFNESS) * 0.72
const KEY_STEPS = {
  ArrowLeft: -1,
  ArrowDown: -1,
  PageDown: -1,
  ArrowRight: 1,
  ArrowUp: 1,
  PageUp: 1,
}

// The bar's offset from a level for a pointer `d` levels away (|d| <= 0.5):
// quadratic, so it barely moves at first and gives most just before the snap.
function resisted(d) {
  const x = Math.min(Math.abs(d) / 0.5, 1)
  return Math.sign(d) * GIVE * x * x
}

const prefersReducedMotion = () =>
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false

export function EffortSlider({
  index,
  count,
  onIndexChange,
  valueText,
  ...props
}) {
  const last = Math.max(count - 1, 0)
  const [shown, setShown] = useState(index)
  const [dragging, setDragging] = useState(false)
  // The level last chosen here, ahead of the `index` prop coming back round.
  const chosen = useRef(index)
  const spring = useRef({ x: index, v: 0, target: index, frame: 0, t: 0 })
  const root = useRef(null)

  const aim = useCallback((target) => {
    const s = spring.current
    s.target = target
    if (prefersReducedMotion()) {
      cancelAnimationFrame(s.frame)
      Object.assign(s, { x: target, v: 0, frame: 0 })
      setShown(target)
      return
    }
    if (s.frame) return
    const tick = (now) => {
      // Capped, so a backgrounded tab does not resume with one huge step.
      const dt = Math.min((now - s.t) / 1000, 1 / 30)
      s.t = now
      s.v += (STIFFNESS * (s.target - s.x) - DAMPING * s.v) * dt
      s.x += s.v * dt
      if (Math.abs(s.target - s.x) < 0.001 && Math.abs(s.v) < 0.01) {
        Object.assign(s, { x: s.target, v: 0, frame: 0 })
      } else {
        s.frame = requestAnimationFrame(tick)
      }
      setShown(s.x)
    }
    s.t = performance.now()
    s.frame = requestAnimationFrame(tick)
  }, [])

  useEffect(() => () => cancelAnimationFrame(spring.current.frame), [])

  // A level set from outside (a model switch clamping the effort) glides there too.
  useEffect(() => {
    if (dragging || index === chosen.current) return
    chosen.current = index
    aim(index)
  }, [index, dragging, aim])

  const choose = (level) => {
    if (level === chosen.current) return
    chosen.current = level
    onIndexChange?.(level)
  }

  return (
    <SliderPrimitive.Root
      ref={root}
      data-slot="slider"
      data-dragging={dragging ? "" : undefined}
      min={0}
      max={last}
      step={0.001}
      value={[shown]}
      onPointerDown={() => setDragging(true)}
      // After release or cancel alike: settle on the chosen level.
      onLostPointerCapture={() => {
        setDragging(false)
        aim(chosen.current)
      }}
      onValueChange={([p]) => {
        const nearest = Math.round(p)
        choose(nearest)
        aim(Math.min(Math.max(nearest + resisted(p - nearest), 0), last))
      }}
      // Whole levels from the keyboard; preventDefault skips Radix's fine step.
      onKeyDown={(e) => {
        const level =
          e.key === "Home"
            ? 0
            : e.key === "End"
              ? last
              : e.key in KEY_STEPS
                ? Math.min(Math.max(chosen.current + KEY_STEPS[e.key], 0), last)
                : null
        if (level === null) return
        e.preventDefault()
        choose(level)
        aim(level)
      }}
      // The fill's end (see .okf-effort-slider in index.css).
      style={{ "--okf-effort-frac": last > 0 ? shown / last || 0.0001 : 1 }}
      className="relative flex w-full touch-none items-center select-none"
      {...props}
    >
      <SliderPrimitive.Track
        data-slot="slider-track"
        className="relative w-full grow overflow-hidden"
      >
        <SliderPrimitive.Range
          data-slot="slider-range"
          className="absolute h-full select-none"
        />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb
        data-slot="slider-thumb"
        aria-label={props["aria-label"]}
        // The level, not the animated position Radix is holding.
        aria-valuenow={index}
        aria-valuetext={valueText}
        className="relative block shrink-0 select-none focus-visible:outline-hidden"
      />
    </SliderPrimitive.Root>
  )
}
