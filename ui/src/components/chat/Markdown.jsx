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

import { CitationGroup } from "@/components/chat/Citation"
import { CodeView } from "@/components/chat/CodeView"
import { TooltipProvider } from "@/components/ui/tooltip"
import { parseCiteList } from "@/lib/sources"

const REMARK = [remarkGfm]

// react-markdown sanitizes hrefs and DROPS unknown URL schemes — our internal
// `okf-cite:` links would become "" and never reach the `a` renderer. This keeps
// them (and defers to the default transform for everything else).
function urlTransform(url) {
  if (typeof url === "string" && url.startsWith(CITE_SCHEME)) return url
  return defaultUrlTransform(url)
}

// Citations: the agent emits the SHORT tag `<c src="bird/formula_1/tables/races,
// https://example.com/x"></c>` after a claim (see the chat system prompt's
// <citations> block — the tag was shortened to save output tokens); stored history
// still carries the original `<cite …></cite>`, so BOTH parse: the short form is
// canonicalized to the long one up front and every regex below handles one shape.
// Sources are fully-qualified wiki doc addresses and/or web URLs. We rewrite each
// tag into ONE markdown link carrying the whole source list, using an internal
// `okf-cite:` scheme, then render that link as a single grouped citation badge
// (the `a` component below → CitationGroup).
// This rides the existing markdown link path — no rehype-raw / HTML-in-markdown
// dependency. A trailing INCOMPLETE tag (mid-stream, e.g. `<cite src="tab`) is
// stripped so it never flashes as raw text while tokens arrive.
//
// ONE badge per claim is the point: the model is told to put every source for a
// claim in a single tag, but it still sometimes emits back-to-back tags, so
// mergeAdjacentCites folds those into one before any link is generated. A row of
// separate pills after one sentence is exactly what we're avoiding.
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
// The agent now emits the SHORT tag `<c src="…"></c>`; canonicalize it to the
// long form BEFORE any other rewrite so the regexes above handle one shape.
// `\s+src` keeps `<c…` from ever matching a real tag (`<code>` has no boundary
// after the c), and a self-closing slip (`<c src="…"/>`) loses its slash to
// become the long EMPTY form. `</c\s*>` cannot match `</cite>` (the char after
// `c` is `i`), so a long closer is never double-converted.
const CITE_SHORT_OPEN_RE = /<c(\s+src="[^"]*"\s*)\/?>/gi
const CITE_SHORT_CLOSE_RE = /<\/c\s*>/gi
function canonicalizeShortCites(md) {
  return md
    .replace(CITE_SHORT_OPEN_RE, "<cite$1>")
    .replace(CITE_SHORT_CLOSE_RE, "</cite>")
}
// A trailing PARTIAL tag at the very end of the (mid-stream) buffer — a partial
// opener (`<c src="tab`, `<cite src="tab`) OR a partial closer (`</c`/`</cite`
// with no `>` yet). The `\/?` is what makes it catch the closer too (the old
// version only stripped a partial opener, so a streamed `…</cite` flashed as
// literal text before its `>` arrived). The `\b` after the name is what keeps
// prose safe: `Vec<char` has no boundary after the `c`, so only a real bare
// `<c`/`<cite` prefix (followed by space, quote, or end) is clipped. Completed
// on the next frame.
const CITE_PARTIAL_RE = /<\/?c(?:ite)?\b[^>]*$/i
const CITE_SCHEME = "okf-cite:"

// While the typewriter is still revealing a block, hold back / repair the
// UNSTABLE TAIL so half-arrived markup never flashes as literal "ghost" text:
//  - a partial `<cite`/`</cite` PREFIX at the very end (`<`, `<c`, `<ci`, …) —
//    CITE_PARTIAL_RE only matches once "cite" is complete, so the first few
//    characters of every citation otherwise flash at the line end;
//  - an unbalanced `**` (bold) / `*` (italic) or single-backtick run —
//    react-markdown renders the opener literally until its pair arrives
//    ("**Wolfs" shows raw asterisks). BALANCING (appending the closer)
//    renders the in-progress span styled as intended instead, with no visual
//    jump when the real closer lands.
// Skipped inside an open ``` fence (CodeView owns that text verbatim), and
// applied ONLY while streaming — a completed message renders exactly as sent.
const CITE_PREFIX_TAIL_RE = /<\/?(?:c(?:i(?:t(?:e)?)?)?)?$/i

