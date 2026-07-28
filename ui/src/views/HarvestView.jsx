import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"
import hljs from "highlight.js/lib/common"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import rehypeHighlight from "rehype-highlight"
import {
  CheckCircle2Icon,
  CheckIcon,
  ChevronDownIcon,
  CircleDashedIcon,
  CoinsIcon,
  DatabaseIcon,
  FileTextIcon,
  GaugeIcon,
  Link2Icon,
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
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
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
  SelectLabel,
  SelectSeparator,
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
import { CopyButton } from "@/components/ui/copy-button"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
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
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import {
  GROUPED_MODEL_CATALOG,
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

// Human-facing labels for the backend's mode strings (the wire values are
// frozen in CONVENTIONS.md; presentation is the UI's job). Unknown modes fall
// back to the raw string so a new backend mode is never hidden.
const MODE_LABEL = {
  full: "Full harvest",
  incremental: "Incremental update",
  annotated: "Apply annotations",
  cross: "Cross-dataset discovery",
  repromote: "Version repromote",
}

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
// `datasets` is the registered-mapping list (threaded from App) — the candidate
// pool for the cross-dataset target picker.
export default function HarvestView({ api, selection, datasets = [] }) {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [startingAnnotations, setStartingAnnotations] = useState(false)
  // Cross-dataset harvest: the target-picker dialog + the chosen counterpart
  // (encoded "<domain>/<dataset>" — segments can't contain a slash).
  const [crossOpen, setCrossOpen] = useState(false)
  const [crossTarget, setCrossTarget] = useState("")
  const [startingCross, setStartingCross] = useState(false)
  // Whether the published bundle carries an external/ subtree (cross-dataset
  // docs). Probed from the bundle listing on selection change; refreshed at
  // click time before the annotation run so the scope question is never stale.
  const [hasExternal, setHasExternal] = useState(false)
  // "Apply annotations" picker modal: a scope dropdown (dataset docs vs
  // external/ cross-dataset docs) over the caller's pending notes, each
  // individually selectable so a run can apply a PARTIAL selection. Always
  // shown (even with no external/ docs) — picking notes is the point.
  const [annoOpen, setAnnoOpen] = useState(false)
  const [annoLoading, setAnnoLoading] = useState(false)
  const [annoNotes, setAnnoNotes] = useState([])
  // "dataset" or "cross:<domain>/<dataset>" — cross scopes are PER TARGET PAIR,
  // never a generic bucket: the selected notes drive the run's session-policy
  // widening (extra_glue_databases), so one run = one visible target.
  const [annoScope, setAnnoScope] = useState("dataset")
  const [annoSelected, setAnnoSelected] = useState(() => new Set())
  // The distinct external/<domain>/<dataset> pairs present in the bundle.
  const [externalPairs, setExternalPairs] = useState([])
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
  // The SUB-AGENT pair (authors/extractors/benchmark). subModel === "" means
  // "same as harvester": nothing is sent and the runtime reuses the harvester's
  // config for the sub-agents.
  const [subModel, setSubModel] = useState(pref.subagentModel)
  const [subEffort, setSubEffortState] = useState(pref.subagentEffort)
  // The adversarial REVIEWER's own pair. revModel === "" means "same as
  // sub-agents" — a different model family here improves review coverage (a
  // fresh model doesn't share the authoring model's blind spots).
  const [revModel, setRevModel] = useState(pref.reviewerModel)
  const [revEffort, setRevEffortState] = useState(pref.reviewerEffort)

  const setEffort = useCallback(
    (next) => {
      setEffortState(next)
      savePreference(model, next, subModel, subEffort, revModel, revEffort)
    },
    [model, subModel, subEffort, revModel, revEffort]
  )
  // When the model changes, snap effort to that model's default if the current
  // effort isn't one it offers (e.g. switching Claude->GPT drops "max"), and
  // persist the resulting pair.
  const onModelChange = useCallback(
    (next) => {
      setModel(next)
      setEffortState((cur) => {
        const resolved = effortsFor(next).includes(cur)
          ? cur
          : defaultEffortFor(next)
        savePreference(next, resolved, subModel, subEffort, revModel, revEffort)
        return resolved
      })
    },
    [subModel, subEffort, revModel, revEffort]
  )
  const setSubEffort = useCallback(
    (next) => {
      setSubEffortState(next)
      savePreference(model, effort, subModel, next, revModel, revEffort)
    },
    [model, effort, subModel, revModel, revEffort]
  )
  // Same snap-to-default dance for the sub-agent pair; "" (same as harvester)
  // clears the effort too.
  const onSubModelChange = useCallback(
    (next) => {
      setSubModel(next)
      setSubEffortState((cur) => {
        const resolved = !next
          ? ""
          : effortsFor(next).includes(cur)
            ? cur
            : defaultEffortFor(next)
        savePreference(model, effort, next, resolved, revModel, revEffort)
        return resolved
      })
    },
    [model, effort, revModel, revEffort]
  )
  const setRevEffort = useCallback(
    (next) => {
      setRevEffortState(next)
      savePreference(model, effort, subModel, subEffort, revModel, next)
    },
    [model, effort, subModel, subEffort, revModel]
  )
  // And for the reviewer pair; "" (same as sub-agents) clears the effort too.
  const onRevModelChange = useCallback(
    (next) => {
      setRevModel(next)
      setRevEffortState((cur) => {
        const resolved = !next
          ? ""
          : effortsFor(next).includes(cur)
            ? cur
            : defaultEffortFor(next)
        savePreference(model, effort, subModel, subEffort, next, resolved)
        return resolved
      })
    },
    [model, effort, subModel, subEffort]
  )
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

  // Probe the bundle listing for an external/ subtree (cross-dataset docs).
  // Cheap (one listing call); returns the fresh answer AND caches it in state so
  // the dialogs' copy can reference it without re-probing.
  const probeExternal = useCallback(async () => {
    if (!api || !hasSelection) return false
    try {
      const files = await api.listBundle(domain, dataset)
      // Collect the distinct target pairs (external/<domain>/<dataset>/…) so
      // the annotation picker can offer one scope PER TARGET.
      const pairs = new Set()
      for (const f of files || []) {
        const cid = f.concept_id || ""
        if (!cid.startsWith("external/")) continue
        const parts = cid.split("/")
        if (parts.length >= 4) pairs.add(`${parts[1]}/${parts[2]}`)
      }
      setHasExternal(pairs.size > 0)
      setExternalPairs([...pairs].sort())
      return pairs.size > 0
    } catch {
      // No bundle yet (or a transient read error) — treat as no external docs.
      setHasExternal(false)
      setExternalPairs([])
      return false
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
    setHasExternal(false)
    setCrossTarget("")
    // Reset the feed whenever the selection changes so a prior dataset's steps
    // don't bleed into the new one (also invalidates any in-flight poll).
    resetFeed()
    if (!hasSelection) return undefined
    poll({ withSpinner: true })
    startPolling()
    pollEvents()
    startFeed()
    loadGuidance()
    probeExternal()
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
    probeExternal,
    hasSelection,
  ])

  const startHarvest = async () => {
    if (!hasSelection) return
    setStarting(true)
    try {
      await api.startHarvest(
        domain,
        dataset,
        "full",
        model,
        effort,
        subModel,
        subModel ? subEffort : "",
        revModel,
        revModel ? revEffort : ""
      )
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
  // `scope` ("dataset" | "cross") + `annotationIds` (the picker's selection)
  // narrow the run; `crossTarget` ("<domain>/<dataset>") names the pair a
  // cross-scoped run verifies against — all chosen in the picker modal below.
  const startAnnotationHarvest = async (scope, annotationIds, crossTarget) => {
    if (!hasSelection) return
    setStartingAnnotations(true)
    try {
      const res = await api.runAnnotationHarvest(
        domain,
        dataset,
        scope,
        annotationIds,
        crossTarget
      )
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

  // The "Apply annotations" picker. Always opens (even with no external/ docs):
  // the modal is where the user picks WHICH notes ride the run. It loads the
  // caller's pending notes (open + in_review stragglers from a dead run — the
  // same set the backend would reclaim) and probes for external/ docs FRESH at
  // click time (a cross harvest may have added them since the view loaded) to
  // build the per-target cross scopes. Notes on external/ docs verify against
  // THAT target's data — the run's SQL permissions are widened to exactly the
  // targets the selected notes reference — so each pair is its own scope.
  const externalPairOf = (n) => {
    const cid = n.concept_id || ""
    if (!cid.startsWith("external/")) return null
    const parts = cid.split("/")
    return parts.length >= 4 ? `${parts[1]}/${parts[2]}` : null
  }
  // The notes belonging to a scope: "dataset" = the dataset's own docs,
  // "cross:<pair>" = that pair's docs. GENERAL (`_dataset`-wide) notes are
  // valid steering for either, so they appear in EVERY scope — the backend
  // scope filter agrees (it never filters them; the selection decides).
  const isGeneralNote = (n) => (n.concept_id || "") === "_dataset"
  const notesInScope = (notes, scope) =>
    scope === "dataset"
      ? notes.filter((n) => !(n.concept_id || "").startsWith("external/"))
      : notes.filter(
          (n) =>
            isGeneralNote(n) ||
            externalPairOf(n) === scope.slice("cross:".length)
        )
  // Preselect everything in scope, EXCEPT general notes in a cross scope —
  // there they're offered (tickable) but opt-in, so switching to a pair never
  // silently consumes general feedback meant for the dataset run.
  const seedAnnoSelection = (notes, scope) =>
    new Set(
      notesInScope(notes, scope)
        .filter((n) => scope === "dataset" || !isGeneralNote(n))
        .map((n) => n.annotation_id)
    )
  const openAnnotationPicker = async () => {
    if (!hasSelection) return
    setAnnoOpen(true)
    setAnnoLoading(true)
    try {
      const [notes] = await Promise.all([
        api.listAnnotations(domain, dataset),
        probeExternal(),
      ])
      const pending = (notes || []).filter(
        (n) => n.status === "open" || n.status === "in_review"
      )
      // Default to the dataset scope unless ONLY cross notes are pending — then
      // land on the first pair that has one.
      let scope = "dataset"
      if (!notesInScope(pending, "dataset").length) {
        const firstPair = pending.map(externalPairOf).find(Boolean)
        if (firstPair) scope = `cross:${firstPair}`
      }
      setAnnoNotes(pending)
      setAnnoScope(scope)
      setAnnoSelected(seedAnnoSelection(pending, scope))
    } catch (err) {
      setAnnoOpen(false)
      toast.error(`Could not load annotations: ${err.message || err}`)
    } finally {
      setAnnoLoading(false)
    }
  }
  // Switching scope re-seeds the selection to "all in that scope" — the common
  // case; unticking is the exception.
  const changeAnnoScope = (scope) => {
    setAnnoScope(scope)
    setAnnoSelected(seedAnnoSelection(annoNotes, scope))
  }
  const toggleAnnoNote = (id) => {
    setAnnoSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const applyPickedAnnotations = async () => {
    const ids = notesInScope(annoNotes, annoScope)
      .filter((n) => annoSelected.has(n.annotation_id))
      .map((n) => n.annotation_id)
    // The wire scope stays "dataset" | "cross" (CONVENTIONS); the pair rides as
    // cross_target so the run's SQL permissions cover the target even when the
    // selection is only general notes (whose ids name no pair).
    const pair =
      annoScope === "dataset" ? null : annoScope.slice("cross:".length)
    await startAnnotationHarvest(pair ? "cross" : "dataset", ids, pair)
    setAnnoOpen(false)
  }

  // Cross-dataset harvest (mode="cross"): documents the relationship between
  // this dataset and the picked target into THIS bundle's external/ folder
  // (one home — the target gets a derived cross-reference signal, never a
  // copy). The server validates the target (registered, glue-backed, a
  // different Glue database, bundle ready).
  const startCrossHarvest = async () => {
    if (!hasSelection || !crossTarget) return
    const [targetDomain, targetDataset] = crossTarget.split("/")
    setStartingCross(true)
    try {
      await api.startHarvest(
        domain,
        dataset,
        "cross",
        model,
        effort,
        subModel,
        subModel ? subEffort : "",
        revModel,
        revModel ? revEffort : "",
        { target: { dataDomain: targetDomain, dataset: targetDataset } }
      )
      toast.success(
        `Cross-dataset discovery queued: ${domain}/${dataset} × ${crossTarget}`
      )
      setCrossOpen(false)
      resetFeed()
      await poll()
      startPolling()
      startFeed()
    } catch (err) {
      const msg = err.message || String(err)
      toast.error(
        /-> 409/.test(msg)
          ? `Could not start: ${msg.split(": ").slice(1).join(": ") || "a harvest is already running."}`
          : `Could not start cross-dataset discovery: ${msg}`
      )
    } finally {
      setStartingCross(false)
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

  // Cross-target candidates: every registered dataset except this one. v1 is
  // Glue↔Glue only (verification is qualified Athena SQL), so non-glue mappings
  // are filtered out; a legacy row with no source descriptor is glue by
  // convention. Readiness is validated server-side (the picker can't know it).
  const crossCandidates = datasets.filter(
    (d) =>
      !(d.data_domain === domain && d.dataset === dataset) &&
      (d.source?.type ?? "glue") === "glue"
  )

  // Annotation picker derivations: the notes in the ACTIVE scope + selection
  // stats. One cross scope PER TARGET PAIR — from the bundle's external/ docs
  // and from pending notes (belt and braces — a note can outlive its docs).
  const visibleAnno = notesInScope(annoNotes, annoScope)
  const annoSelectedCount = visibleAnno.filter((n) =>
    annoSelected.has(n.annotation_id)
  ).length
  const allAnnoSelected =
    visibleAnno.length > 0 && annoSelectedCount === visibleAnno.length
  const crossScopePairs = [
    ...new Set([
      ...externalPairs,
      ...annoNotes.map(externalPairOf).filter(Boolean),
    ]),
  ].sort()
  const showCrossScope = crossScopePairs.length > 0
  // A dataset-scoped run with nothing ticked still fires when the guidance is
  // dirty — applying the updated instructions is its own reason to run.
  const guidanceOnlyRun =
    annoScope === "dataset" &&
    !!guidance?.guidance_dirty &&
    annoSelectedCount === 0
  const toggleAllAnno = () => {
    setAnnoSelected((prev) => {
      const next = new Set(prev)
      if (allAnnoSelected) visibleAnno.forEach((n) => next.delete(n.annotation_id))
      else visibleAnno.forEach((n) => next.add(n.annotation_id))
      return next
    })
  }
  // The reindex-derived cross-reference signal for THIS dataset (see
  // CONVENTIONS.md): pairs we hold docs for, and datasets whose own bundle
  // documents a relationship to us (their docs, one hop away — the pair docs
  // live only in the initiating bundle).
  const selfMapping = datasets.find(
    (d) => d.data_domain === domain && d.dataset === dataset
  )
  const crossReferences = selfMapping?.cross_references || []
  const crossReferencedBy = selfMapping?.cross_referenced_by || []

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
          <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
            <Button
              variant="outline"
              size="icon"
              onClick={() => setSettingsOpen(true)}
              title="Harvest settings (model + dataset guidance)"
              aria-label="Harvest settings"
              // Always clickable — dataset guidance lives in the panel now and
              // must stay editable mid-run; only the model selects lock inside.
              className="relative"
            >
              <SlidersHorizontalIcon className="size-4" />
              {guidance?.guidance_dirty ? (
                // Guidance edited but not yet applied — surface the pending
                // state on the (otherwise opaque) settings button.
                <span
                  className="absolute -right-0.5 -top-0.5 size-2 rounded-full bg-amber-500"
                  aria-hidden="true"
                />
              ) : null}
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
                      onSelect={openAnnotationPicker}
                      disabled={startingAnnotations}
                      className="flex-col items-start gap-0.5"
                    >
                      <span className="flex items-center gap-2">
                        <MessageSquareTextIcon />
                        Apply annotations…
                        {guidance?.guidance_dirty ? " + guidance" : ""}
                      </span>
                      <span className="pl-6 text-[11px] text-muted-foreground">
                        {guidance?.guidance_dirty
                          ? "In-place: pick which notes to apply, plus the updated guidance."
                          : "In-place: pick which of your open notes to apply."}
                      </span>
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onSelect={() => setCrossOpen(true)}
                      disabled={startingCross}
                      className="flex-col items-start gap-0.5"
                    >
                      <span className="flex items-center gap-2">
                        <Link2Icon />
                        Cross-dataset discovery…
                      </span>
                      <span className="pl-6 text-[11px] text-muted-foreground">
                        Discover + verify relationships with another dataset.
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
          <HarvestSettingsSheet
            open={settingsOpen}
            onOpenChange={setSettingsOpen}
            model={model}
            effort={effort}
            onModelChange={onModelChange}
            onEffortChange={setEffort}
            subModel={subModel}
            subEffort={subEffort}
            onSubModelChange={onSubModelChange}
            onSubEffortChange={setSubEffort}
            revModel={revModel}
            revEffort={revEffort}
            onRevModelChange={onRevModelChange}
            onRevEffortChange={setRevEffort}
            locked={running || starting}
            guidance={guidance}
            guidanceDraft={guidanceDraft}
            onGuidanceDraftChange={setGuidanceDraft}
            guidanceLoading={guidanceLoading}
            savingGuidance={savingGuidance}
            onSaveGuidance={saveGuidance}
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
                  edits
                  {hasExternal
                    ? ", and its cross-dataset references (external/) — re-run" +
                      " a cross-dataset discovery afterwards to restore them"
                    : ""}
                  . This can't be undone. To apply new feedback without a
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
          {/* Cross-dataset target picker. Rendered at CardHeader level (a
              sibling of the trigger, like the confirm dialog above) — nesting
              a Dialog inside the DropdownMenuItem would bubble its close click
              back into the menu. */}
          <Dialog open={crossOpen} onOpenChange={setCrossOpen}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Cross-dataset discovery</DialogTitle>
                <DialogDescription>
                  Explore one other dataset for relationships with{" "}
                  <span className="font-medium text-foreground">
                    {domain}/{dataset}
                  </span>
                  , verify them against live data, and write the resulting
                  join/metric docs into{" "}
                  <span className="font-medium">this dataset's external/</span>{" "}
                  folder. The target's listing gains a "referenced by" signal
                  pointing here — nothing is written into its bundle. Both
                  datasets need a published bundle; a full harvest of{" "}
                  <span className="font-medium">this</span> dataset removes
                  its external/ docs (re-run the cross harvest to restore
                  them).
                </DialogDescription>
              </DialogHeader>
              <div className="flex flex-col gap-2">
                <Label htmlFor="cross-target">Target dataset</Label>
                {/* A searchable list, not a plain dropdown: real catalogs run
                    to thousands of datasets. cmdk filters as you type. */}
                <Command className="rounded-2xl border">
                  <CommandInput
                    id="cross-target"
                    placeholder="Search datasets…"
                  />
                  <CommandList className="max-h-52">
                    <CommandEmpty>No datasets match.</CommandEmpty>
                    <CommandGroup>
                      {crossCandidates.map((d) => {
                        const id = `${d.data_domain}/${d.dataset}`
                        // Already documented (either direction) — re-running
                        // replaces that pair's docs.
                        const done =
                          crossReferences.includes(id) ||
                          crossReferencedBy.includes(id)
                        return (
                          <CommandItem
                            key={id}
                            value={id}
                            onSelect={() =>
                              setCrossTarget(crossTarget === id ? "" : id)
                            }
                          >
                            <CheckIcon
                              className={cn(
                                "size-4",
                                crossTarget === id
                                  ? "opacity-100"
                                  : "opacity-0"
                              )}
                            />
                            <span className="min-w-0 truncate">{id}</span>
                            {done ? (
                              <span className="ml-auto text-xs text-muted-foreground">
                                documented
                              </span>
                            ) : null}
                          </CommandItem>
                        )
                      })}
                    </CommandGroup>
                  </CommandList>
                </Command>
                {crossCandidates.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    No other Glue-backed datasets are registered yet — map one
                    first (cross-references are Glue↔Glue for now).
                  </p>
                ) : null}
              </div>
              <DialogFooter>
                <DialogClose asChild>
                  <Button variant="outline">Cancel</Button>
                </DialogClose>
                <Button
                  onClick={startCrossHarvest}
                  disabled={!crossTarget || startingCross}
                >
                  {startingCross ? (
                    <Spinner />
                  ) : (
                    <PlayIcon data-icon="inline-start" />
                  )}
                  Start discovery
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
          {/* Annotation picker — scope dropdown + per-note checkboxes so a run
              can apply a PARTIAL selection. Always shown; the cross scope is
              offered only when external/ docs (or notes on them) exist. */}
          <Dialog open={annoOpen} onOpenChange={setAnnoOpen}>
            <DialogContent className="sm:max-w-lg">
              <DialogHeader>
                <DialogTitle>Apply annotations</DialogTitle>
                <DialogDescription>
                  Pick which of your pending notes on{" "}
                  <span className="font-medium text-foreground">
                    {domain}/{dataset}
                  </span>{" "}
                  this run should apply.
                  {showCrossScope
                    ? " Notes on cross-dataset docs are verified against the" +
                      " target dataset's data — each target is its own scope," +
                      " and the run's SQL access is widened to that target" +
                      " only."
                    : ""}
                </DialogDescription>
              </DialogHeader>
              {annoLoading ? (
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-8 w-44" />
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                </div>
              ) : (
                // min-w-0: DialogContent is a grid, and a grid item's default
                // min-width:auto lets a nowrap child (the truncating quote
                // line) blow the column past the dialog's max-w — the same
                // overflow as the mapping select. Shrinkable item = real
                // truncation.
                <div className="flex min-h-0 min-w-0 flex-col gap-3">
                  <div className="flex flex-col gap-2">
                    <Label htmlFor="anno-scope">Scope</Label>
                    <Select value={annoScope} onValueChange={changeAnnoScope}>
                      <SelectTrigger id="anno-scope" className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent
                        position="popper"
                        className="max-w-(--radix-select-trigger-width)"
                      >
                        <SelectGroup>
                          <SelectItem value="dataset">
                            <DatabaseIcon className="size-4" />
                            Dataset docs
                          </SelectItem>
                          {/* One scope per cross-dataset TARGET: the pair names
                              exactly which dataset the run verifies against. */}
                          {crossScopePairs.map((pair) => (
                            <SelectItem key={pair} value={`cross:${pair}`}>
                              <Link2Icon className="size-4" />
                              <span className="min-w-0 truncate">
                                Cross-dataset · {pair}
                              </span>
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">
                      {annoSelectedCount} of {visibleAnno.length} selected
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={toggleAllAnno}
                      disabled={!visibleAnno.length}
                    >
                      {allAnnoSelected ? "Unselect all" : "Select all"}
                    </Button>
                  </div>
                  {visibleAnno.length ? (
                    <div className="flex max-h-64 flex-col gap-1.5 overflow-y-auto pr-1">
                      {visibleAnno.map((n) => (
                        <label
                          key={n.annotation_id}
                          className="flex min-w-0 cursor-pointer items-start gap-2.5 overflow-hidden rounded-xl border px-3 py-2 transition-colors hover:bg-muted/50"
                        >
                          <input
                            type="checkbox"
                            className="mt-0.5 size-3.5 shrink-0 accent-primary"
                            checked={annoSelected.has(n.annotation_id)}
                            onChange={() => toggleAnnoNote(n.annotation_id)}
                          />
                          <span className="flex min-w-0 flex-col gap-0.5">
                            <span className="text-sm leading-snug break-words">
                              {n.note}
                            </span>
                            <span className="truncate font-mono text-[11px] text-muted-foreground">
                              {n.concept_id === "_dataset"
                                ? "general note"
                                : n.concept_id}
                              {n.quote ? ` · “${n.quote}”` : ""}
                            </span>
                          </span>
                        </label>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      {annoScope !== "dataset"
                        ? `No pending notes on the ${annoScope.slice("cross:".length)} cross-dataset docs.`
                        : guidance?.guidance_dirty
                          ? "No pending notes on this dataset's docs — the run applies the updated guidance only."
                          : "No pending notes on this dataset's docs."}
                    </p>
                  )}
                  {annoScope === "dataset" &&
                  guidance?.guidance_dirty &&
                  visibleAnno.length ? (
                    <p className="text-xs text-muted-foreground">
                      The updated dataset guidance rides this run too.
                    </p>
                  ) : null}
                </div>
              )}
              <DialogFooter>
                <DialogClose asChild>
                  <Button variant="outline">Cancel</Button>
                </DialogClose>
                <Button
                  onClick={applyPickedAnnotations}
                  disabled={
                    annoLoading ||
                    startingAnnotations ||
                    (annoSelectedCount === 0 && !guidanceOnlyRun)
                  }
                >
                  {startingAnnotations ? (
                    <Spinner />
                  ) : (
                    <PlayIcon data-icon="inline-start" />
                  )}
                  {annoSelectedCount
                    ? `Apply ${annoSelectedCount} note${annoSelectedCount === 1 ? "" : "s"}`
                    : guidanceOnlyRun
                      ? "Apply guidance"
                      : "Apply"}
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

              {crossReferences.length || crossReferencedBy.length ? (
                // The reindex-derived cross-reference signal. Pair docs live in
                // the bundle that authored them, so "referenced by" points at
                // ANOTHER dataset's external/ folder — worth showing here, since
                // nothing in this bundle reveals it.
                <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                  {crossReferences.length ? (
                    <span className="flex flex-wrap items-center gap-1.5">
                      <Link2Icon className="size-3.5" />
                      Cross-references
                      {crossReferences.map((id) => (
                        <Badge key={id} variant="secondary">
                          {id}
                        </Badge>
                      ))}
                    </span>
                  ) : null}
                  {crossReferencedBy.length ? (
                    <span className="flex flex-wrap items-center gap-1.5">
                      <Link2Icon className="size-3.5" />
                      Referenced by
                      {crossReferencedBy.map((id) => (
                        <Badge key={id} variant="outline">
                          {id}
                        </Badge>
                      ))}
                      <span>(their bundle holds the pair docs)</span>
                    </span>
                  ) : null}
                </div>
              ) : null}

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
                      <dd className="break-words">
                        {MODE_LABEL[inner.mode] || inner.mode}
                      </dd>
                    </div>
                  )}
                  {inner.cross_target && (
                    // Cross-dataset discovery: WHO the run is against (stamped
                    // on the status row at trigger time).
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">Target</dt>
                      <dd className="break-words">{inner.cross_target}</dd>
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
                  {inner.subagent_model && (
                    // Present only when the run chose a separate sub-agent
                    // config; absent means the sub-agents ran on the model above.
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">
                        Sub-agent model
                      </dt>
                      <dd className="break-words">
                        {entryFor(inner.subagent_model)?.label ||
                          inner.subagent_model}
                        {inner.subagent_effort
                          ? ` / ${inner.subagent_effort}`
                          : ""}
                      </dd>
                    </div>
                  )}
                  {inner.reviewer_model && (
                    // Present only when the run gave the adversarial reviewer
                    // its own config; absent = same as the sub-agents.
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">
                        Reviewer model
                      </dt>
                      <dd className="break-words">
                        {entryFor(inner.reviewer_model)?.label ||
                          inner.reviewer_model}
                        {inner.reviewer_effort
                          ? ` / ${inner.reviewer_effort}`
                          : ""}
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
                    api={api}
                    domain={domain}
                    dataset={dataset}
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

function BenchmarkRow({ row, api, domain, dataset }) {
  const [open, setOpen] = useState(false)
  const iterLabel = `Round ${(row.iteration ?? 0) + 1}${
    row.max_iterations ? `/${row.max_iterations}` : ""
  }`
  const phase = row.phase || "solving"
  const total = row.total || 0
  const current = row.current || 0
  const pct =
    total > 0 ? Math.round((current / total) * 100) : row.done ? 100 : 0
  // A completed round is clickable iff the runtime persisted a review artifact
  // (has_review) and we know the session id + which round to fetch.
  const canReview =
    row.done &&
    Boolean(row.has_review) &&
    Boolean(api && domain && dataset && row.runtime_session_id)

  return (
    <>
      <div
        className={cn(
          "flex flex-col gap-1.5 rounded-md border px-2 py-1.5 text-sm",
          canReview && "cursor-pointer hover:bg-muted/50"
        )}
        {...(canReview
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
            variant={row.target_met ? "default" : "outline"}
            className="shrink-0"
          >
            {row.target_met ? "Target met" : "Below target"}
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
          {canReview ? (
            <span className="text-primary">· click to review answers</span>
          ) : null}
        </div>
      )}
      </div>
      {/* Dialog is a SIBLING of the clickable row (not a child): React events
          bubble up the component tree even from a portal, so nesting it inside the
          row made the X-button click re-trigger the row's onClick and reopen it. */}
      {canReview ? (
        <BenchmarkReviewDialog
          open={open}
          onOpenChange={setOpen}
          api={api}
          domain={domain}
          dataset={dataset}
          session={row.runtime_session_id}
          iteration={row.iteration ?? 0}
          iterLabel={iterLabel}
        />
      ) : null}
    </>
  )
}

// The classification tabs shown in the review modal, in reading order. Keys match
// the persisted review buckets (harvest tool.py BUCKET_* / adjudicator categories).
const REVIEW_TABS = [
  { key: "passed", label: "Passed" },
  { key: "genuine_error", label: "Genuine gaps" },
  { key: "noisy_gold", label: "Noisy gold" },
  { key: "ambiguous", label: "Ambiguous" },
  { key: "unknown", label: "Unclassified" },
  { key: "discarded", label: "Discarded" },
]

const REVIEW_TAB_HELP = {
  passed: "The wiki led to SQL that matched the expected answer.",
  genuine_error:
    "A real wiki gap: the reviewer confirmed the docs were missing or wrong about something the answer needed. These drive the improvements.",
  noisy_gold:
    "The expected answer itself looks wrong against the data, so the wiki isn't at fault. Dropped from later rounds.",
  ambiguous:
    "The question is under-specified (or the fact was already documented) — not a wiki gap. Dropped from later rounds.",
  unknown:
    "The reviewer couldn't reach a verdict (errored or unparseable). Counts against the wiki until resolved.",
  discarded:
    "The expected SQL couldn't run against the data, so the question is unanswerable and excluded from the score.",
}

// The full per-question review for one round, grouped into classification tabs.
// Fetches the off-mount review JSON (gold-carrying, human-facing) on open — this
// detail never reaches the harvest agent, only the human here.
function BenchmarkReviewDialog({
  open,
  onOpenChange,
  api,
  domain,
  dataset,
  session,
  iteration,
  iterLabel,
}) {
  const [state, setState] = useState({ status: "idle", doc: null, error: null })

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setState({ status: "loading", doc: null, error: null })
    api
      .getBenchmarkReview(domain, dataset, session, iteration)
      .then((doc) => {
        if (!cancelled) setState({ status: "ok", doc, error: null })
      })
      .catch((e) => {
        if (!cancelled)
          setState({ status: "error", doc: null, error: e.message || String(e) })
      })
    return () => {
      cancelled = true
    }
  }, [open, api, domain, dataset, session, iteration])

  const doc = state.doc
  const counts = doc?.counts || {}
  const questions = Array.isArray(doc?.questions) ? doc.questions : []
  // Default to the first tab that has any questions (so the modal doesn't open on
  // an empty tab); fall back to "passed".
  const firstNonEmpty =
    REVIEW_TABS.find((t) => (counts[t.key] || 0) > 0)?.key || "passed"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <GaugeIcon className="size-4" />
            Benchmark review — {iterLabel}
          </DialogTitle>
          <DialogDescription>
            Every question this round, grouped by what the reviewer decided. This
            detail (including the expected SQL) is shown only here — the harvester
            never sees it.
          </DialogDescription>
        </DialogHeader>

        {state.status === "loading" ? (
          <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
            <Spinner className="size-4" /> Loading review…
          </div>
        ) : state.status === "error" ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn’t load the review</AlertTitle>
            <AlertDescription>{state.error}</AlertDescription>
          </Alert>
        ) : (
          <Tabs defaultValue={firstNonEmpty} className="min-w-0">
            <TabsList className="flex-wrap">
              {REVIEW_TABS.map((t) => (
                <TabsTrigger key={t.key} value={t.key} className="gap-1.5">
                  {t.label}
                  <span className="rounded bg-muted px-1 text-xs tabular-nums text-muted-foreground">
                    {counts[t.key] || 0}
                  </span>
                </TabsTrigger>
              ))}
            </TabsList>
            {REVIEW_TABS.map((t) => (
              <TabsContent key={t.key} value={t.key} className="min-w-0">
                <p className="mb-2 text-xs text-muted-foreground">
                  {REVIEW_TAB_HELP[t.key]}
                </p>
                {/* Native max-height + overflow scroll (NOT Radix ScrollArea): its
                    viewport's height:100% needs a definite parent height, which a
                    TabsContent block doesn't give — so it grows to full content
                    height and overflows the dialog instead of scrolling. */}
                <div className="okf-thin-scroll flex max-h-[60vh] min-w-0 flex-col gap-3 overflow-y-auto pr-3">
                  {questions.filter((q) => q.bucket === t.key).length ? (
                    questions
                      .filter((q) => q.bucket === t.key)
                      .map((q) => <ReviewQuestion key={q.q_id} q={q} />)
                  ) : (
                    <p className="py-6 text-center text-sm text-muted-foreground">
                      No questions in this category.
                    </p>
                  )}
                </div>
              </TabsContent>
            ))}
          </Tabs>
        )}
      </DialogContent>
    </Dialog>
  )
}

// One question card in the review modal: the question, the reviewer's note (if
// any), and the predicted vs expected SQL.
function ReviewQuestion({ q }) {
  return (
    <div className="min-w-0 rounded-md border p-3">
      <p className="text-sm font-medium break-words">{q.question || "—"}</p>
      {q.note ? (
        <p className="mt-1 text-xs text-muted-foreground break-words">
          Reviewer: {q.note}
        </p>
      ) : q.reason ? (
        <p className="mt-1 text-xs text-muted-foreground break-words">
          {q.reason}
        </p>
      ) : null}
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <ReviewSql label="Wiki-derived answer" sql={q.predicted_sql} />
        <ReviewSql label="Expected answer" sql={q.gold_sql} />
      </div>
    </div>
  )
}

function ReviewSql({ label, sql }) {
  const source = typeof sql === "string" ? sql.trim() : ""
  // Highlight with the SAME highlight.js + `.okf-prose .hljs-*` theme the chat
  // CodeView and concept docs use, so SQL colors are consistent app-wide. Force
  // the `sql` grammar (these are always SQL); fall back to escaped text on error.
  const html = useMemo(() => {
    if (!source) return ""
    try {
      return hljs.highlight(source, { language: "sql" }).value
    } catch {
      const div = document.createElement("div")
      div.textContent = source
      return div.innerHTML
    }
  }, [source])

  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
          {label}
        </span>
        {source ? (
          <CopyButton
            text={source}
            label={`Copy ${label}`}
            className="size-6 shrink-0"
          />
        ) : null}
      </div>
      <pre className="min-w-0 overflow-x-auto rounded border bg-muted p-2 text-xs whitespace-pre-wrap break-words">
        {source ? (
          <code
            className="hljs okf-prose bg-transparent p-0"
            dangerouslySetInnerHTML={{ __html: html }}
          />
        ) : (
          "—"
        )}
      </pre>
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

// Parse a benchmark-feedback agent message: the `run_benchmark` tool returns an
// `improvements` list and the agent echoes it back as JSON (raw, or in a ```json
// fence, possibly wrapped in prose). Returns the improvement strings when the
// message IS that feedback, else null so a normal agent message renders as-is.
// This is what lets us show a readable list + a "Benchmark feedback" title
// instead of dumping raw JSON in the modal.
function parseBenchmarkImprovements(text) {
  if (!text || typeof text !== "string" || !text.includes("improvements")) {
    return null
  }
  // Try, in order: a ```json fence's body, the whole string, and the span from
  // the first "{" to the last "}" (JSON embedded in surrounding prose).
  const candidates = []
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i)
  if (fenced) candidates.push(fenced[1])
  candidates.push(text)
  const first = text.indexOf("{")
  const last = text.lastIndexOf("}")
  if (first !== -1 && last > first) candidates.push(text.slice(first, last + 1))
  for (const c of candidates) {
    try {
      const obj = JSON.parse(c.trim())
      if (obj && Array.isArray(obj.improvements)) {
        return obj.improvements.map((s) => String(s).trim()).filter(Boolean)
      }
    } catch {
      // not valid JSON — try the next candidate
    }
  }
  return null
}

// The full agent message in a modal. For benchmark feedback (an `improvements`
// list) it renders a readable, numbered list titled "Benchmark feedback"; any
// other agent message renders as GFM markdown titled "Agent message". Opened by
// clicking a feed row whose text was trimmed to fit one line.
function AgentMessageDialog({ open, onOpenChange, text, improvements }) {
  const isBench = Array.isArray(improvements)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {isBench ? (
              <GaugeIcon className="size-4" />
            ) : (
              <SparklesIcon className="size-4" />
            )}
            {isBench ? "Benchmark feedback" : "Agent message"}
          </DialogTitle>
          {isBench ? (
            <DialogDescription>
              {improvements.length
                ? "Wiki gaps this benchmark round surfaced. The agent revises the docs to address these, then re-benchmarks."
                : "This benchmark round found no wiki gaps to address."}
            </DialogDescription>
          ) : null}
        </DialogHeader>
        <ScrollArea className="max-h-[70vh] min-w-0">
          {isBench ? (
            improvements.length ? (
              <ol className="min-w-0 space-y-2.5 pr-3">
                {improvements.map((item, i) => (
                  <li key={i} className="flex gap-3">
                    <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-medium text-primary tabular-nums">
                      {i + 1}
                    </span>
                    <span className="min-w-0 flex-1 text-sm leading-relaxed break-words">
                      {item}
                    </span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="pr-3 text-sm text-muted-foreground">
                No improvements — the wiki answered this round without gaps.
              </p>
            )
          ) : (
            // Long fenced code/JSON a plain agent message emits would blow past
            // the modal: a <pre> inside Radix's ScrollArea (a display:table
            // viewport child) in a grid DialogContent never engages
            // overflow-x-auto — its intrinsic width just grows. Wrap code lines
            // instead so content always stays within the modal.
            <div className="okf-prose min-w-0 pr-3 [&_code]:break-words [&_pre]:break-words [&_pre]:whitespace-pre-wrap">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                rehypePlugins={[
                  [rehypeHighlight, { detect: true, ignoreMissing: true }],
                ]}
              >
                {text}
              </ReactMarkdown>
            </div>
          )}
        </ScrollArea>
      </DialogContent>
    </Dialog>
  )
}

// Per-harvest settings panel — a FLOATING right sheet (inset + rounded, like
// the app's floating sidebar) merging the model/effort picker with the dataset
// guidance editor. Model options come from the Terraform-provided catalog
// (MODEL_CATALOG); the effort list is model-specific and re-derived each render.
// Guidance stays editable even mid-run (`locked` only freezes model/effort,
// which are read at harvest start): edit + Save persists it (goes DIRTY); the
// next full harvest, or an "Apply annotations + guidance" re-harvest, applies
// it and clears dirty.
// Sentinel for the sub-agent model Select's "same as harvester" item — Radix
// forbids an empty-string item value, so "" (the state/preference encoding of
// "same") maps to/from this sentinel at the Select boundary only.
const SAME_AS_HARVESTER = "__same__"
const SAME_AS_SUBAGENTS = "__same_sub__"

// The model options shared by the three pickers: one labelled group per
// provider family, most capable first (see GROUPED_MODEL_CATALOG).
function ModelOptions() {
  return GROUPED_MODEL_CATALOG.map((g) => (
    <SelectGroup key={g.family}>
      <SelectLabel>{g.family}</SelectLabel>
      {g.models.map((m) => (
        <SelectItem key={m.model} value={m.model}>
          {m.label}
        </SelectItem>
      ))}
    </SelectGroup>
  ))
}

function HarvestSettingsSheet({
  open,
  onOpenChange,
  model,
  effort,
  onModelChange,
  onEffortChange,
  subModel,
  subEffort,
  onSubModelChange,
  onSubEffortChange,
  revModel,
  revEffort,
  onRevModelChange,
  onRevEffortChange,
  locked,
  guidance,
  guidanceDraft,
  onGuidanceDraftChange,
  guidanceLoading,
  savingGuidance,
  onSaveGuidance,
}) {
  const efforts = effortsFor(model)
  const subEfforts = effortsFor(subModel)
  const revEfforts = effortsFor(revModel)
  // The Select dropdown portals OUTSIDE the sheet, so clicking an item (or the
  // dropdown's own outside-click) looks like an "interact outside" and would
  // close the whole panel. Ignore dismissal events that originate from a Select
  // popup so only a real outside click closes it.
  const keepOpenOnSelectInteraction = (e) => {
    const t = e.target
    if (t instanceof Element && t.closest("[data-slot='select-content']")) {
      e.preventDefault()
    }
  }
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        // Floating mode: inset from the viewport edges with rounded corners
        // (data-side variants so they win over the flush inset-y-0/right-0 the
        // base sheet sets for side="right").
        className="rounded-2xl border shadow-lg data-[side=right]:inset-y-3 data-[side=right]:right-3 data-[side=right]:h-auto data-[side=right]:sm:max-w-md"
        onPointerDownOutside={keepOpenOnSelectInteraction}
        onInteractOutside={keepOpenOnSelectInteraction}
      >
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <SlidersHorizontalIcon className="size-4" />
            Harvest settings
          </SheetTitle>
          <SheetDescription>
            Model and reasoning effort for the next harvest.
          </SheetDescription>
        </SheetHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 pb-6">
          <span className="text-sm font-medium">Harvester</span>
          <div className="flex flex-col gap-2">
            <Label htmlFor="harvest-model">Model</Label>
            <Select value={model} onValueChange={onModelChange} disabled={locked}>
              <SelectTrigger id="harvest-model" className="w-full">
                <SelectValue placeholder="Select a model..." />
              </SelectTrigger>
              <SelectContent>
                <ModelOptions />
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="harvest-effort">Reasoning effort</Label>
            <Select value={effort} onValueChange={onEffortChange} disabled={locked}>
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

          <span className="text-sm font-medium">Sub-agents</span>
          <p className="-mt-3 text-xs text-muted-foreground">
            The table authors, reference authors, reviewers, and the benchmark
            solver.
          </p>
          <div className="flex flex-col gap-2">
            <Label htmlFor="harvest-sub-model">Model</Label>
            <Select
              value={subModel || SAME_AS_HARVESTER}
              onValueChange={(v) =>
                onSubModelChange(v === SAME_AS_HARVESTER ? "" : v)
              }
              disabled={locked}
            >
              <SelectTrigger id="harvest-sub-model" className="w-full">
                <SelectValue placeholder="Select a model..." />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SAME_AS_HARVESTER}>
                  Same as harvester
                </SelectItem>
                <SelectSeparator />
                <ModelOptions />
              </SelectContent>
            </Select>
          </div>
          {subModel ? (
            <div className="flex flex-col gap-2">
              <Label htmlFor="harvest-sub-effort">Reasoning effort</Label>
              <Select
                value={subEffort}
                onValueChange={onSubEffortChange}
                disabled={locked}
              >
                <SelectTrigger id="harvest-sub-effort" className="w-full">
                  <SelectValue placeholder="Select an effort..." />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {subEfforts.map((e) => (
                      <SelectItem key={e} value={e}>
                        {e}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </div>
          ) : null}
          <span className="text-sm font-medium">Adversarial reviewer</span>
          <p className="-mt-3 text-xs text-muted-foreground">
            The review pass that verifies the authored docs.
          </p>
          <div className="flex flex-col gap-2">
            <Label htmlFor="harvest-rev-model">Model</Label>
            <Select
              value={revModel || SAME_AS_SUBAGENTS}
              onValueChange={(v) =>
                onRevModelChange(v === SAME_AS_SUBAGENTS ? "" : v)
              }
              disabled={locked}
            >
              <SelectTrigger id="harvest-rev-model" className="w-full">
                <SelectValue placeholder="Select a model..." />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SAME_AS_SUBAGENTS}>
                  Same as sub-agents
                </SelectItem>
                <SelectSeparator />
                <ModelOptions />
              </SelectContent>
            </Select>
          </div>
          {revModel ? (
            <div className="flex flex-col gap-2">
              <Label htmlFor="harvest-rev-effort">Reasoning effort</Label>
              <Select
                value={revEffort}
                onValueChange={onRevEffortChange}
                disabled={locked}
              >
                <SelectTrigger id="harvest-rev-effort" className="w-full">
                  <SelectValue placeholder="Select an effort..." />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {revEfforts.map((e) => (
                      <SelectItem key={e} value={e}>
                        {e}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </div>
          ) : null}
          {locked ? (
            <p className="text-xs text-muted-foreground">
              A harvest is in flight — model and effort unlock when it finishes.
            </p>
          ) : null}

          <Separator />

          <div className="flex min-h-0 flex-col gap-2">
            <div className="flex items-center gap-2">
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
            <p className="text-xs text-muted-foreground">
              Shared authoring instructions applied by the next harvest
            </p>
            {guidanceLoading ? (
              <Skeleton className="h-32 w-full" />
            ) : (
              <>
                <Textarea
                  value={guidanceDraft}
                  onChange={(e) => onGuidanceDraftChange(e.target.value)}
                  placeholder="Add dataset-specific authoring guidance…"
                  className="min-h-40 resize-y text-sm"
                  disabled={savingGuidance}
                />
                <div className="flex items-center justify-end">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={onSaveGuidance}
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
        </div>
      </SheetContent>
    </Sheet>
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

  const modalText = step.full || step.label
  // Benchmark feedback arrives as an agent message whose body is the
  // `run_benchmark` tool's `improvements` JSON. Detect it so we can tag the row
  // "Benchmark feedback", show a readable inline summary (not raw JSON), and
  // render a clean list in the modal.
  const improvements = isAgent ? parseBenchmarkImprovements(modalText) : null
  const isBench = Array.isArray(improvements)
  // A benchmark-feedback row is ALWAYS clickable (the readable list lives in the
  // modal); a plain agent row is clickable only when trimmed/overflowing.
  const expandable = isBench || (isAgent && (Boolean(step.full) || overflowing))

  // Icon: benchmark feedback -> gauge; agent -> sparkles; tool -> tool-specific,
  // swapping to a check/cross once the result lands so completion reads at a glance.
  let Icon
  if (isBench) {
    Icon = GaugeIcon
  } else if (isAgent) {
    Icon = SparklesIcon
  } else if (step.state === "ok") {
    Icon = CheckCircle2Icon
  } else if (step.state === "failed") {
    Icon = XCircleIcon
  } else {
    Icon = toolIcon(step.tool)
  }

  // The inline row text: benchmark feedback gets a plain-language summary instead
  // of the raw JSON blob; other agent/tool rows show their label as before.
  const benchSummary = isBench
    ? improvements.length
      ? `Benchmark feedback — ${improvements.length} wiki improvement${
          improvements.length === 1 ? "" : "s"
        } to address`
      : "Benchmark feedback — no wiki gaps found"
    : null

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
        {isBench ? (
          <Badge
            variant="secondary"
            className="shrink-0 justify-center whitespace-nowrap"
          >
            Benchmark
          </Badge>
        ) : (
          <Badge
            variant={isAgent ? "secondary" : "outline"}
            className={cn(LABEL_CELL, "justify-center")}
          >
            {isAgent ? "Agent" : "Tool"}
          </Badge>
        )}
        <span ref={labelRef} className="min-w-0 flex-1 truncate">
          {isBench ? (
            benchSummary
          ) : isAgent ? (
            <InlineMarkdown text={step.label} />
          ) : (
            step.label
          )}
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
          improvements={improvements}
        />
      ) : null}
    </>
  )
}

// Scope palette for the drill-down: the distribution bar's segments and each
// row's swatch share a class, so the bar doubles as the legend. Order matters
// (it is the render + bar order).
const SCOPE_STYLES = [
  { key: "supervisor", label: "Harvester", swatch: "bg-primary" },
  { key: "subagents", label: "Sub-agents", swatch: "bg-primary/60" },
  { key: "reviewer", label: "Reviewers", swatch: "bg-primary/30" },
]

// One run-total stat tile (Input / Output). Compact value up front; the
// precise count rides the tooltip.
function UsageStat({ label, value }) {
  return (
    <div
      className="rounded-lg border bg-muted/40 px-3 py-2"
      title={`${value.toLocaleString()} tokens`}
    >
      <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </div>
      <div className="text-base font-semibold tabular-nums">
        {fmtTokens(value)}
      </div>
    </div>
  )
}

// One drill-down table row: swatch + scope name, then the four dimensions
// (In / Cache R / Cache W / Out) as aligned numeric cells — everything small
// (text-xs) so a whole scope fits on ONE line. Cache counts are a BREAKDOWN of
// input (Bedrock/LangChain's `input` already includes cache_read +
// cache_write) — the column placement between In and Out keeps them read as
// such, and a zero renders as a muted dash. Numbers are compact (fmtTokens);
// precise values ride the tooltip.
function ScopeRow({ label, counts, swatch }) {
  const input = counts?.input || 0
  const output = counts?.output || 0
  const cacheRead = counts?.cache_read || 0
  const cacheWrite = counts?.cache_write || 0
  const precise =
    `${label}: ${input.toLocaleString()} in ` +
    `(${cacheRead.toLocaleString()} cache read, ` +
    `${cacheWrite.toLocaleString()} cache written) / ` +
    `${output.toLocaleString()} out`
  const cell = (v) =>
    v ? (
      <span className="justify-self-end text-xs tabular-nums">
        {fmtTokens(v)}
      </span>
    ) : (
      <span className="justify-self-end text-xs text-muted-foreground/60">
        —
      </span>
    )
  return (
    <Fragment>
      <span className="flex min-w-0 items-center gap-1.5 text-xs" title={precise}>
        <span className={cn("size-1.5 shrink-0 rounded-full", swatch)} />
        <span className="truncate">{label}</span>
      </span>
      {cell(input)}
      {cell(cacheRead)}
      {cell(cacheWrite)}
      {cell(output)}
    </Fragment>
  )
}

// Running-total token chip shown in the feed header. `usage` is the latest
// cumulative snapshot ({input, output, cache_read, cache_write, total}), plus
// an optional `by` object splitting the same counters per metering scope
// ({supervisor, subagents, reviewer}). The popover shows the run totals as two
// Input/Output tiles (cache detail lives per scope), then the per-agent
// drill-down: a thin distribution bar (share of total per scope) whose
// segments match each scope row's swatch.
function UsagePill({ usage }) {
  const input = usage.input || 0
  const output = usage.output || 0
  const total = usage.total ?? input + output
  const by = usage.by
  const scopes = by
    ? SCOPE_STYLES.filter((s) => by[s.key]).map((s) => {
        const counts = by[s.key]
        return {
          ...s,
          counts,
          total: counts.total ?? (counts.input || 0) + (counts.output || 0),
        }
      })
    : []
  const barTotal = scopes.reduce((n, s) => n + s.total, 0)
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
      <PopoverContent align="end" className="w-80 gap-3">
        <PopoverHeader>
          <PopoverTitle className="flex items-center gap-1.5 text-sm">
            <CoinsIcon className="size-3.5" />
            Token usage
          </PopoverTitle>
          <PopoverDescription>Cumulative for this run.</PopoverDescription>
        </PopoverHeader>
        <div className="grid grid-cols-2 gap-2">
          <UsageStat label="Input" value={input} />
          <UsageStat label="Output" value={output} />
        </div>
        {scopes.length ? (
          <div className="flex flex-col gap-2">
            {barTotal > 0 ? (
              <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted">
                {scopes.map((s) =>
                  s.total > 0 ? (
                    <div
                      key={s.key}
                      className={s.swatch}
                      style={{ width: `${(s.total / barTotal) * 100}%` }}
                    />
                  ) : null
                )}
              </div>
            ) : null}
            {/* Aligned mini-table: one LINE per scope — label + the four
                dimensions (In / Cache R / Cache W / Out) as columns, so the
                drill-down scans vertically instead of reading as prose. */}
            <div className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto_auto] items-baseline gap-x-2.5 gap-y-1.5">
              <span />
              <span className="justify-self-end text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
                In
              </span>
              <span className="justify-self-end text-right text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
                Cache&nbsp;R
              </span>
              <span className="justify-self-end text-right text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
                Cache&nbsp;W
              </span>
              <span className="justify-self-end text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
                Out
              </span>
              {scopes.map((s) => (
                <ScopeRow
                  key={s.key}
                  label={s.label}
                  counts={s.counts}
                  swatch={s.swatch}
                />
              ))}
            </div>
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  )
}

// The scrollable, auto-following step feed, merged into the Harvest card. Snaps
// to the bottom on new events only when the user is already pinned there (so
// scrolling up to read history isn't yanked away). Uses a native overflow div —
// shadcn ScrollArea exposes no viewport ref, which the auto-follow needs.
function HarvestFeed({
  events,
  running,
  aborted,
  draining = false,
  api,
  domain,
  dataset,
}) {
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
            <BenchmarkRow
              key={`bench-${r.iteration}`}
              row={r}
              api={api}
              domain={domain}
              dataset={dataset}
            />
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
