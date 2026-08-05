// The solver-trace panel: one solver's step-by-step run — its reasoning, the tool
// calls it made, the docs it opened and what they returned — read out of a
// benchmark report's traces document. Used by the Benchmark report page.

import { useMemo, useState } from "react"
import hljs from "highlight.js/lib/common"
import {
  BrainIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  FileTextIcon,
  ListTreeIcon,
  MessageSquareTextIcon,
  WrenchIcon,
  XIcon,
} from "lucide-react"

import { Markdown } from "@/components/chat/Markdown"
import { cn } from "@/lib/utils"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { CopyButton } from "@/components/ui/copy-button"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"

// The side panel: how ONE bundle-blind solver got to its SQL — its reasoning, the
// globs/greps it ran, the docs it opened, and what those returned. This is the same
// trace the adjudicator reads when it decides whether a failure was a real wiki gap,
// so a human can check that call.
export function SolverTracePanel({
  q,
  traces,
  onClose,
  onResizeStart,
  resizing = false,
}) {
  const trace = traces.status === "ok" ? traces.byId[q.q_id] : null
  const steps = Array.isArray(trace?.steps) ? trace.steps : []
  const files = Array.isArray(trace?.files_read) ? trace.files_read : []

  return (
    // The same surface + resize affordance as the chat page's doc peek
    // (PanelShell): a drag grip in a left gutter, then a rounded full-height
    // card — so every side panel in the app reads and behaves alike.
    <div
      className={cn(
        "relative flex h-full min-h-0 min-w-0 flex-col",
        onResizeStart ? "pl-2.5" : "pl-3"
      )}
    >
      {onResizeStart ? (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize panel"
          onPointerDown={onResizeStart}
          className="group absolute inset-y-0 left-0 z-10 flex w-2.5 cursor-col-resize touch-none items-center justify-center select-none"
        >
          <div
            className={cn(
              "h-10 w-[3px] rounded-full transition-colors duration-150",
              resizing ? "bg-primary" : "bg-border group-hover:bg-primary/60"
            )}
          />
        </div>
      ) : null}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-xl border bg-card shadow-sm">
      <div className="flex items-start gap-2 border-b px-3 py-2">
        <ListTreeIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">Solver steps</p>
          <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground break-words">
            {q.question || "—"}
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onClose}
          aria-label="Close solver steps"
          className="shrink-0"
        >
          <XIcon />
        </Button>
      </div>

      {traces.status === "loading" ? (
        <div className="flex items-center gap-2 p-3 text-sm text-muted-foreground">
          <Spinner className="size-4" /> Loading steps…
        </div>
      ) : traces.status === "error" ? (
        <div className="p-3">
          <Alert variant="destructive">
            <AlertTitle>Couldn’t load the steps</AlertTitle>
            <AlertDescription>{traces.error}</AlertDescription>
          </Alert>
        </div>
      ) : !trace ? (
        <p className="p-3 text-sm text-muted-foreground">
          No trace was recorded for this question.
        </p>
      ) : (
        <div className="okf-thin-scroll min-h-0 flex-1 overflow-y-auto p-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground tabular-nums">
            <span>{trace.turns ?? 0} turns</span>
            <span>{trace.tool_calls ?? 0} tool calls</span>
            <span>
              {files.length} file{files.length === 1 ? "" : "s"} read
            </span>
          </div>

          {trace.error ? (
            <Alert variant="destructive" className="mt-2">
              <AlertTitle>The solver run errored</AlertTitle>
              <AlertDescription className="break-words">
                {trace.error}
              </AlertDescription>
            </Alert>
          ) : null}

          {files.length ? (
            <div className="mt-2 flex flex-wrap gap-1">
              {files.map((f) => (
                <span
                  key={f}
                  className="inline-flex min-w-0 items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                >
                  <FileTextIcon className="size-3 shrink-0" />
                  <span className="truncate">{f}</span>
                </span>
              ))}
            </div>
          ) : (
            // The "answered blind" signature: it never opened a doc, so this failure
            // says nothing about whether the docs were right.
            <p className="mt-2 text-xs text-muted-foreground">
              This solver opened no wiki files.
            </p>
          )}

          <Separator className="my-3" />

          {steps.length ? (
            <ol className="flex min-w-0 flex-col gap-2">
              {steps.map((s, i) => (
                <TraceStepRow key={i} step={s} />
              ))}
            </ol>
          ) : (
            <p className="text-sm text-muted-foreground">
              No steps were captured.
            </p>
          )}

          {trace.truncated ? (
            <p className="mt-3 text-xs text-muted-foreground">
              Trace truncated — only the first steps of a long run are kept.
            </p>
          ) : null}
        </div>
      )}
      </div>
    </div>
  )
}

