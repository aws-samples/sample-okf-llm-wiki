// The Analysis page — two tabs over the analyses feature's durable state:
//
//   Analyses — every analysis document across datasets. A row opens the
//   analysis rendered as markdown in the main layout (#/analysis/a~<domain>~
//   <dataset>~<name> — the page's single trailing-id slot, discriminated by
//   prefix from report ids). Owners get Edit and Delete on THAT page: Edit
//   swaps the rendered document for an inline editor (no modals), Delete is
//   a two-step destructive button. Both are owner-gated server-side; the
//   buttons just mirror owned_by_you. The agent deliberately has no delete
//   tool — this page is where an analysis is retired.
//
//   Reports — every report published by analysis runs, filterable by dataset
//   and/or analysis. A row opens the report rendered in the main layout
//   (#/analysis/<report_id>) with a PDF download, on the chat ReportPanel's
//   shared document plumbing.

import {
  ArrowLeftIcon,
  DatabaseIcon,
  DownloadIcon,
  Loader2Icon,
  MicroscopeIcon,
  PencilIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { toast } from "sonner"

import { AnalysisDocument } from "@/components/chat/AnalysisPeek"
import { ReportBody } from "@/components/chat/ReportPanel"
import { useResolvedTheme } from "@/hooks/useResolvedTheme"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"

const FILTER_ALL = "__all__"

// Analysis detail ids share the page's single trailing URL slot with report
// ids ("rep~…"), discriminated by prefix: a~<domain>~<dataset>~<name>.
// Every segment is a slug (no "~" possible), so the split is unambiguous.
function analysisItemId(a) {
  return `a~${a.data_domain}~${a.dataset}~${a.name}`
}

function parseAnalysisItemId(id) {
  const parts = String(id || "").split("~")
  return parts.length === 4 && parts[0] === "a" && parts.every(Boolean)
    ? { domain: parts[1], dataset: parts[2], name: parts[3] }
    : null
}

// ---------------------------------------------------------------------------
// Report detail — a published report rendered full-page (main layout).
// ---------------------------------------------------------------------------

function ReportPage({ api, reportId, row, rowLoading = false, onBack }) {
  const [state, setState] = useState({
    loading: true,
    error: null,
    html: null,
    pdfUrl: "",
  })

  useEffect(() => {
    if (!api || !reportId) return undefined
    let cancelled = false
    setState({ loading: true, error: null, html: null, pdfUrl: "" })
    ;(async () => {
      try {
        const meta = await api.getReport(reportId)
        const res = await fetch(meta.html_url, { credentials: "omit" })
        if (!res.ok) {
          throw new Error(`could not fetch the report document (${res.status})`)
        }
        const html = await res.text()
        if (!cancelled) {
          setState({
            loading: false,
            error: null,
            html,
            pdfUrl: meta.pdf_url || "",
          })
        }
      } catch (e) {
        if (!cancelled) {
          setState({
            loading: false,
            error: e?.message || "failed to load the report",
            html: null,
            pdfUrl: "",
          })
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [api, reportId])

  // Live theme adoption — the ReportPanel arrangement: the composed HTML
  // carries a dark token set behind [data-theme="dark"]; downloads stay the
  // light print document. On top of that, THIS page strips the document's
  // own page surface (transparent body, left-aligned column) so the report
  // sits directly on the app background, its content sharing the header's
  // left edge — the analysis-detail arrangement.
  const theme = useResolvedTheme()
  const themedHtml = useMemo(() => {
    if (!state.html) return state.html
    const html =
      theme === "dark"
        ? state.html.replace(
            '<html lang="en">',
            '<html lang="en" data-theme="dark">'
          )
        : state.html
    return html.replace(
      "</head>",
      "<style>body{background:transparent!important;padding:4px 0 48px!important}" +
        "main,header.rpt,footer.rpt{margin-left:0!important}" +
        // The light code/SQL chip (#f5f5f4) was tuned for the white paper
        // surface — on the app page background (#FCFCFB) it disappears, so
        // step it down to stone-200. Dark stays: its token block is scoped
        // to [data-theme=dark] (higher specificity) and already contrasts.
        ":root{--rpt-code-bg:#e7e5e4}</style></head>"
    )
  }, [state.html, theme])

  const title = row?.title || "Report"
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Fixed header — the analysis-detail arrangement: back in the reading
          column's left gutter, heading + actions on one row, meta below.
          Only the document region scrolls (inside the iframe). */}
      <div className="relative mx-auto w-full max-w-4xl py-3 pr-4 pl-9">
        <Button
          variant="ghost"
          size="icon"
          className="absolute top-3 left-0 size-8 shrink-0"
          onClick={onBack}
          aria-label="Back to the Analysis page"
        >
          <ArrowLeftIcon className="size-4" />
        </Button>
        <div className="flex h-8 items-center gap-2">
          {rowLoading && !row ? (
            <Skeleton className="h-5 w-64" />
          ) : (
            <h2
              className="min-w-0 truncate font-heading text-lg font-medium"
              title={title}
            >
              {title}
            </h2>
          )}
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {state.pdfUrl ? (
              <Button variant="outline" size="sm" asChild>
                <a href={state.pdfUrl} download>
                  <DownloadIcon data-icon="inline-start" />
                  Download PDF
                </a>
              </Button>
            ) : null}
          </div>
        </div>
        {rowLoading && !row ? (
          <Skeleton className="mt-1.5 h-3 w-80" />
        ) : row ? (
          <div className="mt-0.5 truncate text-xs text-muted-foreground">
            {row.analysis} v{row.analysis_version} · {row.data_domain}/
            {row.dataset}
            {row.published_at
              ? ` · ${String(row.published_at).slice(0, 10)}`
              : ""}
          </div>
        ) : null}
      </div>
      {/* The document, directly on the app background — same column + gutter
          as the header, the iframe's own page owning the scrollbar. */}
      <div className="min-h-0 flex-1 overflow-hidden">
        <div className="mx-auto h-full w-full max-w-4xl pr-4 pl-9">
          <ReportBody
            className="h-full"
            transparent
            loading={state.loading}
            error={state.error}
            html={themedHtml}
            title={title}
          />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Analysis detail — the document rendered as markdown; owners edit it inline
// (the same text the agent maintains) and delete from here. No modals.
// ---------------------------------------------------------------------------

function AnalysisPage({ api, target, onBack, onMutated }) {
  const { domain, dataset, name } = target
  const [doc, setDoc] = useState(null)
  const [error, setError] = useState("")
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState("")
  const [saving, setSaving] = useState(false)
  const [problems, setProblems] = useState("")
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const load = useCallback(() => {
    setError("")
    api
      .getAnalysis(domain, dataset, name)
      .then(setDoc)
      .catch((e) => setError(String(e?.message || e)))
  }, [api, domain, dataset, name])
  useEffect(() => {
    load()
  }, [load])

  // The armed delete disarms itself — an accidental click shouldn't leave a
  // red trap waiting minutes later.
  useEffect(() => {
    if (!confirmDelete) return undefined
    const t = setTimeout(() => setConfirmDelete(false), 4000)
    return () => clearTimeout(t)
  }, [confirmDelete])

  const startEdit = () => {
    setDraft(doc?.document || "")
    setProblems("")
    setEditing(true)
  }

  const save = async () => {
    setSaving(true)
    setProblems("")
    try {
      await api.updateAnalysis(domain, dataset, name, {
        document: draft,
        version: doc.version,
      })
      setEditing(false)
      load()
    } catch (err) {
      // 400 carries the named validation problems; 409 the stale-version
      // hint — both belong beside the editor, where the fix happens.
      setProblems(String(err?.message || err))
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    setDeleting(true)
    try {
      await api.deleteAnalysis(domain, dataset, name)
      toast.success(`Deleted ${name}`)
      onMutated?.() // the delete purged publication rows — refetch the Reports tab
      onBack()
    } catch (err) {
      toast.error(`Could not delete: ${err.message || err}`)
      setDeleting(false)
      setConfirmDelete(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Fixed header — only the document region below scrolls. Back lives
          in a fixed left GUTTER inside the reading column (the Benchmark
          Studio arrangement: column pl-9, button at left-0), so the title
          and the document below share ONE left edge with the arrow just
          beside it. */}
      <div className="relative mx-auto w-full max-w-4xl py-3 pr-4 pl-9">
        {/* The title ROW: a proper heading with the actions on the same
            line; the meta line sits below it. The back arrow is centered on
            the heading row (h-8, matching the buttons). */}
        <Button
          variant="ghost"
          size="icon"
          className="absolute top-3 left-0 size-8 shrink-0"
          onClick={onBack}
          aria-label="Back to the Analysis page"
        >
          <ArrowLeftIcon className="size-4" />
        </Button>
        <div className="flex h-8 items-center gap-2">
          {doc === null && !error ? (
            <Skeleton className="h-5 w-64" />
          ) : (
            <h2
              className="min-w-0 truncate font-heading text-lg font-medium"
              title={doc?.title || name}
            >
              {doc?.title || name}
            </h2>
          )}
          {doc?.owned_by_you ? (
            <Badge variant="secondary" className="shrink-0 text-[10px]">
              yours
            </Badge>
          ) : null}
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {doc?.owned_by_you && !editing ? (
              <>
                <Button variant="outline" size="sm" onClick={startEdit}>
                  <PencilIcon data-icon="inline-start" />
                  Edit
                </Button>
                <Button
                  variant={confirmDelete ? "destructive" : "outline"}
                  size="sm"
                  disabled={deleting}
                  onClick={() =>
                    confirmDelete ? remove() : setConfirmDelete(true)
                  }
                >
                  {deleting ? (
                    <Spinner />
                  ) : (
                    <Trash2Icon data-icon="inline-start" />
                  )}
                  {confirmDelete ? "Confirm Delete" : "Delete"}
                </Button>
              </>
            ) : null}
            {editing ? (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setEditing(false)}
                  disabled={saving}
                >
                  <XIcon data-icon="inline-start" />
                  Cancel
                </Button>
                <Button size="sm" onClick={save} disabled={saving}>
                  {saving ? (
                    <Spinner />
                  ) : (
                    <PencilIcon data-icon="inline-start" />
                  )}
                  Save
                </Button>
              </>
            ) : null}
          </div>
        </div>
        {doc === null && !error ? (
          <Skeleton className="mt-1.5 h-3 w-80" />
        ) : (
          <div className="mt-0.5 truncate text-xs text-muted-foreground">
            {name} · {domain}/{dataset}
            {doc ? ` · v${doc.version}` : ""}
          </div>
        )}
      </div>

      {error ? (
        <div className="mx-auto w-full max-w-4xl pr-4 pl-9 text-sm text-destructive">
          {error}
        </div>
      ) : doc === null ? (
        // Skeleton document while the content loads — a short descriptive
        // block, a question card, then prose lines.
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 pr-4 pl-9">
          <div className="flex flex-col gap-1.5">
            <Skeleton className="h-3.5 w-full" />
            <Skeleton className="h-3.5 w-2/3" />
          </div>
          <Skeleton className="h-16 w-full rounded-md" />
          <div className="flex flex-col gap-1.5 pt-2">
            <Skeleton className="h-3.5 w-1/3" />
            <Skeleton className="h-3.5 w-full" />
            <Skeleton className="h-3.5 w-full" />
            <Skeleton className="h-3.5 w-3/4" />
          </div>
        </div>
      ) : editing ? (
        // The editor fills the remaining height; the textarea owns the scroll.
        <div className="mx-auto flex min-h-0 w-full max-w-4xl flex-1 flex-col gap-2 pr-4 pb-4 pl-9">
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className="min-h-0 flex-1 resize-none font-mono text-xs leading-relaxed"
          />
          {problems ? (
            <p className="text-sm whitespace-pre-wrap text-destructive">
              {problems}
            </p>
          ) : null}
        </div>
      ) : (
        // The document, directly on the layout — the page's only scroll region.
        <div className="okf-thin-scroll min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-4xl pr-4 pb-8 pl-9">
            <AnalysisDocument doc={doc} questions={doc.questions} />
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Analyses tab — the cross-dataset list; a row opens the detail page.
// ---------------------------------------------------------------------------

function AnalysesTab({ api, onOpen }) {
  const [analyses, setAnalyses] = useState(null)
  const [error, setError] = useState("")
  const [datasetFilter, setDatasetFilter] = useState(FILTER_ALL)

  useEffect(() => {
    let alive = true
    api
      .listAnalyses()
      .then((res) => {
        if (alive) setAnalyses(Array.isArray(res?.analyses) ? res.analyses : [])
      })
      .catch((e) => {
        if (alive) setError(String(e?.message || e))
      })
    return () => {
      alive = false
    }
  }, [api])

  const datasets = useMemo(
    () =>
      [
        ...new Set(
          (analyses || []).map((a) => `${a.data_domain}/${a.dataset}`)
        ),
      ].sort(),
    [analyses]
  )
  const filtered = useMemo(
    () =>
      (analyses || []).filter(
        (a) =>
          datasetFilter === FILTER_ALL ||
          `${a.data_domain}/${a.dataset}` === datasetFilter
      ),
    [analyses, datasetFilter]
  )

  if (error) return <div className="text-sm text-destructive">{error}</div>
  if (analyses === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2Icon className="h-4 w-4 animate-spin" /> loading
      </div>
    )
  }
  if (analyses.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        No analyses yet. Ask the agent in chat to create one for your dataset.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Select value={datasetFilter} onValueChange={setDatasetFilter}>
          <SelectTrigger aria-label="Filter by dataset" className="w-52">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            <SelectItem value={FILTER_ALL}>All Datasets</SelectItem>
            {datasets.map((d) => (
              <SelectItem key={d} value={d}>
                <DatabaseIcon className="size-4" />
                {d}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {filtered.length === 0 ? (
        <p className="px-1 py-6 text-center text-sm text-muted-foreground">
          No analyses match the current filter.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {filtered.map((a) => (
            <button
              key={`${a.data_domain}/${a.dataset}/${a.name}`}
              className="flex items-center gap-3 rounded-lg border border-edge bg-card px-4 py-3 text-left hover:bg-accent/50"
              onClick={() => onOpen(analysisItemId(a))}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium">
                    {a.title || a.name}
                  </span>
                  {a.owned_by_you ? (
                    <Badge variant="secondary" className="shrink-0 text-[10px]">
                      yours
                    </Badge>
                  ) : null}
                </div>
                <div className="truncate text-xs text-muted-foreground">
                  {a.name} · {a.data_domain}/{a.dataset} · v{a.version} ·{" "}
                  {a.questions} question{a.questions === 1 ? "" : "s"} ·{" "}
                  {a.published_reports} report
                  {a.published_reports === 1 ? "" : "s"}
                </div>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Reports tab — published reports, filterable, opening the full-page render.
// ---------------------------------------------------------------------------

function ReportsTab({ reports, error, onOpen }) {
  const [datasetFilter, setDatasetFilter] = useState(FILTER_ALL)
  const [analysisFilter, setAnalysisFilter] = useState(FILTER_ALL)

  const datasets = useMemo(
    () =>
      [
        ...new Set((reports || []).map((r) => `${r.data_domain}/${r.dataset}`)),
      ].sort(),
    [reports]
  )
  // Analysis options follow the dataset choice (an analysis is dataset-scoped,
  // so a cross-dataset name collision must not merge two analyses).
  const analyses = useMemo(() => {
    const pool = (reports || []).filter(
      (r) =>
        datasetFilter === FILTER_ALL ||
        `${r.data_domain}/${r.dataset}` === datasetFilter
    )
    return [...new Set(pool.map((r) => r.analysis))].sort()
  }, [reports, datasetFilter])

  // Derived reset: a dataset switch that orphans the analysis choice clears it
  // (setState-during-render, keyed on the option list — the MemoryView pattern).
  const [analysisKey, setAnalysisKey] = useState(analyses)
  if (analysisKey !== analyses) {
    setAnalysisKey(analyses)
    if (analysisFilter !== FILTER_ALL && !analyses.includes(analysisFilter)) {
      setAnalysisFilter(FILTER_ALL)
    }
  }

  const filtered = useMemo(
    () =>
      (reports || []).filter(
        (r) =>
          (datasetFilter === FILTER_ALL ||
            `${r.data_domain}/${r.dataset}` === datasetFilter) &&
          (analysisFilter === FILTER_ALL || r.analysis === analysisFilter)
      ),
    [reports, datasetFilter, analysisFilter]
  )

  if (error) return <div className="text-sm text-destructive">{error}</div>
  if (reports === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2Icon className="h-4 w-4 animate-spin" /> loading
      </div>
    )
  }
  if (reports.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        No reports yet. When an analysis runs in chat, its report is published
        here.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Select value={datasetFilter} onValueChange={setDatasetFilter}>
          <SelectTrigger aria-label="Filter by dataset" className="w-52">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            <SelectItem value={FILTER_ALL}>All Datasets</SelectItem>
            {datasets.map((d) => (
              <SelectItem key={d} value={d}>
                <DatabaseIcon className="size-4" />
                {d}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={analysisFilter} onValueChange={setAnalysisFilter}>
          <SelectTrigger aria-label="Filter by analysis" className="w-52">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            <SelectItem value={FILTER_ALL}>All Analyses</SelectItem>
            {analyses.map((a) => (
              <SelectItem key={a} value={a}>
                <MicroscopeIcon className="size-4" />
                {a}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {filtered.length === 0 ? (
        <p className="px-1 py-6 text-center text-sm text-muted-foreground">
          No published reports match the current filters.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {filtered.map((r) => (
            <button
              key={r.report_id}
              className="flex items-center gap-3 rounded-lg border border-edge bg-card px-4 py-3 text-left hover:bg-accent/50"
              onClick={() => onOpen(r.report_id)}
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">
                  {r.title || r.report_id}
                </div>
                <div className="truncate text-xs text-muted-foreground">
                  {r.analysis} v{r.analysis_version} · {r.data_domain}/
                  {r.dataset}
                </div>
              </div>
              <span className="shrink-0 text-xs text-muted-foreground">
                {String(r.published_at || "").slice(0, 10)}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------

export function AnalysisView({ api, reportId, onOpen, onBack }) {
  // The reports list is shared: the Reports tab renders it, and the report
  // detail header looks its row up by id (reports have no DB row of their
  // own — GET /report serves only artifact URLs).
  const [reports, setReports] = useState(null)
  const [reportsError, setReportsError] = useState("")
  // Bumped after a destructive mutation on a detail page (deleting an
  // analysis purges its publication rows): the component survives the
  // detail→landing navigation, so without a refetch the Reports tab kept
  // listing the just-purged publications.
  const [reportsReload, setReportsReload] = useState(0)
  useEffect(() => {
    let alive = true
    api
      .listAnalysisReports()
      .then((res) => {
        if (alive) setReports(Array.isArray(res?.reports) ? res.reports : [])
      })
      .catch((e) => {
        if (alive) setReportsError(String(e?.message || e))
      })
    return () => {
      alive = false
    }
  }, [api, reportsReload])

  // The trailing URL slot carries either detail: an analysis (a~…) or a
  // published report (rep~…).
  const analysisTarget = parseAnalysisItemId(reportId)
  if (analysisTarget) {
    return (
      <AnalysisPage
        api={api}
        target={analysisTarget}
        onBack={onBack}
        onMutated={() => setReportsReload((k) => k + 1)}
      />
    )
  }
  if (reportId) {
    const row = (reports || []).find((r) => r.report_id === reportId) || null
    return (
      <ReportPage
        api={api}
        reportId={reportId}
        row={row}
        // The skeleton must have a terminal: when the publications list
        // FAILS, reports stays null forever — fall back to the plain title
        // instead of shimmering indefinitely.
        rowLoading={reports === null && !reportsError}
        onBack={onBack}
      />
    )
  }

  return (
    <div className="okf-thin-scroll min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 p-4">
        <h1 className="text-lg font-semibold">Analysis</h1>
        <p className="text-sm text-muted-foreground">
          Saved analyses the agent can run on your data, and the report each run
          publishes.
        </p>
        <Tabs defaultValue="analyses" className="gap-3">
          <TabsList>
            <TabsTrigger value="analyses">Analyses</TabsTrigger>
            <TabsTrigger value="reports">Reports</TabsTrigger>
          </TabsList>
          <TabsContent value="analyses">
            <AnalysesTab api={api} onOpen={onOpen} />
          </TabsContent>
          <TabsContent value="reports">
            <ReportsTab
              reports={reports}
              error={reportsError}
              onOpen={onOpen}
            />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}
