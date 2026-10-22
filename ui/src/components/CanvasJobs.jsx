// Canvas-jobs indicator + drawer, scoped to ONE chat conversation.
//
// Jobs are canvas CI runs the chat agent delegated (create/revise canvas).
// The pieces are split so ChatPanel can host them in its side-panel chrome:
// `useCanvasJobs` owns the polling, `CanvasJobsButton` is the small ghost
// trigger (ChatPanel hides it whenever ANY side panel is open), and
// `CanvasJobsPanel` renders inside the SAME PanelShell clip as the doc peek /
// history drawers. A single job opens straight into its detail; several open
// as a list, with a top-left back button from detail to list.
//
// A job shows its TITLE only (the calling agent names the canvas — never the
// raw request prompt), and its activity as the chat transcript's thinking
// timeline, CHROMELESS (no collapsible header) and with the same curated
// per-tool icons + labels (wikiTools presentation, driven by the feed's tool
// name + sanitized args). Steps carry no result payloads, so nothing expands.

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowLeftIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  LayoutDashboardIcon,
  Loader2Icon,
  XCircleIcon,
  XIcon,
} from "lucide-react"

import { UnifiedThinkingBlock } from "@/components/chat/UnifiedThinkingBlock"

const ACTIVE = new Set(["queued", "running"])
const POLL_MS = 5000

// Fired from the transcript the moment a delegation call appears in the
// stream — the jobs hook refreshes immediately instead of waiting for a
// reload to discover the new row.
export const CANVAS_JOBS_EVENT = "okf:canvas-jobs-changed"

export function useCanvasJobs(api, threadId, open) {
  const [jobs, setJobs] = useState([])
  const timer = useRef(null)

  const refresh = useCallback(() => {
    if (!threadId) return
    api
      .listCanvasJobs(false, threadId)
      .then((all) => setJobs(all || []))
      .catch(() => {}) // transient poll failures are silent; the next tick retries
  }, [api, threadId])

  // New conversation (or thread switch): reset and re-check.
  useEffect(() => {
    setJobs([])
    refresh()
  }, [threadId, refresh])

  const activeCount = jobs.filter((j) => ACTIVE.has(j.status)).length

  // Poll while anything runs OR the drawer is open; go quiet otherwise.
  useEffect(() => {
    clearInterval(timer.current)
    if (threadId && (activeCount > 0 || open)) {
      timer.current = setInterval(refresh, POLL_MS)
    }
    return () => clearInterval(timer.current)
  }, [threadId, activeCount, open, refresh])

  // A delegation call in the live transcript: refresh now (the tool writes
  // the queued row before its ack), so the trigger appears without a reload.
  useEffect(() => {
    window.addEventListener(CANVAS_JOBS_EVENT, refresh)
    return () => window.removeEventListener(CANVAS_JOBS_EVENT, refresh)
  }, [refresh])

  return { jobs, activeCount }
}

