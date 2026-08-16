import path from "path"
import { fileURLToPath } from "url"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// React MPA: multiple HTML entry points (Vite crawls each one's module graph).
// index.html          = the admin console (login-gated dashboard)
// callback.html       = the Cognito OIDC redirect landing page
// report-harness.html = the chart-render harness the chat runtime's headless
//                       Chromium drives (report_render.py) — built SEPARATELY
//                       (BUILD_REPORT_HARNESS=1) into dist-report-harness with a
//                       relative base, since the chat image serves it from an
//                       arbitrary local dir, not the CloudFront root.
const reportHarness = process.env.BUILD_REPORT_HARNESS === "1"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  base: reportHarness ? "./" : "/",
  // Local dev server. The port is PINNED (strictPort) because the Cognito app
  // client only allows http://localhost:5173/callback.html and
  // http://localhost:5173/ as OAuth redirect/logout URLs — if Vite silently
  // fell back to 5174 the login redirect would be rejected. Fail loudly instead.
  server: {
    port: 5173,
    strictPort: true,
  },
  build: reportHarness
    ? {
        outDir: "dist-report-harness",
        rollupOptions: {
          input: {
            "report-harness": path.resolve(__dirname, "report-harness.html"),
          },
        },
      }
    : {
        rollupOptions: {
          input: {
            main: path.resolve(__dirname, "index.html"),
            callback: path.resolve(__dirname, "callback.html"),
          },
        },
      },
})
