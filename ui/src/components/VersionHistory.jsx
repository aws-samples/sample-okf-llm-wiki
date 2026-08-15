// Bundle version history: compare any two published versions of a dataset's
// docs and repromote an older one to current.
//
// Split across BOTH Browse panes (state lives in the useVersionHistory hook,
// owned by BrowseView): the LEFT pane swaps the concept tree for the changed-
// file list with status filter chips (all / modified / added / removed), and
// the RIGHT pane holds the version pickers, restore controls, and the selected
// file's diff — rendered by @git-diff-view/react (word-level highlights,
// GitHub-style unified/split modes).
//
// A *version* is one completed harvest/repromote, identified by an opaque
// version_id the server reconstructs from S3 object versions — the UI never
// interprets ids, only orders/labels what /versions returns. The right select
// may also be the "live" sentinel: the current working files, which is how the
// changes of an INTERRUPTED harvest (cancelled/crashed — no version entry) are
// inspected and rolled back.
//
// Repromote is append-only on the server (the restored version becomes a NEW
// head) and is only DONE when the vector index has converged on the promoted
// content — so after the POST the hook polls /repromote until
// state === "converged" before declaring success. stalled/stalled_lease states
// surface a one-click Retry (the server lets a retry take over a dead
// repromote's lease immediately).

import { DiffModeEnum, DiffView } from "@git-diff-view/react"
import DOMPurify from "dompurify"
import HtmlDiffModule from "htmldiff-js"
import { marked } from "marked"
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleDotIcon,
  FileDiffIcon,
  MinusCircleIcon,
  PlusCircleIcon,
  RotateCcwIcon,
} from "lucide-react"
import {
  Component,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import { toast } from "sonner"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { parseDocument } from "@/lib/bundle"
import { cn } from "@/lib/utils"

import "@git-diff-view/react/styles/diff-view.css"

// htmldiff-js ships CJS with a default export; interop differs between the
// node test runner and Vite's ESM shim, so resolve both shapes.
const HtmlDiff = HtmlDiffModule?.execute ? HtmlDiffModule : HtmlDiffModule.default

const LIVE = "live"
// Select can't carry "": stands for "let the server pick the base" — the
// version right before `to` (or an empty baseline when `to` is the oldest,
// which diffs as all-added).
const AUTO = "__auto__"
const POLL_MS = 3000

function fmtVersion(v) {
  if (!v?.completed_at) return v?.version_id?.slice(0, 8) || "unknown"
  const d = new Date(v.completed_at)
  return Number.isNaN(d.getTime()) ? v.completed_at : d.toLocaleString()
}

// Track the applied theme off <html>'s `dark` class (same pattern as
// ChartFrame) so the diff view re-renders with the right palette on toggle.
function readResolvedTheme() {
  if (typeof document === "undefined") return "light"
  return document.documentElement.classList.contains("dark") ? "dark" : "light"
}

function useResolvedTheme() {
  const [theme, setTheme] = useState(readResolvedTheme)
  useEffect(() => {
    if (typeof document === "undefined") return undefined
    const sync = () => setTheme(readResolvedTheme())
    sync()
    const obs = new MutationObserver(sync)
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    })
    return () => obs.disconnect()
  }, [])
  return theme
}

// --- state hook ---------------------------------------------------------------

