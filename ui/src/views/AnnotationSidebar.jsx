import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  CheckCircle2Icon,
  MessageSquarePlusIcon,
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
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover"
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
//  - <SelectionAnnotator>: wraps the rendered doc; on a text selection it floats
//    an "Annotate" button (PopoverAnchor pinned to the selection rect) that opens
//    a note composer.
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

// Wrap the rendered doc; capture selections and offer an Annotate affordance.
export function SelectionAnnotator({
  api,
  domain,
  dataset,
  conceptId,
  onCreated,
  children,
}) {
  const containerRef = useRef(null)
  // The pending selection anchor + the popover's virtual anchor. Radix's
  // PopoverAnchor reads `virtualRef.current` (a measurable with
  // getBoundingClientRect), so `virtualRef` MUST be a ref OBJECT — not the
  // measurable itself. We keep a STABLE virtualRef whose getBoundingClientRect
  // reads the latest captured rect from `rectRef`, so the popover floats over
  // the selection and the anchor resolves once (no per-selection ref churn).
  const [anchor, setAnchor] = useState(null)
  const [open, setOpen] = useState(false)
  const [note, setNote] = useState("")
  const [saving, setSaving] = useState(false)

  const rectRef = useRef(null)
  const virtualRef = useRef({
    getBoundingClientRect: () =>
      rectRef.current || { top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0 },
  })

  const onMouseUp = useCallback(() => {
    const cap = captureSelection(containerRef.current)
    if (cap) {
      // Snapshot the selection rect BEFORE opening (focusing the textarea
      // collapses the live selection, but this frozen rect keeps the popover
      // pinned where the text was).
      rectRef.current = cap.rect
      setAnchor(cap)
      setNote("")
      setOpen(true)
    }
  }, [])

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
      setOpen(false)
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
    <div ref={containerRef} onMouseUp={onMouseUp}>
      {children}
      <Popover
        open={open}
        onOpenChange={(o) => {
          setOpen(o)
          if (!o) setAnchor(null)
        }}
      >
        <PopoverAnchor virtualRef={virtualRef} />
        {/* collisionPadding keeps a gap from the viewport edge; capping height to
            Radix's computed available-height (+ overflow-y-auto) means a selection
            near the bottom gets a scrollable popover that stays on-screen instead
            of spilling past the fold. */}
        <PopoverContent
          side="top"
          align="center"
          collisionPadding={12}
          className="flex max-h-[var(--radix-popover-content-available-height)] w-80 flex-col gap-3 overflow-y-auto"
        >
          <div className="flex items-center gap-2 text-sm font-medium">
            <MessageSquarePlusIcon className="size-4" />
            Annotate this passage
          </div>
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
          <div className="flex justify-end gap-2">
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
          </div>
        </PopoverContent>
      </Popover>
    </div>
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
    // note reads as a distinct object.
    <li className="flex flex-col gap-1.5 rounded-xl border border-border/60 bg-card p-3 text-sm shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <OutcomeBadge status={ann.status} outcome={ann.outcome} />
        <button
          type="button"
          className="truncate font-mono text-xs text-muted-foreground hover:text-foreground hover:underline"
          title={ann.concept_id}
          onClick={() => onOpenConcept?.(ann.concept_id)}
        >
          {ann.concept_id}
        </button>
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

  const renderList = (items, emptyCopy) =>
    items.length === 0 ? (
      <Alert>
        <MessageSquarePlusIcon />
        <AlertTitle>Nothing here</AlertTitle>
        <AlertDescription>{emptyCopy}</AlertDescription>
      </Alert>
    ) : (
      <ScrollArea className="min-h-0 flex-1">
        <ul className={cn("flex flex-col gap-2 pr-3")}>
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
