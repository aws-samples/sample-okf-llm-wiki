import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import { ThemeProvider } from "@/components/theme-provider"
import { DocsApp } from "@/docs/DocsApp"

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ThemeProvider storageKey="data-wiki-docs-theme">
      <DocsApp />
    </ThemeProvider>
  </StrictMode>
)