export function useVersionHistory({
  api,
  domain,
  dataset,
  active,
  initialTo,
  onRestored,
}) {
  const [versions, setVersions] = useState(null) // null = loading
  const [versionsError, setVersionsError] = useState(null)
  const [fromId, setFromId] = useState(AUTO)
  const [toId, setToId] = useState("")
  const [diff, setDiff] = useState(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [diffError, setDiffError] = useState(null)
  const [filter, setFilter] = useState("all") // all | modified | added | removed
  const [selectedKey, setSelectedKey] = useState(null)
  const [mode, setMode] = useState("split") // split (default) | unified | rich
  const [confirmOpen, setConfirmOpen] = useState(false)
  // Restore lifecycle: idle | posting | polling | converged | stalled |
  // stalled_lease. Carries the poll payload (done/total/target).
  const [restore, setRestore] = useState({ phase: "idle" })
  const pollRef = useRef(null)

  const currentId = versions?.[0]?.version_id || ""
  const byId = useMemo(() => {
    const m = new Map()
    for (const v of versions || []) m.set(v.version_id, v)
    return m
  }, [versions])

  const loadVersions = useCallback(
    async ({ keepSelection = false } = {}) => {
      setVersionsError(null)
      try {
        const res = await api.listBundleVersions(domain, dataset)
        const list = Array.isArray(res?.versions) ? res.versions : []
        setVersions(list)
        if (!keepSelection && list.length) {
          if (initialTo === LIVE) {
            // Interrupted-harvest review: last good version -> working files.
            setFromId(list[0].version_id)
            setToId(LIVE)
          } else {
            setToId(list[0].version_id)
            setFromId(list[1]?.version_id || AUTO)
          }
        }
      } catch (e) {
        setVersions([])
        setVersionsError(e.message || String(e))
      }
    },
    [api, domain, dataset, initialTo]
  )

  useEffect(() => {
    if (!active) return undefined
    setVersions(null)
    setDiff(null)
    setFilter("all")
    setSelectedKey(null)
    setRestore({ phase: "idle" })
    loadVersions()
    // Surface a repromote that died mid-write in an earlier session (404 = no
    // repromote ever ran — the normal case — so it is swallowed).
    let alive = true
    api
      .repromoteStatus(domain, dataset)
      .then((s) => {
        if (alive && s?.state === "stalled_lease") {
          setRestore({ phase: "stalled_lease", target: s.target_version_id })
        }
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [active, api, domain, dataset, loadVersions])

  // Fetch the diff whenever the selection changes.
  useEffect(() => {
    if (!active || !toId || versions === null) return undefined
    let alive = true
    setDiffLoading(true)
    setDiffError(null)
    api
      .bundleDiff(domain, dataset, fromId === AUTO ? undefined : fromId, toId)
      .then((d) => {
        if (!alive) return
        setDiff(d)
        setSelectedKey(d?.files?.[0]?.key ?? null)
        setFilter("all")
      })
      .catch((e) => alive && setDiffError(e.message || String(e)))
      .finally(() => alive && setDiffLoading(false))
    return () => {
      alive = false
    }
  }, [active, api, domain, dataset, fromId, toId, versions])

  const files = useMemo(() => diff?.files || [], [diff])
  const counts = useMemo(() => {
    const c = { all: files.length, modified: 0, added: 0, removed: 0 }
    for (const f of files) if (c[f.status] != null) c[f.status] += 1
    return c
  }, [files])
  const filteredFiles = useMemo(
    () => (filter === "all" ? files : files.filter((f) => f.status === filter)),
    [files, filter]
  )

  // Keep the selection inside the filtered list.
  useEffect(() => {
    if (!filteredFiles.length) {
      setSelectedKey(null)
      return
    }
    if (!filteredFiles.some((f) => f.key === selectedKey)) {
      setSelectedKey(filteredFiles[0].key)
    }
  }, [filteredFiles, selectedKey])

  const selectedFile = useMemo(
    () => files.find((f) => f.key === selectedKey) || null,
    [files, selectedKey]
  )

  // Which version would "Restore" promote? Comparing TO an older version
  // promotes it; reviewing interrupted changes (to = live) restores the base.
  // In live mode the AUTO base resolves to the newest version, and the restore
  // is offered exactly when that version is NOT current (the live marker moved
  // past it — a cancelled/crashed harvest left uncommitted working files);
  // when it IS current, live == that version and a restore would be a no-op
  // (the server refuses it with a 409 anyway).
  const restoreTarget =
    toId === LIVE
      ? fromId !== AUTO
        ? fromId
        : versions?.[0] && !versions[0].current
          ? versions[0].version_id
          : ""
      : toId && toId !== currentId
        ? toId
        : ""

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])
  useEffect(() => stopPolling, [stopPolling])

  const beginPolling = useCallback(() => {
    stopPolling()
    let inFlight = false
    let failures = 0
    pollRef.current = setInterval(async () => {
      if (inFlight) return
      inFlight = true
      try {
        const s = await api.repromoteStatus(domain, dataset)
        failures = 0
        if (s.state === "converged") {
          stopPolling()
          setRestore({ phase: "converged", ...s })
          toast.success("Version restored — the wiki and index are current.")
          await loadVersions({ keepSelection: false })
          onRestored?.()
        } else if (s.state === "stalled" || s.state === "stalled_lease") {
          stopPolling()
          setRestore({ phase: s.state, ...s, target: s.target_version_id })
        } else {
          setRestore((r) => ({ ...r, phase: "polling", ...s }))
        }
      } catch (e) {
        // Tolerate transient poll errors, but don't spin on the initial 0/N
        // forever if the status endpoint is consistently failing (e.g. a
        // server-side permission problem) — surface it instead.
        failures += 1
        if (failures >= 5) {
          stopPolling()
          setRestore((r) => ({
            ...r,
            phase: "poll_failed",
            error: e.message || String(e),
          }))
        }
      } finally {
        inFlight = false
      }
    }, POLL_MS)
  }, [api, domain, dataset, loadVersions, onRestored, stopPolling])

  // Resume the convergence poll after a poll_failed stop ("Check again").
  const resumePolling = useCallback(() => {
    setRestore((r) => ({ ...r, phase: "polling" }))
    beginPolling()
  }, [beginPolling])

  const runRestore = useCallback(
    async (versionId) => {
      setConfirmOpen(false)
      setRestore({ phase: "posting", target: versionId })
      try {
        const out = await api.repromote(domain, dataset, versionId)
        setRestore({
          phase: "polling",
          target: versionId,
          done: 0,
          total: (out.copied || 0) + (out.deleted || 0),
        })
        beginPolling()
      } catch (e) {
        const msg = e.message || String(e)
        setRestore({ phase: "idle" })
        toast.error(
          /409/.test(msg)
            ? "A harvest is running for this dataset — try again when it finishes."
            : `Restore failed: ${msg}`
        )
      }
    },
    [api, domain, dataset, beginPolling]
  )

  return {
    api,
    domain,
    dataset,
    versions,
    versionsError,
    currentId,
    byId,
    fromId,
    setFromId,
    toId,
    setToId,
    diff,
    diffLoading,
    diffError,
    files,
    counts,
    filter,
    setFilter,
    filteredFiles,
    selectedKey,
    setSelectedKey,
    selectedFile,
    mode,
    setMode,
    restore,
    restoreTarget,
    confirmOpen,
    setConfirmOpen,
    runRestore,
    resumePolling,
  }
}

// --- left pane: changed files + status filter ----------------------------------

const STATUS_META = {
  modified: {
    label: "modified",
    Icon: CircleDotIcon,
    className: "text-amber-600 dark:text-amber-400",
  },
  added: {
    label: "added",
    Icon: PlusCircleIcon,
    className: "text-emerald-600 dark:text-emerald-400",
  },
  removed: {
    label: "removed",
    Icon: MinusCircleIcon,
    className: "text-rose-600 dark:text-rose-400",
  },
}

const FILTERS = ["all", "modified", "added", "removed"]

// GitHub-style per-file line stats (+added / −removed), shared by the file
// list rows and the diff card header. Zero sides are omitted.
function LineStats({ file, className }) {
  const added = file.lines_added || 0
  const removed = file.lines_removed || 0
  if (!added && !removed) return null
  return (
    <span
      className={cn(
        "flex shrink-0 items-center gap-1.5 font-mono text-[11px]",
        className
      )}
    >
      {added ? (
        <span className="text-emerald-600 dark:text-emerald-400">+{added}</span>
      ) : null}
      {removed ? (
        <span className="text-rose-600 dark:text-rose-400">−{removed}</span>
      ) : null}
    </span>
  )
}

export function VersionFilePane({ vh }) {
  if (vh.versions === null || vh.diffLoading) {
    return (
      <div className="flex flex-col gap-2 p-4">
        <Skeleton className="h-6 w-full" />
        <Skeleton className="h-6 w-3/4" />
        <Skeleton className="h-6 w-5/6" />
      </div>
    )
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Status filter: one click narrows the list to that change kind. */}
      <div className="flex flex-wrap gap-1 px-3 pt-2 pb-1.5">
        {FILTERS.map((f) => {
          const count = vh.counts[f] ?? 0
          const active = vh.filter === f
          return (
            <button
              key={f}
              type="button"
              onClick={() => vh.setFilter(f)}
              disabled={f !== "all" && count === 0}
              className={cn(
                "rounded-md border px-2 py-0.5 text-[11px] capitalize transition-colors",
                active
                  ? "border-transparent bg-muted font-medium text-foreground"
                  : "text-muted-foreground hover:bg-muted/50",
                f !== "all" && count === 0 && "opacity-40"
              )}
            >
              {f} {count}
            </button>
          )
        })}
      </div>
      <div className="h-px shrink-0 bg-gradient-to-r from-transparent via-border/60 to-transparent" />
      <ScrollArea className="okf-tree-scroll min-h-0 flex-1">
        <ul className="flex flex-col p-2">
          {vh.filteredFiles.length === 0 ? (
            <li className="px-2 py-3 text-xs text-muted-foreground">
              {vh.files.length === 0
                ? "No differences between these versions."
                : "No files match this filter."}
            </li>
          ) : (
            vh.filteredFiles.map((f) => {
              const meta = STATUS_META[f.status] || STATUS_META.modified
              const selected = vh.selectedKey === f.key
              return (
                <li key={f.key}>
                  <button
                    type="button"
                    onClick={() => vh.setSelectedKey(f.key)}
                    className={cn(
                      "flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted",
                      selected && "bg-muted font-medium text-foreground"
                    )}
                  >
                    <meta.Icon
                      className={cn("size-3.5 shrink-0", meta.className)}
                    />
                    <span className="min-w-0 truncate font-mono text-xs">
                      {f.concept_id || f.key.split("/").slice(3).join("/")}
                    </span>
                    <LineStats file={f} className="ml-auto" />
                  </button>
                </li>
              )
            })
          )}
        </ul>
      </ScrollArea>
    </div>
  )
}

// --- right pane: pickers + restore + the selected file's diff ------------------

// The server sends difflib unified-diff text, which opens with `--- a/...` /
// `+++ b/...` header lines — @git-diff-view's parser REQUIRES those headers
// (verified against v0.1.7: header-less hunks parse to zero lines), so the
// text is handed over verbatim as one hunk entry.
function toHunks(diffText) {
  const body = diffText || ""
  return body.trim() ? [body] : []
}

// If the library ever fails to parse a server diff, degrade to the raw text
// instead of unmounting the Browse pane.
class DiffBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }
  static getDerivedStateFromError() {
    return { hasError: true }
  }
  componentDidCatch(error) {
    console.error("[VersionHistory] diff render error:", error)
  }
  render() {
    if (this.state.hasError) {
      return (
        <pre className="overflow-auto p-3 font-mono text-xs whitespace-pre-wrap">
          {this.props.raw}
        </pre>
      )
    }
    return this.props.children
  }
}

