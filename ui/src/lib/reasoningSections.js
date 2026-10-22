// Split a reasoning-summary text into titled sections.
//
// GPT reasoning summaries arrive as PARTS, each opening with its own bold
// title line ("**Designing the dashboard**\n\nI'm narrowing…"). Rendered as one
// markdown blob they read as a wall of text with stray bold fragments; split,
// each part becomes its own timeline step with the title as the step label.
// Claude's reasoning carries no such title lines and comes back as ONE
// untitled section — rendering for it is unchanged.
//
// The chat server keeps part boundaries as a blank line; text from before
// that fix (or a dropped part-added frame) can glue a title onto the previous
// sentence ("…selectors.**Refining finish metrics**\n\n"), so a title glued to
// the end of a line is detached first.

// A title line: the WHOLE line is one bold run (short, no nested asterisks).
const TITLE_LINE = /^\*\*([^*\n]{1,120})\*\*[ \t]*$/

// "…sentence.**Title**" followed by a line break / end of text.
const GLUED_TITLE = /([^\s*])(\*\*[^*\n]{1,120}\*\*)(?=[ \t]*(?:\n|$))/g

export function splitReasoningSections(text) {
  const src = String(text || "").replace(GLUED_TITLE, "$1\n\n$2")
  const sections = []
  let current = { title: null, lines: [] }
  let prevBlank = true // a title must open a paragraph
  for (const line of src.split("\n")) {
    const m = prevBlank ? TITLE_LINE.exec(line.trim()) : null
    if (m) {
      if (current.title !== null || current.lines.some((l) => l.trim())) {
        sections.push(current)
      }
      current = { title: m[1].trim(), lines: [] }
    } else {
      current.lines.push(line)
    }
    prevBlank = line.trim() === ""
  }
  sections.push(current)
  return sections
    .map((s) => ({ title: s.title, body: s.lines.join("\n").trim() }))
    .filter((s) => s.title !== null || s.body)
}
