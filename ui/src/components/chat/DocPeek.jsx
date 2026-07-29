// Doc peek — the chat's side-by-side doc reader, opened by clicking a wiki
// citation card. It renders the cited concept with the SAME renderer Browse
// uses (ConceptDoc: frontmatter header, GFM, label-grid tables, CodeView) and
// wraps it in SelectionAnnotator, so highlighting a passage here files an
// annotation exactly like it does in Browse — read the agent's claim, check the
// doc, annotate the doc, without leaving the conversation.
//
// It PUSHES the chat layout rather than overlaying it (ChatPanel mounts it in a
// width-animated clip, like the history drawer) so the transcript and the doc
// are readable side by side. In-doc links navigate INSIDE the panel — including
// bundle-escaping cross-dataset addresses, which swap the panel to the
// counterpart's bundle (`onTarget`).

import { XIcon } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { PanelShell } from "@/components/chat/PanelShell"
import { DocIcon } from "@/components/chat/SourceIcon"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Spinner } from "@/components/ui/spinner"
import { SelectionAnnotator } from "@/views/AnnotationSidebar"
import { ConceptDoc } from "@/views/BrowseView"

export function DocPeek({
  api,
  target,
  onTarget,
  onClose,
  onResizeStart,
  resizing = false,
}) {
  const { dataDomain, dataset, conceptId } = target || {}
  const [text, setText] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // The bundle listing resolves concept id → S3 key (same two-step read as
  // Browse). Cached per bundle so in-panel navigation between docs of the same
  // dataset costs one read, not a re-list.
  const listRef = useRef({ key: "", files: null })

  useEffect(() => {
    if (!api || !dataDomain || !dataset || !conceptId) return
    let alive = true
    setLoading(true)
    setError(null)
    setText("")
    ;(async () => {
      try {
        const bundleKey = `${dataDomain}/${dataset}`
        let files =
          listRef.current.key === bundleKey ? listRef.current.files : null
        if (!files) {
          const list = await api.listBundle(dataDomain, dataset)
          files = Array.isArray(list) ? list : []
          listRef.current = { key: bundleKey, files }
        }
        if (!alive) return
        const file = files.find((f) => f.concept_id === conceptId)
        if (!file) throw new Error(`Not found in ${bundleKey}: ${conceptId}`)
        const res = await api.readBundleFile(dataDomain, dataset, file.key)
        if (alive) setText(res?.text ?? "")
      } catch (e) {
        if (alive) setError(e.message || String(e))
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [api, dataDomain, dataset, conceptId])

  return (
    <PanelShell onResizeStart={onResizeStart} resizing={resizing}>
      <div className="flex items-start gap-2 border-b p-3">
        <DocIcon
          conceptId={conceptId}
          size={16}
          className="mt-0.5 shrink-0 text-primary"
        />
        <div className="flex min-w-0 flex-1 flex-col">
          <span
            className="truncate font-mono text-xs text-foreground"
            title={conceptId}
          >
            {conceptId}
          </span>
          <span className="truncate text-[11px] text-muted-foreground">
            {dataDomain}/{dataset} · highlight text to annotate
          </span>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          onClick={onClose}
          aria-label="Close doc panel"
        >
          <XIcon className="size-4" />
        </Button>
      </div>
      <ScrollArea className="okf-doc-scroll min-h-0 flex-1">
        <div className="min-w-0 p-4">
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner />
              Loading…
            </div>
          ) : error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to read page</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : (
            <SelectionAnnotator
              api={api}
              domain={dataDomain}
              dataset={dataset}
              conceptId={conceptId}
            >
              <ConceptDoc
                conceptId={conceptId}
                text={text}
                domain={dataDomain}
                dataset={dataset}
                onNavigate={(cid) =>
                  onTarget({ dataDomain, dataset, conceptId: cid })
                }
                onNavigateCross={(dd, ds, cid) =>
                  onTarget({ dataDomain: dd, dataset: ds, conceptId: cid })
                }
              />
            </SelectionAnnotator>
          )}
        </div>
      </ScrollArea>
    </PanelShell>
  )
}
