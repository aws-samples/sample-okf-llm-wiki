// The conversation timeline: a column of ticks on the transcript's mid-left
// edge, one per question, for jumping around a transcript that overflows its
// viewport (ported from Sentry's TurnRail). Hovering the
// rail (or tabbing onto it) grows it into a panel listing every question's first
// line. The rail tracks the viewport itself, so the lit tick moving re-renders
// the rail, not the thread.

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react"

import { FadeText } from "@/components/FadeText"
import { cn } from "@/lib/utils"

// Overflow below this is no scroll worth a rail: a lone tail turn's reserve
// overflows by a few pixels. ChatThread's jump-to-bottom button uses it too, so
// the button and the lit last tick agree on where the bottom is.
export const SLACK = 24
// The rail spans 4-28px in from the viewport's left edge; the column's text
// starts 16px inside its box. Below this gap between the viewport's edge and
// the column they would overlap.
const RAIL_ROOM = 24
// The nav's cap (max-h-[calc(100%-9rem)]) leaves no room for a tick (h-2.5) in a
// shorter viewport, where an invisible rail would still hold a tab stop.
const MIN_HEIGHT = 154
// Crossing the rail on the way somewhere else should not flash the panel, and
// the pointer moving from the ticks onto the panel should not close it.
const OPEN_DELAY = 120
const CLOSE_DELAY = 180

// The question's first non-blank line, without splitting the whole prompt.
function firstLine(text) {
  const s = String(text || "").trimStart()
  const nl = s.indexOf("\n")
  return (nl < 0 ? s : s.slice(0, nl)).trim() || "Resumed Turn"
}

// The turn at the top of the view: the last one whose top has passed `line`,
// or the last turn once the view is at the bottom.
function turnAt(viewport, content, count, line) {
  const n = Math.min(count, content.children.length)
  if (!n) return -1
  const gap = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight
  if (gap <= SLACK) return n - 1
  // Tops only grow down the column.
  let lo = 0
  let hi = n - 1
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (content.children[mid].getBoundingClientRect().top <= line) lo = mid
    else hi = mid - 1
  }
  // Short turns at the end can share the last screen: the bottom takes over
  // before their tops reach the line, so their ticks would never light. When the
  // next turn is one of them, lo and the turns before the last share out the
  // scroll left once that turn is in view, in order.
  const rest = n - 1 - lo
  if (rest > 1) {
    const room = gap - SLACK
    const next = content.children[lo + 1].getBoundingClientRect().top
    if (next - room >= line) {
      const since = Math.max(
        0,
        Math.min(
          viewport.scrollTop,
          line - content.children[lo].getBoundingClientRect().top,
          viewport.getBoundingClientRect().bottom - next
        )
      )
      return Math.min(n - 2, lo + Math.floor((since / (since + room)) * rest))
    }
  }
  return lo
}

const Tick = memo(function Tick({ index, text, active, tabbable, onJump }) {
  return (
    <button
      type="button"
      // A tick takes focus when pressed, so the jump hands it on to the turn.
      onClick={() => onJump(index, true)}
      tabIndex={tabbable ? 0 : -1}
      aria-label={`Turn ${index + 1}: ${firstLine(text)}`}
      aria-current={active ? "true" : undefined}
      // The hit area is the full row; the line is the span, flush left. Forced
      // colors drop the ring and repaint backgrounds, so they get system colours.
      className="group flex h-2.5 w-6 shrink-0 items-center rounded-sm px-1.5 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-hidden"
    >
      <span
        className={cn(
          "h-0.5 w-3 rounded-full transition-colors forced-colors:forced-color-adjust-none",
          active
            ? "bg-primary forced-colors:bg-[Highlight]"
            : "bg-foreground/20 group-hover:bg-foreground/50 group-focus-visible:bg-foreground/50 forced-colors:bg-[CanvasText]"
        )}
      />
    </button>
  )
})

// A panel row. Mouse only: keyboard focus stays on the ticks, mirrored here.
const Row = memo(function Row({ index, text, active, focused, onJump }) {
  return (
    <button
      type="button"
      tabIndex={-1}
      // Keep focus where it was; the jump moves the view, not the caret.
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => onJump(index)}
      className={cn(
        "flex h-7 w-full shrink-0 items-center rounded-md px-2 text-left text-[13px] outline-none hover:bg-foreground/5 hover:text-foreground",
        active ? "font-medium text-primary" : "text-muted-foreground",
        focused && "bg-foreground/5 text-foreground"
      )}
    >
      <FadeText className="flex-1">{firstLine(text)}</FadeText>
    </button>
  )
})

