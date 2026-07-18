// Markdown renderer for chat text — the SAME stack Browse/Harvest use
// (react-markdown + remark-gfm + rehype-highlight + the .okf-prose theme class),
// so the agent's answers inherit the app's typography and, crucially, render
// GFM TABLES (domain/dataset listings) as real tables instead of raw `| … |`.
//
// FENCED CODE BLOCKS render through the read-only CodeView (a language-labeled,
// copyable, scrollable highlighted block) — so when the agent writes code it gets
// proper chrome, not a bare <pre>. INLINE code that looks like a CONCEPT ID
// (`tables/races`, `references/metrics/race_wins`) renders as a distinct LABEL
// pill; other inline code (`like this`) keeps the plain .okf-prose pill. We
// disable rehype-highlight here (CodeView highlights the block itself with
// highlight.js) so there's no double-processing.
//
// External links open in a new tab; concept-style links (#anchor) are left as
// plain anchors (the chat isn't a doc navigator). No sanitize plugin here: the
// content is our own agent's markdown, not third-party HTML.

import { useMemo } from "react"
import ReactMarkdown, { defaultUrlTransform } from "react-markdown"
import remarkGfm from "remark-gfm"

import { CodeView } from "@/components/chat/CodeView"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

const REMARK = [remarkGfm]

// react-markdown sanitizes hrefs and DROPS unknown URL schemes — our internal
// `okf-cite:` links would become "" and never reach the `a` renderer. This keeps
// them (and defers to the default transform for everything else).
function urlTransform(url) {
  if (typeof url === "string" && url.startsWith(CITE_SCHEME)) return url
  return defaultUrlTransform(url)
}

// A concept id's top-level kind → a human label for the citation popup.
const CITE_KIND = {
  tables: "Table",
  references: "Reference",
  datasets: "Dataset",
}

// Citations: the agent emits `<cite src="tables/races,references/joins/x"></cite>`
// after a claim (see the chat system prompt's <citations> block). We rewrite each
// tag into one markdown link per concept id using an internal `okf-cite:` scheme,
// then render those links as compact citation chips (the `a` component below).
// This rides the existing markdown link path — no rehype-raw / HTML-in-markdown
// dependency. A trailing INCOMPLETE tag (mid-stream, e.g. `<cite src="tab`) is
// stripped so it never flashes as raw text while tokens arrive.
//
// The tag is SUPPOSED to be empty (`<cite src="…"></cite>`), but the model
// sometimes wraps gloss text: `<cite src="…">titles counted from …</cite>`. We must
// handle that: keep the chips (from `src`), DROP the inner gloss (attribution is the
// chip, not prose), and — critically — consume the matching `</cite>` so it never
// leaks as literal text (the old regex only ate a `</cite>` immediately adjacent to
// the opener, so a content-bearing tag left a stray `</cite>` in the output). The
// content form is matched first (non-greedy inner, so back-to-back cites don't merge),
// then the empty/adjacent form, then any orphan `</cite>` with no opener.
const CITE_TAG_CONTENT_RE = /<cite\s+src="([^"]*)"\s*>[\s\S]*?<\/cite\s*>/gi
const CITE_TAG_EMPTY_RE = /<cite\s+src="([^"]*)"\s*>\s*(?:<\/cite\s*>)?/gi
const CITE_ORPHAN_CLOSE_RE = /<\/cite\s*>/gi
// A trailing PARTIAL tag at the very end of the (mid-stream) buffer — a partial
// opener (`<cite src="tab`) OR a partial closer (`</cite` with no `>` yet). The
// `\/?` is what makes it catch the closer too (the old version only stripped a
// partial opener, so a streamed `…</cite` flashed as literal text before its `>`
// arrived). Only matched once "cite" is fully present, so it can't clip legit
// prose like a trailing "a <" at a frame boundary. Completed on the next frame.
const CITE_PARTIAL_RE = /<\/?cite\b[^>]*$/i
const CITE_SCHEME = "okf-cite:"

// Turn a comma-separated `src` into clustered citation-chip markdown links (one per
// concept id). Empty → "" (drops a src-less tag entirely).
function citeChips(src) {
  const ids = src
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
  if (ids.length === 0) return ""
  // One link per id; the label IS the id (the chip shows a compact form). No
  // separators so the chips cluster tightly after the claim.
  return ids.map((id) => `[${id}](${CITE_SCHEME}${encodeURIComponent(id)})`).join("")
}

function preprocessCitations(md) {
  // Guard on any `cite` tag — opener OR orphan closer (`</cite>` contains `</cite`,
  // NOT `<cite`, so an `indexOf("<cite")` alone would skip a stray closer).
  if (!md || md.indexOf("cite") === -1) return md || ""
  // 1) Content-bearing tags first: `<cite src="…">gloss</cite>` → chips (gloss dropped,
  //    closer consumed). Non-greedy so adjacent cites aren't swallowed as one span.
  let out = md.replace(CITE_TAG_CONTENT_RE, (_m, src) => citeChips(src))
  // 2) Empty / self-adjacent tags: `<cite src="…"></cite>` or `<cite src="…">`.
  out = out.replace(CITE_TAG_EMPTY_RE, (_m, src) => citeChips(src))
  // 3) Drop a dangling partial OPENER/CLOSER at the very end (still streaming, e.g. `<cite src="tab`).
  out = out.replace(CITE_PARTIAL_RE, "")
  // 4) Belt-and-suspenders: strip any orphan `</cite>` left with no matching opener
  //    (e.g. a mid-stream frame that delivered the closer before the opener, or a
  //    malformed tag) so a bare `</cite>` never renders as literal text.
  out = out.replace(CITE_ORPHAN_CLOSE_RE, "")
  return out
}