// Rendered ("rich") diff: both sides' markdown -> HTML (marked, GFM) ->
// htmldiff-js weaves word-level <ins>/<del> marks into the merged document ->
// DOMPurify strips everything unsafe (this is the ONLY dangerouslySetInnerHTML
// in the app, and it renders agent-authored content — the sanitize is not
// optional). Frontmatter is stripped (parseDocument) — its changes are source-
// level detail; the source modes show them.
function RichDiff({ vh, file }) {
  const [state, setState] = useState({ status: "loading" })

  useEffect(() => {
    let alive = true
    setState({ status: "loading" })
    const side = (versionId, absent) =>
      absent
        ? Promise.resolve("")
        : vh.api
            .readBundleFile(vh.domain, vh.dataset, file.key, versionId || undefined)
            .then((r) => r?.text ?? "")
    Promise.all([
      side(file.old_version_id, file.status === "added"),
      side(file.new_version_id, file.status === "removed"),
    ])
      .then(([oldText, newText]) => {
        if (!alive) return
        const render = (text) =>
          marked.parse(parseDocument(text || "").body || "", { gfm: true })
        const merged = HtmlDiff.execute(render(oldText), render(newText))
        setState({ status: "ok", html: DOMPurify.sanitize(merged) })
      })
      .catch((e) => {
        if (alive) setState({ status: "error", error: e.message || String(e) })
      })
    return () => {
      alive = false
    }
  }, [vh.api, vh.domain, vh.dataset, file])

  if (state.status === "loading") {
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
        <Spinner /> Rendering…
      </div>
    )
  }
  if (state.status === "error") {
    return (
      <p className="p-3 text-xs text-muted-foreground">
        Couldn't build the rendered diff ({state.error}) — use the source views.
      </p>
    )
  }
  return (
    <div
      className="okf-prose okf-richdiff p-4"
      dangerouslySetInnerHTML={{ __html: state.html }}
    />
  )
}

