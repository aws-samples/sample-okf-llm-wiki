// The report card (present_report, lifted out of the tool timeline by
// buildMessageBlocks and PINNED to the bottom of the AI turn — composing/
// create_report shows as an ordinary step in the thinking timeline). Full
// width and deliberately artifact-like: title + state line on the left, and
// a tilted mock document (stacked pages, faux text lines, a mini chart —
// pure CSS on theme tokens) bleeding off the right edge. Three states —
// preparing (subtle pulse while the call streams), error (a bad report id),
// ready (clickable → onOpenReport, threaded down from ChatPanel like
// onOpenDoc, opens the inline ReportPanel beside the transcript).

import { cn } from "@/lib/utils"

// One faux page of the document mock. `front` pages carry the sketched
// content (title line, text lines, a small bar chart); back pages are blank
// depth. Decorative only — hidden from the a11y tree by the parent.
function MockPage({ front = false, className }) {
  return (
    <div
      className={cn(
        "absolute overflow-hidden rounded-md border bg-background shadow-sm",
        "h-20 w-[4.2rem] p-2",
        className
      )}
    >
      {front ? (
        <div className="flex h-full flex-col gap-1.5">
          <div className="h-1.5 w-8 rounded-full bg-foreground/30" />
          <div className="h-1 w-full rounded-full bg-foreground/10" />
          <div className="h-1 w-4/5 rounded-full bg-foreground/10" />
          <div className="mt-auto flex items-end gap-1">
            <div className="h-2.5 w-1.5 rounded-sm bg-primary/40" />
            <div className="h-5 w-1.5 rounded-sm bg-primary/70" />
            <div className="h-3.5 w-1.5 rounded-sm bg-primary/55" />
            <div className="h-6 w-1.5 rounded-sm bg-primary/85" />
            <div className="h-2 w-1.5 rounded-sm bg-primary/40" />
          </div>
        </div>
      ) : null}
    </div>
  )
}

export function ReportCard({
  title,
  reportId,
  pending,
  error,
  isComplete,
  onOpenReport,
}) {
  const ready = Boolean(reportId) && isComplete && !error
  // Complete with neither an id nor an error = the ack never arrived (a
  // truncated stream) — dead, but not the pulsing "composing" state.
  const stalled = isComplete && !ready && !error && !pending

  return (
    <button
      type="button"
      disabled={!ready}
      onClick={() => onOpenReport?.({ reportId, title })}
      className={cn(
        "relative my-3 flex min-h-20 w-full items-center gap-4 overflow-hidden rounded-xl border bg-card px-4 py-3 text-left transition-colors",
        ready && "cursor-pointer hover:bg-muted/60",
        !ready && !error && !stalled && "animate-pulse",
        error && "border-destructive/40 bg-destructive/10"
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-foreground">
          {title || "Report"}
        </div>
        <div
          className={cn(
            "mt-1 truncate text-xs",
            error ? "text-destructive" : "text-muted-foreground"
          )}
          title={error || undefined}
        >
          {error
            ? error
            : ready
              ? "Report"
              : stalled
                ? "Report unavailable"
                : "Preparing report…"}
        </div>
      </div>

      {/* The document mock: two pages fanned + tilted, clipped by the card's
          overflow so they read as a sheet sliding out of the corner. Sized/
          positioned in absolute terms so the card's height (min-h-24) — not
          the mock — owns the layout. */}
      <div
        aria-hidden
        className="pointer-events-none relative h-16 w-[5.5rem] shrink-0 select-none"
      >
        <MockPage className="right-5 top-2 rotate-[10deg] opacity-60" />
        <MockPage front className="right-0 top-1 rotate-6" />
      </div>
    </button>
  )
}
