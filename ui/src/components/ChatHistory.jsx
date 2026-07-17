// The per-user conversation history — a right-hand drawer inside the chat page,
// toggled from the sidebar (closed by default). Lists the caller's conversations
// (GET /chat/threads, filtered server-side by the JWT sub), and lets them search,
// resume, rename, or delete one. Resuming remounts the conversation at that
// threadId; the chat session then fetches the persisted turns from the runtime
// (get_session_history over the DynamoDB checkpoint), so a resumed thread fills
// in immediately.
//
// It renders at a FIXED width (w-72) and is clipped by an animated wrapper in
// ChatPanel, so opening/closing is a smooth width slide without the content
// reflowing mid-transition.

import {
  HistoryIcon,
  MessageSquareTextIcon,
  PencilIcon,
  SearchIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

// Coarse day buckets for the group headers. Threads arrive sorted newest-first,
// so we just label the boundaries as we walk them.
function dayBucket(iso) {
  if (!iso) return "Older"
  const startOfDay = (d) =>
    new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const days = Math.round(
    (startOfDay(new Date()) - startOfDay(new Date(iso))) / 86400000
  )
  if (days <= 0) return "Today"
  if (days === 1) return "Yesterday"
  if (days < 7) return "Previous 7 days"
  if (days < 30) return "Previous 30 days"
  return "Older"
}

// Compact "time-ago" for the row's meta line.
function relativeTime(iso) {
  if (!iso) return ""
  const secs = (Date.now() - new Date(iso).getTime()) / 1000
  if (secs < 60) return "just now"
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  const days = Math.floor(secs / 86400)
  if (days < 7) return `${days}d ago`
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  })
}

