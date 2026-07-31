// Benchmark Studio — one page per dataset: the question set, and the reports.
//
// Benchmarking is standalone and human-led: no harvest, no improve-and-rescore
// loop, no stop target. The QUESTION SET lives here (upload/replace — it's the
// dataset's standing answer key, not a per-run choice); everything a single run
// decides (which checks, solver + judge models, N independent runs, the wiki
// version to target) lives in the "New benchmark" modal, so the page stays a
// short read and the run form is a deliberate act.
//
// Runs take no harvest lease — they're concurrent with harvests and with each
// other. Live progress rides the REPORT# index row (this list polls it); a row
// becomes clickable once the run completes and opens the report detail page
// (#/benchmark/<domain>/<dataset>/<report_id>).

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  GaugeIcon,
  PlayIcon,
  Trash2Icon,
  UploadIcon,
  XCircleIcon,
} from "lucide-react"

import { uploadToPresigned } from "@/lib/api"
import {
  GROUPED_MODEL_CATALOG,
  MODEL_CATALOG,
  defaultEffortFor,
  effortsFor,
} from "@/lib/harvestModels"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"

// The checks, in report order. Labels + one-line meanings mirror
// docs/BENCHMARK_GUIDE.md.
export const CHECK_META = [
  {
    key: "sql",
    label: "Accuracy",
    help: "write a full SQL query; result set must match the gold SQL's",
  },
  {
    key: "behavior",
    label: "Behavior",
    help: "answer in free form; the judge grades every run against the expected behavior",
  },
]

const LIST_POLL_MS = 4000
const ACTIVE_STATUSES = new Set(["queued", "running"])

export function fmtScore(v) {
  if (typeof v !== "number") return "—"
  return `${Math.round(v * 100)}%`
}

