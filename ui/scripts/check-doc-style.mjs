import { readdir, readFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url))
const docsDirectory = path.resolve(scriptDirectory, "../../docs/guide")
const maximumSentenceWords = 28
const errors = []

async function markdownFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const files = []

  for (const entry of entries) {
    const target = path.join(directory, entry.name)
    if (entry.isDirectory()) {
      files.push(...(await markdownFiles(target)))
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      files.push(target)
    }
  }

  return files
}

function wordCount(value) {
  return value.match(/[A-Za-z0-9][A-Za-z0-9'./:+_-]*/g)?.length || 0
}

function cleanMarkdown(value) {
  return value
    .replace(/!\[([^\]]*)]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)]\([^)]*\)/g, "$1")
    .replace(/`[^`]*`/g, "code")
    .replace(/<[^>]+>/g, " ")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^>\s?/gm, "")
    .replace(/^!!!\s+\w+(?:\s+"[^"]+")?\s*$/gm, "")
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, "")
    .replace(/\*\*|__|\*|_/g, "")
    .replace(/\s+/g, " ")
    .trim()
}

function proseBlocks(markdown) {
  const blocks = []
  let current = []
  let startLine = 1
  let inFence = false

  const flush = () => {
    if (current.length) {
      blocks.push({ line: startLine, text: current.join(" ") })
      current = []
    }
  }

  markdown.split("\n").forEach((line, index) => {
    const lineNumber = index + 1
    if (/^\s*```/.test(line)) {
      flush()
      inFence = !inFence
      return
    }
    if (inFence || /^\s*\|/.test(line) || /^\s*<!--/.test(line)) return
    if (!line.trim()) {
      flush()
      return
    }
    if (/^\s*(?:[-*+]|\d+\.)\s+/.test(line)) {
      flush()
      startLine = lineNumber
      current.push(line)
      flush()
      return
    }
    if (!current.length) startLine = lineNumber
    current.push(line)
  })

  flush()
  return blocks
}

for (const file of await markdownFiles(docsDirectory)) {
  const markdown = await readFile(file, "utf8")
  const relative = path.relative(path.resolve(docsDirectory, "../.."), file)

  markdown.split("\n").forEach((line, index) => {
    if (/[\u2013\u2014]/.test(line)) {
      errors.push(`${relative}:${index + 1}: replace the en dash or em dash`)
    }
  })

  for (const block of proseBlocks(markdown)) {
    const prose = cleanMarkdown(block.text)
    if (!prose) continue
    const sentences = prose.split(/(?<=[.!?])\s+(?=[A-Z0-9])/)
    for (const sentence of sentences) {
      const count = wordCount(sentence)
      if (count > maximumSentenceWords) {
        errors.push(
          `${relative}:${block.line}: sentence has ${count} words; maximum is ${maximumSentenceWords}`
        )
      }
    }
  }
}

if (errors.length) {
  console.error("Documentation style check failed:")
  for (const error of errors) console.error(`- ${error}`)
  process.exitCode = 1
} else {
  console.log(
    `Documentation style check passed. Sentences use ${maximumSentenceWords} words or fewer.`
  )
}
