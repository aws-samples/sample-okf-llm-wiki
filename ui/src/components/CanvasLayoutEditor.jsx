// The canvas drag/resize layout editor (Cloudscape-board-style): the SAME 12
// columns / 56px rows / 12px gaps as the view grid, with live push-reflow
// while dragging and a corner resize handle. A SEPARATE, lazily-imported
// module on purpose: react-grid-layout (+ its CSS) loads only when the user
// clicks "Edit layout", and never rides the main view path or the CI render
// harness bundle (harness.jsx imports Tile from CanvasView).
//
// This edits GEOMETRY ONLY. The draft lives in CanvasPage; Save posts it
// through the layout-only patch route — data/encodings are structurally
// untouchable server-side (okf_core.apply_layout_patch).

import { GridLayout, useContainerWidth } from "react-grid-layout"

import { Tile } from "@/views/CanvasView.jsx"

import "react-grid-layout/css/styles.css"

export default function CanvasLayoutEditor({ tiles, tileState, layout, onChange }) {
  const { width, mounted, containerRef } = useContainerWidth()
  const byId = Object.fromEntries(layout.map((l) => [l.i, l]))
  return (
    <div ref={containerRef} className="min-h-0">
      {mounted && width > 0 ? (
        <GridLayout
          width={width}
          // The lint caps every dimension at the 12-unit grid (okf_core
          // _check_layout: 1 <= w,h <= 12). Without matching editor caps a
          // tile could be dragged taller than 12 rows, then Save 400s on a
          // layout the editor itself produced. The FLOOR is the caller's
          // (CanvasPage sets 2×2 — below that a tile is all chrome); 1 only
          // as the lint-parity fallback.
          layout={layout.map((l) => ({
            ...l,
            minW: l.minW ?? 1,
            minH: l.minH ?? 1,
            maxW: 12,
            maxH: 12,
          }))}
          gridConfig={{
            cols: 12,
            rowHeight: 56,
            margin: [12, 12],
            containerPadding: [0, 0],
          }}
          onLayoutChange={(l) =>
            onChange(l.map(({ i, x, y, w, h }) => ({ i, x, y, w, h })))
          }
        >
          {tiles.map((tile) => (
            <div
              key={tile.id}
              className="relative flex cursor-move [&>*]:min-w-0 [&>*]:flex-1"
            >
              {/* The REAL tile, live data included, sized by the draft layout
                  (so chart heights track a resize). positioned=false: the
                  wrapper owns geometry here, not the tile's grid styles. */}
              <Tile
                tile={{ ...tile, layout: { ...tile.layout, ...byId[tile.id] } }}
                state={tileState[tile.id]}
                positioned={false}
              />
              {/* Transparent shield over the tile: chart tiles host IFRAMES,
                  which swallow pointer events and kill a drag mid-gesture.
                  The resize handle sits above it (z-20, index.css). */}
              <div className="absolute inset-0 z-10" />
            </div>
          ))}
        </GridLayout>
      ) : null}
    </div>
  )
}
