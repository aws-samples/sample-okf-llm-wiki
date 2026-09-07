# Troubleshooting

Use these checks before you change infrastructure or rerun a large harvest.

## Deployment cannot access AWS

Run:

```bash
aws sts get-caller-identity
```

Refresh your AWS session when the command fails or returns the wrong account.

## Container build fails

Confirm that Docker is running. Then verify `docker buildx version`.

Run the image stage again:

```bash
./scripts/deploy.sh images
```

The script refreshes private and public Amazon ECR authentication.

After a successful image build, run the compute stage to deploy the new image URIs:

```bash
./scripts/deploy.sh compute
```

## Console sign-in fails

Run the Cognito URL stage again:

```bash
./scripts/deploy.sh cognito-urls
```

Then rebuild and upload the console:

```bash
./scripts/deploy.sh ui
```

Confirm that the browser URL matches a Cognito callback URL.

## A dataset does not appear

Confirm that the domain and mapping exist. Check that the selected source type is enabled.

For Glue, confirm that metadata roles can describe the database. Lake Formation can hide tables without a `DESCRIBE` grant.

## A harvest fails

Read the status detail first.

Open the CloudWatch GenAI Observability trace when Transaction Search is enabled. Otherwise, inspect AgentCore and CloudWatch logs.

Check these common causes:

- Missing Bedrock model access
- Source permission failure
- Athena or Redshift query failure
- Missing Lake Formation grants
- Context extraction error reported in status or logs
- Model throttling

Code interpreter startup or upload errors can fall back to text-only processing. They do not always fail the harvest.

## A bundle is not searchable

Check the reindex queue, dead-letter queue, and worker logs.

Old events with the same sequencer are skipped. Replaying the same event does not rebuild its vector.

Restore or rewrite the live Markdown object to create a new event. The repository does not include a full vector backfill command.

## MCP returns 401 or 403

Confirm the token endpoint, MCP URL, client ID, secret, and scope.

Remove `~/.okf/token-cache.json` before testing changed credentials. A valid cached token can hide a bad credential pair.

Use the status-only request in [Connect an agent](user-guide/connect-an-agent.md). Do not print bearer tokens into shared logs.

Revoke and recreate the machine credential when the secret is lost.

## Documentation build fails

Run:

```bash
npm --prefix ui run docs:check
npm --prefix ui run docs:build
```

The style check reports the source file and line. The build reports missing pages, assets, or imports.
