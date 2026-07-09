# Least-privilege policy documents for each Lambda.

# Reindex worker: read the bundle bucket, embed (Bedrock), Put/Delete/GetIndex on
# S3 Vectors, read/write its dedup rows in the freshness table, consume its SQS.
data "aws_iam_policy_document" "reindex" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${local.d.bundle_bucket_arn}/*"]
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
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.reindex.arn]
  }
}

# Incremental orchestrator: read Glue, read/write freshness + registry, write the
# pending diff to S3, invoke the harvest runtime, consume its SQS.
data "aws_iam_policy_document" "incremental" {
  # checkov:skip=CKV_AWS_356:glue:Get* (read-only metadata) targets the whole catalog by design — the source database being reconciled is not known until an event arrives and Glue read actions carry no cross-database data exposure here. InvokeAgentRuntime IS scoped to the single harvest runtime (local.harvest_invoke_resources) below. All write paths (DynamoDB, S3, SQS) are resource-scoped.
  statement {
    actions   = ["glue:GetTable", "glue:GetTableVersions", "glue:GetTables", "glue:GetDatabases"]
    resources = ["*"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [local.d.freshness_table_arn, local.d.registry_table_arn]
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
}

# Control API: list Glue, read/write registry, read/write bundle + presign
# uploads, read freshness, invoke the harvest runtime.
data "aws_iam_policy_document" "control_api" {
  # checkov:skip=CKV_AWS_356:glue:GetDatabases/GetTables (read-only catalog listing for the "register a dataset" picker) targets the whole catalog by design — the API lists across all databases. InvokeAgentRuntime IS scoped to the single harvest runtime (local.harvest_invoke_resources); StopRuntimeSession stays "*" (see its statement below). logs:FilterLogEvents is already scoped to the AgentCore runtime log-group namespace; DynamoDB/S3/Cognito grants below are resource-scoped.
  statement {
    actions   = ["glue:GetDatabases", "glue:GetTables"]
    resources = ["*"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [local.d.registry_table_arn, local.d.freshness_table_arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
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
}
