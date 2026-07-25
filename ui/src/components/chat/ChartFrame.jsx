// Renders one agent-authored chart inside a sandboxed iframe, inline in the chat.
//
// The agent's render_chart tool call carries `code` (JS calling renderChart(el,
// spec)) + a `title`. We build a frozen HTML document (see lib/chartIframe.js) with
// Chart.js inlined + the app palette injected, and load it into an
// <iframe sandbox="allow-scripts" srcdoc=…> (opaque origin — model JS can't reach
// our page or token), sizing the frame from the height it postMessages back.
//
// CSP note: a srcdoc (local-scheme) iframe INHERITS the embedding page's CSP, so
// the frame's inline scripts (Chart.js + the render code) run only because the app
// CSP allows 'unsafe-inline' in script-src (see infra/compute/ui.tf, where the
// trade-off is documented). The frame stays confined regardless: the sandbox gives
// it an opaque origin with no allow-same-origin, and its own <meta> CSP
// (default-src 'none'; connect-src 'none') denies all network — it only draws to a
// canvas and postMessages its height/status back.
//
// THREE layers of confinement, so a bad chart never harms the app:
//   1. the sandbox + the frame's own <meta> CSP (network denied, DOM isolated);
//   2. a status/error postMessage → a contained in-place error card, not a blank;
//   3. a React error boundary around this component → even a failure to BUILD the
//      document (or a React render throw) shows the fallback, never crashes the tree.
//
// Charts rebuild when the theme changes (the text/axis colors differ light vs
// dark) — driven by a MutationObserver on the <html> `dark` class (see below).

import { AlertTriangleIcon } from "lucide-react"
import { Component, useEffect, useMemo, useRef, useState } from "react"

import { buildChartSrcdoc, resolveChartPalette } from "@/lib/chartIframe"
import { cn } from "@/lib/utils"

// A compact, INLINE error note shown when a chart can't render (bad code, bad
// spec, or a frame-build failure). Kept chrome-light (no card/border) to match the
// inline chart treatment — just a muted line the reader can skim past. Never throws
// further; it's the safe fallback.
function ChartError({ title, message }) {
  return (
    <div className="my-3 flex items-start gap-2 text-sm text-muted-foreground">
      <AlertTriangleIcon className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0">
        <span>{title ? `Couldn't render "${title}"` : "Couldn't render chart"}</span>
        {message ? (
          <span className="ml-1 truncate text-xs opacity-80" title={message}>
            — {message}
          </span>
        ) : null}
      </div>
    </div>
  )
}

// Error boundary: if building the srcdoc throws, or anything in the inner frame
// component throws during render, show the contained error instead of unmounting
// the whole chat. This is the outermost of the three confinement layers.
class ChartBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, message: null }
  }
  static getDerivedStateFromError(error) {
    return { hasError: true, message: error?.message || "chart failed to render" }
  }
  componentDidCatch(error) {
    // Contained: log for debugging, don't rethrow.
    // eslint-disable-next-line no-console
    console.error("[ChartFrame] render error:", error)
  }
  render() {
    if (this.state.hasError) {
      return <ChartError title={this.props.title} message={this.state.message} />
    }
    return this.props.children
  }
}

// Read the currently-applied theme off <html> (the theme-provider toggles a
// `dark` class there). Used as the initial value + by the observer below.
function readResolvedTheme() {
  if (typeof document === "undefined") return "light"
  return document.documentElement.classList.contains("dark") ? "dark" : "light"
}

