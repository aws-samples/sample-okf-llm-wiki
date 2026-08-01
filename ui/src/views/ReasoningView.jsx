// Reasoning — per-dataset Automated Reasoning enrollment + policy transparency.
//
// Enrollment is OPT-IN (the account allows 100 policies total, so each one is a
// deliberate spend): a dataset starts unenrolled, can only enroll once a wiki
// exists (the policy is derived FROM the bundle), and unenrolling DELETES the
// policy, its guardrail, and the derived artifacts — confirmed first, since a
// re-enroll rebuilds from scratch. The rest of the page is transparency: which
// wiki files feed the policy, the rules the reasoner actually enforces (each
// quoting its source page), when the last build ran, and whether it still
// matches the live wiki — with a manual Sync as the fail-safe when it doesn't.
// Builds run async on the backend (minutes); the page polls while one is live.

import { useCallback, useEffect, useState } from "react"
import {
  BookOpenTextIcon,
  FileTextIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  ShieldOffIcon,
} from "lucide-react"

import { Markdown } from "@/components/chat/Markdown"
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
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
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
  degraded: { text: "Ready (reduced coverage)", variant: "secondary", cls: "" },
  building: { text: "Building…", variant: "secondary", cls: "" },
  stale: { text: "Rebuild pending", variant: "secondary", cls: "" },
  failed: { text: "Build failed", variant: "destructive", cls: "" },
  unsupported_region: {
    text: "Unavailable in this region",
    variant: "secondary",
    cls: "",
  },
}

