// CodeView — a lightweight, READ-ONLY highlighted code block for agent answers.
//
// Deliberately NOT CodeMirror (what Sparky uses): CodeMirror is an editor and
// pulls in @codemirror/view + state + a per-language pkg each. We only need
// read-only display, and this repo already ships highlight.js (via
// rehype-highlight) + the .okf-prose hljs token theme. So this renders ONE block
// with highlight.js directly — same colors as concept docs, zero new deps.
//
// Chrome: a header with the language label + a copy button, then the scrollable
// highlighted <pre>. Used by the chat Markdown renderer for fenced code blocks
// and by the SQL "View SQL" disclosure.

import hljs from "highlight.js/lib/common"
import { memo, useMemo } from "react"

import { CopyButton } from "@/components/ui/copy-button"
import { cn } from "@/lib/utils"

import "@/components/chat/CodeView.css"

// A friendly display name for the header. highlight.js language ids are lowercase
// (`sql`, `js`); map the common ones to a nicer label, else Title-case the id.
const LABELS = {
  sql: "SQL",
  js: "JavaScript",
  javascript: "JavaScript",
  ts: "TypeScript",
  typescript: "TypeScript",
  py: "Python",
  python: "Python",
  json: "JSON",
  yaml: "YAML",
  yml: "YAML",
  bash: "Bash",
  sh: "Shell",
  shell: "Shell",
  hcl: "HCL",
  tf: "Terraform",
  html: "HTML",
  css: "CSS",
  md: "Markdown",
  markdown: "Markdown",
  text: "Text",
  plaintext: "Text",
}

function prettyLang(lang) {
  if (!lang) return ""
  const key = lang.toLowerCase()
  if (LABELS[key]) return LABELS[key]
  return key.charAt(0).toUpperCase() + key.slice(1)
}

export const CodeView = memo(function CodeView({ code, language, className }) {
  const source = typeof code === "string" ? code.replace(/\n$/, "") : ""

  // Highlight once per (code, language). Fall back to auto-detect when the fence
  // has no/unknown language, and to escaped plain text if hljs throws.
  const html = useMemo(() => {
    if (!source) return ""
    try {
      if (language && hljs.getLanguage(language)) {
        return hljs.highlight(source, { language }).value
      }
      return hljs.highlightAuto(source).value
    } catch {
      const div = document.createElement("div")
      div.textContent = source
      return div.innerHTML
    }
  }, [source, language])

  if (!source) return null
  const label = prettyLang(language) || "Code"

  return (
    <div className={cn("okf-codeview", className)}>
      <div className="okf-codeview-header">
        <span className="okf-codeview-lang">{label}</span>
        <CopyButton text={source} className="okf-codeview-copy" />
      </div>
      <div className="okf-codeview-scroll okf-thin-scroll">
        {/* hljs-* classes are colored by the .okf-prose hljs rules (index.css). */}
        <pre className="okf-codeview-pre">
          <code
            className="hljs okf-prose"
            dangerouslySetInnerHTML={{ __html: html }}
          />
        </pre>
      </div>
    </div>
  )
})