// The "generating" placeholder shown before the chart reveals: a halftone dot
// cloud that breathes like fabric under at most THREE roaming peaks. Each peak
// is a gaussian bump with its own random center, radius, and lifespan: it
// grows from nothing, blooms, then REGRESSES until its whole area fades back
// to the faint rest cloud — and the slot respawns somewhere else after a
// random beat. Dots never move; a small rAF driver writes scale + opacity per
// dot from the sum of the live peaks, so the fading logic is structural: a
// gaussian's falloff guarantees that around a peaking dot the neighbors are
// progressively smaller and fainter in every direction. The driver is seeded
// with Math.random() PER LAUNCH (peaks start mid-life at random phases and at
// random spots), so no two generations play the same show. The loop runs only
// while the theater is active — on reveal it stops and the field freezes
// mid-state for the cross-fade — and prefers-reduced-motion skips it
// entirely, leaving the static cloud.
//
// The dot GRID itself stays deterministic (module-level, sin-hash jitter, no
// Math.random) so renders/tests are stable: per dot, the elliptical falloff
// from the cloud's center drives base size + opacity (the halftone ramp), and
// edge dots below the alpha floor are skipped.
const SKETCH_MAX_PEAKS = 3
function buildSketchDots() {
  const COLS = 30
  const ROWS = 16
  const CX = 0.46 // cloud center (normalized), slightly left of middle
  const CY = 0.52
  const hash = (i) => {
    const s = Math.sin(i * 127.1 + 311.7) * 43758.5453
    return s - Math.floor(s) // deterministic 0..1
  }
  const dots = []
  for (let r = 0; r < ROWS; r++) {
    for (let c = 0; c < COLS; c++) {
      const i = r * COLS + c
      const nx = (c + 0.5) / COLS
      const ny = (r + 0.5) / ROWS
      const d = Math.hypot((nx - CX) / 0.52, (ny - CY) / 0.5)
      const base = Math.max(0, 1 - d) ** 1.4 * (0.85 + 0.3 * hash(i * 3 + 1))
      if (base < 0.06) continue // skip dots the falloff fully dissolved
      dots.push({
        nx,
        ny,
        left: `${(nx * 100).toFixed(2)}%`,
        top: `${(ny * 100 + (hash(i * 7 + 3) - 0.5) * 1.6).toFixed(2)}%`,
        size: +(1.8 + 2.6 * base).toFixed(2),
        base: +(0.2 + 0.6 * base).toFixed(3),
      })
    }
  }
  return dots
}
const SKETCH_DOTS = buildSketchDots()

// Rest state (no peak nearby): dots sit small and faint but present, so the
// cloud never blanks between peaks. A peak lifts a dot toward full size/alpha.
const SKETCH_REST_SCALE = 0.55
const SKETCH_REST_ALPHA = 0.22

