// Source helpers shared by the web-search result card and the citation chips.
//
// A "source" in the chat is one of two things, and both flow through the same
// citation UI:
//   - a WIKI DOC, identified by its concept id (`tables/races`);
//   - a WEB PAGE, identified by its URL (from the web_search tool).
// The agent cites both with `<cite src="…">`; everything below is what lets one
// badge describe a mixed set of them.

// A concept id: one of the OKF bundle's top-level dirs followed by 1+ slash-
// joined segments (mirrors okf_core.paths). Used to tell an id from a URL.
const CONCEPT_ID_RE =
  /^(datasets|tables|references)\/[A-Za-z0-9_][A-Za-z0-9_.-]*(\/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$/

export function isWebSource(item) {
  return typeof item === "string" && /^https?:\/\//i.test(item)
}

export function isConceptId(item) {
  return typeof item === "string" && CONCEPT_ID_RE.test(item)
}

// The display host of a URL ("reuters.com"), or "" when it won't parse.
export function hostOf(url) {
  try {
    return new URL(String(url)).hostname.replace(/^www\./, "")
  } catch {
    return ""
  }
}

// Where a source's favicon comes from: the SOURCE ORIGIN itself, not a
// third-party icon service. Google's/DuckDuckGo's favicon endpoints are more
// reliable, but they'd tell a third party every domain our users' agents read —
// a privacy review we don't need. The site already published the page we're
// citing, so asking it for its own icon adds no new disclosure, and every
// failure degrades to the monogram tile in SourceIcon. Swap this one function if
// a deployment prefers a proxy.
export function faviconUrl(url) {
  const host = hostOf(url)
  return host ? `https://${host}/favicon.ico` : ""
}

// Split a `<cite src="…">` value into individual sources.
//
// Comma-separated, but a URL may legally CONTAIN a comma — so after splitting we
// re-join any fragment that is neither a URL nor a concept id back onto the
// previous item (restoring its comma). `a,b` → [a, b]; `https://x/a,b` stays one
// URL, because "b" alone isn't a valid item.
export function parseCiteList(src) {
  const raw = String(src || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
  const out = []
  for (const item of raw) {
    if (out.length && !isWebSource(item) && !isConceptId(item)) {
      const prev = out[out.length - 1]
      // Only a URL can absorb a stray fragment; a bad concept id stays its own
      // (broken) item so it's visible rather than silently glued onto a doc id.
      if (isWebSource(prev)) {
        out[out.length - 1] = `${prev},${item}`
        continue
      }
    }
    out.push(item)
  }
  return out
}

// Tool content arrives as an object or a JSON string; normalize to a value.
function coerce(content) {
  if (typeof content !== "string") return content
  const t = content.trim()
  if (!t.startsWith("{") && !t.startsWith("[")) return content
  try {
    return JSON.parse(t)
  } catch {
    return content
  }
}

// Build a URL → metadata index from a turn's web_search tool RESULTS, so a
// citation can show the page's title, date, and snippet instead of a bare link.
// Cheap and idempotent: the same URL returned by two searches keeps the first
// (richer) entry rather than being overwritten by a thinner one.
export function collectWebSources(events) {
  const index = new Map()
  for (const ev of events || []) {
    if (ev.type !== "tool" || ev.tool_name !== "web_search" || ev.tool_start) continue
    const content = coerce(ev.content)
    const results = Array.isArray(content?.results) ? content.results : []
    for (const r of results) {
      const url = typeof r?.url === "string" ? r.url : ""
      if (!url) continue
      const existing = index.get(url)
      const next = {
        url,
        host: hostOf(url),
        title: typeof r.title === "string" ? r.title : "",
        publishedDate: typeof r.published_date === "string" ? r.published_date : "",
        text: typeof r.text === "string" ? r.text : "",
      }
      if (!existing || (!existing.title && next.title)) index.set(url, next)
    }
  }
  return index
}

// A stable signature over the web_search results in a turn's events, so the
// index can be memoized without re-running on every streamed token (the events
// array identity changes on every flush).
export function webSourcesSignature(events) {
  let sig = ""
  for (const ev of events || []) {
    if (ev.type !== "tool" || ev.tool_name !== "web_search" || ev.tool_start) continue
    const len = typeof ev.content === "string" ? ev.content.length : 1
    sig += `${ev.id || ""}:${len}|`
  }
  return sig
}

// Format a publication date for display ("Jun 6, 2026"). Dates come back as
// YYYY-MM-DD; anything unparseable passes through unchanged.
export function formatPublished(date) {
  const raw = String(date || "").trim()
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw)
  if (!m) return raw
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]))
  if (Number.isNaN(d.getTime())) return raw
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  })
}
