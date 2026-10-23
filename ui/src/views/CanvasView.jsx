// Canvas (dashboards): the per-user list + the live dial-driven canvas page.
//
// A canvas is agent-authored CONFIG (okf_core/canvas.py): tiles
// bind Attested Computation parameters to dashboard-level dials; loading the
// page executes every tile through the control API's tile-run path (the same
// receipt surface as the computation Run modal) and binds results into the
// declarative renderChart spec via canvasBind — no stored code ever runs.
// Dial CONTROL types derive from the computation contracts (enum → select,
// min/max → bounded number, date → date input, timestamp → datetime-local,
// boolean → switch), fetched per computation.

import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowLeftIcon,
  BadgeCheckIcon,
  CalendarIcon,
  CheckIcon,
  ChevronDownIcon,
  LayoutDashboardIcon,
  Loader2Icon,
  MoveIcon,
  PencilIcon,
  PlayIcon,
  SlidersHorizontalIcon,
  Trash2Icon,
} from "lucide-react"

import { toast } from "sonner"

import { ChartFrame } from "@/components/chat/ChartFrame"
import { ValueInput } from "@/components/ValueInput"
import { VisualExportMenu } from "@/components/VisualExportMenu"
import { captureNodePng } from "@/lib/visualExport"
import { Button } from "@/components/ui/button"
import { Calendar } from "@/components/ui/calendar"
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { asNumber, bindChart, formatValue, isChart, shapeResult } from "@/lib/canvasBind"
import { cn } from "@/lib/utils"

// Loaded only when the user clicks "Edit layout" — react-grid-layout stays
// out of the main bundle (and the CI harness bundle, which imports Tile).
const CanvasLayoutEditor = lazy(() => import("@/components/CanvasLayoutEditor.jsx"))

