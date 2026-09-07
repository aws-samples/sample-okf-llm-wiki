import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

import { DOC_ROUTES } from "../src/docs/navigation.js"

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url))
const siteDirectory = path.resolve(scriptDirectory, "../../site")
const indexFile = path.join(siteDirectory, "index.html")
const shell = await readFile(indexFile, "utf8")

for (const route of DOC_ROUTES) {
  const routeDirectory = path.join(siteDirectory, route)
  await mkdir(routeDirectory, { recursive: true })
  await writeFile(path.join(routeDirectory, "index.html"), shell)
}

await copyFile(indexFile, path.join(siteDirectory, "404.html"))
console.log(`Generated ${DOC_ROUTES.length} documentation routes.`)
