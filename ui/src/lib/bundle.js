// Bundle helpers: frontmatter parsing, intra-bundle link resolution, and the
// concept -> directory tree. These are JS ports of the Python primitives so the
// viewer agrees with the harvester on what a concept id is and how links
// resolve:
//   - frontmatter block           -> okf_core/document.py (OKFDocument.parse)
//   - relative .md link resolution -> okf_core/links.py   (extract_links)
// Keeping them in one dependency-free module lets us unit-test the logic with a
// plain node harness.

const FRONTMATTER_DELIM = "---"

// Split a concept doc into { frontmatter, body }, mirroring OKFDocument.parse:
// a leading `---` line opens a YAML block that runs to the next `---`; the rest
// is the body. No frontmatter => the whole text is the body. We keep the raw
// frontmatter text too so callers can show it verbatim if they ever want to.
export function parseDocument(text) {
  const src = text ?? ""
  const lines = src.split("\n")
  if (lines.length === 0 || lines[0].trim() !== FRONTMATTER_DELIM) {
    return { frontmatter: {}, frontmatterText: "", body: src }
  }
  let end = -1
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === FRONTMATTER_DELIM) {
      end = i
      break
    }
  }
  if (end === -1) {
    // Unterminated block — treat the whole file as body rather than throwing;
    // the viewer should degrade gracefully on a malformed doc.
    return { frontmatter: {}, frontmatterText: "", body: src }
  }
  const fmText = lines.slice(1, end).join("\n")
  let body = lines.slice(end + 1).join("\n")
  if (body.startsWith("\n")) body = body.slice(1)
  return { frontmatter: parseFrontmatter(fmText), frontmatterText: fmText, body }
}

// Lightweight YAML-ish parser for the handful of scalar/list keys OKF concept
// frontmatter uses (type/title/description/tags/timestamp/resource). This is
// intentionally NOT a full YAML parser — it handles `key: value`, inline
// `[a, b]` flow lists, `-`-prefixed block lists, and multi-line scalars (both
// wrapped plain scalars and `>`/`|` block scalars), which is all the reference
// producer emits. Anything it can't parse is simply omitted from the header.
function parseFrontmatter(fmText) {
  const out = {}
  const lines = fmText.split("\n")
  // A line that starts a NEW top-level key (`key:`) or a block-list item ends
  // the previous key's value; anything else that's indented is a continuation.
  const isNewKey = (ln) => /^[A-Za-z0-9_-]+:\s*(.*)$/.test(ln)
  const isBlockItem = (ln) => /^\s*-\s+/.test(ln)
  const isContinuation = (ln) =>
    ln.trim() !== "" && /^\s/.test(ln) && !isNewKey(ln) && !isBlockItem(ln)

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (!line.trim() || line.trimStart().startsWith("#")) continue
    const m = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line)
    if (!m) continue
    const key = m[1]
    let rest = m[2].trim()

    // Block scalar (`>` folded / `|` literal): the value is the indented lines
    // that follow. Fold them into a single string (good enough for display).
    if (rest === ">" || rest === "|" || rest === ">-" || rest === "|-") {
      const parts = []
      let j = i + 1
      while (j < lines.length && isContinuation(lines[j])) {
        parts.push(lines[j].trim())
        j++
      }
      out[key] = parts.join(" ").trim()
      i = j - 1
      continue
    }

    if (rest === "") {
      // Possible block list: consume following `- item` lines.
      const items = []
      let j = i + 1
      while (j < lines.length && isBlockItem(lines[j])) {
        items.push(stripQuotes(lines[j].replace(/^\s*-\s+/, "").trim()))
        j++
      }
      if (items.length) {
        out[key] = items
        i = j - 1
      } else {
        out[key] = ""
      }
      continue
    }
    if (rest.startsWith("[") && rest.endsWith("]")) {
      out[key] = rest
        .slice(1, -1)
        .split(",")
        .map((s) => stripQuotes(s.trim()))
        .filter(Boolean)
      continue
    }

    // Plain scalar, possibly wrapped across indented continuation lines. YAML
    // folds continuations into the value with a space — do the same so a long
    // `description:` that wraps isn't cut off at the first line.
    const parts = [rest]
    let j = i + 1
    while (j < lines.length && isContinuation(lines[j])) {
      parts.push(lines[j].trim())
      j++
    }
    i = j - 1
    out[key] = stripQuotes(parts.join(" ").trim())
  }
  return out
}

