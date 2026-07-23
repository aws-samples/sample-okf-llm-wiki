# IAM execution roles for the two AgentCore runtimes. These perms apply to ALL
# agent code and can't be scoped per-invocation, so keep them least-privilege.

locals {
  # Bedrock Mantle (OpenAI-compatible) perms are needed whenever a GPT model is
  # REACHABLE at harvest time — NOT just when it's the deploy-time default. The
  # harvest UI's per-run picker lets a caller select any model in
  # harvest_model_catalog, and the Control API validates against that catalog and
  # passes the choice through as a per-invocation override (the runtime does not
  # re-allowlist it). So a Claude-default deploy can still route a single harvest
  # to Mantle. Gating the grant on var.harvest_model alone left those runs failing
  # with 401 permission_denied on bedrock-mantle:CreateInference. Grant when the
  # default OR any catalog entry is an openai.* id.
  harvest_mantle_enabled = anytrue(concat(
    [startswith(var.harvest_model, "openai.")],
    [for m in var.harvest_model_catalog : startswith(m.model, "openai.")],
  ))

  # Same reasoning for the chat runtime: grant Mantle when a GPT id is reachable
  # at chat time (the deploy-time default OR any catalog entry the picker offers).
  chat_mantle_enabled = anytrue(concat(
    [startswith(var.chat_model, "openai.")],
    [for m in var.chat_model_catalog : startswith(m.model, "openai.")],
  ))
}

data "aws_iam_policy_document" "agentcore_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    # Canonical confused-deputy guards for the AgentCore service principal.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:*"]
    }
  }
}

