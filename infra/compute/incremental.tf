# Incremental path: Glue "Table State Change" -> EventBridge -> SQS ->
# orchestrator Lambda -> scoped re-harvest. Plus a nightly reconcile schedule
# that re-scans Glue versions to catch missed events.

resource "aws_sqs_queue" "incremental_dlq" {
  name                    = "${var.name_prefix}-incremental-dlq"
  sqs_managed_sse_enabled = true # SSE-SQS at rest (CKV_AWS_27)
  tags                    = var.tags
}

resource "aws_sqs_queue" "incremental" {
  name                       = "${var.name_prefix}-incremental"
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
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.glue_table_change.arn]
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

module "incremental_fn" {
  source      = "../modules/lambda"
  name        = "${var.name_prefix}-incremental"
  handler     = "incremental.handler.lambda_handler"
  source_dir  = "${local.build_root}/incremental"
  policy_json = data.aws_iam_policy_document.incremental.json
  timeout     = 60
  memory_size = 512
  environment = merge(local.common_env, {
    OKF_HARVEST_RUNTIME_ARN = try(aws_bedrockagentcore_agent_runtime.harvest[0].agent_runtime_arn, "")
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
