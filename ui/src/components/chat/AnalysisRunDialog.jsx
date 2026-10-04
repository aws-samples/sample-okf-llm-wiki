// The "+ → Run an analysis" dialog — a command palette over the saved
// analyses, then the analysis's own questionnaire, then Run.
//
// Stage 1: search the analyses (cmdk), filterable by dataset — a PINNED
// conversation locks the filter to its dataset (shown as a chip, not
// editable). Stage 2: the picked analysis's prerequisite questions rendered
// through AskHumanForm — the SAME form the agent's ask_human would show,
// because the frontmatter questions are the same objects. Run composes the
// human prompt: the analysis's address (name + dataset) plus the answers
// keyed by question id — exactly what the execution skill resolves, so the
// run starts with every input already answered and no ask_human round.

import { DatabaseIcon, MicroscopeIcon, PlayIcon } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { AskHumanForm } from "@/components/chat/AskHumanForm"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"

const FILTER_ALL = "__all__"

// Raw frontmatter questions → the AskHumanForm payload shape (the server
// normalizer's output): kind defaulted, options list-ified, free-text
// "Other" offered on choice kinds exactly like a live ask_human round.
function toFormQuestions(raw) {
  return (Array.isArray(raw) ? raw : []).map((q, i) => {
    // Lowercase like every server validator does (`kind: Text` is a valid
    // stored document) — exact-case matching silently degraded it to a
    // 'single' with no options here while the live ask_human round rendered
    // the right widget.
    const rawKind = String(q?.kind || "single")
      .trim()
      .toLowerCase()
    const kind = ["single", "multi", "text"].includes(rawKind)
      ? rawKind
      : "single"
    return {
      id: String(q?.id || `q${i + 1}`),
      prompt: String(q?.prompt || ""),
      kind,
      options: Array.isArray(q?.options) ? q.options.map(String) : [],
      allow_other: kind !== "text",
    }
  })
}

// The human prompt the run becomes: the analysis's address + the answers by
// question id (the ids the analysis's steps reference).
function composePrompt(row, answers) {
  const head = `Run the analysis "${row.name}" on ${row.data_domain}/${row.dataset}.`
  const lines = (answers || []).map(
    (a) =>
      `- ${a.id}: ${Array.isArray(a.answer) ? a.answer.join(", ") : a.answer}`
  )
  return lines.length ? `${head}\n\n${lines.join("\n")}` : head
}

