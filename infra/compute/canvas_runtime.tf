# --- Canvas CI runtime (dashboard authoring) ----------------------------------
# A dedicated AgentCore runtime hosting the dashboard-authoring agent
# (services/canvas): reads the wiki + executes Attested Computations, lints and
# visually inspects dashboards (headless Chromium is baked into the image),
# writes ONLY its own DynamoDB rows (canvas + job rows, chat-table checkpoints).
# No S3-Files mount, no bundle writes, no lease. Invoked with SigV4 by the chat
# runtime (delegation tools) — no inbound JWT authorizer.
#
# The role's execution grants mirror the consumption runtime's computation
# ceiling: only the wiki's frozen statements (typed literals substituted by the
# platform) ever reach Athena. Deploying this runtime (canvas_image_uri set)
# implies computation execution FOR IT — the whole point of the CI is to test
# tiles against live data — independent of var.enable_attested_computations,
# which continues to gate the machine-credential MCP surface.

data "aws_iam_policy_document" "canvas" {
  source_policy_documents = concat(
    [data.aws_iam_policy_document.computations_execution_core.json],
    var.enable_lakeformation ? [data.aws_iam_policy_document.computations_execution_lakeformation.json] : [],
    var.enable_redshift ? [data.aws_iam_policy_document.computations_execution_redshift.json] : [],
  )
  # checkov:skip=CKV_AWS_108:ComputationsTableDataRead (shared doc) s3:GetObject must be "*" because a Glue table's storage location can be any bucket; read-only, and only frozen computation statements reach Athena (see consumption's identical residual).
  # checkov:skip=CKV_AWS_111:Athena/S3 write is scoped to the dedicated results bucket; the broad grants are read/list metadata actions that don't support ARN scoping.
  # checkov:skip=CKV_AWS_356:glue read + athena query are catalog/workgroup-level actions that cannot be pinned to one resource ARN.
  statement {
    sid       = "BundleBucketRead"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [local.d.bundle_bucket_arn, "${local.d.bundle_bucket_arn}/*"]
  }

  # The agent LLM (Converse; adaptive thinking with display omitted).
  statement {
    sid       = "BedrockInvoke"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["*"]
  }

  statement {
    sid = "S3VectorsQuery"
    # GetVectors REQUIRED alongside QueryVectors for filtered queries, else 403.
    actions   = ["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:GetIndex"]
    resources = [local.d.vector_index_arn, local.d.vector_bucket_arn]
  }

  statement {
    sid       = "RegistryRead"
    actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"]
    resources = [local.d.registry_table_arn, "${local.d.registry_table_arn}/index/*"]
  }

  # Canvas + job rows (this runtime is their writer) and the LangGraph
  # checkpoints (thread id = <sub>:canvasjob-<job_id>, chat's table + offload
  # bucket — one durable store for every agent conversation surface).
  statement {
    sid = "CanvasRowsReadWrite"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [local.d.canvas_table_arn]
  }
  statement {
    sid = "CheckpointsReadWrite"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:BatchWriteItem",
    ]
    resources = [local.d.chat_checkpoints_table_arn]
  }
  statement {
    sid = "CheckpointOffload"
    # PutObjectTagging: with ttl_seconds set (the 30-day job-thread TTL),
    # DynamoDBSaver's put_object carries `Tagging: ttl-days=N` — a separate
    # IAM action from PutObject (its absence failed a live job). The tag is
    # belt-and-braces here: the offload bucket's blanket Terraform lifecycle
    # rule owns actual expiry. Deliberately NO Get/PutLifecycleConfiguration —
    # Terraform owns bucket config, and the saver's own attempt to install a
    # tag-keyed rule fails softly (logged + swallowed inside the saver).
    actions   = ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging", "s3:DeleteObject", "s3:ListBucket"]
    resources = [local.d.chat_checkpoint_bucket_arn, "${local.d.chat_checkpoint_bucket_arn}/*"]
  }

  # Computation execution — the SHARED ceiling (computations_iam.tf), merged
  # via source_policy_documents above. Unconditional here: deploying this
  # runtime IS the execution opt-in (testing tiles against live data is the
  # whole point of the CI).
}

resource "aws_iam_role" "canvas" {
  count              = var.canvas_image_uri != "" ? 1 : 0
  name               = "${var.name_prefix}-canvas-runtime"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "canvas_full" {
  source_policy_documents = [
    data.aws_iam_policy_document.agentcore_baseline.json,
    data.aws_iam_policy_document.canvas.json,
  ]
}

resource "aws_iam_role_policy" "canvas" {
  count  = var.canvas_image_uri != "" ? 1 : 0
  name   = "canvas-policy"
  role   = aws_iam_role.canvas[0].id
  policy = data.aws_iam_policy_document.canvas_full.json
}

resource "aws_bedrockagentcore_agent_runtime" "canvas" {
  count = var.canvas_image_uri != "" ? 1 : 0

  agent_runtime_name = "${var.name_prefix}_canvas"
  role_arn           = aws_iam_role.canvas[0].arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.canvas_image_uri
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = merge(local.common_env, local.otel_common_env, {
    AWS_REGION               = var.region
    OTEL_RESOURCE_ATTRIBUTES = "service.name=${var.name_prefix}_canvas"

    # Checkpoints: chat's table + offload bucket (thread ids are job-scoped and
    # per-sub namespaced, so the surfaces can't collide). Job threads expire —
    # a finished job's thread is pure history (the canvas row is the durable
    # state); 30 days unless overridden.
    OKF_CHAT_CHECKPOINT_TABLE     = local.d.chat_checkpoints_table
    OKF_CHECKPOINT_OFFLOAD_BUCKET = local.d.chat_checkpoint_bucket
    OKF_CANVAS_JOB_TTL_SECONDS    = "2592000"

    # The authoring model (adaptive thinking; reasoning text never returned —
    # okf_aws.model_factory default).
    OKF_CANVAS_MODEL      = var.canvas_model
    OKF_CANVAS_EFFORT     = var.canvas_effort
    OKF_CANVAS_MAX_TOKENS = tostring(var.canvas_max_tokens)

    # Computation execution (the tile test path). Redshift-mapped datasets
    # additionally need the Data API client — same deploy flag as everywhere.
    OKF_ATHENA_WORKGROUP     = var.athena_workgroup
    OKF_ATHENA_OUTPUT        = local.athena_output
    OKF_COMPUTATION_MAX_ROWS = tostring(var.computation_max_rows)
    OKF_REDSHIFT_ENABLED     = var.enable_redshift ? "true" : ""
  })

  tags = var.tags
}