function jobTitle(job) {
  // Models occasionally emit HTML-escaped titles ("A &amp; B") — decode the
  // common entities so chrome text reads as intended. One pass, so a literal
  // "&amp;lt;" decodes to "&lt;" rather than being double-unescaped to "<".
  return String(job.title || "Canvas").replace(
    /&(amp|lt|gt|quot|#39);/g,
    (_, e) => TITLE_ENTITIES[e]
  )
}

const TITLE_ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" }

function StatusIcon({ status }) {
  if (ACTIVE.has(status))
    return <Loader2Icon className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
  if (status === "completed")
    return <CheckCircle2Icon className="h-4 w-4 shrink-0 text-emerald-500" />
  return <XCircleIcon className="h-4 w-4 shrink-0 text-destructive" />
}

export function CanvasJobsButton({ jobs, hidden, onClick }) {
  // Only a chat that HAS canvas jobs shows the indicator — and never while a
  // side panel (citations, history, or this drawer itself) is open. No count,
  // no spinner: just the icon, entering with a soft fade/zoom.
  if (hidden || jobs.length === 0) return null
  return (
    <button
      className="absolute right-4 top-4 z-20 rounded-md p-2 text-muted-foreground animate-in fade-in-0 zoom-in-75 duration-300 hover:bg-foreground/10 hover:text-foreground"
      onClick={onClick}
      title="Canvas jobs"
    >
      <LayoutDashboardIcon className="h-5 w-5" />
    </button>
  )
}

function JobDetail({ job, onOpenCanvas }) {
  const running = ACTIVE.has(job.status)
  // The feed's steps as chat-timeline segments: tool steps carry (name,
  // sanitized args) for the curated labels; the agent's final text renders
  // as a plain reasoning-text step. No result payloads → nothing expands.
  const blocks = useMemo(() => {
    // The job itself is the timeline's FIRST step (title + status via the
    // curated `canvas_job` presentation) — not a separate header above it.
    const out = [
      {
        type: "tool",
        id: "job-head",
        toolName: "canvas_job",
        input: { title: jobTitle(job), status: job.status },
        isComplete: !ACTIVE.has(job.status),
      },
    ]
    for (const s of (job.steps || []).filter(
      (x) => x.type === "tool" || x.type === "text"
    )) {
      const prev = out[out.length - 1]
      if (s.type === "tool" && prev?.type === "tool" && prev.toolName === s.label) {
        // Consecutive calls to the SAME tool (the validate-fix loop) collapse
        // into one step with a ×N badge — the latest args win the label.
        prev.count = (prev.count || 1) + 1
        prev.input = s.input
        continue
      }
      out.push(
        s.type === "tool"
          ? {
              type: "tool",
              id: `s${out.length}`,
              toolName: s.label,
              input: s.input,
              isComplete: true,
            }
          : { type: "text", content: s.label }
      )
    }
    return out
  }, [job])
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-auto p-3">
      {job.error && <div className="text-xs text-destructive">{job.error}</div>}
      {blocks.length > 0 && (
        // The timeline's marker pads mask the vertical line with the PAGE bg
        // by default; inside this bg-card drawer that reads as grey boxes.
        // The variable is declared ON .unified-thinking-block, so an inline
        // ancestor style loses — the .unified-on-card host class re-points it
        // (see UnifiedThinkingBlock.css).
        <div className="unified-on-card">
          <UnifiedThinkingBlock
            contentBlocks={blocks}
            isGroupComplete={!running}
            chromeless
          />
        </div>
      )}
      {job.status === "completed" && job.canvas_id && (
        <button
          className="self-start rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90"
          onClick={() => onOpenCanvas(job.canvas_id)}
        >
          Open canvas
        </button>
      )}
    </div>
  )
}

export function CanvasJobsPanel({ jobs, onOpenCanvas, onClose }) {
  // null = the list; a job_id = that job's detail. A single job skips the
  // list entirely — there is nothing to choose.
  const [selected, setSelected] = useState(null)
  const only = jobs.length === 1 ? jobs[0] : null
  const job = only || jobs.find((j) => j.job_id === selected) || null

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-1 border-b px-2 py-2">
        {job && !only ? (
          <button
            className="rounded-md p-1 text-muted-foreground hover:bg-foreground/10 hover:text-foreground"
            onClick={() => setSelected(null)}
            title="Back to jobs"
          >
            <ArrowLeftIcon className="h-4 w-4" />
          </button>
        ) : null}
        <div className="min-w-0 flex-1 truncate px-1 text-sm font-semibold">
          {job ? jobTitle(job) : "Canvas jobs"}
        </div>
        <button
          className="rounded-md p-1 text-muted-foreground hover:bg-foreground/10 hover:text-foreground"
          onClick={onClose}
          title="Close"
        >
          <XIcon className="h-4 w-4" />
        </button>
      </div>
      {job ? (
        <JobDetail job={job} onOpenCanvas={onOpenCanvas} />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto p-2">
          {jobs.map((j) => (
            <button
              key={j.job_id}
              className="flex items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-foreground/5"
              onClick={() => setSelected(j.job_id)}
            >
              <StatusIcon status={j.status} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm">{jobTitle(j)}</div>
                <div className="truncate text-xs text-muted-foreground">
                  {j.data_domain}/{j.dataset} · {j.status}
                </div>
              </div>
              <ChevronRightIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
