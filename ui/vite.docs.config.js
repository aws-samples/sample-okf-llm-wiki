import path from "node:path"
import process from "node:process"
import { fileURLToPath } from "node:url"

import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const uiDirectory = path.dirname(fileURLToPath(import.meta.url))
const repositoryRoot = path.resolve(uiDirectory, "..")
const githubRepository = process.env.GITHUB_REPOSITORY?.split("/")[1]
const base =
  process.env.DOCS_BASE ||
  (process.env.GITHUB_ACTIONS && githubRepository
    ? `/${githubRepository}/`
    : "/")

export default defineConfig({
  root: path.resolve(uiDirectory, "docs"),
  base,
  publicDir: path.resolve(uiDirectory, "public"),
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(uiDirectory, "src"),
      "@content": repositoryRoot,
    },
  },
  build: {
    outDir: path.resolve(repositoryRoot, "site"),
    emptyOutDir: true,
    assetsDir: "static",
  },
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
    fs: {
      allow: [repositoryRoot],
    },
  },
})