const KEY_STEP = { ArrowUp: -1, ArrowDown: 1 }

function TurnRailImpl({
  turns,
  viewport,
  content,
  topGap,
  // Scrolls turn i to the top and returns the clamped scrollTop it is heading to.
  onJump,
}) {
  const navRef = useRef(null)
  const [active, setActive] = useState(-1)
  const [overflows, setOverflows] = useState(false)
  const [roomy, setRoomy] = useState(false)
  const [height, setHeight] = useState(0)
  const countRef = useRef(turns.length)
  // A jump's destination. It holds the lit tick while the view closes in on it
  // (a short tail turn cannot reach the top, so the position-based pick would
  // differ) and lets go on a wheel over the rail, on any scroll that moves away,
  // and when the turn count changes.
  const holdRef = useRef(null)
  const panelRef = useRef(null)
  // The panel is open while the pointer is over the rail or panel, or while a
  // tick has keyboard focus (`focused` is its index, else -1).
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(-1)
  const hoverTimerRef = useRef(null)
  const open = hovered || focused >= 0

  const hoverSoon = useCallback((next) => {
    clearTimeout(hoverTimerRef.current)
    hoverTimerRef.current = setTimeout(
      () => setHovered(next),
      next ? OPEN_DELAY : CLOSE_DELAY
    )
  }, [])
  useEffect(() => () => clearTimeout(hoverTimerRef.current), [])

  const measure = useCallback(() => {
    if (!viewport || !content) return
    const vr = viewport.getBoundingClientRect()
    const max = viewport.scrollHeight - viewport.clientHeight
    setOverflows(max > SLACK)
    setRoomy(content.getBoundingClientRect().left - vr.left >= RAIL_ROOM)
    setHeight(viewport.clientHeight)
    const hold = holdRef.current
    if (hold) {
      const dist = Math.abs(viewport.scrollTop - hold.top)
      if (dist <= hold.dist + 1) {
        hold.dist = Math.min(hold.dist, dist)
        return
      }
      holdRef.current = null
    }
    setActive(turnAt(viewport, content, countRef.current, vr.top + topGap + 8))
  }, [viewport, content, topGap])

  useEffect(() => {
    if (!viewport || !content) return undefined
    viewport.addEventListener("scroll", measure, { passive: true })
    // Observing reports the first size too, so this also takes the first reading.
    const ro = new ResizeObserver(measure)
    ro.observe(viewport)
    ro.observe(content)
    return () => {
      viewport.removeEventListener("scroll", measure)
      ro.disconnect()
    }
  }, [viewport, content, measure])

  // A turn arriving or the transcript reloading pins or jumps the view itself.
  // The column's resize re-measures.
  useLayoutEffect(() => {
    countRef.current = turns.length
    holdRef.current = null
  }, [turns.length])

  const jump = useCallback(
    (i, focus) => {
      const top = onJump(i, focus)
      if (top == null) return
      holdRef.current = { top, dist: Math.abs(viewport.scrollTop - top) }
      setActive(i)
    },
    [onJump, viewport]
  )

  const shown = overflows && roomy && height >= MIN_HEIGHT && turns.length > 0

  // A capped rail keeps the lit tick in view, before paint. Set by hand:
  // scrollIntoView would also scroll the page's ancestors.
  useLayoutEffect(() => {
    const nav = navRef.current
    const tick = nav?.children[active]
    if (!tick) return
    const top = tick.offsetTop
    const bottom = top + tick.offsetHeight
    if (top < nav.scrollTop) nav.scrollTop = top
    else if (bottom > nav.scrollTop + nav.clientHeight) {
      nav.scrollTop = bottom - nav.clientHeight
    }
  }, [active, shown, height])

  // The open panel keeps the focused row, else the lit one, in view.
  useLayoutEffect(() => {
    const panel = panelRef.current
    const row = open && panel?.children[focused >= 0 ? focused : active]
    if (!row) return
    const top = row.offsetTop
    const bottom = top + row.offsetHeight
    if (top < panel.scrollTop) panel.scrollTop = top
    else if (bottom > panel.scrollTop + panel.clientHeight) {
      panel.scrollTop = bottom - panel.clientHeight
    }
  }, [open, active, focused])

  // The rail sits beside the viewport, not in it, so a wheel over it would scroll
  // nothing; it drives the transcript instead, and the rail follows the lit tick.
  const onWheel = (e) => {
    // Ctrl+wheel and trackpad pinches are zoom gestures, not scrolls.
    if (e.ctrlKey || !e.deltaY) return
    const unit =
      e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? viewport.clientHeight : 1
    // The user takes over, so a jump still in flight stops holding its tick: this
    // instant scroll cuts its smooth scroll short wherever it has got to.
    holdRef.current = null
    viewport.scrollBy({ top: e.deltaY * unit })
  }

  // A panel long enough to scroll takes the wheel itself.
  const onPanelWheel = (e) => {
    const panel = e.currentTarget
    if (panel.scrollHeight <= panel.clientHeight) onWheel(e)
  }

  // Only keyboard focus opens the panel: a clicked tick keeps focus in Chromium,
  // and that would hold the panel open after the pointer left.
  const onFocus = (e) => {
    const at = [...e.currentTarget.children].indexOf(e.target)
    if (at >= 0 && e.target.matches(":focus-visible")) setFocused(at)
  }
  const onBlur = (e) => {
    if (!e.currentTarget.contains(e.relatedTarget)) setFocused(-1)
  }

  // One tab stop: the focused tick while focus is in the rail, else the lit one
  // (see tabStop); arrows move between ticks.
  const onKeyDown = (e) => {
    const ticks = [...e.currentTarget.children]
    const at = ticks.indexOf(document.activeElement)
    if (at < 0) return
    if (e.key === "Escape") {
      clearTimeout(hoverTimerRef.current)
      setHovered(false)
      setFocused(-1)
      return
    }
    let to = null
    if (e.key in KEY_STEP) to = at + KEY_STEP[e.key]
    else if (e.key === "Home") to = 0
    else if (e.key === "End") to = ticks.length - 1
    if (to === null) return
    e.preventDefault()
    ticks[Math.max(0, Math.min(ticks.length - 1, to))].focus()
  }

  if (!shown) return null
  // Following focus, Tab and Shift+Tab leave the rail from whichever tick the
  // arrows reached, rather than stopping again on the lit one.
  const tabStop =
    focused >= 0 && focused < turns.length ? focused : active >= 0 ? active : 0
  return (
    <>
      {/* Centred on the viewport's left edge; a long list is capped short of the
        fades and scrolled only to follow the lit tick. */}
      <nav
        ref={navRef}
        aria-label="Conversation Turns"
        onWheel={onWheel}
        onKeyDown={onKeyDown}
        onFocus={onFocus}
        onBlur={onBlur}
        onPointerEnter={() => hoverSoon(true)}
        onPointerLeave={() => hoverSoon(false)}
        className={cn(
          "absolute top-1/2 left-1 z-10 flex max-h-[calc(100%-9rem)] -translate-y-1/2 flex-col overflow-hidden transition-opacity duration-150",
          open && "opacity-0"
        )}
      >
        {/* Keyed by position: a history re-read swaps live turn ids for stored
            ones, and remounting the ticks would drop a focused one's focus. */}
        {turns.map((turn, i) => (
          <Tick
            key={i}
            index={i}
            text={turn.userMessage}
            active={i === active}
            tabbable={i === tabStop}
            onJump={jump}
          />
        ))}
      </nav>
      {/* Grows rightward out of the rail over the transcript. The ticks carry the
        accessible names, so this mirror is hidden from assistive tech. */}
      <div
        ref={panelRef}
        aria-hidden="true"
        onWheel={onPanelWheel}
        onPointerEnter={() => hoverSoon(true)}
        onPointerLeave={() => hoverSoon(false)}
        className={cn(
          "absolute top-1/2 left-1 z-20 flex max-h-[calc(100%-6rem)] w-72 origin-left -translate-y-1/2 flex-col overflow-y-auto overscroll-contain rounded-lg bg-popover p-1 text-popover-foreground shadow-md ring-1 ring-edge transition-[opacity,translate,scale,visibility] duration-150 ease-out motion-reduce:transition-none",
          open
            ? "visible translate-x-0 scale-100 opacity-100"
            : "pointer-events-none invisible -translate-x-1 scale-95 opacity-0"
        )}
      >
        {turns.map((turn, i) => (
          <Row
            key={turn.id}
            index={i}
            text={turn.userMessage}
            active={i === active}
            focused={i === focused}
            onJump={jump}
          />
        ))}
      </div>
    </>
  )
}

// chatTurns changes identity on every stream flush; only the questions matter.
function sameRail(a, b) {
  for (const key of Object.keys(a)) {
    if (key !== "turns" && a[key] !== b[key]) return false
  }
  return (
    a.turns.length === b.turns.length &&
    a.turns.every(
      (t, i) =>
        t.id === b.turns[i].id && t.userMessage === b.turns[i].userMessage
    )
  )
}

export const TurnRail = memo(TurnRailImpl, sameRail)
