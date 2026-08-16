#!/usr/bin/env bash
# headersHelper for the `okf` MCP server (see ../.mcp.json).
#
# Claude Code runs this on every MCP connection (and re-runs it on a 401/403,
# retrying the call once). It must print the auth headers as JSON to stdout:
#
#     {"Authorization": "Bearer <token>"}
#
# The heavy lifting — fetching, and on-disk caching, of the short-lived OAuth2
# token — lives in the token helper, which reuses a cached token until it is
# near expiry so this stays well under Claude Code's 10s helper timeout.
set -euo pipefail

# CLAUDE_PLUGIN_ROOT is set when invoked as a plugin; fall back to this script's
# own location so the helper also works from a plain checkout.
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
  root="$CLAUDE_PLUGIN_ROOT"
else
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

exec python3 "$root/scripts/okf-mcp-token.py" headers
