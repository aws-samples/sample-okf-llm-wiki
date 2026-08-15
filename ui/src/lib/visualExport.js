// Export plumbing for the visuals kebab (components/VisualExportMenu): every
// chart and tile offers Copy to clipboard / Download SVG / Download PNG, and
// all three derive from ONE capture contract — a {dataUrl, width, height} PNG.
//
// Two capture paths feed that contract:
//   - Charts render inside opaque-origin sandboxed iframes (chartIframe.js),
//     so the parent can never touch their canvas. requestChartExport() asks
//     the frame over the existing postMessage channel; the frame re-renders
//     offscreen at export resolution and posts the dataURL back.
//   - HTML tiles (kpi / table / pivot) are captured from the live DOM with
//     html-to-image (computed-style inlining, so theme tokens come along).
//
// "SVG" is the PNG wrapped in an SVG envelope: Chart.js rasters to canvas, so
// a true vector export would need a different renderer entirely. The envelope
// carries the hi-res raster at the visual's CSS size — it scales cleanly in
// decks/docs and opens everywhere (foreignObject-style SVGs don't).

import { toPng } from "html-to-image"

// Correlates one export request with its frame's reply; concurrent exports
// (two tiles clicked fast) each get their own id + listener.
let exportSeq = 0

export function requestChartExport(iframe, { scale = 2, bg = "card", title = "" } = {}) {
  return new Promise((resolve, reject) => {
    const target = iframe?.contentWindow
    if (!target) {
      reject(new Error("The chart is still rendering"))
      return
    }
    const id = `exp-${++exportSeq}`
    const timer = setTimeout(() => {
      cleanup()
      reject(new Error("Chart export timed out"))
    }, 10000)
    function cleanup() {
      clearTimeout(timer)
      window.removeEventListener("message", onMessage)
    }
    function onMessage(e) {
      const d = e.data
      if (!d || d.source !== "okf-chart" || d.status !== "export" || d.id !== id) return
      if (e.source !== iframe.contentWindow) return // another frame's reply
      cleanup()
      if (d.dataUrl) resolve({ dataUrl: d.dataUrl, width: d.width, height: d.height })
      else reject(new Error(d.error || "Chart export failed"))
    }
    window.addEventListener("message", onMessage)
    target.postMessage({ source: "okf-chart-host", type: "export", id, scale, bg, title }, "*")
  })
}

// DOM capture for tiles that render as plain HTML. Elements marked
// data-export-exclude (the kebab itself, transient spinners) are filtered out
// of the clone — at capture time the pointer is still on the tile, so the
// hover-revealed button would otherwise land in the image.
export async function captureNodePng(node, { scale = 2 } = {}) {
  if (!node) throw new Error("Nothing to capture yet")
  const dataUrl = await toPng(node, {
    pixelRatio: scale,
    filter: (n) =>
      !(typeof n.hasAttribute === "function" && n.hasAttribute("data-export-exclude")),
  })
  return { dataUrl, width: node.offsetWidth, height: node.offsetHeight }
}

export function pngToSvg({ dataUrl, width, height }) {
  const w = Math.max(1, Math.round(width || 0))
  const h = Math.max(1, Math.round(height || 0))
  // href for modern viewers, xlink:href so older tooling resolves the image too.
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"` +
    ` width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
    `<image width="${w}" height="${h}" href="${dataUrl}" xlink:href="${dataUrl}"/></svg>`
  )
}

// Manual base64 decode rather than fetch(dataUrl) — the app CSP governs what
// fetch may touch, and a Blob needs no network round-trip anyway.
export function dataUrlToBlob(dataUrl) {
  const [head, b64] = String(dataUrl).split(",")
  const mime = /data:([^;]+)/.exec(head)?.[1] || "application/octet-stream"
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return new Blob([bytes], { type: mime })
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 5000)
}

export function downloadDataUrl(dataUrl, filename) {
  downloadBlob(dataUrlToBlob(dataUrl), filename)
}

export function downloadText(text, filename, type) {
  downloadBlob(new Blob([text], { type }), filename)
}

// Clipboard images need the ClipboardItem constructed SYNCHRONOUSLY in the
// user gesture with a Promise payload — Safari rejects a write that happens
// after an await, and this pattern is the cross-browser way around it.
export function copyPngToClipboard(getPng) {
  if (!navigator.clipboard?.write || typeof window.ClipboardItem === "undefined") {
    return Promise.reject(new Error("This browser can't copy images to the clipboard"))
  }
  const blob = Promise.resolve()
    .then(getPng)
    .then((png) => dataUrlToBlob(png.dataUrl))
  return navigator.clipboard.write([new ClipboardItem({ "image/png": blob })])
}

export function exportFilename(title) {
  const slug = String(title || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
  return slug || "visual"
}