function fmtWhen(iso) {
  if (!iso) return ""
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export function fmtVersionLabel(v) {
  if (!v) return ""
  if (v.completed_at) {
    try {
      return new Date(v.completed_at).toLocaleString()
    } catch {
      return v.completed_at
    }
  }
  return (v.version_id || "").slice(0, 8)
}

export default function BenchmarkView({ api, selection, onOpenReport }) {
  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const hasSelection = Boolean(domain && dataset)

  const [questions, setQuestions] = useState(null)
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef(null)

  const [reports, setReports] = useState(null) // null until first load
  const [listError, setListError] = useState(null)
  const [deleting, setDeleting] = useState(() => new Set())

  // Bundle versions for the setup modal's target picker (cheap; loaded with the
  // page so the modal opens without a spinner).
  const [versions, setVersions] = useState([])
  const [setupOpen, setSetupOpen] = useState(false)

  const inspect = useCallback(async () => {
    if (!api || !hasSelection) return
    try {
      setQuestions(await api.inspectBenchmarkQuestions(domain, dataset))
    } catch (e) {
      setQuestions({ uploaded: false, inspectError: e.message || String(e) })
    }
  }, [api, domain, dataset, hasSelection])

  const loadReports = useCallback(async () => {
    if (!api || !hasSelection) return
    try {
      const res = await api.listBenchmarkReports(domain, dataset)
      setReports(Array.isArray(res?.reports) ? res.reports : [])
      setListError(null)
    } catch (e) {
      setListError(e.message || String(e))
    }
  }, [api, domain, dataset, hasSelection])

  const loadVersions = useCallback(async () => {
    if (!api || !hasSelection) return
    try {
      const res = await api.listBundleVersions(domain, dataset)
      setVersions(Array.isArray(res?.versions) ? res.versions : [])
    } catch {
      setVersions([]) // no published versions yet — "current" still works
    }
  }, [api, domain, dataset, hasSelection])

  // Reset + load on dataset change.
  useEffect(() => {
    setQuestions(null)
    setReports(null)
    setSetupOpen(false)
    inspect()
    loadReports()
    loadVersions()
  }, [inspect, loadReports, loadVersions])

  // Poll the list while any run is in flight (progress rides the index rows).
  const anyActive = useMemo(
    () => (reports || []).some((r) => ACTIVE_STATUSES.has(r.status)),
    [reports]
  )
  useEffect(() => {
    if (!anyActive) return
    const id = setInterval(loadReports, LIST_POLL_MS)
    return () => clearInterval(id)
  }, [anyActive, loadReports])

  const onPickFile = async (e) => {
    const file = e.target.files?.[0]
    if (fileInputRef.current) fileInputRef.current.value = ""
    if (!file || !hasSelection) return
    setUploading(true)
    try {
      const { url, fields, max_bytes } = await api.presignBenchmarkUpload(
        domain,
        dataset,
        file.type || "text/csv"
      )
      if (!url || !fields)
        throw new Error("presign response missing 'url'/'fields'")
      if (max_bytes && file.size > max_bytes) {
        throw new Error(
          `file is ${(file.size / 1048576).toFixed(1)} MB; max is ${(
            max_bytes / 1048576
          ).toFixed(0)} MB`
        )
      }
      await uploadToPresigned({ url, fields }, file)
      const parsed = await api.inspectBenchmarkQuestions(domain, dataset)
      setQuestions(parsed)
      if (parsed?.valid) {
        toast.success(
          `${parsed.count} question${parsed.count === 1 ? "" : "s"} extracted` +
            (parsed.capped
              ? ` (capped from ${parsed.total_in_csv} at ${parsed.max_questions})`
              : "")
        )
      } else {
        toast.error(`CSV format problem: ${parsed?.error || "could not parse"}`)
      }
    } catch (err) {
      toast.error(`Upload failed: ${err.message || err}`)
    } finally {
      setUploading(false)
    }
  }

  const deleteReport = async (reportId) => {
    setDeleting((prev) => new Set(prev).add(reportId))
    try {
      await api.deleteBenchmarkReport(domain, dataset, reportId)
      toast.success("Report deleted.")
      await loadReports()
    } catch (e) {
      toast.error(`Could not delete: ${e.message || e}`)
    } finally {
      setDeleting((prev) => {
        const next = new Set(prev)
        next.delete(reportId)
        return next
      })
    }
  }

  if (!hasSelection) {
    return (
      <Alert>
        <GaugeIcon />
        <AlertTitle>Select a dataset first</AlertTitle>
        <AlertDescription>
          Pick a dataset from the sidebar to benchmark its wiki.
        </AlertDescription>
      </Alert>
    )
  }

  return (
    // Cap the card at the view column's height (natural height below it) and
    // let ONLY the Reports list scroll: header + question set stay put, so the
    // min-h-0 shrink chain runs Card → CardContent → reports section → list.
    <Card className="max-h-full min-h-0">
      <CardHeader className="shrink-0 border-b">
        <CardTitle className="flex items-center gap-2">
          <GaugeIcon className="size-4" />
          Benchmark Studio
        </CardTitle>
        <CardDescription>
          Measure how well the wiki for{" "}
          <span className="font-medium text-foreground">
            {domain}/{dataset}
          </span>{" "}
          answers real questions.
        </CardDescription>
        {/* One action only: the list reloads itself (on mount, after a start or
            delete, and on a poll while any run is active), so a manual Refresh
            was never the way to see current state. */}
        <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
          <Button
            onClick={() => setSetupOpen(true)}
            disabled={!questions?.valid}
          >
            <PlayIcon data-icon="inline-start" />
            New benchmark
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-col gap-6">
        {/* The dataset's standing answer key — lands OFF the okf/ mount so the
            gold stays hidden from every LLM role. */}
        <div className="flex shrink-0 flex-col gap-2">
          <Label>Question set</Label>
          <p className="text-sm text-muted-foreground">
            A CSV with a <code>question</code> column and one gold column per
            check: <code>gold_sql</code> (Accuracy),{" "}
            <code>expected_behavior</code> (Behavior — free-form: what the
            agent should do, e.g. refuse, cite a caveat, not invent numbers).
          </p>
          <div>
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              accept=".csv,text/csv"
              onChange={onPickFile}
            />
            <Button
              variant="outline"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
            >
              {uploading ? <Spinner /> : <UploadIcon data-icon="inline-start" />}
              {questions?.valid
                ? "Replace questions CSV"
                : "Upload questions CSV"}
            </Button>
          </div>
          <QuestionSetStatus questions={questions} />
        </div>

        <Separator />

        {/* Report history. */}
        <div className="flex min-h-0 flex-col gap-2">
          <div className="flex shrink-0 items-baseline justify-between gap-2">
            <Label>Reports</Label>
          </div>
          {listError ? (
            <Alert variant="destructive">
              <AlertTitle>Couldn’t load reports</AlertTitle>
              <AlertDescription>{listError}</AlertDescription>
            </Alert>
          ) : reports === null ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : reports.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {questions?.valid
                ? "No reports yet — start one with “New benchmark”."
                : "No reports yet — upload a question set to run your first benchmark."}
            </p>
          ) : (
            <div className="okf-thin-scroll flex min-h-0 flex-col gap-2 overflow-y-auto pr-1">
              {reports.map((r) => (
                <ReportRow
                  key={r.report_id}
                  report={r}
                  onOpen={onOpenReport}
                  onDelete={deleteReport}
                  deleting={deleting.has(r.report_id)}
                />
              ))}
            </div>
          )}
        </div>
      </CardContent>

      {setupOpen ? (
        <NewBenchmarkDialog
          open={setupOpen}
          onOpenChange={setSetupOpen}
          api={api}
          domain={domain}
          dataset={dataset}
          questions={questions}
          versions={versions}
          onStarted={loadReports}
        />
      ) : null}
    </Card>
  )
}