// Balance emphasis with a tiny SCANNER, not a `**`-pair parity count — the
// parity version flickered two ways: a chunk boundary landing BETWEEN the two
// stars of a `**` left a lone trailing `*` (parity "balanced" → literal
// asterisk for a frame), and bullets (`* item`) or asterisks inside inline
// code threw the count off, so bolds that were ALREADY closed toggled between
// styled and literal from frame to frame. The scanner:
//  1. masks inline code spans (a `*` inside backticks is not emphasis);
//  2. walks star RUNS (`*` italic, `**` bold, `***`+ both — so a balanced
//     `***key point***` never leaves a phantom half-open delimiter);
//  3. skips list bullets (line-start `* ` after only whitespace);
//  4. applies FLANKING rules so prose asterisks are never read as emphasis:
//     a run may OPEN only after start/whitespace/an opening bracket or quote
//     and before a non-space, non-closing-punct char; it may CLOSE only after
//     a non-space, non-opening-bracket char. `COUNT(*)`, `3*4`, and
//     `price * quantity` all fail both tests and stay plain text — the old
//     scanner pushed them as openers and appended a spurious closer to every
//     streaming frame;
//  5. tracks open bold/italic as a stack and appends the missing closers in
//     reverse order (`**a *b` → `**a *b***`).
// Deliberately conservative: intraword emphasis (`un**usual**`) is not
// repaired — it renders literally for a frame and settles when complete,
// which is the old parity behavior and strictly better than mis-styling
// arithmetic. Underscore emphasis is not handled — the model writes `**`/`*`.
function scanEmphasis(text) {
  const masked = text.replace(/`[^`]*`/g, (m) => "\0".repeat(m.length))
  const stack = []
  const toggle = (delim, canOpen, canClose) => {
    if (canClose) {
      const open = stack.lastIndexOf(delim)
      if (open >= 0) {
        stack.length = open
        return
      }
    }
    if (canOpen) stack.push(delim)
  }
  for (let i = 0; i < masked.length; i++) {
    if (masked[i] !== "*") continue
    let j = i
    while (masked[j + 1] === "*") j++
    const n = j - i + 1
    const prev = i > 0 ? masked[i - 1] : ""
    const next = j + 1 < masked.length ? masked[j + 1] : ""
    // A single `*`: bullet iff at line start (only blanks before it on the
    // line) and followed by a space — that's list syntax, not emphasis.
    if (n === 1) {
      const lineStart = masked.lastIndexOf("\n", i - 1) + 1
      if (/^[ \t]*$/.test(masked.slice(lineStart, i)) && next === " ") {
        i = j
        continue
      }
    }
    const canOpen =
      next !== "" &&
      !/[\s)\]},.;:!?'"]/.test(next) &&
      (prev === "" || /[\s([{"'\-–—]/.test(prev))
    const canClose = prev !== "" && !/[\s([{]/.test(prev)
    if (n >= 3) {
      toggle("**", canOpen, canClose)
      toggle("*", canOpen, canClose)
    } else {
      toggle(n === 2 ? "**" : "*", canOpen, canClose)
    }
    i = j
  }
  return stack
}

function balanceEmphasis(md) {
  let out = md.replace(/(^|[\s(])\*{1,3}$/, "$1")
  // A trailing single star in CLOSER position (non-space before it) can be
  // the half-arrived first char of a `**` closer — the chunk boundary landed
  // between the two stars. If bold is the innermost open emphasis at that
  // point, complete the closer; reading it as an italic toggle re-styled the
  // whole bold span for a frame (the reported flicker).
  if (/[^\s*]\*$/.test(out)) {
    const before = scanEmphasis(out.slice(0, -1))
    if (before[before.length - 1] === "**") out += "*"
  }
  const stack = scanEmphasis(out)
  for (let j = stack.length - 1; j >= 0; j--) out += stack[j]
  return out
}

function stripStreamTail(md) {
  if (!md) return md || ""
  let out = md.replace(CITE_PREFIX_TAIL_RE, "")
  const inFence = (out.match(/^```/gm) || []).length % 2 === 1
  if (!inFence) {
    // Close an open inline-code span FIRST so the emphasis scanner's mask
    // sees it as code and ignores any `*` inside it.
    if ((out.match(/`/g) || []).length % 2 === 1) out += "`"
    out = balanceEmphasis(out)
  }
  return out
}

// Turn a `src` list into ONE citation-badge markdown link carrying every source.
// Empty → "" (drops a src-less tag entirely).
//
// encodeURIComponent does NOT escape parentheses, and an unescaped ")" would
// TERMINATE the markdown link early — so a perfectly ordinary source URL like
// `…/wiki/Golf_(car)` would render as broken half-link plus literal text. Escape
// them explicitly (decodeURIComponent reverses %28/%29 on the way out).
function encodeCiteHref(list) {
  return encodeURIComponent(list).replace(/\(/g, "%28").replace(/\)/g, "%29")
}

function citeBadge(src) {
  const items = parseCiteList(src)
  if (items.length === 0) return ""
  // The link TEXT is unused (CitationGroup renders its own label) but must be
  // non-empty for react-markdown to emit an anchor at all.
  return `[cite](${CITE_SCHEME}${encodeCiteHref(items.join(","))})`
}

// Fold back-to-back cite tags (separated by nothing, spaces, or a stray comma)
// into ONE tag with the source lists concatenated — so a claim the model backed
// with two adjacent tags still renders as a single badge. Runs BEFORE the tag→link
// rewrites, and loops because each merge can create a new adjacency (a,b,c).
const CITE_ADJACENT_RE =
  /<cite\s+src="([^"]*)"\s*>\s*(?:<\/cite\s*>)?[\s,]*(?=<cite\s+src=")/gi