function fmtWhen(iso) {
  if (!iso) return ""
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function pct(x) {
  return `${Math.round((Number(x) || 0) * 100)}%`
}

export default function ReasoningView({ api, selection }) {
  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const hasSelection = Boolean(domain && dataset)

  const [data, setData] = useState(null) // null until first load
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false) // an enroll/unenroll/sync in flight
  const [confirmOpen, setConfirmOpen] = useState(false)
  // The ar_rules.md viewer: fetched on first open (the doc can be tens of
  // kilobytes — never with the polled status call), cached per dataset.
  const [docOpen, setDocOpen] = useState(false)
  const [doc, setDoc] = useState(null) // {exists, text} once fetched
  const [docError, setDocError] = useState(null)
  // A queued sync/enroll reaches the pipeline asynchronously: the row still
  // reads failed/ready/"" for a few seconds until the build flips it to
  // building. Poll through that window so the page catches the transition
  // (and a fast snapshot restore, which can complete between two polls).
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
    setConfirmOpen(false)
    setDoc(null)
    setDocOpen(false)
    setDocError(null)
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
    building ||
    (data?.up_to_date === true &&
      (data?.status === "ready" || data?.status === "degraded"))
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
  const enroll = () =>
    run(async () => {
      await api.setReasoningEnrollment(domain, dataset, true)
      setSyncPending(true) // the first build is queued; poll it into view
    })
  const unenroll = () => {
    setConfirmOpen(false)
    run(() => api.setReasoningEnrollment(domain, dataset, false))
  }
  const sync = () =>
    run(async () => {
      await api.triggerReasoningSync(domain, dataset)
      setSyncPending(true)
    })
  const openDoc = async () => {
    setDocOpen(true)
    if (doc) return // cached for this dataset
    try {
      setDoc(await api.getReasoningDocument(domain, dataset))
      setDocError(null)
    } catch (e) {
      setDocError(e.message || String(e))
    }
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

  return (
    <div className="space-y-3">
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-primary" />
                Automated reasoning
              </CardTitle>
              <CardDescription>
                Checks chat answers about{" "}
                <span className="font-medium text-foreground">
                  {domain}/{dataset}
                </span>{" "}
                against rules derived from its wiki. Opt-in per dataset; the
                policy rebuilds automatically when the wiki changes.
              </CardDescription>
            </div>
            {data?.enrolled ? (
              <Button
                variant="outline"
                disabled={busy || building}
                onClick={() => setConfirmOpen(true)}
              >
                {busy ? <Spinner className="size-3.5" /> : <ShieldOffIcon className="size-3.5" />}
                Unenroll
              </Button>
            ) : (
              <Button disabled={busy || !data?.can_enroll} onClick={enroll}>
                {busy ? <Spinner className="size-3.5" /> : <ShieldCheckIcon className="size-3.5" />}
                Enroll dataset
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {error ? (
            <p className="text-sm text-destructive">{error}</p>
          ) : null}
          {!data?.enrolled ? (
            <p className="text-sm text-muted-foreground">
              {data?.can_enroll
                ? "Not enrolled. Enrolling builds the first policy from this wiki's reference docs (takes a few minutes)."
                : data?.reason ||
                  "This dataset can't be enrolled yet — run a harvest first."}
              {data?.can_enroll && !data?.has_sources
                ? " Note: this wiki has no policy-source files (usage guardrails, enums, metrics, recipes, known issues) yet, so a build would produce no rules."
                : ""}
            </p>
          ) : (
            <div className="space-y-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
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
              {/* Building: the page polls, but the row only flips when a
                  completion authority stamps it — and if the runtime's
                  in-session completion was interrupted, nothing else fires
                  automatically (the nightly reconcile is opt-in). Sync IS the
                  manual completion trigger (the rebuild authority checks
                  building rows for a finished workflow first), so it must be
                  reachable here — a building row without it can strand. */}
              {building ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">
                    A build is running — this page updates automatically. If it
                    looks stuck, Sync checks the build and completes it.
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
                  policy built from anything but the current wiki never renders
                  a verdict, so "out of date" means checks are paused until the
                  rebuild lands. Sync is the fail-safe when no automatic
                  trigger caught the change. A failed row owns its own retry
                  line below instead — checks are off there whatever the hash
                  says, so a freshness verdict would mislead. */}
              {data?.up_to_date === false && !building && !failed ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-amber-700 dark:text-amber-400">
                    Out of date — the wiki changed since this policy was built.
                    Checks are paused until it rebuilds.
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
              {/* A failed build must never strand the dataset: enrollment
                  can't be re-run (the dataset IS enrolled) and the failure may
                  be a transient service error, so the retry lives here. The
                  rebuild authority retries a failed row even when the wiki is
                  unchanged (its unchanged-skip excludes `failed`). */}
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
              {/* No build stamp at all (status ""): the first build was queued
                  by enroll but nothing has flipped the row yet. If the queued
                  window lapses (a lost event), Sync is the recovery. */}
              {!chip && !building && !syncPending ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-muted-foreground">
                    Waiting for the first build to start.
                  </span>
                  <Button size="sm" variant="ghost" disabled={busy} onClick={sync}>
                    <RefreshCwIcon className="size-3.5" />
                    Sync
                  </Button>
                </div>
              ) : null}
              {data?.fidelity_coverage || data?.fidelity_accuracy ? (
                <p className="text-muted-foreground">
                  Policy fidelity: {pct(data.fidelity_coverage)} of the source
                  material became rules, translated at{" "}
                  {pct(data.fidelity_accuracy)} accuracy.
                  {data?.status === "degraded"
                    ? " Below the quality bar — checks still run, but coverage is reduced."
                    : ""}
                </p>
              ) : null}
            </div>
          )}
        </CardContent>
      </Card>

      {data?.enrolled && data?.sources?.length ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <FileTextIcon className="size-4 text-muted-foreground" />
              Source files
            </CardTitle>
            <CardDescription>
              The wiki pages the policy is inferred from. Editing any of these
              (or a harvest changing them) makes the policy out of date.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1 font-mono text-xs text-muted-foreground">
              {data.sources.map((path) => (
                <li key={path} className="truncate">
                  {path}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      {data?.enrolled ? (
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="space-y-1.5">
                <CardTitle className="flex items-center gap-2 text-base">
                  <BookOpenTextIcon className="size-4 text-muted-foreground" />
                  Rules{data?.rules?.length ? ` (${data.rules.length})` : ""}
                </CardTitle>
                <CardDescription>
                  What the reasoner enforces, verbatim — each rule traces to
                  the wiki page it came from.
                </CardDescription>
              </div>
              {/* The document exists once anything has been authored — i.e.
                  any build state at all, including building/failed. */}
              {data?.status ? (
                <Button size="sm" variant="outline" onClick={openDoc}>
                  <FileTextIcon className="size-3.5" />
                  View ar_rules.md
                </Button>
              ) : null}
            </div>
          </CardHeader>
          <CardContent>
            {data?.rules?.length ? (
              <ul className="space-y-2.5">
                {data.rules.map((rule) => (
                  <li key={rule.id} className="space-y-0.5 text-sm">
                    <p className="border-l-2 border-border pl-2.5">{rule.text}</p>
                    {rule.source_page ? (
                      <p className="pl-2.5 font-mono text-[11px] text-muted-foreground">
                        {rule.source_page}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">
                {building
                  ? "The first build is running — rules appear here when it finishes."
                  : "No rules yet."}
              </p>
            )}
          </CardContent>
        </Card>
      ) : null}

      {/* The ar_rules.md viewer — the document the policy is built from,
          rendered in a right sheet (the app's side-panel idiom; the chat's
          DocPeek is welded into ChatPanel's layout and reads bundle pages,
          which this off-mount document is not). Version-faithful by
          construction: authoring rewrites the file at build time and a
          restore rewrites it from the restored era. */}
      <Sheet open={docOpen} onOpenChange={setDocOpen}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 sm:max-w-xl">
          <SheetHeader>
            <SheetTitle className="flex items-center gap-2 font-mono text-sm">
              <FileTextIcon className="size-4 text-muted-foreground" />
              ar_rules.md
            </SheetTitle>
            <SheetDescription>
              The numbered rules document this policy is built from, authored
              from the wiki&apos;s reference docs — as of the last sync.
            </SheetDescription>
          </SheetHeader>
          <ScrollArea className="min-h-0 flex-1 px-4 pb-4">
            {docError ? (
              <p className="text-sm text-destructive">{docError}</p>
            ) : doc === null ? (
              <div className="flex justify-center py-8">
                <Spinner className="size-4" />
              </div>
            ) : doc.exists ? (
              <Markdown>{doc.text}</Markdown>
            ) : (
              <p className="text-sm text-muted-foreground">
                No rules document yet — the first build writes it.
              </p>
            )}
          </ScrollArea>
        </SheetContent>
      </Sheet>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Unenroll from reasoning?</DialogTitle>
            <DialogDescription>
              This deletes the dataset&apos;s reasoning policy, its guardrail,
              and the derived rule artifacts. Chat answers about{" "}
              <span className="font-medium text-foreground">
                {domain}/{dataset}
              </span>{" "}
              will no longer be checkable. Re-enrolling rebuilds everything
              from the wiki (a few minutes).
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={unenroll}>
              <ShieldOffIcon className="size-3.5" />
              Unenroll and delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
