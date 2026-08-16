import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  CheckCircle2Icon,
  CopyIcon,
  ExternalLinkIcon,
  FileTextIcon,
  MessageSquarePlusIcon,
  PlayIcon,
  TextSelectIcon,
  Trash2Icon,
  XCircleIcon,
} from "lucide-react"
import { toast } from "sonner"

import { cn } from "@/lib/utils"
import { captureSelection } from "@/lib/annotationAnchor"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"

// The annotation feature for BrowseView, kept in one file so the Browse diff
// stays small. Two pieces that share the annotations list + refresh:
//  - <SelectionAnnotator>: wraps the rendered doc in a RIGHT-CLICK context
//    menu (Annotate — first, selection-only — then Copy / Open Link / Open
//    Link in New Tab / Select All); Annotate opens a modal note composer.
//    (Replaced the old select-then-popover: it fired on EVERY selection,
//    which punished plain copy-selecting.)
//  - <AnnotationSidebar>: lists the caller's annotations for this dataset and
//    runs the annotation-mode re-harvest.
// A shared hook owns fetch/create/delete so both stay in sync.

export function useAnnotations(api, domain, dataset) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!domain || !dataset) return
    setLoading(true)
    setError(null)
    try {
      const list = await api.listAnnotations(domain, dataset)
      setItems(Array.isArray(list) ? list : [])
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }, [api, domain, dataset])

  useEffect(() => {
    load()
  }, [load])

  return { items, loading, error, reload: load }
}

// Compact sizing for the doc context menu's items — the vendored default
// (min-h-7 / text-sm / size-4 icons) reads oversized for a browser-style
// right-click menu; tailwind-merge drops the conflicting defaults.
const _menuItem = "min-h-6 py-1 text-xs [&_svg:not([class*='size-'])]:size-3.5"