function mergeAdjacentCites(md) {
  let out = md
  for (let pass = 0; pass < 8; pass++) {
    const next = out.replace(
      CITE_ADJACENT_RE,
      (_m, src) => `<cite src="${src},`
    )
    // The replacement above leaves `<cite src="a,<cite src="b">` — collapse the
    // inner opener so the two srcs share one tag.
    const joined = next.replace(
      /<cite\s+src="([^"]*),<cite\s+src="/gi,
      '<cite src="$1,'
    )
    if (joined === out) break
    out = joined
  }
  return out
}

function preprocessCitations(md) {
  // Guard on anything that could be a citation tag: an opener (`<c`, which
  // `<cite` also contains) or an orphan closer (`</c`, which `</cite` also
  // contains — note `</cite>` does NOT contain `<c`).
  if (!md || (md.indexOf("<c") === -1 && md.indexOf("</c") === -1))
    return md || ""
  // 0) Short tags become long ones, so the rewrites below handle ONE form.
  let out = canonicalizeShortCites(md)
  // 1) Adjacent tags become one, so one claim yields one badge.
  out = mergeAdjacentCites(out)
  // 2) Content-bearing tags first: `<cite src="…">gloss</cite>` → badge (gloss dropped,
  //    closer consumed). Non-greedy so adjacent cites aren't swallowed as one span.
  out = out.replace(CITE_TAG_CONTENT_RE, (_m, src) => citeBadge(src))
  // 3) Empty / self-adjacent tags: `<cite src="…"></cite>` or `<cite src="…">`.
  out = out.replace(CITE_TAG_EMPTY_RE, (_m, src) => citeBadge(src))
  // 4) Drop a dangling partial OPENER/CLOSER at the very end (still streaming, e.g. `<c src="tab`).
  out = out.replace(CITE_PARTIAL_RE, "")
  // 5) Belt-and-suspenders: strip any orphan `</cite>` left with no matching opener
  //    (e.g. a mid-stream frame that delivered the closer before the opener, or a
  //    malformed tag) so a bare `</cite>` never renders as literal text.
  out = out.replace(CITE_ORPHAN_CLOSE_RE, "")
  return out
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

// Components depend on datasetScope + the turn's web/wiki source indexes (all
// feed the citation cards) + the doc-peek opener, so build them per turn.
// Memoized in Markdown so the object is stable across streaming re-renders.
function makeComponents(datasetScope, webSources, wikiSources, onOpenDoc) {
  return {
    a({ href, children, ...props }) {
      // Citation badge — an `okf-cite:<encoded source list>` link (from
      // preprocessCitations), rendered as ONE grouped pill with a paginated card.
      if (typeof href === "string" && href.startsWith(CITE_SCHEME)) {
        const list = decodeURIComponent(href.slice(CITE_SCHEME.length))
        return (
          <CitationGroup
            sources={parseCiteList(list)}
            datasetScope={datasetScope}
            webSources={webSources}
            wikiSources={wikiSources}
            onOpenDoc={onOpenDoc}
          />
        )
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

export function Markdown({
  children,
  datasetScope = null,
  streaming = false,
  webSources = null,
  wikiSources = null,
  onOpenDoc = null,
}) {
  const scopeKey = datasetScope
    ? `${datasetScope.data_domain}/${datasetScope.dataset}`
    : ""
  const components = useMemo(
    () => makeComponents(datasetScope, webSources, wikiSources, onOpenDoc),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [scopeKey, webSources, wikiSources, onOpenDoc]
  )
  const source = preprocessCitations(
    streaming ? stripStreamTail(children) : children
  )
  return (
    <div className="okf-prose text-sm">
      <TooltipProvider delayDuration={150}>
        <ReactMarkdown
          remarkPlugins={REMARK}
          urlTransform={urlTransform}
          components={components}
        >
          {source}
        </ReactMarkdown>
      </TooltipProvider>
    </div>
  )
}
