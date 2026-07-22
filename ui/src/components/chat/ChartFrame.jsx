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

// The "generating" placeholder shown before the chart reveals: a ghost bar
// chart breathing — faint axes and a handful of primary-tinted bars that
// slowly rise and fall, each on its own rhythm (.okf-chart-breathe in
// index.css). Absolutely positioned over the (still invisible) iframe so the
// reveal is an in-place cross-fade, not a layout jump; the bars freeze
// mid-breath as the placeholder fades out.
//
// Hand-tuned like the avatar's DOTS table: each bar's resting height (% of the
// ghost plot area), breath duration, and starting phase. The phase becomes a
// NEGATIVE animation-delay, so even the frozen (paused) field reads as varied
// bar heights, never a flat row.
const BREATHE_BARS = [
  { h: 66, dur: 2.4, phase: 0.55 },
  { h: 46, dur: 2.3, phase: 0.1 },
  { h: 72, dur: 2.7, phase: 0.45 },
  { h: 58, dur: 2.1, phase: 0.75 },
  { h: 86, dur: 2.9, phase: 0.25 },
  { h: 64, dur: 2.5, phase: 0.6 },
  { h: 50, dur: 2.2, phase: 0.9 },
  { h: 78, dur: 2.6, phase: 0.35 },
]

function ChartGenerating({ active }) {
  return (
    <div
      className="pointer-events-none absolute inset-0 transition-opacity duration-500"
      style={{ opacity: active ? 1 : 0 }}
      aria-hidden="true"
    >
      <div className={cn("okf-chart-breathe", active && "is-active")}>
        {BREATHE_BARS.map((b, i) => (
          <span
            key={i}
            style={{
              height: `${b.h}%`,
              animationDuration: `${b.dur}s`,
              animationDelay: `${(-(b.phase * b.dur)).toFixed(2)}s`,
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
  useEffect(() => {
    if (status !== "loading") return undefined
    const t = setTimeout(() => {
      setStatus((s) => (s === "loading" ? "error" : s))
    }, 6000)
    return () => clearTimeout(t)
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
    </div>
  )
}

// Public component: the boundary-wrapped chart frame. `code`/`title` come straight
// from the render_chart tool call's args (see buildMessageBlocks chart block).
// `live` = the block appeared mid-stream (drives the generating-animation hold);
// history-loaded charts pass false and plot immediately.
export function ChartFrame({ code, title, live = false }) {
  if (!code || typeof code !== "string") {
    return <ChartError title={title} message="chart had no code to run" />
  }
  return (
    <ChartBoundary title={title}>
      <ChartFrameInner code={code} title={title} live={live} />
    </ChartBoundary>
  )
}