function StatusBadge({ status }) {
  const styles = {
    verified:
      "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 ring-emerald-500/20",
    created: "bg-amber-500/10 text-amber-600 dark:text-amber-400 ring-amber-500/20",
  }
  // DISPLAY mapping only — the stored status contract stays created|verified.
  // "created" describes the row's history; what the human deciding whether to
  // trust the dashboard needs to read is that nobody has attested it yet.
  const label = status === "verified" ? "Verified" : "Unverified"
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ring-1 ${styles[status] || styles.created}`}
    >
      {status === "verified" && <BadgeCheckIcon className="h-3.5 w-3.5" />}
      {label}
    </span>
  )
}

// Mirrors okf_core.canvas.MAX_TITLE — the lint's cap on config titles; the
// rename route enforces the same server-side.
const TITLE_MAX = 120

// The page title with a hover pen: click to edit inline. One exit path —
// everything funnels through blur (Enter blurs to save, Escape flags a
// cancel first), so there's no save/cancel race when the input unmounts.
function EditableTitle({ title, onRename }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(title)
  const [saving, setSaving] = useState(false)
  const cancelled = useRef(false)

  if (editing) {
    return (
      <Input
        autoFocus
        value={draft}
        maxLength={TITLE_MAX}
        disabled={saving}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur()
          else if (e.key === "Escape") {
            cancelled.current = true
            e.currentTarget.blur()
          }
        }}
        onBlur={() => {
          if (cancelled.current) {
            cancelled.current = false
            setEditing(false)
            return
          }
          const next = draft.trim()
          if (!next || next === title) {
            setEditing(false)
            return
          }
          setSaving(true)
          onRename(next)
            .then(() => setEditing(false))
            .catch((err) => toast.error(err?.message || "Couldn't rename the canvas"))
            .finally(() => setSaving(false))
        }}
        className="h-8 w-80 text-lg font-semibold"
      />
    )
  }
  return (
    <div className="group/title flex min-w-0 items-center gap-1.5">
      <h1 className="truncate text-lg font-semibold">{title}</h1>
      <Button
        variant="ghost"
        size="icon"
        aria-label="Rename canvas"
        className="size-6 shrink-0 text-muted-foreground opacity-0 group-hover/title:opacity-100 focus-visible:opacity-100"
        onClick={() => {
          setDraft(title)
          setEditing(true)
        }}
      >
        <PencilIcon className="size-3.5" />
      </Button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Dials
// ---------------------------------------------------------------------------

// Contract dates are ISO strings; construct LOCAL dates by hand —
// `new Date("YYYY-MM-DD")` parses as UTC and shifts a day west of Greenwich.
const isoToDate = (s) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || "").trim())
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : undefined
}
const dateToIso = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`

/** The shadcn date picker (Popover + Calendar) for date dials; `withTime`
 * (timestamp dials) adds a time input under the calendar and emits the
 * backend's canonical space-form ISO (`YYYY-MM-DD HH:MM`). The contract's
 * min/max become disabled calendar ranges (date-part granularity). */
function DateDialControl({ value, param, onChange, withTime }) {
  const [open, setOpen] = useState(false)
  const raw = String(value || "").replace("T", " ").trim()
  const datePart = raw.slice(0, 10)
  const timePart = withTime ? raw.slice(11, 16) || "00:00" : ""
  const selected = isoToDate(datePart)
  const bounds = {}
  if (param?.min) bounds.before = isoToDate(param.min)
  if (param?.max) bounds.after = isoToDate(param.max)
  const label = selected
    ? selected.toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      }) + (withTime ? ` · ${timePart}` : "")
    : "Pick a date"
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" className="justify-start font-normal">
          <CalendarIcon className="h-4 w-4 text-muted-foreground" />
          {label}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-auto p-0" align="start">
        <Calendar
          mode="single"
          selected={selected}
          defaultMonth={selected}
          disabled={bounds.before || bounds.after ? bounds : undefined}
          onSelect={(d) => {
            if (!d) return
            onChange(withTime ? `${dateToIso(d)} ${timePart}` : dateToIso(d))
            if (!withTime) setOpen(false)
          }}
        />
        {withTime && (
          <div className="border-t p-2">
            <Input
              type="time"
              className="w-full"
              value={timePart}
              onChange={(e) =>
                onChange(
                  `${datePart || dateToIso(new Date())} ${e.target.value || "00:00"}`
                )
              }
            />
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}

// Past this many options, scanning a plain dropdown loses to typing — the
// closed enum upgrades to the shadcn combobox (searchable Command list).
const ENUM_SEARCH_THRESHOLD = 8

/** Closed-enum picker. Enum values may be typed (integers); Radix speaks
 * strings — display as strings, and the run path's normalize_value coerces
 * back per contract. Selection stays CLOSED either way: the search filters
 * the declared values, it never accepts free text (that's the open
 * `column`-domain path). */
function EnumDialControl({ param, value, onChange }) {
  const [open, setOpen] = useState(false)
  const values = param.enum.map(String)
  if (values.length <= ENUM_SEARCH_THRESHOLD) {
    return (
      <Select value={String(value)} onValueChange={onChange}>
        <SelectTrigger className="w-fit min-w-28">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {values.map((v) => (
            <SelectItem key={v} value={v}>
              {v}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  }
  const current = String(value ?? "")
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" className="w-44 justify-between font-normal">
          <span className="truncate">{current || "Pick a value"}</span>
          <ChevronDownIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-56 p-0" align="start">
        <Command>
          <CommandInput placeholder="Search…" />
          <CommandList>
            <CommandEmpty>No match.</CommandEmpty>
            {values.map((v) => (
              <CommandItem
                key={v}
                value={v}
                onSelect={(val) => {
                  onChange(val)
                  setOpen(false)
                }}
              >
                <CheckIcon
                  className={cn("h-4 w-4", v === current ? "opacity-100" : "opacity-0")}
                />
                {v}
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

/** The contract-derived control for one dial. `param` is the bound
 * computation parameter (from getComputation), or null while contracts load —
 * the fallback input still works, typed off the default value. */
function DialControl({ param, domain, value, onChange }) {
  const type = param?.type
  if (param?.enum?.length) {
    return <EnumDialControl param={param} value={value} onChange={onChange} />
  }
  if (type === "integer" || type === "number" || typeof value === "number") {
    return (
      <Input
        type="number"
        className="w-28"
        value={value}
        min={param?.min}
        max={param?.max}
        step={type === "integer" ? 1 : "any"}
        onChange={(e) => {
          // An emptied field must NOT become a value: parseInt("") is NaN
          // (serialized null -> server 400 on every tile) and Number("") is
          // 0 — a number the user never typed, silently executed. Store ""
          // so the field reads empty and Apply refuses until it's filled.
          const raw = e.target.value
          if (raw === "") {
            onChange("")
            return
          }
          const n = type === "integer" ? parseInt(raw, 10) : Number(raw)
          onChange(Number.isFinite(n) ? n : "")
        }}
      />
    )
  }
  if (type === "date" || type === "timestamp") {
    return (
      <DateDialControl
        value={value}
        param={param}
        onChange={onChange}
        withTime={type === "timestamp"}
      />
    )
  }
  if (type === "boolean" || typeof value === "boolean") {
    const on = value === true || String(value).toLowerCase() === "true"
    return (
      <div className="flex h-8 items-center">
        <Switch checked={on} onCheckedChange={onChange} />
      </div>
    )
  }
  if (domain?.values?.length) {
    // Column-bound parameter with profiled evidence: the Run modal's
    // advisory type-ahead — observed values suggested, free text legal (the
    // executor warns-and-runs on a miss).
    return (
      <ValueInput
        value={String(value ?? "")}
        onChange={onChange}
        domain={domain}
        className="h-8 w-44 text-sm"
      />
    )
  }
  return (
    <Input
      type="text"
      className="w-36"
      value={String(value)}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

// ---------------------------------------------------------------------------
// Tiles
// ---------------------------------------------------------------------------

// Channel field lists: the lint accepts BOTH `{fields: [...]}` and the
// single `{field: "..."}` form, and save_canvas stores the raw config — so
// every renderer must bridge both (KpiTile and canvasBind already did;
// PivotTile/TableTile silently rendered nothing for the `field` form).
const channelFields = (enc) => enc?.fields || (enc?.field ? [enc.field] : [])

function KpiTile({ tile, result }) {
  const { columns, rows } = shapeResult(tile, result)
  const idx = (f) =>
    columns.findIndex((c) => String(c).toLowerCase() === String(f).toLowerCase())
  const enc = tile.encoding?.value
  const field = enc?.fields?.[0] || enc?.field
  const value = rows[0]?.[idx(field)]
  // The catalog's optional `delta` channel — the period-over-period change
  // next to the headline number. Sign colors follow the convention (up
  // emerald / down red); the number itself comes from the SQL untouched.
  const dEnc = tile.encoding?.delta
  const dField = dEnc?.fields?.[0] || dEnc?.field
  const dRaw = dField ? rows[0]?.[idx(dField)] : undefined
  const dNum = asNumber(dRaw)
  return (
    <div className="flex h-full flex-col items-start justify-center px-4">
      <div className="text-3xl font-semibold tracking-tight">
        {formatValue(value, enc?.format)}
      </div>
      {dField && dRaw !== undefined && dRaw !== null && (
        <div
          className={
            "mt-1 flex items-center gap-1 text-xs font-medium " +
            (dNum === null || dNum === 0
              ? "text-muted-foreground"
              : dNum > 0
                ? "text-emerald-600 dark:text-emerald-400"
                : "text-red-600 dark:text-red-400")
          }
        >
          {dNum !== null && dNum !== 0 && (dNum > 0 ? "▲" : "▼")}
          {formatValue(dRaw, dEnc?.format)}
          {dEnc?.label && (
            <span className="font-normal text-muted-foreground">{dEnc.label}</span>
          )}
        </div>
      )}
      {enc?.label && (
        <div className="mt-1 text-xs text-muted-foreground">{enc.label}</div>
      )}
    </div>
  )
}

function TableTile({ tile, result }) {
  const { columns, rows } = shapeResult(tile, result)
  const wanted = channelFields(tile.encoding?.columns)
  const idx = (wanted.length ? wanted : columns).map((f) =>
    columns.findIndex((c) => String(c).toLowerCase() === String(f).toLowerCase())
  )
  const heads = (wanted.length ? wanted : columns).filter((_, k) => idx[k] >= 0)
  return (
    <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-card text-left text-xs text-muted-foreground">
          <tr>
            {heads.map((h) => (
              <th key={h} className="px-2 py-1.5 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className="border-t border-border/60">
              {idx
                .filter((i) => i >= 0)
                .map((i, k) => (
                  <td key={k} className="px-2 py-1.5">
                    {row[i] === null || row[i] === undefined ? "—" : String(row[i])}
                  </td>
                ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// Conditional-formatting fills (pivot `rules`): theme-safe literal tints —
// first matching rule wins; values outside every rule stay unpainted.
const RULE_BG = {
  emerald: "rgba(16,185,129,0.16)",
  amber: "rgba(245,158,11,0.18)",
  red: "rgba(239,68,68,0.16)",
  sky: "rgba(14,165,233,0.16)",
  muted: "rgba(128,128,128,0.14)",
}

function ruleBg(rules, field, value) {
  const n = asNumber(value)
  if (n === null) return undefined
  for (const r of rules || []) {
    if (r.field !== field) continue
    if (r.min !== undefined && n < r.min) continue
    if (r.max !== undefined && n > r.max) continue
    return RULE_BG[r.color]
  }
  return undefined
}

// Pivot: long rows -> a matrix. TOTALS COME FROM THE SQL (GROUP BY ROLLUP):
// a row whose group column(s) are NULL is a subtotal/total row and renders
// styled as one — this tile lays out and formats, it NEVER sums cells.
function PivotTile({ tile, result }) {
  const { columns, rows } = shapeResult(tile, result)
  const rowFields = channelFields(tile.encoding?.rows)
  const colField = tile.encoding?.columns?.fields?.[0] || tile.encoding?.columns?.field
  const valFields = channelFields(tile.encoding?.values)
  const vFormat = tile.encoding?.values?.format
  const rules = tile.options?.rules
  const idx = (f) => columns.findIndex((c) => String(c).toLowerCase() === String(f).toLowerCase())
  const rIdx = rowFields.map(idx)
  const cIdx = colField ? idx(colField) : -1
  const vIdx = valFields.map(idx)

  // First-seen distinct pivot-column values (ROLLUP nulls become "Total").
  const colVals = []
  let hasColTotal = false
  if (cIdx >= 0)
    for (const r of rows) {
      const v = r[cIdx]
      if (v === null || v === undefined) hasColTotal = true
      else if (!colVals.includes(String(v))) colVals.push(String(v))
    }
  const colKeys = cIdx >= 0 ? [...colVals, ...(hasColTotal ? [null] : [])] : [null]

  // Group by the rows-tuple; each group holds one result row per column key.
  const groups = []
  const seen = new Map()
  for (const r of rows) {
    const keyVals = rIdx.map((i) => (i >= 0 ? r[i] : null))
    const key = JSON.stringify(keyVals.map((v) => (v === undefined ? null : v)))
    let g = seen.get(key)
    if (!g) {
      g = { keyVals, isTotal: keyVals.some((v) => v === null || v === undefined), cells: new Map() }
      seen.set(key, g)
      groups.push(g)
    }
    const cv = cIdx >= 0 ? r[cIdx] : null
    g.cells.set(cv === null || cv === undefined ? null : String(cv), r)
  }

  const headFor = (ck, vf) => {
    const col = ck === null ? (cIdx >= 0 ? "Total" : "") : ck
    return valFields.length > 1 && col ? `${col} · ${vf}` : col || vf
  }
  return (
    <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-card text-left text-xs text-muted-foreground">
          <tr>
            {rowFields.map((f) => (
              <th key={f} className="px-2 py-1.5 font-medium whitespace-nowrap">{f}</th>
            ))}
            {colKeys.flatMap((ck) =>
              valFields.map((vf) => (
                <th
                  key={`${ck}·${vf}`}
                  className="px-2 py-1.5 text-right font-medium whitespace-nowrap"
                >
                  {headFor(ck, vf)}
                </th>
              ))
            )}
          </tr>
        </thead>
        <tbody>
          {groups.map((g, gi) => (
            <tr
              key={gi}
              className={
                g.isTotal
                  ? "border-t border-border/60 bg-foreground/5 font-medium"
                  : "border-t border-border/60"
              }
            >
              {g.keyVals.map((v, i) => {
                const firstNull =
                  (v === null || v === undefined) &&
                  g.keyVals.findIndex((x) => x === null || x === undefined) === i
                return (
                  <td key={i} className="px-2 py-1.5">
                    {v === null || v === undefined ? (firstNull ? "Total" : "") : String(v)}
                  </td>
                )
              })}
              {colKeys.flatMap((ck) =>
                valFields.map((vf, j) => {
                  const row = g.cells.get(ck)
                  const v = row && vIdx[j] >= 0 ? row[vIdx[j]] : null
                  return (
                    <td
                      key={`${ck}·${vf}`}
                      className="px-2 py-1.5 text-right tabular-nums"
                      style={{ backgroundColor: ruleBg(rules, vf, v) }}
                    >
                      {v === null || v === undefined
                        ? "—"
                        : formatValue(v, vFormat || "number")}
                    </td>
                  )
                })
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// The tile's cell in a 12-column grid. Configs store FULL placement
// ({x, y, w, h} — lint-required), and it must be honored: flow order only
// coincidentally matches when the author emits tiles in reading order, and
// the user can now rearrange tiles by hand (explicit x/y is what persists).
// Span-only fallback keeps layout-less tiles (previews) rendering.
export function tileGridStyle(layout) {
  const l = layout || {}
  const w = l.w || 6
  const h = l.h || 4
  return {
    gridColumn: Number.isInteger(l.x) ? `${l.x + 1} / span ${w}` : `span ${w} / span ${w}`,
    gridRow: Number.isInteger(l.y) ? `${l.y + 1} / span ${h}` : `span ${h} / span ${h}`,
  }
}

// Exported for the CI render harness (src/harness.jsx) — the screenshots the
// authoring agent inspects render THIS component, so what it judges is what
// users get. `positioned=false` lets the layout editor's wrapper own
// geometry (grid placement styles would leak into its flex wrapper).
export function Tile({ tile, state, positioned = true }) {
  const { loading, error, result } = state || {}
  const cardRef = useRef(null)
  const chartExportRef = useRef(null)
  const chart = isChart(tile.widget)
  // The export kebab (hover-revealed, top right): chart tiles export through
  // their sandboxed frame (the frame re-renders offscreen, injecting the tile
  // title the card header carries); HTML tiles (kpi/table/pivot) capture the
  // whole card from the DOM. Hidden while there's nothing to export and in
  // the layout editor (positioned=false — the drag shield owns the pointer).
  const exportable = positioned && !error && result && result.executed !== false
  const getPng = (opts) => {
    if (chart) {
      const fn = chartExportRef.current
      if (!fn) return Promise.reject(new Error("The chart is still rendering"))
      return fn(opts)
    }
    // opts (e.g. bg: "transparent") deliberately not forwarded: an HTML tile
    // is its card — stripping the surface leaves floating table rows.
    return captureNodePng(cardRef.current)
  }
  return (
    <div
      ref={cardRef}
      className="group/tile flex h-full min-h-0 flex-col overflow-hidden rounded-lg border border-edge bg-card"
      style={positioned ? tileGridStyle(tile.layout) : undefined}
    >
      <div className="flex items-center justify-between gap-2 px-3 pt-2.5">
        <div className="truncate text-sm font-medium">{tile.title}</div>
        <div className="flex items-center gap-1">
          {loading && (
            <Loader2Icon
              className="h-3.5 w-3.5 animate-spin text-muted-foreground"
              data-export-exclude=""
            />
          )}
          {exportable && (
            <VisualExportMenu
              title={tile.title}
              getPng={getPng}
              // size-5 keeps the header row exactly the title line's height —
              // a taller button would push the chart into the card's bottom
              // breathing gap (the tile is grid-fixed; ChartFrame's height
              // formula budgets the header).
              className="-mr-1 size-5 opacity-0 group-hover/tile:opacity-100"
            />
          )}
        </div>
      </div>
      {error ? (
        <div className="flex flex-1 items-center px-4 text-sm text-destructive">
          {error}
        </div>
      ) : !result ? (
        <div className="flex-1" />
      ) : result.executed === false ? (
        <div className="flex flex-1 items-center px-4 text-sm text-muted-foreground">
          {result.note || "execution is not enabled on this deploy"}
        </div>
      ) : tile.widget === "kpi" ? (
        <KpiTile tile={tile} result={result} />
      ) : tile.widget === "pivot" ? (
        <PivotTile tile={tile} result={result} />
      ) : tile.widget === "table" ? (
        <TableTile tile={tile} result={result} />
      ) : isChart(tile.widget) ? (
        // px-3 matches the header's gutter — at px-1 the chart plot ran
        // right up against the tile border.
        <div className="min-h-0 flex-1 px-3">
          <ChartFrame
            // The binder emits the SAME declarative spec chat charts use; the
            // sandboxed iframe applies palette + theme itself. Height fills
            // the tile: h rows of 56px PLUS the (h-1) 12px row gaps the
            // spanning cell absorbs (omitting the gaps left a dead band
            // under every chart) minus the header row AND a bottom breathing
            // gap — the iframe's real content runs ~8px past the prop (the
            // srcdoc's wrap padding), so an exact fit reads flush/cut at the
            // card border.
            code={`renderChart(el, ${JSON.stringify({ ...bindChart(tile, result), title: "" })});`}
            title={tile.title}
            height={Math.max(
              160,
              (tile.layout?.h || 4) * 56 + ((tile.layout?.h || 4) - 1) * 12 - 60
            )}
            // The tile's header kebab drives exports (exportRef); no second
            // menu floating inside the chart area. The frame injects the tile
            // title (spec.title is "" here — the card header owns it) and
            // composites the card surface so the image isn't transparent.
            exportMenu={false}
            exportRef={chartExportRef}
            exportTitle={tile.title}
            exportBg="card"
          />
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The canvas page (one dashboard, live)
// ---------------------------------------------------------------------------

function CanvasPage({ api, canvasId, onBack, onDeleted }) {
  const [meta, setMeta] = useState(null)
  const [error, setError] = useState("")
  const [dials, setDials] = useState({})
  const [applied, setApplied] = useState({})
  const [tileState, setTileState] = useState({})
  const [contracts, setContracts] = useState({})
  const [flipping, setFlipping] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  // Delete-in-flight state for the confirm dialog: the spinner + disabled
  // buttons ARE the feedback; navigation happens only on confirmed deletion.
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState("")
  // Layout editing (drag/resize): the draft is RGL layout items keyed by
  // tile id; Save posts it through the geometry-only patch route.
  const [editing, setEditing] = useState(false)
  const [draftLayout, setDraftLayout] = useState([])
  const [savingLayout, setSavingLayout] = useState(false)
  const [layoutError, setLayoutError] = useState("")
  const runSeq = useRef({})
  // The dial state the last run executed with (null = nothing ran yet).
  const lastApplied = useRef(null)

  const config = meta?.config
  const tiles = useMemo(() => config?.tiles || [], [config])

  useEffect(() => {
    let alive = true
    setMeta(null)
    setError("")
    // A different canvas: its first run executes every tile (no Apply diff).
    lastApplied.current = null
    api
      .getCanvas(canvasId)
      .then((c) => {
        if (!alive) return
        setMeta(c)
        // The server voided a verification because a computation behind a
        // tile changed since the Verify click — say so, don't just drop the
        // badge silently.
        const changed = c.verification_reset?.changed_computations
        if (changed?.length) {
          toast.warning(
            `Verification Removed — the SQL behind ${changed.join(", ")} changed since it was verified`
          )
        }
        const defaults = Object.fromEntries(
          (c.config?.dials || []).map((d) => [d.name, d.default])
        )
        setDials(defaults)
        setApplied(defaults)
      })
      .catch(
        (e) =>
          alive &&
          setError({ status: e?.status, message: String(e?.message || e) })
      )
    return () => {
      alive = false
    }
  }, [api, canvasId])

  // Live-DISTINCT observed values (24h server cache) per bound `column` —
  // fetched async so the page never blocks on an engine query; the dial
  // upgrades from plain text to type-ahead when values arrive.
  const [colDomains, setColDomains] = useState({})
  const domainFetched = useRef(new Set())
  useEffect(() => {
    if (!config) return
    for (const t of tiles) {
      const params = t.data?.sql
        ? t.data?.parameters || []
        : contracts[t.data?.computation]?.parameters || []
      const bound = new Set(
        Object.entries(t.data?.bind || {})
          .filter(([, s]) => s.dial)
          .map(([p]) => p)
      )
      for (const p of params) {
        if (!p?.column || !bound.has(p.name) || domainFetched.current.has(p.column)) continue
        domainFetched.current.add(p.column)
        api
          .getColumnDomain(config.data_domain, config.dataset, p.column)
          .then((d) =>
            setColDomains((prev) => ({
              ...prev,
              [p.column]: { values: d.values || [], exhaustive: !!d.exhaustive },
            }))
          )
          .catch(() => {}) // advisory — the dial stays a plain text input
      }
    }
  }, [api, config, tiles, contracts])

  // Contracts for dial control types: one fetch per distinct computation.
  useEffect(() => {
    if (!config) return
    const slugs = [...new Set(tiles.map((t) => t.data?.computation).filter(Boolean))]
    slugs.forEach((slug) => {
      api
        .getComputation(config.data_domain, config.dataset, slug)
        .then((c) => setContracts((prev) => ({ ...prev, [slug]: c })))
        .catch(() => {}) // controls fall back to typed-off-default inputs
    })
  }, [api, config, tiles])

  // The parameter contract behind one dial (first bound param wins — the
  // contract layer guarantees all bound params share a type).
  const dialBinding = useCallback(
    (name) => {
      for (const t of tiles) {
        for (const [pname, src] of Object.entries(t.data?.bind || {})) {
          if (src.dial === name) {
            // Ad-hoc tiles carry their typed contract INLINE (data.parameters);
            // slug tiles get theirs from the fetched computation doc. The
            // advisory `observed` domain (profile evidence for a `column`-
            // bound parameter — the type-ahead) rides the canvas GET for
            // ad-hoc tiles and the computation contract for slug tiles.
            const isSql = Boolean(t.data?.sql)
            const params = isSql
              ? t.data?.parameters || []
              : contracts[t.data.computation]?.parameters || []
            const p = params.find((q) => q.name === pname)
            if (p) {
              // Live-DISTINCT domain first; profile evidence (the canvas GET /
              // contract enrichment) bridges while the fetch is in flight.
              const domain =
                (p.column && colDomains[p.column]) ||
                (isSql
                  ? meta?.observed?.[t.id]?.[pname]
                  : contracts[t.data.computation]?.observed?.[pname])
              return { param: p, domain: domain || null }
            }
          }
        }
      }
      return { param: null, domain: null }
    },
    [tiles, contracts, meta, colDomains]
  )

  // Executes the given tiles (default: all). Sequencing is PER TILE: a newer
  // run of the same tile supersedes an older one, but an Apply that touches
  // tile C never discards the in-flight results of tiles A and B.
  const runTiles = useCallback(
    (dialState, only) => {
      tiles.forEach((tile) => {
        if (only && !only.has(tile.id)) return
        const seq = (runSeq.current[tile.id] || 0) + 1
        runSeq.current[tile.id] = seq
        setTileState((prev) => ({
          ...prev,
          [tile.id]: { ...prev[tile.id], loading: true, error: "" },
        }))
        api
          .runCanvasTile(canvasId, tile.id, dialState)
          .then((result) => {
            if (runSeq.current[tile.id] !== seq) return // superseded
            setTileState((prev) => ({ ...prev, [tile.id]: { result } }))
          })
          .catch((e) => {
            if (runSeq.current[tile.id] !== seq) return
            setTileState((prev) => ({
              ...prev,
              [tile.id]: { error: String(e?.message || e) },
            }))
          })
      })
    },
    [api, canvasId, tiles]
  )

  // Keyed on config PRESENCE, not identity: a layout-only save swaps the
  // config object without changing any query, and must not re-execute every
  // tile against the engine. An Apply re-runs ONLY the tiles bound to a dial
  // whose value changed — the rest would rescan identical data.
  const configLoaded = Boolean(config)
  useEffect(() => {
    if (!configLoaded) return
    const prev = lastApplied.current
    lastApplied.current = applied
    if (prev === null) {
      runTiles(applied)
      return
    }
    const changed = new Set(
      Object.keys({ ...prev, ...applied }).filter(
        (k) => JSON.stringify(prev[k]) !== JSON.stringify(applied[k])
      )
    )
    if (changed.size === 0) return
    const affected = new Set(
      tiles
        .filter((t) =>
          Object.values(t.data?.bind || {}).some((src) => changed.has(src?.dial))
        )
        .map((t) => t.id)
    )
    if (affected.size) runTiles(applied, affected)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run on load + Apply only
  }, [configLoaded, applied])

  const dirty = useMemo(
    () => JSON.stringify(dials) !== JSON.stringify(applied),
    [dials, applied]
  )

  // A numeric dial someone emptied holds "" (DialControl's guard) — Apply
  // must refuse rather than post null/0 to every bound tile. Free-text dials
  // keep their existing "" freedom; numeric-ness comes from the contract, or
  // from the config default's type while contracts are still loading.
  const dialsIncomplete = useMemo(
    () =>
      (config?.dials || []).some((d) => {
        const v = dials[d.name]
        if (typeof v === "number" && !Number.isFinite(v)) return true
        const t = dialBinding(d.name).param?.type
        const numeric =
          t === "integer" || t === "number" || typeof d.default === "number"
        return numeric && (v === "" || v == null)
      }),
    [config, dials, dialBinding]
  )

  const flip = async (action) => {
    setFlipping(true)
    try {
      const out = await api[action](canvasId)
      setMeta((prev) => ({ ...prev, ...out }))
    } catch (e) {
      // setError is the PAGE-LOAD error slot — it never renders once meta is
      // set, which made a failed Verify (incl. the designed 409 stale-flip)
      // invisible. Toast it, and resync meta so a stale badge corrects itself.
      toast.error(String(e?.message || e))
      api
        .getCanvas(canvasId)
        .then((fresh) => setMeta((prev) => ({ ...prev, ...fresh })))
        .catch(() => {})
    } finally {
      setFlipping(false)
    }
  }

  const startEditLayout = () => {
    setDraftLayout(
      tiles.map((t, i) => ({
        i: t.id,
        x: Number.isInteger(t.layout?.x) ? t.layout.x : 0,
        y: Number.isInteger(t.layout?.y) ? t.layout.y : i * 4,
        w: t.layout?.w || 6,
        h: t.layout?.h || 4,
        // Below 2×2 a tile is all chrome, no content.
        minW: 2,
        minH: 2,
      }))
    )
    setLayoutError("")
    setEditing(true)
  }

  const saveLayout = async () => {
    setSavingLayout(true)
    try {
      const layouts = Object.fromEntries(
        draftLayout.map(({ i, x, y, w, h }) => [i, { x, y, w, h }])
      )
      const out = await api.updateCanvasLayout(canvasId, layouts)
      // The patched config comes back whole — the view grid re-renders the
      // new geometry without re-executing any tile (see configLoaded).
      setMeta((prev) => ({ ...prev, ...out }))
      setEditing(false)
    } catch (e) {
      setLayoutError(String(e?.message || e))
    } finally {
      setSavingLayout(false)
    }
  }

  if (error && !meta) {
    // A 404 is BOTH "deleted" and "not yours" — the API can't distinguish
    // (per-user partition keys), so the message names both honestly.
    const missing = error.status === 404
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
          <LayoutDashboardIcon className="h-6 w-6 text-muted-foreground" />
        </div>
        <div className="text-base font-medium">
          {missing ? "This canvas doesn't exist" : "Couldn't load this canvas"}
        </div>
        <div className="max-w-sm text-sm text-muted-foreground">
          {missing
            ? "It may have been deleted, or it belongs to another account."
            : error.message}
        </div>
        <Button size="sm" variant="outline" onClick={onBack}>
          Back to canvases
        </Button>
      </div>
    )
  }
  if (!meta) {
    return (
      <div className="flex flex-1 items-center justify-center text-muted-foreground">
        <Loader2Icon className="h-5 w-5 animate-spin" />
      </div>
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* PINNED header (benchmark-report chrome): centered width-capped row
          with the ghost back arrow in a left gutter. top-4/pt-4 centers it on
          the sidebar brand/collapse line (the CollapsedTriggerOverlay's
          top-4 left-4 spot) — this is a full-inset page with no topbar. The
          canvas scrolls in the region BELOW, so title + actions never move. */}
      <div className="relative mx-auto flex w-full max-w-[74.25rem] shrink-0 flex-col pl-9 pr-3">
        <Button
          variant="ghost"
          size="icon"
          className="absolute top-4 left-0 rounded-md"
          onClick={onBack}
          aria-label="Back"
        >
          <ArrowLeftIcon />
        </Button>
        <div className="flex flex-wrap items-center gap-3 px-1 pt-4 pb-3">
          <EditableTitle
            title={meta.title}
            onRename={(next) =>
              api.renameCanvas(canvasId, next).then((out) =>
                setMeta((m) => ({
                  ...m,
                  title: out.title,
                  updated_at: out.updated_at,
                }))
              )
            }
          />
          <div className="ml-auto flex items-center gap-2">
            <StatusBadge status={meta.status} />
            {editing ? (
              <>
                {layoutError && (
                  <span className="max-w-64 truncate text-xs text-destructive" title={layoutError}>
                    {layoutError}
                  </span>
                )}
                <Button size="sm" disabled={savingLayout} onClick={saveLayout}>
                  Save layout
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={savingLayout}
                  onClick={() => {
                    setEditing(false)
                    setLayoutError("")
                  }}
                >
                  Cancel
                </Button>
              </>
            ) : (
              // ONE Actions dropdown instead of a row of buttons — verify,
              // layout editing, and delete are occasional settings, not
              // constant controls. The delete CONFIRM dialog stays mounted at
              // page level, outside the menu (a Dialog inside a
              // DropdownMenuItem bubbles its close click back into the menu —
              // the HarvestView lesson).
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button size="sm" variant="outline">
                    Actions
                    <ChevronDownIcon data-icon="inline-end" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {meta.status === "created" && (
                    <DropdownMenuItem
                      disabled={flipping}
                      onSelect={() => flip("verifyCanvas")}
                    >
                      <BadgeCheckIcon /> Verify
                    </DropdownMenuItem>
                  )}
                  {meta.status === "verified" && (
                    <DropdownMenuItem
                      disabled={flipping}
                      onSelect={() => flip("unverifyCanvas")}
                    >
                      <BadgeCheckIcon /> Unverify
                    </DropdownMenuItem>
                  )}
                  {tiles.length > 0 && (
                    <DropdownMenuItem onSelect={startEditLayout}>
                      <MoveIcon /> Edit layout
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    onSelect={() => setConfirmDelete(true)}
                  >
                    <Trash2Icon /> Delete
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        </div>
      </div>

      {/* The SCROLL REGION spans the full layout width so the scrollbar rides
          the inset's right edge, not the centered column; the content
          re-centers inside on the same max-w + gutter math as the pinned
          header (pl-10/pr-4 = the header's pl-9/pr-3 + its px-1 inset). */}
      <div className="okf-thin-scroll min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-[74.25rem] flex-col gap-3 pb-4 pl-10 pr-4">
          {(config?.dials || []).length > 0 && (
            <div className="rounded-lg border border-edge bg-card px-3 py-2.5">
              {/* Section header matching the tile headers (text-sm font-medium
                  foreground — black/white per theme); sentence case (house
                  style: no all-caps). Same glyph as the wireframe's dial chips. */}
              <div className="mb-2 flex items-center gap-1.5 text-sm font-medium">
                <SlidersHorizontalIcon className="h-4 w-4 text-muted-foreground" />
                Controls
              </div>
              <div className="flex flex-wrap items-end gap-3">
                {config.dials.map((d) => {
                  const { param, domain } = dialBinding(d.name)
                  return (
                    <label key={d.name} className="flex flex-col gap-1 text-xs">
                      <span className="text-muted-foreground">{d.label || d.name}</span>
                      <DialControl
                        param={param}
                        domain={domain}
                        value={dials[d.name]}
                        onChange={(v) => setDials((prev) => ({ ...prev, [d.name]: v }))}
                      />
                    </label>
                  )
                })}
                {/* Default size (h-9) matches the dial inputs/selects exactly;
                    ml-auto anchors it to the strip's right edge — filters
                    left, action right — instead of floating mid-row. items-end
                    on the row keeps it level with the input line, not the
                    labels. */}
                <Button
                  className="ml-auto"
                  disabled={!dirty || dialsIncomplete}
                  onClick={() => setApplied({ ...dials })}
                >
                  <PlayIcon data-icon="inline-start" /> Apply
                </Button>
              </div>
            </div>
          )}

          {editing ? (
            <Suspense
              fallback={
                <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
                  <Loader2Icon className="h-4 w-4 animate-spin" /> loading the layout editor
                </div>
              }
            >
              <CanvasLayoutEditor
                tiles={tiles}
                tileState={tileState}
                layout={draftLayout}
                onChange={(next) =>
                  setDraftLayout(next.map((it) => ({ ...it, minW: 2, minH: 2 })))
                }
              />
            </Suspense>
          ) : (
            <div
              className="grid flex-1 gap-3"
              style={{ gridTemplateColumns: "repeat(12, minmax(0, 1fr))", gridAutoRows: "56px" }}
            >
              {tiles.map((tile) => (
                <Tile key={tile.id} tile={tile} state={tileState[tile.id]} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Deleting a canvas erases agent work a stray click shouldn't — same
          confirm pattern as the benchmark report delete. */}
      {confirmDelete ? (
        <Dialog
          open
          onOpenChange={(o) => {
            // No closing mid-delete (Esc/overlay) — the request is in flight
            // and the dialog's spinner is what reports it.
            if (!o && !deleting) {
              setConfirmDelete(false)
              setDeleteError("")
            }
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Delete this canvas?</DialogTitle>
              <DialogDescription>
                <span className="font-medium text-foreground">{meta.title}</span>{" "}
                will be deleted, with its tiles and dials. This can&apos;t be
                undone — recreating it costs a new authoring job.
              </DialogDescription>
            </DialogHeader>
            {deleteError && (
              <div className="text-sm text-destructive">{deleteError}</div>
            )}
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline" disabled={deleting}>
                  Cancel
                </Button>
              </DialogClose>
              <Button
                variant="destructive"
                disabled={deleting}
                onClick={async () => {
                  setDeleting(true)
                  setDeleteError("")
                  try {
                    await api.deleteCanvas(canvasId)
                    // Navigate only on CONFIRMED deletion — the dialog (with
                    // its spinner) is the in-flight state.
                    onDeleted()
                  } catch (e) {
                    setDeleteError(String(e?.message || e))
                    setDeleting(false)
                  }
                }}
              >
                {deleting ? (
                  <Loader2Icon className="animate-spin" data-icon="inline-start" />
                ) : (
                  <Trash2Icon data-icon="inline-start" />
                )}
                {deleting ? "Deleting…" : "Delete"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The list + entry point
// ---------------------------------------------------------------------------

export function CanvasView({ api, canvasId, onOpen, onBack }) {
  const [canvases, setCanvases] = useState(null)
  const [error, setError] = useState("")

  const refresh = useCallback(() => {
    api
      .listCanvases()
      .then(setCanvases)
      .catch((e) => setError(String(e?.message || e)))
  }, [api])

  useEffect(() => {
    if (!canvasId) refresh()
  }, [canvasId, refresh])

  if (canvasId) {
    return (
      <CanvasPage
        api={api}
        canvasId={canvasId}
        onBack={onBack}
        onDeleted={() => {
          onBack()
        }}
      />
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-3 p-4">
      <h1 className="text-lg font-semibold">Canvases</h1>
      <p className="text-sm text-muted-foreground">
        A canvas is a dashboard the agent builds from your data including live charts,
        tables, and KPIs.
      </p>
      {error && <div className="text-sm text-destructive">{error}</div>}
      {canvases === null ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2Icon className="h-4 w-4 animate-spin" /> loading
        </div>
      ) : canvases.length === 0 ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          No canvases yet.
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {canvases.map((c) => (
            <button
              key={c.canvas_id}
              className="flex items-center gap-3 rounded-lg border border-edge bg-card px-4 py-3 text-left hover:bg-accent/50"
              onClick={() => onOpen(c.canvas_id)}
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">{c.title}</div>
                <div className="truncate text-xs text-muted-foreground">
                  {c.data_domain}/{c.dataset} · updated {c.updated_at}
                </div>
              </div>
              <StatusBadge status={c.status} />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
