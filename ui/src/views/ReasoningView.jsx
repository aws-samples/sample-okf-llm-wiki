// Guardrails — per-dataset policy-check status + the authored guardrails.
//
// Guardrail checks are ALWAYS ON per dataset (no enrollment): once a wiki
// exists (the guardrails are derived FROM the bundle), the document authors
// automatically on every wiki change — a harvest, an increment, a restore.
// A dataset that predates the feature is deliberately not backfilled in
// bulk; its first document comes from the Generate button here or its next
// wiki change. The rest of the page is transparency, in ONE card: build
// state and freshness (with manual Sync as the fail-safe), the source files
// behind a modal, and each guardrail as its own sub-card (id, track badge,
// Condition/Action, source page). Authoring runs async on the backend
// (minutes); the page polls while one is live.
//
// (File and internal ids keep the historical "reasoning"/"policy" names —
// only the user-visible copy says Guardrails.)

import { useCallback, useEffect, useState } from "react"
import {
  ChevronRightIcon,
  FileTextIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
} from "lucide-react"

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
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"

// A build is minutes-scale; poll gently while one is in flight so the page
// flips to ready/degraded without a manual refresh.
const BUILD_POLL_MS = 8000

// Row-status chips, muted throughout (house style: a state is information,
// not an alarm). `stale`/mismatch renders via the separate freshness line.
const STATUS_CHIP = {
  ready: {
    text: "Ready",
    variant: "outline",
    cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  },
  building: { text: "Authoring…", variant: "secondary", cls: "" },
  stale: { text: "Re-author pending", variant: "secondary", cls: "" },
  failed: { text: "Authoring failed", variant: "destructive", cls: "" },
}