export function AnalysisRunDialog({
  api,
  open,
  onOpenChange,
  datasetScope = null,
  onRun,
}) {
  const [analyses, setAnalyses] = useState(null)
  const [error, setError] = useState("")
  // A failed PICK (the getAnalysis fetch) is its own, non-fatal state: it
  // renders as a line ABOVE the palette and clears on the next pick/open —
  // folding it into `error` replaced the whole list with a dead end.
  const [pickError, setPickError] = useState("")
  const [datasetFilter, setDatasetFilter] = useState(FILTER_ALL)
  // The picked analysis: { row, questions } — questions null while the
  // document (their source) loads.
  const [picked, setPicked] = useState(null)

  // Fresh state per open — the list is cheap and an analysis may have been
  // created/edited since the last open.
  useEffect(() => {
    if (!open || !api) return undefined
    let alive = true
    setAnalyses(null)
    setError("")
    setPickError("")
    setPicked(null)
    setDatasetFilter(FILTER_ALL)
    api
      .listAnalyses()
      .then((res) => {
        if (alive) setAnalyses(Array.isArray(res?.analyses) ? res.analyses : [])
      })
      .catch((e) => {
        if (alive) setError(String(e?.message || e))
      })
    return () => {
      alive = false
    }
  }, [api, open])

  const pinnedKey = datasetScope
    ? `${datasetScope.data_domain}/${datasetScope.dataset}`
    : null
  const datasets = useMemo(
    () =>
      [
        ...new Set(
          (analyses || []).map((a) => `${a.data_domain}/${a.dataset}`)
        ),
      ].sort(),
    [analyses]
  )
  const filtered = useMemo(
    () =>
      (analyses || []).filter((a) => {
        const key = `${a.data_domain}/${a.dataset}`
        if (pinnedKey) return key === pinnedKey
        return datasetFilter === FILTER_ALL || key === datasetFilter
      }),
    [analyses, pinnedKey, datasetFilter]
  )

  const pick = (row) => {
    setPickError("")
    setPicked({ row, questions: null })
    api
      .getAnalysis(row.data_domain, row.dataset, row.name)
      .then((res) =>
        setPicked((cur) =>
          cur && cur.row === row
            ? { row, questions: toFormQuestions(res.questions) }
            : cur
        )
      )
      .catch((e) => {
        // Staleness guard mirrors the success path: a slow rejection from an
        // ABANDONED pick (user pressed Back, picked another analysis) must
        // not yank the current questionnaire away.
        setPicked((cur) => {
          if (!cur || cur.row !== row) return cur
          setPickError(String(e?.message || e))
          return null
        })
      })
  }

  // Every close resets to stage 1 — the dialog must be reusable within the
  // conversation, and a stale `picked` would otherwise remount the NEXT open
  // straight into the old questionnaire.
  const close = (o) => {
    if (!o) setPicked(null)
    onOpenChange(o)
  }

  const run = (answers) => {
    onRun(composePrompt(picked.row, answers))
    close(false)
  }

  // ONE Dialog root, one return — only the CONTENT swaps between stages, so
  // the Radix root (overlay, body lock, focus trap) lives exactly once per
  // open/close cycle whatever stage changes happen inside.
  let content
  if (picked) {
    const { row, questions } = picked
    content = (
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <MicroscopeIcon className="size-4 shrink-0 text-primary" />
            <span className="min-w-0 truncate">{row.title || row.name}</span>
          </DialogTitle>
          <DialogDescription>
            {row.description ||
              `${row.name} · ${row.data_domain}/${row.dataset}`}
          </DialogDescription>
        </DialogHeader>
        {questions === null ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : questions.length === 0 ? (
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setPicked(null)}>
              Back
            </Button>
            <Button onClick={() => run([])}>
              <PlayIcon data-icon="inline-start" />
              Run
            </Button>
          </div>
        ) : (
          <AskHumanForm
            questions={questions}
            onSubmit={run}
            onCancel={() => setPicked(null)}
            submitLabel="Run"
          />
        )}
      </DialogContent>
    )
  } else {
    content = (
      <DialogContent
        className="overflow-hidden p-0 sm:max-w-lg"
        showCloseButton={false}
      >
        <DialogHeader className="sr-only">
          <DialogTitle>Run An Analysis</DialogTitle>
          <DialogDescription>
            Search the saved analyses, answer the questions, run.
          </DialogDescription>
        </DialogHeader>
        <Command className="rounded-xl">
          <CommandInput placeholder="Search analyses…" />
          <div className="flex h-11 items-center gap-2 border-b px-3">
            {pinnedKey ? (
              <>
                <Badge variant="secondary" className="gap-1">
                  <DatabaseIcon className="size-3" />
                  {pinnedKey}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  pinned to this conversation
                </span>
              </>
            ) : (
              <Select value={datasetFilter} onValueChange={setDatasetFilter}>
                <SelectTrigger
                  aria-label="Filter by dataset"
                  size="sm"
                  className="w-56"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper">
                  <SelectItem value={FILTER_ALL}>All Datasets</SelectItem>
                  {datasets.map((d) => (
                    <SelectItem key={d} value={d}>
                      <DatabaseIcon className="size-4" />
                      {d}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          <CommandList>
            {pickError ? (
              <div className="border-b px-3 py-2 text-xs text-destructive">
                Could not open that analysis: {pickError}
              </div>
            ) : null}
            {error ? (
              <div className="px-3 py-6 text-center text-sm text-destructive">
                {error}
              </div>
            ) : analyses === null ? (
              <div className="flex flex-col gap-2 p-3">
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
              </div>
            ) : (
              <>
                <CommandEmpty>No analyses found.</CommandEmpty>
                {filtered.map((a) => {
                  const key = `${a.data_domain}/${a.dataset}`
                  return (
                    <CommandItem
                      key={`${key}/${a.name}`}
                      value={`${a.title} ${a.name} ${key}`}
                      onSelect={() => pick(a)}
                    >
                      <MicroscopeIcon className="size-4 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="truncate">{a.title || a.name}</div>
                        <div className="truncate text-xs text-muted-foreground">
                          {a.name} · {key} · {a.questions} question
                          {a.questions === 1 ? "" : "s"}
                        </div>
                      </div>
                    </CommandItem>
                  )
                })}
              </>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    )
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      {content}
    </Dialog>
  )
}
