// Benchmark report detail — one run's persisted report, routed as a page
// (#/benchmark/<domain>/<dataset>/<report_id>, the chat-thread URL pattern).
//
// Two tabs. SUMMARY is report-style: per-check score tiles (raw + judge-adjusted,
// mean ± spread across the N runs), the pass/overturned/confirmed breakdown, a
// per-question stability distribution, and telemetry (per-tool call distribution,
// tokens by role) — all native bars, keeping the app's subtle visual language.
// DETAILED is the per-question list: outcome chips with cross-run stability,
// expanding to every attempt's output, the gold, grading reasons, the judge's
// verdict + mandatory comment, and every attempt's solver steps (passing runs
// included) — those open in a full-height side panel beside the scroll column
// (the chat doc-peek pattern), fed lazily from the report's traces document.
//
// "Generate annotations" (header, both tabs) runs the aggregator over the
// judge's annotation candidates; the user then selects/edits the final set and
// applies — batch-created as normal annotations (submitted_via="benchmark") that
// an ordinary annotation harvest folds into the wiki.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  GaugeIcon,
  ListTreeIcon,
  SparklesIcon,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import { entryFor } from "@/lib/harvestModels"
import { usePanelWidth } from "@/hooks/usePanelWidth"
import { AnnotationsPanel } from "@/components/benchmark/AnnotationsPanel"
import { ChartFrame } from "@/components/chat/ChartFrame"
import { SolverTracePanel, SqlBlock } from "@/components/benchmark/TracePanel"
import { CHECK_META, fmtVersionLabel } from "@/views/BenchmarkView.jsx"

const POLL_MS = 4000
// The side panel's persisted width (its own key — the report's panel and the
// chat doc peek are different surfaces with different natural widths).
const PANEL_WIDTH_KEY = "okf.benchmark.panelWidth"

function checkLabel(key) {
  return CHECK_META.find((c) => c.key === key)?.label || key
}