function fmtWhen(iso) {
  if (!iso) return ""
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

// The check track (v3 type split): computational runs against SQL queries,
// behavioural against the agent's steps. Distinct tints so the two tracks
// read apart at a glance (sky vs violet — both survive light and dark).
const TYPE_BADGE = {
  computational:
    "border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-400",
  behavioural:
    "border-violet-500/40 bg-violet-500/10 text-violet-700 dark:text-violet-400",
}

// "forbidden_aggregation" -> "Forbidden Aggregation" (UI copy is Title Case;
// the raw key stays snake_case everywhere machine-facing).
function ruleTitle(dimension) {
  return String(dimension || "")
    .split("_")
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ")
}

// Every load-bearing binding, mirroring okf_core.policy_rules.rule_label —
// the accordion is this feature's human-review surface, so a reviewer must
// see WHAT the rule checks (which filter, which grouping, which functions),
// not just which table it touches.
function ruleBindings(rule) {
  const parts = [...(rule.targets || [])]
  const table = rule.table
  if (table) {
    if (rule.group_by) {
      parts.push(
        `${table} group by ${rule.group_by} → count distinct ${rule.count_distinct}`
      )
    }
    if (rule.require) {
      const value = rule.require.value != null ? ` ${rule.require.value}` : ""
      parts.push(
        `${table} requires ${rule.require.column} ${rule.require.op}${value}`
      )
    }
    if (rule.or_group_by) parts.push(`or group by ${table}.${rule.or_group_by}`)
    if (rule.when_filtered) {
      const value =
        rule.when_filtered.value != null ? ` ${rule.when_filtered.value}` : ""
      parts.push(
        `when ${rule.when_filtered.column} ${rule.when_filtered.op}${value}`
      )
    }
    if (!parts.length) parts.push(table)
  }
  if (rule.dimension === "forbidden_aggregation" && rule.aggs?.length)
    parts.push(`aggs: ${rule.aggs.join(", ")}`)
  if (rule.dimension === "forbidden_usage" && rule.contexts?.length < 4)
    parts.push(`in: ${rule.contexts.join(", ")}`)
  if (rule.functions?.length) parts.push(`functions: ${rule.functions.join(", ")}`)
  if (rule.dimension === "forbidden_function" && rule.cast_types?.length)
    parts.push(`cast to: ${rule.cast_types.join(", ")}`)
  if (rule.dimension === "required_guard" && rule.guard_functions?.length)
    parts.push(`guard: ${rule.guard_functions.join("/")}`)
  return parts.join(" · ")
}

// One rule, rendered from its dimension + bindings. Deliberately terse: the
// dimension names the shape, the bindings name the columns and the exact
// check, and the examples show what it flags and what it lets through.
function RuleRow({ rule }) {
  const bindings = ruleBindings(rule)
  return (
    <li className="space-y-1 rounded border border-border/50 bg-background/60 px-2 py-1.5">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-[11px] font-medium text-foreground">
          {ruleTitle(rule.dimension)}
        </span>
        {bindings ? (
          <span className="font-mono text-[11px] text-muted-foreground">
            {bindings}
          </span>
        ) : null}
      </div>
      {rule.examples?.violation ? (
        <p className="font-mono text-[10px] leading-relaxed text-muted-foreground">
          <span className="text-rose-600 dark:text-rose-400">Flags:</span>{" "}
          {rule.examples.violation}
        </p>
      ) : null}
      {rule.examples?.pass ? (
        <p className="font-mono text-[10px] leading-relaxed text-muted-foreground">
          <span className="text-emerald-600 dark:text-emerald-400">
            Allows:
          </span>{" "}
          {rule.examples.pass}
        </p>
      ) : null}
    </li>
  )
}

// One authored guardrail as its own sub-card: id + track badge + source on
// the top line, then the two authored fields verbatim (sky labels, full
// foreground text — the guardrail text is the payload, not an aside). A
// guardrail carrying deterministic rules shows them behind a collapsed
// accordion — the count is the summary; the rows are review material.
function GuardrailItem({ policy }) {
  const rules = policy.rules || []
  const [rulesOpen, setRulesOpen] = useState(false)
  return (
    <li className="space-y-1 rounded-md border border-border/60 bg-muted/30 px-3 py-2.5 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] text-muted-foreground">
          {policy.id}
        </span>
        {policy.type ? (
          <Badge
            variant="outline"
            className={`px-1.5 py-0 text-[10px] font-normal capitalize ${
              TYPE_BADGE[policy.type] || "text-muted-foreground"
            }`}
          >
            {policy.type}
          </Badge>
        ) : null}
        {policy.source ? (
          <span className="ml-auto truncate font-mono text-[11px] text-muted-foreground/70">
            {policy.source}
          </span>
        ) : null}
      </div>
      <p className="text-foreground">
        <span className="font-medium text-sky-600 dark:text-sky-400">
          Condition:
        </span>{" "}
        {policy.condition}
      </p>
      <p className="text-foreground">
        <span className="font-medium text-sky-600 dark:text-sky-400">
          Action:
        </span>{" "}
        {policy.action}
      </p>
      {rules.length ? (
        <div className="space-y-1.5 pt-1">
          <button
            type="button"
            className="flex items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
            onClick={() => setRulesOpen((open) => !open)}
            aria-expanded={rulesOpen}
          >
            <ChevronRightIcon
              className={`h-3 w-3 transition-transform ${
                rulesOpen ? "rotate-90" : ""
              }`}
            />
            {rules.length === 1 ? "1 Rule" : `${rules.length} Rules`}
          </button>
          {rulesOpen ? (
            <ul className="space-y-1">
              {rules.map((rule, i) => (
                <RuleRow key={i} rule={rule} />
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </li>
  )
}

export default function ReasoningView({ api, selection }) {
  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const hasSelection = Boolean(domain && dataset)

  const [data, setData] = useState(null) // null until first load
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false) // a sync in flight
  const [sourcesOpen, setSourcesOpen] = useState(false)
  // The list's track filter: "all" | "behavioural" | "computational".
  const [typeFilter, setTypeFilter] = useState("all")
  // A queued sync reaches the pipeline asynchronously: the row still reads
  // failed/ready/"" for a few seconds until the build flips it to building.
  // Poll through that window so the page catches the transition (and a fast
  // snapshot restore, which can complete between two polls).
  const [syncPending, setSyncPending] = useState(false)
  const [confirmSyncOpen, setConfirmSyncOpen] = useState(false)

  const load = useCallback(async () => {
    if (!api || !hasSelection) return
    try {
      setData(await api.getReasoning(domain, dataset))
      setError(null)
    } catch (e) {
      setError(e.message || String(e))
    }
  }, [api, domain, dataset, hasSelection])

  // Reset + load on dataset change. The confirm dialog must close too — the
  // view is not remounted per selection, so an open dialog would survive a
  // sidebar switch and offer its destructive button against the NEW dataset.
  useEffect(() => {
    setData(null)
    setError(null)
    setSourcesOpen(false)
    setConfirmSyncOpen(false)
    setTypeFilter("all")
    load()
  }, [load])

  const building = data?.status === "building"
  const failed = data?.status === "failed"
  useEffect(() => {
    if (!building && !syncPending) return
    const id = setInterval(load, BUILD_POLL_MS)
    return () => clearInterval(id)
  }, [building, syncPending, load])
  // The queued window ends when the build poll takes over — a forced Sync
  // ALWAYS dispatches an authoring run, so `building` is the one expected
  // next state (a ready+current row no longer counts as settled: that was
  // the old rebuild-iff-changed semantics, and it closed the window the
  // instant a re-sync of a current document was clicked — the page then sat
  // frozen until a manual refresh). If the dispatch was lost, give up after
  // 90s and re-enable the manual buttons.
  useEffect(() => {
    if (!syncPending) return
    if (building) {
      setSyncPending(false)
      return
    }
    const id = setTimeout(() => setSyncPending(false), 90_000)
    return () => clearTimeout(id)
  }, [syncPending, building])

  const run = async (fn) => {
    setBusy(true)
    try {
      await fn()
      setError(null)
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setBusy(false)
      load()
    }
  }
  const sync = () =>
    run(async () => {
      await api.triggerReasoningSync(domain, dataset)
      setSyncPending(true)
    })
  // Syncing over an ALREADY-AUTHORED set re-derives the document: entries can
  // be rewritten, merged, or dropped (stable ids survive only where their
  // source material is unchanged). That's worth a confirmation; a dataset
  // with no guardrails yet syncs straight away — nothing to overwrite.
  const requestSync = () => {
    if ((data?.policies?.length || 0) > 0) setConfirmSyncOpen(true)
    else sync()
  }
  const confirmSync = () => {
    setConfirmSyncOpen(false)
    sync()
  }

  if (!hasSelection) return null
  if (!data && !error) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }

  const chip = STATUS_CHIP[data?.status]
  const shownPolicies = (data?.policies || []).filter(
    (p) => typeFilter === "all" || p.type === typeFilter
  )

  return (
    <>
      {/* Cap the card at the view column's height (natural height below it)
          and let ONLY the guardrail list scroll: header + build status stay
          put, so the min-h-0 shrink chain runs Card → CardContent → list —
          the Benchmark page's pattern. */}
      <Card className="max-h-full min-h-0">
        <CardHeader className="shrink-0 border-b">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-primary" />
                Guardrails
              </CardTitle>
              <CardDescription>
                Chat answers about{" "}
                <span className="font-medium text-foreground">
                  {domain}/{dataset}
                </span>{" "}
                are judged by a model fleet against guardrails derived from
                its wiki's guardrail-source pages (usage guardrails, enums,
                metrics, recipes, known issues — see Source files). They
                re-author automatically when THOSE pages change; other edits
                (table docs, titles, joins) don't affect them.
              </CardDescription>
            </div>
            {data?.wiki_ready && data?.sources?.length ? (
              <Button
                size="sm"
                variant="outline"
                onClick={() => setSourcesOpen(true)}
              >
                <FileTextIcon className="size-3.5" />
                Source files
              </Button>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="flex min-h-0 flex-col gap-4">
          {error ? (
            <p className="text-sm text-destructive">{error}</p>
          ) : null}
          {/* A harvest mid-write (`wiki_rewriting`) is NOT "no wiki yet" —
              keep the authored guardrails on screen and add a notice below
              instead of collapsing the page. */}
          {!data?.wiki_ready && !data?.wiki_rewriting ? (
            <p className="text-sm text-muted-foreground">
              {data?.reason || "No wiki yet — run a harvest first."}
            </p>
          ) : !data?.status && !syncPending ? (
            /* Never authored (a dataset predating the feature): there is no
               bulk backfill by design — the first document comes from this
               button, the next harvest/increment, or a restore. */
            <div className="space-y-2 text-sm">
              <p className="text-muted-foreground">
                No guardrails yet. They author automatically on the next wiki
                change (a harvest, an annotation, a restore) — or generate
                them now (takes a few minutes).
                {!data?.has_sources
                  ? " Note: this wiki has no guardrail-source files (usage guardrails, enums, metrics, recipes, known issues) yet, so a build would produce no rules."
                  : ""}
              </p>
              <Button disabled={busy} onClick={sync}>
                {busy ? (
                  <Spinner className="size-3.5" />
                ) : (
                  <ShieldCheckIcon className="size-3.5" />
                )}
                Generate guardrails
              </Button>
            </div>
          ) : (
            <div className="flex min-h-0 flex-col gap-2 text-sm">
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                {chip ? (
                  <Badge variant={chip.variant} className={chip.cls}>
                    {building ? <Spinner className="size-3" /> : null}
                    {chip.text}
                  </Badge>
                ) : (
                  <Badge variant="secondary">Queued</Badge>
                )}
                {data?.built_at ? (
                  <span className="text-muted-foreground">
                    Last sync {fmtWhen(data.built_at)}
                  </span>
                ) : null}
                {syncPending && !building ? (
                  <span className="flex items-center gap-1.5 text-muted-foreground">
                    <Spinner className="size-3" />
                    Sync queued…
                  </span>
                ) : null}
              </div>
              {/* Freshness is moot while the wiki is being rewritten (the
                  fingerprint would compare against a half-written tree), so
                  the up_to_date lines stay hidden and this explains why. */}
              {data?.wiki_rewriting ? (
                <p className="text-muted-foreground">
                  A harvest is rewriting this wiki — the guardrails below
                  re-author automatically when it commits.
                </p>
              ) : null}
              {/* Building: the page polls; authoring IS completion (v3 — no
                  build workflow, no completion authority). Sync is DISABLED
                  while a build is in flight — it only runs from a settled row
                  (ready or failed); the in-flight lease would refuse it
                  anyway, and offering a dead button reads as broken. A build
                  that dies is reaped automatically after about an hour (the
                  rebuild authority's stall reaper / nightly reconcile), which
                  flips the row to failed and re-enables the retry here. */}
              {building ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">
                    A build is running — this page updates automatically.
                  </span>
                  <Button size="sm" variant="ghost" disabled>
                    <RefreshCwIcon className="size-3.5" />
                    Sync
                  </Button>
                </div>
              ) : null}
              {/* The freshness line IS the fingerprint gate, for humans: a
                  guardrail set built from anything but the current wiki never
                  renders a verdict, so "out of date" means checks are paused
                  until the rebuild lands. Sync is the fail-safe when no
                  automatic trigger caught the change. A failed row owns its
                  own retry line below instead — checks are off there whatever
                  the hash says, so a freshness verdict would mislead. */}
              {data?.up_to_date === false && !building && !failed ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-amber-700 dark:text-amber-400">
                    Out of date — the guardrail-source files changed since
                    these guardrails were built. Checks are paused until they
                    rebuild.
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy || syncPending}
                    onClick={requestSync}
                  >
                    {busy ? (
                      <Spinner className="size-3.5" />
                    ) : (
                      <RefreshCwIcon className="size-3.5" />
                    )}
                    Sync now
                  </Button>
                </div>
              ) : null}
              {data?.up_to_date === true && !building && !failed ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">
                    Up to date — the guardrail-source files are
                    unchanged since the last authoring.
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy || syncPending}
                    onClick={requestSync}
                  >
                    {busy ? (
                      <Spinner className="size-3.5" />
                    ) : (
                      <RefreshCwIcon className="size-3.5" />
                    )}
                    Sync
                  </Button>
                </div>
              ) : null}
              {/* A failed build must never strand the dataset — the failure
                  may be a transient service error, so the retry lives here.
                  The rebuild authority retries a failed row even when the
                  wiki is unchanged (its unchanged-skip excludes `failed`). */}
              {failed && !building ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">
                    Last build failed
                    {data?.build_detail ? `: ${data.build_detail}` : ""}. Checks
                    are off until a build succeeds.
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy || syncPending}
                    onClick={requestSync}
                  >
                    <RefreshCwIcon className="size-3.5" />
                    Retry build
                  </Button>
                </div>
              ) : null}

              {/* The authored guardrails, in the same card — one sub-card per
                  entry, individually tracked by its stable id. The LIST is
                  the page's only scroll region (okf-thin-scroll: the app's
                  transparent-track scrollbar), so status stays visible. */}
              <div className="flex min-h-0 flex-col pt-2">
                <div className="flex shrink-0 items-center justify-between gap-2 pb-2">
                  <p className="text-sm font-medium text-muted-foreground">
                    Guardrails
                    {shownPolicies.length
                      ? ` (${shownPolicies.length}${
                          typeFilter === "all"
                            ? ""
                            : ` of ${data.policies.length}`
                        })`
                      : ""}
                  </p>
                  {data?.policies?.length ? (
                    <Select value={typeFilter} onValueChange={setTypeFilter}>
                      <SelectTrigger
                        size="sm"
                        className="text-xs"
                        aria-label="Filter guardrails by track"
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent align="end">
                        <SelectItem value="all">Show all</SelectItem>
                        <SelectItem value="behavioural">Behavioural</SelectItem>
                        <SelectItem value="computational">
                          Computational
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  ) : null}
                </div>
                {shownPolicies.length ? (
                  <ul className="okf-thin-scroll min-h-0 space-y-2 overflow-y-auto pr-1">
                    {shownPolicies.map((policy) => (
                      <GuardrailItem key={policy.id} policy={policy} />
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    {building
                      ? "The first authoring run is in flight — guardrails appear here when it finishes."
                      : data?.policies?.length
                        ? `No ${typeFilter} guardrails.`
                        : "No guardrails yet."}
                  </p>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* The source-file list, on demand instead of inline — it is context,
          not status, and can be dozens of paths. */}
      {/* Overwrite confirmation — only reachable when authored guardrails
          exist (requestSync syncs straight away otherwise). */}
      <Dialog open={confirmSyncOpen} onOpenChange={setConfirmSyncOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Overwrite authored guardrails?</DialogTitle>
            <DialogDescription>
              <span className="font-medium text-foreground">
                {domain}/{dataset}
              </span>{" "}
              already has {data?.policies?.length || 0} authored guardrail
              {(data?.policies?.length || 0) === 1 ? "" : "s"}. Sync re-authors
              the document from the current wiki: entries can be rewritten,
              merged, or dropped.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Cancel</Button>
            </DialogClose>
            <Button
              variant="destructive"
              onClick={confirmSync}
              // Same guard as every sync trigger: a dispatch already in
              // flight while the dialog sat open must not double-fire.
              disabled={busy || syncPending}
            >
              <RefreshCwIcon data-icon="inline-start" />
              Sync and overwrite
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={sourcesOpen} onOpenChange={setSourcesOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-base">
              <FileTextIcon className="size-4 text-muted-foreground" />
              Source files
            </DialogTitle>
            <DialogDescription>
              The wiki pages the guardrails are inferred from.
            </DialogDescription>
          </DialogHeader>
          {/* Plain scroll div, NOT ScrollArea: Radix's viewport wraps children
              in a display:table div that grows to fit long unbreakable paths,
              so truncation never engages and the list bleeds past the dialog.
              break-all keeps the full path readable within the width. */}
          <div className="okf-thin-scroll max-h-80 min-w-0 overflow-y-auto">
            <ul className="space-y-1 pr-3 font-mono text-xs text-muted-foreground">
              {(data?.sources || []).map((path) => (
                <li key={path} className="break-all">
                  {path}
                </li>
              ))}
            </ul>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