function stripQuotes(s) {
  if (s.length >= 2) {
    const a = s[0]
    const b = s[s.length - 1]
    if ((a === '"' && b === '"') || (a === "'" && b === "'")) {
      return s.slice(1, -1)
    }
  }
  return s
}

// Resolve one markdown link target to a full concept id, mirroring the Python
// resolver (okf_core/links.py): links are relative `.md` paths resolved against
// the *directory* of the linking doc's concept id. External links (`://`) and
// absolute paths (`/foo`) return null; so do targets that escape the bundle
// root via `..`. A trailing `#anchor` is captured separately.
//
//   resolveConceptLink("customers.md", "tables/orders")
//     -> { conceptId: "tables/customers", anchor: "" }
//   resolveConceptLink("../references/joins/a__b.md", "tables/orders")
//     -> { conceptId: "references/joins/a__b", anchor: "" }
export function resolveConceptLink(target, fromConceptId) {
  if (!target) return null
  if (target.includes("://") || target.startsWith("/")) return null
  if (target.startsWith("#")) return null // pure in-page anchor

  let path = target
  let anchor = ""
  const hash = path.indexOf("#")
  if (hash !== -1) {
    anchor = path.slice(hash + 1)
    path = path.slice(0, hash)
  }
  // Case-sensitive `.md` to match the Python resolver's regex (links.py),
  // which only matches lowercase — so the viewer and harvester agree on which
  // links are intra-bundle. An uppercase `.MD` is treated as a non-concept link.
  if (!path.endsWith(".md")) return null

  const baseDir = dirOf(fromConceptId)
  const resolved = posixResolve(baseDir, path)
  if (resolved == null) return null // escaped the bundle root
  const conceptId = resolved.replace(/\.md$/, "")
  if (!conceptId) return null
  return { conceptId, anchor }
}

// Directory part of a concept id: "tables/races" -> "tables", "index" -> "".
function dirOf(conceptId) {
  const idx = (conceptId || "").lastIndexOf("/")
  return idx === -1 ? "" : conceptId.slice(0, idx)
}

// Join a base dir with a relative target and normalize `.`/`..` segments. A
// `..` that would rise above the root returns null (Python drops such links as
// out-of-bundle). Pure POSIX-style join — no filesystem.
function posixResolve(baseDir, target) {
  const stack = baseDir ? baseDir.split("/").filter(Boolean) : []
  for (const seg of target.split("/")) {
    if (seg === "" || seg === ".") continue
    if (seg === "..") {
      if (stack.length === 0) return null
      stack.pop()
    } else {
      stack.push(seg)
    }
  }
  return stack.join("/")
}

// Build a nested directory tree from the flat concept list the bundle API
// returns ([{ concept_id, key }]). Directories sort before files, both
// alphabetical. Each node is { name, path, type, children?, key?, conceptId? }
// where `path` is the concept-id prefix for a dir and the full concept id for a
// file.
export function buildTree(files) {
  const root = { name: "", path: "", type: "dir", children: [], _map: new Map() }

  for (const file of files || []) {
    const id = file.concept_id
    if (!id) continue
    const parts = id.split("/")
    let node = root
    let prefix = ""
    for (let i = 0; i < parts.length; i++) {
      const seg = parts[i]
      prefix = prefix ? `${prefix}/${seg}` : seg
      const isLeaf = i === parts.length - 1
      if (isLeaf) {
        node.children.push({
          name: seg,
          path: id,
          type: "file",
          conceptId: id,
          key: file.key,
        })
      } else {
        let child = node._map.get(seg)
        if (!child) {
          child = {
            name: seg,
            path: prefix,
            type: "dir",
            children: [],
            _map: new Map(),
          }
          node._map.set(seg, child)
          node.children.push(child)
        }
        node = child
      }
    }
  }

  return sortTree(root).children
}

function sortTree(node) {
  if (node.type !== "dir") return node
  node.children.sort((a, b) => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1
    return a.name.localeCompare(b.name)
  })
  for (const child of node.children) sortTree(child)
  delete node._map
  return node
}