// Size the diff scroll box to EXACTLY the remaining viewport below its own
// top edge (the .okf-diff-scroll CSS calc is only the pre-measure fallback —
// a fixed offset can't know how much chrome sits above). Re-measures on
// window resize and on ancestor scrolls (capture phase catches the Browse
// pane's own ScrollArea moving the box).
function useViewportFill(deps) {
  const ref = useRef(null)
  const [maxH, setMaxH] = useState(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const measure = () => {
      const top = el.getBoundingClientRect().top
      // 44px clears the pane's bottom padding + card border below the box,
      // so the page itself never gains a scrollbar.
      setMaxH(Math.max(240, window.innerHeight - top - 44))
    }
    measure()
    window.addEventListener("resize", measure)
    window.addEventListener("scroll", measure, true)
    return () => {
      window.removeEventListener("resize", measure)
      window.removeEventListener("scroll", measure, true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return [ref, maxH]
}

function SelectedFileDiff({ vh, file, mode, theme }) {
  const [scrollRef, maxH] = useViewportFill([file.key, mode])
  const scrollStyle = maxH != null ? { maxHeight: `${maxH}px` } : undefined
  const hunks = useMemo(() => toHunks(file.diff), [file.diff])
  const meta = STATUS_META[file.status] || STATUS_META.modified
  return (
    // okf-diffview scopes the index.css overrides that re-skin the library's
    // GitHub palette onto the app's theme tokens.
    <div className="okf-diffview overflow-hidden rounded-md border">
      <div className="flex items-center gap-2 border-b bg-muted/40 px-3 py-2">
        <meta.Icon className={cn("size-3.5 shrink-0", meta.className)} />
        <span className="min-w-0 truncate font-mono text-xs">
          {file.concept_id || file.key.split("/").slice(3).join("/")}
        </span>
        {file.title ? (
          <span className="min-w-0 truncate text-xs text-muted-foreground">
            {file.title}
          </span>
        ) : null}
        <LineStats file={file} className="ml-auto" />
        <Badge variant="outline" className={cn("shrink-0", meta.className)}>
          {meta.label}
        </Badge>
      </div>
      {mode === "rich" ? (
        <div ref={scrollRef} style={scrollStyle} className="okf-diff-scroll">
          <RichDiff vh={vh} file={file} />
        </div>
      ) : hunks.length ? (
        <div ref={scrollRef} style={scrollStyle} className="okf-diff-scroll">
        <DiffBoundary raw={file.diff}>
          <DiffView
            key={`${file.key}:${theme}`}
            data={{
              oldFile: { fileName: `a/${file.key}`, fileLang: "markdown" },
              newFile: { fileName: `b/${file.key}`, fileLang: "markdown" },
              hunks,
            }}
            diffViewMode={
              mode === "split" ? DiffModeEnum.Split : DiffModeEnum.Unified
            }
            diffViewTheme={theme}
            diffViewWrap
            diffViewHighlight
            diffViewFontSize={12}
          />
        </DiffBoundary>
        </div>
      ) : (
        <p className="p-3 text-xs text-muted-foreground">
          Diff omitted (response size cap) — compare fewer versions apart.
        </p>
      )}
      {file.diff_truncated ? (
        <p className="border-t px-3 py-1.5 text-xs text-muted-foreground">
          Diff truncated — showing the first lines only.
        </p>
      ) : null}
    </div>
  )
}

export function VersionDiffPane({ vh }) {
  const theme = useResolvedTheme()

  if (vh.versions === null) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }
  if (vh.versionsError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Failed to load versions</AlertTitle>
        <AlertDescription>{vh.versionsError}</AlertDescription>
      </Alert>
    )
  }
  if (!vh.versions.length) {
    return (
      <Alert>
        <FileDiffIcon />
        <AlertTitle>No published versions</AlertTitle>
        <AlertDescription>
          Version history appears after the first completed harvest.
        </AlertDescription>
      </Alert>
    )
  }

  const versionLabel = (v) => (
    <span className="flex items-center gap-2">
      <span>{fmtVersion(v)}</span>
      {v.current ? (
        <Badge variant="secondary" className="text-[10px]">
          current
        </Badge>
      ) : null}
      {v.repromoted_from ? (
        <Badge variant="outline" className="text-[10px]">
          restored
        </Badge>
      ) : null}
    </span>
  )

  const summary = vh.diff?.summary
  const restore = vh.restore

  return (
    <div className="flex flex-col gap-4">
      {/* Left/right version pickers + view toggle + restore. */}
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">Compare from</span>
          <Select value={vh.fromId} onValueChange={vh.setFromId}>
            <SelectTrigger size="sm" className="max-w-60 text-xs">
              <SelectValue placeholder="Base version…" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={AUTO}>Auto (version before the target)</SelectItem>
              {vh.versions.map((v) => (
                <SelectItem key={v.version_id} value={v.version_id}>
                  {versionLabel(v)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">to</span>
          <Select value={vh.toId} onValueChange={vh.setToId}>
            <SelectTrigger size="sm" className="max-w-60 text-xs">
              <SelectValue placeholder="Target version…" />
            </SelectTrigger>
            <SelectContent>
              {vh.versions.map((v) => (
                <SelectItem key={v.version_id} value={v.version_id}>
                  {versionLabel(v)}
                </SelectItem>
              ))}
              <SelectItem value={LIVE}>Working files (uncommitted)</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="ml-auto flex h-7 items-stretch overflow-hidden rounded-md border">
          {["split", "unified", "rich"].map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => vh.setMode(m)}
              className={cn(
                "flex items-center px-2.5 text-xs capitalize transition-colors",
                vh.mode === m
                  ? "bg-muted font-medium text-foreground"
                  : "text-muted-foreground hover:bg-muted/50"
              )}
            >
              {m}
            </button>
          ))}
        </div>
        {vh.restoreTarget ? (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            disabled={restore.phase === "posting" || restore.phase === "polling"}
            onClick={() => vh.setConfirmOpen(true)}
          >
            <RotateCcwIcon className="size-3.5" />
            Restore this version
          </Button>
        ) : null}
      </div>

      {/* Restore progress / recovery states. */}
      {restore.phase === "posting" || restore.phase === "polling" ? (
        <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-2 text-sm">
          <Spinner className="size-4" />
          {restore.phase === "posting"
            ? "Restoring…"
            : `Restored — indexing ${restore.done ?? 0}/${restore.total ?? "…"}…`}
        </div>
      ) : null}
      {restore.phase === "converged" ? (
        <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-2 text-sm">
          <CheckCircle2Icon className="size-4 text-emerald-600 dark:text-emerald-400" />
          Version restored and the search index is up to date.
        </div>
      ) : null}
      {restore.phase === "poll_failed" ? (
        <Alert>
          <AlertTriangleIcon />
          <AlertTitle>Can't check indexing progress</AlertTitle>
          <AlertDescription className="flex items-center gap-3">
            <span>
              The restore itself completed; the progress check keeps failing
              ({restore.error}). The index usually converges on its own.
            </span>
            <Button size="sm" variant="outline" onClick={vh.resumePolling}>
              Check again
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {restore.phase === "stalled" || restore.phase === "stalled_lease" ? (
        <Alert>
          <AlertTriangleIcon />
          <AlertTitle>
            {restore.phase === "stalled"
              ? "The search index hasn't caught up"
              : "A previous restore didn't finish"}
          </AlertTitle>
          <AlertDescription className="flex items-center gap-3">
            <span>
              {restore.phase === "stalled"
                ? "The files were restored but some documents may be stale in search. Retrying is safe."
                : "It stopped mid-write; the bundle may be inconsistent. Retry to take over and finish it."}
            </span>
            {restore.target ? (
              <Button
                size="sm"
                variant="outline"
                onClick={() => vh.runRestore(restore.target)}
              >
                <RotateCcwIcon className="size-3.5" />
                Retry
              </Button>
            ) : null}
          </AlertDescription>
        </Alert>
      ) : null}

      {/* The selected file's diff. */}
      {vh.diffLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Computing diff…
        </div>
      ) : vh.diffError ? (
        <Alert variant="destructive">
          <AlertTitle>Failed to diff</AlertTitle>
          <AlertDescription>{vh.diffError}</AlertDescription>
        </Alert>
      ) : vh.diff ? (
        <div className="flex flex-col gap-2">
          {summary ? (
            <p className="text-xs text-muted-foreground">
              {summary.added} added · {summary.removed} removed ·{" "}
              {summary.modified} modified · {summary.unchanged} unchanged
              {vh.diff.truncated ? " · list truncated" : ""}
            </p>
          ) : null}
          {vh.selectedFile ? (
            <SelectedFileDiff
              vh={vh}
              file={vh.selectedFile}
              mode={vh.mode}
              theme={theme}
            />
          ) : vh.files.length ? (
            <p className="text-sm text-muted-foreground">
              Select a file on the left to see its changes.
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">
              No differences between these versions.
            </p>
          )}
        </div>
      ) : null}

      {/* Restore confirmation. */}
      <Dialog open={vh.confirmOpen} onOpenChange={vh.setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Restore this version?</DialogTitle>
            <DialogDescription>
              The docs from{" "}
              <span className="font-medium text-foreground">
                {fmtVersion(vh.byId.get(vh.restoreTarget))}
              </span>{" "}
              become current again. The restore is saved as a new version, so
              nothing is lost, and the search index updates to match. Keep in
              mind that a future harvest, whether run manually or triggered by
              a catalog change, will overwrite the restored docs.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => vh.setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button onClick={() => vh.runRestore(vh.restoreTarget)}>
              <RotateCcwIcon className="size-3.5" />
              Restore
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
