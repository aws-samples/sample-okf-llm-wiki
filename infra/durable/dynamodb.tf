# App state. Small, on-demand, keeps the agent + workers stateless.
# Item shapes are documented in docs/CONVENTIONS.md and are load-bearing.

# Customer-managed CMK for the tables (CKV_AWS_119). Gated by
# var.dynamodb_customer_managed_cmk (default on); count keeps the ref valid when off.
#
# The key policy has TWO statements, mirroring the AWS-managed aws/dynamodb key so
# access stays transparent to the existing table-consumer roles (harvest,
# control-api, consumption, reindex, incremental) with NO kms:* grant added to
# them — important because those roles live in the compute stack and durable can't
# see their ARNs:
#   1. EnableIAMUserPermissions — account root can manage the key + delegate to IAM.
#   2. AllowDynamoDBService — any principal IN THIS ACCOUNT may use the key for
#      crypto ops, but ONLY when the request is made THROUGH DynamoDB
#      (kms:ViaService = dynamodb.<region>) and originates in this account
#      (kms:CallerAccount). This is exactly how the AWS-managed key grants
#      transparent access; a role that can call dynamodb:* on the table can use
#      the key via the service, and cannot use it for anything else.
data "aws_iam_policy_document" "dynamodb_cmk" {
  count = var.dynamodb_customer_managed_cmk ? 1 : 0

  # checkov:skip=CKV_AWS_109:This is a KMS KEY policy, not an identity policy — its "resources = *" is self-referential (it always means "this key"); a key resource ARN cannot appear in its own key policy. The account-root delegation to IAM is the AWS-recommended default key policy.
  # checkov:skip=CKV_AWS_111:Same — key-policy resource is inherently "*" (this key). Actual write access is authorized by the consuming roles' IAM policies (dynamodb:* on the table) AND'd with the ViaService/CallerAccount conditions below, not by this document alone.
  # checkov:skip=CKV_AWS_356:A key policy cannot self-reference its own key ARN, so "*" is required and does not widen scope beyond this single key. The AllowDynamoDBService statement is further constrained by kms:ViaService + kms:CallerAccount conditions.
  statement {
    sid       = "EnableIAMUserPermissions"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid    = "AllowDynamoDBService"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["*"] # any principal in THIS account, gated by the conditions below
    }
    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "dynamodb" {
  count                   = var.dynamodb_customer_managed_cmk ? 1 : 0
  description             = "${var.name_prefix} DynamoDB tables (registry + freshness + annotations) at-rest CMK"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.dynamodb_cmk[0].json
  tags                    = var.tags
}

resource "aws_kms_alias" "dynamodb" {
  count         = var.dynamodb_customer_managed_cmk ? 1 : 0
  name          = "alias/${var.name_prefix}-dynamodb"
  target_key_id = aws_kms_key.dynamodb[0].key_id
}

locals {
  # CMK arn when enabled, else null -> DynamoDB uses the AWS-managed aws/dynamodb key.
  dynamodb_kms_key_arn = var.dynamodb_customer_managed_cmk ? aws_kms_key.dynamodb[0].arn : null
}

# Domain registry (domain -> datasets) + harvest status.
resource "aws_dynamodb_table" "registry" {
  name         = "${var.name_prefix}-registry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  # Encryption at rest with a customer-managed CMK (CKV_AWS_119) when enabled,
  # else the AWS-managed key. Point-in-time recovery (CKV_AWS_28) so the registry
  # — domain mappings, harvest leases, CRED# metadata — can be restored after an
  # accidental/malicious write.
  server_side_encryption {
    enabled     = true
    kms_key_arn = local.dynamodb_kms_key_arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = var.tags
}

# Freshness: reindex sequencer dedup + per-table Glue version tracking.
resource "aws_dynamodb_table" "freshness" {
  name         = "${var.name_prefix}-freshness"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.dynamodb_kms_key_arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = var.tags
}

# Wiki annotations: user-scoped feedback on concept docs, awaiting an
# annotation-mode re-harvest. Isolation is STRUCTURAL — the partition key embeds
# the author's Cognito subject (pk = "ANNO#<domain>#<dataset>#<user_sub>"), so a
# user's Query can only ever read their OWN annotations; there is no cross-user
# read path. Item shapes are documented in docs/CONVENTIONS.md and are load-bearing.
#
# TTL is on `expires_at`, which is set ONLY when an annotation reaches a terminal
# state (resolved / rejected / orphaned) — an OPEN annotation carries no
# expires_at and never expires. Terminal annotations linger 7 days as history,
# then DynamoDB reaps them. A dedicated table (not registry/freshness) keeps this
# sweep OFF the durable rows: a stray expires_at can only ever delete an
# annotation, never a domain mapping or a Glue-version marker.
resource "aws_dynamodb_table" "annotations" {
  name         = "${var.name_prefix}-annotations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.dynamodb_kms_key_arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = var.tags
}

# Chat-agent conversation checkpoints. Owned + written by the LangGraph
# `DynamoDBSaver` (langgraph-checkpoint-aws), NOT by our code — so the KEY SCHEMA
# is DICTATED BY THAT LIBRARY, not chosen here: partition key `PK` + sort key
# `SK`, both String, UPPERCASE (verified against the installed package source).
# One table holds both checkpoints and pending writes, distinguished by PK prefix
# (`CHECKPOINT_<thread_id>` vs `WRITES_<thread_id>#<ns>#<ckpt_id>`) and queried
# with begins_with(SK, …); there is NO GSI. Per-user isolation is STRUCTURAL: the
# chat runtime namespaces the thread id with the caller's Cognito sub
# (`<sub>:<thread_id>`) before it reaches the saver, so one user's checkpoints can
# never be read under another user's thread id (see services/chat/server.py).
#
# TTL: the saver writes an epoch-seconds attribute named `ttl` (lowercase) ONLY
# when constructed with ttl_seconds (OKF_CHAT_CHECKPOINT_TTL_SECONDS). Enable TTL
# on `ttl` so expired conversations are reaped; a checkpoint with no `ttl` never
# expires. A dedicated table (not registry) keeps this sweep off the durable rows.
resource "aws_dynamodb_table" "chat_checkpoints" {
  name         = "${var.name_prefix}-chat-checkpoints"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.dynamodb_kms_key_arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = var.tags
}

# Chat conversation INDEX: the per-user list of conversations the UI shows
# (title, model, effort, dataset scope, timestamps). The DynamoDBSaver table is
# keyed by thread id and can't answer "list user X's conversations", so this
# small index does. Isolation is STRUCTURAL, mirroring `annotations`: the
# partition key embeds the author's Cognito sub (pk = "CHAT#<user_sub>"), so a
# user's Query can only ever read their OWN threads — no cross-user read path.
# The chat runtime writes rows (create-on-first-run, touch on each turn); the
# Control API reads/renames/deletes them for the UI. TTL on `expires_at` (set
# only when a conversation is deleted/expired) reaps terminal rows; an active
# conversation carries no expires_at and never expires.
resource "aws_dynamodb_table" "chat" {
  name         = "${var.name_prefix}-chat"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.dynamodb_kms_key_arn
  }
  point_in_time_recovery {
    enabled = true
  }

  tags = var.tags
}
