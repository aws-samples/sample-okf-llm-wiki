import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"
import {
  CheckIcon,
  CopyIcon,
  FileCodeIcon,
  FileTextIcon,
  KeyRoundIcon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

// Cognito M2M endpoint + scope, baked in at build time (compute ui_env output).
// Shown in the "how to use" block so a freshly vended credential is actionable.
const TOKEN_ENDPOINT = import.meta.env.VITE_COGNITO_TOKEN_ENDPOINT || ""
const MCP_SCOPE = import.meta.env.VITE_MCP_SCOPE || "okf-mcp/invoke"
const AWS_REGION = import.meta.env.VITE_AWS_REGION || ""
const MCP_RUNTIME_ARN = import.meta.env.VITE_MCP_RUNTIME_ARN || ""

// The consumption MCP server's streamable-HTTP endpoint. AgentCore exposes a
// runtime at its data-plane URL, where the runtime ARN is URL-encoded into the
// path and the qualifier selects the endpoint (DEFAULT). Derived from the region
// + runtime ARN (both baked into the build); "" if either is missing so the UI
// can hide the field rather than show a broken URL. Mirrors okf-mcp/.mcp.json.
const MCP_URL =
  AWS_REGION && MCP_RUNTIME_ARN
    ? `https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${encodeURIComponent(
        MCP_RUNTIME_ARN
      )}/invocations?qualifier=DEFAULT`
    : ""

// Trigger a client-side download of a text file (no server round-trip).
function downloadText(filename, text, mime = "text/plain") {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// A filesystem-safe stem from the credential name (for the download filenames).
function fileStem(name) {
  const slug = (name || "okf-mcp")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
  return slug || "okf-mcp"
}

// Markdown quickstart. Deliberately contains NO client_id/secret — the reader
// supplies those via environment variables at runtime, so the file is safe to
// store/share. Only non-secret config (endpoint, scope, region) is baked in.
function buildGuideMarkdown({ name }) {
  return `# Data wiki MCP credential — ${name}

Machine credential (OAuth2 client_credentials) for calling the Data wiki
consumption MCP server.

> **Your client ID and secret are NOT in this file.** They are shown only once,
> at creation, in the console. Store them securely (a secrets manager or env
> vars) and provide them to the script/commands below at runtime.

| Field | Value |
|---|---|
| MCP URL | \`${MCP_URL}\` |
| Token endpoint | \`${TOKEN_ENDPOINT}\` |
| Scope | \`${MCP_SCOPE}\` |
| AWS region | \`${AWS_REGION}\` |
| MCP runtime ARN | \`${MCP_RUNTIME_ARN}\` |

## 1. Get an access token

Set your credentials as environment variables, then POST to the token endpoint
with HTTP basic auth:

\`\`\`bash
export OKF_MCP_CLIENT_ID=<your client id>
export OKF_MCP_CLIENT_SECRET=<your client secret>

curl -s -X POST "${TOKEN_ENDPOINT}" \\
  -u "$OKF_MCP_CLIENT_ID:$OKF_MCP_CLIENT_SECRET" \\
  -H "Content-Type: application/x-www-form-urlencoded" \\
  -d "grant_type=client_credentials&scope=${MCP_SCOPE}"
\`\`\`

The response is JSON: \`{"access_token": "…", "expires_in": 3600, "token_type": "Bearer"}\`.
Tokens are short-lived (~1 hour) — cache and re-fetch when expired.

## 2. Call the MCP server

Point your MCP client (streamable-HTTP transport) at the MCP URL, sending the
token as a bearer header:

\`\`\`
MCP URL: ${MCP_URL}
Authorization: Bearer <access_token>
\`\`\`

See \`okf-mcp-token.py\` for a ready-to-run helper that reads the same two
environment variables (or prompts for them) and fetches/caches a token.
`
}

// Self-contained Python helper (stdlib only — no pip install). Contains NO
// credentials: it reads client id/secret from env, or prompts for them (secret
// hidden). Only non-secret config (endpoint, scope) is baked in, so the file is
// safe to store and share.
function buildPythonScript({ name }) {
  return `#!/usr/bin/env python3
"""Fetch an OAuth2 access token for the Data wiki MCP server (credential: ${name}).

Standard library only — no dependencies. Uses the client_credentials grant and
caches the token in memory until it expires.

Credentials are NEVER stored in this file. Provide them via environment
variables (recommended for automation):

    export OKF_MCP_CLIENT_ID=<your client id>
    export OKF_MCP_CLIENT_SECRET=<your client secret>

If either is unset, the script prompts for it interactively (the secret input
is hidden).
"""
import base64
import getpass
import json
import os
import sys
import time
import urllib.parse
import urllib.request

TOKEN_ENDPOINT = "${TOKEN_ENDPOINT}"
SCOPE = "${MCP_SCOPE}"
MCP_URL = "${MCP_URL}"

_cache = {"token": None, "expires_at": 0.0}


def _load_credentials() -> tuple[str, str]:
    """Client id/secret from env, prompting for whatever is missing."""
    client_id = os.environ.get("OKF_MCP_CLIENT_ID")
    client_secret = os.environ.get("OKF_MCP_CLIENT_SECRET")
    if not client_id:
        client_id = input("Client ID: ").strip()
    if not client_secret:
        client_secret = getpass.getpass("Client secret: ").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing credentials. Set OKF_MCP_CLIENT_ID and "
            "OKF_MCP_CLIENT_SECRET, or enter them when prompted."
        )
    return client_id, client_secret


def get_token(force: bool = False) -> str:
    """Return a valid access token, fetching a new one when needed (60s skew)."""
    if not TOKEN_ENDPOINT:
        raise SystemExit(
            "TOKEN_ENDPOINT is empty — this script was generated without a "
            "configured Cognito token endpoint. Re-download it from the "
            "Credentials page in the Data wiki console."
        )
    now = time.time()
    if not force and _cache["token"] and now < _cache["expires_at"] - 60:
        return _cache["token"]

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
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)

    _cache["token"] = payload["access_token"]
    _cache["expires_at"] = now + int(payload.get("expires_in", 3600))
    return _cache["token"]


if __name__ == "__main__":
    token = get_token()
    print(token)
    print(
        "\\nUse it against the MCP server (streamable-HTTP):\\n"
        f"  MCP URL: {MCP_URL or '<not configured in this build>'}\\n"
        f"  Authorization: Bearer {token[:24]}…",
        file=sys.stderr,
    )
`
}

// Vend machine credentials (Cognito M2M app clients) for MCP access. Each
// credential is a client_id/client_secret pair the holder exchanges at the
// Cognito token endpoint (client_credentials grant) for a bearer token that the
// consumption MCP server's scope-based authorizer accepts. The secret is shown
// ONCE at creation and never retrievable again.
export default function CredentialsView({ api, email }) {
  const [creds, setCreds] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!api) return
    setLoading(true)
    setError(null)
    try {
      const list = await api.listCredentials()
      setCreds(Array.isArray(list) ? list : [])
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }, [api])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2">
            <KeyRoundIcon className="size-4" />
            MCP credentials
          </CardTitle>
          <CardDescription>
            Machine credentials for applications and agents to call the MCP
            server. Each is an OAuth2 client_credentials app client.
          </CardDescription>
          <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
            <Button variant="outline" onClick={load} disabled={loading}>
              {loading ? <Spinner /> : <RefreshCwIcon data-icon="inline-start" />}
              Refresh
            </Button>
            <NewCredentialDialog api={api} email={email} onCreated={load} />
          </div>
        </CardHeader>
        <CardContent>
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load credentials</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : loading ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : creds.length === 0 ? (
            <Alert>
              <KeyRoundIcon />
              <AlertTitle>No credentials yet</AlertTitle>
              <AlertDescription>
                Create one with "New credential" to let an app or agent call the
                MCP server.
              </AlertDescription>
            </Alert>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Client ID</TableHead>
                  <TableHead>Created by</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="w-0" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {creds.map((c) => (
                  <TableRow key={c.client_id}>
                    <TableCell className="font-medium">{c.name}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {c.client_id}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {c.created_by || "—"}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {c.created_at
                        ? new Date(c.created_at).toLocaleString()
                        : "—"}
                    </TableCell>
                    <TableCell>
                      <RevokeCredentialDialog
                        api={api}
                        credential={c}
                        onRevoked={load}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// Small copy-to-clipboard button with a transient check.
function CopyButton({ value, label }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error("Copy failed — select and copy manually.")
    }
  }
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      aria-label={`Copy ${label}`}
      onClick={copy}
    >
      {copied ? <CheckIcon className="text-primary" /> : <CopyIcon />}
    </Button>
  )
}

function ReadonlyField({ label, value }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <Label className="text-muted-foreground text-xs">{label}</Label>
      <div className="flex min-w-0 items-center gap-1">
        <code className="min-w-0 flex-1 truncate rounded bg-muted px-2 py-1.5 font-mono text-xs">
          {value}
        </code>
        <div className="shrink-0">
          <CopyButton value={value} label={label} />
        </div>
      </div>
    </div>
  )
}

function NewCredentialDialog({ api, email, onCreated }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [submitting, setSubmitting] = useState(false)
  // Once created, we hold the secret in memory to show ONCE. Cleared on close.
  const [issued, setIssued] = useState(null)

  const reset = () => {
    setName("")
    setIssued(null)
  }

  const submit = async (e) => {
    e.preventDefault()
    if (!name.trim()) {
      toast.error("Give the credential a name.")
      return
    }
    setSubmitting(true)
    try {
      const res = await api.createCredential(name.trim(), email)
      setIssued(res)
      onCreated?.()
    } catch (err) {
      toast.error(`Could not create credential: ${err.message || err}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o)
        if (!o) reset()
      }}
    >
      <DialogTrigger asChild>
        <Button>
          <PlusIcon data-icon="inline-start" />
          New credential
        </Button>
      </DialogTrigger>
      <DialogContent>
        {issued ? (
          // Post-creation: show the secret ONCE.
          <div className="flex min-w-0 flex-col gap-4">
            <DialogHeader>
              <DialogTitle>Credential created</DialogTitle>
              <DialogDescription>
                Copy the client secret now — it is shown only once and cannot be
                retrieved again.
              </DialogDescription>
            </DialogHeader>
            <div className="flex min-w-0 flex-col gap-3">
              <ReadonlyField label="Client ID" value={issued.client_id} />
              <ReadonlyField label="Client secret" value={issued.client_secret} />
              {MCP_URL ? (
                <ReadonlyField label="MCP URL" value={MCP_URL} />
              ) : null}
              {TOKEN_ENDPOINT ? (
                <ReadonlyField label="Token endpoint" value={TOKEN_ENDPOINT} />
              ) : null}
            </div>
            <Alert>
              <KeyRoundIcon />
              <AlertTitle>How to generate a token</AlertTitle>
              <AlertDescription className="min-w-0">
                <div className="flex min-w-0 flex-col gap-2">
                  {/* Ready-to-run artifacts — more useful than copy-pasting.
                      Downloadable only here, while the secret is in memory.
                      Disabled if the build lacks the token endpoint (misconfig),
                      so we never emit a script that can't fetch a token. */}
                  <div className="flex flex-wrap gap-2 pt-1">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={!TOKEN_ENDPOINT}
                      onClick={() =>
                        downloadText(
                          `${fileStem(issued.name)}-mcp-guide.md`,
                          buildGuideMarkdown({ name: issued.name }),
                          "text/markdown"
                        )
                      }
                    >
                      <FileTextIcon data-icon="inline-start" />
                      Guide (.md)
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={!TOKEN_ENDPOINT}
                      onClick={() =>
                        downloadText(
                          `${fileStem(issued.name)}-mcp-token.py`,
                          buildPythonScript({ name: issued.name }),
                          "text/x-python"
                        )
                      }
                    >
                      <FileCodeIcon data-icon="inline-start" />
                      Script (.py)
                    </Button>
                  </div>
                  {!TOKEN_ENDPOINT ? (
                    <span className="text-muted-foreground text-xs">
                      Downloads unavailable: this build has no token endpoint
                      configured.
                    </span>
                  ) : null}
                </div>
              </AlertDescription>
            </Alert>
            <DialogFooter>
              <DialogClose asChild>
                <Button type="button">Done</Button>
              </DialogClose>
            </DialogFooter>
          </div>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-4">
            <DialogHeader>
              <DialogTitle>New MCP credential</DialogTitle>
              <DialogDescription>
                Creates an OAuth2 client_credentials app client scoped to the MCP
                server. You'll get a client ID and secret.
              </DialogDescription>
            </DialogHeader>
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-cred-name">Name</Label>
              <Input
                id="new-cred-name"
                value={name}
                placeholder="e.g. analytics-agent-prod"
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <DialogFooter>
              <DialogClose asChild>
                <Button type="button" variant="outline">
                  Cancel
                </Button>
              </DialogClose>
              <Button type="submit" disabled={submitting}>
                {submitting ? <Spinner /> : null}
                Create
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

function RevokeCredentialDialog({ api, credential, onRevoked }) {
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const revoke = async () => {
    setDeleting(true)
    try {
      await api.deleteCredential(credential.client_id)
      toast.success(`Revoked ${credential.name}`)
      setOpen(false)
      onRevoked?.()
    } catch (err) {
      toast.error(`Could not revoke: ${err.message || err}`)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Revoke credential">
          <Trash2Icon />
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Revoke credential?</DialogTitle>
          <DialogDescription>
            This deletes the app client{" "}
            <span className="font-medium text-foreground">
              {credential.name}
            </span>{" "}
            ({credential.client_id}). Any app using it will immediately stop
            getting tokens. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline">
              Cancel
            </Button>
          </DialogClose>
          <Button variant="destructive" onClick={revoke} disabled={deleting}>
            {deleting ? <Spinner /> : <Trash2Icon data-icon="inline-start" />}
            Revoke
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
