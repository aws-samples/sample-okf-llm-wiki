// The inline WIREFRAME for a canvas plan — rendered when the chat agent calls
// `canvas_skeleton` (lifted out of the thinking timeline into its own block,
// exactly like render_chart; see buildMessageBlocks). This is the
// requirements-capture surface: the user reacts to THIS card ("swap the pie
// for a bar", "add a season filter") before any authoring job is dispatched,
// so it renders from the tool call's ARGS the moment they stream — no result
// round-trip. Deliberately a sketch, not a preview: dashed tiles, no data —
// nothing has been validated against the dataset yet, and looking finished
// would overpromise.

import {
  AreaChartIcon,
  BarChart3Icon,
  ChartNoAxesCombinedIcon,
  FilterIcon,
  GaugeIcon,
  Grid3x3Icon,
  LayoutGridIcon,
  LineChartIcon,
  PencilRulerIcon,
  PieChartIcon,
  RadarIcon,
  ScatterChartIcon,
  SlidersHorizontalIcon,
  Table2Icon,
  WaypointsIcon,
} from "lucide-react"

// One glyph per widget-catalog type (okf_core canvas WIDGETS); unknown types
// (an off-catalog guess the tool will bounce) still sketch with the fallback.
const WIDGET_ICONS = {
  bar: BarChart3Icon,
  line: LineChartIcon,
  area: AreaChartIcon,
  pie: PieChartIcon,
  doughnut: PieChartIcon,
  polarArea: PieChartIcon,
  radar: RadarIcon,
  scatter: ScatterChartIcon,
  bubble: ScatterChartIcon,
  sankey: WaypointsIcon,
  treemap: LayoutGridIcon,
  table: Table2Icon,
  kpi: GaugeIcon,
  combo: ChartNoAxesCombinedIcon,
  pivot: Grid3x3Icon,
  heatmap: LayoutGridIcon,
  waterfall: BarChart3Icon,
  funnel: FilterIcon,
  gauge: GaugeIcon,
  histogram: BarChart3Icon,
  boxplot: BarChart3Icon,
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, Number(v) || 0))

export function CanvasSkeletonCard({ title, widgets, dials, pending, error }) {
  if (pending) {
    // The call is announced but its args are still streaming (the plan is the
    // long part) — hold the slot with a quiet pulse, like ChartFrame's theater.
    return (
      <div className="mt-1 mb-4 flex animate-pulse items-center gap-2 rounded-xl border border-edge bg-card px-3 py-3 text-sm text-muted-foreground">
        <PencilRulerIcon className="h-4 w-4 shrink-0" />
        Sketching the canvas plan…
      </div>
    )
  }
  if (error) {
    // The tool REFUSED this plan — never show it as a wireframe the user
    // could agree to; the agent re-sketches after fixing it.
    return (
      <div className="mt-1 mb-4 overflow-hidden rounded-xl border border-destructive/40 bg-card">
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <PencilRulerIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1 truncate text-sm font-medium">
            {title || "Canvas"}
          </div>
          <span className="rounded-md bg-destructive/10 px-1.5 py-0.5 text-[10px] font-medium text-destructive">
            Plan Refused
          </span>
        </div>
        <div className="px-3 py-2.5 text-xs text-muted-foreground">{error}</div>
      </div>
    )
  }
  const tiles = Array.isArray(widgets) ? widgets : []
  const chips = Array.isArray(dials) ? dials : []
  return (
    // mb-4 on top of the transcript's gap: the card is a bounded visual, and
    // prose starting close beneath it read as part of the card.
    <div className="mt-1 mb-4 overflow-hidden rounded-xl border border-edge bg-card">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <PencilRulerIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1 truncate text-sm font-medium">
          {title || "Canvas"}
        </div>
        <span className="rounded-md bg-foreground/5 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
          Plan
        </span>
      </div>
      {chips.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-b px-3 py-2">
          {chips.map((d, i) => (
            <span
              key={d?.name || i}
              className="inline-flex items-center gap-1 rounded-full bg-foreground/5 px-2 py-0.5 text-xs text-muted-foreground"
              title={d?.filters || undefined}
            >
              <SlidersHorizontalIcon className="h-3 w-3" />
              {d?.label || d?.name}
            </span>
          ))}
        </div>
      )}
      {/* The page's 12-column grid at wireframe scale (14px rows vs the live
          56px), so the plan's PROPORTIONS read true to the eventual canvas. */}
      <div
        className="grid gap-2 p-3"
        style={{ gridTemplateColumns: "repeat(12, minmax(0, 1fr))" }}
      >
        {tiles.map((w, i) => {
          const Icon = WIDGET_ICONS[w?.widget] || BarChart3Icon
          const span = clamp(w?.layout?.w ?? 6, 1, 12) || 6
          const rows = clamp(w?.layout?.h ?? 4, 1, 12) || 4
          return (
            <div
              key={i}
              className="flex flex-col gap-1 rounded-md border border-dashed border-border bg-muted/40 px-2.5 py-2"
              style={{
                gridColumn: `span ${span} / span ${span}`,
                minHeight: rows * 14,
              }}
            >
              <div className="flex items-center gap-1.5">
                <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <div className="truncate text-xs font-medium">{w?.title}</div>
              </div>
              {w?.represents && (
                <div className="line-clamp-2 text-[11px] leading-snug text-muted-foreground">
                  {w.represents}
                </div>
              )}
              <div className="mt-auto pt-1 text-[10px] uppercase tracking-wide text-muted-foreground/70">
                {w?.widget}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
