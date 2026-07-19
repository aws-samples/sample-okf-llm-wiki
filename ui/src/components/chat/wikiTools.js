// Per-tool presentation for the wiki chat — the analog of Sparky's
// toolClassification.js (which renders web_search vs web_extract vs generic
// distinctly). Our tools are all well-defined wiki reads with known return
// shapes (see services/consumption_mcp/tools.py), so we render each one
// meaningfully instead of dumping raw JSON:
//
//   - a status LABEL keyed off the tool + its args ("Searching “races”",
//     "Reading tables/races", "Grep “winner”"), shimmering while it runs;
//   - a result SUMMARY ("12 results", "3 backlinks", "846 lines");
//   - a structured DETAIL the card renders (result cards / chips / line snippets)
//     rather than a JSON blob.
//
// A tool we don't recognize falls back to a generic name + raw args/result, so
// nothing ever breaks — new tools just look plain until we teach this module.

import {
  BarChart3Icon,
  BookOpenIcon,
  DatabaseIcon,
  FolderTreeIcon,
  Link2Icon,
  ListTreeIcon,
  ScanSearchIcon,
  SearchIcon,
  TerminalIcon,
  TextSearchIcon,
  WrenchIcon,
} from "lucide-react"

const ICONS = {
  list_domains: DatabaseIcon,
  list_declared_domains: ListTreeIcon,
  // semantic search = "scan" the meaning of the corpus (not an AI/sparkle glyph)
  search_domains: ScanSearchIcon,
  semantic_search: ScanSearchIcon,
  list_directory: FolderTreeIcon,
  read_page: BookOpenIcon,
  get_backlinks: Link2Icon,
  glob: SearchIcon,
  grep: TextSearchIcon,
  // run_sql = a live query against the catalog (terminal/prompt glyph)
  run_sql: TerminalIcon,
  // render_chart is normally lifted into its own inline chart block (see
  // buildMessageBlocks), so it rarely renders as a tool card — but keep an icon +
  // label so an edge case (e.g. a raw tool listing) doesn't fall through to raw.
  render_chart: BarChart3Icon,
}

export function toolIcon(toolName) {
  return ICONS[toolName] || WrenchIcon
}

function s(v) {
  return v == null ? "" : String(v)
}

// A short scope suffix like " · bird/schools" when the args carry a location.
function scopeSuffix(args) {
  if (!args || typeof args !== "object") return ""
  const dd = args.data_domain
  const ds = args.dataset
  if (dd && ds) return ` · ${dd}/${ds}`
  if (dd) return ` · ${dd}`
  return ""
}

// The running/label text (Sparky's getToolDisplayText), keyed off tool + args.
// `running=true` while the tool is in flight, else the settled "done" label.
export function toolLabel(toolName, args, running) {
  const a = args && typeof args === "object" ? args : {}
  switch (toolName) {
    case "list_domains":
      // Returns (domain, dataset) PAIRS — i.e. datasets, not domains.
      return running ? "Listing datasets" : "Datasets"
    case "list_declared_domains":
      return running ? "Listing domains" : "Domains"
    case "search_domains":
    case "semantic_search": {
      const q = s(a.query)
      const quoted = q ? `“${q}”` : ""
      return running ? `Searching ${quoted}`.trim() : `Searched ${quoted}`.trim()
    }
    case "list_directory": {
      // Server folds the conversation scope into args (chat.server), but guard
      // against missing location so the label never reads "undefined/undefined".
      const base = [s(a.data_domain), s(a.dataset)].filter(Boolean).join("/")
      const loc = a.path ? [base, s(a.path)].filter(Boolean).join("/") : base
      const label = loc || "wiki"
      return running ? `Browsing ${label}` : `Browsed ${label}`
    }
    case "read_page":
      return running ? `Reading ${s(a.concept_id)}` : `Read ${s(a.concept_id)}`
    case "get_backlinks":
      return running ? `Finding backlinks to ${s(a.concept_id)}` : `Backlinks to ${s(a.concept_id)}`
    case "glob": {
      const p = s(a.pattern)
      return running ? `Globbing “${p}”` : `Glob “${p}”`
    }
    case "grep": {
      const p = s(a.pattern)
      return `${running ? "Grep" : "Grep"} “${p}”${scopeSuffix(a)}`
    }
    case "run_sql": {
      // Collapse whitespace + trim the query so a multi-line SQL reads on one line.
      const q = s(a.sql).replace(/\s+/g, " ").trim()
      const short = q.length > 48 ? `${q.slice(0, 48)}…` : q
      const label = short ? `“${short}”` : ""
      return running ? `Querying ${label}`.trim() : `Queried ${label}`.trim()
    }
    case "render_chart": {
      const t = s(a.title)
      const label = t ? `“${t}”` : ""
      return running ? `Charting ${label}`.trim() : `Charted ${label}`.trim()
    }
    default: {
      const name = prettyName(toolName)
      return running ? `Running ${name}` : name
    }
  }
}

export function prettyName(toolName) {
  if (!toolName) return "Tool"
  return toolName
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(" ")
}

