// Synthetic question-bank generation — the "Generate questions" surface on the
// Benchmark Studio page.
//
// A generation is a standalone run on the harvest runtime (no lease, nothing
// written to the wiki) whose author agents see ONLY the dataset's ground truth
// (.metadata/ + .context/) plus live SQL — never the authored wiki, so the
// questions test whether the wiki captured the source truth instead of
// parroting it. This section owns: the config modal (count, check mix + ratio,
// dimensions, model), the generation rows with live progress (polling the
// QBANK# index rows like the reports list), and the completed-bank browser
// with Download CSV / Apply-to-Studio.

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"
import {
  CheckCircle2Icon,
  ChevronRightIcon,
  CircleStopIcon,
  DownloadIcon,
  Trash2Icon,
  WandSparklesIcon,
  XCircleIcon,
} from "lucide-react"

import {
  GROUPED_MODEL_CATALOG,
  MODEL_CATALOG,
  defaultEffortFor,
  effortsFor,
} from "@/lib/harvestModels"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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
import { Slider } from "@/components/ui/slider"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"

// UI mirror of the dimension taxonomy — the SOURCE OF TRUTH is
// okf_core/qbank.py (keys, titles, per-check affinities); keep in sync.
export const DIMENSION_META = [
  { key: "direct_retrieval", title: "Direct Retrieval", checks: ["sql"] },
  { key: "aggregation", title: "Aggregation & Decomposition", checks: ["sql"] },
  {
    key: "nl_disambiguation",
    title: "NL Resolution & Disambiguation",
    checks: ["sql", "behavior"],
  },
  { key: "comparison", title: "Comparison", checks: ["sql"] },
  { key: "derived_kpi", title: "Derived KPI Computation", checks: ["sql"] },
  { key: "multi_step", title: "Conditional & Multi-Step", checks: ["sql"] },
  {
    key: "anomaly_detection",
    title: "Anomaly & Pattern Detection",
    checks: ["sql", "behavior"],
  },
  {
    key: "counterfactual",
    title: "Counterfactual & Projection",
    checks: ["behavior"],
  },
  {
    key: "meta_introspection",
    title: "Meta / Introspection",
    checks: ["behavior"],
  },
  {
    key: "join_trap",
    title: "Grain & Join-Trap Safety",
    checks: ["sql", "behavior"],
  },
  {
    key: "null_semantics",
    title: "Null & Sentinel Semantics",
    checks: ["sql", "behavior"],
  },
  {
    key: "unanswerable",
    title: "Unanswerable / Honesty",
    checks: ["behavior"],
  },
]

const TIER_ORDER = ["easy", "medium", "hard"]
const LIST_POLL_MS = 4000
const ACTIVE = new Set(["queued", "running"])
// Mirrors the reports list's stale escape (an AgentCore session caps at 8h).
const STALE_AFTER_MS = 8 * 60 * 60 * 1000

function rowIsStale(row) {
  const ts = row.updated_at || row.started_at || row.created_at
  if (!ts) return true
  const t = Date.parse(ts)
  return Number.isFinite(t) ? Date.now() - t > STALE_AFTER_MS : true
}

