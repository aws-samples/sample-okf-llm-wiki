# Connect an agent

Data Wiki serves published bundles through a streamable HTTP MCP server.

## Create a machine credential

1. Open **Credentials**.
2. Choose **New credential**.
3. Enter a name that identifies one consumer.
4. Create the credential.
5. Copy the client ID and secret.

The secret appears once. Store it in an approved secrets manager.

Use one credential per application or agent. This approach lets you revoke one consumer without affecting others.

## Get a bearer token

Set the credential values in your shell:

```bash
export OKF_MCP_CLIENT_ID="<client-id>"
export OKF_MCP_CLIENT_SECRET="<client-secret>"
```

Exchange them at the Cognito token endpoint:

```bash
curl -s -X POST "<token-endpoint>" \
  -u "$OKF_MCP_CLIENT_ID:$OKF_MCP_CLIENT_SECRET" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&scope=okf-mcp/invoke"
```

Send the returned token as `Authorization: Bearer <token>`.

## Use the included plugin

The `okf-mcp` directory contains a Claude Code plugin.

Copy the MCP URL and token endpoint from the credential dialog. Configure these environment variables:

```bash
export OKF_MCP_URL="<consumption-runtime-url>"
export OKF_MCP_TOKEN_ENDPOINT="<cognito-token-endpoint>"
```

Create `~/.okf/credentials` outside the repository:

```bash
mkdir -p ~/.okf
chmod 700 ~/.okf
printf '%s\n' \
  'OKF_MCP_CLIENT_ID=<client-id>' \
  'OKF_MCP_CLIENT_SECRET=<client-secret>' \
  > ~/.okf/credentials
chmod 600 ~/.okf/credentials
```

The setup skill explains this process. It does not write secrets.

The headers helper refreshes and caches tokens when Claude Code connects.

The refresh skill can reuse a cached token and prints the full token. Do not use it in shared terminals or logs.

To validate new credentials without printing a token response, run:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -u "$OKF_MCP_CLIENT_ID:$OKF_MCP_CLIENT_SECRET" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&scope=okf-mcp/invoke" \
  "$OKF_MCP_TOKEN_ENDPOINT"
```

An HTTP `200` response confirms the endpoint and credential pair.

## Use another MCP client

Configure the client with the consumption runtime endpoint. Add the bearer token to every request.

Call `read_me` first. Use `search_domains` to choose a domain.

Then call `list_domains` with that domain to discover its datasets.

## Revoke access

Delete the credential from **Credentials**. New token issuance stops immediately.

Existing tokens can remain usable until they expire. Machine tokens currently last up to one hour.

## Next step

Review the [MCP tool reference](mcp-tools.md).