// Wrap the rendered doc in a right-click CONTEXT MENU. Annotate leads the
// menu when the right-click happened over a text selection; the standard
// browser affordances the native menu would have offered (Copy, Open Link,
// Open Link in New Tab, Select All) follow, so hijacking contextmenu costs
// the reader nothing.
export function SelectionAnnotator({
  api,
  domain,
  dataset,
  conceptId,
  onCreated,
  children,
}) {
  const containerRef = useRef(null)
  // Menu context, snapshotted AT right-click time: Radix opens the menu on
  // the same contextmenu event and focusing it can collapse the live
  // selection, so the anchor (quote/prefix/suffix/block_line), the raw
  // selection text (for Copy), and the link under the cursor are all frozen
  // before the menu renders.
  const [anchor, setAnchor] = useState(null)
  const [selectionText, setSelectionText] = useState("")
  const [linkTarget, setLinkTarget] = useState(null)
  const [composerOpen, setComposerOpen] = useState(false)
  const [note, setNote] = useState("")
  const [saving, setSaving] = useState(false)

  const onContextMenu = useCallback((e) => {
    setAnchor(captureSelection(containerRef.current))
    setSelectionText(window.getSelection()?.toString() ?? "")
    const a = e.target instanceof Element ? e.target.closest("a[href]") : null
    setLinkTarget(a && containerRef.current?.contains(a) ? a : null)
  }, [])

  const copySelection = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(selectionText)
    } catch {
      toast.error("Could not copy the selection")
    }
  }, [selectionText])

  const selectAll = useCallback(() => {
    const root = containerRef.current
    if (!root) return
    const range = document.createRange()
    range.selectNodeContents(root)
    const sel = window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
  }, [])

  // Open Link CLICKS the real anchor so a concept link keeps its SPA
  // navigation (the markdown renderer's onClick), while an external link
  // follows its href; new-tab always goes through window.open on the
  // resolved absolute href (a concept link's #hash lands the app there).
  const openLink = useCallback(() => linkTarget?.click(), [linkTarget])
  const openLinkNewTab = useCallback(() => {
    if (linkTarget?.href)
      window.open(linkTarget.href, "_blank", "noopener,noreferrer")
  }, [linkTarget])

  const submit = useCallback(async () => {
    const text = note.trim()
    if (!text || !anchor) return
    setSaving(true)
    try {
      await api.createAnnotation(domain, dataset, conceptId, text, {
        quote: anchor.quote,
        prefix: anchor.prefix,
        suffix: anchor.suffix,
        block_line: anchor.block_line,
      })
      toast.success("Annotation added")
      setComposerOpen(false)
      setAnchor(null)
      setNote("")
      onCreated?.()
    } catch (e) {
      toast.error(`Could not save annotation: ${e.message || e}`)
    } finally {
      setSaving(false)
    }
  }, [api, domain, dataset, conceptId, note, anchor, onCreated])

  return (
    <>
      <ContextMenu>
        {/* select-text OVERRIDES the trigger wrapper's baked-in select-none
            (tailwind-merge drops the conflict) — this wraps the whole doc,
            and Annotate/Copy exist precisely to act on a text selection. */}
        <ContextMenuTrigger asChild className="select-text">
          {/* asChild merges this onContextMenu with Radix's own — ours runs
              first, so the snapshot lands before the menu opens. */}
          <div ref={containerRef} onContextMenu={onContextMenu}>
            {children}
          </div>
        </ContextMenuTrigger>
        <ContextMenuContent className="w-44">
          {anchor && (
            <>
              <ContextMenuItem
                className={_menuItem}
                onSelect={() => {
                  setNote("")
                  setComposerOpen(true)
                }}
              >
                <MessageSquarePlusIcon />
                Annotate
              </ContextMenuItem>
              <ContextMenuSeparator />
            </>
          )}
          <ContextMenuItem
            className={_menuItem}
            disabled={!selectionText}
            onSelect={copySelection}
          >
            <CopyIcon />
            Copy
          </ContextMenuItem>
          {linkTarget && (
            <>
              <ContextMenuItem className={_menuItem} onSelect={openLink}>
                <ExternalLinkIcon />
                Open Link
              </ContextMenuItem>
              <ContextMenuItem className={_menuItem} onSelect={openLinkNewTab}>
                <ExternalLinkIcon />
                Open Link in New Tab
              </ContextMenuItem>
            </>
          )}
          <ContextMenuSeparator />
          <ContextMenuItem className={_menuItem} onSelect={selectAll}>
            <TextSelectIcon />
            Select All
          </ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>

      {/* The note composer — a modal now (the old rect-pinned popover fired
          on every selection; the menu makes annotating deliberate). */}
      <Dialog
        open={composerOpen}
        onOpenChange={(o) => {
          setComposerOpen(o)
          if (!o) setNote("")
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <MessageSquarePlusIcon className="size-4" />
              Annotate this passage
            </DialogTitle>
            <DialogDescription>
              Feedback about the selected text. The next annotation harvest
              verifies it against live data before applying.
            </DialogDescription>
          </DialogHeader>
          {anchor?.quote && (
            <blockquote className="max-h-24 overflow-y-auto border-l-2 border-border pl-2 text-xs text-muted-foreground italic">
              “{anchor.quote}”
            </blockquote>
          )}
          <Textarea
            autoFocus
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="What's wrong or missing here? The harvester will verify it against the data."
            className="min-h-[90px] text-sm"
          />
          <DialogFooter>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setComposerOpen(false)}
              disabled={saving}
            >
              Cancel
            </Button>
            <Button size="sm" onClick={submit} disabled={saving || !note.trim()}>
              {saving ? <Spinner /> : null}
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

// A header button that files feedback about the WHOLE current page (concept),
// not a text selection and not the whole dataset. It creates an annotation with
// the real `conceptId` but no anchor (empty quote/prefix/suffix), so it sits
// between the selection-anchored notes (which carry a quote) and the
// `_dataset` general notes. Opens a modal composer rather than a rect-pinned
// popover, since there's no selection to anchor to.
export function PageAnnotator({ api, domain, dataset, conceptId, onCreated }) {
  const [open, setOpen] = useState(false)
  const [note, setNote] = useState("")
  const [saving, setSaving] = useState(false)

  const submit = useCallback(async () => {
    const text = note.trim()
    if (!text) return
    setSaving(true)
    try {
      await api.createAnnotation(domain, dataset, conceptId, text, {})
      toast.success("Page annotation added")
      setOpen(false)
      setNote("")
      onCreated?.()
    } catch (e) {
      toast.error(`Could not save annotation: ${e.message || e}`)
    } finally {
      setSaving(false)
    }
  }, [api, domain, dataset, conceptId, note, onCreated])

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        disabled={!conceptId}
        onClick={() => {
          setNote("")
          setOpen(true)
        }}
      >
        <MessageSquarePlusIcon className="size-3.5" />
        Annotate
      </Button>
      <Dialog
        open={open}
        onOpenChange={(o) => {
          setOpen(o)
          if (!o) setNote("")
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <FileTextIcon className="size-4" />
              Annotate this page
            </DialogTitle>
            <DialogDescription>
              Feedback about this whole page. Applies to{" "}
              <span className="font-mono text-xs">{conceptId}</span>, not a
              selection or the whole dataset.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            autoFocus
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="What's wrong or missing on this page? The harvester will verify it against the data."
            className="min-h-[120px] text-sm"
          />
          <DialogFooter>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setOpen(false)}
              disabled={saving}
            >
              Cancel
            </Button>
            <Button size="sm" onClick={submit} disabled={saving || !note.trim()}>
              {saving ? <Spinner /> : null}
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function OutcomeBadge({ status, outcome }) {
  if (status !== "resolved") {
    const label = status === "in_review" ? "In review" : "Open"
    return <Badge variant="secondary">{label}</Badge>
  }
  if (outcome === "applied") {
    return (
      <Badge className="bg-emerald-600/15 text-emerald-700 dark:text-emerald-400">
        <CheckCircle2Icon className="size-3" /> Applied
      </Badge>
    )
  }
  if (outcome === "orphaned") {
    return <Badge variant="outline">Orphaned</Badge>
  }
  return (
    <Badge variant="outline" className="text-muted-foreground">
      <XCircleIcon className="size-3" /> Rejected
    </Badge>
  )
}

function AnnotationCard({ ann, onOpenConcept, onDelete, deleting }) {
  return (
    // bg-card + shadow lift the card off the sheet's popover background so each
    // note reads as a distinct object. min-w-0 + overflow-wrap:anywhere keep the
    // card bounded by the panel: Radix ScrollArea's viewport wraps content in a
    // `display:table; min-width:100%` div, so any unbreakable min-content — the
    // nowrap concept-id chip, a long code token in a note — otherwise widens the
    // WHOLE list past the sheet and pushes the right-aligned controls (concept
    // chip, Delete) out of view, clipping every card's text at a phantom edge.
    <li className="flex min-w-0 flex-col gap-1.5 rounded-xl border border-border/60 bg-card p-3 text-sm shadow-sm [overflow-wrap:anywhere]">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="flex shrink-0 items-center gap-1.5">
          <OutcomeBadge status={ann.status} outcome={ann.outcome} />
          {/* Filed by the chat agent on the user's behalf (provenance). */}
          {ann.submitted_via === "agent" && (
            <Badge variant="outline" className="text-[10px]">
              agent
            </Badge>
          )}
          {/* Whole-page feedback: a real concept but no text selection to
              anchor to (vs a selection note, which carries a quote). */}
          {ann.concept_id !== "_dataset" && !ann.quote && (
            <Badge variant="outline" className="text-[10px]">
              page
            </Badge>
          )}
        </span>
        {ann.concept_id === "_dataset" ? (
          <span className="min-w-0 truncate font-mono text-xs text-muted-foreground">
            whole dataset
          </span>
        ) : (
          <button
            type="button"
            className="min-w-0 truncate font-mono text-xs text-muted-foreground hover:text-foreground hover:underline"
            title={ann.concept_id}
            onClick={() => onOpenConcept?.(ann.concept_id)}
          >
            {ann.concept_id}
          </button>
        )}
      </div>
      {ann.quote && (
        <blockquote className="border-l-2 border-border pl-2 text-xs text-muted-foreground italic">
          “{ann.quote}”
        </blockquote>
      )}
      <p className="text-sm">{ann.note}</p>
      {ann.resolution && (
        // The harvester's verdict can be a long paragraph, so collapse it into an
        // accordion — the badge already conveys the outcome at a glance; expand
        // for the reasoning.
        <Accordion
          type="single"
          collapsible
          className="mt-1 rounded-lg bg-muted/60 px-2"
        >
          <AccordionItem value="response">
            <AccordionTrigger className="py-2 text-xs font-medium">
              Harvester response
            </AccordionTrigger>
            <AccordionContent className="text-xs text-muted-foreground">
              {ann.resolution}
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      )}
      {ann.status !== "resolved" && (
        <div className="flex justify-end">
          <Button
            variant="ghost"
            size="xs"
            className="text-muted-foreground"
            onClick={() => onDelete?.(ann)}
            disabled={deleting}
          >
            <Trash2Icon className="size-3.5" /> Delete
          </Button>
        </div>
      )}
    </li>
  )
}

// Right-hand panel: the caller's annotations. The re-harvest itself lives in the
// Harvest view (its "Start full harvest" split button) — this panel is for
// authoring and reviewing feedback, not for kicking off a run.
export function AnnotationSidebar({
  api,
  domain,
  dataset,
  annotations,
  loading,
  error,
  reload,
  onOpenConcept,
  onApplyAnnotations,
}) {
  const [deletingId, setDeletingId] = useState(null)

  // Split into the two tabs: OPEN (open + in_review — still actionable) and
  // RESOLVED (applied/rejected/orphaned — history). Splitting keeps each list
  // short instead of one long scroll mixing live + done.
  const [openItems, resolvedItems] = useMemo(() => {
    const o = []
    const r = []
    for (const a of annotations) (a.status === "resolved" ? r : o).push(a)
    return [o, r]
  }, [annotations])

  const del = useCallback(
    async (ann) => {
      setDeletingId(ann.annotation_id)
      try {
        await api.deleteAnnotation(domain, dataset, ann.concept_id, ann.annotation_id)
        reload()
      } catch (e) {
        toast.error(`Could not delete: ${e.message || e}`)
      } finally {
        setDeletingId(null)
      }
    },
    [api, domain, dataset, reload]
  )

  // Dataset-level general feedback: a note with no selection to anchor to. It
  // files under the `_dataset` sentinel concept and rides the same annotation
  // queue -> annotation-harvest loop as anchored notes.
  const [generalNote, setGeneralNote] = useState("")
  const [filing, setFiling] = useState(false)
  const fileGeneral = useCallback(async () => {
    const note = generalNote.trim()
    if (!note) return
    setFiling(true)
    try {
      await api.createAnnotation(domain, dataset, "_dataset", note, {})
      setGeneralNote("")
      reload()
    } catch (e) {
      toast.error(`Could not save feedback: ${e.message || e}`)
    } finally {
      setFiling(false)
    }
  }, [api, domain, dataset, generalNote, reload])

  const renderList = (items, emptyCopy) =>
    items.length === 0 ? (
      <Alert>
        <MessageSquarePlusIcon />
        <AlertTitle>Nothing here</AlertTitle>
        <AlertDescription>{emptyCopy}</AlertDescription>
      </Alert>
    ) : (
      // Radix ScrollArea wraps content in an inline-styled `display:table;
      // min-width:100%` div, which sizes to the content's INTRINSIC min-width —
      // a nowrap concept-id chip or a long code token in a note widens the whole
      // list past the sheet, so every card wraps at a phantom edge and the
      // right-aligned controls (concept chip, Delete) are pushed out of view
      // (min-w-0 on the chip can't help: intrinsic contributions ignore it).
      // Forcing the wrapper back to block makes the viewport width bind; the
      // cards' [overflow-wrap:anywhere] then wraps long tokens within it.
      <ScrollArea className="min-h-0 flex-1 [&_[data-slot=scroll-area-viewport]>div]:block!">
        <ul className={cn("flex w-full flex-col gap-2 pr-3")}>
          {items.map((a) => (
            <AnnotationCard
              key={a.annotation_id}
              ann={a}
              onOpenConcept={onOpenConcept}
              onDelete={del}
              deleting={deletingId === a.annotation_id}
            />
          ))}
        </ul>
      </ScrollArea>
    )

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Failed to load annotations</AlertTitle>
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    )
  }
  if (loading) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
      </div>
    )
  }

  return (
    <Tabs defaultValue="open" className="flex h-full min-h-0 flex-col gap-3">
      {/* General feedback: no selection needed — files a dataset-level note
          (the `_dataset` sentinel) into the same queue as anchored ones. */}
      <div className="flex flex-col gap-1.5 rounded-xl border border-border/60 bg-card p-2 shadow-sm">
        <textarea
          value={generalNote}
          onChange={(e) => setGeneralNote(e.target.value)}
          rows={2}
          placeholder="General feedback about this dataset's docs…"
          className="w-full resize-none rounded-md bg-transparent px-1.5 py-1 text-sm outline-none placeholder:text-muted-foreground"
        />
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-muted-foreground">
            Applies to the whole dataset
          </span>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            disabled={filing || !generalNote.trim()}
            onClick={fileGeneral}
          >
            {filing ? "Saving…" : "Add feedback"}
          </Button>
        </div>
      </div>
      {/* Kick off an annotation-mode re-harvest for this dataset. Rather than
          running it here (fire-and-forget, no progress), this jumps to the
          Harvest view's "Apply annotations" picker — where the operator can
          pick a scope + subset and watch the run's live progress. Enabled only
          when there are open notes to apply. */}
      {onApplyAnnotations && (
        <Button
          className="w-full"
          disabled={openItems.length === 0}
          onClick={onApplyAnnotations}
        >
          <PlayIcon className="size-4" />
          Apply annotations
          {openItems.length > 0 && (
            <Badge variant="secondary" className="ml-1">
              {openItems.length}
            </Badge>
          )}
        </Button>
      )}
      <TabsList className="w-full">
        <TabsTrigger value="open" className="flex-1">
          Open
          {openItems.length > 0 && (
            <Badge variant="secondary" className="ml-1.5">
              {openItems.length}
            </Badge>
          )}
        </TabsTrigger>
        <TabsTrigger value="resolved" className="flex-1">
          Resolved
          {resolvedItems.length > 0 && (
            <Badge variant="secondary" className="ml-1.5">
              {resolvedItems.length}
            </Badge>
          )}
        </TabsTrigger>
      </TabsList>
      <TabsContent value="open" className="flex min-h-0 flex-col">
        {renderList(
          openItems,
          "Select any text in a doc to leave feedback, then apply it from the " +
            "Harvest tab — the agent verifies each note against the data."
        )}
      </TabsContent>
      <TabsContent value="resolved" className="flex min-h-0 flex-col">
        {renderList(
          resolvedItems,
          "Resolved annotations appear here for 7 days, with the harvester's verdict."
        )}
      </TabsContent>
    </Tabs>
  )
}
