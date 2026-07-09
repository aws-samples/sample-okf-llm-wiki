---
name: refresh-okf-mcp-token
description: Fetch or refresh an OAuth2 access token for the OKF Data wiki consumption MCP server. Use when the OKF MCP server returns 401/403/unauthorized/expired-token errors, when the user asks to refresh, renew, or get a new OKF MCP token, or when setting up authentication to call the okf-mcp / okf_consumption MCP runtime.
---

# Refresh OKF MCP Token

Fetches a short-lived (~1 hour) OAuth2 access token for the OKF Data wiki
consumption MCP server using the `client_credentials` grant.

**Normal operation:** The token is fetched and cached automatically by the
`headersHelper` script (`scripts/okf-headers-helper.sh`) on every MCP
connection. Claude Code also re-runs it on 401/403 and retries, so manual
refresh should rarely be needed.

**When to use this skill:** If you need to debug token issues, force a fresh
token from Cognito, or use the token outside of Claude Code.

## Server details (defaults)

| Field | Value |
|---|---|
| Token endpoint | `https://okf-<AWS_ACCOUNT_ID>.auth.<AWS_REGION>.amazoncognito.com/oauth2/token` |
| Scope | `okf-mcp/invoke` |
| AWS region | `<AWS_REGION>` |
| MCP runtime ARN | `arn:aws:bedrock-agentcore:<AWS_REGION>:<AWS_ACCOUNT_ID>:runtime/okf_consumption-<RUNTIME_ID>` |
| MCP endpoint | `https://bedrock-agentcore.<AWS_REGION>.amazonaws.com/runtimes/<url-encoded-ARN>/invocations?qualifier=DEFAULT` |

## Configuration

Credentials are read from (in order):
1. Environment variables (`OKF_MCP_CLIENT_ID`, `OKF_MCP_CLIENT_SECRET`)
2. The credentials file: `~/.okf/credentials` (env-style KEY=VALUE lines)
3. Interactive prompt (only when a TTY is attached)

Run the `setup` skill to create `~/.okf/credentials` for the first time.

| Variable | Required | Purpose |
|---|---|---|
| `OKF_MCP_CLIENT_ID` | — | OAuth2 client id (or read from credentials file) |
| `OKF_MCP_CLIENT_SECRET` | — | OAuth2 client secret (or read from credentials file) |
| `OKF_MCP_CREDENTIALS_FILE` | — | Override path to credentials file (default `~/.okf/credentials`) |
| `OKF_MCP_TOKEN_CACHE` | — | Override path to cached token (default `~/.okf/token-cache.json`) |
| `OKF_MCP_URL` | — | Override the MCP endpoint URL |
| `OKF_MCP_TOKEN_ENDPOINT` | — | Override the Cognito token endpoint |
| `OKF_MCP_SCOPE` | — | Override the OAuth2 scope (default `okf-mcp/invoke`) |

## Steps

1. Check credentials are available (env, `~/.okf/credentials`, or prompt). If
   not and the run is non-interactive, ask the user to run the `setup` skill
   first.

2. Run the bundled token helper (lives in the plugin's `scripts/` folder):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/okf-mcp-token.py"
   ```

   Output modes (first positional argument):
   - `token` (default) — prints the raw access token
   - `export` — prints `export OKF_TOKEN=<token>` (for `eval`)
   - `headers` — prints `{"Authorization": "Bearer <token>"}` (what `headersHelper` uses)

   To force a fresh token (ignore cache):

   ```bash
   rm -f ~/.okf/token-cache.json
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/okf-mcp-token.py"
   ```

3. `curl` fallback (no script):

   ```bash
   curl -s -X POST "${OKF_MCP_TOKEN_ENDPOINT:-https://okf-<AWS_ACCOUNT_ID>.auth.<AWS_REGION>.amazoncognito.com/oauth2/token}" \
     -u "$OKF_MCP_CLIENT_ID:$OKF_MCP_CLIENT_SECRET" \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "grant_type=client_credentials&scope=${OKF_MCP_SCOPE:-okf-mcp/invoke}"
   ```

   The response is JSON:
   `{"access_token": "…", "expires_in": 3600, "token_type": "Bearer"}`.

## How it all fits together

```
.mcp.json (headersHelper)
   └── scripts/okf-headers-helper.sh
         └── scripts/okf-mcp-token.py headers
               ├── reads ~/.okf/credentials (client id/secret)
               ├── fetches token from Cognito (or returns cached)
               ├── caches to ~/.okf/token-cache.json
               └── prints {"Authorization": "Bearer <token>"}
```

Claude Code calls the `headersHelper` on every connection and on 401/403, so
token refresh is fully automatic. No `OKF_TOKEN` env var is needed anymore.

## Notes

- **Do not print or log the full token** in shared output unless asked; truncate
  it (e.g. first 24 chars) as the helper's stderr hint does.
- Tokens are short-lived (~1 hour). On a 401/403 from the MCP server, Claude
  Code automatically re-runs the helper and retries the call once.
- `scripts/okf-mcp-token.py` uses only the Python standard library — no dependencies.
- The token cache and credentials are stored in `~/.okf/` (outside the repo).
