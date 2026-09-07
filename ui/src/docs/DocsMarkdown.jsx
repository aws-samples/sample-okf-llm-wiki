/* eslint-disable react-refresh/only-export-components */
import { Children, isValidElement } from "react"
import {
  InfoIcon,
  LightbulbIcon,
  LinkIcon,
  Maximize2Icon,
  TriangleAlertIcon,
} from "lucide-react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { CodeView } from "@/components/chat/CodeView"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  assetUrlFor,
  resolveRepositoryPath,
  ROUTE_BY_SOURCE,
} from "@/docs/content"
import { cn } from "@/lib/utils"

const ADMONITIONS = {
  important: {
    Icon: TriangleAlertIcon,
    label: "Important",
    className: "border-primary/45 bg-primary/5",
    iconClassName: "text-primary",
  },
  note: {
    Icon: InfoIcon,
    label: "Note",
    className: "border-edge bg-muted/35",
    iconClassName: "text-muted-foreground",
  },
  tip: {
    Icon: LightbulbIcon,
    label: "Tip",
    className: "border-primary/35 bg-primary/5",
    iconClassName: "text-primary",
  },
  warning: {
    Icon: TriangleAlertIcon,
    label: "Warning",
    className: "border-destructive/40 bg-destructive/5",
    iconClassName: "text-destructive",
  },
}

function normalizeAdmonitions(markdown) {
  const lines = markdown.split("\n")
  const output = []

  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index].match(/^!!!\s+([a-z-]+)(?:\s+"([^"]+)")?\s*$/)
    if (!match) {
      output.push(lines[index])
      continue
    }

    const type = match[1].toLowerCase()
    const title = match[2]?.trim()
    const body = []
    let cursor = index + 1

    while (cursor < lines.length) {
      const line = lines[cursor]
      if (line.startsWith("    ")) {
        body.push(line.slice(4))
        cursor += 1
        continue
      }
      if (
        line.trim() === "" &&
        cursor + 1 < lines.length &&
        lines[cursor + 1].startsWith("    ")
      ) {
        body.push("")
        cursor += 1
        continue
      }
      break
    }

    output.push(`> [!${type.toUpperCase()}]${title ? ` ${title}` : ""}`)
    output.push(">")
    for (const line of body) {
      output.push(line ? `> ${line}` : ">")
    }
    index = cursor - 1
  }

  return output.join("\n")
}

function normalizeDefinitionLists(markdown) {
  const lines = markdown.split("\n")
  const output = []

  for (let index = 0; index < lines.length; index += 1) {
    const definition = lines[index + 1]?.match(/^:\s+(.+)$/)
    if (!definition || lines[index].trim() === "") {
      output.push(lines[index])
      continue
    }

    const term = lines[index].trim().replace(/^\*\*(.+)\*\*$/, "$1")
    output.push(`- **${term}:** ${definition[1]}`)
    index += 1
  }

  return output.join("\n")
}

export function normalizeDocsMarkdown(markdown) {
  return normalizeDefinitionLists(normalizeAdmonitions(markdown))
}

function plainText(value) {
  if (value == null) return ""
  if (typeof value === "string" || typeof value === "number") {
    return String(value)
  }
  if (Array.isArray(value)) return value.map(plainText).join("")
  if (isValidElement(value)) return plainText(value.props.children)
  return ""
}

