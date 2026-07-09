#!/usr/bin/env bash
#
# Mint a fresh Cognito ACCESS token for the OKF consumption MCP runtime and
# print an `export OKF_TOKEN=...` line (so `eval "$(scripts/refresh-okf-token.sh)"`
# loads it into the current shell for the LangChain `.mcp.json` ${OKF_TOKEN} ref).
#
# WHY an access token, not an ID token: the consumption runtime's AgentCore JWT
# authorizer is configured with `allowedClients`, which is matched against the
# token's `client_id` claim. That claim exists ONLY on the Cognito ACCESS token
# (the ID token carries `aud`, not `client_id`), so an ID token is rejected even
# when fresh. Verified live: get-agent-runtime -> customJWTAuthorizer.allowedClients.
#
# WHY REFRESH_TOKEN_AUTH, not SRP: the web client is public (no secret) and its
# refresh token lasts 30 days, so you authenticate once and re-mint 60-min access
# tokens for a month without re-typing a password. A refresh response returns a
# new AccessToken + IdToken but NOT a new refresh token — the same refresh token
# keeps working until it expires.
#
# Config (deployment-specific; NOT hardcoded — this file is public):
#   Provide the pool/client/region via env vars or scripts/.deployment.config
#   (gitignored, same file deploy.sh writes). See scripts/.deployment.config.example:
#     OKF_USER_POOL_ID, OKF_WEB_CLIENT_ID, AWS_REGION (or OKF_TOKEN_REGION)
#   All three are in `terraform output` after a deploy.
#
# Usage:
#   1. One-time: obtain a refresh token (SRP login), then stash it:
#        export OKF_REFRESH_TOKEN='...'         # or put it in scripts/.okf-refresh-token (gitignored)
#   2. Each session (or when the 60-min access token lapses):
#        eval "$(scripts/refresh-okf-token.sh)"
#
# To bootstrap the refresh token the first time, run with --login (needs pycognito):
#        scripts/refresh-okf-token.sh --login
#   which prompts for username/password via SRP and prints the refresh token to save.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_FILE="$SCRIPT_DIR/.okf-refresh-token"

log() { echo "$@" >&2; }  # diagnostics go to stderr so stdout stays a clean `export` line

# Deployment-specific identifiers (Cognito pool/client + region) are NOT hardcoded
# here — this script is committed to a public repo. They come from the environment,
# or from the gitignored scripts/.deployment.config the deploy writes (same file
# `deploy.sh` uses). client_id is a PUBLIC SPA client id (ships in the UI bundle,
# not a secret); pool id + region are environment identifiers we keep out of git.
# shellcheck disable=SC1091
[[ -f "$SCRIPT_DIR/.deployment.config" ]] && source "$SCRIPT_DIR/.deployment.config"

REGION="${OKF_TOKEN_REGION:-${AWS_REGION:-}}"
CLIENT_ID="${OKF_WEB_CLIENT_ID:-}"
POOL_ID="${OKF_USER_POOL_ID:-}"

if [[ -z "$REGION" || -z "$CLIENT_ID" || -z "$POOL_ID" ]]; then
  log "Missing config. Set these (env vars or scripts/.deployment.config):"
  log "  AWS_REGION (or OKF_TOKEN_REGION), OKF_WEB_CLIENT_ID, OKF_USER_POOL_ID"
  log "They are printed by 'terraform output' after a deploy (user_pool_client_id,"
  log "the pool id, and the region). See scripts/.deployment.config.example."
  exit 1
fi

# --- --login: one-time SRP bootstrap to obtain a refresh token ----------------
if [[ "${1:-}" == "--login" ]]; then
  if ! python3 -c "import pycognito" 2>/dev/null; then
    log "pycognito not installed. Run: python3 -m pip install pycognito"
    exit 1
  fi
  read -r -p "Cognito username (email): " OKF_USER
  read -r -s -p "Password: " OKF_PASS; echo >&2
  REFRESH="$(OKF_USER="$OKF_USER" OKF_PASS="$OKF_PASS" python3 - <<PY
import os
from pycognito import Cognito
u = Cognito("$POOL_ID", "$CLIENT_ID", user_pool_region="$REGION",
            username=os.environ["OKF_USER"])
u.authenticate(password=os.environ["OKF_PASS"])
print(u.refresh_token)
PY
)"
  umask 077
  printf '%s\n' "$REFRESH" > "$TOKEN_FILE"
  log "Saved refresh token to $TOKEN_FILE (mode 600). It is valid ~30 days."
  log "From now on just run:  eval \"\$(scripts/refresh-okf-token.sh)\""
  exit 0
fi

# --- Normal path: exchange refresh token -> fresh access token ----------------
REFRESH="${OKF_REFRESH_TOKEN:-}"
if [[ -z "$REFRESH" && -f "$TOKEN_FILE" ]]; then
  REFRESH="$(cat "$TOKEN_FILE")"
fi
if [[ -z "$REFRESH" ]]; then
  log "No refresh token. Set OKF_REFRESH_TOKEN, or run: scripts/refresh-okf-token.sh --login"
  exit 1
fi

# Public client + no secret -> --no-sign-request (no AWS credentials needed).
RESP="$(aws cognito-idp initiate-auth \
  --region "$REGION" --no-sign-request \
  --client-id "$CLIENT_ID" \
  --auth-flow REFRESH_TOKEN_AUTH \
  --auth-parameters "REFRESH_TOKEN=${REFRESH}" \
  --query 'AuthenticationResult.AccessToken' --output text 2>&1)" || {
    log "initiate-auth failed: $RESP"
    log "If this says 'Invalid Refresh Token', the 30-day token lapsed — re-run with --login."
    exit 1
  }

# Sanity-check the minted token: must be token_use=access with time left.
OKF_TOKEN="$RESP" python3 - <<'PY' >&2
import os, json, base64, time
t = os.environ["OKF_TOKEN"]
pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
claims = json.loads(pad(t.split(".")[1]))
tu, left = claims.get("token_use"), claims["exp"] - int(time.time())
print(f"token_use={tu}  client_id={claims.get('client_id')}  exp-now={left}s", file=__import__("sys").stderr)
assert tu == "access", f"expected access token, got token_use={tu}"
assert left > 0, "token already expired"
PY

echo "export OKF_TOKEN='${RESP}'"
