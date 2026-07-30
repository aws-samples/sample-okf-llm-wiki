// The benchmark report's annotation review surface — a side panel (not a modal),
// so the report stays readable beside it while you edit: the judge's finding for
// a note and the note itself are the same decision, and a dialog hid the
// evidence behind it.
//
// Each row is one draft annotation: its TARGET on top (a concept id, or
// dataset-wide) then a generous editable body. Deliberately MONOCHROME — every
// row is selected by default and the notes are all equally real, so tinting
// them primary just made a wall of teal; the checkbox and the border carry
// selection, and the accent is spent on the one action button instead.
//
// The de-identification boundary is HUMAN review: nothing is filed until it is
// selected here, so this panel never auto-applies.

import { useState } from "react"
import { toast } from "sonner"
import { FileTextIcon, SparklesIcon, XIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { loadPreference } from "@/lib/harvestModels"
import { cn } from "@/lib/utils"

export function AnnotationsPanel({
  api,
  domain,
  dataset,
  reportId,
  finals,
  onClose,
  onResizeStart,
  resizing = false,
}) {
  const [drafts, setDrafts] = useState(() =>
    finals.map((f) => ({
      note: f.note || "",
      concept_id: f.concept_id || "",
      selected: true,
    }))
  )
  const [applying, setApplying] = useState(false)
  const [created, setCreated] = useState(null) // the apply response
  const [startingHarvest, setStartingHarvest] = useState(false)

  const locked = Boolean(created)
  const selectedCount = drafts.filter((d) => d.selected && d.note.trim()).length
  const allSelected = drafts.length > 0 && selectedCount === drafts.length

  const toggleAll = () =>
    setDrafts((prev) => prev.map((d) => ({ ...d, selected: !allSelected })))

  const apply = async () => {
    setApplying(true)
    try {
      const annotations = drafts
        .filter((d) => d.selected && d.note.trim())
        .map((d) => ({ note: d.note.trim(), concept_id: d.concept_id }))
      const res = await api.applyReportAnnotations(
        domain,
        dataset,
        reportId,
        annotations
      )
      setCreated(res)
      toast.success(`${res.count} annotation${res.count === 1 ? "" : "s"} filed.`)
    } catch (e) {
      toast.error(`Could not apply: ${e.message || e}`)
    } finally {
      setApplying(false)
    }
  }

  const startHarvest = async () => {
    setStartingHarvest(true)
    try {
      const ids = (created?.created || []).map((a) => a.annotation_id)
      // Same picker preference the Harvest tab saves/reads (localStorage) — this
      // panel has no picker of its own, so an operator's chosen model/effort for
      // this dataset carries over instead of the run silently falling back to
      // the runtime's deploy-time default.
      const pref = loadPreference()
      // NO scope: the run applies exactly the ids filed above. The aggregator
      // legitimately targets external/ pages too, and a "dataset"-scoped run
      // silently dropped those notes (they stayed open while the toast claimed
      // success). Unscoped, the handler also derives the counterpart Glue DBs
      // from external concept ids, so cross claims stay verifiable.
      const res = await api.runAnnotationHarvest(
        domain,
        dataset,
        undefined,
        ids,
        undefined,
        pref.model,
        pref.effort,
        pref.subagentModel,
        pref.subagentModel ? pref.subagentEffort : "",
        pref.reviewerModel,
        pref.reviewerModel ? pref.reviewerEffort : ""
      )
      if (res?.skipped) {
        // The pre-flight found nothing live to apply (e.g. every filed note was
        // auto-resolved as orphaned) — no run started; don't claim one did.
        toast.info("Nothing to apply — no annotation harvest was started.")
      } else {
        toast.success(
          "Annotation harvest started — the fixes fold into the wiki; benchmark the new version to prove the delta."
        )
      }
      onClose()
    } catch (e) {
      toast.error(`Could not start the harvest: ${e.message || e}`)
    } finally {
      setStartingHarvest(false)
    }
  }

  return (
    // Same shell as the solver-steps panel: a drag grip in a left gutter, then
    // the rounded full-height card.
    <div
      className={cn(
        "relative flex h-full flex-col",
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

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border bg-card shadow-sm">
        <div className="flex items-start gap-2 border-b px-3 py-2">
          <SparklesIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">Wiki annotations</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              The aggregator consolidated the judge’s findings into these doc
              fixes. Select and edit the ones to file.
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="Close annotations"
            className="shrink-0"
          >
            <XIcon />
          </Button>
        </div>

        <div className="flex items-center justify-between gap-2 px-3 py-1.5">
          <span className="text-xs text-muted-foreground tabular-nums">
            {selectedCount} of {drafts.length} selected
          </span>
          {!locked ? (
            <Button variant="ghost" size="sm" onClick={toggleAll}>
              {allSelected ? "Unselect all" : "Select all"}
            </Button>
          ) : null}
        </div>
        <Separator />

        <div className="okf-thin-scroll min-h-0 flex-1 overflow-y-auto p-3">
          <div className="flex flex-col gap-2">
            {drafts.map((d, i) => (
              <div
                key={i}
                className={cn(
                  "flex shrink-0 flex-col gap-1.5 rounded-xl border px-3 py-2.5",
                  // Monochrome: selection reads through the checkbox + a muted
                  // fill, never a colored border.
                  d.selected ? "bg-muted/40" : "opacity-60"
                )}
              >
                {/* TARGET FIRST: which doc this note lands on frames the note
                    you're about to read, so it leads instead of trailing. */}
                <label className="flex cursor-pointer items-center gap-2">
                  <input
                    type="checkbox"
                    className="size-3.5 shrink-0 accent-muted-foreground"
                    checked={d.selected}
                    disabled={locked}
                    onChange={() =>
                      setDrafts((prev) =>
                        prev.map((x, j) =>
                          j === i ? { ...x, selected: !x.selected } : x
                        )
                      )
                    }
                  />
                  <FileTextIcon className="size-3 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground">
                    {d.concept_id || "dataset-wide"}
                  </span>
                </label>
                <Textarea
                  value={d.note}
                  disabled={locked}
                  onChange={(e) =>
                    setDrafts((prev) =>
                      prev.map((x, j) =>
                        j === i ? { ...x, note: e.target.value } : x
                      )
                    )
                  }
                  // Generous by default — these are paragraph-length doc fixes,
                  // and field-height editing made them unreadable.
                  className="min-h-32 resize-y bg-background text-sm"
                />
              </div>
            ))}
          </div>
        </div>

        <Separator />
        <div className="flex items-center justify-end gap-2 px-3 py-2">
          {locked ? (
            <Button onClick={startHarvest} disabled={startingHarvest}>
              {startingHarvest ? <Spinner data-icon="inline-start" /> : null}
              Start annotation harvest now
            </Button>
          ) : (
            <Button onClick={apply} disabled={applying || selectedCount === 0}>
              {applying ? <Spinner data-icon="inline-start" /> : null}
              File {selectedCount} annotation{selectedCount === 1 ? "" : "s"}
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