export function headingSlug(value) {
  return plainText(value)
    .toLowerCase()
    .replace(/[`*_]/g, "")
    .replace(/[^a-z0-9\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
}

function nodeText(node) {
  if (!node) return ""
  if (node.type === "text") return node.value || ""
  return (node.children || []).map(nodeText).join("")
}

function Admonition({ node, children }) {
  const marker = nodeText(node)
    .trimStart()
    .match(/^\[!(IMPORTANT|NOTE|TIP|WARNING)\](?:\s+([^\n]+))?/i)
  if (!marker) {
    return <blockquote>{children}</blockquote>
  }

  const type = marker[1].toLowerCase()
  const config = ADMONITIONS[type] || ADMONITIONS.note
  const childNodes = Children.toArray(children)
  const markerIndex = childNodes.findIndex((child) =>
    /^\[!(IMPORTANT|NOTE|TIP|WARNING)\]/i.test(plainText(child).trim())
  )
  const body = childNodes.filter((_, index) => index !== markerIndex)
  const { Icon } = config

  return (
    <Alert className={cn("docs-alert", config.className)}>
      <Icon className={cn("mt-0.5", config.iconClassName)} />
      <AlertTitle>{config.label}</AlertTitle>
      <AlertDescription>{body}</AlertDescription>
    </Alert>
  )
}

function resolveSource(source, href) {
  const [rawPath, rawHash = ""] = href.split("#", 2)
  const targetSource = resolveRepositoryPath(source, rawPath)
  const route = ROUTE_BY_SOURCE.get(targetSource)
  if (route === undefined) return null
  return { route, hash: rawHash ? `#${rawHash}` : "" }
}

function codeText(children) {
  return plainText(children).replace(/\n$/, "")
}

function DocumentationImage({ source, src, alt }) {
  if (/^(?:[a-z]+:|\/\/)/i.test(src)) {
    return <img src={src} alt={alt} loading="lazy" />
  }

  const resolved = assetUrlFor(source, src)
  if (!resolved) return <img src={src} alt={alt} loading="lazy" />
  const label = alt || "Data Wiki documentation image"

  return (
    <figure className="docs-figure">
      <Dialog>
        <div className="docs-image-shell">
          <img src={resolved} alt={label} loading="lazy" />
          <DialogTrigger asChild>
            <Button
              type="button"
              variant="secondary"
              size="icon-sm"
              className="docs-image-expand"
              aria-label="View full-size image"
              title="View full-size image"
            >
              <Maximize2Icon data-icon="inline-start" />
            </Button>
          </DialogTrigger>
        </div>
        {alt ? <figcaption>{alt}</figcaption> : null}
        <DialogContent className="docs-image-dialog">
          <DialogHeader className="pr-8">
            <DialogTitle>{label}</DialogTitle>
            <DialogDescription>
              View the image at its full documentation size.
            </DialogDescription>
          </DialogHeader>
          <div className="docs-image-dialog-body">
            <img src={resolved} alt={label} />
          </div>
        </DialogContent>
      </Dialog>
    </figure>
  )
}

function componentsFor({ source, makeHref, navigate }) {
  const Heading = (Tag) =>
    function DocsHeading({ children }) {
      const id = headingSlug(children)
      return (
        <Tag id={id} className="docs-heading">
          <a
            href={`#${id}`}
            className="docs-heading-link"
            onClick={(event) => navigate(event, null, `#${id}`)}
          >
            {children}
            <LinkIcon aria-hidden="true" className="docs-heading-link-icon" />
          </a>
        </Tag>
      )
    }

  return {
    h1: Heading("h1"),
    h2: Heading("h2"),
    h3: Heading("h3"),
    h4: Heading("h4"),
    blockquote: Admonition,
    pre({ children }) {
      const child = Children.only(children)
      const language =
        child.props.className?.match(/language-([\w-]+)/)?.[1] || "text"
      return (
        <CodeView
          code={codeText(child.props.children)}
          language={language}
          className="docs-codeview okf-codeview-full"
        />
      )
    },
    code({ className, children }) {
      if (className?.startsWith("language-")) return <code>{children}</code>
      return <code className={className}>{children}</code>
    },
    img({ src = "", alt = "" }) {
      return <DocumentationImage source={source} src={src} alt={alt} />
    },
    a({ href = "", children, ...props }) {
      if (/^(?:[a-z]+:|\/\/)/i.test(href)) {
        return (
          <a href={href} target="_blank" rel="noreferrer noopener" {...props}>
            {children}
          </a>
        )
      }

      if (href.startsWith("#")) {
        return (
          <a
            href={href}
            onClick={(event) => navigate(event, null, href)}
            {...props}
          >
            {children}
          </a>
        )
      }

      const resolved = resolveSource(source, href)
      if (!resolved) return <a href={href}>{children}</a>
      return (
        <a
          href={makeHref(resolved.route, resolved.hash)}
          onClick={(event) => navigate(event, resolved.route, resolved.hash)}
          {...props}
        >
          {children}
        </a>
      )
    },
    table({ children }) {
      return (
        <div className="docs-table">
          <table>{children}</table>
        </div>
      )
    },
  }
}

export function DocsMarkdown({ markdown, source, makeHref, navigate }) {
  return (
    <div className="docs-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={componentsFor({ source, makeHref, navigate })}
      >
        {normalizeDocsMarkdown(markdown)}
      </ReactMarkdown>
    </div>
  )
}
