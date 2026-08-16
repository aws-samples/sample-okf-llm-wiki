// Render harness for report charts — the page the chat runtime's headless
// Chromium drives at create_report time (services/chat/src/chat/report_render.py).
// The Python side injects window.__OKF_REPORT_CHARTS__ = [{spec, height}, ...]
// BEFORE navigation, waits for body[data-render-complete] ("true" = every frame
// settled clean), reads [data-okf-chart][data-error] for per-chart failures, and
// screenshots each [data-okf-chart='<index>'] container. Plain JS, no React —
// nothing here is interactive.
//
// index.css supplies the theme tokens resolveChartPalette reads off :root.
// The Python side drives TWO passes — window.__OKF_REPORT_THEME__ "light" then
// "dark" — and screenshots each with omit_background, so the composed report
// can show a theme-correct transparent PNG per app theme.
import "./index.css"

import { buildChartSrcdoc, resolveChartPalette } from "@/lib/chartIframe"

// The dark class must land BEFORE resolveChartPalette below reads the
// computed tokens off :root.
if (window.__OKF_REPORT_THEME__ === "dark") {
  document.documentElement.classList.add("dark")
}
// Nothing may paint behind the charts: index.css gives body the page
// background, which would bake into every screenshot — the PNGs must stay
// transparent so the report surface (light paper or dark panel) shows through.
document.documentElement.style.background = "transparent"
document.body.style.background = "transparent"

// 2x this width is the composed report's figure resolution (report_render.py
// screenshots at device_scale_factor 2 over a 760px content column).
const CHART_WIDTH = 800
const DEFAULT_HEIGHT = 340
// Frames that never settle (a hung plugin, a spec that renders nothing) must
// still release the Python wait — it treats any value but "true" as a failure.
const WATCHDOG_MS = 15_000

const charts = Array.isArray(window.__OKF_REPORT_CHARTS__)
  ? window.__OKF_REPORT_CHARTS__
  : []

const frames = [] // index-aligned with `charts`; entry i renders chart i
const done = charts.map(() => false)

function complete(signal) {
  // First writer wins: the watchdog must not overwrite a clean "true", and a
  // straggler frame must not flip a "timeout" back to success after the
  // Python side already read the signal.
  if (!document.body.dataset.renderComplete) {
    document.body.dataset.renderComplete = signal
  }
}

function settle(index) {
  if (done[index]) return
  done[index] = true
  if (done.every(Boolean)) complete("true")
}

// ChartFrame's listener, minus React: identify the reporting frame by its
// contentWindow (the sandbox is an opaque origin — no other handle exists),
// then ok/error settle it and "size" tracks the frame's reported height so the
// screenshot crops exactly what the frame drew (same 120–720 clamp).
window.addEventListener("message", (e) => {
  const data = e.data
  if (!data || data.source !== "okf-chart") return
  const index = frames.findIndex((f) => f && f.contentWindow === e.source)
  if (index === -1) return
  const container = frames[index].parentElement
  if (data.status === "size" && typeof data.height === "number") {
    container.style.height = `${Math.min(Math.max(data.height, 120), 720)}px`
  } else if (data.status === "ok") {
    settle(index)
  } else if (data.status === "error") {
    container.setAttribute(
      "data-error",
      typeof data.error === "string" ? data.error : "chart failed to render"
    )
    settle(index)
  }
})

const palette = resolveChartPalette()
const fontFamily = getComputedStyle(document.body).fontFamily

charts.forEach((entry, index) => {
  const height = Number(entry && entry.height) || DEFAULT_HEIGHT
  const container = document.createElement("div")
  container.setAttribute("data-okf-chart", String(index))
  container.style.width = `${CHART_WIDTH}px`
  container.style.height = `${height}px`
  container.style.background = "transparent"
  const iframe = document.createElement("iframe")
  iframe.setAttribute("sandbox", "allow-scripts")
  iframe.style.width = "100%"
  iframe.style.height = "100%"
  iframe.style.border = "0"
  iframe.style.display = "block"
  try {
    // The srcdoc's bootstrap neutralizes "</script" sequences in this code
    // string, so a spec containing one can't break out of the inline script.
    iframe.srcdoc = buildChartSrcdoc({
      code: `renderChart(el, ${JSON.stringify(entry ? entry.spec : null)})`,
      palette,
      fontFamily,
      height,
    })
  } catch (err) {
    container.setAttribute(
      "data-error",
      (err && err.message) || "failed to build the chart document"
    )
    settle(index)
  }
  container.appendChild(iframe)
  frames[index] = iframe
  document.body.appendChild(container)
})

if (charts.length === 0) complete("true")
setTimeout(() => complete("timeout"), WATCHDOG_MS)
