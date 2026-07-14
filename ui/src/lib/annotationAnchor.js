// Capture a durable anchor for a text selection in the rendered wiki.
//
// The wiki is rendered HTML, but the harvest agent edits the raw markdown source
// and a re-harvest rewrites the doc — so we deliberately DON'T anchor on a
// character offset (it goes stale instantly). Instead we capture a W3C-style
// TextQuoteSelector: the selected `quote` plus the MINIMAL surrounding
// `prefix`/`suffix` that makes the (prefix+quote+suffix) window UNIQUE in the
// rendered text. Two identical quotes on a page get different context, so the
// backend can tell them apart; the orphan check only needs the quote to still
// exist. `block_line` is a best-effort source-line hint read from a `data-sl`
// stamp the renderer puts on block elements (see ConceptDoc) — the agent uses it
// to jump close, never as the source of truth.

// How far to grow context, and the cap. Real docs disambiguate within a few
// words; the cap bounds a pathological (fully-repeated) passage.
const CONTEXT_STEP = 8
const CONTEXT_MAX = 160

// Count non-overlapping occurrences of `needle` in `haystack`.
function countOccurrences(haystack, needle) {
  if (!needle) return 0
  let n = 0
  let i = haystack.indexOf(needle)
  while (i !== -1) {
    n++
    i = haystack.indexOf(needle, i + needle.length)
  }
  return n
}

// Grow prefix/suffix outward from the selection until the window is unique in
// `full` (or we hit the cap). `start`/`end` are the selection's char offsets in
// `full`. Returns the minimal { prefix, suffix } that disambiguates.
export function minimalUniqueContext(full, start, end) {
  const quote = full.slice(start, end)
  // A quote that already occurs once needs no context at all.
  if (countOccurrences(full, quote) <= 1) return { prefix: "", suffix: "" }

  let pad = CONTEXT_STEP
  while (pad <= CONTEXT_MAX) {
    const prefix = full.slice(Math.max(0, start - pad), start)
    const suffix = full.slice(end, Math.min(full.length, end + pad))
    if (countOccurrences(full, prefix + quote + suffix) <= 1) {
      return { prefix, suffix }
    }
    pad += CONTEXT_STEP
  }
  // Couldn't disambiguate (a truly repeated passage) — return max context; the
  // backend falls back to occurrence order and the agent reasons about it.
  return {
    prefix: full.slice(Math.max(0, start - CONTEXT_MAX), start),
    suffix: full.slice(end, Math.min(full.length, end + CONTEXT_MAX)),
  }
}

// Nearest ancestor source line: walk up from `node` to the closest element
// carrying a `data-sl` stamp (the block's 1-based source line within the body).
function nearestBlockLine(node) {
  let el = node?.nodeType === Node.TEXT_NODE ? node.parentElement : node
  while (el && el !== document.body) {
    const sl = el.getAttribute && el.getAttribute("data-sl")
    if (sl != null) {
      const n = parseInt(sl, 10)
      if (!Number.isNaN(n)) return n
    }
    el = el.parentElement
  }
  return null
}

// Offset of `node`+`offset` within `root`'s textContent, by walking text nodes.
function offsetWithin(root, node, offset) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null)
  let total = 0
  let cur = walker.nextNode()
  while (cur) {
    if (cur === node) return total + offset
    total += cur.textContent.length
    cur = walker.nextNode()
  }
  // Node not under root (shouldn't happen for a contained selection).
  return null
}

// Build an anchor from the current window selection, IF it is non-empty and
// fully inside the rendered markdown BODY. Returns null otherwise (no popup).
//
// Only the `.okf-prose` body is annotatable — NOT the frontmatter header
// (type/tags/title/description), which isn't prose the agent can act on and
// whose text isn't in the body the backend re-anchors against (a header
// selection would falsely orphan the moment it's run). `container` is the
// wrapper around the whole doc; we resolve the body element within it and treat
// THAT as the capture root, so offsets / prefix / suffix / block_line all align
// to the body.
//
// Returns { quote, prefix, suffix, block_line, rect } — `rect` is the selection's
// bounding client rect, so the caller can position the "Annotate" popover above
// it. quote is trimmed of surrounding whitespace but keeps interior text as-is.
export function captureSelection(container) {
  const root =
    container?.matches?.(".okf-prose") === true
      ? container
      : container?.querySelector?.(".okf-prose") || null
  if (!root) return null

  const sel = typeof window !== "undefined" ? window.getSelection() : null
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null
  const range = sel.getRangeAt(0)
  // BOTH endpoints must be inside the body, so a selection that starts in the
  // header and drags into the body is rejected too (not just fully-header ones).
  if (
    !root.contains(range.startContainer) ||
    !root.contains(range.endContainer)
  )
    return null

  const quote = sel.toString().trim()
  if (!quote) return null

  const full = root.textContent || ""
  const startOff = offsetWithin(root, range.startContainer, range.startOffset)
  const endOff = offsetWithin(root, range.endContainer, range.endOffset)

  let prefix = ""
  let suffix = ""
  if (startOff != null && endOff != null && endOff > startOff) {
    // Re-derive the exact selected slice from offsets so trimming lines up.
    const rawStart = full.indexOf(quote, Math.max(0, startOff - 2))
    const s = rawStart === -1 ? startOff : rawStart
    const e = s + quote.length
    ;({ prefix, suffix } = minimalUniqueContext(full, s, e))
  }

  const rect = range.getBoundingClientRect()
  return {
    quote,
    prefix,
    suffix,
    block_line: nearestBlockLine(range.startContainer),
    rect,
  }
}
