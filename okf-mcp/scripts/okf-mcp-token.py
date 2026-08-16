#!/usr/bin/env python3
"""Fetch an OAuth2 access token for the OKF Data wiki MCP server (client_credentials machine credential).

Standard library only — no dependencies. Uses the client_credentials grant and
caches the token on disk until it expires, so repeated invocations (e.g. the
`headersHelper` in .mcp.json, which runs on every MCP connection) reuse a valid
token instead of hitting Cognito each time.

Configuration is env-var driven so the script stays generic:

    OKF_MCP_CLIENT_ID       OAuth2 client id
    OKF_MCP_CLIENT_SECRET   OAuth2 client secret
    OKF_MCP_CREDENTIALS_FILE (optional) path to a gitignored file holding the
                            client id/secret; defaults to ~/.okf/credentials
    OKF_MCP_TOKEN_ENDPOINT  (optional) Cognito token endpoint; defaults to the
                            OKF pool below
    OKF_MCP_SCOPE           (optional) OAuth2 scope; defaults to okf-mcp/invoke
    OKF_MCP_TOKEN_CACHE     (optional) path to the on-disk token cache; defaults
                            to ~/.okf/token-cache.json

Credentials are NEVER stored in this file. Provide them via environment
variables, or via the credentials file (env-style `KEY=VALUE` lines):

    # ~/.okf/credentials  (chmod 600, gitignored)
    OKF_MCP_CLIENT_ID=<your client id>
    OKF_MCP_CLIENT_SECRET=<your client secret>

Resolution order for each credential: environment variable, then the
credentials file, then (if a TTY) an interactive prompt.

Print modes (positional arg):
    (none) / token   print just the access token to stdout
    export           print `export OKF_TOKEN=<token>` for `eval`/`source`
                     (OKF_TOKEN is the bearer var the `okf` MCP server reads)
    headers          print `{"Authorization": "Bearer <token>"}` — the JSON
                     shape Claude Code's `headersHelper` expects on stdout
"""

import base64
import getpass
import json
import os
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Defaults for the OKF deployment; override via env vars to reuse elsewhere.
TOKEN_ENDPOINT = os.environ.get(
    "OKF_MCP_TOKEN_ENDPOINT",
    "https://okf-<AWS_ACCOUNT_ID>.auth.<AWS_REGION>.amazoncognito.com/oauth2/token",
)
SCOPE = os.environ.get("OKF_MCP_SCOPE", "okf-mcp/invoke")
CREDENTIALS_FILE = os.path.expanduser(
    os.environ.get("OKF_MCP_CREDENTIALS_FILE", "~/.okf/credentials")
)
TOKEN_CACHE_FILE = os.path.expanduser(
    os.environ.get("OKF_MCP_TOKEN_CACHE", "~/.okf/token-cache.json")
)
# Refresh a bit before actual expiry to avoid handing out an about-to-die token.
EXPIRY_SKEW_SECONDS = 60


def _credentials_path() -> str:
    """Resolve the credentials file path, confining it to the user's home dir.

    The path is operator-supplied (OKF_MCP_CREDENTIALS_FILE) and read with the
    user's own privileges, but confining it to $HOME (via realpath, so `..` and
    symlinks can't escape) removes any path-traversal surface (CWE-22).
    """
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(CREDENTIALS_FILE)
    if resolved != home and not resolved.startswith(home + os.sep):
        raise SystemExit(
            f"Refusing to read credentials outside the home directory: "
            f"{CREDENTIALS_FILE}"
        )
    return resolved


def _read_credentials_file() -> dict:
    """Parse env-style `KEY=VALUE` lines from the credentials file, if present."""
    values: dict = {}
    try:
        with open(_credentials_path(), encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return values


def _load_credentials() -> tuple[str, str]:
    """Client id/secret from env, then the credentials file, then a prompt."""
    from_file = _read_credentials_file()
    client_id = os.environ.get("OKF_MCP_CLIENT_ID") or from_file.get(
        "OKF_MCP_CLIENT_ID"
    )
    client_secret = os.environ.get("OKF_MCP_CLIENT_SECRET") or from_file.get(
        "OKF_MCP_CLIENT_SECRET"
    )
    # A common misconfiguration is exporting the literal placeholder text (e.g.
    # settings.json `env` does not expand `$VAR`). Treat that as unset.
    if client_id and client_id.startswith("$"):
        client_id = None
    if client_secret and client_secret.startswith("$"):
        client_secret = None
    if not client_id and sys.stdin.isatty():
        client_id = input("Client ID: ").strip()
    if not client_secret and sys.stdin.isatty():
        client_secret = getpass.getpass("Client secret: ").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing credentials. Set OKF_MCP_CLIENT_ID and "
            f"OKF_MCP_CLIENT_SECRET, or add them to {CREDENTIALS_FILE}."
        )
    return client_id, client_secret