function ChartGenerating({ active }) {
  const fieldRef = useRef(null)

  useEffect(() => {
    if (!active) return undefined // reveal started: freeze the field mid-state
    const el = fieldRef.current
    if (!el) return undefined
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches) {
      return undefined // static cloud only
    }
    const spans = Array.from(el.children) // aligned 1:1 with SKETCH_DOTS

    // One peak slot: a gaussian bump living `dur` ms. `born` in the future is
    // the between-peaks beat; the FIRST generation is back-dated so the show
    // opens mid-life at random phases instead of three synchronized births.
    const newPeak = (first) => ({
      x: 0.18 + Math.random() * 0.58, // keep centers in the cloud's meat
      y: 0.22 + Math.random() * 0.56,
      sigma: 0.08 + Math.random() * 0.06,
      dur: 2600 + Math.random() * 1900,
      born: performance.now() + (first ? -Math.random() * 2600 : 400 + Math.random() * 1100),
    })
    const peaks = Array.from({ length: SKETCH_MAX_PEAKS }, () => newPeak(true))

    let raf
    let last = 0
    const tick = (now) => {
      raf = requestAnimationFrame(tick)
      if (now - last < 33) return // ~30fps is plenty for slow swells
      last = now
      for (let k = 0; k < peaks.length; k++) {
        if (now - peaks[k].born > peaks[k].dur) peaks[k] = newPeak(false)
      }
      for (let i = 0; i < spans.length; i++) {
        const d = SKETCH_DOTS[i]
        let v = 0
        for (const p of peaks) {
          const t = (now - p.born) / p.dur
          if (t <= 0 || t >= 1) continue
          const env = Math.sin(Math.PI * t) ** 2 // grow → bloom → regress to 0
          const dx = d.nx - p.x
          const dy = (d.ny - p.y) * 1.35 // wide card: weight y so bumps stay round
          v += env * Math.exp(-(dx * dx + dy * dy) / (2 * p.sigma * p.sigma))
        }
        if (v > 1) v = 1
        const s = spans[i].style
        s.transform = `scale(${(SKETCH_REST_SCALE + (1.35 - SKETCH_REST_SCALE) * v).toFixed(3)})`
        // Aggressive shading: the halftone base only sets the REST look; a
        // peak lifts any dot it covers all the way to opacity 1 — full
        // button-cyan at the crest — with the gaussian grading the ring.
        const rest = d.base * SKETCH_REST_ALPHA
        s.opacity = (rest + (1 - rest) * v).toFixed(3)
      }
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [active])

  return (
    <div
      className="pointer-events-none absolute inset-0 transition-opacity duration-500"
      style={{ opacity: active ? 1 : 0 }}
      aria-hidden="true"
    >
      <div ref={fieldRef} className="okf-chart-sketch">
        {SKETCH_DOTS.map((d, i) => (
          <span
            key={i}
            className="okf-sketch-dot"
            style={{
              left: d.left,
              top: d.top,
              width: `${d.size}px`,
              height: `${d.size}px`,
              opacity: d.base * SKETCH_REST_ALPHA,
              transform: `scale(${SKETCH_REST_SCALE})`,
            }}
          />
        ))}
      </div>
    </div>
  )
}
// How long the generating animation holds on a LIVE turn before the chart is
// allowed to reveal. The code arrives whole and the frame draws in tens of ms,
// so without this beat the skeleton would just flash.
const MIN_GENERATING_MS = 900

function ChartFrameInner({ code, title, live }) {
  const iframeRef = useRef(null)
  // Default matches what the frame will report (#chartbox 340px + 8px wrap
  // padding, see chartIframe.js) so the placeholder footprint == the reveal.
  const [height, setHeight] = useState(348)
  const [status, setStatus] = useState("loading") // loading | ok | error
  const [errorMsg, setErrorMsg] = useState(null)

  // "Theater" = this chart mounted mid-generation (a live streaming turn). Only
  // then does the generating grid show and hold for its minimum beat; a
  // history-loaded chart (live=false at mount) renders NO placeholder at all —
  // just the reserved space, with the chart fading in when its frame reports ok.
  // Mount-time capture on purpose — `live` flipping later (turn finishing) must
  // not add or remove the theater mid-hold.
  const [theater] = useState(() => Boolean(live))
  const [minElapsed, setMinElapsed] = useState(() => !live)
  useEffect(() => {
    if (minElapsed) return undefined
    const t = setTimeout(() => setMinElapsed(true), MIN_GENERATING_MS)
    return () => clearTimeout(t)
  }, [minElapsed])

  // The chart's TEXT/axis/grid colors are resolved from the app theme tokens
  // (--foreground/--border) at srcdoc-build time, so the frame must REBUILD when
  // light/dark flips. We can't derive that from useTheme() alone: its value is
  // often "system", which doesn't change when the OS appearance (or the applied
  // `dark` class) flips — and the class is toggled in a post-render effect, so
  // reading it inline during render is stale. Instead, OBSERVE the <html> class
  // with a MutationObserver and drive a state var, so a theme change re-renders
  // this component AFTER the class actually flips → srcDoc rebuilds with the
  // correct light/dark colors. (This is the "unreadable in light mode" fix: the
  // chart kept dark-mode white text on the light page.)
  const [themeSig, setThemeSig] = useState(readResolvedTheme)

  useEffect(() => {
    if (typeof document === "undefined") return undefined
    const root = document.documentElement
    const sync = () => setThemeSig(readResolvedTheme())
    sync() // catch a flip that happened between initial state + effect attach
    const obs = new MutationObserver(sync)
    obs.observe(root, { attributes: true, attributeFilter: ["class"] })
    // Also track OS-level changes while the app is in "system" mode (the class
    // may not change synchronously with the media query on some setups).
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)")
    mq?.addEventListener?.("change", sync)
    return () => {
      obs.disconnect()
      mq?.removeEventListener?.("change", sync)
    }
  }, [])

  // Build the frozen chart document. Memoized on the code + theme so it only
  // rebuilds when the chart or the palette actually changes (not on every parent
  // re-render — the chat re-renders per streamed token). If buildChartSrcdoc throws,
  // the boundary catches it.
  const srcDoc = useMemo(() => {
    if (!code) return null // pending: no document until the args arrive
    const palette = resolveChartPalette()
    const fontFamily =
      typeof document !== "undefined"
        ? getComputedStyle(document.body).fontFamily
        : "system-ui, sans-serif"
    return buildChartSrcdoc({ code, palette, fontFamily })
    // themeSig is a dep on purpose: a theme flip must rebuild with new colors.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code, themeSig])

  // Reset transient status when we (re)build the frame.
  useEffect(() => {
    setStatus("loading")
    setErrorMsg(null)
  }, [srcDoc])

  // Receive height/status/error from THIS frame only (match on contentWindow —
  // works for the opaque-origin sandbox). Ignore messages from other chart frames.
  useEffect(() => {
    function onMessage(e) {
      const data = e.data
      if (!data || data.source !== "okf-chart") return
      if (iframeRef.current && e.source !== iframeRef.current.contentWindow) return
      if (data.status === "size" && typeof data.height === "number") {
        // Clamp to a sane range so a runaway layout can't push a giant frame.
        setHeight(Math.min(Math.max(data.height, 120), 720))
      } else if (data.status === "ok") {
        setStatus("ok")
      } else if (data.status === "error") {
        setStatus("error")
        setErrorMsg(typeof data.error === "string" ? data.error : null)
      }
    }
    window.addEventListener("message", onMessage)
    return () => window.removeEventListener("message", onMessage)
  }, [])

  // A frame that never reports back (blocked/blank) shouldn't spin forever — after
  // a grace period with no "ok"/"error", treat it as failed so the user sees the
  // contained error rather than an empty box.
  // The grace clock only runs while the tab is VISIBLE: hidden tabs pause the
  // frame's rAF and throttle its timers (Chrome: up to a minute), so a chart
  // mounted while the user is away would otherwise be declared dead before it
  // ever had a frame to report in — the "chart could not be generated (but
  // renders after refresh)" symptom.
  useEffect(() => {
    if (status !== "loading" || !srcDoc) return undefined
    let t = null
    const fail = () => setStatus((s) => (s === "loading" ? "error" : s))
    const arm = () => {
      if (t == null && !document.hidden) t = setTimeout(fail, 6000)
    }
    const disarm = () => {
      if (t != null) {
        clearTimeout(t)
        t = null
      }
    }
    const onVisibility = () => (document.hidden ? disarm() : arm())
    arm()
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      disarm()
      document.removeEventListener("visibilitychange", onVisibility)
    }
  }, [status, srcDoc])

  if (status === "error") {
    return <ChartError title={title} message={errorMsg} />
  }

  // Revealed = the frame has drawn AND the live minimum beat has passed. The
  // wrapper reserves the chart's footprint (the frame's reported height; 348
  // default) from the very start, so the generating grid sits exactly where
  // the chart will plot and the reveal is a cross-fade — no layout jump.
  const showChart = status === "ok" && minElapsed

  return (
    // Inline with the answer — no card/border/background. The frame body is
    // transparent (see chartIframe.js), so the chart sits directly on the chat
    // surface like a paragraph, not a contained widget. Just vertical rhythm.
    <div
      className="relative my-3"
      style={{ height: `${height}px`, transition: "height 0.4s ease" }}
    >
      {theater ? <ChartGenerating active={!showChart} /> : null}
      {srcDoc == null ? null : (
      <iframe
        ref={iframeRef}
        title={title || "chart"}
        sandbox="allow-scripts"
        srcDoc={srcDoc}
        loading="lazy"
        // Kept mounted at full size (it must load + draw to report ok) but
        // transparent until reveal; the placeholder floats above it meanwhile.
        style={{
          width: "100%",
          height: "100%",
          border: "0",
          display: "block",
          background: "transparent",
          colorScheme: "normal",
          opacity: showChart ? 1 : 0,
          transform: showChart ? "none" : "translateY(6px) scale(0.99)",
          transition: "opacity 0.5s ease, transform 0.5s ease",
        }}
      />
      )}
    </div>
  )
}

// Public component: the boundary-wrapped chart frame. `code`/`title` come straight
// from the render_chart tool call's args (see buildMessageBlocks chart block).
// `live` = the block appeared mid-stream (drives the generating-animation hold);
// history-loaded charts pass false and plot immediately.
export function ChartFrame({ code, title, live = false, pending = false }) {
  const codeStr = typeof code === "string" ? code : ""
  if (!codeStr && !pending) {
    return <ChartError title={title} message="chart had no code to run" />
  }
  // PENDING (no code yet — the model announced the call but is still
  // generating its args): render the SAME inner frame with empty code. The
  // element tree is identical to the filled state, so when the code lands
  // React keeps the ChartFrameInner INSTANCE — the generating theater mounts
  // exactly once and runs uninterrupted through pending → code → reveal (no
  // re-animation at the payload seam). The inner frame builds no iframe until
  // code exists.
  return (
    <ChartBoundary title={title}>
      <ChartFrameInner code={codeStr} title={title} live={live} />
    </ChartBoundary>
  )
}
