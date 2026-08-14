// Per-tool presentation for the wiki chat — the analog of Sparky's
// toolClassification.js (which renders web_search vs web_extract vs generic
// distinctly). Every tool here has a known return shape (the wiki reads in
// services/consumption_mcp/tools.py, plus run_sql and web_search), so we render
// each one meaningfully instead of dumping raw JSON:
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
  GlobeIcon,
  Link2Icon,
  ListTreeIcon,
  MessageCircleQuestionIcon,
  ScanSearchIcon,
  SearchIcon,
  SigmaIcon,
  TerminalIcon,
  TextSearchIcon,
  WrenchIcon,
} from "lucide-react"

import { hostOf } from "@/lib/sources"

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
  // Attested Computations: canonical parameterized SQL (sigma = a blessed
  // aggregate, deliberately distinct from run_sql's ad-hoc terminal)
  list_computations: SigmaIcon,
  describe_computation: SigmaIcon,
  run_computation: SigmaIcon,
  // web_search = the one tool that reads OUTSIDE the org (globe, deliberately
  // distinct from the wiki's own search glyphs)
  web_search: GlobeIcon,
  // ask_human = a pause to ask the user clarifying questions (question glyph)
  ask_human: MessageCircleQuestionIcon,
  // render_chart is lifted into its own inline chart block and never appears in
  // the timeline (see buildMessageBlocks) — but keep an icon + label so an edge
  // case (e.g. a raw tool listing) doesn't fall through to raw.
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
    case "list_computations":
      return running ? "Listing computations" : "Computations"
    case "describe_computation": {
      const n = s(a.name)
      return running ? `Reading computation ${n}` : `Computation ${n}`
    }
    case "run_computation": {
      const n = s(a.name)
      return running
        ? `Computing ${n}`.trim()
        : `Computed ${n}`.trim()
    }
    case "web_search": {
      // Any period lives in the query text itself (the tool has no date args —
      // it steers time by query), so the query IS the whole label.
      const q = s(a.query)
      const quoted = q ? `“${q}”` : ""
      const verb = running ? "Searching the web" : "Web search"
      return `${verb} ${quoted}`.trim()
    }
    case "ask_human": {
      // args.questions is the list the model asked; count them for the label.
      const n = Array.isArray(a.questions) ? a.questions.length : 0
      const suffix = n ? ` · ${plural(n, "question")}` : ""
      return running ? "Asking you" : `Asked you${suffix}`
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
//   table   — { columns:[{key,header,mono?,wrap?}], rows:[{<key>:value}] }
//             (the primary shape: every list tool renders as a real table)
//   sources — { items:[{url,title,host,publishedDate}] } — web_search: favicon +
//             title + host rows, each a link out
//   chips   — compact monospace pills (list_directory entries)
//   qa      — { pairs:[{prompt,answer}] } — ask_human: the questions the agent
//             asked paired with what the user answered (so the selection is
//             visible in the transcript instead of buried in raw JSON)
//   none    — summary only, no expandable body (read_page / index dir)
//   raw     — JSON fallback for anything unrecognized
export function parseToolResult(toolName, rawContent) {
  const content = coerce(rawContent)

  switch (toolName) {
    case "list_domains": {
      // Paginated shape {datasets, next_cursor}; a bare array is the legacy
      // pre-pagination reply (old transcripts re-rendered after a deploy).
      const arr = Array.isArray(content)
        ? content
        : Array.isArray(content?.datasets)
          ? content.datasets
          : []
      const more = Boolean(content?.next_cursor)
      return {
        summary: plural(arr.length, "dataset") + (more ? " (more pages)" : ""),
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
      // Unreachable in practice: the chart renders as its own block and never
      // enters the timeline (see buildMessageBlocks). Kept as a safe fallback —
      // label only, no expandable body (the ack is just "rendered").
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
    case "list_computations": {
      const arr = Array.isArray(content?.computations) ? content.computations : []
      return {
        summary: plural(arr.length, "computation"),
        kind: "table",
        columns: [
          { key: "computation", header: "Computation", mono: true },
          { key: "title", header: "Title", wrap: true },
          { key: "runtime", header: "Runtime", mono: true },
          { key: "verification", header: "Verification", mono: true },
        ],
        rows: arr.map((c) => ({
          computation: c.computation,
          title: c.title,
          runtime: c.runtime,
          verification: c.verification,
        })),
      }
    }
    case "describe_computation": {
      const params = Array.isArray(content?.parameters) ? content.parameters : []
      return {
        summary: content?.error
          ? "not found"
          : `${plural(params.length, "parameter")} · ${s(content?.verification) || "unverified"}`,
        kind: "table",
        columns: [
          { key: "name", header: "Parameter", mono: true },
          { key: "type", header: "Type", mono: true },
          { key: "required", header: "Required", mono: true },
          { key: "example", header: "Example", mono: true, wrap: true },
        ],
        rows: params.map((p) => ({
          name: p.name,
          type: p.type,
          required: p.required ? "yes" : "no",
          example: p.example == null ? "" : String(p.example),
        })),
      }
    }
    case "run_computation": {
      // The receipt: POSITIONAL rows (unlike run_sql's dict rows) + the
      // verification status a consumer should weigh alongside the numbers.
      if (content?.error) return { summary: "refused", kind: "raw", raw: content }
      const cols = Array.isArray(content?.columns) ? content.columns : []
      const rows = Array.isArray(content?.rows) ? content.rows : []
      const badge = s(content?.verification)
      if (!content?.executed) {
        return { summary: badge ? `not executed · ${badge}` : "not executed", kind: "raw", raw: content }
      }
      return {
        summary:
          plural(content?.row_count ?? rows.length, "row") +
          (content?.truncated ? "+" : "") +
          (badge ? ` · ${badge}` : ""),
        kind: "table",
        columns: cols.map((name) => ({ key: name, header: name, mono: true })),
        rows: rows.map((r) => {
          const out = {}
          cols.forEach((name, i) => {
            const v = Array.isArray(r) ? r[i] : r?.[name]
            out[name] = v == null ? "NULL" : v
          })
          return out
        }),
      }
    }
    case "web_search": {
      // { query, as_of, result_count, results:[{title,url,published_date,text}] }.
      // Rendered as the `sources` view (not a table): favicon + title + host per
      // row, each row a link. Provenance is the point of this card — the answer's
      // external claims have to be traceable to these pages, one click away.
      const results = Array.isArray(content?.results) ? content.results : []
      return {
        summary: plural(results.length, "result"),
        kind: "sources",
        items: results.map((r) => ({
          url: s(r.url),
          title: s(r.title),
          host: hostOf(r.url),
          publishedDate: s(r.published_date),
        })),
      }
    }
    case "ask_human": {
      // { status, answers:[{id,prompt,answer}], note } on resume, or
      // { status:"error", error } if the model's question set was malformed.
      // Surface the question→answer pairs so the user's selection reads inline
      // (rather than being buried in the raw JSON tool result).
      if (content && typeof content === "object" && content.status === "answered") {
        const answers = Array.isArray(content.answers) ? content.answers : []
        return {
          summary: plural(answers.length, "answer"),
          kind: "qa",
          pairs: answers.map((x) => ({
            prompt: s(x.prompt),
            answer: s(x.answer),
          })),
        }
      }
      // Malformed / noop / anything unexpected → raw so nothing is hidden.
      return { summary: "", kind: "raw", raw: content }
    }
    default:
      return { summary: "", kind: "raw", raw: content }
  }
}