# Baseline permissions EVERY AgentCore runtime execution role needs — the
# create-time validator rejects the role without them. Straight from the AWS
# "IAM Permissions for AgentCore Runtime" doc: ECR image pull + auth token,
# CloudWatch Logs for the runtime log group, X-Ray traces, and the scoped
# PutMetricData. Attached to BOTH runtimes' roles.
data "aws_iam_policy_document" "agentcore_baseline" {
  statement {
    sid       = "ECRImageAccess"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = ["arn:aws:ecr:${var.region}:${local.account_id}:repository/*"]
  }
  statement {
    sid       = "ECRTokenAccess"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken does not support resource scoping
  }
  statement {
    sid       = "LogGroup"
    actions   = ["logs:CreateLogGroup", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }
  statement {
    sid       = "LogGroupDescribe"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:*"]
  }
  statement {
    sid       = "LogStream"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }
}

# --- Harvest DATA role (assumed per-invocation, down-scoped) -------------------
# The Glue/Athena/table-data grants a harvest NEEDS are inherently broad on the
# STATIC role — the target database isn't known until invoke time (threats #9/#60,
# and AgentCore exposes the execution role to any in-VM code via MMDS, so a
# tool-layer allow-list is NOT a credential boundary). So these broad grants live
# on a SEPARATE data role that the harvest runtime assumes PER INVOCATION with an
# inline STS session policy pinned to the one database + workgroup being harvested
# (see services/harvest/src/harvest/clients.py:build_scoped_session). The session
# policy can only INTERSECT this ceiling, so the effective per-run permission is
# the single dataset — enforced by IAM, outside the (injectable) Python process.
#
# Actions that genuinely cannot be ARN-scoped stay here at the ceiling:
#   - glue:GetDatabases (list) is catalog-level only, and
#   - TableDataRead s3:GetObject is "*" because Glue tables point at arbitrary
#     buckets (e.g. the BIRD data bucket) — the session policy keeps these broad.
data "aws_iam_policy_document" "harvest_data" {
  # checkov:skip=CKV_AWS_108:Accepted residual — TableDataRead s3:GetObject must be "*" because a Glue table's storage location can be any bucket; contained by the per-invocation STS session policy (clients._session_policy), not this static ceiling.
  # checkov:skip=CKV_AWS_111:Athena/S3 write is scoped to the dedicated results bucket; the remaining broad reads are metadata/list actions that do not support ARN scoping. See the block comment above.
  # checkov:skip=CKV_AWS_356:glue:GetDatabases and athena:* are catalog/workgroup-level actions that cannot be pinned to one resource ARN; cross-database containment is carried by the per-invocation session policy's pinned Glue table ARNs. redshift-data:*, the redshift auth actions, and secretsmanager:GetSecretValue likewise cannot be ARN-pinned on this static ceiling — Redshift mappings are self-describing so the target cluster/workgroup/secret aren't known at deploy time; the per-invocation session policy (clients._redshift_session_policy) pins all three to the one cluster/workgroup/secret the run actually uses.
  statement {
    sid = "GlueReadOnly"
    actions = [
      "glue:GetDatabase", "glue:GetDatabases",
      "glue:GetTable", "glue:GetTables",
      "glue:GetPartitions", "glue:GetTableVersions",
    ]
    resources = ["*"]
  }

  statement {
    sid = "AthenaSampling"
    actions = [
      "athena:StartQueryExecution", "athena:GetQueryExecution",
      "athena:GetQueryResults", "athena:StopQueryExecution",
    ]
    resources = ["*"]
  }

  # Athena WRITES results to the dedicated results bucket (scoped).
  statement {
    sid       = "AthenaResultsWrite"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.athena_results.arn, "${aws_s3_bucket.athena_results.arn}/*"]
  }

  # Athena READS the table data locations, which can live in ANY bucket a Glue
  # table points at (e.g. the BIRD data bucket) — so read stays broad.
  statement {
    sid       = "TableDataRead"
    actions   = ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["*"]
  }

  # Lake Formation-governed catalogs (var.enable_lakeformation): the query engine
  # calls lakeformation:GetDataAccess on the caller's behalf to obtain short-lived,
  # LF-vended S3 credentials scoped to the governed table's location. GetDataAccess
  # does NOT support resource-level scoping, so it is "*". This grant is necessary
  # but NOT sufficient — the adopter must also GRANT the role LF SELECT/DESCRIBE and
  # register the data location (see docs/LAKE_FORMATION.md); LF then AND's with IAM.
  # NOTE: must ALSO be added to the per-invocation session policy in clients.py
  # (build the session policy sends is intersected with this role), else it's stripped.
  dynamic "statement" {
    for_each = var.enable_lakeformation ? [1] : []
    content {
      sid       = "LakeFormationDataAccess"
      actions   = ["lakeformation:GetDataAccess"]
      resources = ["*"]
    }
  }

  # Amazon Redshift data source (var.enable_redshift): the ceiling for a harvest
  # that reads a Redshift database via the Redshift Data API. This is a SUPERSET
  # of the per-invocation session policy minted in clients._redshift_session_policy
  # (the session policy is intersected with this role, so anything it grants must
  # appear here or it's stripped). redshift-data:* actions are not ARN-scopable, so
  # "*"; the auth grants (GetClusterCredentials / GetCredentials / GetSecretValue)
  # carry cross-target containment and are pinned per-invocation by the session
  # policy from the mapping's self-describing source descriptor. Enable via
  # var.enable_redshift (no deploy-time connection config — see variables.tf).
  dynamic "statement" {
    for_each = var.enable_redshift ? [1] : []
    content {
      sid = "RedshiftDataApi"
      actions = [
        "redshift-data:ExecuteStatement",
        "redshift-data:DescribeStatement",
        "redshift-data:GetStatementResult",
      ]
      resources = ["*"]
    }
  }

  dynamic "statement" {
    for_each = var.enable_redshift ? [1] : []
    content {
      sid = "RedshiftAuth"
      actions = [
        "redshift:GetClusterCredentials",
        "redshift-serverless:GetCredentials",
      ]
      resources = ["*"]
    }
  }

  # Secret-based Redshift auth: read the connection secret a mapping names in its
  # source descriptor. Per-mapping secrets can't be enumerated at deploy time, so
  # the grant is account-wide ("*") — the per-invocation session policy
  # (clients._redshift_session_policy) pins it to the one secret for the run.
  dynamic "statement" {
    for_each = var.enable_redshift ? [1] : []
    content {
      sid       = "RedshiftSecretRead"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = ["*"]
    }
  }
}

# Trust: ONLY the harvest execution role may assume the data role.
data "aws_iam_policy_document" "harvest_data_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.harvest.arn]
    }
  }
}

