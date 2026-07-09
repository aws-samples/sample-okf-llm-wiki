# Freshness pipeline: bundle-bucket .md events -> EventBridge -> SQS -> reindex
# Lambda -> Put/DeleteVectors. The SQS queue smooths bursts against Bedrock's
# per-minute embedding throttle.

resource "aws_sqs_queue" "reindex_dlq" {
  name                    = "${var.name_prefix}-reindex-dlq"
  sqs_managed_sse_enabled = true # SSE-SQS at rest (CKV_AWS_27)
  tags                    = var.tags
}

resource "aws_sqs_queue" "reindex" {
  name                       = "${var.name_prefix}-reindex"
  visibility_timeout_seconds = 360  # >= 6x the Lambda timeout
  sqs_managed_sse_enabled    = true # SSE-SQS at rest (CKV_AWS_27)
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.reindex_dlq.arn
    maxReceiveCount     = 5
  })
  tags = var.tags
}

# Allow EventBridge to deliver to the queue.
data "aws_iam_policy_document" "reindex_sqs" {
  statement {
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.reindex.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.reindex.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "reindex" {
  queue_url = aws_sqs_queue.reindex.id
  policy    = data.aws_iam_policy_document.reindex_sqs.json
}

# Match .md Object Created/Deleted on the bundle bucket. S3 EventBridge is
# enabled on the bucket in the durable stack (all events -> default bus); we
# filter here to .md creates/deletes under okf/.
resource "aws_cloudwatch_event_rule" "reindex" {
  name = "${var.name_prefix}-bundle-md-changes"
  # NOTE: an ARRAY of content filters under object.key is combined with OR, not
  # AND — [{prefix="okf/"}, {suffix=".md"}] would match "okf/*" OR "*.md". To get
  # prefix-AND-suffix (only okf/**/*.md) use a single `wildcard` filter. The
  # reindex Lambda re-filters via parse_bundle_key as defense-in-depth, but this
  # keeps the rule from enqueuing no-op events (markers, .context, non-md).
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created", "Object Deleted"]
    detail = {
      bucket = { name = [local.d.bundle_bucket] }
      object = { key = [{ wildcard = "okf/*.md" }] }
    }
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_target" "reindex" {
  rule = aws_cloudwatch_event_rule.reindex.name
  arn  = aws_sqs_queue.reindex.arn
}

module "reindex_fn" {
  source      = "../modules/lambda"
  name        = "${var.name_prefix}-reindex"
  handler     = "reindex.handler.lambda_handler"
  source_dir  = "${local.build_root}/reindex"
  policy_json = data.aws_iam_policy_document.reindex.json
  timeout     = 60
  memory_size = 512
  environment = local.common_env
  tags        = var.tags
}

resource "aws_lambda_event_source_mapping" "reindex" {
  event_source_arn                   = aws_sqs_queue.reindex.arn
  function_name                      = module.reindex_fn.function_arn
  batch_size                         = 10
  function_response_types            = ["ReportBatchItemFailures"]
  maximum_batching_window_in_seconds = 5

  scaling_config {
    maximum_concurrency = 5 # throttle Bedrock embedding pressure
  }
}