// The run setup: everything ONE benchmark decides. Mounted only while open, so
// each run starts from the defaults (Accuracy, 3 runs, the catalog's default
// model, the current wiki) rather than inheriting a stale setup.
function NewBenchmarkDialog({
  open,
  onOpenChange,
  api,
  domain,
  dataset,
  questions,
  versions,
  onStarted,
}) {
  const defaultModel = MODEL_CATALOG[0]?.model || ""
  const [checks, setChecks] = useState(() => new Set(["sql"]))
  const [solverModel, setSolverModel] = useState(defaultModel)
  const [solverEffort, setSolverEffort] = useState(defaultEffortFor(defaultModel))
  const [judgeModel, setJudgeModel] = useState(defaultModel)
  const [judgeEffort, setJudgeEffort] = useState(defaultEffortFor(defaultModel))
  const [runs, setRuns] = useState("3")
  const [versionId, setVersionId] = useState("") // "" = current wiki
  // Behavior-only option: hand the Behavior solver read-only run_sql against
  // the live data (a truer consumer simulation). Accuracy solvers stay
  // SQL-blind regardless — live queries would let them brute-force EX.
  const [behaviorLiveSql, setBehaviorLiveSql] = useState(false)
  const [starting, setStarting] = useState(false)

  const checkCounts = questions?.check_counts || {}
  const enabledWithQuestions = [...checks].some((c) => (checkCounts[c] || 0) > 0)
  const canStart = checks.size > 0 && enabledWithQuestions && !starting

  const toggleCheck = (key) =>
    setChecks((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  // Snap the effort to the model's default when a picked model doesn't offer
  // the current one (same pattern as the harvest settings sheet).
  const pickModel = (setModel, setEffort, currentEffort) => (model) => {
    setModel(model)
    if (!effortsFor(model).includes(currentEffort))
      setEffort(defaultEffortFor(model))
  }

  const startRun = async () => {
    setStarting(true)
    try {
      const res = await api.startBenchmarkRun(domain, dataset, {
        checks: [...checks],
        runs: Number(runs),
        solver_model: solverModel || undefined,
        solver_effort: solverEffort || undefined,
        judge_model: judgeModel || undefined,
        judge_effort: judgeEffort || undefined,
        version_id: versionId || undefined,
        behavior_live_sql:
          checks.has("behavior") && behaviorLiveSql ? true : undefined,
      })
      toast.success(
        `Benchmark started — ${res.question_count} question(s), ${res.runs} run(s).`
      )
      onOpenChange(false)
      await onStarted()
    } catch (e) {
      toast.error(`Could not start: ${e.message || e}`)
    } finally {
      setStarting(false)
    }
  }

  // The Athena/token cost scales with runs × checks × participating questions —
  // surfaced so a 5-run three-check start isn't a surprise.
  const solveCount =
    Number(runs) * [...checks].reduce((n, c) => n + (checkCounts[c] || 0), 0)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <GaugeIcon className="size-4" />
            New benchmark
          </DialogTitle>
          <DialogDescription>
            {domain}/{dataset} · {questions?.count || 0} question
            {questions?.count === 1 ? "" : "s"} in the current set.
          </DialogDescription>
        </DialogHeader>

        <div className="okf-thin-scroll flex max-h-[65vh] min-w-0 flex-col gap-4 overflow-y-auto pr-1">
          <div className="flex flex-col gap-2">
            <Label>Checks to run</Label>
            <div className="grid gap-2 sm:grid-cols-2">
              {CHECK_META.map((c) => {
                const count = checkCounts[c.key] || 0
                return (
                  <label
                    key={c.key}
                    className={cn(
                      "flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2 transition-colors hover:bg-muted/50",
                      checks.has(c.key) && "border-primary/50 bg-muted/40",
                      count === 0 && "opacity-60"
                    )}
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5 size-3.5 shrink-0 accent-primary"
                      checked={checks.has(c.key)}
                      onChange={() => toggleCheck(c.key)}
                    />
                    <span className="flex min-w-0 flex-col gap-0.5">
                      {/* nowrap on the count so "25 questions" never splits
                          across lines — it drops as one unit or not at all. */}
                      <span className="text-sm font-medium">
                        {c.label}
                        <span className="ml-1.5 text-xs font-normal whitespace-nowrap text-muted-foreground tabular-nums">
                          {count} question{count === 1 ? "" : "s"}
                        </span>
                      </span>
                      <span className="text-xs leading-snug text-muted-foreground">
                        {c.help}
                      </span>
                    </span>
                  </label>
                )
              })}
            </div>
            {checks.has("behavior") ? (
              <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2 transition-colors hover:bg-muted/50">
                <input
                  type="checkbox"
                  className="mt-0.5 size-3.5 shrink-0 accent-primary"
                  checked={behaviorLiveSql}
                  onChange={() => setBehaviorLiveSql((v) => !v)}
                />
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-sm font-medium">
                    Behavior solver: live SQL
                  </span>
                  <span className="text-xs leading-snug text-muted-foreground">
                    Also give the Behavior solver read-only{" "}
                    <code className="font-mono">run_sql</code> against the live
                    data — simulates a consumer agent with query access.
                  </span>
                </span>
              </label>
            ) : null}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <ModelEffortField
              idPrefix="bench-solver"
              label="Solver model"
              help="The consumer being simulated."
              model={solverModel}
              effort={solverEffort}
              onModel={pickModel(setSolverModel, setSolverEffort, solverEffort)}
              onEffort={setSolverEffort}
            />
            <ModelEffortField
              idPrefix="bench-judge"
              label="Judge model"
              help="Reviews every failure (always on)."
              model={judgeModel}
              effort={judgeEffort}
              onModel={pickModel(setJudgeModel, setJudgeEffort, judgeEffort)}
              onEffort={setJudgeEffort}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-2">
              <Label htmlFor="bench-runs">Independent runs</Label>
              <Select value={runs} onValueChange={setRuns}>
                <SelectTrigger id="bench-runs" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {["1", "2", "3", "4", "5"].map((n) => (
                    <SelectItem key={n} value={n}>
                      {n} run{n === "1" ? "" : "s"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Each question is solved independently per run.
              </p>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="bench-version">Wiki version</Label>
              <Select
                value={versionId || "__current__"}
                onValueChange={(v) => setVersionId(v === "__current__" ? "" : v)}
              >
                <SelectTrigger id="bench-version" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__current__">
                    Current (live wiki)
                  </SelectItem>
                  {versions.map((v) => (
                    <SelectItem key={v.version_id} value={v.version_id}>
                      {fmtVersionLabel(v)}
                      {v.current ? " · current" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Pinning targets that version's docs.
              </p>
            </div>
          </div>
        </div>

        <DialogFooter className="items-center sm:justify-between">
          <span className="text-xs text-muted-foreground tabular-nums">
            {checks.size > 0 && !enabledWithQuestions
              ? "No question in the set participates in the selected check(s)."
              : `${solveCount} solve${solveCount === 1 ? "" : "s"} · judge reviews every failure`}
          </span>
          <Button onClick={startRun} disabled={!canStart}>
            {starting ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <PlayIcon data-icon="inline-start" />
            )}
            Start benchmark
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// One model + effort picker pair over the shared harvest catalog (the benchmark
// pickers use the harvest catalog by design — not chat's pinned model).
function ModelEffortField({ idPrefix, label, help, model, effort, onModel, onEffort }) {
  const efforts = effortsFor(model)
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={`${idPrefix}-model`}>{label}</Label>
      <div className="flex gap-2">
        <Select value={model} onValueChange={onModel}>
          <SelectTrigger id={`${idPrefix}-model`} className="min-w-0 flex-1">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {GROUPED_MODEL_CATALOG.map((group) => (
              <SelectGroup key={group.family}>
                <SelectLabel>{group.family}</SelectLabel>
                {group.models.map((m) => (
                  <SelectItem key={m.model} value={m.model}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
        <Select value={effort} onValueChange={onEffort}>
          <SelectTrigger
            id={`${idPrefix}-effort`}
            className="w-28 shrink-0"
            aria-label={`${label} effort`}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {efforts.map((e) => (
              <SelectItem key={e} value={e}>
                {e}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <p className="text-xs text-muted-foreground">{help}</p>
    </div>
  )
}

// One report row: live progress while running, headline scores once complete.
function ReportRow({ report, onOpen, onDelete, deleting }) {
  const status = report.status || "queued"
  const active = ACTIVE_STATUSES.has(status)
  const complete = status === "complete"
  const failed = status === "failed"
  const checks = String(report.checks || "")
    .split(",")
    .filter(Boolean)
  const clickable = complete && typeof onOpen === "function"

  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 rounded-xl border px-3 py-2.5",
        clickable && "cursor-pointer transition-colors hover:bg-muted/50"
      )}
      {...(clickable
        ? {
            role: "button",
            tabIndex: 0,
            onClick: () => onOpen(report.report_id),
            onKeyDown: (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault()
                onOpen(report.report_id)
              }
            },
          }
        : {})}
    >
      <div className="flex items-center gap-2">
        {active ? (
          <Spinner className="size-3.5 shrink-0" />
        ) : complete ? (
          <CheckCircle2Icon className="size-3.5 shrink-0 text-emerald-600 dark:text-emerald-500" />
        ) : (
          <XCircleIcon className="size-3.5 shrink-0 text-destructive" />
        )}
        <span className="text-sm font-medium tabular-nums">
          {fmtWhen(report.created_at)}
        </span>
        {checks.map((c) => (
          <Badge key={c} variant="secondary" className="shrink-0">
            {CHECK_META.find((m) => m.key === c)?.label || c}
          </Badge>
        ))}
        {report.version_id ? (
          <Badge variant="outline" className="shrink-0">
            pinned
          </Badge>
        ) : null}
        {report.behavior_live_sql ? (
          <Badge variant="outline" className="shrink-0">
            live SQL
          </Badge>
        ) : null}
        <span className="min-w-0 flex-1" />
        {/* No delete affordance while the run is still working — a disabled
            trash next to a live run read as "you could stop this". */}
        {active ? null : (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Delete report"
            disabled={deleting}
            onClick={(e) => {
              e.stopPropagation()
              onDelete(report.report_id)
            }}
          >
            {deleting ? <Spinner className="size-3.5" /> : <Trash2Icon />}
          </Button>
        )}
        {clickable ? (
          <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground" />
        ) : null}
      </div>

      {active ? (
        <ActiveProgress report={report} />
      ) : failed ? (
        <p className="pl-5.5 text-xs break-words text-destructive">
          {report.detail || "the run failed"}
        </p>
      ) : (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-5.5 text-xs text-muted-foreground tabular-nums">
          {report.runs ? <span>Runs: {report.runs}</span> : null}
          {checks.map((c) => (
            <span key={c}>
              {CHECK_META.find((m) => m.key === c)?.label || c}{" "}
              <span className="font-medium text-foreground">
                {fmtScore(report[`${c}_raw`])}
              </span>
              {typeof report[`${c}_adjusted`] === "number" ? (
                <span> · Judge adjudication {fmtScore(report[`${c}_adjusted`])}</span>
              ) : null}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// The live phase line + progress bar for a queued/running row, from the index
// row's progress stamps (phase / progress_check / progress_run / current/total).
function ActiveProgress({ report }) {
  // Backend progress stamps are lowercase verbs ("solving", "judging") —
  // capitalize for display so the line reads as a proper label.
  const rawPhase = report.phase || "starting"
  const phase = rawPhase.charAt(0).toUpperCase() + rawPhase.slice(1)
  const checkLabel =
    CHECK_META.find((m) => m.key === report.progress_check)?.label ||
    report.progress_check ||
    ""
  const current = report.progress_current || 0
  const total = report.progress_total || 0
  const pct = total > 0 ? Math.round((current / total) * 100) : 0
  const runPart =
    report.progress_run && report.total_runs
      ? `${report.progress_run}/${report.total_runs}`
      : ""

  return (
    <div className="flex flex-col gap-1 pl-5.5">
      <span className="text-xs text-muted-foreground tabular-nums">
        {report.status === "queued" && !report.phase
          ? "Queued…"
          : [runPart, checkLabel, phase].filter(Boolean).join(" · ") +
            (total > 0 ? ` ${current}/${total}` : "")}
      </span>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

// Inline feedback on the uploaded question set: nothing yet, per-check counts,
// or a format error.
function QuestionSetStatus({ questions }) {
  if (!questions) return null

  if (questions.inspectError) {
    return (
      <p className="text-sm text-muted-foreground">
        Could not read the question set: {questions.inspectError}
      </p>
    )
  }

  if (!questions.uploaded) {
    return (
      <p className="text-sm text-muted-foreground">
        No question set uploaded yet.
      </p>
    )
  }

  if (!questions.valid) {
    return (
      <Alert variant="destructive">
        <AlertTriangleIcon />
        <AlertTitle>Invalid question set</AlertTitle>
        <AlertDescription>
          {questions.error || "The CSV could not be parsed."}
        </AlertDescription>
      </Alert>
    )
  }

  const counts = questions.check_counts || {}
  return (
    <div className="flex items-start gap-2 text-sm">
      <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-500" />
      <span className="text-muted-foreground">
        <span className="font-medium text-foreground">
          {questions.count} question{questions.count === 1 ? "" : "s"}
        </span>{" "}
        —{" "}
        {CHECK_META.map((c) => `${c.label}: ${counts[c.key] || 0}`).join(", ")}
        .
        {questions.capped
          ? ` Capped from ${questions.total_in_csv} rows at the ${questions.max_questions}-question limit (first ${questions.max_questions} used).`
          : ""}
      </span>
    </div>
  )
}