resource "aws_iam_role" "harvest_data" {
  name               = "${var.name_prefix}-harvest-data"
  assume_role_policy = data.aws_iam_policy_document.harvest_data_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "harvest_data" {
  name   = "harvest-data-policy"
  role   = aws_iam_role.harvest_data.id
  policy = data.aws_iam_policy_document.harvest_data.json
}

# --- Harvest runtime (execution) role -----------------------------------------
# The EXECUTION role (what AgentCore assumes for the microVM) holds only what the
# runtime needs to bootstrap: Bedrock InvokeModel, bundle read/write (via the S3
# Files mount), the registry status write, the S3 Files mount grants, and — the
# key change — sts:AssumeRole on the harvest DATA role above. Glue/Athena/table
# reads were MOVED to the data role and are reached only through the scoped,
# per-invocation assume; a compromised runtime that reads the execution-role creds
# from MMDS can at most re-assume the data role (bounded by its ceiling), not read
# Glue/Athena account-wide directly.

data "aws_iam_policy_document" "harvest" {
  # Down-scope hop: assume the data role per invocation with an inline session
  # policy pinned to the target database + workgroup (clients.build_scoped_session).
  statement {
    sid       = "AssumeDataRole"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.harvest_data.arn]
  }

  statement {
    sid       = "BedrockInvoke"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["*"]
  }

  # Bedrock Mantle (OpenAI-compatible endpoint) — needed when a GPT id is
  # reachable at harvest time (see local.harvest_mantle_enabled: the deploy-time
  # default OR any catalog entry the UI picker can select). Mantle is a SEPARATE
  # IAM namespace from bedrock:* — the bedrock:InvokeModel grant above does NOT
  # cover it. The bearer token provide_token() mints inherits THIS role's
  # identity, so the role itself needs these actions:
  #   - CreateInference: invoke time (missing -> 401 permission_denied at call)
  #   - Get*/List*: model discovery/sync
  # ...scoped to the Mantle "default" project in the Mantle region (independent
  # of var.region; GPT-5.x is only in us-east-2/us-west-2). CallWithBearerToken
  # is the bearer-auth action itself and is not project- or region-scopable, so
  # it takes resources=["*"]. Absent when no GPT model is reachable, so the role
  # stays least-privilege on a Converse-only (Claude) catalog.
  dynamic "statement" {
    for_each = local.harvest_mantle_enabled ? [1] : []
    content {
      sid = "BedrockMantleInvoke"
      actions = [
        "bedrock-mantle:CreateInference",
        "bedrock-mantle:GetInference",
        "bedrock-mantle:GetModel",
        "bedrock-mantle:ListModels",
      ]
      resources = [
        "arn:aws:bedrock-mantle:${var.harvest_mantle_region}:${local.account_id}:project/default",
      ]
    }
  }

  dynamic "statement" {
    for_each = local.harvest_mantle_enabled ? [1] : []
    content {
      sid       = "BedrockMantleBearerToken"
      actions   = ["bedrock-mantle:CallWithBearerToken"]
      resources = ["*"]
    }
  }

  # Code Interpreter data plane — the run_code sandbox for extracting text from
  # binary .context/ docs. Scoped to the one custom interpreter we provision
  # (SANDBOX/network-isolated). Only present when var.enable_code_interpreter, so
  # the harvest role stays least-privilege when the feature is off.
  dynamic "statement" {
    for_each = var.enable_code_interpreter ? [1] : []
    content {
      sid = "CodeInterpreterInvoke"
      actions = [
        "bedrock-agentcore:StartCodeInterpreterSession",
        "bedrock-agentcore:InvokeCodeInterpreter",
        "bedrock-agentcore:StopCodeInterpreterSession",
      ]
      resources = [aws_bedrockagentcore_code_interpreter.harvest[0].code_interpreter_arn]
    }
  }

  statement {
    sid = "BundleBucketReadWrite"
    actions = [
      "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
    ]
    resources = [
      local.d.bundle_bucket_arn,
      "${local.d.bundle_bucket_arn}/*",
    ]
  }

  # The agent reports its own lifecycle back to the registry status row
  # (queued -> running -> complete|failed) so the UI's GET /harvest reflects
  # reality. UpdateItem only (touches status/updated_at/detail); the Control
  # API owns the initial put. Scoped to the single registry table.
  statement {
    sid       = "RegistryStatusWrite"
    actions   = ["dynamodb:UpdateItem"]
    resources = [local.d.registry_table_arn]
  }

  # Annotation-mode re-harvest write-back. When the run finishes, the RUNNER
  # (not the LLM — the agent has zero DynamoDB tools) reconciles the agent's
  # on-mount resolution file into the annotations table: each processed note is
  # flipped to resolved/rejected with the agent's verdict comment and a 7-day
  # expires_at. UpdateItem only; the Control API owns the initial put + the
  # orphan sweep. Scoped to the single annotations table.
  statement {
    sid       = "AnnotationResolveWrite"
    actions   = ["dynamodb:UpdateItem"]
    resources = [local.d.annotations_table_arn]
  }

  # S3 Files mount. Canonical form (AWS "File system configurations" doc): the
  # RESOURCE is the FILE-SYSTEM arn, gated by an ArnEquals condition on
  # s3files:AccessPointArn — NOT the access-point arn as the resource.
  dynamic "statement" {
    for_each = local.harvest_has_fs ? [1] : []
    content {
      sid       = "S3FilesMount"
      actions   = ["s3files:ClientMount", "s3files:ClientWrite", "s3files:GetAccessPoint"]
      resources = [local.harvest_fs_arn]
      condition {
        test     = "ArnEquals"
        variable = "s3files:AccessPointArn"
        values   = [local.harvest_access_point_arn]
      }
    }
  }

  # The CreateAgentRuntime validator probes an (undocumented, evolving) set of
  # s3files read/list/describe actions — and list actions don't support
  # resource-level scoping, so granting them on a specific ARN leaves them denied
  # on "*". Rather than chase each probed action one at a time, grant the entire
  # READ-ONLY s3files family (Get*/List*/Describe*) on "*". This covers any probe
  # while granting NO mutating actions (no Create/Delete/Client*/Put), so it's
  # still least-privilege for a read/validate surface. The actual mount grant
  # (ClientMount/ClientWrite, above) stays tightly scoped with the condition.
  dynamic "statement" {
    for_each = local.harvest_has_fs ? [1] : []
    content {
      sid       = "S3FilesReadOnly"
      actions   = ["s3files:Get*", "s3files:List*", "s3files:Describe*"]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role" "harvest" {
  name               = "${var.name_prefix}-harvest-runtime"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
  tags               = var.tags
}

# Merge the shared baseline (ECR/logs/xray) with the harvest-specific perms into
# ONE policy so it's all in place before CreateAgentRuntime validates the role.
data "aws_iam_policy_document" "harvest_full" {
  source_policy_documents = [
    data.aws_iam_policy_document.agentcore_baseline.json,
    data.aws_iam_policy_document.harvest.json,
  ]
}

resource "aws_iam_role_policy" "harvest" {
  name   = "harvest-policy"
  role   = aws_iam_role.harvest.id
  policy = data.aws_iam_policy_document.harvest_full.json
}

# --- Consumption MCP runtime role --------------------------------------------
# Read the bundle bucket, embed the query (Bedrock), and query S3 Vectors. The
# QueryVectors-with-filter/metadata 403 trap means we MUST also grant GetVectors.

data "aws_iam_policy_document" "consumption" {
  statement {
    sid       = "BundleBucketRead"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [local.d.bundle_bucket_arn, "${local.d.bundle_bucket_arn}/*"]
  }

  statement {
    sid       = "BedrockEmbed"
    actions   = ["bedrock:InvokeModel"]
    resources = ["*"]
  }

  statement {
    sid = "S3VectorsQuery"
    # GetVectors is REQUIRED alongside QueryVectors for filtered / metadata
    # queries, else 403.
    actions   = ["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:GetIndex"]
    resources = [local.d.vector_index_arn, local.d.vector_bucket_arn]
  }

  statement {
    sid       = "RegistryRead"
    actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"]
    resources = [local.d.registry_table_arn]
  }
}

resource "aws_iam_role" "consumption" {
  name               = "${var.name_prefix}-consumption-runtime"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "consumption_full" {
  source_policy_documents = [
    data.aws_iam_policy_document.agentcore_baseline.json,
    data.aws_iam_policy_document.consumption.json,
  ]
}

resource "aws_iam_role_policy" "consumption" {
  name   = "consumption-policy"
  role   = aws_iam_role.consumption.id
  policy = data.aws_iam_policy_document.consumption_full.json
}

# --- Chat agent runtime role -------------------------------------------------
# The chat agent reads the wiki with the SAME read-only reach as consumption
# (bundle read, Bedrock embed for semantic_search, S3 Vectors query, registry
# read — it reuses ConsumptionTools in-process), PLUS: Bedrock InvokeModel for the
# chat LLM (+ conditional Mantle for GPT), and read/write on the two chat tables
# (the DynamoDBSaver checkpoints + the per-user conversation index). It has NO
# Glue/Athena reach (it never touches source data) and NO bundle WRITE (read-only
# over the wiki).
data "aws_iam_policy_document" "chat" {
  # checkov:skip=CKV_AWS_108:Only when var.enable_chat_sql — TableDataRead s3:GetObject must be "*" because a Glue table's storage location can be any bucket; read-only (no Put to source) and off by default.
  # checkov:skip=CKV_AWS_111:Only when var.enable_chat_sql — Athena/S3 write is scoped to the dedicated results bucket; the remaining broad grants are read/list metadata actions that don't support ARN scoping. See the block comment on ChatSqlGlueRead.
  # checkov:skip=CKV_AWS_356:glue list + athena:* are catalog/workgroup-level actions that cannot be pinned to one resource ARN (same residual as harvest_data); the runtime's read-only query guard bounds them.
  statement {
    sid       = "BundleBucketRead"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [local.d.bundle_bucket_arn, "${local.d.bundle_bucket_arn}/*"]
  }

  # Titan embed for semantic_search AND the chat LLM (InvokeModel +
  # WithResponseStream for token streaming). Both are the bedrock:* namespace.
  statement {
    sid       = "BedrockInvoke"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["*"]
  }

  # S3 Vectors query for semantic_search. GetVectors is REQUIRED alongside
  # QueryVectors for filtered/metadata queries, else 403 (same trap as consumption).
  statement {
    sid       = "S3VectorsQuery"
    actions   = ["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:GetIndex"]
    resources = [local.d.vector_index_arn, local.d.vector_bucket_arn]
  }

  # Registry read for list_domains / list_declared_domains.
  statement {
    sid       = "RegistryRead"
    actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"]
    resources = [local.d.registry_table_arn]
  }

  # Conversation memory: the DynamoDBSaver checkpoint table (full item R/W —
  # get/put/query/delete_thread) + the per-user conversation INDEX table (the
  # runtime creates/touches rows; the Control API also reads/deletes them).
  statement {
    sid = "ChatTablesReadWrite"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
    ]
    resources = [local.d.chat_checkpoints_table_arn, local.d.chat_table_arn]
  }

  # Bedrock Mantle (OpenAI-compatible) — needed when a GPT id is reachable at chat
  # time (deploy-time default OR any catalog entry the picker offers). Separate IAM
  # namespace from bedrock:*; the bearer token provide_token() mints inherits this
  # role's identity. Scoped to the Mantle "default" project in chat_mantle_region;
  # CallWithBearerToken is not project/region-scopable so it takes "*". Absent when
  # no GPT model is reachable, keeping the role least-privilege on a Claude catalog.
  dynamic "statement" {
    for_each = local.chat_mantle_enabled ? [1] : []
    content {
      sid = "BedrockMantleInvoke"
      actions = [
        "bedrock-mantle:CreateInference",
        "bedrock-mantle:GetInference",
        "bedrock-mantle:GetModel",
        "bedrock-mantle:ListModels",
      ]
      resources = [
        "arn:aws:bedrock-mantle:${var.chat_mantle_region}:${local.account_id}:project/default",
      ]
    }
  }

  dynamic "statement" {
    for_each = local.chat_mantle_enabled ? [1] : []
    content {
      sid       = "BedrockMantleBearerToken"
      actions   = ["bedrock-mantle:CallWithBearerToken"]
      resources = ["*"]
    }
  }

  # Optional read-only SQL (var.enable_chat_sql): catalog-wide Glue metadata +
  # Athena query READ, plus Athena results-bucket write. This is the ONE grant
  # that lets the browser-facing chat runtime touch SOURCE DATA, so it's OFF by
  # default and read-only by construction — NO glue:*Table (write), NO source-data
  # PutObject. Unlike harvest, chat is NOT pinned to one database per invocation
  # (no scoped STS hop), so these reads are catalog-wide; the runtime's query guard
  # (SELECT/WITH/… only) + this write-free grant are the read-only boundary.
  # Actions that can't be ARN-scoped (Glue list, athena:* workgroup-level, source
  # TableDataRead across arbitrary buckets) stay at "*", same as harvest_data.
  dynamic "statement" {
    for_each = var.enable_chat_sql ? [1] : []
    content {
      sid = "ChatSqlGlueRead"
      actions = [
        "glue:GetDatabase", "glue:GetDatabases",
        "glue:GetTable", "glue:GetTables",
        "glue:GetPartitions", "glue:GetTableVersions",
      ]
      resources = ["*"]
    }
  }

  dynamic "statement" {
    for_each = var.enable_chat_sql ? [1] : []
    content {
      sid = "ChatSqlAthenaQuery"
      actions = [
        "athena:StartQueryExecution", "athena:GetQueryExecution",
        "athena:GetQueryResults", "athena:StopQueryExecution",
      ]
      resources = ["*"]
    }
  }

  # Athena WRITES query results to the dedicated results bucket (scoped) — this is
  # the only write the SQL grant carries, and it's to a scratch bucket, not source.
  dynamic "statement" {
    for_each = var.enable_chat_sql ? [1] : []
    content {
      sid       = "ChatSqlAthenaResultsWrite"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
      resources = [aws_s3_bucket.athena_results.arn, "${aws_s3_bucket.athena_results.arn}/*"]
    }
  }

  # Athena READS the table data locations, which can live in ANY bucket a Glue
  # table points at — so source read stays broad (read-only; no Put here).
  dynamic "statement" {
    for_each = var.enable_chat_sql ? [1] : []
    content {
      sid       = "ChatSqlTableDataRead"
      actions   = ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"]
      resources = ["*"]
    }
  }

  # Lake Formation-governed catalogs: the query engine calls GetDataAccess to get
  # LF-vended, short-lived S3 creds for governed table data. Mirrors harvest_data;
  # only added when BOTH SQL and LF are enabled. GetDataAccess can't be ARN-scoped.
  dynamic "statement" {
    for_each = var.enable_chat_sql && var.enable_lakeformation ? [1] : []
    content {
      sid       = "ChatSqlLakeFormationDataAccess"
      actions   = ["lakeformation:GetDataAccess"]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role" "chat" {
  name               = "${var.name_prefix}-chat-runtime"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "chat_full" {
  source_policy_documents = [
    data.aws_iam_policy_document.agentcore_baseline.json,
    data.aws_iam_policy_document.chat.json,
  ]
}

resource "aws_iam_role_policy" "chat" {
  name   = "chat-policy"
  role   = aws_iam_role.chat.id
  policy = data.aws_iam_policy_document.chat_full.json
}

# IAM is eventually consistent: after PutRolePolicy, a CreateAgentRuntime call
# issued seconds later can validate against a STALE snapshot of the role and
# report a permission as missing (the error oscillates between s3files actions
# across applies — the tell-tale sign of a propagation race, not a wrong policy).
# Wait for the policy changes to propagate before either runtime is created.
resource "time_sleep" "iam_propagation" {
  triggers = {
    harvest_policy      = aws_iam_role_policy.harvest.policy
    harvest_data_policy = aws_iam_role_policy.harvest_data.policy
    consumption_policy  = aws_iam_role_policy.consumption.policy
    chat_policy         = aws_iam_role_policy.chat.policy
  }
  create_duration = "30s"
}
