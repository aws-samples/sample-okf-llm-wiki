// Analysis peek — the chat's side-by-side reader for a saved analysis
// (procedure doc), opened by clicking the panel affordance on an analysis
// tool step in the thinking timeline. Same PanelShell chrome + width-animated
// clip as the doc peek, so citations / reports / analyses read as ONE surface
// that PUSHES the chat rather than overlaying it.
//
// The Control API returns the document server-parsed (title, the prerequisite
// questions, the steps body), so this stays a renderer: header meta, the
// questions as cards, the body through the chat's Markdown. Always fetched
// fresh — an update_analysis earlier in the turn must show the NEW version.

import { MicroscopeIcon, XIcon } from "lucide-react"
import { useEffect, useState } from "react"

import { Markdown } from "@/components/chat/Markdown"
import { PanelShell } from "@/components/chat/PanelShell"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Spinner } from "@/components/ui/spinner"

// The rendered document — description, the prerequisite questions as cards,
// then the steps as markdown. Shared with the Analysis page's detail view
// (same document, main-layout width there, side-panel width here).
export function AnalysisDocument({ doc, questions }) {
  const qs = Array.isArray(questions) ? questions : []
  return (
    <div className="flex flex-col gap-4">
      {doc.description ? (
        <p className="text-sm text-muted-foreground">{doc.description}</p>
      ) : null}
      {qs.length ? (
        <div className="flex flex-col gap-1.5">
          <div className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
            Asks First
          </div>
          {qs.map((q, i) => (
            <div
              key={q?.id || i}
              // Foreground-alpha, not bg-muted: light-mode --muted
              // matches the card bg and would vanish.
              className="rounded-md border border-border/60 bg-foreground/5 px-2.5 py-2 text-[13px] leading-snug"
            >
              <div className="mb-1 flex items-center gap-2">
                <span className="font-mono text-[11px] text-muted-foreground">
                  {q?.id}
                </span>
                <Badge variant="secondary" className="text-[10px]">
                  {q?.kind || "single"}
                </Badge>
              </div>
              <div className="text-foreground">{q?.prompt}</div>
              {Array.isArray(q?.options) && q.options.length ? (
                <div className="mt-1 text-[11px] text-muted-foreground">
                  {q.options.join(" · ")}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      <Markdown>{doc.body || ""}</Markdown>
    </div>
  )
}

export function AnalysisPeek({
  api,
  target,
  onClose,
  onResizeStart,
  resizing = false,
}) {
  const { dataDomain, dataset, name } = target || {}
  const [doc, setDoc] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!api) return undefined
    if (!dataDomain || !dataset || !name) {
      // A silent early-return left the panel permanently blank when the
      // opener couldn't resolve a location (history-rebuilt args with the
      // scope chip since cleared) — say so instead.
      setLoading(false)
      setDoc(null)
      setError(
        "This analysis's dataset could not be determined from the step — " +
          "open it from the Analysis page instead."
      )
      return undefined
    }
    let alive = true
    setLoading(true)
    setError(null)
    setDoc(null)
    ;(async () => {
      try {
        const res = await api.getAnalysis(dataDomain, dataset, name)
        if (alive) setDoc(res)
      } catch (e) {
        if (alive) setError(e.message || String(e))
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [api, dataDomain, dataset, name, target])

  const questions = Array.isArray(doc?.questions) ? doc.questions : []

  return (
    <PanelShell onResizeStart={onResizeStart} resizing={resizing}>
      <div className="flex items-start gap-2 border-b p-3">
        <MicroscopeIcon size={16} className="mt-0.5 shrink-0 text-primary" />
        <div className="flex min-w-0 flex-1 flex-col">
          <span
            className="truncate text-xs font-medium text-foreground"
            title={doc?.title || name}
          >
            {doc?.title || name}
          </span>
          <span className="truncate text-[11px] text-muted-foreground">
            {dataDomain}/{dataset} · {name}
            {doc ? ` · v${doc.version}` : ""}
            {doc?.owned_by_you ? " · yours" : ""}
          </span>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          onClick={onClose}
          aria-label="Close analysis panel"
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
              <AlertTitle>Failed To Read The Analysis</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : doc ? (
            <AnalysisDocument doc={doc} questions={questions} />
          ) : null}
        </div>
      </ScrollArea>
    </PanelShell>
  )
}
