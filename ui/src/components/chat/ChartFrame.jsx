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

import { AlertTriangleIcon, BarChart3Icon } from "lucide-react"
import { Component, useEffect, useMemo, useRef, useState } from "react"

import { buildChartSrcdoc, resolveChartPalette } from "@/lib/chartIframe"

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

function ChartFrameInner({ code, title }) {
  const iframeRef = useRef(null)
  const [height, setHeight] = useState(280)
  const [status, setStatus] = useState("loading") // loading | ok | error
  const [errorMsg, setErrorMsg] = useState(null)

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

  return (
    // Inline with the answer — no card/border/background. The frame body is
    // transparent (see chartIframe.js), so the chart sits directly on the chat
    // surface like a paragraph, not a contained widget. Just vertical rhythm.
    <div className="my-3">
      {status === "loading" ? (
        <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
          <BarChart3Icon className="size-4 animate-pulse" />
          <span>Rendering {title ? `"${title}"` : "chart"}…</span>
        </div>
      ) : null}
      <iframe
        ref={iframeRef}
        title={title || "chart"}
        sandbox="allow-scripts"
        srcDoc={srcDoc}
        loading="lazy"
        // Hidden until the frame reports ok, so the loading row doesn't jump.
        style={{
          width: "100%",
          height: status === "ok" ? `${height}px` : 0,
          border: "0",
          display: "block",
          background: "transparent",
          colorScheme: "normal",
        }}
      />
    </div>
  )
}

// Public component: the boundary-wrapped chart frame. `code`/`title` come straight
// from the render_chart tool call's args (see buildMessageBlocks chart block).
export function ChartFrame({ code, title }) {
  if (!code || typeof code !== "string") {
    return <ChartError title={title} message="chart had no code to run" />
  }
  return (
    <ChartBoundary title={title}>
      <ChartFrameInner code={code} title={title} />
    </ChartBoundary>
  )
}