function fmtWhen(iso) {
  const t = Date.parse(iso || "")
  if (!Number.isFinite(t)) return "—"
  return new Date(t).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

export default function QuestionBankSection({
  api,
  domain,
  dataset,
  onApplied,
}) {
  const [qbanks, setQbanks] = useState(null)
  const [genOpen, setGenOpen] = useState(false)
  const [browseId, setBrowseId] = useState(null)
  const [deleting, setDeleting] = useState(() => new Set())
  const epochRef = useRef(0)

  const load = useCallback(async () => {
    if (!api || !domain || !dataset) return
    const epoch = epochRef.current
    try {
      const res = await api.listQbanks(domain, dataset)
      if (epoch === epochRef.current)
        setQbanks(Array.isArray(res?.qbanks) ? res.qbanks : [])
    } catch {
      if (epoch === epochRef.current) setQbanks([])
    }
  }, [api, domain, dataset])

  useEffect(() => {
    epochRef.current += 1
    setQbanks(null)
    setGenOpen(false)
    setBrowseId(null)
    load()
    // Keyed on the SCOPE, not on `load`: `load`'s identity changes whenever
    // `api` does (App rebuilds it on every silent OIDC token renewal), and
    // resetting on that closed the generate/browse dialogs mid-edit roughly
    // hourly. Only a real domain/dataset switch should wipe this state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain, dataset])

  const anyActive = useMemo(
    () => (qbanks || []).some((r) => ACTIVE.has(r.status)),
    [qbanks]
  )
  useEffect(() => {
    if (!anyActive) return
    const id = setInterval(load, LIST_POLL_MS)
    return () => clearInterval(id)
  }, [anyActive, load])

  const deleteBank = async (qbankId) => {
    setDeleting((prev) => new Set(prev).add(qbankId))
    try {
      await api.deleteQbank(domain, dataset, qbankId)
      toast.success("Generated bank deleted.")
      await load()
    } catch (e) {
      toast.error(`Could not delete: ${e.message || e}`)
    } finally {
      setDeleting((prev) => {
        const next = new Set(prev)
        next.delete(qbankId)
        return next
      })
    }
  }

  const cancelBank = async (qbankId) => {
    setDeleting((prev) => new Set(prev).add(qbankId))
    try {
      const out = await api.cancelQbank(domain, dataset, qbankId)
      if (out?.cancelled) {
        toast.success("Generation cancelled — the partial bank was discarded.")
      } else {
        // It finished (or failed) between the click and the stop — the real
        // terminal state is already on the row; the reload shows it.
        toast.info(out?.detail || "The generation had already finished.")
      }
      await load()
    } catch (e) {
      toast.error(`Could not cancel: ${e.message || e}`)
    } finally {
      setDeleting((prev) => {
        const next = new Set(prev)
        next.delete(qbankId)
        return next
      })
    }
  }

  return (
    <div className="flex shrink-0 flex-col gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <Label>Generated question banks</Label>
        <Button variant="outline" size="sm" onClick={() => setGenOpen(true)}>
          <WandSparklesIcon data-icon="inline-start" />
          Generate questions
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        An agent authors a question set from the dataset&apos;s{" "}
        <span className="font-medium text-foreground">ground truth only</span>{" "}
        (catalog snapshot + uploaded context, never the wiki) and validates
        every gold SQL live. Review the result, then download it or apply it as
        this dataset&apos;s question set.
      </p>
      {qbanks === null ? null : qbanks.length === 0 ? null : (
        <div className="flex flex-col gap-2">
          {qbanks.map((row) => (
            <QbankRow
              key={row.qbank_id}
              row={row}
              onOpen={(id) => setBrowseId(id)}
              onDelete={(r) => deleteBank(r.qbank_id)}
              onCancel={(r) => cancelBank(r.qbank_id)}
              deleting={deleting.has(row.qbank_id)}
            />
          ))}
        </div>
      )}
      <GenerateDialog
        open={genOpen}
        onOpenChange={setGenOpen}
        api={api}
        domain={domain}
        dataset={dataset}
        onStarted={load}
      />
      {browseId ? (
        <BankBrowserDialog
          open={Boolean(browseId)}
          onOpenChange={(open) => !open && setBrowseId(null)}
          api={api}
          domain={domain}
          dataset={dataset}
          qbankId={browseId}
          onApplied={onApplied}
        />
      ) : null}
    </div>
  )
}

function QbankRow({ row, onOpen, onDelete, onCancel, deleting }) {
  const status = row.status || "queued"
  const active = ACTIVE.has(status)
  const staleActive = active && rowIsStale(row)
  const complete = status === "complete"
  const cancelled = status === "cancelled"
  const clickable = complete && typeof onOpen === "function"
  const current = row.progress_current || 0
  const total = row.progress_total || row.requested_count || 0
  const pct = total > 0 ? Math.round((current / total) * 100) : 0
  const phase = (row.phase || "starting").replace(/^./, (c) => c.toUpperCase())

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
            onClick: () => onOpen(row.qbank_id),
            onKeyDown: (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault()
                onOpen(row.qbank_id)
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
          {fmtWhen(row.created_at)}
        </span>
        <Badge variant="secondary" className="shrink-0">
          {complete
            ? `${row.question_count ?? "?"}/${row.requested_count ?? "?"} questions`
            : `${row.requested_count ?? "?"} requested`}
        </Badge>
        {complete && Number(row.dropped_count) > 0 ? (
          <Badge variant="outline" className="shrink-0">
            {row.dropped_count} dropped
          </Badge>
        ) : null}
        <span className="min-w-0 flex-1" />
        {/* A live run offers CANCEL (stop the microVM, discard the partial
            bank); everything else — terminal rows and dead-heartbeat
            "active" rows — offers delete. */}
        {active && !staleActive ? (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Cancel generation"
            title="Cancel — the partial bank is discarded"
            disabled={deleting}
            onClick={(e) => {
              e.stopPropagation()
              onCancel(row)
            }}
          >
            {deleting ? (
              <Spinner className="size-3.5" />
            ) : (
              // Lucide strokes everything; filling the inner rect (the stop
              // "square") makes it read as a media-stop control at 16px
              // instead of a circled outline.
              <CircleStopIcon className="text-destructive [&_rect]:fill-current" />
            )}
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Delete generated bank"
            disabled={deleting}
            onClick={(e) => {
              e.stopPropagation()
              onDelete(row)
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
        <div className="flex flex-col gap-1 pl-5.5">
          <span className="text-xs text-muted-foreground tabular-nums">
            {status === "queued" && !row.phase
              ? "Queued…"
              : `${phase}${total > 0 ? ` ${current}/${total}` : ""}`}
          </span>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary transition-[width] duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      ) : status === "failed" ? (
        <p className="pl-5.5 text-xs break-words text-destructive">
          {row.detail || "the generation failed"}
        </p>
      ) : cancelled ? (
        <p className="pl-5.5 text-xs text-muted-foreground">
          {row.detail || "cancelled"} — the partial bank was discarded
        </p>
      ) : null}
    </div>
  )
}

function GenerateDialog({
  open,
  onOpenChange,
  api,
  domain,
  dataset,
  onStarted,
}) {
  const defaultModel = MODEL_CATALOG[0]?.model || ""
  const [count, setCount] = useState(40)
  const [checks, setChecks] = useState(() => new Set(["sql", "behavior"]))
  const [sqlShare, setSqlShare] = useState(70) // percent, UI-side
  const [dims, setDims] = useState(
    () => new Set(DIMENSION_META.map((d) => d.key))
  )
  const [model, setModel] = useState(defaultModel)
  const [effort, setEffort] = useState(defaultEffortFor(defaultModel))
  const [starting, setStarting] = useState(false)

  const both = checks.has("sql") && checks.has("behavior")
  // A dimension is usable iff it can author for at least one ENABLED check —
  // unusable ones gray out and are excluded from the request (the server
  // would 400 a config whose slots nobody can fill).
  const usable = (d) => d.checks.some((c) => checks.has(c))
  const selectedDims = DIMENSION_META.filter(
    (d) => dims.has(d.key) && usable(d)
  ).map((d) => d.key)
  const canStart = checks.size > 0 && selectedDims.length > 0 && !starting

  const toggleCheck = (key) =>
    setChecks((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next.size ? next : prev // at least one check stays on
    })
  const toggleDim = (key) =>
    setDims((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const pickModel = (m) => {
    setModel(m)
    if (!effortsFor(m).includes(effort)) setEffort(defaultEffortFor(m))
  }

  const start = async () => {
    setStarting(true)
    try {
      await api.startQbankGeneration(domain, dataset, {
        count,
        checks: [...checks],
        sql_share: both ? sqlShare / 100 : undefined,
        dimensions: selectedDims,
        model: model || undefined,
        effort: effort || undefined,
      })
      toast.success(
        `Generating ${count} questions — watch the list for progress.`
      )
      onOpenChange(false)
      await onStarted()
    } catch (e) {
      toast.error(`Could not start: ${e.message || e}`)
    } finally {
      setStarting(false)
    }
  }

  // Python's round() (banker's: .5 ties go to the EVEN neighbor) — the
  // allocator's arithmetic (okf_core.qbank.split_checks), mirrored so the
  // preview never promises a split the run won't deliver (Math.round(22.5)
  // is 23; the backend allocates 22).
  const roundHalfEven = (x) => {
    const floor = Math.floor(x)
    const diff = x - floor
    if (diff > 0.5) return floor + 1
    if (diff < 0.5) return floor
    return floor % 2 === 0 ? floor : floor + 1
  }
  const sqlCount = both
    ? Math.max(1, Math.min(count - 1, roundHalfEven((count * sqlShare) / 100)))
    : 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <WandSparklesIcon className="size-4" />
            Generate a question bank
          </DialogTitle>
          <DialogDescription>
            {domain}/{dataset} — authored from the catalog snapshot and uploaded
            context.
          </DialogDescription>
        </DialogHeader>

        <div className="okf-thin-scroll flex max-h-[65vh] min-w-0 flex-col gap-5 overflow-y-auto pr-1">
          <div className="flex flex-col gap-2">
            <div className="flex items-baseline justify-between">
              <Label htmlFor="qbank-count">Questions</Label>
              <span className="text-sm font-medium tabular-nums">{count}</span>
            </div>
            <Slider
              id="qbank-count"
              min={20}
              max={100}
              step={1}
              value={[count]}
              onValueChange={([v]) => setCount(v)}
            />
            <p className="text-xs text-muted-foreground">
              20–100 (the studio grades at most 100). Complexity mixes
              automatically: ~30% easy, 40% medium, 30% hard.
            </p>
          </div>

          <div className="flex flex-col gap-2">
            <Label>Checks</Label>
            <div className="grid gap-2 sm:grid-cols-2">
              {[
                {
                  key: "sql",
                  label: "Accuracy (SQL EX)",
                  help: "Gold SQL, graded by result-set equality",
                },
                {
                  key: "behavior",
                  label: "Behavior",
                  help: "Expected behavior, graded by the judge",
                },
              ].map((c) => (
                <label
                  key={c.key}
                  className={cn(
                    "flex cursor-pointer items-start gap-2.5 rounded-xl border px-3 py-2 transition-colors hover:bg-muted/50",
                    checks.has(c.key) && "border-primary/50 bg-muted/40"
                  )}
                >
                  <input
                    type="checkbox"
                    className="mt-0.5 size-3.5 shrink-0 accent-primary"
                    checked={checks.has(c.key)}
                    onChange={() => toggleCheck(c.key)}
                  />
                  <span className="flex min-w-0 flex-col gap-0.5">
                    <span className="text-sm font-medium">{c.label}</span>
                    <span className="text-xs text-muted-foreground">
                      {c.help}
                    </span>
                  </span>
                </label>
              ))}
            </div>
            {both ? (
              <div className="flex flex-col gap-2 rounded-xl border px-3 py-2.5">
                <div className="flex items-baseline justify-between">
                  <Label htmlFor="qbank-ratio" className="text-xs">
                    Mix
                  </Label>
                  <span className="text-xs text-muted-foreground tabular-nums">
                    {sqlCount} accuracy · {count - sqlCount} behavior
                  </span>
                </div>
                <Slider
                  id="qbank-ratio"
                  min={10}
                  max={90}
                  step={5}
                  value={[sqlShare]}
                  onValueChange={([v]) => setSqlShare(v)}
                />
              </div>
            ) : null}
          </div>

          <div className="flex flex-col gap-2">
            <Label>Dimensions</Label>
            <p className="text-xs text-muted-foreground">
              What the questions probe. Dimensions that can&apos;t author for
              the enabled checks are grayed out; the count spreads across the
              selected ones.
            </p>
            <div className="grid gap-1.5 sm:grid-cols-2">
              {DIMENSION_META.map((d) => {
                const enabled = usable(d)
                return (
                  <label
                    key={d.key}
                    className={cn(
                      "flex cursor-pointer items-center gap-2 rounded-lg border px-2.5 py-1.5 text-sm transition-colors hover:bg-muted/50",
                      dims.has(d.key) &&
                        enabled &&
                        "border-primary/50 bg-muted/40",
                      !enabled && "cursor-not-allowed opacity-50"
                    )}
                  >
                    <input
                      type="checkbox"
                      className="size-3.5 shrink-0 accent-primary"
                      checked={dims.has(d.key) && enabled}
                      disabled={!enabled}
                      onChange={() => toggleDim(d.key)}
                    />
                    <span className="min-w-0 truncate">{d.title}</span>
                  </label>
                )
              })}
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <Label>Author model</Label>
            <div className="flex gap-2">
              <Select value={model} onValueChange={pickModel}>
                <SelectTrigger className="min-w-0 flex-1">
                  <SelectValue placeholder="model" />
                </SelectTrigger>
                <SelectContent>
                  {GROUPED_MODEL_CATALOG.map((group) => (
                    <SelectGroup key={group.label}>
                      <SelectLabel>{group.label}</SelectLabel>
                      {group.models.map((m) => (
                        <SelectItem key={m.model} value={m.model}>
                          {m.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  ))}
                </SelectContent>
              </Select>
              <Select value={effort} onValueChange={setEffort}>
                <SelectTrigger className="w-28">
                  <SelectValue placeholder="effort" />
                </SelectTrigger>
                <SelectContent>
                  {effortsFor(model).map((e) => (
                    <SelectItem key={e} value={e}>
                      {e}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={start} disabled={!canStart}>
            {starting ? <Spinner data-icon="inline-start" /> : null}
            Generate
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function BankBrowserDialog({
  open,
  onOpenChange,
  api,
  domain,
  dataset,
  qbankId,
  onApplied,
}) {
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)
  const [dimFilter, setDimFilter] = useState("all")
  const [tierFilter, setTierFilter] = useState("all")
  const [checkFilter, setCheckFilter] = useState("all")
  const [applying, setApplying] = useState(false)
  const [confirmApply, setConfirmApply] = useState(false)

  useEffect(() => {
    if (!open) return
    let alive = true
    setRes(null)
    setError(null)
    api
      .getQbank(domain, dataset, qbankId)
      .then(async (r) => {
        // A bank past the inline cap ships as a short-lived presigned URL
        // (same degrade as report_url) — follow it transparently so the
        // question list renders either way; `csv` stays inline regardless.
        if (r && !r.bank && r.bank_url) {
          const resp = await fetch(r.bank_url)
          if (!resp.ok) throw new Error(`bank fetch failed (${resp.status})`)
          r = { ...r, bank: await resp.json() }
        }
        if (alive) setRes(r)
      })
      .catch((e) => alive && setError(e.message || String(e)))
    return () => {
      alive = false
    }
  }, [open, api, domain, dataset, qbankId])

  const questions = res?.bank?.questions || []
  const dropped = res?.bank?.dropped || []
  const filtered = questions.filter(
    (q) =>
      (dimFilter === "all" || q.dimension === dimFilter) &&
      (tierFilter === "all" || q.tier === tierFilter) &&
      (checkFilter === "all" || q.check === checkFilter)
  )
  const usedDims = [...new Set(questions.map((q) => q.dimension))]

  const download = () => {
    const blob = new Blob([res?.csv || ""], { type: "text/csv;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `${dataset}-questions-${qbankId}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const apply = async () => {
    setApplying(true)
    try {
      const out = await api.applyQbank(domain, dataset, qbankId)
      toast.success(
        `Applied as the question set — sql: ${out.check_counts?.sql ?? 0}, ` +
          `behavior: ${out.check_counts?.behavior ?? 0}.`
      )
      setConfirmApply(false)
      onOpenChange(false)
      await onApplied?.()
    } catch (e) {
      toast.error(`Could not apply: ${e.message || e}`)
    } finally {
      setApplying(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Generated question bank</DialogTitle>
          <DialogDescription>
            {domain}/{dataset}
            {res?.bank
              ? ` — ${questions.length} question${questions.length === 1 ? "" : "s"}` +
                (dropped.length
                  ? ` · ${dropped.length} slot${dropped.length === 1 ? "" : "s"} dropped`
                  : "")
              : null}
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : !res ? (
          <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
            <Spinner /> Loading the bank…
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Select value={checkFilter} onValueChange={setCheckFilter}>
                <SelectTrigger size="sm" className="w-36">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All checks</SelectItem>
                  <SelectItem value="sql">Accuracy</SelectItem>
                  <SelectItem value="behavior">Behavior</SelectItem>
                </SelectContent>
              </Select>
              <Select value={tierFilter} onValueChange={setTierFilter}>
                <SelectTrigger size="sm" className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All tiers</SelectItem>
                  {TIER_ORDER.map((t) => (
                    <SelectItem key={t} value={t}>
                      {t}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={dimFilter} onValueChange={setDimFilter}>
                <SelectTrigger size="sm" className="w-56">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All dimensions</SelectItem>
                  {usedDims.map((k) => (
                    <SelectItem key={k} value={k}>
                      {DIMENSION_META.find((d) => d.key === k)?.title || k}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span className="ml-auto text-xs text-muted-foreground tabular-nums">
                {filtered.length}/{questions.length}
              </span>
            </div>

            <div className="okf-thin-scroll flex max-h-[45vh] flex-col gap-1.5 overflow-y-auto pr-1">
              {filtered.map((q, i) => (
                <QuestionItem key={`${q.question}-${i}`} q={q} />
              ))}
              {dropped.length > 0 &&
              dimFilter === "all" &&
              tierFilter === "all" &&
              checkFilter === "all" ? (
                <div className="mt-2 flex flex-col gap-1 rounded-xl border border-dashed px-3 py-2">
                  <span className="text-xs font-medium text-muted-foreground">
                    Dropped slots (requested but not delivered)
                  </span>
                  {dropped.map((d, i) => (
                    <p key={i} className="text-xs text-muted-foreground">
                      <span className="font-medium">
                        [{d.check}] [{d.tier}]{" "}
                        {DIMENSION_META.find((m) => m.key === d.dimension)
                          ?.title || d.dimension}
                      </span>{" "}
                      — {d.reason}
                    </p>
                  ))}
                </div>
              ) : null}
            </div>

            <DialogFooter className="items-center gap-2">
              {confirmApply ? (
                <>
                  <span className="mr-auto text-xs text-muted-foreground">
                    Replaces the current question set (the old version stays
                    recoverable; in-flight runs are unaffected).
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setConfirmApply(false)}
                  >
                    Cancel
                  </Button>
                  <Button size="sm" onClick={apply} disabled={applying}>
                    {applying ? <Spinner data-icon="inline-start" /> : null}
                    Confirm apply
                  </Button>
                </>
              ) : (
                <>
                  <Button variant="outline" onClick={download}>
                    <DownloadIcon data-icon="inline-start" />
                    Download CSV
                  </Button>
                  <Button
                    onClick={() => setConfirmApply(true)}
                    disabled={!questions.length}
                  >
                    Apply to Studio
                  </Button>
                </>
              )}
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

function QuestionItem({ q }) {
  const [openDetail, setOpenDetail] = useState(false)
  const rowCount = q.validation?.row_count
  return (
    <div
      className="flex cursor-pointer flex-col gap-1 rounded-xl border px-3 py-2 transition-colors hover:bg-muted/50"
      role="button"
      tabIndex={0}
      onClick={() => setOpenDetail((v) => !v)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault()
          setOpenDetail((v) => !v)
        }
      }}
    >
      <div className="flex items-start gap-2">
        <span className="min-w-0 flex-1 text-sm">{q.question}</span>
        <Badge variant="secondary" className="shrink-0">
          {q.check === "sql" ? "Accuracy" : "Behavior"}
        </Badge>
        <Badge variant="outline" className="shrink-0">
          {q.tier}
        </Badge>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
        <span>
          {DIMENSION_META.find((d) => d.key === q.dimension)?.title ||
            q.dimension}
        </span>
        {typeof rowCount === "number" ? (
          <span className="tabular-nums">gold executed · {rowCount} rows</span>
        ) : null}
      </div>
      {openDetail ? (
        q.check === "sql" ? (
          <pre className="okf-thin-scroll mt-1 overflow-x-auto rounded-md border bg-foreground/[0.03] p-2 font-mono text-xs whitespace-pre-wrap">
            {q.gold_sql}
          </pre>
        ) : (
          <p className="mt-1 rounded-md border bg-foreground/[0.03] p-2 text-xs">
            {q.expected_behavior}
          </p>
        )
      ) : null}
    </div>
  )
}
