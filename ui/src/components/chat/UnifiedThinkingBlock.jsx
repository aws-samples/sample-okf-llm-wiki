// UnifiedThinkingBlock — ported from Sparky. The reasoning + tool "working…"
// timeline: an animated (shimmer while streaming) header that toggles a vertical
// timeline of steps. Each step is a marker on a line: a thinking step (clock
// icon + reasoning prose) or a tool step (wrench icon + status text, expandable
// to raw args/result). A completion check caps a finished block.
//
// Trimmed to the wiki chat's vocabulary: all our tools are generic wiki reads
// (read_page/grep/semantic_search/…), shown with a formatted name + status +
// expandable args/result. No web-search/sub-agent/browser/canvas categories.

import { ChevronDown, CircleCheck, ClockFading, Code2, Table2 } from "lucide-react"
import { memo, useCallback, useMemo, useState } from "react"

import { CodeView } from "@/components/chat/CodeView"
import { Markdown } from "@/components/chat/Markdown"
import { buildTimelineSteps, mergeThinkingSteps } from "@/components/chat/timelineParser"
import {
  parseToolResult,
  prettyName,
  toolIcon,
  toolLabel,
} from "@/components/chat/wikiTools"
import { Marker, MarkerContent } from "@/components/ui/marker"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"

import "@/components/chat/UnifiedThinkingBlock.css"

// Coerce tool content (object / JSON string / string) to pretty text (raw fallback).
function coerceToolText(content) {
  if (content == null) return ""
  if (typeof content === "string") {
    const t = content.trim()
    if (
      (t.startsWith("{") && t.endsWith("}")) ||
      (t.startsWith("[") && t.endsWith("]"))
    ) {
      try {
        return JSON.stringify(JSON.parse(t), null, 2)
      } catch {
        return content
      }
    }
    return content
  }
  try {
    return JSON.stringify(content, null, 2)
  } catch {
    return String(content)
  }
}