// One step of a solver trace. Reasoning and answer text render inline (they're
// capped server-side); a tool RESULT collapses to one line, since what it read
// matters more at a glance than the bytes it got back.
function TraceStepRow({ step }) {
  const [expanded, setExpanded] = useState(false)
  const text = typeof step?.text === "string" ? step.text : ""

  if (step?.kind === "tool_call") {
    return (
      <li className="flex min-w-0 items-start gap-1.5 text-xs">
        <WrenchIcon className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
        <span className="min-w-0 font-mono break-words">
          <span className="text-foreground">{step.name || "tool"}</span>
          {step.args && Object.keys(step.args).length ? (
            <span className="text-muted-foreground">
              {"("}
              {Object.entries(step.args)
                .map(([k, v]) => `${k}=${v}`)
                .join(", ")}
              {")"}
            </span>
          ) : null}
        </span>
      </li>
    )
  }

  if (step?.kind === "tool_result") {
    // Indented under the call it answers, collapsed to its first line.
    return (
      <li className="min-w-0 pl-4">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="flex w-full min-w-0 items-start gap-1.5 text-left text-xs text-muted-foreground hover:text-foreground"
        >
          {expanded ? (
            <ChevronDownIcon className="mt-0.5 size-3 shrink-0" />
          ) : (
            <ChevronRightIcon className="mt-0.5 size-3 shrink-0" />
          )}
          <span className="min-w-0 flex-1 truncate font-mono">
            {text ? firstLine(text) : "(empty result)"}
          </span>
        </button>
        {expanded && text ? (
          <pre className="okf-thin-scroll mt-1 ml-4 max-h-48 overflow-auto rounded border bg-muted p-2 font-mono text-[11px] whitespace-pre-wrap break-words">
            {text}
            {step.truncated ? "\n…" : ""}
          </pre>
        ) : null}
      </li>
    )
  }

  const thinking = step?.kind === "thinking"
  // Reasoning and said text render through the SAME Markdown stack as the chat
  // transcript (okf-prose typography, CodeView'd fences, concept-id pills) — a
  // trace step is agent output like any chat message, so it gets the same
  // rendering, not a bespoke one. Thinking is dimmed (okf-prose hardcodes
  // text-foreground, so an opacity wash mutes it without fighting the theme).
  // A step TRUNCATED mid-fence still renders: an unterminated fence parses as
  // code to the end.
  return (
    <li className="flex min-w-0 items-start gap-1.5">
      {thinking ? (
        <BrainIcon className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
      ) : (
        <MessageSquareTextIcon className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
      )}
      {/* -mt-1 cancels okf-prose's relaxed line-height half-leading: the prose's
          first LINE BOX starts above its glyphs, so without this the text's cap
          height sits visibly below the icon (the tool-call rows, which are plain
          text, align correctly on their own). */}
      <div className={cn("-mt-1 min-w-0 flex-1", thinking && "opacity-70")}>
        <Markdown>{text}</Markdown>
        {step?.truncated ? (
          <p className="text-xs text-muted-foreground">…</p>
        ) : null}
      </div>
    </li>
  )
}

function firstLine(text) {
  const [line] = String(text).split("\n")
  return line.length > 160 ? `${line.slice(0, 160)}…` : line
}

export function SqlBlock({ label, sql }) {
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
