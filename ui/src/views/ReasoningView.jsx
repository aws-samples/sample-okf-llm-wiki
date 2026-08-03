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
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
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

// One authored guardrail as its own sub-card: id + track badge + source on
// the top line, then the two authored fields verbatim (cyan labels, full
// foreground text — the guardrail text is the payload, not an aside).
function GuardrailItem({ policy }) {
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
        <span className="font-medium text-cyan-600 dark:text-cyan-400">
          Condition:
        </span>{" "}
        {policy.condition}
      </p>
      <p className="text-foreground">
        <span className="font-medium text-cyan-600 dark:text-cyan-400">
          Action:
        </span>{" "}
        {policy.action}
      </p>
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

  const load = useCallback(async () => {
    if (!api || !hasSelection) return
    try {
      setData(await api.getReasoning(domain, dataset))
      setError(null)
    } catch (e) {
      setError(e.message || String(e))
    }
  }, [api, domain, dataset, hasSelection])

  // Reset + load on dataset change.
  useEffect(() => {
    setData(null)
    setError(null)
    setSourcesOpen(false)
    setTypeFilter("all")
    load()
  }, [load])

  const building = data?.status === "building"
  const failed = data?.status === "failed"
  // "Settled" ends the queued-sync window: either the build poll takes over
  // (building) or the row already reached a usable, current policy — a fast
  // snapshot restore can land between two polls without ever showing
  // building. Booleans (not `data`) as deps so polling doesn't reset the
  // give-up timer every refresh.
  const settled =
    building || (data?.up_to_date === true && data?.status === "ready")
  useEffect(() => {
    if (!building && !syncPending) return
    const id = setInterval(load, BUILD_POLL_MS)
    return () => clearInterval(id)
  }, [building, syncPending, load])
  // Once the row settles, the queued window closes; if the dispatch was lost,
  // give up after 90s and re-enable the manual buttons.
  useEffect(() => {
    if (!syncPending) return
    if (settled) {
      setSyncPending(false)
      return
    }
    const id = setTimeout(() => setSyncPending(false), 90_000)
    return () => clearTimeout(id)
  }, [syncPending, settled])

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
                its wiki. They re-author automatically when the wiki changes.
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
          {!data?.wiki_ready ? (
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
              {/* Building: the page polls; authoring IS completion (v3 — no
                  build workflow, no completion authority). A build that died
                  leaves the row `building` until the rebuild authority's
                  reaper: Sync (and every policy_rebuild event) is a NO-OP for
                  a building row younger than the ~1h grace, then fails and
                  re-dispatches it. Sync stays reachable here as that
                  post-grace recovery — not as a completion trigger. */}
              {building ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">
                    A build is running — this page updates automatically. A
                    build that dies is retried by Sync after about an hour;
                    until then Sync leaves it undisturbed.
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy || syncPending}
                    onClick={sync}
                  >
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
                    Out of date — the wiki changed since these guardrails were
                    built. Checks are paused until they rebuild.
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy || syncPending}
                    onClick={sync}
                  >
                    <RefreshCwIcon className="size-3.5" />
                    Sync now
                  </Button>
                </div>
              ) : null}
              {data?.up_to_date === true && !building && !failed ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">
                    Up to date with the latest wiki version.
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy || syncPending}
                    onClick={sync}
                  >
                    <RefreshCwIcon className="size-3.5" />
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
                    onClick={sync}
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
      <Dialog open={sourcesOpen} onOpenChange={setSourcesOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-base">
              <FileTextIcon className="size-4 text-muted-foreground" />
              Source files
            </DialogTitle>
            <DialogDescription>
              The wiki pages the guardrails are inferred from. Editing any of
              these (or a harvest changing them) makes the guardrails out of
              date.
            </DialogDescription>
          </DialogHeader>
          <ScrollArea className="max-h-80">
            <ul className="space-y-1 pr-3 font-mono text-xs text-muted-foreground">
              {(data?.sources || []).map((path) => (
                <li key={path} className="truncate">
                  {path}
                </li>
              ))}
            </ul>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </>
  )
}
