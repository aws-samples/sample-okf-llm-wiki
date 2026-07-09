---
name: setup
description: Set up the OKF MCP plugin — outputs a guide for the user to create their ~/.okf/credentials file with their OAuth2 client id/secret. Use when first enabling the plugin, when headersHelper fails with "Missing credentials", or when the user says "setup okf".
---

# OKF MCP Setup

Outputs a guide for the user to configure the plugin's credentials. The
client id and secret are long-lived machine credentials that must NEVER be
pasted into the conversation.

## When to use

- First time enabling the okf-mcp plugin
- When the user says "setup okf", "configure okf credentials", or similar
- When the headersHelper fails with "Missing credentials"

## Steps

1. Check if `~/.okf/credentials` already exists:

   ```bash
   test -f ~/.okf/credentials && echo "exists ($(wc -l < ~/.okf/credentials) lines)" || echo "not found"
   ```

2. If it already exists and the user didn't ask to reconfigure, inform them
   the file is present and suggest running the `refresh-okf-mcp-token` skill
   to test it.

3. Output the following guide as markdown to the user (do NOT ask them to
   paste secrets into the chat):

---

## OKF MCP Credentials Setup

Create `~/.okf/credentials` with your OAuth2 client ID and secret. Run these
commands in your terminal (outside of Claude Code) or prefix with `!` to run
inline:

```bash
mkdir -p ~/.okf && chmod 700 ~/.okf
cat > ~/.okf/credentials << 'EOF'
OKF_MCP_CLIENT_ID=<paste your client id here>
OKF_MCP_CLIENT_SECRET=<paste your client secret here>
EOF
chmod 600 ~/.okf/credentials
```

Replace the `<paste ...>` placeholders with the real values from when you
created the credential in the OKF console.

**Where to find your credentials:** They were shown once when you created the
machine credential in the OKF console. If you've lost them, create
a new credential pair in the console.

**Verify it works:**

```bash
python3 "<plugin_root>/scripts/okf-mcp-token.py" headers
```

This should print a JSON object with an `Authorization` header. After that,
restart Claude Code (or reconnect the MCP via `/mcp`) for the plugin to pick
up the credentials automatically.

---

4. After outputting the guide, mention that `<plugin_root>` should be replaced
   with the actual CLAUDE_PLUGIN_ROOT path if running manually, or they can
   just restart the session and the `headersHelper` will take care of it.

## Important

- NEVER ask the user to paste their client ID or secret into the conversation
- NEVER write secrets directly — only output the shell commands they can run
- The credentials file lives at `~/.okf/credentials` (outside the repo)
- The token cache lives at `~/.okf/token-cache.json` (auto-managed)