// A skeleton stand-in for the list while the first fetch is in flight — mirrors
// the real grouped layout (a header + a few rows of varying widths) so the drawer
// doesn't pop from empty to full. Widths vary per row to feel organic.
function HistorySkeleton() {
  const widths = ["78%", "62%", "88%", "54%", "70%", "46%"]
  return (
    <div className="flex flex-col gap-3" aria-hidden="true">
      {[0, 1].map((g) => (
        <div key={g} className="flex flex-col gap-1.5">
          <Skeleton className="ml-2 h-2.5 w-16 rounded-full" />
          {(g === 0 ? widths.slice(0, 4) : widths.slice(4)).map((w, i) => (
            <div key={i} className="flex flex-col gap-1 px-2 py-1.5">
              <Skeleton className="h-3.5 rounded-md" style={{ width: w }} />
              <Skeleton className="h-2.5 w-20 rounded-md" />
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

export function ChatHistory({
  api,
  activeThreadId,
  onResume,
  onDeleted,
  onClose,
  reloadKey,
}) {
  const [threads, setThreads] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(null) // thread_id being renamed
  const [draft, setDraft] = useState("")
  const [query, setQuery] = useState("")
  const searchRef = useRef(null)

  const load = useCallback(async () => {
    if (!api) return
    setLoading(true)
    setError(null)
    try {
      const res = await api.listChatThreads()
      setThreads(Array.isArray(res?.threads) ? res.threads : [])
    } catch (e) {
      setError(e.message || "failed to load conversations")
      setThreads([])
    } finally {
      setLoading(false)
    }
  }, [api])

  // Reload on open and whenever the parent bumps reloadKey (e.g. after a turn
  // creates/renames a row, or a new chat starts).
  useEffect(() => {
    load()
  }, [load, reloadKey])

  // Live title filter (case-insensitive). The dataset scope is searchable too so
  // "@sales" narrows to a domain's chats.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return threads
    return threads.filter((t) => {
      const scope = t.dataset_scope
        ? `${t.dataset_scope.data_domain}/${t.dataset_scope.dataset}`
        : ""
      return (
        (t.title || "").toLowerCase().includes(q) ||
        scope.toLowerCase().includes(q)
      )
    })
  }, [threads, query])

  // Group into coarse day buckets, preserving the server's newest-first order.
  // A running index across ALL rows drives the entrance stagger.
  const groups = useMemo(() => {
    const out = []
    let cur = null
    let idx = 0
    for (const t of filtered) {
      const bucket = dayBucket(t.updated_at)
      if (!cur || cur.bucket !== bucket) {
        cur = { bucket, items: [] }
        out.push(cur)
      }
      cur.items.push({ thread: t, idx: idx++ })
    }
    return out
  }, [filtered])

  const submitRename = useCallback(
    async (threadId) => {
      const title = draft.trim()
      setEditing(null)
      if (!title) return
      // optimistic
      setThreads((ts) =>
        ts.map((t) => (t.thread_id === threadId ? { ...t, title } : t))
      )
      try {
        await api.renameChatThread(threadId, title)
      } catch {
        load() // revert to server truth on failure
      }
    },
    [api, draft, load]
  )

  const remove = useCallback(
    async (threadId) => {
      // optimistic removal
      setThreads((ts) => ts.filter((t) => t.thread_id !== threadId))
      try {
        await api.deleteChatThread(threadId)
        onDeleted?.(threadId)
      } catch {
        load()
      }
    },
    [api, load, onDeleted]
  )

  const hasThreads = threads.length > 0
  const noMatches = !loading && !error && hasThreads && filtered.length === 0

  return (
    <div className="flex h-full w-72 shrink-0 flex-col border-l bg-card/40">
      {/* Header — mirrors the sidebar's own headers (icon + label), with the
          count as a subtle pill and a close affordance on the right. */}
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        <HistoryIcon className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">History</span>
        {!loading && hasThreads ? (
          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground tabular-nums">
            {threads.length}
          </span>
        ) : null}
        {onClose ? (
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto size-7 text-muted-foreground"
            aria-label="Close history"
            onClick={onClose}
          >
            <XIcon className="size-4" />
          </Button>
        ) : null}
      </div>

      {/* Search — shown once there's anything to search. A leading icon and a
          clear button that appears while typing. */}
      {hasThreads || query ? (
        <div className="shrink-0 border-b p-2">
          <div className="relative">
            <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && query) {
                  setQuery("")
                  e.stopPropagation()
                }
              }}
              placeholder="Search conversations…"
              className="h-8 w-full rounded-lg border border-transparent bg-input/50 pr-7 pl-8 text-sm outline-none transition-[color,box-shadow] duration-200 placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/30"
            />
            {query ? (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => {
                  setQuery("")
                  searchRef.current?.focus()
                }}
                className="absolute top-1/2 right-1.5 flex size-5 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <XIcon className="size-3.5" />
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="okf-thin-scroll min-h-0 flex-1 overflow-y-auto p-2">
        {error ? (
          <div className="rounded-md bg-destructive/10 px-2 py-1.5 text-xs text-destructive">
            {error}
          </div>
        ) : null}

        {loading ? <HistorySkeleton /> : null}

        {!loading && !error && !hasThreads ? (
          <div className="flex flex-col items-center gap-2 px-2 py-10 text-center">
            <MessageSquareTextIcon className="size-6 text-muted-foreground/40" />
            <div className="text-xs text-muted-foreground">
              No conversations yet.
            </div>
          </div>
        ) : null}

        {noMatches ? (
          <div className="flex flex-col items-center gap-2 px-2 py-10 text-center">
            <SearchIcon className="size-6 text-muted-foreground/40" />
            <div className="text-xs text-muted-foreground">
              No matches for “{query.trim()}”.
            </div>
          </div>
        ) : null}

        {!loading && !error ? (
          <div className="flex flex-col gap-3">
            {groups.map((group) => (
              <div key={group.bucket} className="flex flex-col gap-0.5">
                <div className="px-2 pt-1 pb-0.5 text-[11px] font-medium tracking-wide text-muted-foreground/70 uppercase">
                  {group.bucket}
                </div>
                {group.items.map(({ thread: t, idx }) => {
                  const active = t.thread_id === activeThreadId
                  const isEditing = editing === t.thread_id
                  if (isEditing) {
                    return (
                      <input
                        key={t.thread_id}
                        autoFocus
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onBlur={() => submitRename(t.thread_id)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") submitRename(t.thread_id)
                          if (e.key === "Escape") setEditing(null)
                        }}
                        className="w-full rounded-md border bg-background px-2 py-1 text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
                      />
                    )
                  }
                  return (
                    <div
                      key={t.thread_id}
                      // Staggered entrance: each row fades + slides in from the
                      // right, delayed by its position (capped so a long list
                      // doesn't crawl). Runs once on mount (React reuses the keyed
                      // element), so hovering/renaming never re-triggers it.
                      className={cn(
                        "group/item relative flex items-center gap-1 rounded-md pr-1 pl-2 transition-colors duration-200 [animation-duration:280ms] [animation-fill-mode:backwards] animate-in fade-in slide-in-from-right-3 motion-reduce:animate-none",
                        active
                          ? "bg-accent text-accent-foreground"
                          : "hover:bg-accent/50"
                      )}
                      style={{ animationDelay: `${Math.min(idx, 14) * 22}ms` }}
                    >
                      {/* Active accent bar on the left edge. */}
                      {active ? (
                        <span className="absolute top-1.5 bottom-1.5 left-0 w-0.5 rounded-full bg-primary" />
                      ) : null}
                      <button
                        type="button"
                        onClick={() => onResume(t)}
                        className="min-w-0 flex-1 py-1.5 text-left"
                        title={t.title || t.thread_id}
                      >
                        <div className="truncate text-sm">
                          {t.title || "Untitled"}
                        </div>
                        <div className="truncate text-[11px] text-muted-foreground">
                          {relativeTime(t.updated_at)}
                          {t.dataset_scope
                            ? ` · @${t.dataset_scope.data_domain}/${t.dataset_scope.dataset}`
                            : ""}
                        </div>
                      </button>
                      {/* Row actions — hidden until the row is hovered (or focused
                          within, so keyboard users can reach them). */}
                      <div className="flex shrink-0 opacity-0 transition-opacity group-focus-within/item:opacity-100 group-hover/item:opacity-100">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7 text-muted-foreground hover:text-foreground"
                          aria-label="Rename"
                          onClick={() => {
                            setEditing(t.thread_id)
                            setDraft(t.title || "")
                          }}
                        >
                          <PencilIcon className="size-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7 text-muted-foreground hover:text-destructive"
                          aria-label="Delete"
                          onClick={() => remove(t.thread_id)}
                        >
                          <Trash2Icon className="size-3.5" />
                        </Button>
                      </div>
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  )
}