// The compact label for a citation chip: the last path segment (e.g.
// "references/metrics/race_wins" → "race_wins"), which reads cleanly inline; the
// full id + dataset live in the hover popup.
function citeLabel(id) {
  const parts = id.split("/")
  return parts[parts.length - 1] || id
}

// A citation chip: a small source pill naming a wiki doc, with a hover popup that
// shows the doc kind, its full concept-id path, and (when the conversation is
// scoped) which dataset it belongs to. Not a link — the chat isn't a doc browser.
function Citation({ id, datasetScope }) {
  const kind = CITE_KIND[id.split("/")[0]] || "Doc"
  const dataset = datasetScope
    ? `${datasetScope.data_domain}/${datasetScope.dataset}`
    : null
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="okf-cite" tabIndex={0}>
          {citeLabel(id)}
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" align="start" className="max-w-xs flex-col items-start gap-1">
        <span className="text-[10px] font-medium tracking-wide uppercase opacity-70">
          {kind}
        </span>
        <span className="font-mono text-xs break-all">{id}</span>
        {dataset ? (
          <span className="text-[11px] opacity-80">
            in <span className="font-mono">{dataset}</span>
          </span>
        ) : null}
      </TooltipContent>
    </Tooltip>
  )
}

// A concept id: one of the OKF bundle's top-level dirs (datasets/tables/
// references — see docs/CONVENTIONS.md) followed by 1+ slash-joined path segments
// (a segment starts alnum/underscore, then alnum/underscore/dot/dash — matches
// okf_core.paths). e.g. `tables/races`, `references/metrics/race_wins`. Anchored
// so it matches the WHOLE inline-code token, not a substring of prose.
const CONCEPT_ID_RE =
  /^(datasets|tables|references)\/[A-Za-z0-9_][A-Za-z0-9_.-]*(\/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$/

// Pull plain text out of react-markdown's children (string | array | nodes).
function textOf(children) {
  if (children == null) return ""
  if (typeof children === "string") return children
  if (Array.isArray(children)) return children.map(textOf).join("")
  if (typeof children === "object" && children.props)
    return textOf(children.props.children)
  return String(children)
}

// Components depend on datasetScope (for the citation popup), so build them per
// scope. Memoized in Markdown so the object is stable across streaming re-renders.
function makeComponents(datasetScope) {
  return {
  a({ href, children, ...props }) {
    // Citation chip — an `okf-cite:<encoded id>` link (from preprocessCitations),
    // rendered as a source pill with a hover popup (kind + path + dataset). Not a
    // navigable link (the chat isn't a doc browser).
    if (typeof href === "string" && href.startsWith(CITE_SCHEME)) {
      const id = decodeURIComponent(href.slice(CITE_SCHEME.length))
      return <Citation id={id} datasetScope={datasetScope} />
    }
    return (
      <a href={href} target="_blank" rel="noreferrer noopener" {...props}>
        {children}
      </a>
    )
  },
  // Wrap GFM tables in the shared label-grid scroll container so chat markdown
  // tables get the SAME gapped, zebra-tinted label look + padded thin scrollbar
  // as the tool-result tables (index.css `.okf-label-grid`).
  table({ children, ...props }) {
    return (
      <div className="okf-label-grid">
        <table {...props}>{children}</table>
      </div>
    )
  },
  // Inline code (`x`) arrives here with no `language-` class (fenced blocks carry
  // one and are handled by `pre`→CodeView). When the token LOOKS LIKE a concept id
  // it renders as a distinct LABEL pill — but this is an INFERENCE from the string
  // shape, not a verified file reference, so deliberately NO file icon (an icon
  // would falsely assert the doc exists; a hallucinated id would look real).
  code({ className, children, ...props }) {
    const cls = className || ""
    if (!/language-/.test(cls)) {
      const txt = textOf(children)
      if (CONCEPT_ID_RE.test(txt)) {
        return (
          <span className="okf-concept-label" title={txt}>
            {txt}
          </span>
        )
      }
    }
    return (
      <code className={className} {...props}>
        {children}
      </code>
    )
  },
  // A fenced block arrives as <pre><code class="language-xxx">…</code></pre>.
  // Render the whole <pre> as a CodeView (reading the language + source off the
  // inner <code>); leave everything else untouched.
  pre({ children }) {
    const child = Array.isArray(children) ? children[0] : children
    const cls = child?.props?.className || ""
    const match = /language-(\w+)/.exec(cls)
    if (child?.props) {
      return (
        <CodeView code={textOf(child.props.children)} language={match?.[1]} />
      )
    }
    return <pre>{children}</pre>
  },
  }
}

export function Markdown({ children, datasetScope = null }) {
  const scopeKey = datasetScope
    ? `${datasetScope.data_domain}/${datasetScope.dataset}`
    : ""
  const components = useMemo(
    () => makeComponents(datasetScope),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [scopeKey]
  )
  return (
    <div className="okf-prose text-sm">
      <TooltipProvider delayDuration={150}>
        <ReactMarkdown
          remarkPlugins={REMARK}
          urlTransform={urlTransform}
          components={components}
        >
          {preprocessCitations(children)}
        </ReactMarkdown>
      </TooltipProvider>
    </div>
  )
}
