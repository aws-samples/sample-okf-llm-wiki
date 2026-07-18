import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import rehypeHighlight from "rehype-highlight"
import {
  CheckCircle2Icon,
  ChevronDownIcon,
  CircleDashedIcon,
  CoinsIcon,
  DatabaseIcon,
  FileTextIcon,
  GaugeIcon,
  ListTreeIcon,
  MessageSquareTextIcon,
  PlayIcon,
  SearchIcon,
  SlidersHorizontalIcon,
  SparklesIcon,
  TerminalIcon,
  UsersIcon,
  WrenchIcon,
  XCircleIcon,
  XIcon,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ButtonGroup } from "@/components/ui/button-group"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Dialog,
  DialogClose,
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
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import {
  MODEL_CATALOG,
  defaultEffortFor,
  effortsFor,
  entryFor,
  loadPreference,
  savePreference,
} from "@/lib/harvestModels"

const POLL_MS = 4000

// The step feed poll cadence. Message-level events (not tokens) at 5s read as
// live while keeping FilterLogEvents well under its 10 TPS account-wide quota
// even with several viewers open at once.
const FEED_POLL_MS = 5000

// Terminal harvest states — once reached, the status row won't change again, so
// the live poll can stop (a new "Start full harvest" restarts it).
const TERMINAL_STATUSES = new Set(["complete", "failed", "cancelled"])

// In-flight states: a harvest here can be cancelled (stops the AgentCore session
// and frees the lease). Mirrors the backend's cancellable predicate.
const CANCELLABLE_STATUSES = new Set(["queued", "running"])

// Map harvest status -> Badge variant. queued/running are in-flight, complete
// is success, failed/cancelled are terminal (cancelled is operator-initiated, so
// outline rather than destructive).
function statusVariant(status) {
  switch (status) {
    case "complete":
      return "default"
    case "failed":
      return "destructive"
    case "running":
    case "queued":
      return "secondary"
    case "cancelled":
      return "outline"
    default:
      return "outline"
  }
}