// The structured detail body for a completed tool, keyed off its parsed result.
function ToolResultDetail({ view, rawContent }) {
  if (!view) return null

  // Every list tool renders as a real shadcn Table (columns + rows). The shared
  // `.okf-label-grid` class (index.css) makes each cell a gapped, zebra-tinted
  // label block and owns the padded thin scrollbar — same look as md tables.
  if (view.kind === "table") {
    if (!view.rows.length) return null
    return (
      <div className="okf-label-grid mt-2 max-h-[280px] max-w-[640px]">
        <Table>
          <TableHeader>
            <TableRow>
              {view.columns.map((c) => (
                <TableHead
                  key={c.key}
                  className={cn(c.align === "right" && "text-right")}
                >
                  {c.header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {view.rows.map((row, i) => (
              <TableRow key={i}>
                {view.columns.map((c) => (
                  <TableCell
                    key={c.key}
                    title={typeof row[c.key] === "string" ? row[c.key] : undefined}
                    className={cn(
                      c.mono && "font-mono",
                      c.align === "right" && "text-right tabular-nums",
                      c.wrap
                        ? "whitespace-normal! text-muted-foreground"
                        : "max-w-[280px] truncate"
                    )}
                  >
                    {row[c.key]}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    )
  }

  if (view.kind === "chips") {
    if (!view.items.length) return null
    return (
      <div className="tool-result-chips">
        {view.items.map((c, i) => (
          <span key={i} className="tool-result-chip">
            {c}
          </span>
        ))}
      </div>
    )
  }
  if (view.kind === "raw") {
    const txt = coerceToolText(rawContent)
    if (!txt) return null
    return (
      <div className="tool-result-raw">
        <div className="tool-result-raw-header">result</div>
        <div className="tool-result-raw-scroll">
          <pre className="tool-result-raw-content">{txt}</pre>
        </div>
      </div>
    )
  }
  return null
}

// run_sql detail: Data / Query TABS. Data (the result table or error) shows by
// default; Query reveals the exact executed SQL in a read-only CodeView. Both are
// always available once complete — inspecting the query matters even for a
// zero-row or errored run. The CodeView is forced full-width so it matches the
// result table's width rather than shrinking to the query's length.
function SqlResultTabs({ sql, resultNode }) {
  const query = typeof sql === "string" ? sql.trim() : ""
  return (
    <Tabs
      defaultValue="data"
      className="tool-result-sql-tabs mt-2 max-w-[640px] gap-1.5"
    >
      <TabsList variant="line" className="h-7">
        <TabsTrigger value="data" className="gap-1 text-xs">
          <Table2 className="size-3.5" />
          Data
        </TabsTrigger>
        <TabsTrigger value="query" className="gap-1 text-xs">
          <Code2 className="size-3.5" />
          Query
        </TabsTrigger>
      </TabsList>
      <TabsContent value="data">{resultNode}</TabsContent>
      <TabsContent value="query">
        <CodeView code={query} language="sql" className="okf-codeview-full" />
      </TabsContent>
    </Tabs>
  )
}

// A tool step's header + expandable detail, rendered per-tool (Sparky renders
// web_search vs generic distinctly; we render each wiki tool by its shape). The
// header is a shadcn Marker: a text-shimmer MarkerContent while running, and a
// chevron when there's detail. (The tool's icon lives on the timeline marker,
// not in the header.)
function ToolResultIndicator({ toolName, input, content, isComplete, error }) {
  const [expanded, setExpanded] = useState(false)

  const view = useMemo(
    () => (isComplete && !error ? parseToolResult(toolName, content) : null),
    [toolName, content, isComplete, error]
  )

  // Header text: while running, the tool+args label; done, that label + a
  // result summary ("Searched “races” · 12 results"); on error, "… failed".
  const label = toolLabel(toolName, input, !isComplete)
  let headerText
  if (error) headerText = `${prettyName(toolName)} failed`
  else if (!isComplete) headerText = label
  else headerText = view?.summary ? `${label} · ${view.summary}` : label

  // run_sql always gets its executed-query disclosure once complete — even a
  // zero-row or errored query is worth inspecting.
  const sqlQuery =
    toolName === "run_sql" && isComplete && input && typeof input === "object"
      ? input.sql
      : null

  const hasDetail =
    isComplete &&
    ((view && view.kind === "table" && view.rows?.length) ||
      (view && view.kind === "chips" && view.items?.length) ||
      (view && view.kind === "raw" && content != null) ||
      Boolean(sqlQuery) ||
      error)

  const header = (
    <Marker
      {...(hasDetail
        ? { asChild: true }
        : { className: "cursor-default" })}
    >
      {hasDetail ? (
        <button type="button" onClick={() => setExpanded((v) => !v)}>
          <MarkerContent>{headerText}</MarkerContent>
          <ChevronDown
            className={cn(
              "ml-auto size-3.5 shrink-0 text-muted-foreground transition-transform",
              expanded && "rotate-180"
            )}
          />
        </button>
      ) : (
        <MarkerContent className={cn(!isComplete && "text-shimmer")}>
          {headerText}
        </MarkerContent>
      )}
    </Marker>
  )

  // The result body (a table/chips/raw view, or the error block) — shared by the
  // plain path and the run_sql "Data" tab.
  const resultNode = error ? (
    <div className="tool-result-raw">
      <div className="tool-result-raw-header">error</div>
      <div className="tool-result-raw-scroll">
        <pre className="tool-result-raw-content">{coerceToolText(content)}</pre>
      </div>
    </div>
  ) : (
    <ToolResultDetail view={view} rawContent={content} />
  )

  return (
    <div className="tool-result-content">
      {header}
      {hasDetail ? (
        <div className={`tool-result-expand ${expanded ? "expanded" : ""}`}>
          <div>
            {/* run_sql: Data / Query tabs (Data default). Other tools: the result
                body directly. */}
            {sqlQuery ? (
              <SqlResultTabs sql={sqlQuery} resultNode={resultNode} />
            ) : (
              resultNode
            )}
          </div>
        </div>
      ) : null}
    </div>
  )
}

const ThinkingStep = memo(function ThinkingStep({ segments, isLast }) {
  return (
    <div className={`timeline-item ${isLast ? "last" : ""}`}>
      <div className="timeline-marker">
        <ClockFading size={18} className="timeline-icon thinking-icon" />
      </div>
      <div className="timeline-content">
        <div className="timeline-thinking-content">
          {segments.map((seg, i) => (
            <div key={i} className="thinking-segment">
              <Markdown>{seg?.content || ""}</Markdown>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
})

const ToolStep = memo(function ToolStep({ step, isLast }) {
  const Icon = toolIcon(step.toolName)
  return (
    <div className={`timeline-item ${isLast ? "last" : ""}`}>
      <div className="timeline-marker">
        <Icon size={18} className="timeline-icon tool-icon" />
      </div>
      <div className="timeline-content">
        <ToolResultIndicator
          toolName={step.toolName}
          input={step.toolInput}
          content={step.toolContent}
          isComplete={step.isToolComplete}
          error={step.toolError}
        />
      </div>
    </div>
  )
})

function CompletionStep() {
  return (
    <div className="timeline-item last completion-step">
      <div className="timeline-marker">
        <CircleCheck size={18} className="timeline-icon completion-icon" />
      </div>
    </div>
  )
}

// The header label mirrors Sparky: current activity while streaming, settled
// state when done. While a tool runs, reflect it (e.g. "Searching “races”").
function headerLabel(steps, isComplete) {
  if (steps.length === 0) return "Reasoning"
  const last = steps[steps.length - 1]
  if (isComplete) return "Reasoning"
  if (last.type === "thinking") return "Reasoning"
  return toolLabel(last.toolName, last.toolInput, !last.isToolComplete)
}

export function UnifiedThinkingBlock({ contentBlocks = [], isGroupComplete = false }) {
  const [expanded, setExpanded] = useState(false)

  // Signature over the segments so mergedSteps recomputes when ANYTHING changes:
  // a new segment, a tool completing, a tool's content growing. The OLD key used
  // only `s.content?.length`/`s.isComplete`, which are undefined on TOOL segments
  // (their state lives in .toolName/.id/.input/.content) — so a second tool, or a
  // tool's result arriving, didn't change the key and the memo returned the STALE
  // timeline: earlier tools "overwritten"/dropped. This captures tool identity.
  const contentKey = useMemo(
    () =>
      contentBlocks
        .map((s, i) =>
          s.type === "text"
            ? `${i}:x${s.content?.length || 0}`
            : `${i}:${s.id || ""}:${s.toolName || ""}:${s.isComplete ? 1 : 0}:${s.error ? 1 : 0}`
        )
        .join("|"),
    [contentBlocks]
  )

  const mergedSteps = useMemo(
    () => mergeThinkingSteps(buildTimelineSteps(contentBlocks)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [contentKey]
  )

  const label = useMemo(
    () => headerLabel(mergedSteps, isGroupComplete),
    [mergedSteps, isGroupComplete]
  )

  const toggle = useCallback(() => setExpanded((v) => !v), [])

  if (mergedSteps.length === 0) return null

  return (
    <div className="unified-thinking-block">
      <div className="unified-block-header">
        <Marker asChild>
          <button type="button" onClick={toggle} aria-expanded={expanded}>
            <MarkerContent className={cn(!isGroupComplete && "text-shimmer")}>
              {label}
            </MarkerContent>
            <ChevronDown
              className={cn(
                "ml-auto size-4 shrink-0 text-muted-foreground transition-transform",
                expanded && "rotate-180"
              )}
            />
          </button>
        </Marker>
      </div>

      <div className={`unified-content-container ${expanded ? "expanded" : "collapsed"}`}>
        <div className="unified-content-wrapper">
          <div className={`timeline ${isGroupComplete ? "timeline-complete" : ""}`}>
            <div className="timeline-line" />
            {mergedSteps.map((step, i) => {
              const isLast = !isGroupComplete && i === mergedSteps.length - 1
              if (step.type === "thinking") {
                return <ThinkingStep key={step.id} segments={step.segments} isLast={isLast} />
              }
              return <ToolStep key={step.id} step={step} isLast={isLast} />
            })}
            {isGroupComplete ? <CompletionStep /> : null}
          </div>
        </div>
      </div>
    </div>
  )
}
