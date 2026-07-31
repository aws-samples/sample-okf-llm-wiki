// Inline citations — ONE badge per claim, however many sources back it.
//
// The agent emits a single `<cite src="a,b,https://c">` after a claim and
// Markdown.preprocessCitations also MERGES adjacent tags, so what arrives here is
// always a group. The badge shows the first source (icon + short label) plus a
// "+N" counter; clicking it opens a card you page through with ← / → (i/N in the
// corner), the way Claude and ChatGPT present grouped sources.
//
// Two source kinds share the UI:
//   - WEB (a URL from web_search): favicon + host, the page title, its publication
//     date and snippet, and the whole card is a link to the page. Metadata comes
//     from the turn's web_search results (lib/sources.collectWebSources); a URL we
//     have no result for still renders — host + link, just without title/snippet.
//   - WIKI DOC (a concept id): kind glyph + "Table"/"Reference"/"Dataset", the full
//     concept-id path, and the doc's dataset. When the doc's bundle is known
//     (conversation scope, or the conversation's own tool traffic —
//     lib/sources.collectWikiSources), the card is CLICKABLE and opens the doc in
//     the chat's doc-peek panel (`onOpenDoc`); an unresolvable id stays a plain
//     summary rather than a dead link.

import {
  ChevronLeftIcon,
  ChevronRightIcon,
  PanelRightOpenIcon,
} from "lucide-react"
import { useState } from "react"

import { DocIcon, SourceIcon } from "@/components/chat/SourceIcon"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import {
  conceptIdOf,
  formatPublished,
  hostOf,
  isWebSource,
  parseWikiSource,
} from "@/lib/sources"

// A concept id's top-level kind → a human label.
const CITE_KIND = {
  tables: "Table",
  references: "Reference",
  datasets: "Dataset",
  external: "Cross-dataset",
}

// The badge's inline label. A web source reads best as its host ("reuters.com");
// a doc as its LAST path segment ("race_wins"), with the full path in the card.
function shortLabel(item) {
  if (isWebSource(item)) return hostOf(item) || item
  const parts = String(item).split("/")
  return parts[parts.length - 1] || item
}

function WebCard({ url, meta }) {
  const host = hostOf(url) || url
  const date = formatPublished(meta?.publishedDate)
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      className="group flex flex-col gap-1.5 no-underline"
    >
      <span className="flex items-center gap-1.5">
        <SourceIcon url={url} size={14} />
        <span className="truncate text-xs text-muted-foreground">{host}</span>
      </span>
      {meta?.title ? (
        <span className="text-sm leading-snug font-medium text-foreground group-hover:underline">
          {meta.title}
        </span>
      ) : (
        <span className="text-xs break-all text-foreground group-hover:underline">
          {url}
        </span>
      )}
      {meta?.text || date ? (
        <span className="line-clamp-3 text-xs leading-relaxed text-muted-foreground">
          {date ? <span className="text-foreground/70">{date} — </span> : null}
          {meta?.text}
        </span>
      ) : null}
    </a>
  )
}

function DocCard({ id, datasetScope, wikiSources, onOpenDoc }) {
  // A FULLY-QUALIFIED id (`bird/formula_1/tables/races` — what the agent is
  // prompted to emit) carries its own location; a bare one (old history, model
  // slips) falls back to the conversation's tool traffic, then the conversation
  // scope. Unresolvable → the card stays a plain, non-clickable summary.
  const wiki = parseWikiSource(id)
  const conceptId = wiki?.conceptId || String(id)
  const kind = CITE_KIND[conceptId.split("/")[0]] || "Doc"
  const loc =
    (wiki?.dataDomain
      ? { data_domain: wiki.dataDomain, dataset: wiki.dataset }
      : null) ||
    wikiSources?.get(conceptId) ||
    datasetScope ||
    null
  const dataset = loc ? `${loc.data_domain}/${loc.dataset}` : null
  const body = (
    <>
      <span className="flex items-center gap-1.5">
        <DocIcon conceptId={conceptId} size={14} />
        <span className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
          {kind} · wiki
        </span>
        {loc && onOpenDoc ? (
          <span className="ml-auto flex items-center gap-1 text-[10px] text-muted-foreground group-hover:text-foreground">
            <PanelRightOpenIcon className="size-3" />
            Open page
          </span>
        ) : null}
      </span>
      <span className="font-mono text-xs break-all text-foreground group-hover:underline">
        {conceptId}
      </span>
      {dataset ? (
        <span className="text-xs text-muted-foreground">
          in <span className="font-mono">{dataset}</span>
        </span>
      ) : null}
    </>
  )
  if (loc && onOpenDoc) {
    return (
      <button
        type="button"
        onClick={() =>
          onOpenDoc({
            dataDomain: loc.data_domain,
            dataset: loc.dataset,
            conceptId,
          })
        }
        className="group flex w-full cursor-pointer flex-col gap-1.5 text-left"
      >
        {body}
      </button>
    )
  }
  return <div className="flex flex-col gap-1.5">{body}</div>
}

export function CitationGroup({
  sources,
  datasetScope = null,
  webSources = null,
  wikiSources = null,
  onOpenDoc = null,
}) {
  const [page, setPage] = useState(0)
  // Controlled so opening a doc in the peek panel also closes the card —
  // otherwise the popover would sit on top of the panel it just opened.
  const [open, setOpen] = useState(false)
  if (!sources || sources.length === 0) return null

  const total = sources.length
  const at = Math.min(page, total - 1)
  const current = sources[at]
  const first = sources[0]
  const extra = total - 1

  const go = (delta) => (e) => {
    // The arrows live inside the popover; don't let a click bubble out to the
    // trigger (which would toggle the popover shut mid-navigation).
    e.preventDefault()
    e.stopPropagation()
    setPage((p) => (p + delta + total) % total)
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <span className="okf-cite" tabIndex={0} role="button">
          {isWebSource(first) ? (
            <SourceIcon url={first} size={11} className="okf-cite-icon" />
          ) : (
            // text-primary: the kind glyph joins the badge's cyan family (cn
            // merges it over DocIcon's muted default). Favicons keep their own
            // colors — the site's mark IS the signal there. conceptIdOf strips a
            // qualified id's domain/dataset prefix so the glyph keys on the kind.
            <DocIcon
              conceptId={conceptIdOf(first)}
              size={11}
              className="okf-cite-icon text-primary"
            />
          )}
          <span className="okf-cite-label">{shortLabel(first)}</span>
          {extra > 0 ? <span className="okf-cite-more">+{extra}</span> : null}
        </span>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        sideOffset={6}
        className="w-80 gap-0 rounded-xl p-0"
      >
        {total > 1 ? (
          <div className="flex items-center gap-1 border-b px-2 py-1.5">
            <button
              type="button"
              onClick={go(-1)}
              aria-label="Previous source"
              className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <ChevronLeftIcon className="size-3.5" />
            </button>
            <button
              type="button"
              onClick={go(1)}
              aria-label="Next source"
              className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <ChevronRightIcon className="size-3.5" />
            </button>
            <span className="ml-auto text-xs text-muted-foreground tabular-nums">
              {at + 1}/{total}
            </span>
          </div>
        ) : null}
        <div className="p-3">
          {isWebSource(current) ? (
            <WebCard url={current} meta={webSources?.get(current)} />
          ) : (
            <DocCard
              id={current}
              datasetScope={datasetScope}
              wikiSources={wikiSources}
              onOpenDoc={
                onOpenDoc
                  ? (target) => {
                      setOpen(false)
                      onOpenDoc(target)
                    }
                  : null
              }
            />
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}
