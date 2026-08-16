import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"
import {
  BrainIcon,
  CalendarClockIcon,
  DatabaseIcon,
  PencilIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react"

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
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"

// User-facing labels for the record types. "stated" = a preference the user
// told the agent to remember; "binding" = a learned term→data shortcut.
const TYPE_LABELS = { stated: "Preference", binding: "Shortcut", personal: "Personal" }

// Sentinel values for the dataset filter's two fixed options ("" is taken by
// the generic-record dataset, and Radix Select forbids an empty item value).
const FILTER_ALL = "__all__"
const FILTER_GENERIC = "__generic__"

// Windowed rendering: the API returns the COMPLETE record set (it paginates
// list_memory_records to exhaustion server-side), so this is presentational —
// render this many rows, grow by the same step when the scroll sentinel comes
// into view, reset when a filter changes.
const PAGE_SIZE = 50

// One memory record row: the text is the content; type/dataset/expiry ride
// below it as small chips; edit/delete actions sit on the right.
function MemoryRecord({ record, onEdit, onDelete }) {
  return (
    <div className="flex items-start gap-3 px-4 py-3">
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <p
          className={cn(
            "text-sm leading-relaxed",
            // Expired records read as inert history, not live memory.
            record.expired ? "text-muted-foreground" : "text-foreground"
          )}
        >
          {record.text}
        </p>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="secondary">
            {TYPE_LABELS[record.type] || record.type}
          </Badge>
          {record.dataset ? (
            <Badge variant="outline">
              <DatabaseIcon />
              {record.dataset}
            </Badge>
          ) : null}
          {record.expires ? (
            record.expired ? (
              <Badge variant="destructive">
                <CalendarClockIcon />
                Expired {record.expires}
              </Badge>
            ) : (
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <CalendarClockIcon className="size-3" />
                Valid until {record.expires}
              </span>
            )
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-0.5">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Edit memory"
          onClick={() => onEdit(record)}
        >
          <PencilIcon />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Delete memory"
          onClick={() => onDelete(record)}
        >
          <Trash2Icon />
        </Button>
      </div>
    </div>
  )
}

// What the agent remembers about you, and the controls to curate it. A chat
// sub-page: records are written by the chat agent; this page only reviews,
// edits, and deletes them, plus the master save/recall switch.
export default function MemoryView({ api }) {
  const [enabled, setEnabled] = useState(false)
  const [records, setRecords] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // 404 on any memory route = the deployment ships without the memory
  // feature — a distinct (non-error) empty state.
  const [unavailable, setUnavailable] = useState(false)
  const [toggling, setToggling] = useState(false)
  // Ref twin of `toggling` for the load callback: a list fetch resolving
  // while a toggle is in flight must not roll the switch back to the
  // pre-toggle server value (state in the closure would be stale).
  const togglingRef = useRef(false)

  // Client-side filters over the loaded records.
  const [datasetFilter, setDatasetFilter] = useState(FILTER_ALL)
  const [temporalOnly, setTemporalOnly] = useState(false)

  // Edit/delete targets (null = closed). Kept as the whole record so the
  // dialogs can show what they act on.
  const [editing, setEditing] = useState(null)
  const [deleting, setDeleting] = useState(null)

  // The fetch itself. Spinner/error RESETS happen at the call sites (mount
  // rides the initial loading=true; Refresh sets them before calling), and
  // every setState here lives in a promise callback — nothing synchronous
  // runs in the mount effect, so react-hooks/set-state-in-effect stays quiet
  // (it flags the app's usual async `load()` idiom even past the await).
  const load = useCallback(() => {
    if (!api) return
    api
      .getMemory()
      .then((res) => {
        if (!togglingRef.current) setEnabled(Boolean(res?.enabled))
        setRecords(Array.isArray(res?.records) ? res.records : [])
        setUnavailable(false)
        setError(null)
      })
      .catch((e) => {
        if (e.status === 404) setUnavailable(true)
        else setError(e.message || String(e))
      })
      .finally(() => setLoading(false))
  }, [api])

  useEffect(() => {
    load()
  }, [load])

  const refresh = () => {
    setLoading(true)
    setError(null)
    load()
  }

  const toggleEnabled = async (next) => {
    // Optimistic flip; reconciled with (or rolled back to) the server answer.
    setEnabled(next)
    setToggling(true)
    togglingRef.current = true
    try {
      const res = await api.setMemorySettings(next)
      setEnabled(Boolean(res?.enabled))
    } catch (e) {
      setEnabled(!next)
      if (e.status === 404) setUnavailable(true)
      else toast.error(`Could not update memory setting: ${e.message || e}`)
    } finally {
      setToggling(false)
      togglingRef.current = false
    }
  }

  // Distinct datasets present in the records, for the filter options.
  const datasets = useMemo(
    () =>
      [...new Set(records.map((r) => r.dataset).filter(Boolean))].sort((a, b) =>
        a.localeCompare(b)
      ),
    [records]
  )

  const filtered = useMemo(
    () =>
      records.filter((r) => {
        if (datasetFilter === FILTER_GENERIC && r.dataset) return false
        if (
          datasetFilter !== FILTER_ALL &&
          datasetFilter !== FILTER_GENERIC &&
          r.dataset !== datasetFilter
        )
          return false
        if (temporalOnly && !r.expires) return false
        return true
      }),
    [records, datasetFilter, temporalOnly]
  )

  // The render window over `filtered`. Keyed on the filter combination and
  // reset via the setState-during-render previous-value pattern, so changing
  // a filter recomputes from the first page.
  const filterKey = `${datasetFilter}|${temporalOnly}`
  const [page, setPage] = useState({ key: filterKey, count: PAGE_SIZE })
  if (page.key !== filterKey) setPage({ key: filterKey, count: PAGE_SIZE })
  const visible = filtered.slice(0, page.count)
  const hasMore = filtered.length > visible.length

  // Grow the window when the sentinel row scrolls into view. The observer is
  // re-created per window step: an observer only fires on threshold
  // CROSSINGS, so on a tall viewport where the sentinel stays visible after
  // a step, re-observing is what keeps the next page loading.
  const listRef = useRef(null)
  const sentinelRef = useRef(null)
  useEffect(() => {
    const el = sentinelRef.current
    if (!el) return undefined
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setPage((p) => ({ ...p, count: p.count + PAGE_SIZE }))
        }
      },
      { root: listRef.current, rootMargin: "200px" }
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasMore, page.count])

  const saveEdit = async (id, text) => {
    const updated = await api.updateMemory(id, text)
    setRecords((prev) => prev.map((r) => (r.id === id ? updated : r)))
  }

  const confirmDelete = async (id) => {
    await api.deleteMemory(id)
    setRecords((prev) => prev.filter((r) => r.id !== id))
  }

  return (
    // pt-1: the full-height column pads 4px; the sidebar's first content
    // sits at 8px (SidebarHeader p-2) — one extra notch makes the card's top
    // edge run parallel with the sidebar. max-h-full + min-h-0 down the
    // chain (root → Card → CardContent → list): the card stays content-sized
    // while short and caps at the viewport once the record list outgrows it,
    // at which point ONLY the list scrolls — header, switch, and filters
    // stay fixed (the ReasoningView/BenchmarkView pattern).
    <div className="flex max-h-full min-h-0 flex-col gap-4 pt-1">
      <Card className="max-h-full min-h-0">
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2">
            <BrainIcon className="size-4" />
            Memory
          </CardTitle>
          <CardDescription>
            What the chat agent remembers about you across conversations —
            review, edit, or delete anything here.
          </CardDescription>
          <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
            <Button
              variant="outline"
              onClick={refresh}
              disabled={loading || unavailable}
            >
              {loading ? (
                <Spinner />
              ) : (
                <RefreshCwIcon data-icon="inline-start" />
              )}
              Refresh
            </Button>
          </div>
        </CardHeader>
        <CardContent className="flex min-h-0 flex-col gap-4">
          {unavailable ? (
            <Alert>
              <BrainIcon />
              <AlertTitle>Memory is not enabled on this deployment</AlertTitle>
              <AlertDescription>
                This deployment ships without the agent-memory feature, so there
                is nothing to manage here.
              </AlertDescription>
            </Alert>
          ) : error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load memory</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : loading ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
              <Skeleton className="h-14 w-full" />
            </div>
          ) : (
            <>
              {/* Master switch: save + recall on/off. Records survive OFF. */}
              <div className="flex items-center justify-between gap-4 rounded-lg border px-4 py-3">
                <div className="flex flex-col gap-0.5">
                  <Label htmlFor="memory-enabled">Memory</Label>
                  <p className="text-xs text-muted-foreground">
                    When off, the agent neither saves new memories nor recalls
                    existing ones — your records below are kept.
                  </p>
                </div>
                <Switch
                  id="memory-enabled"
                  checked={enabled}
                  disabled={toggling}
                  onCheckedChange={toggleEnabled}
                />
              </div>

              {records.length === 0 ? (
                <Alert>
                  <AlertTitle>No memories yet</AlertTitle>
                  <AlertDescription>
                    As you chat, the agent remembers things worth keeping:
                    preferences you state ("always show speeds in km/h") and
                    shortcuts it learns for terms you use. They appear here so
                    you can review, edit, or delete them at any time.
                  </AlertDescription>
                </Alert>
              ) : (
                <>
                  {/* Client-side filters over the loaded records. */}
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                    <Select
                      value={datasetFilter}
                      onValueChange={setDatasetFilter}
                    >
                      <SelectTrigger
                        aria-label="Filter by dataset"
                        className="w-52"
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent position="popper">
                        <SelectItem value={FILTER_ALL}>All datasets</SelectItem>
                        <SelectItem value={FILTER_GENERIC}>
                          Generic only
                        </SelectItem>
                        {datasets.map((d) => (
                          <SelectItem key={d} value={d}>
                            <DatabaseIcon className="size-4" />
                            {d}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <div className="flex items-center gap-2">
                      <Switch
                        id="memory-temporal"
                        size="sm"
                        checked={temporalOnly}
                        onCheckedChange={setTemporalOnly}
                      />
                      <Label
                        htmlFor="memory-temporal"
                        className="text-xs font-normal text-muted-foreground"
                      >
                        Temporal only
                      </Label>
                    </div>
                  </div>

                  {filtered.length === 0 ? (
                    <p className="px-1 py-6 text-center text-sm text-muted-foreground">
                      No memories match the current filters.
                    </p>
                  ) : (
                    // The page's ONLY scroll region — the border is its fixed
                    // frame, the rows (windowed to `visible`) scroll inside.
                    <div
                      ref={listRef}
                      className="okf-thin-scroll min-h-0 divide-y overflow-y-auto rounded-lg border"
                    >
                      {visible.map((r) => (
                        <MemoryRecord
                          key={r.id}
                          record={r}
                          onEdit={setEditing}
                          onDelete={setDeleting}
                        />
                      ))}
                      {hasMore ? (
                        <div
                          ref={sentinelRef}
                          className="px-4 py-3 text-center text-xs text-muted-foreground"
                        >
                          Showing {visible.length} of {filtered.length} — scroll
                          for more
                        </div>
                      ) : null}
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <EditMemoryDialog
        record={editing}
        onClose={() => setEditing(null)}
        onSave={saveEdit}
      />
      <DeleteMemoryDialog
        record={deleting}
        onClose={() => setDeleting(null)}
        onConfirm={confirmDelete}
      />
    </div>
  )
}

// Edit ONLY the text (type/dataset/expiry are the agent's to manage). The
// FORM is a separate component keyed on the record id, so opening a different
// record re-seeds the textarea via initial state — no re-seeding effect.
function EditMemoryDialog({ record, onClose, onSave }) {
  return (
    <Dialog open={Boolean(record)} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        {record ? (
          <EditMemoryForm
            key={record.id}
            record={record}
            onClose={onClose}
            onSave={onSave}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function EditMemoryForm({ record, onClose, onSave }) {
  const [text, setText] = useState(record.text || "")
  const [saving, setSaving] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    if (!text.trim()) {
      toast.error("Memory text cannot be empty.")
      return
    }
    setSaving(true)
    try {
      await onSave(record.id, text.trim())
      onClose()
    } catch (err) {
      toast.error(`Could not save: ${err.message || err}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>Edit memory</DialogTitle>
        <DialogDescription>
          Rewrite what the agent remembers. Only the text changes — the type,
          dataset, and expiry stay as they are.
        </DialogDescription>
      </DialogHeader>
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={4}
        aria-label="Memory text"
      />
      <DialogFooter>
        <DialogClose asChild>
          <Button type="button" variant="outline">
            Cancel
          </Button>
        </DialogClose>
        <Button type="submit" disabled={saving}>
          {saving ? <Spinner /> : null}
          Save
        </Button>
      </DialogFooter>
    </form>
  )
}

function DeleteMemoryDialog({ record, onClose, onConfirm }) {
  const [deleting, setDeleting] = useState(false)
  // The dialog animates closed AFTER `record` goes null — keep rendering the
  // last one so the description doesn't flash empty quotes during the exit
  // (setState-during-render is React's documented previous-value pattern).
  const [shown, setShown] = useState(record)
  if (record && record !== shown) setShown(record)

  const confirm = async () => {
    setDeleting(true)
    try {
      await onConfirm(record.id)
      onClose()
    } catch (err) {
      toast.error(`Could not delete: ${err.message || err}`)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Dialog open={Boolean(record)} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete memory?</DialogTitle>
          <DialogDescription>
            The agent will forget{" "}
            <span className="font-medium text-foreground">
              "{shown?.text}"
            </span>
            . This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline">
              Cancel
            </Button>
          </DialogClose>
          <Button variant="destructive" onClick={confirm} disabled={deleting}>
            {deleting ? <Spinner /> : <Trash2Icon data-icon="inline-start" />}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
