import path from "path"
import { fileURLToPath } from "url"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// React MPA: multiple HTML entry points (Vite crawls each one's module graph).
// index.html    = the admin console (login-gated dashboard)
// callback.html = the Cognito OIDC redirect landing page
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // Local dev server. The port is PINNED (strictPort) because the Cognito app
  // client only whitelists http://localhost:5173/callback.html and
  // http://localhost:5173/ as OAuth redirect/logout URLs — if Vite silently
  // fell back to 5174 the login redirect would be rejected. Fail loudly instead.
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, "index.html"),
        callback: path.resolve(__dirname, "callback.html"),
      },
    },
  },
})
