// Source helpers shared by the web-search result card and the citation chips.
//
// A "source" in the chat is one of two things, and both flow through the same
// citation UI:
//   - a WIKI DOC, identified by its concept id (`tables/races`);
//   - a WEB PAGE, identified by its URL (from the web_search tool).
// The agent cites both with `<c src="…"></c>` (legacy history: `<cite …>`);
// everything below is what lets one badge describe a mixed set of them.

// A concept id: one of the OKF bundle's top-level dirs followed by 1+ slash-
// joined segments (mirrors okf_core.paths). Used to tell an id from a URL.
// `external/` is a citable top dir too (cross-dataset pair docs).
const CONCEPT_ID_RE =
  /^(datasets|tables|references|external)\/[A-Za-z0-9_][A-Za-z0-9_.-]*(\/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$/

// A FULLY-QUALIFIED wiki source: `<data_domain>/<dataset>/<concept id>` — the
// form the agent is prompted to cite (and the shape of a semantic_search vector
// key). Groups: 1 = domain, 2 = dataset, 3 = the concept id.
const QUALIFIED_ID_RE =
  /^([A-Za-z0-9_][A-Za-z0-9_.-]*)\/([A-Za-z0-9_][A-Za-z0-9_.-]*)\/((?:datasets|tables|references|external)\/[A-Za-z0-9_][A-Za-z0-9_.-]*(?:\/[A-Za-z0-9_][A-Za-z0-9_.-]*)*)$/

export function isWebSource(item) {
  return typeof item === "string" && /^https?:\/\//i.test(item)
}

// Decompose a wiki source into its parts, or null when it isn't one.
// A BARE id (`tables/races` — old history, or a model slip) has no location:
// `{conceptId, dataDomain: null, dataset: null}`. A QUALIFIED id
// (`bird/formula_1/tables/races`) carries its own. Bare is checked FIRST so a
// deep bare id (`references/known_issues/tables/x`) is never misread as a
// qualified one whose domain happens to be named like a top-level dir.
export function parseWikiSource(item) {
  if (typeof item !== "string") return null
  if (CONCEPT_ID_RE.test(item))
    return { conceptId: item, dataDomain: null, dataset: null }
  const m = QUALIFIED_ID_RE.exec(item)
  if (m) return { conceptId: m[3], dataDomain: m[1], dataset: m[2] }
  return null
}

export function isConceptId(item) {
  return parseWikiSource(item) !== null
}

// The bare concept id of a wiki source, qualified or not — what glyphs, kind
// labels, and doc-peek lookups key on. Non-wiki items pass through unchanged.
export function conceptIdOf(item) {
  const wiki = parseWikiSource(item)
  return wiki ? wiki.conceptId : String(item ?? "")
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

// Split a `<c src="…">` (or legacy `<cite src="…">`) value into individual sources.
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

// Build a concept-id → {data_domain, dataset} index from the conversation's
// wiki tool traffic, so a cited BARE concept id can still be OPENED (the chat's
// doc-peek panel) in an unscoped conversation. New citations are fully
// qualified and don't need this; it's the fallback for stored history and model
// slips. The agent may cite any doc a tool SHOWED it — not just ones it
// read_page'd — so every feed that names concept ids contributes:
//   - tool CALL args carrying {data_domain, dataset, concept_id} (read_page /
//     get_backlinks — for scoped conversations the server folds the scope into
//     the args, so the event always shows the resolved location);
//   - read_page / glob / grep RESULTS, which echo their location;
//   - get_backlinks RESULTS ({id, …} — located via their own call's args);
//   - semantic_search RESULTS, whose `concept_id` is the full vector key
//     `<domain>/<dataset>/<concept path>`.
// First writer wins, so a doc read twice keeps its first (authoritative)
// location. Includes external/ pair docs.
const WIKI_TOP_RE = /^(datasets|tables|references|external)\//

export function collectWikiSources(events) {
  const index = new Map()
  const add = (cid, dd, ds) => {
    if (typeof cid !== "string" || !WIKI_TOP_RE.test(cid)) return
    if (!dd || !ds || index.has(cid)) return
    index.set(cid, { data_domain: String(dd), dataset: String(ds) })
  }
  // Each call's location by tool-call id, so results that name concept ids
  // WITHOUT one (get_backlinks) inherit it. Starts stream before results.
  const callLoc = new Map()
  for (const ev of events || []) {
    if (ev.type !== "tool") continue
    const content = coerce(ev.content)
    if (ev.tool_start) {
      if (!content || typeof content !== "object") continue
      const dd = content.data_domain
      const ds = content.dataset
      if (!dd || !ds) continue
      if (ev.id) callLoc.set(ev.id, { dd, ds })
      add(content.concept_id, dd, ds)
      continue
    }
    if (ev.tool_name === "read_page") {
      if (content && typeof content === "object")
        add(content.concept_id, content.data_domain, content.dataset)
    } else if (ev.tool_name === "glob") {
      if (Array.isArray(content))
        for (const r of content) add(r?.concept_id, r?.data_domain, r?.dataset)
    } else if (ev.tool_name === "grep") {
      const matches = Array.isArray(content?.matches) ? content.matches : []
      for (const r of matches)
        add(r?.concept_id, content.data_domain, content.dataset)
    } else if (ev.tool_name === "get_backlinks") {
      const loc = callLoc.get(ev.id)
      if (loc && Array.isArray(content))
        for (const r of content) add(r?.id, loc.dd, loc.ds)
    } else if (ev.tool_name === "semantic_search") {
      const results = Array.isArray(content)
        ? content
        : Array.isArray(content?.results)
          ? content.results
          : []
      for (const r of results) {
        const key = typeof r?.concept_id === "string" ? r.concept_id : ""
        const m = /^([^/]+)\/([^/]+)\/(.+)$/.exec(key)
        if (m) add(m[3], m[1], m[2])
      }
    }
  }
  return index
}

// A stable signature over ALL tool events (calls and results), so the wiki
// source index can be memoized without re-running on every streamed token.
export function wikiSourcesSignature(events) {
  let sig = ""
  for (const ev of events || []) {
    if (ev.type !== "tool") continue
    const len = typeof ev.content === "string" ? ev.content.length : ev.content ? 1 : 0
    sig += `${ev.id || ""}${ev.tool_start ? "s" : "r"}${len}|`
  }
  return sig
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