// Start a harvest for the selected dataset and poll its status every ~4s.
export default function HarvestView({ api, selection }) {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [startingAnnotations, setStartingAnnotations] = useState(false)
  // Dataset guidance (shared authoring instructions). `guidance` is the saved
  // server value + dirty flag; `guidanceDraft` is the editable buffer; `dirty`
  // means the draft differs from what's saved (unsaved edits).
  const [guidance, setGuidance] = useState(null)
  const [guidanceDraft, setGuidanceDraft] = useState("")
  const [guidanceLoading, setGuidanceLoading] = useState(false)
  const [savingGuidance, setSavingGuidance] = useState(false)
  // Confirm modal before a full harvest OVERWRITES an existing bundle (a full
  // run wipes and re-authors every doc — clean_authored_output — so a misclick
  // is destructive). Only gated when a bundle already exists; see requestStartHarvest.
  const [confirmFullOpen, setConfirmFullOpen] = useState(false)
  // Per-harvest model/effort picker — a persisted USER PREFERENCE (localStorage),
  // so it survives a page refresh. Initialised from loadPreference() (validated
  // against the current catalog) and saved on every change. Sent on the next
  // Start; when untouched, this still sends the persisted/default choice
  // explicitly (the backend also has a deploy-time default, but sending it makes
  // the choice visible/auditable).
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [pref] = useState(loadPreference)
  const [model, setModel] = useState(pref.model)
  const [effort, setEffortState] = useState(pref.effort)

  const setEffort = useCallback(
    (next) => {
      setEffortState(next)
      savePreference(model, next)
    },
    [model]
  )
  // When the model changes, snap effort to that model's default if the current
  // effort isn't one it offers (e.g. switching Claude->GPT drops "max"), and
  // persist the resulting pair.
  const onModelChange = useCallback((next) => {
    setModel(next)
    setEffortState((cur) => {
      const resolved = effortsFor(next).includes(cur)
        ? cur
        : defaultEffortFor(next)
      savePreference(next, resolved)
      return resolved
    })
  }, [])
  const [events, setEvents] = useState([])
  // True while the feed is still pulling pages of a TERMINAL run (the endpoint
  // caps each response, so a completed harvest with a long history takes several
  // polls to drain). Distinct from `running`: it drives a "loading more" hint so
  // a fresh load of a completed run doesn't jump from page 1 to the final state
  // with nothing in between.
  const [draining, setDraining] = useState(false)
  const intervalRef = useRef(null)
  // Live feed: its own faster interval + a monotonic seq cursor. The cursor is a
  // ref (not state) so advancing it never re-triggers the poll effect.
  const feedIntervalRef = useRef(null)
  const feedCursorRef = useRef(0)
  // Timestamp cursor (ms): the highest CloudWatch event ts seen. Bounds the
  // server's scan window so each poll is O(recent), not O(whole run). 0 on a
  // fresh feed → the server backfills the whole current run from its start.
  const feedTsCursorRef = useRef(0)
  // Guards against overlapping polls: FilterLogEvents can take longer than
  // FEED_POLL_MS, so a naive interval fires the next poll before the previous
  // resolves — both read the same (un-advanced) cursor and re-fetch + append the
  // same batch (the duplicate-rows bug). Skip a tick while one is in flight.
  const feedInFlightRef = useRef(false)
  // Bumped on every feed reset (selection change / new harvest). A poll that was
  // in flight across a reset compares the generation it started under and drops
  // its result, so a prior dataset's late events can't append to the new feed.
  const feedGenRef = useRef(0)
  // Always points at the latest pollEvents so the drain can re-invoke it without
  // a self-referencing useCallback dependency cycle.
  const pollEventsRef = useRef(null)

  // Reset the feed to empty for a fresh run/selection (shared by the effect and
  // startHarvest so the invalidation is done in exactly one place).
  const resetFeed = useCallback(() => {
    feedGenRef.current += 1
    feedInFlightRef.current = false
    feedCursorRef.current = 0
    feedTsCursorRef.current = 0
    setEvents([])
    setDraining(false)
  }, [])

  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const hasSelection = Boolean(domain && dataset)

  const stopPolling = useCallback(() => {
    if (intervalRef.current) clearInterval(intervalRef.current)
    intervalRef.current = null
  }, [])

  const stopFeed = useCallback(() => {
    if (feedIntervalRef.current) clearInterval(feedIntervalRef.current)
    feedIntervalRef.current = null
  }, [])

  // Poll the incremental step feed: fetch events after our cursor, append the
  // new batch, advance the cursor. Stop the feed interval only once the harvest
  // is terminal AND this poll drained the tail (no new events past our cursor):
  // the endpoint caps each response at a page limit, so on a fresh load of a
  // completed run the first poll can report `done` while more events remain —
  // stopping on `done` alone would strand that tail (feed lags the summary).
  // Best-effort: a transient error just retries next tick.
  const pollEvents = useCallback(async () => {
    if (!api || !hasSelection) return
    // Only one poll at a time — a slow FilterLogEvents must not let the next
    // tick re-fetch from the same cursor and double-append.
    if (feedInFlightRef.current) return
    feedInFlightRef.current = true
    const gen = feedGenRef.current
    try {
      const res = await api.harvestEvents(
        domain,
        dataset,
        feedCursorRef.current,
        feedTsCursorRef.current
      )
      // The feed was reset (selection changed / new harvest) while we awaited —
      // drop this stale result so a prior run's events don't leak in.
      if (gen !== feedGenRef.current) return
      // Advance the timestamp cursor regardless of whether new events parsed, so
      // the scan window keeps sliding forward and polls stay cheap.
      if (typeof res?.next_ts === "number")
        feedTsCursorRef.current = res.next_ts
      const batch = Array.isArray(res?.events) ? res.events : []
      if (batch.length) {
        feedCursorRef.current = res.next ?? feedCursorRef.current
        // Dedup by seq defensively: even with the in-flight guard, a retry or a
        // cursor that didn't advance shouldn't render the same step twice.
        setEvents((prev) => {
          const seen = new Set(prev.map((e) => e.seq))
          const fresh = batch.filter((e) => !seen.has(e.seq))
          return fresh.length ? [...prev, ...fresh] : prev
        })
      } else if (typeof res?.next === "number") {
        feedCursorRef.current = res.next
      }
      // Terminal harvest: keep draining until a poll returns no new events, so a
      // completed run with more than one page of steps isn't cut off at the page
      // limit. Once done and this poll was empty, the tail is flushed — stop.
      // While pages are still coming back, surface a "loading more" hint AND
      // re-poll immediately (rather than waiting a full FEED_POLL_MS per page) so
      // the backlog drains promptly instead of trickling in every few seconds.
      if (res?.done) {
        if (batch.length) {
          setDraining(true)
          // Re-run on the next microtask so this poll's `finally` clears the
          // in-flight guard first; guarded by the generation so a reset cancels it.
          Promise.resolve().then(() => {
            if (gen === feedGenRef.current) pollEventsRef.current?.()
          })
        } else {
          setDraining(false)
          stopFeed()
        }
      }
    } catch {
      // Non-fatal: the feed is an enhancement over the durable status.
    } finally {
      // Only clear if a reset didn't already flip it (and start a new poll);
      // clearing across a reset could let two polls run concurrently.
      if (gen === feedGenRef.current) feedInFlightRef.current = false
    }
  }, [api, domain, dataset, hasSelection, stopFeed])

  // Keep the ref pointing at the latest pollEvents so the drain re-invoke above
  // always calls the current closure (fresh domain/dataset/api).
  pollEventsRef.current = pollEvents

  const startFeed = useCallback(() => {
    stopFeed()
    feedIntervalRef.current = setInterval(() => pollEvents(), FEED_POLL_MS)
  }, [pollEvents, stopFeed])

  const poll = useCallback(
    async ({ withSpinner = false } = {}) => {
      if (!api || !hasSelection) return null
      if (withSpinner) setLoading(true)
      try {
        const s = await api.harvestStatus(domain, dataset)
        setStatus(s)
        setError(null)
        // Stop the live poll once the harvest reaches a terminal state — the
        // status row is durable and won't change until a new harvest starts.
        if (TERMINAL_STATUSES.has(s?.status?.status)) stopPolling()
        return s
      } catch (e) {
        setError(e.message || String(e))
        return null
      } finally {
        if (withSpinner) setLoading(false)
      }
    },
    [api, domain, dataset, hasSelection, stopPolling]
  )

  const startPolling = useCallback(() => {
    stopPolling()
    intervalRef.current = setInterval(() => poll(), POLL_MS)
  }, [poll, stopPolling])

  // Load the dataset's saved guidance (+ dirty flag) and seed the editable draft.
  const loadGuidance = useCallback(async () => {
    if (!api || !hasSelection) return
    setGuidanceLoading(true)
    try {
      const g = await api.getDatasetGuidance(domain, dataset)
      setGuidance(g)
      setGuidanceDraft(g.guidance || "")
    } catch {
      // A brand-new mapping may 404 briefly; treat as empty guidance.
      setGuidance({ guidance: "", guidance_dirty: false })
      setGuidanceDraft("")
    } finally {
      setGuidanceLoading(false)
    }
  }, [api, domain, dataset, hasSelection])

  // Persist the draft. Bumps the server version → guidance goes DIRTY until the
  // next re-harvest applies it.
  const saveGuidance = useCallback(async () => {
    if (!api || !hasSelection) return
    setSavingGuidance(true)
    try {
      const g = await api.setDatasetGuidance(domain, dataset, guidanceDraft)
      setGuidance(g)
      setGuidanceDraft(g.guidance || "")
      toast.success(
        g.guidance
          ? "Guidance saved — run a re-harvest to apply it."
          : "Guidance cleared."
      )
    } catch (e) {
      toast.error(`Could not save guidance: ${e.message || e}`)
    } finally {
      setSavingGuidance(false)
    }
  }, [api, domain, dataset, hasSelection, guidanceDraft])

  useEffect(() => {
    setStatus(null)
    setError(null)
    // Reset the feed whenever the selection changes so a prior dataset's steps
    // don't bleed into the new one (also invalidates any in-flight poll).
    resetFeed()
    if (!hasSelection) return undefined
    poll({ withSpinner: true })
    startPolling()
    pollEvents()
    startFeed()
    loadGuidance()
    return () => {
      stopPolling()
      stopFeed()
    }
  }, [
    poll,
    startPolling,
    stopPolling,
    pollEvents,
    startFeed,
    stopFeed,
    resetFeed,
    loadGuidance,
    hasSelection,
  ])

  const startHarvest = async () => {
    if (!hasSelection) return
    setStarting(true)
    try {
      await api.startHarvest(domain, dataset, "full", model, effort)
      toast.success(`Harvest queued for ${domain}/${dataset}`)
      // A fresh harvest starts a new run: clear the prior feed + rewind the
      // cursor so we don't show stale steps or skip the new run's early events.
      resetFeed()
      await poll()
      // A fresh harvest is in flight — resume live polling even if a prior run
      // had reached a terminal state and stopped the interval.
      startPolling()
      startFeed()
    } catch (err) {
      toast.error(`Could not start harvest: ${err.message || err}`)
    } finally {
      setStarting(false)
    }
  }

  // Re-harvest driven by the caller's wiki annotations. The backend takes the
  // same per-dataset lease, sweeps orphaned notes, and only invokes the agent if
  // some live annotations remain (else returns {status:"complete", skipped}).
  const startAnnotationHarvest = async () => {
    if (!hasSelection) return
    setStartingAnnotations(true)
    try {
      const res = await api.runAnnotationHarvest(domain, dataset)
      if (res?.skipped) {
        toast.info(
          res.orphaned
            ? `No live annotations — ${res.orphaned} auto-resolved as orphaned.`
            : "No open annotations or guidance changes to apply."
        )
        await poll()
        return
      }
      // A run can carry annotations, a pending guidance change, or both.
      const parts = []
      if (res.annotations) parts.push(`${res.annotations} annotation(s)`)
      if (res.guidance_applied) parts.push("updated guidance")
      toast.success(
        `Re-harvest queued for ${parts.join(" + ") || "this dataset"}` +
          (res.orphaned ? ` (${res.orphaned} orphaned)` : "")
      )
      // A guidance run just cleared its dirty flag on success — refresh it.
      loadGuidance()
      resetFeed()
      await poll()
      startPolling()
      startFeed()
    } catch (err) {
      const msg = err.message || String(err)
      toast.error(
        /-> 409/.test(msg)
          ? "A harvest is already running for this dataset."
          : `Could not start annotation harvest: ${msg}`
      )
    } finally {
      setStartingAnnotations(false)
    }
  }

  const cancelHarvest = async () => {
    if (!hasSelection) return
    setCancelling(true)
    try {
      const res = await api.cancelHarvest(domain, dataset)
      if (res?.cancelled) {
        // Distinguish "runtime session stopped" from "lease freed but the session
        // couldn't be stopped" (already gone / stop call errored) so the operator
        // knows whether compute was actually torn down.
        if (res.stopped_session) {
          toast.success(
            `Harvest cancelled for ${domain}/${dataset} — runtime session stopped`
          )
        } else {
          toast.warning(
            `Harvest cancelled for ${domain}/${dataset}, but the runtime session ` +
              `could not be stopped${res.stop_error ? ` (${res.stop_error})` : ""} — ` +
              `it may already have ended`
          )
        }
      } else {
        // The harvest reached a terminal state before the cancel landed — the
        // backend didn't clobber it. Report what actually happened.
        toast.info(
          `Harvest already ${res?.status || "finished"}; nothing to cancel.`
        )
      }
      await poll()
    } catch (err) {
      toast.error(`Could not cancel harvest: ${err.message || err}`)
      // Re-sync so a 409 (already terminal) is reflected in the UI.
      await poll()
    } finally {
      setCancelling(false)
    }
  }

  if (!hasSelection) {
    return (
      <Alert>
        <CircleDashedIcon />
        <AlertTitle>Select a dataset first</AlertTitle>
        <AlertDescription>
          Pick a dataset from the sidebar to start and watch a harvest.
        </AlertDescription>
      </Alert>
    )
  }

  const inner = status?.status || {}
  const currentStatus = inner.status || null
  const ready = status?.ready
  const running = CANCELLABLE_STATUSES.has(currentStatus)
  const aborted = currentStatus === "failed" || currentStatus === "cancelled"
  // Started and Updated only differ once the run reaches a terminal state
  // (report_status writes updated_at on transitions). While running they mirror
  // each other, so show Updated only when terminal to avoid a redundant row.
  const showUpdated = inner.updated_at && TERMINAL_STATUSES.has(currentStatus)

  // Full harvest is destructive when a bundle already exists (it wipes + rebuilds
  // every doc, discarding any prior authoring incl. applied annotations). Confirm
  // in that case; on a first-ever harvest (no bundle yet) start straight away.
  const requestStartHarvest = () => {
    if (ready) setConfirmFullOpen(true)
    else startHarvest()
  }
  const confirmStartHarvest = () => {
    setConfirmFullOpen(false)
    startHarvest()
  }

  return (
    // h-full: fill the content region (which is bounded by the viewport / the
    // floating sidebar's bottom gap), so the card — and its live feed — grow to
    // the max height available instead of a fixed cap. The region is
    // overflow-y-auto, so on a very short viewport the card still scrolls.
    <div className="flex h-full flex-col gap-4">
      <Card className="min-h-0 flex-1">
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2">
            <PlayIcon className="size-4" />
            Harvest
          </CardTitle>
          <CardDescription>
            Induct{" "}
            <span className="font-medium text-foreground">
              {domain}/{dataset}
            </span>{" "}
            into a knowledge bundle.
          </CardDescription>
          <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
            <Button
              variant="outline"
              size="icon"
              onClick={() => setSettingsOpen(true)}
              disabled={running || starting}
              title="Harvest settings (model + reasoning effort)"
              aria-label="Harvest settings"
            >
              <SlidersHorizontalIcon className="size-4" />
            </Button>
            {running ? (
              <Button
                variant="destructive"
                onClick={cancelHarvest}
                disabled={cancelling}
              >
                {cancelling ? <Spinner /> : <XIcon data-icon="inline-start" />}
                Cancel harvest
              </Button>
            ) : (
              // A split control: the dropdown (LEFT) offers the annotation
              // re-harvest; the primary button (RIGHT) runs a full harvest.
              // no-overlap (ml-0): two solid buttons share a seam, so a visible
              // divider reads better than the default 1px border-overlap (which
              // would hide under the neighbour).
              <ButtonGroup className="[&>*:not(:first-child)]:ml-0">
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      size="icon"
                      disabled={starting || startingAnnotations}
                      aria-label="More harvest options"
                      title="More harvest options"
                      // Buttons carry `border border-transparent bg-clip-padding`;
                      // in dark mode that transparent 1px reveals the near-black
                      // page behind the button (a stray dark ring). Tint the border
                      // to the fill (border-primary) so it blends into the blue.
                      className="border-primary"
                    >
                      {startingAnnotations ? <Spinner /> : <ChevronDownIcon />}
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="w-64">
                    <DropdownMenuLabel>Re-harvest</DropdownMenuLabel>
                    <DropdownMenuItem
                      onSelect={startAnnotationHarvest}
                      disabled={startingAnnotations}
                      className="flex-col items-start gap-0.5"
                    >
                      <span className="flex items-center gap-2">
                        <MessageSquareTextIcon />
                        Apply annotations
                        {guidance?.guidance_dirty ? " + guidance" : ""}
                      </span>
                      <span className="pl-6 text-[11px] text-muted-foreground">
                        {guidance?.guidance_dirty
                          ? "In-place: your notes + the updated guidance."
                          : "In-place: applies your open annotations."}
                      </span>
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
                <Button
                  onClick={requestStartHarvest}
                  disabled={starting}
                  // border-primary blends the transparent edges into the fill (no
                  // dark ring in dark mode); the left edge is the divider seam.
                  className="border-primary border-l-primary-foreground/30"
                >
                  {starting ? (
                    <Spinner />
                  ) : (
                    <PlayIcon data-icon="inline-start" />
                  )}
                  Start full harvest
                </Button>
              </ButtonGroup>
            )}
          </div>
          <HarvestSettingsDialog
            open={settingsOpen}
            onOpenChange={setSettingsOpen}
            model={model}
            effort={effort}
            onModelChange={onModelChange}
            onEffortChange={setEffort}
          />
          <Dialog open={confirmFullOpen} onOpenChange={setConfirmFullOpen}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Re-run a full harvest?</DialogTitle>
                <DialogDescription>
                  <span className="font-medium text-foreground">
                    {domain}/{dataset}
                  </span>{" "}
                  already has a knowledge bundle. A full harvest rebuilds it
                  from scratch — every existing doc is discarded and
                  re-authored, including any applied annotations and manual
                  edits. This can't be undone. To apply new feedback without a
                  rebuild, use{" "}
                  <span className="font-medium">Apply my annotations</span>{" "}
                  instead.
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <DialogClose asChild>
                  <Button variant="outline">Cancel</Button>
                </DialogClose>
                <Button variant="destructive" onClick={confirmStartHarvest}>
                  <PlayIcon data-icon="inline-start" />
                  Rebuild bundle
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </CardHeader>
        <CardContent className="flex min-h-0 flex-1 flex-col gap-4">
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to read harvest status</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : loading && !status ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-6 w-40" />
              <Skeleton className="h-4 w-64" />
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <span className="text-sm text-muted-foreground">Status</span>
                {currentStatus ? (
                  <Badge variant={statusVariant(currentStatus)}>
                    {currentStatus}
                  </Badge>
                ) : (
                  <Badge variant="outline">no harvest yet</Badge>
                )}
                <Separator orientation="vertical" className="h-5" />
                <span className="text-sm text-muted-foreground">Bundle</span>
                {ready ? (
                  <Badge variant="default">
                    <CheckCircle2Icon />
                    ready
                  </Badge>
                ) : (
                  <Badge variant="outline">
                    <CircleDashedIcon />
                    not ready
                  </Badge>
                )}
              </div>

              {/* Dataset guidance — shared authoring instructions that steer every
                  harvest. Edit + Save persists it (goes DIRTY); the next full
                  harvest, or an "Apply annotations + guidance" re-harvest, applies
                  it and clears dirty. */}
              <div className="rounded-lg border bg-muted/20 p-3">
                <div className="mb-1.5 flex items-center gap-2">
                  <SparklesIcon className="size-4 text-muted-foreground" />
                  <span className="text-sm font-medium">Dataset guidance</span>
                  {guidance?.guidance_dirty ? (
                    <Badge
                      variant="outline"
                      className="border-amber-500/50 text-amber-600 dark:text-amber-400"
                    >
                      pending re-harvest
                    </Badge>
                  ) : guidance?.guidance ? (
                    <Badge variant="outline">
                      <CheckCircle2Icon />
                      applied
                    </Badge>
                  ) : null}
                </div>
                {guidanceLoading ? (
                  <Skeleton className="h-20 w-full" />
                ) : (
                  <>
                    <Textarea
                      value={guidanceDraft}
                      onChange={(e) => setGuidanceDraft(e.target.value)}
                      placeholder="Add dataset-specific authoring guidance…"
                      className="min-h-20 resize-y text-sm"
                      disabled={savingGuidance}
                    />
                    <div className="mt-2 flex items-center justify-end gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={saveGuidance}
                        disabled={
                          savingGuidance ||
                          guidanceDraft === (guidance?.guidance || "")
                        }
                      >
                        {savingGuidance ? <Spinner /> : null}
                        Save guidance
                      </Button>
                    </div>
                  </>
                )}
              </div>

              {(inner.mode ||
                inner.started_at ||
                showUpdated ||
                inner.detail ||
                inner.model) && (
                // Parallel columns to save vertical space: each field is a small
                // stacked (label over value) cell in a fixed 2-column grid (2×2 for
                // the usual Mode/Model/Started/Updated set).
                <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                  {inner.mode && (
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">Mode</dt>
                      <dd className="break-words">{inner.mode}</dd>
                    </div>
                  )}
                  {(inner.model || inner.effort) && (
                    // Model + effort merged into one field: model_id / effort_level.
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">Model</dt>
                      <dd className="break-words">
                        {entryFor(inner.model)?.label || inner.model || "—"}
                        {inner.effort ? ` / ${inner.effort}` : ""}
                      </dd>
                    </div>
                  )}
                  {inner.started_at && (
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">Started</dt>
                      <dd className="break-words">
                        {new Date(inner.started_at).toLocaleString()}
                      </dd>
                    </div>
                  )}
                  {showUpdated && (
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">Updated</dt>
                      <dd className="break-words">
                        {new Date(inner.updated_at).toLocaleString()}
                      </dd>
                    </div>
                  )}
                  {inner.detail && (
                    <div className="col-span-full min-w-0">
                      <dt className="text-xs text-muted-foreground">Detail</dt>
                      <dd className="break-words">{inner.detail}</dd>
                    </div>
                  )}
                </dl>
              )}

              {!currentStatus ? (
                <p className="text-sm text-muted-foreground">
                  No harvest has been recorded for this dataset yet. Click
                  "Start full harvest" to begin.
                </p>
              ) : (
                <>
                  <Separator />
                  <HarvestFeed
                    events={events}
                    running={running}
                    aborted={aborted}
                    draining={draining}
                  />
                </>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// -- Live step feed ---------------------------------------------------------

// A more specific icon for known tool_call labels, so the feed reads at a glance.
function toolIcon(tool) {
  switch (tool) {
    case "ls":
    case "glob":
      return ListTreeIcon
    case "grep":
      return SearchIcon
    case "read_file":
    case "write_file":
    case "edit_file":
      return FileTextIcon
    case "list_concepts":
    case "read_concept_raw":
    case "sample_rows":
    case "run_sql":
      return DatabaseIcon
    case "run_code":
      return TerminalIcon
    case "task":
      return UsersIcon
    default:
      return WrenchIcon
  }
}

// Fold the raw event stream into ordered display rows. Three row kinds:
//  - "agent": an AIMessage summary (passes through).
//  - "tool": a tool_call + its matching tool_result (same call_id) collapsed
//    into ONE row with a pending/ok/failed state.
//  - "fleet": a sub-agent fan-out shown as a row of squares. TWO fan-out paths
//    both fold into this row kind:
//      * QuickJS `eval()` → `task()`: lifecycle rides the custom stream as
//        `subagent` events (batch = the eval tool-call id). The `eval` tool row
//        itself is suppressed; the squares render where it happened.
//      * the static `task` tool: the model dispatches N `task` calls (often in
//        one turn, in parallel) with NO custom-stream lifecycle — each is a
//        plain top-level tool_call. We fold a contiguous WAVE of them into one
//        squares row (each dispatch = one square) instead of N "Started
//        table-author…" tool rows. A wave ends at any intervening non-task row
//        (an agent turn or another tool), so a later fan-out (e.g. the reviewer
//        pass) forms its own row.
// Rows stay in timeline order (by the seq they first appeared at).
function mergeRows(events, aborted) {
  const rows = []
  // call_id -> where its tool_result lands: a normal tool row, or a task square.
  const callIndex = new Map()
  const batchIndex = new Map() // fleet batch key -> index of its fleet row
  // The open static-task wave key, or null between waves. Reset by any row that
  // isn't a `task` call (agent/tool/subagent) — task RESULTS produce no row, so
  // they don't close a wave that's still being dispatched.
  let openTaskWave = null
  let taskWaveSeq = 0

  const fleetRowFor = (batch, seq) => {
    let idx = batchIndex.get(batch)
    if (idx == null) {
      idx = rows.length
      batchIndex.set(batch, idx)
      rows.push({ kind: "fleet", batch, seq, squares: new Map() })
    }
    return rows[idx]
  }

  for (const e of events) {
    if (e.kind === "subagent") {
      openTaskWave = null
      if (!e.batch || !e.sub_id) continue
      const row = fleetRowFor(`sub:${e.batch}`, e.seq)
      const sq = row.squares.get(e.sub_id)
      if (e.phase === "start") {
        if (sq) sq.state = "active"
        else
          row.squares.set(e.sub_id, {
            id: e.sub_id,
            state: "active",
            label: e.label,
          })
      } else if (e.phase === "complete" || e.phase === "error") {
        const state = e.phase === "complete" ? "done" : "error"
        if (sq) sq.state = state
        else row.squares.set(e.sub_id, { id: e.sub_id, state, label: e.label })
      }
    } else if (e.kind === "tool_call") {
      // The eval tool IS the QuickJS fan-out dispatcher — don't show it as a tool
      // row; its batch renders as a fleet row (created when its subagents arrive).
      if (e.tool === "eval") {
        openTaskWave = null
        continue
      }
      if (e.tool === "task") {
        // Static task-tool dispatch → a square in the current wave's fleet row.
        if (openTaskWave == null) {
          taskWaveSeq += 1
          openTaskWave = `task:${taskWaveSeq}`
        }
        const row = fleetRowFor(openTaskWave, e.seq)
        row.squares.set(e.call_id, {
          id: e.call_id,
          state: "active",
          label: e.label,
        })
        callIndex.set(e.call_id, { kind: "task", batch: openTaskWave })
        continue
      }
      openTaskWave = null
      callIndex.set(e.call_id, { kind: "tool", idx: rows.length })
      // kind AFTER the spread so e's own `kind:"tool_call"` can't overwrite it.
      rows.push({ ...e, kind: "tool", state: "pending" })
    } else if (e.kind === "tool_result") {
      // A result produces no new row, so it does NOT close an open task wave.
      const target = e.call_id != null ? callIndex.get(e.call_id) : undefined
      if (!target) continue
      if (target.kind === "tool") {
        rows[target.idx] = {
          ...rows[target.idx],
          state: e.ok ? "ok" : "failed",
        }
      } else {
        // A task dispatch finished — flip its square done/error.
        const idx = batchIndex.get(target.batch)
        const sq = idx != null ? rows[idx].squares.get(e.call_id) : null
        if (sq) sq.state = e.ok ? "done" : "error"
      }
      // Orphan result (call outside our window, or the eval's): carries no
      // tool/label, so rendering it standalone would be a blank row. Drop it.
    } else if (e.kind === "agent") {
      openTaskWave = null
      rows.push({ ...e, kind: "agent" })
    } else if (e.kind === "benchmark_progress" || e.kind === "benchmark") {
      // One row PER benchmark iteration, updated in place as phase/counters
      // advance (progress ticks) and finalized by the round-done "benchmark"
      // event (which carries the KPIs). Keyed by iteration so a round's many
      // ticks collapse into a single advancing row rather than N feed rows.
      const iter = e.iteration ?? 0
      const key = `bench:${iter}`
      let idx = batchIndex.get(key)
      if (idx == null) {
        idx = rows.length
        batchIndex.set(key, idx)
        rows.push({ kind: "benchmark", iteration: iter, seq: e.seq })
      }
      const done = e.kind === "benchmark"
      rows[idx] = {
        ...rows[idx],
        ...e,
        kind: "benchmark", // keep the row kind regardless of event kind
        done,
      }
    }
  }

  // Materialise fleet rows' square maps to arrays. When the run is aborted
  // (failed/cancelled), any square still "active" never got a terminal event —
  // render it red so a killed fan-out doesn't look like it's still working.
  return rows.map((r) => {
    if (r.kind !== "fleet") return r
    const squares = [...r.squares.values()].map((sq) =>
      aborted && sq.state === "active" ? { ...sq, state: "error" } : sq
    )
    return { ...r, squares }
  })
}

// The latest cumulative token-usage snapshot in the stream, or null. Usage
// events (kind="usage") carry ABSOLUTE cumulative counts for the whole run, so
// the last one wins — no client-side summing (robust to a missed/re-ordered
// poll). Scans from the end for the first usage event.
function latestUsage(events) {
  for (let i = events.length - 1; i >= 0; i--) {
    const u = events[i]?.usage
    if (u && typeof u === "object") return u
  }
  return null
}

// Compact token count: 1234 -> "1.2K", 1_200_000 -> "1.2M". Whole numbers under
// 1000 render as-is. Used for the running-total pill so it stays one glanceable
// chip regardless of magnitude.
function fmtTokens(n) {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`
  return `${v}`
}

// Fixed label-cell width so the "Agent"/"Tool" badges align in a column and the
// message text starts at the same x regardless of which kind a row is.
const LABEL_CELL = "w-14 shrink-0"

// Per-square color by lifecycle state. active = a lighter primary tint
// (running), done = primary, error = destructive.
const SQUARE_CLASS = {
  active: "bg-primary/40 animate-pulse",
  done: "bg-primary",
  error: "bg-destructive",
}

function FleetRow({ row }) {
  const done = row.squares.filter(
    (s) => s.state === "done" || s.state === "error"
  ).length
  return (
    <div className="flex items-center gap-2 rounded-md border px-2 py-1.5 text-sm">
      <UsersIcon className="size-3.5 shrink-0 text-muted-foreground" />
      <Badge variant="secondary" className={cn(LABEL_CELL, "justify-center")}>
        Agents
      </Badge>
      <div className="flex min-w-0 flex-1 flex-wrap gap-1">
        {row.squares.length === 0 ? (
          <span className="text-muted-foreground">Dispatching sub-agents…</span>
        ) : (
          row.squares.map((sq, i) => (
            <span
              key={sq.id || i}
              title={`${sq.label || "sub-agent"} — ${sq.state}`}
              className={cn("size-3.5 rounded-[3px]", SQUARE_CLASS[sq.state])}
            />
          ))
        )}
      </div>
      {row.squares.length ? (
        <span className="shrink-0 text-[10px] text-muted-foreground tabular-nums">
          {done}/{row.squares.length}
        </span>
      ) : null}
    </div>
  )
}

// A recursive-improvement benchmark row: one per iteration, updated in place as
// the round advances through phases (solving → grading → reviewing → done). Shows
// a phase label, an N/M counter + progress bar while running, and the KPI summary
// once the round is done.
const BENCH_PHASE_LABEL = {
  solving: "Solving",
  grading: "Grading",
  reviewing: "Reviewing",
  done: "Done",
}

function BenchmarkRow({ row }) {
  const iterLabel = `Round ${(row.iteration ?? 0) + 1}${
    row.max_iterations ? `/${row.max_iterations}` : ""
  }`
  const phase = row.phase || "solving"
  const total = row.total || 0
  const current = row.current || 0
  const pct =
    total > 0 ? Math.round((current / total) * 100) : row.done ? 100 : 0

  return (
    <div className="flex flex-col gap-1.5 rounded-md border px-2 py-1.5 text-sm">
      <div className="flex items-center gap-2">
        <GaugeIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <Badge variant="secondary" className="shrink-0">
          Benchmark
        </Badge>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">
          {iterLabel} — {BENCH_PHASE_LABEL[phase] || phase}
          {!row.done && total > 0 ? (
            <span className="tabular-nums">
              {" "}
              {current}/{total}
            </span>
          ) : null}
        </span>
        {row.done ? (
          <Badge
            variant={row.threshold_met ? "default" : "outline"}
            className="shrink-0"
          >
            {row.threshold_met ? "Target met" : "Below target"}
          </Badge>
        ) : (
          <span className="shrink-0 text-[10px] text-muted-foreground tabular-nums">
            {pct}%
          </span>
        )}
      </div>

      {/* Progress bar while running. */}
      {!row.done ? (
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary transition-[width] duration-500"
            style={{ width: `${pct}%` }}
          />
        </div>
      ) : (
        // Round-done KPI summary line.
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-6 text-xs text-muted-foreground tabular-nums">
          <span>
            EX{" "}
            <span className="font-medium text-foreground">
              {fmtPct(row.ex_score)}
            </span>
          </span>
          <span>
            Judge{" "}
            <span className="font-medium text-foreground">
              {fmtPct(row.judge_accuracy)}
            </span>
          </span>
          <span>
            {row.passed}/{row.graded} passed
          </span>
          {row.discarded ? <span>{row.discarded} discarded</span> : null}
          {Array.isArray(row.improvements) && row.improvements.length ? (
            <span>{row.improvements.length} improvement(s) fed back</span>
          ) : null}
        </div>
      )}
    </div>
  )
}

function fmtPct(v) {
  if (typeof v !== "number") return "—"
  return `${Math.round(v * 100)}%`
}

// Render an agent one-liner as INLINE markdown: GFM formatting (bold, code,
// links) but flattened to a single line — block elements render as inline spans
// (see the `.okf-inline-md` CSS) so it stays on the feed row and truncates.
const INLINE_MD_COMPONENTS = {
  // Strip the wrapping <p> so text flows inline without block margins.
  p: ({ children }) => <>{children}</>,
}

function InlineMarkdown({ text }) {
  return (
    <span className="okf-inline-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={INLINE_MD_COMPONENTS}
      >
        {text}
      </ReactMarkdown>
    </span>
  )
}

// The full agent message, rendered as GFM markdown in a modal. Opened by
// clicking an agent row whose text was trimmed to fit one line.
function AgentMessageDialog({ open, onOpenChange, text }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SparklesIcon className="size-4" />
            Agent message
          </DialogTitle>
        </DialogHeader>
        <ScrollArea className="max-h-[70vh]">
          <div className="okf-prose pr-3">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[
                [rehypeHighlight, { detect: true, ignoreMissing: true }],
              ]}
            >
              {text}
            </ReactMarkdown>
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}

// Per-harvest model + reasoning-effort picker. The options come from the
// Terraform-provided catalog (MODEL_CATALOG); the effort list is model-specific
// and re-derived each render so switching models shows only that model's efforts.
function HarvestSettingsDialog({
  open,
  onOpenChange,
  model,
  effort,
  onModelChange,
  onEffortChange,
}) {
  const efforts = effortsFor(model)
  // The Select dropdown portals OUTSIDE DialogContent, so clicking an item (or
  // the dropdown's own outside-click) looks like an "interact outside" to the
  // Dialog and would close the whole modal. Ignore dismissal events that
  // originate from a Select popup so only a real outside click closes the modal.
  const keepOpenOnSelectInteraction = (e) => {
    const t = e.target
    if (t instanceof Element && t.closest("[data-slot='select-content']")) {
      e.preventDefault()
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-md"
        onPointerDownOutside={keepOpenOnSelectInteraction}
        onInteractOutside={keepOpenOnSelectInteraction}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SlidersHorizontalIcon className="size-4" />
            Harvest settings
          </DialogTitle>
          <DialogDescription>
            Choose the model and reasoning effort for the next harvest.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="harvest-model">Model</Label>
            <Select value={model} onValueChange={onModelChange}>
              <SelectTrigger id="harvest-model" className="w-full">
                <SelectValue placeholder="Select a model..." />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {MODEL_CATALOG.map((m) => (
                    <SelectItem key={m.model} value={m.model}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="harvest-effort">Reasoning effort</Label>
            <Select value={effort} onValueChange={onEffortChange}>
              <SelectTrigger id="harvest-effort" className="w-full">
                <SelectValue placeholder="Select an effort..." />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {efforts.map((e) => (
                    <SelectItem key={e} value={e}>
                      {e}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </div>
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button">Done</Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function StepRow({ step }) {
  const isAgent = step.kind === "agent"
  const [open, setOpen] = useState(false)
  // "Trimmed" is a DISPLAY concern: an agent line is expandable if it doesn't fit
  // one row (CSS truncation) OR the server sent a richer `full` body. We measure
  // the label span's overflow so a short-but-too-wide line (no server `full`) is
  // still clickable — that was the bug. Fall back to `label` as the modal text.
  const labelRef = useRef(null)
  const [overflowing, setOverflowing] = useState(false)

  useEffect(() => {
    if (!isAgent) return undefined
    const el = labelRef.current
    if (!el) return undefined
    const check = () => setOverflowing(el.scrollWidth > el.clientWidth + 1)
    check()
    // Re-measure on width changes (sidebar toggle, window resize, feed growth).
    const ro = new ResizeObserver(check)
    ro.observe(el)
    return () => ro.disconnect()
  }, [isAgent, step.label])

  const expandable = isAgent && (Boolean(step.full) || overflowing)
  const modalText = step.full || step.label

  // Icon: agent -> sparkles; tool -> tool-specific, swapping to a check/cross
  // once the result lands so completion reads at a glance.
  let Icon
  if (isAgent) {
    Icon = SparklesIcon
  } else if (step.state === "ok") {
    Icon = CheckCircle2Icon
  } else if (step.state === "failed") {
    Icon = XCircleIcon
  } else {
    Icon = toolIcon(step.tool)
  }

  return (
    <>
      <div
        className={cn(
          "flex items-center gap-2 rounded-md border px-2 py-1.5 text-sm",
          expandable && "cursor-pointer hover:bg-muted/50"
        )}
        {...(expandable
          ? {
              role: "button",
              tabIndex: 0,
              onClick: () => setOpen(true),
              onKeyDown: (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault()
                  setOpen(true)
                }
              },
            }
          : {})}
      >
        <Icon className="size-3.5 shrink-0 text-muted-foreground" />
        <Badge
          variant={isAgent ? "secondary" : "outline"}
          className={cn(LABEL_CELL, "justify-center")}
        >
          {isAgent ? "Agent" : "Tool"}
        </Badge>
        <span ref={labelRef} className="min-w-0 flex-1 truncate">
          {isAgent ? <InlineMarkdown text={step.label} /> : step.label}
        </span>
        {/* Right-aligned trailing group: tool outcome, then time. */}
        <div className="flex shrink-0 items-center gap-2">
          {!isAgent && step.state === "pending" ? (
            <Spinner className="size-3" />
          ) : !isAgent && step.state === "failed" ? (
            <Badge variant="destructive">failed</Badge>
          ) : null}
          {step.ts ? (
            <span className="text-[10px] text-muted-foreground">
              {new Date(step.ts).toLocaleTimeString()}
            </span>
          ) : null}
        </div>
      </div>
      {/* Dialog is a SIBLING of the row (not a child): React events bubble up the
          component tree even from a portal, so nesting it inside the clickable
          row made the close button re-trigger the row's onClick and reopen it. */}
      {expandable ? (
        <AgentMessageDialog
          open={open}
          onOpenChange={setOpen}
          text={modalText}
        />
      ) : null}
    </>
  )
}

// One label/value line in the usage breakdown popover. `sub` renders it as an
// indented, smaller "of which…" child so cache lines read as a SUBSET of input
// rather than additional tokens (see UsagePill).
function UsageRow({ label, value, sub = false }) {
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-6",
        sub && "pl-3 text-xs"
      )}
    >
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums">{value.toLocaleString()}</span>
    </div>
  )
}

// Running-total token chip shown in the feed header. `usage` is the latest
// cumulative snapshot ({input, output, cache_read, cache_write, total}).
//
// IMPORTANT — Bedrock/LangChain semantics (langchain_aws _extract_usage_metadata):
// `input` is the FULL input count and ALREADY INCLUDES cache_read + cache_write;
// they are a breakdown of the input, not extra tokens (and `total` = input +
// output). So we must NOT list cache as a sibling of Input (that double-counts
// it, e.g. 4.09M input where 3.9M is cache reads reads as ~8M). Instead we show
// cache read/write as indented "of which…" children under Input, and derive the
// fresh (non-cached) input as input - cache_read - cache_write.
function UsagePill({ usage }) {
  const input = usage.input || 0
  const output = usage.output || 0
  const cacheRead = usage.cache_read || 0
  const cacheWrite = usage.cache_write || 0
  const total = usage.total ?? input + output
  const freshInput = Math.max(0, input - cacheRead - cacheWrite)
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="ml-auto focus-visible:outline-hidden"
          aria-label="Token usage breakdown"
        >
          <Badge
            variant="secondary"
            className="gap-1 font-normal tabular-nums transition-colors hover:bg-secondary/80"
          >
            <CoinsIcon className="size-3" />
            {fmtTokens(total)} tokens
          </Badge>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-60 gap-2">
        <PopoverHeader>
          <PopoverTitle className="flex items-center gap-1.5 text-sm">
            <CoinsIcon className="size-3.5" />
            Token usage
          </PopoverTitle>
          <PopoverDescription>Cumulative for this run.</PopoverDescription>
        </PopoverHeader>
        <div className="flex flex-col gap-1.5">
          <UsageRow label="Input" value={input} />
          {/* Cache lines are a breakdown of Input (indented), not additive. */}
          {cacheRead ? (
            <UsageRow sub label="from cache" value={cacheRead} />
          ) : null}
          {cacheWrite ? (
            <UsageRow sub label="cache writes" value={cacheWrite} />
          ) : null}
          {cacheRead || cacheWrite ? (
            <UsageRow sub label="fresh" value={freshInput} />
          ) : null}
          <UsageRow label="Output" value={output} />
          <Separator className="my-0.5" />
          <div className="flex items-center justify-between gap-6 font-medium">
            <span>Total</span>
            <span className="tabular-nums">{total.toLocaleString()}</span>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  )
}

// The scrollable, auto-following step feed, merged into the Harvest card. Snaps
// to the bottom on new events only when the user is already pinned there (so
// scrolling up to read history isn't yanked away). Uses a native overflow div —
// shadcn ScrollArea exposes no viewport ref, which the auto-follow needs.
function HarvestFeed({ events, running, aborted, draining = false }) {
  const viewportRef = useRef(null)
  const stickRef = useRef(true)

  useEffect(() => {
    const el = viewportRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [events])

  const onScroll = (e) => {
    const el = e.currentTarget
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }

  const rows = mergeRows(events, aborted)
  const usage = latestUsage(events)

  return (
    // min-h-0 flex-1: grow to fill the card's remaining height (the parent chain
    // is flex-1 down from the viewport-bounded content region), so the feed's
    // scroll area is as tall as the space allows rather than a fixed cap.
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
        <ListTreeIcon className="size-4" />
        Live feed
        {running || draining ? <Spinner className="size-3" /> : null}
        {usage ? <UsagePill usage={usage} /> : null}
      </div>
      <div
        ref={viewportRef}
        onScroll={onScroll}
        // min-h-0 flex-1: fill the remaining card height (bounded by the sidebar
        // bottom via the viewport-height shell) instead of a fixed max-h cap.
        // py-6: buffer so the first/last rows clear the scroll-fade mask (which
        // dissolves the top/bottom edges) instead of sitting under it at rest.
        className="okf-thin-scroll flex min-h-0 flex-1 scroll-fade-y flex-col gap-1 overflow-y-auto py-6 pr-1"
      >
        {rows.map((r) =>
          r.kind === "fleet" ? (
            <FleetRow key={`fleet-${r.batch}`} row={r} />
          ) : r.kind === "benchmark" ? (
            <BenchmarkRow key={`bench-${r.iteration}`} row={r} />
          ) : (
            <StepRow key={r.seq} step={r} />
          )
        )}
        {running && !rows.length ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Spinner className="size-3" />
            Waiting for the first step…
          </div>
        ) : null}
        {/* Draining a completed run's backlog: show progress as pages stream in
            so the feed doesn't jump from page 1 to the final state. */}
        {draining ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Spinner className="size-3" />
            Loading more steps…
          </div>
        ) : null}
        {!running && !draining && !rows.length ? (
          <p className="text-sm text-muted-foreground">
            No steps recorded for this run.
          </p>
        ) : null}
      </div>
    </div>
  )
}
