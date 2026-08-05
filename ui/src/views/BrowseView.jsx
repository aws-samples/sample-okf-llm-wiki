import { Fragment, useCallback, useEffect, useMemo, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import {
  AlertTriangleIcon,
  ChevronRightIcon,
  CodeIcon,
  FileTextIcon,
  FolderIcon,
  FolderOpenIcon,
  HistoryIcon,
  MessageSquareTextIcon,
  XIcon,
} from "lucide-react"

import { CodeView } from "@/components/chat/CodeView"
import {
  useVersionHistory,
  VersionDiffPane,
  VersionFilePane,
} from "@/components/VersionHistory"
import {
  buildTree,
  parseDocument,
  resolveConceptLink,
  resolveCrossBundleLink,
} from "@/lib/bundle"
import { cn } from "@/lib/utils"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import {
  Card,
  CardTitle,
} from "@/components/ui/card"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import {
  AnnotationSidebar,
  PageAnnotator,
  SelectionAnnotator,
  useAnnotations,
} from "@/views/AnnotationSidebar.jsx"

// Pull plain text out of react-markdown's children (string | array | nodes) —
// used to extract a fenced block's source for CodeView (same helper as chat).
function textOf(children) {
  if (children == null) return ""
  if (typeof children === "string") return children
  if (Array.isArray(children)) return children.map(textOf).join("")
  if (typeof children === "object" && children.props)
    return textOf(children.props.children)
  return String(children)
}

// Browse a harvested bundle: a directory tree of concepts + a markdown viewer.
export default function BrowseView({
  api,
  selection,
  concept,
  onConceptChange,
  onOpenCross,
  onApplyAnnotations,
  picker,
}) {
  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const hasSelection = Boolean(domain && dataset)

  if (!hasSelection) {
    return (
      <Alert>
        <FileTextIcon />
        <AlertTitle>Select a dataset first</AlertTitle>
        <AlertDescription>
          Pick a dataset from the sidebar to browse its knowledge bundle.
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <FilesPane
      picker={picker}
      api={api}
      domain={domain}
      dataset={dataset}
      concept={concept}
      onConceptChange={onConceptChange}
      onOpenCross={onOpenCross}
      onApplyAnnotations={onApplyAnnotations}
    />
  )
}

function FilesPane({
  api,
  domain,
  dataset,
  concept,
  onConceptChange,
  onOpenCross,
  onApplyAnnotations,
  picker,
}) {
  const [files, setFiles] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [content, setContent] = useState("")
  const [loadingDoc, setLoadingDoc] = useState(false)
  const [docError, setDocError] = useState(null)

  // Selected concept: local state so a click updates the tree/viewer instantly,
  // synced to the URL `concept` prop so browser back/forward (which change the
  // prop) also move between docs. Without the local state there'd be a one-frame
  // lag: the URL updates async via hashchange before the prop flows back.
  const [selectedId, setSelectedId] = useState(concept || null)
  useEffect(() => {
    setSelectedId(concept || null)
  }, [concept])

  // Show the raw markdown (frontmatter + body) instead of the rendered doc.
  // Reset to rendered whenever the selected doc changes.
  const [rawView, setRawView] = useState(false)
  useEffect(() => {
    setRawView(false)
  }, [selectedId])

  // Version-history pane state: null = normal doc view; {} opens it on the
  // default last-harvest diff; { to: "live" } opens it comparing the last good
  // version against the working files (the interrupted-harvest review).
  const [versionMode, setVersionMode] = useState(null)
  // Harvest status (ready + last status row) for the interrupted-harvest
  // banner: cancelled/failed with ready=false means the live bundle is a
  // half-written state no complete marker ever blessed.
  const [bundleState, setBundleState] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const list = await api.listBundle(domain, dataset)
      setFiles(Array.isArray(list) ? list : [])
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
    // Best-effort, non-blocking: the banner degrades off if this fails.
    api
      .harvestStatus(domain, dataset)
      .then(setBundleState)
      .catch(() => setBundleState(null))
  }, [api, domain, dataset])

  useEffect(() => {
    load()
  }, [load])

  // Concept id -> S3 key, so an in-doc link (which resolves to a concept id)
  // can be opened without re-listing.
  const keyByConcept = useMemo(() => {
    const m = new Map()
    for (const f of files) m.set(f.concept_id, f.key)
    return m
  }, [files])

  const tree = useMemo(() => buildTree(files), [files])

  // Fetch the selected concept's doc whenever the selection (URL) or the file
  // listing changes. The file list is needed to resolve concept id -> S3 key.
  useEffect(() => {
    if (!selectedId || loading) {
      setContent("")
      setDocError(null)
      return
    }
    const key = keyByConcept.get(selectedId)
    if (!key) {
      setContent("")
      setDocError(`Not found in this bundle: ${selectedId}`)
      return
    }
    let alive = true
    setLoadingDoc(true)
    setDocError(null)
    setContent("")
    api
      .readBundleFile(domain, dataset, key)
      .then((res) => {
        if (alive) setContent(res?.text ?? "")
      })
      .catch((e) => {
        if (alive) setDocError(e.message || String(e))
      })
      .finally(() => {
        if (alive) setLoadingDoc(false)
      })
    return () => {
      alive = false
    }
  }, [api, domain, dataset, selectedId, keyByConcept, loading])

  // Selecting a file / following an in-doc link: update local state immediately
  // (instant tree highlight + fetch) AND the URL (for history/back-forward).
  const openConcept = useCallback(
    (conceptId) => {
      setVersionMode(null) // picking a doc always returns to the doc view
      setSelectedId(conceptId)
      onConceptChange?.(conceptId)
    },
    [onConceptChange]
  )

  const interrupted = Boolean(
    bundleState &&
      bundleState.ready === false &&
      ["cancelled", "failed"].includes(bundleState.status?.status)
  )

  // Version-history state, shared by BOTH panes while versionMode is open:
  // the left pane lists the changed files (with status filters), the right
  // pane owns the pickers/restore controls and the selected file's diff.
  const vh = useVersionHistory({
    api,
    domain,
    dataset,
    active: Boolean(versionMode),
    initialTo: versionMode?.to,
    onRestored: load,
  })

  // Annotations for this dataset (user-scoped server-side). Shared by the
  // in-doc selection popover and the slide-in sidebar so both stay in sync.
  const annotations = useAnnotations(api, domain, dataset)
  const [annotationsOpen, setAnnotationsOpen] = useState(false)
  const openCount = useMemo(
    () => annotations.items.filter((a) => a.status !== "resolved").length,
    [annotations.items]
  )

  // Jump to a concept from the annotations panel, then close the overlay so the
  // reader lands on the doc.
  const openConceptFromSidebar = useCallback(
    (conceptId) => {
      openConcept(conceptId)
      setAnnotationsOpen(false)
    },
    [openConcept]
  )

  return (
    // Fill the content region. Tree + viewer are ONE card on md+ (two panes
    // split by a border, the tree pane tinted so the halves read apart);
    // annotations live in a slide-in Sheet.
    <div className="flex min-h-0 flex-1 flex-col md:h-full">
      {interrupted && !versionMode ? (
        <Alert className="mb-3">
          <AlertTriangleIcon />
          <AlertTitle>Interrupted harvest</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span>
              The last harvest was {bundleState.status.status} mid-write, so the
              published docs may be a half-written state. Review what it changed
              and restore the last good version.
            </span>
            <Button
              size="sm"
              variant="outline"
              className="shrink-0"
              onClick={() => setVersionMode({ to: "live" })}
            >
              <HistoryIcon className="size-3.5" />
              Review &amp; restore
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {/* View header: dataset selector on the left, page-level actions pinned
          right on the same line — matching the Context Docs / Benchmark /
          Harvest views. The per-doc breadcrumb still lives in the doc pane.
          mt-2 lines the picker's center up with the top strip's (~32px from
          the inset top, the sidebar brand row's line): Browse is strip-less
          (pt-2 region, h-8 picker → ~24px) so it needs the 8px the strip's
          h-12/pt-4 geometry provides elsewhere. */}
      <div className="mt-2 mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 items-center">{picker}</div>
        <div className="flex shrink-0 items-center gap-2">
          {versionMode ? (
            <Button variant="outline" onClick={() => setVersionMode(null)}>
              <XIcon />
              Close
            </Button>
          ) : (
            <>
              {/* Compare/restore published bundle versions. Ghost, not
                  filled: Annotations is the row's one primary action — two
                  filled buttons side by side compete for attention. */}
              <Button variant="ghost" onClick={() => setVersionMode({})}>
                <HistoryIcon />
                History
              </Button>
              {/* Open the annotations panel. The badge shows how many of the
                  caller's notes are still open (unresolved) for this dataset. */}
              <Button onClick={() => setAnnotationsOpen(true)}>
                <MessageSquareTextIcon />
                Annotations
                {openCount > 0 && (
                  <Badge variant="secondary" className="ml-1">
                    {openCount}
                  </Badge>
                )}
              </Button>
            </>
          )}
        </div>
      </div>
      <Card className="grid min-h-0 flex-1 grid-cols-1 gap-0 overflow-hidden py-0 md:grid-rows-1 md:grid-cols-[minmax(240px,320px)_1fr]">
        <div className="flex min-h-0 flex-col border-b bg-muted/40 max-md:h-[40vh] md:border-r md:border-b-0">
          {/* Equal-height header rows (h-12) in BOTH panes + the same fade
              hairline below, so the two separators sit on one level. */}
          <div className="flex h-12 shrink-0 items-center justify-between gap-2 px-4">
            <CardTitle className="text-sm">Concepts</CardTitle>
            <span className="shrink-0 text-xs text-muted-foreground">
              {versionMode
                ? `${vh.files.length} changed`
                : `${files.length} file${files.length === 1 ? "" : "s"}`}
            </span>
          </div>
          <div className="h-px shrink-0 bg-gradient-to-r from-transparent via-border/60 to-transparent" />
          <div className="flex min-h-0 flex-1 flex-col">
          {versionMode ? (
            <VersionFilePane vh={vh} />
          ) : error ? (
            <div className="p-4">
              <Alert variant="destructive">
                <AlertTitle>Failed to list bundle</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            </div>
          ) : loading ? (
            <div className="flex flex-col gap-2 p-4">
              <Skeleton className="h-7 w-full" />
              <Skeleton className="h-7 w-3/4" />
              <Skeleton className="h-7 w-5/6" />
            </div>
          ) : files.length === 0 ? (
            <div className="p-4">
              <Alert>
                <FileTextIcon />
                <AlertTitle>No bundle files</AlertTitle>
                <AlertDescription>
                  Run a harvest for this dataset to generate concept docs.
                </AlertDescription>
              </Alert>
            </div>
          ) : (
            // okf-tree-scroll forces Radix's viewport wrapper (display:table,
            // which shrink-wraps to content width and lets long names grow the
            // pane) to block, so the tree fills the card width and long file
            // names truncate instead of overflowing. See index.css.
            <ScrollArea className="okf-tree-scroll h-full">
              <div className="p-2">
                <FileTree
                  nodes={tree}
                  selectedId={selectedId}
                  onSelect={openConcept}
                />
              </div>
            </ScrollArea>
          )}
          </div>
        </div>

        {/* min-w-0: this is the grid's `1fr` doc column. Grid items default to
            min-width:auto, so a wide child (a table or a long code line) would grow
            the track past the viewport instead of scrolling internally. min-w-0 lets
            the track shrink so the inner ScrollArea + label-grid/CodeView scroll. */}
        <div className="flex min-h-0 min-w-0 flex-col max-md:h-[60vh]">
          <div className="flex h-12 shrink-0 flex-row items-center justify-between gap-2 px-4">
          {versionMode ? (
            <CardTitle className="flex items-center gap-2 text-sm">
              <HistoryIcon className="size-4 text-muted-foreground" />
              Version history
            </CardTitle>
          ) : selectedId ? (
            <ConceptBreadcrumb conceptId={selectedId} />
          ) : (
            <CardTitle className="text-sm text-muted-foreground">
              No file selected
            </CardTitle>
          )}
          {/* Per-doc actions, pinned right of the breadcrumb. Raw toggles the
              rendered doc for its source markdown (frontmatter + body); the
              PageAnnotator files feedback about THIS page (concept), distinct
              from a text selection or the whole dataset. */}
          {!versionMode && selectedId && (
            <div className="flex shrink-0 items-center gap-2">
              {/* Radix's Switch renders a real <button>, which is labelable —
                  so wrapping it in a <label> makes the icon + text clickable. */}
              <label className="flex cursor-pointer items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <CodeIcon className="size-3.5" />
                Raw
                <Switch
                  size="sm"
                  checked={rawView}
                  onCheckedChange={setRawView}
                  aria-label="Show raw markdown"
                />
              </label>
              <PageAnnotator
                api={api}
                domain={domain}
                dataset={dataset}
                conceptId={selectedId}
                onCreated={annotations.reload}
              />
            </div>
          )}
          </div>
          <div className="h-px shrink-0 bg-gradient-to-r from-transparent via-border/60 to-transparent" />
          <div className="min-h-0 flex-1">
            {!versionMode && selectedId && rawView && content ? (
              // Raw source: the whole file including YAML frontmatter, filling
              // the entire content card (full width + height). Rendered OUTSIDE
              // the padded markdown ScrollArea so CodeView owns its own scroll;
              // no annotation wrapper — anchoring targets the rendered doc.
              <div className="h-full min-w-0 p-5">
                <CodeView
                  code={content}
                  language="markdown"
                  className="okf-codeview-full okf-codeview-doc"
                />
              </div>
            ) : (
              <ScrollArea className="okf-doc-scroll h-full">
                <div className="min-w-0 p-5">
                  {versionMode ? (
                    <VersionDiffPane vh={vh} />
                  ) : loadingDoc ? (
                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                      <Spinner />
                      Loading…
                    </div>
                  ) : docError ? (
                    <Alert variant="destructive">
                      <AlertTitle>Failed to read file</AlertTitle>
                      <AlertDescription>{docError}</AlertDescription>
                    </Alert>
                  ) : !selectedId ? (
                    <p className="text-sm text-muted-foreground">
                      Select a concept on the left to render its markdown.
                    </p>
                  ) : (
                    // Wrap the rendered doc so a text selection surfaces the
                    // "Annotate" popover; a saved note refreshes the sidebar list.
                    <SelectionAnnotator
                      api={api}
                      domain={domain}
                      dataset={dataset}
                      conceptId={selectedId}
                      onCreated={annotations.reload}
                    >
                      <ConceptDoc
                        conceptId={selectedId}
                        text={content}
                        onNavigate={openConcept}
                        domain={domain}
                        dataset={dataset}
                        onNavigateCross={onOpenCross}
                      />
                    </SelectionAnnotator>
                  )}
                </div>
              </ScrollArea>
            )}
          </div>
        </div>
      </Card>

      {/* Annotations: a floating panel that slides in from the RIGHT. Floating
          (inset from the edges + rounded) rather than flush to the screen edge:
          the inset-y / right offsets + rounded-xl override the Sheet's default
          full-height flush styling, and w-auto lets the max-w cap the width. */}
      <Sheet open={annotationsOpen} onOpenChange={setAnnotationsOpen}>
        <SheetContent
          side="right"
          className="data-[side=right]:inset-y-3 data-[side=right]:right-3 data-[side=right]:h-auto data-[side=right]:w-[92vw] data-[side=right]:rounded-xl data-[side=right]:border data-[side=right]:sm:max-w-md"
        >
          <SheetHeader className="border-b p-4">
            <SheetTitle className="flex items-center gap-2">
              <MessageSquareTextIcon className="size-4" />
              Annotations
            </SheetTitle>
            <SheetDescription>Your feedback for this dataset</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 p-4">
            <AnnotationSidebar
              api={api}
              domain={domain}
              dataset={dataset}
              annotations={annotations.items}
              loading={annotations.loading}
              error={annotations.error}
              reload={annotations.reload}
              onOpenConcept={openConceptFromSidebar}
              onApplyAnnotations={onApplyAnnotations}
            />
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}

// Recursive directory tree. Directories are collapsible (open by default);
// files are selectable rows. Depth drives the indent so nesting reads clearly.
function FileTree({ nodes, selectedId, onSelect, depth = 0 }) {
  return (
    <ul className="flex flex-col">
      {nodes.map((node) =>
        node.type === "dir" ? (
          // Key is type-qualified because a concept id can be BOTH a file
          // ("tables") and a directory prefix ("tables/races"), yielding two
          // sibling nodes that share `path`. Plain path keys would collide.
          <TreeDir
            key={`dir:${node.path}`}
            node={node}
            selectedId={selectedId}
            onSelect={onSelect}
            depth={depth}
          />
        ) : (
          <TreeFile
            key={`file:${node.path}`}
            node={node}
            selected={selectedId === node.conceptId}
            onSelect={onSelect}
            depth={depth}
          />
        )
      )}
    </ul>
  )
}

function TreeDir({ node, selectedId, onSelect, depth }) {
  const [open, setOpen] = useState(true)
  return (
    <li>
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger
          className="flex w-full items-center gap-1.5 rounded-md py-1.5 pr-2 text-sm font-medium transition-colors hover:bg-muted"
          style={{ paddingLeft: `${depth * 12 + 8}px` }}
        >
          <ChevronRightIcon
            className={cn(
              "size-3.5 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-90"
            )}
          />
          {open ? (
            <FolderOpenIcon className="size-4 shrink-0 text-muted-foreground" />
          ) : (
            <FolderIcon className="size-4 shrink-0 text-muted-foreground" />
          )}
          {/* min-w-0 lets the flex item shrink below its content so `truncate`
              (overflow-hidden + ellipsis) actually engages; without it a long
              name grows the row past the card's width. */}
          <span className="min-w-0 truncate">{node.name}</span>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <FileTree
            nodes={node.children}
            selectedId={selectedId}
            onSelect={onSelect}
            depth={depth + 1}
          />
        </CollapsibleContent>
      </Collapsible>
    </li>
  )
}

function TreeFile({ node, selected, onSelect, depth }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(node.conceptId)}
        style={{ paddingLeft: `${depth * 12 + 8}px` }}
        className={cn(
          "flex w-full items-center gap-1.5 rounded-md py-1.5 pr-2 text-left text-sm transition-colors hover:bg-muted",
          selected && "bg-muted font-medium text-foreground"
        )}
      >
        {/* Spacer standing in for the folder rows' chevron (size-3.5), so the
            file icon lines up with sibling folders' icons (the flex gap-1.5
            supplies the matching gap). */}
        <span className="size-3.5 shrink-0" aria-hidden="true" />
        <FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 truncate">{node.name}</span>
      </button>
    </li>
  )
}

// Full concept path as a breadcrumb, e.g. references / joins / races__results.
function ConceptBreadcrumb({ conceptId }) {
  const parts = conceptId.split("/")
  return (
    <Breadcrumb>
      <BreadcrumbList className="text-xs sm:gap-1.5">
        {parts.map((part, i) => {
          const last = i === parts.length - 1
          return (
            <Fragment key={i}>
              <BreadcrumbItem>
                {last ? (
                  <BreadcrumbPage className="font-mono">{part}</BreadcrumbPage>
                ) : (
                  <span className="font-mono text-muted-foreground">
                    {part}
                  </span>
                )}
              </BreadcrumbItem>
              {!last && <BreadcrumbSeparator />}
            </Fragment>
          )
        })}
      </BreadcrumbList>
    </Breadcrumb>
  )
}

// Render one concept doc: strip the YAML frontmatter, show its title/type/tags
// as a compact header, then render the body as GFM markdown. Intra-bundle
// links (relative `.md`) are rewritten to full concept ids and intercepted so
// clicking one opens that concept in place instead of navigating the browser.
// Bundle-ESCAPING `.md` links (a cross-dataset reference doc addressing the
// counterpart's docs — see CONVENTIONS.md "Cross-dataset references") are
// resolved against the shared okf/ tree and navigate into THAT dataset's
// Browse view via `onNavigateCross`; the address may dangle, in which case the
// target view reports the missing doc.
// Exported: the chat's doc-peek panel (components/chat/DocPeek) renders cited
// docs with THIS component so a citation opens Browse-grade markdown.
export function ConceptDoc({
  conceptId,
  text,
  onNavigate,
  domain,
  dataset,
  onNavigateCross,
}) {
  const { frontmatter, body } = useMemo(() => parseDocument(text), [text])

  const components = useMemo(() => {
    // Stamp each block with its 1-based source line (from react-markdown's hast
    // `node.position`) as `data-sl`, so a text selection can recover the source
    // line of its enclosing block (annotationAnchor.nearestBlockLine). The line
    // is BODY-relative (frontmatter is stripped before render) — a hint for the
    // agent to jump near, never the anchor's source of truth (that's the quote).
    const withPos = (Tag) =>
      function Block({ node, ...props }) {
        const line = node?.position?.start?.line
        return <Tag {...(line != null ? { "data-sl": line } : {})} {...props} />
      }
    return {
      p: withPos("p"),
      li: withPos("li"),
      h1: withPos("h1"),
      h2: withPos("h2"),
      h3: withPos("h3"),
      h4: withPos("h4"),
      h5: withPos("h5"),
      h6: withPos("h6"),
      td: withPos("td"),
      th: withPos("th"),
      blockquote: withPos("blockquote"),
      // Tables + fenced code render the SAME way as the chat agent: GFM tables get
      // the shared label-grid look (index.css `.okf-label-grid`), and fenced code
      // renders through the read-only CodeView (language label + copy + highlight)
      // instead of a bare highlighted <pre>. `data-sl` is preserved on the wrapper
      // so annotation anchoring still finds the block's source line.
      table({ node, children, ...props }) {
        const line = node?.position?.start?.line
        // okf-label-grid-wrap: doc tables WRAP long cell text (prose docs, not the
        // chat's compact tool-result grids which keep nowrap + per-column config).
        return (
          <div
            className="okf-label-grid okf-label-grid-wrap"
            {...(line != null ? { "data-sl": line } : {})}
          >
            <table {...props}>{children}</table>
          </div>
        )
      },
      pre({ node, children }) {
        const line = node?.position?.start?.line
        const child = Array.isArray(children) ? children[0] : children
        const cls = child?.props?.className || ""
        const match = /language-(\w+)/.exec(cls)
        if (child?.props) {
          return (
            <div {...(line != null ? { "data-sl": line } : {})}>
              <CodeView
                code={textOf(child.props.children)}
                language={match?.[1]}
              />
            </div>
          )
        }
        return (
          <pre {...(line != null ? { "data-sl": line } : {})}>{children}</pre>
        )
      },
      a({ href, children, ...props }) {
        const resolved = href ? resolveConceptLink(href, conceptId) : null
        if (resolved) {
          return (
            <a
              href={`#${resolved.conceptId}`}
              onClick={(e) => {
                e.preventDefault()
                onNavigate(resolved.conceptId)
              }}
              {...props}
            >
              {children}
            </a>
          )
        }
        // A bundle-escaping .md link: a cross-dataset ADDRESS into another
        // dataset's bundle. Navigate there in-app (switch selection + open the
        // concept); Back returns here.
        const cross =
          href && onNavigateCross
            ? resolveCrossBundleLink(href, conceptId, {
                data_domain: domain,
                dataset,
              })
            : null
        if (cross) {
          return (
            <a
              href={`#${cross.dataDomain}/${cross.dataset}/${cross.conceptId}`}
              title={`${cross.dataDomain}/${cross.dataset} · ${cross.conceptId}`}
              onClick={(e) => {
                e.preventDefault()
                onNavigateCross(cross.dataDomain, cross.dataset, cross.conceptId)
              }}
              {...props}
            >
              {children}
            </a>
          )
        }
        // External / non-concept link: open safely in a new tab.
        return (
          <a href={href} target="_blank" rel="noreferrer noopener" {...props}>
            {children}
          </a>
        )
      },
    }
  }, [conceptId, onNavigate, domain, dataset, onNavigateCross])

  const title = frontmatter.title
  const type = frontmatter.type
  const description = frontmatter.description
  const tags = Array.isArray(frontmatter.tags) ? frontmatter.tags : []
  const resource = frontmatter.resource
  const hasHeader = title || type || description || tags.length > 0

  return (
    <div className="flex flex-col gap-4">
      {hasHeader && (
        <div className="flex flex-col gap-2 border-b pb-4">
          <div className="flex flex-wrap items-center gap-2">
            {type && <Badge variant="secondary">{type}</Badge>}
            {tags.map((t) => (
              <Badge key={t} variant="outline">
                {t}
              </Badge>
            ))}
          </div>
          {title && (
            <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          )}
          {description && (
            <p className="text-sm text-muted-foreground">{description}</p>
          )}
          {resource && /^https?:\/\//.test(resource) && (
            <a
              href={resource}
              target="_blank"
              rel="noreferrer noopener"
              className="w-fit truncate font-mono text-xs text-primary underline-offset-4 hover:underline"
            >
              {resource}
            </a>
          )}
        </div>
      )}
      <div className="okf-prose">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
          {body}
        </ReactMarkdown>
      </div>
    </div>
  )
}