function fmtTokens(n) {
  if (!n) return "0"
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

// Wall time as the unit a reader thinks in: seconds under a minute, then m/h.
function fmtDuration(ms) {
  const s = Math.round((ms || 0) / 1000)
  if (!s) return "—"
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

export default function BenchmarkReportView({
  api,
  selection,
  reportId,
  onBack,
}) {
  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const [state, setState] = useState({ status: "loading", data: null, error: null })

  // ONE side-panel slot, two occupants (the chat doc-peek pattern): the solver
  // steps, or the annotation review. `panel` is "trace" | "annotations" | null;
  // opening one replaces the other, since they compete for the same space and
  // both are "the detail beside the report". Width is drag-resizable and
  // persisted, exactly like the chat doc peek (usePanelWidth).
  const [panel, setPanel] = useState(null)
  // Summary/Detailed as CONTROLLED tabs, plus the page-level check scope (one
  // of the report's OWN checks — never a "combined" merge: an Accuracy
  // solver's SQL-tool calls and a Behavior solver's free-form exploration
  // don't mean anything folded into one number) that drives every summary
  // widget — one dropdown beside the tabs instead of per-widget filter pills.
  const [tab, setTab] = useState("summary")
  const [scope, setScope] = useState(null)
  const {
    width: panelWidth,
    dragging: panelDragging,
    startResize: startPanelResize,
  } = usePanelWidth({ storageKey: PANEL_WIDTH_KEY, min: 340, defaultWidth: 448 })

  // `traceSel` names one attempt {q_id, check, run, question} and PERSISTS
  // through the close slide so the panel doesn't blank mid-animation. The
  // traces document is fetched ONCE per report, imperatively on the first open
  // — an effect keyed on the fetch status would cancel its own in-flight
  // request when the status flips idle→loading (the panel then shows
  // "Loading steps…" forever).
  const [traceSel, setTraceSel] = useState(null)
  const [traces, setTraces] = useState({ status: "idle", byId: {}, error: null })
  const tracesStarted = useRef(false)

  // Stale-response guard — the `let alive` effect-cleanup idiom, hoisted to a
  // shared epoch because `load` and the traces fetch are also called OUTSIDE
  // the reset effect (the poll, the annotations reload, a panel open): bumping
  // the epoch on report change makes any still-in-flight response for the
  // previous report land silently instead of under the new one.
  const epochRef = useRef(0)

  const ensureTraces = useCallback(() => {
    if (tracesStarted.current) return
    tracesStarted.current = true
    const epoch = epochRef.current
    setTraces({ status: "loading", byId: {}, error: null })
    api
      .getBenchmarkReportTraces(domain, dataset, reportId)
      .then((doc) => {
        if (epoch !== epochRef.current) return
        const rows = Array.isArray(doc?.traces) ? doc.traces : []
        setTraces({
          status: "ok",
          byId: Object.fromEntries(
            rows.map((r) => [`${r.check}:${r.run}:${r.q_id}`, r])
          ),
          error: null,
        })
      })
      .catch((e) => {
        if (epoch !== epochRef.current) return
        tracesStarted.current = false // the next open retries
        setTraces({ status: "error", byId: {}, error: e.message || String(e) })
      })
  }, [api, domain, dataset, reportId])

  const openTrace = useCallback(
    (sel) => {
      if (sel === null) {
        setPanel(null)
        return
      }
      setTraceSel(sel)
      setPanel("trace")
      ensureTraces()
    },
    [ensureTraces]
  )

  const load = useCallback(async () => {
    if (!api || !domain || !dataset || !reportId) return
    const epoch = epochRef.current
    try {
      const data = await api.getBenchmarkReport(domain, dataset, reportId)
      if (epoch === epochRef.current)
        setState({ status: "ok", data, error: null })
    } catch (e) {
      if (epoch === epochRef.current)
        setState({ status: "error", data: null, error: e.message || String(e) })
    }
  }, [api, domain, dataset, reportId])

  useEffect(() => {
    epochRef.current += 1
    setState({ status: "loading", data: null, error: null })
    setTraceSel(null)
    setPanel(null)
    setTab("summary")
    setScope(null)
    setTraces({ status: "idle", byId: {}, error: null })
    tracesStarted.current = false
    load()
  }, [load])

  const row = state.data?.row
  const report = state.data?.report

  // Default the scope to the report's first present check once it loads (and
  // re-derive if a stale scope no longer names one of this report's checks —
  // e.g. navigating between reports with different enabled checks).
  const reportChecks = report?.scores ? Object.keys(report.scores) : []
  useEffect(() => {
    if (reportChecks.length && !reportChecks.includes(scope)) {
      setScope(reportChecks[0])
    }
  }, [reportChecks, scope])

  // Poll while the run (or an aggregation) is still working.
  const active =
    row && (["queued", "running"].includes(row.status) || row.agg_status === "running")
  useEffect(() => {
    if (!active) return
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [active, load])

  let body
  if (state.status === "loading") {
    body = (
      <div className="mx-auto flex w-full max-w-[74.25rem] flex-col gap-3 px-1 pt-3 pl-9">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  } else if (state.status === "error") {
    body = (
      <div className="mx-auto w-full max-w-[74.25rem] px-1 pt-3 pl-9">
        <Alert variant="destructive">
          <AlertTitle>Couldn’t load the report</AlertTitle>
          <AlertDescription>{state.error}</AlertDescription>
          <Button variant="outline" className="mt-2 w-fit" onClick={onBack}>
            <ArrowLeftIcon data-icon="inline-start" />
            Back to Benchmark
          </Button>
        </Alert>
      </div>
    )
  } else {
    body = (
      // Tabs is the LAYOUT root so TabsList (pinned with the header) and
      // TabsContent (in the scroll region) share one tab state across regions.
      <Tabs
        value={tab}
        onValueChange={setTab}
        className="flex min-h-0 min-w-0 flex-1 flex-col"
      >
        {/* Back lives in a fixed left GUTTER outside the content column, so the
            title (with its icon), the tabs, and the question cards all share
            ONE left edge. The column is capped at content + gutter rather than
            pulled with a negative margin — a negative margin would slide the
            button under the sidebar (and get clipped) on a narrow inset. */}
        <div className="relative mx-auto flex w-full max-w-[74.25rem] shrink-0 flex-col pl-9">
          <Button
            variant="ghost"
            size="icon"
            className="absolute top-3 left-0 rounded-md"
            onClick={onBack}
            aria-label="Back"
          >
            <ArrowLeftIcon />
          </Button>
          {/* PINNED: header + tabs never scroll away — only the content below
              overflows. pt-3 (+ the region's pt-1) centers the title row on the
              same 32px line as the sidebar brand/toggle. */}
          <div className="flex shrink-0 flex-col gap-4 px-1 pt-3 pb-3">
            <ReportHeader row={row} />
            {report ? (
              <div className="flex items-center justify-between gap-2">
                <TabsList>
                  <TabsTrigger value="summary">Summary</TabsTrigger>
                  <TabsTrigger value="detailed">Detailed</TabsTrigger>
                </TabsList>
                {/* Right side of the tabs row, all h-8: the check scope
                    (Summary only — Detailed always shows everything; hidden
                    outright when only one check ran, since there's nothing to
                    pick), then the annotations flow button at the far edge. */}
                <div className="flex items-center gap-2">
                  {tab === "summary" && reportChecks.length > 1 ? (
                    <Select value={scope || ""} onValueChange={setScope}>
                      <SelectTrigger className="w-44">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent align="end">
                        {reportChecks.map((c) => (
                          <SelectItem key={c} value={c}>
                            {checkLabel(c)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : null}
                  <AnnotationsButton
                    api={api}
                    domain={domain}
                    dataset={dataset}
                    reportId={reportId}
                    row={row}
                    report={report}
                    reload={load}
                    onReview={() => setPanel("annotations")}
                  />
                </div>
              </div>
            ) : null}
          </div>
        </div>
        {/* The SCROLL REGION spans the full layout width so the scrollbar rides
            the region's edge, not the centered column; the content re-centers
            inside it on the same max-w + gutter math as the pinned header
            above (pl-10 = the pl-9 gutter + the old p-1 inset). */}
        <div className="okf-thin-scroll min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[74.25rem] p-1 pl-10">
            {!report ? (
              <Card>
                <CardContent className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
                  {row?.status === "failed" ? (
                    <span className="text-destructive">
                      The run failed: {row.detail || "no detail recorded"}
                    </span>
                  ) : (
                    <>
                      <Spinner className="size-4" />
                      The run is still working — the report appears when it completes.
                    </>
                  )}
                </CardContent>
              </Card>
            ) : (
              <>
                <TabsContent value="summary" className="min-w-0">
                  <SummaryTab report={report} row={row} scope={scope} />
                </TabsContent>
                <TabsContent value="detailed" className="min-w-0">
                  <DetailedTab
                    report={report}
                    activeTrace={panel === "trace" ? traceSel : null}
                    onOpenTrace={openTrace}
                  />
                </TabsContent>
              </>
            )}
          </div>
        </div>
      </Tabs>
    )
  }

  // The view fills the content region EDGE TO EDGE (it renders OUTSIDE the
  // shared centered scroll block, see App.jsx) — the chat-page geometry: the
  // report content stays capped (max-w-6xl) and centers in whatever space is
  // left of the panel, while the solver-steps panel stands full-height pinned
  // to the region's right edge, a proper sidebar. Same width-animated clip as
  // the chat doc peek, with the inner wrapper at fixed width so the panel's
  // content never reflows mid-slide.
  const finals = report?.annotations?.final || []
  const panelOpen = Boolean(panel)
  return (
    <div className="flex min-h-0 w-full flex-1 items-stretch">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">{body}</div>
      {/* The width-animated clip. Inline style (not a class) because the width
          is a live drag number; the transition is disabled mid-drag so the
          panel tracks the pointer instead of rubber-banding behind it. */}
      <div
        className={cn(
          "h-full shrink-0 overflow-hidden",
          !panelDragging &&
            "transition-[width] duration-300 ease-in-out motion-reduce:transition-none"
        )}
        style={{ width: panelOpen ? panelWidth : 0 }}
        aria-hidden={!panelOpen}
      >
        <div
          className={cn("h-full py-1 pr-1", !panelOpen && "invisible")}
          style={{ width: panelWidth }}
        >
          {panel === "annotations" ? (
            <AnnotationsPanel
              api={api}
              domain={domain}
              dataset={dataset}
              reportId={reportId}
              finals={finals}
              onClose={() => setPanel(null)}
              onResizeStart={startPanelResize}
              resizing={panelDragging}
            />
          ) : traceSel ? (
            <SolverTracePanel
              q={{
                q_id: `${traceSel.check}:${traceSel.run}:${traceSel.q_id}`,
                question: `${traceSel.question} — ${checkLabel(traceSel.check)}, run ${traceSel.run + 1}`,
              }}
              traces={traces}
              onClose={() => setPanel(null)}
              onResizeStart={startPanelResize}
              resizing={panelDragging}
            />
          ) : null}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Header (title only) + the annotations flow button (lives on the tabs row)
// ---------------------------------------------------------------------------

function ReportHeader({ row }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <GaugeIcon className="size-4 shrink-0 text-muted-foreground" />
      <h2 className="font-heading text-lg font-medium">
        Benchmark report
        {row?.created_at ? (
          <span className="ml-2 text-sm font-normal text-muted-foreground tabular-nums">
            {new Date(row.created_at).toLocaleString()}
          </span>
        ) : null}
      </h2>
    </div>
  )
}

// Generate → Aggregating… → Review N annotations. One button, three states,
// driven by the report's candidates + the aggregation status on the row.
function AnnotationsButton({
  api,
  domain,
  dataset,
  reportId,
  row,
  report,
  reload,
  onReview,
}) {
  const candidates = report?.annotations?.candidates || []
  const finals = report?.annotations?.final || []
  const aggStatus = row?.agg_status || report?.annotations?.status || "idle"
  const [kicking, setKicking] = useState(false)

  const generate = async () => {
    setKicking(true)
    try {
      await api.aggregateReportAnnotations(domain, dataset, reportId)
      toast.success("Aggregating annotations — this takes a minute or two.")
      await reload()
    } catch (e) {
      toast.error(`Could not start the aggregation: ${e.message || e}`)
    } finally {
      setKicking(false)
    }
  }

  if (!report || !candidates.length) return null
  if (aggStatus === "running") {
    return (
      <Button variant="outline" disabled>
        <Spinner data-icon="inline-start" />
        Aggregating…
      </Button>
    )
  }
  if (aggStatus === "complete" && finals.length) {
    return (
      <Button onClick={onReview}>
        <SparklesIcon data-icon="inline-start" />
        Review {finals.length} annotation{finals.length === 1 ? "" : "s"}
      </Button>
    )
  }
  // A failed aggregation must be visible, not a silent reset to "Generate":
  // surface the row's agg_detail (the failure reason the runtime stamps on the
  // REPORT# row) beside the button, which stays live so the user can retry.
  const aggFailed = aggStatus === "failed"
  return (
    <div className="flex min-w-0 items-center gap-2">
      {aggFailed ? (
        <span
          className="max-w-72 min-w-0 truncate text-xs text-destructive"
          title={row?.agg_detail || undefined}
        >
          Aggregation failed: {row?.agg_detail || "no detail recorded"}
        </span>
      ) : null}
      <Button onClick={generate} disabled={kicking}>
        {kicking ? (
          <Spinner data-icon="inline-start" />
        ) : (
          <SparklesIcon data-icon="inline-start" />
        )}
        {aggFailed
          ? "Retry aggregation"
          : `Generate annotations (${candidates.length})`}
      </Button>
    </div>
  )
}

function SummaryTab({ report, row, scope }) {
  const scores = report.scores || {}
  const telemetry = report.telemetry || {}
  const checks = Object.keys(scores)
  // `scope` names exactly one of the report's own checks (never a "combined"
  // merge — an Accuracy solver's SQL-tool calls and a Behavior solver's
  // free-form exploration don't mean anything folded into one number), so
  // this ever shows at most one check's widgets. `visible` still keys the map
  // below so it degrades to nothing rather than crashing while `scope` is
  // still settling on mount.
  const visible = checks.filter((c) => c === scope)

  // One 3-column grid, widgets roughly square; the Sankey and the time chart
  // take 2 of the 3 columns (they need the horizontal room to breathe).
  return (
    <div className="flex flex-col gap-4">
      <RunConfigCard report={report} row={row} />
      <div className="grid gap-4 lg:grid-cols-3">
        {visible.map((c) => (
          <Fragment key={c}>
            <ScoreWidget block={scores[c]} />
            <OutcomesWidget
              block={scores[c]}
              stability={report.stability?.[c]}
              className="lg:col-span-2"
            />
            {(scores[c].raw?.per_run?.length || 0) > 1 ? (
              <VarianceWidget
                block={scores[c]}
                report={report}
                className="lg:col-span-3"
              />
            ) : null}
          </Fragment>
        ))}
        <TimeToAnswerWidget report={report} scope={scope} className="lg:col-span-2" />
        <ToolUsageWidget telemetry={telemetry} scope={scope} />
      </div>
    </div>
  )
}

// Every summary block is a WIDGET: title, one-line description, content — and
// an optional header action (the scope filter pills).
function Widget({ title, description, action, className, children }) {
  return (
    <Card size="sm" className={className}>
      <CardHeader>
        <CardTitle className="text-sm">{title}</CardTitle>
        <CardDescription className="text-xs">{description}</CardDescription>
        {action ? <CardAction>{action}</CardAction> : null}
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-3">
        {children}
      </CardContent>
    </Card>
  )
}

// The run's identity, as a card rather than a cramped strip under the title:
// what was measured, with what, against which wiki — the provenance you need to
// trust (or reproduce) the numbers below it.
function RunConfigCard({ report, row }) {
  const config = report.config || {}
  const runs = config.runs || row?.runs || 1
  const solverEntry = entryFor(config.solver_model)
  const judgeEntry = entryFor(config.judge_model)
  const q = config.questions || {}

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="text-sm">Run configuration</CardTitle>
        <CardDescription>
          {row?.created_at ? new Date(row.created_at).toLocaleString() : "—"}
          {report.completed_at ? " · completed" : ""}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:grid-cols-6">
        <Field label="Checks">
          <span className="flex flex-wrap gap-1">
            {(config.checks || []).map((c) => (
              <Badge key={c} variant="secondary">
                {checkLabel(c)}
              </Badge>
            ))}
            {config.behavior_live_sql ? (
              // Comparability marker: this run's Behavior solver could run
              // read-only SQL — its scores aren't comparable to wiki-only runs.
              <Badge variant="outline">live SQL</Badge>
            ) : null}
          </span>
        </Field>
        <Field label="Independent runs">
          <span className="tabular-nums">{runs}</span>
        </Field>
        <Field label="Questions">
          <span className="tabular-nums">
            {q.total ?? "—"}
            {q.dropped ? (
              <span className="text-muted-foreground"> (+{q.dropped} dropped)</span>
            ) : null}
          </span>
        </Field>
        <Field label="Solver">
          {solverEntry?.label || config.solver_model || "default"}
          {config.solver_effort ? (
            <span className="text-muted-foreground"> / {config.solver_effort}</span>
          ) : null}
        </Field>
        <Field label="Judge">
          {judgeEntry?.label || config.judge_model || "default"}
          {config.judge_effort ? (
            <span className="text-muted-foreground"> / {config.judge_effort}</span>
          ) : null}
        </Field>
        <Field label="Wiki version">
          {config.version_id ? (
            <span className="font-mono text-xs">
              {fmtVersionLabel({ version_id: config.version_id })}
            </span>
          ) : (
            "current"
          )}
        </Field>
      </CardContent>
    </Card>
  )
}

function Field({ label, children }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </span>
      <span className="min-w-0 truncate text-sm font-medium">{children}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Per-check widgets
// ---------------------------------------------------------------------------

// One Chart.js chart inside the app's sandboxed frame (the chat's ChartFrame):
// interactive tooltips/legends, app palette, crisp at any zoom. `spec` is the
// same shape the chat agent's render_chart authors.

// Every widget chart shares ONE height so the summary grid reads as a uniform
// dashboard — per-widget heights left the chart bottoms misaligned within a
// row (the card frames stretch to the grid row; the boxes inside didn't).
const WIDGET_CHART_HEIGHT = 230

// The box ChartFrame reports for that height (+8px frame padding). Non-chart
// widget bodies (the score dial, the empty states) size to the SAME box so
// every widget lands at the same height whatever it holds.
const WIDGET_BODY = "min-h-[238px] flex-1"

function WidgetChart({ spec, height = WIDGET_CHART_HEIGHT }) {
  const code = useMemo(
    () => `renderChart(el, ${JSON.stringify(spec)})`,
    // The spec is built fresh each render; key the memo on its JSON.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [JSON.stringify(spec)]
  )
  // exportMenu={false}: the dashboard is a fixed report surface, not an
  // authored chat visual — no export kebab floating over the widgets.
  return <ChartFrame code={code} height={height} exportMenu={false} />
}

function ScoreWidget({ block }) {
  const raw = block.raw?.mean || 0
  // A judge-graded check (Behavior) has adjusted: null — its raw outcomes
  // already carry the judge's authority, so there is no second ring to show.
  const hasAdjusted = Boolean(block.adjusted)
  const adjusted = block.adjusted?.mean || 0
  // The spread (min–max range across the N runs) only means something with
  // more than one run.
  const multiRun = (block.raw?.per_run?.length || 0) > 1
  const fmtSpread = (s) => ` ±${Math.round((s || 0) * 100)}%`

  // Fully-discarded check (graded 0): a 0% dial would read as a real score.
  if (block.graded === 0) {
    return (
      <Widget
        title="Score"
        description="No score for this check — nothing was graded."
      >
        <p
          className={cn(
            "flex items-center justify-center text-xs text-muted-foreground",
            WIDGET_BODY
          )}
        >
          Not graded — every question was discarded.
        </p>
      </Widget>
    )
  }

  return (
    <Widget
      title="Score"
      description={
        hasAdjusted
          ? "Bold ring: raw pass rate. Ghost ring: the judge-adjusted score."
          : "Pass rate across all runs — every run was graded by the judge."
      }
    >
      <div
        className={cn(
          "flex flex-col items-center justify-center gap-3",
          WIDGET_BODY
        )}
      >
        <ScoreDial raw={raw} adjusted={hasAdjusted ? adjusted : 0} />
        <p className="flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
          <span className="flex items-center gap-1.5">
            <span className="size-2 rounded-full bg-primary" />
            raw {Math.round(raw * 100)}%
            {multiRun ? fmtSpread(block.raw?.spread) : ""}
          </span>
          {hasAdjusted ? (
            <span className="flex items-center gap-1.5">
              <span className="size-2 rounded-full bg-primary/30" />
              judge-adjusted {Math.round(adjusted * 100)}%
              {multiRun ? fmtSpread(block.adjusted?.spread) : ""}
            </span>
          ) : null}
        </p>
      </div>
    </Widget>
  )
}

// The radial score dial: the judge-adjusted arc sits BEHIND the raw arc as a
// ghost, so the exposed remainder IS the judge's lift — one glance, no second
// number needed.
function ScoreDial({ raw = 0, adjusted = 0 }) {
  const R = 42
  const C = 2 * Math.PI * R
  const clamp = (v) => Math.max(0, Math.min(1, v || 0))
  return (
    <div className="relative shrink-0">
      <svg viewBox="0 0 100 100" className="size-44 -rotate-90">
        <circle
          cx="50"
          cy="50"
          r={R}
          fill="none"
          className="stroke-muted"
          strokeWidth="8"
        />
        <circle
          cx="50"
          cy="50"
          r={R}
          fill="none"
          className="stroke-primary/30"
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={`${clamp(adjusted) * C} ${C}`}
        />
        <circle
          cx="50"
          cy="50"
          r={R}
          fill="none"
          className="stroke-primary"
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={`${clamp(raw) * C} ${C}`}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-4xl font-semibold tabular-nums">
          {Math.round(clamp(raw) * 100)}%
        </span>
        <span className="text-[10px] tracking-wide text-muted-foreground uppercase">
          raw
        </span>
      </div>
    </div>
  )
}

// The outcome story as a treemap: every graded question tiled by where it
// ended up — tile area = count, failures split by the judge's ruling. Hover a
// tile for its exact count. The counts are the report's STORED breakdown
// (report_run persists passed_all_runs/overturned/confirmed_failed/discarded
// alongside the scores) — the per-question detail is display material, not a
// second place to derive the same numbers. Judge-errored reviews sit inside
// confirmed_failed: the backend counts an unreviewable failure against the
// wiki.
function OutcomesWidget({ block, stability, className }) {
  // Behavior failures aren't "wiki gaps" by definition — the judge failed the
  // RUN against the expectation (the fault may be the agent's); there is no
  // overturn ruling to split by.
  const judgeGraded = block.check === "behavior"

  const items = [
    { label: "Passed", value: block.passed_all_runs || 0 },
    {
      label: judgeGraded ? "Failed" : "Wiki gap",
      value: block.confirmed_failed || 0,
    },
    { label: "Not a wiki gap", value: block.overturned || 0 },
    { label: "Discarded", value: block.discarded || 0 },
  ].filter((d) => d.value > 0)

  // The stored stability histogram ({"<passes>": count}, discarded excluded),
  // compact: how many questions passed N/N vs k/N vs 0/N. Only meaningful
  // with more than one run.
  const runsN = block.raw?.per_run?.length || 1
  const stabilityLine =
    runsN > 1
      ? Object.entries(stability || {})
          .map(([k, v]) => [Number(k), v])
          .filter(([, v]) => v > 0)
          .sort((a, b) => b[0] - a[0])
          .map(([k, v]) => `passed ${k}/${runsN}: ${v}`)
          .join(" · ")
      : ""

  return (
    <Widget
      className={className}
      title="Question outcomes"
      description={
        judgeGraded
          ? "Every question tiled by outcome — the judge ruled each run against the expected behavior."
          : "Every graded question tiled by outcome — failures split by the judge's ruling on fault."
      }
    >
      {items.length ? (
        <>
          <WidgetChart
            spec={{
              type: "treemap",
              series: [
                {
                  name: "questions",
                  data: items,
                  // Semantic tiles: pass green, wiki gap red, no-fault yellow,
                  // the rest muted (palette indices resolved in-frame).
                  colors: {
                    Passed: 2,
                    "Wiki gap": 9,
                    Failed: 9,
                    // A true yellow (the palette's closest slot is amber) — raw
                    // triple, resolved in-frame like the palette colors.
                    "Not a wiki gap": "234, 179, 8",
                    Discarded: "muted",
                  },
                },
              ],
            }}
          />
          {stabilityLine ? (
            <p className="text-xs text-muted-foreground tabular-nums">
              Stability — {stabilityLine}
            </p>
          ) : null}
        </>
      ) : (
        <p
          className={cn(
            "flex items-center justify-center text-xs text-muted-foreground",
            WIDGET_BODY
          )}
        >
          Nothing graded — no outcomes to chart.
        </p>
      )}
    </Widget>
  )
}

// ---------------------------------------------------------------------------
// Run-wide telemetry widgets (scoped by the page-level check dropdown)
// ---------------------------------------------------------------------------

function ToolUsageWidget({ telemetry, scope }) {
  // `telemetry.by_check` is the per-check split; a report from before that
  // split existed falls back to the run-wide total (still real numbers, just
  // not scoped to `scope`), flagged so the description says so.
  const perCheck = telemetry.by_check?.[scope]
  const stats = perCheck || telemetry.solver || {}
  const usingTotals = !perCheck
  const entries = Object.entries(stats?.tool_counts || {}).sort(
    (a, b) => b[1] - a[1]
  )
  return (
    <Widget
      title="Tool usage"
      description={
        usingTotals
          ? "How the solvers explored the wiki (run-wide total — this report predates the per-check telemetry split)."
          : "Tool usage across the runs."
      }
    >
      {entries.length ? (
        <WidgetChart
          spec={{
            type: "doughnut",
            labels: entries.map(([name]) => name),
            series: [{ name: "calls", data: entries.map(([, n]) => n) }],
          }}
        />
      ) : (
        <p
          className={cn(
            "flex items-center justify-center text-center text-xs text-muted-foreground",
            WIDGET_BODY
          )}
        >
          No tool calls recorded — the solvers answered without opening the
          wiki, so this run says nothing about the docs.
        </p>
      )}
    </Widget>
  )
}

function TimeToAnswerWidget({ report, scope, className }) {
  // Times come from the per-question detail, so the scope works on ANY report
  // (the tool split needs the newer telemetry shape).
  const solves = []
  for (const q of report.questions || []) {
    for (const [check, b] of Object.entries(q.checks || {})) {
      if (check !== scope) continue
      for (const a of b.attempts || []) {
        if (!a.wall_ms) continue
        solves.push({ question: q.question, run: a.run, wall: a.wall_ms })
      }
    }
  }
  const times = solves.map((s) => Math.round(s.wall / 100) / 10)
  const avg = times.length
    ? Math.round((times.reduce((a, v) => a + v, 0) / times.length) * 10) / 10
    : 0
  let minIdx = 0
  let maxIdx = 0
  times.forEach((v, i) => {
    if (v < times[minIdx]) minIdx = i
    if (v > times[maxIdx]) maxIdx = i
  })
  const multiRun = (report.config?.runs || 1) > 1
  const markerLabel = (tag, i) => [
    `${tag} — ${fmtDuration(solves[i].wall)}${multiRun ? ` (run ${solves[i].run + 1})` : ""}`,
    solves[i].question.length > 72
      ? `${solves[i].question.slice(0, 72)}…`
      : solves[i].question,
  ]

  return (
    <Widget
      className={className}
      title="Time to answer"
      description="Every solve in order with the run average — hover the marked points for the fastest and slowest questions."
    >
      {times.length ? (
        <WidgetChart
          spec={{
            type: "area",
            labels: times.map((_, i) => String(i + 1)),
            axes: false,
            xTicks: false,
            series: [
              { name: "seconds", data: times, points: false },
              {
                name: "average",
                data: times.map(() => avg),
                dashed: true,
                badge: `avg ${avg}s`,
              },
            ],
            markers:
              minIdx === maxIdx
                ? [{ index: minIdx, value: times[minIdx], label: markerLabel("Fastest", minIdx) }]
                : [
                    { index: minIdx, value: times[minIdx], label: markerLabel("Fastest", minIdx) },
                    { index: maxIdx, value: times[maxIdx], label: markerLabel("Slowest", maxIdx) },
                  ],
          }}
        />
      ) : (
        <p
          className={cn(
            "flex items-center justify-center text-xs text-muted-foreground",
            WIDGET_BODY
          )}
        >
          No timed solves recorded.
        </p>
      )}
    </Widget>
  )
}

// Outcomes as numbers (pass = 1, fail = 0): a question answered the same way
// in every run has variance 0; one that flips between runs rises toward 0.25
// (a coin flip). The LINE across all questions is the wiki's determinism
// profile — ideally flat at zero.
function VarianceWidget({ block, report, className }) {
  const perQuestion = []
  for (const q of report.questions || []) {
    const b = q.checks?.[block.check]
    if (!b || b.discarded) continue
    let passes = 0
    let total = 0
    for (const a of b.attempts || []) {
      if (a.outcome === "DISCARDED") continue
      passes += a.outcome === "PASS" ? 1 : 0
      total += 1
    }
    if (total > 0) {
      const pq = passes / total
      perQuestion.push({
        question: q.question,
        variance: Math.round(pq * (1 - pq) * 1000) / 1000,
      })
    }
  }
  if (!perQuestion.length) return null

  return (
    <Widget
      className={className}
      title="Variance across runs"
      description="How consistently each question was answered across the runs (pass = 1, fail = 0) — a flat line at 0 means every run agreed; hover a bump for the question."
    >
      <WidgetChart
        spec={{
          type: "area",
          xTicks: false,
          labels: perQuestion.map((q) =>
            q.question.length > 72 ? `${q.question.slice(0, 72)}…` : q.question
          ),
          series: [
            {
              name: "variance",
              data: perQuestion.map((q) => q.variance),
              points: false,
            },
          ],
        }}
      />
    </Widget>
  )
}

// ---------------------------------------------------------------------------
// Detailed tab

// ---------------------------------------------------------------------------

function DetailedTab({ report, activeTrace, onOpenTrace }) {
  const questions = report.questions || []
  // The solver-steps panel state (and the traces fetch) live in the view root,
  // where the panel renders beside the scroll column. `activeTrace` is the
  // OPEN attempt {q_id, check, run, question} | null, for row highlighting.
  return (
    <div className="flex min-w-0 flex-col gap-3">
      {questions.map((q) => (
        <QuestionCard
          key={q.q_id}
          q={q}
          openTrace={activeTrace}
          onOpenTrace={onOpenTrace}
        />
      ))}
    </div>
  )
}

// Badge tone per (stability, judge) shape: green all-pass, amber flaky, red
// confirmed fail, muted overturned/discarded.
function outcomeChip(block) {
  if (block.discarded)
    return { text: "discarded", variant: "secondary", cls: "" }
  // Denominate over GRADED runs only: a transient grading fault leaves one
  // run DISCARDED beside graded ones, and an ungraded run must not read as a
  // failed one (the backend's pass/flaky/confirmed tally uses the same rule).
  const graded = (block.attempts || []).filter((a) => a.outcome !== "DISCARDED")
  const passed = block.passed_runs
  const total = graded.length || block.total_runs
  if (passed === total)
    return {
      text: `passed ${passed}/${total}`,
      variant: "outline",
      cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
    }
  if (block.judge?.verdict === "pass")
    return {
      text: `overturned (${passed}/${total})`,
      variant: "secondary",
      cls: "",
    }
  if (passed > 0)
    return {
      text: `flaky ${passed}/${total}`,
      variant: "outline",
      cls: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    }
  return { text: `failed ${passed}/${total}`, variant: "destructive", cls: "" }
}

function QuestionCard({ q, openTrace, onOpenTrace }) {
  const [expanded, setExpanded] = useState(false)
  const checks = q.checks || {}
  const checkKeys = Object.keys(checks)
  const anyDetail = checkKeys.length > 0

  return (
    <div className="min-w-0 rounded-xl border bg-card p-3 shadow-xs">
      <button
        type="button"
        onClick={() => anyDetail && setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-start gap-2 text-left"
      >
        {expanded ? (
          <ChevronDownIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRightIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        )}
        <span className="min-w-0 flex-1 text-sm font-medium break-words">
          {q.question}
        </span>
        <span className="flex shrink-0 flex-wrap items-center gap-1.5">
          {checkKeys.map((c) => {
            const chip = outcomeChip(checks[c])
            return (
              <Badge
                key={c}
                variant={chip.variant}
                className={cn("shrink-0 tabular-nums", chip.cls)}
              >
                {checkLabel(c)} · {chip.text}
              </Badge>
            )
          })}
        </span>
      </button>

      {expanded
        ? checkKeys.map((c) => (
            <CheckDetail
              key={c}
              qId={q.q_id}
              question={q.question}
              check={c}
              block={checks[c]}
              openTrace={openTrace}
              onOpenTrace={onOpenTrace}
            />
          ))
        : null}
    </div>
  )
}

// A labelled prose box — the Behavior check's gold and answers are free-form
// text, not SQL, so they skip the highlighter but keep SqlBlock's chrome.
function TextBlock({ label, text }) {
  const value = typeof text === "string" ? text.trim() : ""
  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
          {label}
        </span>
      </div>
      <div className="min-w-0 rounded border bg-muted p-2 text-xs whitespace-pre-wrap break-words">
        {value || "—"}
      </div>
    </div>
  )
}

function CheckDetail({ qId, question, check, block, openTrace, onOpenTrace }) {
  const judge = block.judge
  const attempts = block.attempts || []
  // Behavior is prose end to end: the gold is an expectation, the prediction
  // is the agent's answer — no SQL highlighting, different judge wording (the
  // judge GRADED the runs; it never overturns itself).
  const prose = check === "behavior"

  return (
    <div className="mt-3 flex min-w-0 flex-col gap-2 border-t pt-3">
      <div className="flex items-center gap-2">
        <Badge variant="secondary">{checkLabel(check)}</Badge>
        {block.discarded ? (
          <span className="text-xs text-muted-foreground">
            the gold SQL couldn’t execute — excluded from the score
          </span>
        ) : null}
      </div>

      {/* The gold — human-facing only; no agent ever saw this page. */}
      {prose ? (
        <TextBlock label="Expected behavior" text={block.gold} />
      ) : (
        <SqlBlock label="Expected (gold)" sql={block.gold} />
      )}

      {judge ? (
        <div
          className={cn(
            "rounded-md border px-3 py-2 text-sm",
            judge.verdict === "pass"
              ? "bg-muted/40 text-muted-foreground"
              : "border-destructive/30 bg-destructive/5"
          )}
        >
          <span className="font-medium">
            Judge:{" "}
            {judge.verdict === "pass"
              ? "overturned — not the wiki’s fault"
              : prose
                ? "failed the expected behavior"
                : "confirmed failure"}
          </span>
          {judge.comment ? <p className="mt-1 break-words">{judge.comment}</p> : null}
          {judge.annotation ? (
            <p className="mt-1 break-words">
              <span className="font-medium">Suggested annotation:</span>{" "}
              {judge.annotation}
            </p>
          ) : null}
          {judge.judge_error ? (
            <p className="mt-1 text-destructive">
              The review itself errored ({judge.judge_error}) — counted against
              the wiki.
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="flex min-w-0 flex-col gap-1.5">
        {attempts.map((a) => {
          const traceOpen =
            openTrace &&
            openTrace.q_id === qId &&
            openTrace.check === check &&
            openTrace.run === a.run
          return (
            <AttemptRow
              key={a.run}
              attempt={a}
              prose={prose}
              traceOpen={traceOpen}
              onToggleTrace={
                a.has_trace
                  ? () =>
                      onOpenTrace(
                        traceOpen
                          ? null
                          : { q_id: qId, check, run: a.run, question }
                      )
                  : null
              }
            />
          )
        })}
      </div>
    </div>
  )
}

function AttemptRow({ attempt, prose = false, traceOpen, onToggleTrace }) {
  const [showPrediction, setShowPrediction] = useState(false)
  const pass = attempt.outcome === "PASS"
  return (
    <div className="min-w-0 rounded-md border bg-muted/40 px-2.5 py-1.5">
      <div className="flex items-center gap-2 text-xs">
        <span className="shrink-0 font-medium tabular-nums">
          Run {attempt.run + 1}
        </span>
        <span
          className={cn(
            "shrink-0 font-medium",
            pass
              ? "text-emerald-600 dark:text-emerald-500"
              : attempt.outcome === "DISCARDED"
                ? "text-muted-foreground"
                : "text-destructive"
          )}
        >
          {attempt.outcome}
        </span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">
          {attempt.reason}
        </span>
        <span className="shrink-0 text-[10px] text-muted-foreground tabular-nums">
          {attempt.tokens ? `${fmtTokens(attempt.tokens)} tok` : ""}
          {attempt.wall_ms ? ` · ${(attempt.wall_ms / 1000).toFixed(1)}s` : ""}
        </span>
        {attempt.prediction ? (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 shrink-0 px-1.5 text-[10px]"
            onClick={() => setShowPrediction((v) => !v)}
          >
            {showPrediction ? "hide" : prose ? "answer" : "SQL"}
          </Button>
        ) : null}
        {onToggleTrace ? (
          <Button
            variant="ghost"
            size="sm"
            className={cn(
              "h-6 shrink-0 gap-1 px-1.5 text-[10px]",
              traceOpen && "text-primary"
            )}
            onClick={onToggleTrace}
            aria-expanded={Boolean(traceOpen)}
          >
            <ListTreeIcon className="size-3" />
            Steps
          </Button>
        ) : null}
      </div>
      {showPrediction && attempt.prediction ? (
        <div className="mt-1.5">
          {prose ? (
            <TextBlock
              label={`Run ${attempt.run + 1} answer`}
              text={attempt.prediction}
            />
          ) : (
            <SqlBlock
              label={`Run ${attempt.run + 1} answer`}
              sql={attempt.prediction}
            />
          )}
        </div>
      ) : null}
    </div>
  )
}
