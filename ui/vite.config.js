import path from "path"
import { fileURLToPath } from "url"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// React MPA: multiple HTML entry points (Vite crawls each one's module graph).
// index.html          = the admin console (login-gated dashboard)
// callback.html       = the Cognito OIDC redirect landing page
// harness.html        = the canvas CI's render surface — built ONLY by
//                       `npm run build:harness` (BUILD_HARNESS=1 → dist-harness,
//                       baked into the canvas runtime image), NEVER shipped to
//                       CloudFront: it has no auth and reads window-injected data.
// report-harness.html = the chart-render harness the CHAT runtime's headless
//                       Chromium drives (chat/report_render.py) — built by
//                       BUILD_REPORT_HARNESS=1 into dist-report-harness, same
//                       non-CloudFront treatment.
const harness = process.env.BUILD_HARNESS === "1"
const reportHarness = process.env.BUILD_REPORT_HARNESS === "1"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // Local dev server. The port is PINNED (strictPort) because the Cognito app
  // client only allows http://localhost:5173/callback.html and
  // http://localhost:5173/ as OAuth redirect/logout URLs — if Vite silently
  // fell back to 5174 the login redirect would be rejected. Fail loudly instead.
  server: {
    port: 5173,
    strictPort: true,
  },
  // Harness builds load from local dirs inside headless Chromium — assets
  // must be relative or they'd resolve to file:///assets/… and 404.
  base: harness || reportHarness ? "./" : "/",
  build: {
    outDir: harness ? "dist-harness" : reportHarness ? "dist-report-harness" : "dist",
    rollupOptions: {
      input: harness
        ? { harness: path.resolve(__dirname, "harness.html") }
        : reportHarness
          ? { "report-harness": path.resolve(__dirname, "report-harness.html") }
          : {
              main: path.resolve(__dirname, "index.html"),
              callback: path.resolve(__dirname, "callback.html"),
            },
    },
  },
})
