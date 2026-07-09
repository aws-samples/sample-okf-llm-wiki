# Quick Start Guide: Connect an Agent (MCP)

The payoff of a knowledge bundle is having an AI agent consume it. Data Wiki exposes bundles through a **consumption MCP server** (on Bedrock AgentCore) behind a Cognito authorizer. This guide covers minting a credential and pointing an agent at the server.

---

## 1. Create an MCP Credential

Humans sign in with Cognito; **applications and agents** use a machine credential (an OAuth2 `client_credentials` pair) that they exchange for a short-lived bearer token.

1. Go to **Credentials** in the console.
2. Click **New credential** and give it a descriptive name (e.g. `analytics-agent-prod`).
3. Click **Create**, then **copy the client secret immediately**.

> **Important:** The client secret is shown **once** and can't be retrieved again. Store it in a secrets manager or env var. If you lose it, revoke the credential and make a new one.

While the dialog is open you can also download two helper artifacts (a Markdown quickstart and a self-contained Python token helper). Neither contains your secret — they read it from environment variables at runtime, so they're safe to store.

**Best practice:** one credential per consumer, so you can revoke one without disrupting the others. Revoking deletes the app client — any app using it immediately stops getting tokens.

## 2. Get a Token

Exchange the credential at the Cognito token endpoint:

```bash
export OKF_MCP_CLIENT_ID=<your client id>
export OKF_MCP_CLIENT_SECRET=<your client secret>

curl -s -X POST "<token endpoint>" \
  -u "$OKF_MCP_CLIENT_ID:$OKF_MCP_CLIENT_SECRET" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&scope=okf-mcp/invoke"
```

The response is `{"access_token": "…", "expires_in": 3600, "token_type": "Bearer"}`. Tokens last ~1 hour — cache and re-fetch when expired. Send it to the MCP server as `Authorization: Bearer <access_token>`.

## 3. Connect Your Agent

### From Claude Code (recommended)

Data Wiki ships an [`okf-mcp`](../okf-mcp/) plugin that handles token refresh for you.

1. Create a credential (step 1) and copy the client ID and secret.
2. Run the `setup` skill (or say "setup okf") to create `~/.okf/credentials`:

   ```bash
   mkdir -p ~/.okf && chmod 700 ~/.okf
   cat > ~/.okf/credentials << 'EOF'
   OKF_MCP_CLIENT_ID=<paste your client id here>
   OKF_MCP_CLIENT_SECRET=<paste your client secret here>
   EOF
   chmod 600 ~/.okf/credentials
   ```

3. Reconnect the MCP (restart Claude Code or reconnect via `/mcp`).

Token refresh is automatic — the plugin re-runs its headers helper on every connection and retries on a 401/403. To force a fresh token or debug auth, use the `refresh-okf-mcp-token` skill.

> **Important:** Never paste your client ID or secret into the chat — put them in `~/.okf/credentials` via the terminal, as shown above.

### From any other MCP client

Point your MCP client at the consumption runtime's HTTP endpoint (see the URL shape in [`okf-mcp/.mcp.json`](../okf-mcp/.mcp.json)) and send the bearer token from step 2 on every request.

## 4. Query Your Bundles

Once connected, the agent discovers and reads bundles with these tools:

| Tool               | What it does                                                       |
| ------------------ | ------------------------------------------------------------------ |
| `list_domains`     | List the `{data_domain, dataset}` pairs available to consume.      |
| `list_directory`   | Navigate a bundle's structure (progressive disclosure).            |
| `read_page`        | Read a concept's markdown and frontmatter, with pagination.        |
| `glob`             | Match concept paths with shell-style patterns (`*`, `**`, `?`).    |
| `grep`             | Search concept content with a regex.                               |
| `get_backlinks`    | Find the concepts that link to a given concept.                    |
| `semantic_search`  | Vector search over the bundle, filterable by hierarchy.            |

A typical agent starts with `list_domains`, drills in with `list_directory` / `read_page`, and uses `semantic_search` or `grep` to jump to relevant concepts. Let the agent discover the schema itself — that's the value of the bundle.

---

## Troubleshooting

- **401 / 403 / expired token** — the token lapsed or the credential is wrong. In Claude Code, run `refresh-okf-mcp-token`; otherwise re-fetch a token. If it persists, confirm the credential wasn't revoked.
- **"Missing credentials"** — `~/.okf/credentials` isn't set up; run the `setup` skill.
- **No domains returned** — there may be no `complete` harvests yet, or the credential's scope isn't accepted. Confirm a dataset has a *ready* bundle in the console.

## Next Steps

- **Build or review a bundle** → [Using the Console](./using-the-console.md)
