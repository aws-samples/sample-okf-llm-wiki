# OKF Console (UI)

React 19 + Vite + shadcn/ui SPA for managing OKF domains, harvests, and bundles.
It authenticates against the deployed Cognito user pool and calls the Control API.

## Local development

You can run the UI locally against the **already-deployed** Cognito + Control API —
no redeploy needed to see changes. The dev server talks to the real backend, so
you sign in with the same Cognito user as production.

**One-time (per environment): generate `.env.local` from the deployed stack.**

```bash
# needs AWS creds for the deploy account + terraform state access
npm run dev:env        # -> writes ui/.env.local from the compute stack's ui_env output
```

`.env.local` is git-ignored. It holds the Cognito authority / client id / domain
and the Control API base URL — the same values baked into the production build.

**Then, every time:**

```bash
npm install            # first run only
npm run dev            # http://localhost:5173
```

Open http://localhost:5173 and sign in. Vite hot-reloads on save.

### Why the port is pinned to 5173

`vite.config.js` sets `server.strictPort: true` on port 5173. The Cognito app
client only whitelists `http://localhost:5173/callback.html` (redirect) and
`http://localhost:5173/` (logout) as OAuth URLs. If Vite fell back to another
port the login redirect would be rejected, so we fail loudly instead.

If 5173 is busy, free it (`lsof -ti:5173 | xargs kill`) rather than changing the
port — or add the new port to the Cognito app client's callback/logout URLs
(`infra/durable` `ui_callback_urls` / `ui_logout_urls`, then re-run
`./scripts/deploy.sh cognito-urls`).

### How localhost is supported

- **Redirect URI** — `src/lib/auth.js` derives it from `window.location.origin`,
  so it is `http://localhost:5173/callback.html` in dev automatically.
- **Cognito** — the app client whitelists the localhost callback/logout URLs
  (see `stage_cognito_urls` in `scripts/deploy.sh`).
- **CORS** — the Control API's API Gateway allows all origins (`allow_origins:
  ["*"]`), so `fetch` from `localhost` works.

## Deploying

The production build + upload is driven from the repo root:

```bash
./scripts/deploy.sh ui     # build, sync to S3 (with cache headers), invalidate CloudFront
```

## Adding shadcn components

```bash
npx shadcn@latest add <component>
```

Components land in `src/components/ui/`. Import via the `@/` alias
(`import { Button } from "@/components/ui/button"`).