// Some tools return a JSON string; normalize to a value.
function coerce(content) {
  if (typeof content !== "string") return content
  const t = content.trim()
  if ((t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]"))) {
    try {
      return JSON.parse(t)
    } catch {
      return content
    }
  }
  return content
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`

// Parse a completed tool result into a structured view the detail renderer can
// show: { summary, kind, ... }.
//   table — { columns:[{key,header,mono?,wrap?}], rows:[{<key>:value}] }
//           (the primary shape: every list tool renders as a real table)
//   chips — compact monospace pills (list_directory entries)
//   none  — summary only, no expandable body (read_page / index dir)
//   raw   — JSON fallback for anything unrecognized
export function parseToolResult(toolName, rawContent) {
  const content = coerce(rawContent)

  switch (toolName) {
    case "list_domains": {
      const arr = Array.isArray(content) ? content : []
      return {
        summary: plural(arr.length, "dataset"),
        kind: "table",
        columns: [
          { key: "data_domain", header: "Domain", mono: true },
          { key: "dataset", header: "Dataset", mono: true },
        ],
        rows: arr.map((d) => ({
          data_domain: d.data_domain,
          dataset: d.dataset,
        })),
      }
    }
    case "list_declared_domains": {
      const arr = Array.isArray(content) ? content : []
      return {
        summary: plural(arr.length, "domain"),
        kind: "table",
        columns: [
          { key: "domain", header: "Domain", mono: true },
          { key: "description", header: "Description", wrap: true },
        ],
        rows: arr.map((d) => ({
          domain: d.data_domain,
          description: d.description,
        })),
      }
    }
    case "search_domains":
    case "semantic_search": {
      const arr = Array.isArray(content) ? content : []
      const hasDist = arr.some((r) => r.distance != null)
      const columns = [
        { key: "concept", header: "Concept", mono: true },
        { key: "title", header: "Title" },
        { key: "description", header: "Description", wrap: true },
      ]
      if (hasDist) columns.push({ key: "score", header: "Score", mono: true })
      return {
        summary: plural(arr.length, "result"),
        kind: "table",
        columns,
        rows: arr.map((r) => ({
          concept: r.concept_id,
          title: r.title,
          description: r.description,
          score: r.distance != null ? Number(r.distance).toFixed(3) : "",
        })),
      }
    }
    case "list_directory": {
      const entries = Array.isArray(content?.entries) ? content.entries : null
      if (entries) {
        return {
          summary: plural(entries.length, "entry", "entries"),
          kind: "chips",
          items: entries.map((e) => (e.type === "dir" ? `${e.name}/` : e.name)),
        }
      }
      // index.md present → the directory has a doc, not a flat listing
      return { summary: "index", kind: "none" }
    }
    case "read_page": {
      if (content && typeof content === "object") {
        const lines = content.total_lines
        return {
          summary: lines != null ? `${plural(lines, "line")}` : "read",
          kind: "none",
        }
      }
      return { summary: "read", kind: "none" }
    }
    case "get_backlinks": {
      const arr = Array.isArray(content) ? content : []
      return {
        summary: plural(arr.length, "backlink"),
        kind: "table",
        columns: [
          { key: "concept", header: "Concept", mono: true },
          { key: "title", header: "Title" },
          { key: "heading", header: "Section", wrap: true },
        ],
        rows: arr.map((b) => ({
          concept: b.id,
          title: b.title,
          heading: b.heading,
        })),
      }
    }
    case "glob": {
      const arr = Array.isArray(content) ? content : []
      return {
        summary: plural(arr.length, "match", "matches"),
        kind: "table",
        columns: [{ key: "concept", header: "Concept", mono: true }],
        rows: arr.map((m) => ({ concept: m.concept_id })),
      }
    }
    case "grep": {
      const matches = Array.isArray(content?.matches) ? content.matches : []
      return {
        summary:
          plural(matches.length, "match", "matches") + (content?.truncated ? "+" : ""),
        kind: "table",
        columns: [
          { key: "concept", header: "Concept", mono: true },
          { key: "line", header: "Line", mono: true, align: "right" },
          { key: "text", header: "Match", mono: true, wrap: true },
        ],
        rows: matches.map((m) => ({
          concept: m.concept_id,
          line: m.line_number,
          text: m.line,
        })),
      }
    }
    case "render_chart": {
      // The chart itself renders as its own block below the reasoning; in the
      // timeline it's a LABEL-ONLY step (no expandable body — the ack the tool
      // returns is just "rendered", nothing worth disclosing). kind:"none" → the
      // step shows its label with no chevron/detail.
      return { summary: "", kind: "none" }
    }
    case "run_sql": {
      // { columns:[name], rows:[{name:value}], row_count, truncated }. Render as a
      // generic result grid keyed by the returned column names (all monospace —
      // it's tabular data). A truncated result gets a "+" on the count.
      const cols = Array.isArray(content?.columns) ? content.columns : []
      const rows = Array.isArray(content?.rows) ? content.rows : []
      return {
        summary:
          plural(content?.row_count ?? rows.length, "row") +
          (content?.truncated ? "+" : ""),
        kind: "table",
        columns: cols.map((name) => ({ key: name, header: name, mono: true })),
        // A SQL NULL comes back as null — show a muted "NULL" so it's distinct
        // from an empty string rather than rendering as blank.
        rows: rows.map((r) => {
          const out = {}
          for (const name of cols) out[name] = r[name] == null ? "NULL" : r[name]
          return out
        }),
      }
    }
    default:
      return { summary: "", kind: "raw", raw: content }
  }
}
