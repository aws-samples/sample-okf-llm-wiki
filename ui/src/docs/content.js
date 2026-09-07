import { DOC_PAGES } from "@/docs/navigation"

import architectureUrl from "@content/assets/architecture.png?url"
import diagramUrl from "@content/assets/diagram.png?url"

const markdownModules = import.meta.glob("@content/docs/guide/**/*.md", {
  eager: true,
  import: "default",
  query: "?raw",
})

function moduleFor(source) {
  const suffix = `/${source}`
  const entry = Object.entries(markdownModules).find(([key]) =>
    key.replaceAll("\\", "/").endsWith(suffix)
  )
  if (!entry) {
    throw new Error(`Missing documentation source: ${source}`)
  }
  return entry[1]
}

export function resolveRepositoryPath(source, href) {
  const rawPath = href.split(/[?#]/, 1)[0]
  if (!rawPath || rawPath.startsWith("/")) return rawPath.replace(/^\/+/, "")

  const parts = source.split("/")
  parts.pop()

  for (const part of rawPath.split("/")) {
    if (!part || part === ".") continue
    if (part === "..") parts.pop()
    else parts.push(part)
  }

  return parts.join("/")
}

const ASSET_BY_SOURCE = new Map([
  ["assets/architecture.png", architectureUrl],
  ["assets/diagram.png", diagramUrl],
])

export function assetUrlFor(source, href) {
  return ASSET_BY_SOURCE.get(resolveRepositoryPath(source, href)) || null
}

export const DOCS = DOC_PAGES.map((page) => ({
  ...page,
  markdown: moduleFor(page.source),
}))

export const DOC_BY_ROUTE = new Map(DOCS.map((page) => [page.route, page]))
export const ROUTE_BY_SOURCE = new Map(
  DOCS.map((page) => [page.source, page.route])
)
