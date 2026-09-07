# Configure deployment options

Terraform variables control models, optional tools, security settings, and retention.

## Pass a Terraform variable

Export a `TF_VAR_` environment variable before the related deployment stage:

```bash
export TF_VAR_enable_redshift=true
./scripts/deploy.sh compute
```

You can also use a Terraform variable file in the target stack.

## Common options

| Variable | Default | Purpose |
| --- | --- | --- |
| `enable_redshift` | `false` | Add Amazon Redshift support |
| `enable_lakeformation` | `false` | Add Lake Formation data access |
| `enable_code_interpreter` | `true` | Extract text from binary context files |
| `enable_chat_sql` | `true` | Offer optional read-only SQL in chat |
| `enable_web_search` | `true` | Add public web search to chat |
| `web_search_region` | `us-east-1` | Select the Region for the web search gateway |
| `enable_attested_computations` | `false` | Allow MCP computation execution |
| `bundle_version_retention_days` | `90` | Retain noncurrent bundle versions |

Review the descriptions in `infra/compute/variables.tf` and `infra/durable/variables.tf`.

Web search is available in `us-east-1`, `eu-west-1`, and `ap-northeast-1`. Its gateway can run outside the main deployment Region.

Set `enable_web_search=false` to disable the feature. Select `web_search_region` to control where its queries run.

## Configure models

Model catalogs define the choices that the console offers. The runtime validates every selected model and effort.

Keep harvest and chat catalogs separate. Harvest authoring often needs a stronger model than routine chat.

## Update one layer

Use the narrowest deployment stage:

```bash
./scripts/deploy.sh images
./scripts/deploy.sh compute
./scripts/deploy.sh ui
```

When runtime code changes, run `images` and then `compute`. The image stage only builds and pushes images.

Run `ui` after web console changes. Run `cognito-urls` when the console URL changes.

## Destroy the deployment

Run:

```bash
./scripts/deploy.sh destroy
```

Use `destroy --force` only after you review the reported cleanup actions.

The force option empties the versioned bundle and UI buckets. It also removes an attached S3 File System when detected.

The Terraform state bucket and private ECR repositories are created outside the stacks. Destroy does not remove them.

Container images also remain in ECR. Remove those resources manually when you no longer need the deployment.

A nonempty chat checkpoint bucket can still block destruction. Empty it before retrying the durable stack destroy.

!!! warning
    Destroy removes resources managed by the compute and durable stacks. It does not delete source databases or every deployment-created resource.
