# Least-privilege policy documents for each Lambda.

# Reindex worker: read the bundle bucket, embed (Bedrock), Put/Delete/GetIndex on
# S3 Vectors, read/write its dedup rows in the freshness table, consume its SQS.
data "aws_iam_policy_document" "reindex" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${local.d.bundle_bucket_arn}/*"]
  }
  # ListBucket: deciding whether a cross-dataset pair still has any doc left
  # (the XREF signal's delete path) reads CURRENT S3 truth for the pair prefix.
  statement {
    actions   = ["s3:ListBucket"]
    resources = [local.d.bundle_bucket_arn]
  }
  statement {
    actions   = ["bedrock:InvokeModel"]
    resources = ["*"]
  }
  statement {
    actions = [
      "s3vectors:PutVectors", "s3vectors:DeleteVectors",
      "s3vectors:GetIndex", "s3vectors:CreateIndex",
    ]
    resources = [local.d.vector_index_arn, local.d.vector_bucket_arn]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [local.d.freshness_table_arn]
  }
  # Registry: the derived cross-dataset reference signal (XREF# rows on the
  # target domain's partition — see reindex.handler._upsert_xref). PutItem +
  # DeleteItem only; the worker never reads or touches other registry rows.
  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [local.d.registry_table_arn]
  }
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.reindex.arn]
  }
}

# Incremental orchestrator: read Glue, read/write freshness + registry, write the
# pending diff to S3, invoke the harvest runtime, consume its SQS. With policy
# builds on, it is also the AR rebuild authority (policy_rebuild events + the
# nightly reconcile that completes/repairs policies).
data "aws_iam_policy_document" "incremental" {
  # checkov:skip=CKV_AWS_356:glue:Get* (read-only metadata) targets the whole catalog by design — the source database being reconciled is not known until an event arrives and Glue read actions carry no cross-database data exposure here. InvokeAgentRuntime IS scoped to the single harvest runtime (local.harvest_invoke_resources) below. All write paths (DynamoDB, S3, SQS) are resource-scoped.
  statement {
    actions   = ["glue:GetTable", "glue:GetTableVersions", "glue:GetTables", "glue:GetDatabases"]
    resources = ["*"]
  }
  statement {
    actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [
      local.d.freshness_table_arn,
      local.d.registry_table_arn,
      "${local.d.registry_table_arn}/index/*",
    ]
  }
  statement {
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${local.d.bundle_bucket_arn}/*"]
  }
  statement {
    # Scoped to the one harvest runtime + its endpoint sub-resources (hierarchical
    # DEFAULT-qualifier auth); see local.harvest_invoke_resources.
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = local.harvest_invoke_resources
  }
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.incremental.arn]
  }

  # Policy rebuild authority (var.enable_policy_build): reached by
  # `policy_rebuild` events and the nightly reconcile. v2 (LLM-judge engine)
  # makes only DETERMINISTIC decisions here — fingerprint checks, document
  # reads, stall reaping, and mode="ar_rules" dispatches through the
  # InvokeAgentRuntime grant above. NO Bedrock grants at all: the authoring
  # agent runs on the harvest runtime.

  # The AR source fingerprint (ar_source_hash) is computed by LISTING the
  # source prefix and GETting each file. The S3 statement above has Put/Get on
  # ".../*" but NO bucket-level ListBucket, so the gather step would
  # AccessDeny on ListObjectsV2 without this (moto doesn't enforce IAM — only
  # a live deploy catches it, the BatchGetItem class of bug).
  dynamic "statement" {
    for_each = local.ar_build_enabled ? [1] : []
    content {
      sid       = "ArSourceList"
      actions   = ["s3:ListBucket"]
      resources = [local.d.bundle_bucket_arn]
    }
  }
}

# Control API: list Glue, read/write registry, read/write bundle + presign
# uploads, read freshness, invoke the harvest runtime.
data "aws_iam_policy_document" "control_api" {
  source_policy_documents = concat(
    var.enable_attested_computations ? [data.aws_iam_policy_document.computations_execution_core.json] : [],
    var.enable_attested_computations && var.enable_lakeformation ? [data.aws_iam_policy_document.computations_execution_lakeformation.json] : [],
    var.enable_attested_computations && var.enable_redshift ? [data.aws_iam_policy_document.computations_execution_redshift.json] : [],
  )
  # checkov:skip=CKV_AWS_356:glue:GetDatabases/GetTables (read-only catalog listing for the "register a dataset" picker) targets the whole catalog by design — the API lists across all databases. InvokeAgentRuntime IS scoped to the single harvest runtime (local.harvest_invoke_resources); StopRuntimeSession stays "*" (see its statement below). logs:FilterLogEvents is already scoped to the AgentCore runtime log-group namespace; DynamoDB/S3/Cognito grants below are resource-scoped. The Redshift picker list actions (redshift:DescribeClusters/redshift-serverless:ListWorkgroups/redshift-data:ListDatabases) don't support resource-level scoping; secretsmanager:GetSecretValue is scoped to the var.redshift_secret_name_prefix name pattern (mappings are self-describing, so per-mapping secrets can't be enumerated at deploy time).
  statement {
    actions   = ["glue:GetDatabases", "glue:GetTables"]
    resources = ["*"]
  }
  statement {
    # registry + freshness as before; annotations table for the user-scoped
    # annotation CRUD routes AND the pre-flight orphan sweep (Query the caller's
    # partition, UpdateItem to auto-resolve orphaned notes) — see handlers.
    # chat index table for the per-user conversation list (GET/rename/delete);
    # chat checkpoint table for the delete-purge (Query/Scan the conversation's
    # checkpoint items + DeleteItem). The chat RUNTIME owns the writes to both;
    # the Control API only reads/renames/deletes for the sidebar.
    # BatchGetItem: the repromote convergence check reads the freshness table's
    # VEC# rows for every touched key in one call — it is a SEPARATE IAM action
    # from GetItem (moto doesn't enforce IAM, so only a live deploy catches it
    # missing: the status GET 500s and the UI's progress poll never advances).
    actions = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [
      local.d.registry_table_arn,
      # index/*: Query against the registry's by-entity GSI authorizes on the
      # index sub-resource, not the table ARN.
      "${local.d.registry_table_arn}/index/*",
      local.d.freshness_table_arn,
      local.d.annotations_table_arn,
      local.d.chat_table_arn,
      local.d.chat_checkpoints_table_arn,
    ]
  }
  statement {
    # GetObjectVersion + ListBucketVersions power the bundle version history /
    # diff / repromote endpoints (CopyObject with a source VersionId authorizes
    # as GetObjectVersion on the source + PutObject on the destination).
    # DeleteObjectVersion: the dataset-delete purge issues version-targeted
    # DeleteObjects (Key + VersionId) against the gold-carrying benchmark/
    # prefix — those authorize as DeleteObjectVersion, NOT DeleteObject, and
    # the failures come back in-band (HTTP 200 with per-key Errors), so a
    # missing grant silently purges nothing (moto doesn't enforce IAM; only a
    # live deploy catches it).
    actions = [
      "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
      "s3:DeleteObjectVersion",
      "s3:GetObjectVersion", "s3:ListBucketVersions",
    ]
    resources = [local.d.bundle_bucket_arn, "${local.d.bundle_bucket_arn}/*"]
  }
  statement {
    # InvokeAgentRuntime starts a harvest. Scoped to the one harvest runtime + its
    # endpoint sub-resources (a DEFAULT-qualifier invoke authorizes hierarchically
    # against both the runtime and its endpoint ARN); see local.harvest_invoke_resources.
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = local.harvest_invoke_resources
  }
  statement {
    # StopRuntimeSession cancels an in-flight harvest (POST /harvest/{domain}/{dataset}/cancel)
    # by stopping the microVM the status row's runtime_session_id points at. Left at
    # "*": the AWS Service Authorization Reference does not confirm identity-based
    # resource-level scoping for this action, so pinning it to the runtime ARN risks
    # AccessDeny on the cancel path. The blast radius is bounded — it only stops a
    # session, and the call is best-effort (handlers.cancel_harvest frees the lease
    # even if the stop fails). Revisit if AWS documents a supported resource type.
    actions   = ["bedrock-agentcore:StopRuntimeSession"]
    resources = ["*"]
  }
  # Read the harvest runtime's own CloudWatch logs to serve the live step feed
  # (GET /harvest/{domain}/{dataset}/events). The runtime writes OKF_STEP lines to
  # its stdout log group; we FilterLogEvents by the run's session id. Scoped to
  # the AgentCore runtime log-group namespace (no new store — reuses these logs).
  statement {
    actions = ["logs:FilterLogEvents"]
    resources = [
      "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*",
    ]
  }
  # Vend/revoke MCP machine credentials = create/delete Cognito user-pool app
  # clients (client_credentials grant, scoped to okf-mcp/invoke). Scoped to the
  # one user pool. Describe is not needed (create returns the secret; list comes
  # from the registry), but included for parity/debuggability.
  statement {
    actions = [
      "cognito-idp:CreateUserPoolClient",
      "cognito-idp:DeleteUserPoolClient",
      "cognito-idp:DescribeUserPoolClient",
    ]
    resources = [local.d.user_pool_arn]
  }

  # `policy_rebuild` publish from the REPROMOTE success path (a non-blocking
  # accelerator: after the restore commits, one UpdateItem flags the dataset
  # row stale and one PutEvents starts the repair — milliseconds; repromote
  # never waits on a build). Correctness still comes from the fingerprint
  # gate, so a lost event only costs freshness until the nightly reconcile.
  # Publish-only, scoped to the DEFAULT bus every rule in this stack lives on.
  dynamic "statement" {
    for_each = local.ar_enabled ? [1] : []
    content {
      sid       = "PolicyRebuildPublish"
      actions   = ["events:PutEvents"]
      resources = [local.event_bus_arn]
    }
  }

  # Redshift source pickers (var.enable_redshift): the UI lists clusters/workgroups
  # (control-plane, no DB connection) and then databases within a chosen target
  # (redshift-data:ListDatabases, which DOES connect and needs a Secrets Manager
  # secret). These list actions don't support resource-level scoping, so "*".
  dynamic "statement" {
    for_each = var.enable_redshift ? [1] : []
    content {
      sid = "RedshiftSourcePickers"
      actions = [
        "redshift:DescribeClusters",
        "redshift-serverless:ListWorkgroups",
        "redshift-data:ListDatabases",
      ]
      resources = ["*"]
    }
  }
  # Read the connection secret to authenticate the database listing. The operator
  # picks per-mapping secrets in the UI, which the API can't enumerate at deploy
  # time, so the grant is scoped by NAME PREFIX (var.redshift_secret_name_prefix,
  # default "okf-") — this bounds which credentials a console user can exercise
  # through the picker endpoints to secrets deliberately named for this system.
  dynamic "statement" {
    for_each = var.enable_redshift ? [1] : []
    content {
      sid       = "RedshiftSecretRead"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = local.redshift_secret_resources
    }
  }

  # Computation execution (the UI's Run modal): the SHARED ceiling
  # (computations_iam.tf) merged via source_policy_documents above, behind
  # var.enable_attested_computations.
}
