#!/usr/bin/env python3
"""Thin CLI over the deployed OKF consumption MCP (AgentCore streamable-HTTP).

This is how the "agent + OKF" generators read bundles: it exposes the deployed
MCP tools (list_domains, list_directory, read_page, glob, grep, semantic_search)
as subcommands, so a generator agent explores a bundle exactly as the product's
agent would — but via a deterministic client that is robust under heavy parallel
fan-out and self-manages the short-lived machine-to-machine (M2M) token.

Auth: Cognito M2M client_credentials (scope ``okf-mcp/invoke``). The access
token is cached to a temp file and re-minted near expiry; the app client secret
is fetched with the AWS CLI (cognito-idp describe-user-pool-client).

Configuration — all via environment variables (no defaults are baked in, since
they are deployment-specific). Get the values from your deployed stack:

    cd infra/compute && terraform output consumption_runtime_arn
    # Cognito user-pool id, domain, and M2M client id come from the durable
    # stack / your OKF admin console when you vend machine credentials.

  OKF_MCP_RUNTIME_ARN   arn:aws:bedrock-agentcore:<region>:<acct>:runtime/okf_consumption-XXXX
  OKF_TOKEN_ENDPOINT    https://<cognito-domain>.auth.<region>.amazoncognito.com/oauth2/token
  OKF_M2M_CLIENT_ID     the machine (client_credentials) app-client id
  OKF_USER_POOL_ID      the Cognito user pool id (to fetch the client secret)
  OKF_REGION            AWS region (default: eu-west-1)

Usage (all 9 MCP tools):
  python3 mcp_query.py domains                                                    # list_domains
  python3 mcp_query.py declared-domains                                           # list_declared_domains
  python3 mcp_query.py search-domains --query "financial"                         # search_domains
  python3 mcp_query.py ls    --domain bird --dataset formula_1 --path .           # list_directory
  python3 mcp_query.py read  --domain bird --dataset formula_1 --path tables/results.md  # read_page
  python3 mcp_query.py glob  --domain bird --dataset formula_1 --pattern "tables/*.md"   # glob
  python3 mcp_query.py grep  --domain bird --dataset formula_1 --pattern "podium"        # grep
  python3 mcp_query.py backlinks --domain bird --dataset formula_1 --path tables/results # get_backlinks
  python3 mcp_query.py search --domain bird --dataset formula_1 --query "fastest lap" [--k 5]  # semantic_search
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

REGION = os.environ.get("OKF_REGION", "eu-west-1")
SCOPE = "okf-mcp/invoke"


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"ERROR: environment variable {name} is not set.\n"
            "  This client targets YOUR deployed OKF stack; there are no baked-in\n"
            "  defaults. Set OKF_MCP_RUNTIME_ARN, OKF_TOKEN_ENDPOINT, OKF_M2M_CLIENT_ID,\n"
            "  and OKF_USER_POOL_ID (see the module docstring / README)."
        )
    return val


def _client_secret(user_pool_id: str, client_id: str) -> str:
    out = subprocess.run(
        ["aws", "cognito-idp", "describe-user-pool-client",
         "--user-pool-id", user_pool_id, "--client-id", client_id,
         "--region", REGION, "--query", "UserPoolClient.ClientSecret",
         "--output", "text"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _mint_token() -> dict:
    endpoint = _require_env("OKF_TOKEN_ENDPOINT")
    client_id = _require_env("OKF_M2M_CLIENT_ID")
    user_pool_id = _require_env("OKF_USER_POOL_ID")
    secret = _client_secret(user_pool_id, client_id)

    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
    basic = urllib.parse.quote(client_id) + ":" + urllib.parse.quote(secret)
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": "Basic " + base64.b64encode(basic.encode()).decode()})
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read())
    tok["_expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 120
    return tok


def _token_cache_path() -> str:
    client_id = _require_env("OKF_M2M_CLIENT_ID")
    return os.path.join("/tmp", f"okf_mcp_token_{client_id}.json")


def get_token() -> str:
    cache = _token_cache_path()
    if os.path.exists(cache):
        try:
            tok = json.load(open(cache))
            if tok.get("_expires_at", 0) > time.time():
                return tok["access_token"]
        except Exception:  # noqa: BLE001
            pass
    tok = _mint_token()
    tmp = cache + f".{os.getpid()}"
    json.dump(tok, open(tmp, "w"))
    os.replace(tmp, cache)  # atomic under parallel writers
    return tok["access_token"]


def _mcp_url() -> str:
    runtime_arn = _require_env("OKF_MCP_RUNTIME_ARN")
    enc = urllib.parse.quote(runtime_arn, safe="")
    return f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"


def _post(payload: dict, token: str, session: str) -> str:
    req = urllib.request.Request(
        _mcp_url(), data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Mcp-Session-Id": session})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def _parse_sse(raw: str) -> dict:
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            obj = json.loads(line)
            if obj.get("id") == 2:
                return obj
    raise RuntimeError(f"no JSON-RPC result in response: {raw[:300]}")


def call_tool(name: str, arguments: dict) -> list[str]:
    token = get_token()
    session = f"minidev-{name}-{os.getpid()}"
    # stateless_http server: initialize then call under the same session id.
    _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                      "clientInfo": {"name": "minidev", "version": "0"}}}, token, session)
    raw = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}, token, session)
    obj = _parse_sse(raw)
    if "error" in obj:
        raise RuntimeError(f"MCP error: {obj['error']}")
    return [c.get("text", "") for c in obj.get("result", {}).get("content", [])]


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Domain discovery (no --domain/--dataset required)
    sub.add_parser("domains")
    sub.add_parser("declared-domains")
    p = sub.add_parser("search-domains")
    p.add_argument("--query", required=True)
    p.add_argument("--k", type=int, default=5)

    # Per-dataset tools (require --domain + --dataset)
    for c in ("ls", "read", "glob", "grep", "backlinks", "search"):
        p = sub.add_parser(c)
        p.add_argument("--domain", required=True)
        p.add_argument("--dataset", required=True)
        p.add_argument("--path")
        p.add_argument("--pattern")
        p.add_argument("--query")
        p.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    if args.cmd == "domains":
        out = call_tool("list_domains", {})
    elif args.cmd == "declared-domains":
        out = call_tool("list_declared_domains", {})
    elif args.cmd == "search-domains":
        out = call_tool("search_domains", {"query": args.query, "top_k": args.k})
    elif args.cmd == "ls":
        # The server treats path="." as a literal subdir (returns empty); the
        # bundle ROOT is path="" (empty string). Default to root.
        out = call_tool("list_directory", {"data_domain": args.domain, "dataset": args.dataset,
                                            "path": args.path if args.path is not None else ""})
    elif args.cmd == "read":
        # read_page takes a concept_id (path WITHOUT the .md suffix), e.g. tables/results
        concept = args.path[:-3] if args.path and args.path.endswith(".md") else args.path
        out = call_tool("read_page", {"data_domain": args.domain, "dataset": args.dataset,
                                      "concept_id": concept})
    elif args.cmd == "glob":
        out = call_tool("glob", {"data_domain": args.domain, "dataset": args.dataset,
                                 "pattern": args.pattern})
    elif args.cmd == "grep":
        out = call_tool("grep", {"data_domain": args.domain, "dataset": args.dataset,
                                 "pattern": args.pattern})
    elif args.cmd == "backlinks":
        # get_backlinks takes a concept_id (like read_page)
        concept = args.path[:-3] if args.path and args.path.endswith(".md") else args.path
        out = call_tool("get_backlinks", {"data_domain": args.domain, "dataset": args.dataset,
                                          "concept_id": concept})
    elif args.cmd == "search":
        out = call_tool("semantic_search", {"data_domain": args.domain, "dataset": args.dataset,
                                            "query": args.query, "top_k": args.k})
    else:
        ap.error("unknown cmd")
    print("\n".join(out) if out else "(empty)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - agents read stderr for the reason
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
