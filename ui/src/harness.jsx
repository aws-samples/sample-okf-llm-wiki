// Canvas CI render harness entry (see harness.html). Renders the REAL tile
// components (CanvasView's Tile — same binder, same theme CSS) from
// window-injected data, then signals completion via
// body[data-render-complete='true'] once every chart iframe loaded and pixels
// settled — that's what the Playwright renderer waits on before screenshotting.

import { StrictMode, useEffect } from "react"
import { createRoot } from "react-dom/client"

import { Tile } from "@/views/CanvasView.jsx"

import "./index.css"

const payload = window.__CANVAS__ || { config: {}, results: {}, dials: {} }
const theme = window.__THEME__ === "dark" ? "dark" : "light"
document.documentElement.classList.toggle("dark", theme === "dark")

function Harness() {
  const tiles = payload.config?.tiles || []

  useEffect(() => {
    // Completion: every chart iframe fired load (or 5s cap), plus a settle
    // beat for layout/fonts. Charts render animation-free (harness.html kills
    // motion), so one settle frame after load is enough.
    const cap = new Promise((resolve) => setTimeout(resolve, 5000))
    const frames = Array.from(document.querySelectorAll("iframe")).map(
      (f) =>
        new Promise((resolve) => {
          f.addEventListener("load", resolve, { once: true })
          setTimeout(resolve, 4000)
        })
    )
    Promise.race([Promise.all(frames), cap]).then(() =>
      setTimeout(() => {
        document.body.dataset.renderComplete = "true"
      }, 800)
    )
  }, [])

  return (
    <div className="bg-background p-4 text-foreground">
      {/* Mirror CanvasPage's width constraint (the benchmark-column cap):
          tile PROPORTIONS in the screenshots must match what users get —
          a full-viewport grid runs ~10% wider, exactly the margin where
          label-collision judgments flip. Page chrome (header, dial bar) is
          deliberately absent: constant, and not tile-quality signal. */}
      <div className="mx-auto w-full max-w-[74.25rem]">
        <div
          className="grid gap-3"
          style={{
            gridTemplateColumns: "repeat(12, minmax(0, 1fr))",
            gridAutoRows: "56px",
          }}
        >
          {tiles.map((tile) => {
            // A tile whose execution failed renders its error text (the
            // Canvas page's error state) — never a silently blank card the
            // agent would misread as a layout problem.
            const r = payload.results?.[tile.id]
            return (
              <Tile
                key={tile.id}
                tile={tile}
                state={
                  r?.error
                    ? { error: r.error }
                    : { result: { executed: true, ...r } }
                }
              />
            )
          })}
        </div>
      </div>
    </div>
  )
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <Harness />
  </StrictMode>
)
