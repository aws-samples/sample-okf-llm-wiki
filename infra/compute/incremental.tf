# Incremental path: Glue "Table State Change" -> EventBridge -> SQS ->
# orchestrator Lambda -> scoped re-harvest. Plus a nightly reconcile schedule
# that re-scans Glue versions to catch missed events.

resource "aws_sqs_queue" "incremental_dlq" {
  name                    = "${var.name_prefix}-incremental-dlq"
  sqs_managed_sse_enabled = true # SSE-SQS at rest (CKV_AWS_27)
  tags                    = var.tags
}

resource "aws_sqs_queue" "incremental" {
  name = "${var.name_prefix}-incremental"
  # >= 6x the consuming Lambda's timeout (the reindex.tf invariant).
  visibility_timeout_seconds = 360
  sqs_managed_sse_enabled    = true # SSE-SQS at rest (CKV_AWS_27)
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.incremental_dlq.arn
    maxReceiveCount     = 5
  })
  tags = var.tags
}

data "aws_iam_policy_document" "incremental_sqs" {
  statement {
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.incremental.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    # Every rule that may deliver to this queue must be listed here. A rule
    # missing from this condition is DENIED SILENTLY — EventBridge drops the
    # delivery and neither side logs an error, so a policy_rebuild path missing
    # from this list would look like "the event never fired".
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values = concat(
        [aws_cloudwatch_event_rule.glue_table_change.arn],
        local.ar_enabled ? [aws_cloudwatch_event_rule.policy_rebuild[0].arn] : [],
      )
    }
  }
}

resource "aws_sqs_queue_policy" "incremental" {
  queue_url = aws_sqs_queue.incremental.id
  policy    = data.aws_iam_policy_document.incremental_sqs.json
}

# The exact Glue table-change signal (source aws.glue, this detail-type).
resource "aws_cloudwatch_event_rule" "glue_table_change" {
  name = "${var.name_prefix}-glue-table-change"
  event_pattern = jsonencode({
    source        = ["aws.glue"]
    "detail-type" = ["Glue Data Catalog Table State Change"]
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_target" "glue_table_change" {
  rule = aws_cloudwatch_event_rule.glue_table_change.name
  arn  = aws_sqs_queue.incremental.arn
}

# --- policy_rebuild events (AR policy freshness accelerator) -------------------
# A custom-source event that rides the SAME EventBridge -> SQS -> incremental
# handler path the Glue change events use: same rule shape, same queue, same
# handler host, one new detail-type. Published by (a) the Control API's
# repromote success path and (b) the chat runtime's policy_check when it lazily
# discovers a stale policy. Duplicate events are harmless — the build start is a
# conditional flip of ar_build_status to "building" on the DATASET# row, so N
# events collapse to one build.
#
# `source` is our OWN custom source ("okf.policy" — okf_core.policy_rebuild),
# NOT an aws.* service source. Match on source AND detail-type so a future
# okf.* event can't be swallowed by this rule.
resource "aws_cloudwatch_event_rule" "policy_rebuild" {
  count = local.ar_enabled ? 1 : 0
  name  = "${var.name_prefix}-policy-rebuild"
  event_pattern = jsonencode({
    source        = ["okf.policy"]
    "detail-type" = ["policy_rebuild"]
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_target" "policy_rebuild" {
  count = local.ar_enabled ? 1 : 0
  rule  = aws_cloudwatch_event_rule.policy_rebuild[0].name
  arn   = aws_sqs_queue.incremental.arn
}

module "incremental_fn" {
  source      = "../modules/lambda"
  name        = "${var.name_prefix}-incremental"
  handler     = "incremental.handler.lambda_handler"
  source_dir  = "${local.build_root}/incremental"
  policy_json = data.aws_iam_policy_document.incremental.json
  # A policy_rebuild event makes deterministic decisions (fingerprint check,
  # stall reaping) and at most fire-and-forgets an authoring run to the
  # harvest runtime — no model calls, no Bedrock work here.
  timeout     = 60
  memory_size = 512
  environment = merge(local.common_env, {
    OKF_HARVEST_RUNTIME_ARN = try(aws_bedrockagentcore_agent_runtime.harvest[0].agent_runtime_arn, "")
    # Policy rebuild authority switch (inert while enable_policy_build is
    # off). No model config: the authoring agent runs on the HARVEST runtime.
    OKF_POLICY_BUILD_ENABLED = tostring(local.ar_build_enabled)
  })
  tags = var.tags
}

resource "aws_lambda_event_source_mapping" "incremental" {
  event_source_arn        = aws_sqs_queue.incremental.arn
  function_name           = module.incremental_fn.function_arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
}

# --- Nightly reconcile (OPT-IN) ----------------------------------------------
# Best-effort Glue events can be missed; re-scan versions on a schedule to catch
# drift. OFF by default (var.enable_reconcile) since it invokes the harvest
# runtime — Bedrock/Athena cost — on a timer with no human in the loop. The
# event-driven incremental path is unaffected and always runs.

module "reconcile_fn" {
  count       = var.enable_reconcile ? 1 : 0
  source      = "../modules/lambda"
  name        = "${var.name_prefix}-reconcile"
  handler     = "incremental.reconcile.reconcile_handler"
  source_dir  = "${local.build_root}/incremental"
  policy_json = data.aws_iam_policy_document.incremental.json
  timeout     = 300
  memory_size = 512
  environment = merge(local.common_env, {
    OKF_HARVEST_RUNTIME_ARN = try(aws_bedrockagentcore_agent_runtime.harvest[0].agent_runtime_arn, "")
    # The nightly pass reaps stalled authoring runs and dispatches
    # re-authoring to the harvest runtime (inert while enable_policy_build
    # is off).
    OKF_POLICY_BUILD_ENABLED = tostring(local.ar_build_enabled)
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "reconcile_nightly" {
  count               = var.enable_reconcile ? 1 : 0
  name                = "${var.name_prefix}-reconcile-nightly"
  schedule_expression = var.reconcile_schedule
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "reconcile_nightly" {
  count = var.enable_reconcile ? 1 : 0
  rule  = aws_cloudwatch_event_rule.reconcile_nightly[0].name
  arn   = module.reconcile_fn[0].function_arn
}

resource "aws_lambda_permission" "reconcile_events" {
  count         = var.enable_reconcile ? 1 : 0
  statement_id  = "AllowEventBridgeReconcile"
  action        = "lambda:InvokeFunction"
  function_name = module.reconcile_fn[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconcile_nightly[0].arn
}