def _read_cache() -> dict:
    try:
        # Refuse a cache that other users can read/write — a loosened-permission
        # or attacker-planted file could leak or inject a token. Treat it as a
        # miss and re-fetch rather than trusting it (CWE-732/-377).
        mode = os.stat(TOKEN_CACHE_FILE).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(
                f"warning: ignoring {TOKEN_CACHE_FILE} — not owner-only (chmod 600).",
                file=sys.stderr,
            )
            return {}
        with open(TOKEN_CACHE_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return {}


def _write_cache(token: str, expires_at: float) -> None:
    """Persist the token in an owner-only (0600) file, created atomically.

    Open with O_CREAT|O_TRUNC and mode 0600 so the token is never briefly
    world-readable between create and chmod (CWE-732).
    """
    directory = os.path.dirname(TOKEN_CACHE_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(TOKEN_CACHE_FILE, flags, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"token": token, "expires_at": expires_at}, handle)
    # If the file pre-existed with looser perms, O_CREAT won't tighten it.
    os.chmod(TOKEN_CACHE_FILE, stat.S_IRUSR | stat.S_IWUSR)


def get_token(force: bool = False) -> str:
    """Return a valid access token, using the on-disk cache when still fresh."""
    if not TOKEN_ENDPOINT:
        raise SystemExit(
            "Token endpoint is empty — set OKF_MCP_TOKEN_ENDPOINT to your "
            "Cognito token endpoint."
        )
    # Refuse to send Basic-auth credentials over a non-TLS endpoint (the
    # endpoint is overridable via env, so a mistyped http:// would leak the
    # client secret in cleartext). urllib already verifies TLS certs by
    # default; this guards the transport itself (CWE-319/-295).
    if not TOKEN_ENDPOINT.lower().startswith("https://"):
        raise SystemExit(
            "Refusing to send credentials to a non-HTTPS token endpoint: "
            f"{TOKEN_ENDPOINT}"
        )
    now = time.time()
    if not force:
        cached = _read_cache()
        if (
            cached.get("token")
            and now < cached.get("expires_at", 0) - EXPIRY_SKEW_SECONDS
        ):
            return cached["token"]

    client_id, client_secret = _load_credentials()
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": SCOPE}
    ).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    # Verify TLS certificates explicitly and translate network/HTTP failures
    # into a clean SystemExit. This avoids surfacing a urllib traceback (whose
    # frames reference the request object carrying the Basic-auth header) to
    # logs (CWE-532). The error text below never includes the credentials.
    ctx = ssl.create_default_context()
    try:
        # nosec B310 - TOKEN_ENDPOINT is a fixed https:// Cognito URL (module
        # constant), never user-controlled, and TLS is verified via the default
        # context above; no file:// / custom-scheme risk.
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:  # nosec B310
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        # Cognito returns e.g. {"error":"invalid_client"} on a 400 — surface the
        # status and any error code, but not the request that carried the secret.
        detail = ""
        try:
            detail = f": {json.load(exc).get('error', '')}".rstrip(": ")
        except Exception:
            pass
        raise SystemExit(
            f"Token endpoint returned HTTP {exc.code}{detail}. "
            "Check OKF_MCP_CLIENT_ID / OKF_MCP_CLIENT_SECRET."
        ) from None
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Could not reach token endpoint {TOKEN_ENDPOINT}: {exc.reason}"
        ) from None

    if "access_token" not in payload:
        # An error response or unexpected shape — report the error field, never
        # the raw payload (it could echo back sensitive request context).
        raise SystemExit(
            "Token endpoint did not return an access_token "
            f"(error: {payload.get('error', 'unknown')})."
        )
    token = payload["access_token"]
    _write_cache(token, now + int(payload.get("expires_in", 3600)))
    return token


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "token"
    token = get_token()
    if mode == "export":
        # Safe to `eval "$(okf-mcp-token.py export)"` to load into the shell.
        # OKF_TOKEN is the bearer var the `okf` MCP server reads (see .mcp.json).
        print(f"export OKF_TOKEN={token}")
    elif mode == "headers":
        # Shape Claude Code's `headersHelper` consumes: JSON headers on stdout.
        print(json.dumps({"Authorization": f"Bearer {token}"}))
    else:
        print(token)
        print(
            "\nUse it as an MCP request header:\n"
            f"  Authorization: Bearer {token[:24]}…",
            file=sys.stderr,
        )
