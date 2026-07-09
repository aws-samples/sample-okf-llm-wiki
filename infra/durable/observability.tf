# CloudWatch Transaction Search — the account+region-wide prerequisite that
# makes AgentCore trace/span data (LLM calls, tool calls, sub-agent fan-out)
# searchable in the CloudWatch "GenAI Observability" console. Without it, even
# correctly-emitted OTEL spans are never indexed into the aws/spans log group.
#
# Three pieces, straight from the AWS "Enable transaction search" +
# AgentCore Observability docs (and the AWS::Logs::ResourcePolicy /
# AWS::XRay::TransactionSearchConfig CloudFormation reference):
#   1. a CloudWatch Logs resource policy letting the X-Ray service PutLogEvents
#      into aws/spans + the application-signals data group;
#   2. route X-Ray trace-segment ingestion to CloudWatch Logs;
#   3. an indexing rule setting the sampled-summary percentage.
#
# Lives in the DURABLE stack: this is long-lived, account-global config, not
# something a routine compute redeploy should churn. NOTE (documented AWS
# behavior): removing aws_xray_trace_segment_destination / aws_xray_indexing_rule
# from Terraform does NOT revert the setting in AWS — destroy is a no-op for them.

data "aws_partition" "current" {}

locals {
  ts_enabled = var.enable_transaction_search ? 1 : 0

  # Log groups X-Ray writes indexed spans into. aws/spans is the Transaction
  # Search span store; the application-signals group backs Application Signals.
  ts_span_log_group_arn   = "arn:${data.aws_partition.current.partition}:logs:${var.region}:${local.account_id}:log-group:aws/spans:*"
  ts_appsig_log_group_arn = "arn:${data.aws_partition.current.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/application-signals/data:*"
}

# 1. Resource policy: allow the X-Ray service principal to ingest spans into the
#    CloudWatch Logs span groups, with the canonical SourceArn/SourceAccount
#    confused-deputy guards.
data "aws_iam_policy_document" "transaction_search" {
  statement {
    sid       = "TransactionSearchXRayAccess"
    effect    = "Allow"
    actions   = ["logs:PutLogEvents"]
    resources = [local.ts_span_log_group_arn, local.ts_appsig_log_group_arn]

    principals {
      type        = "Service"
      identifiers = ["xray.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:xray:${var.region}:${local.account_id}:*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "transaction_search" {
  count           = local.ts_enabled
  policy_name     = "${var.name_prefix}-TransactionSearchAccess"
  policy_document = data.aws_iam_policy_document.transaction_search.json
}

# 2. Route trace segments to CloudWatch Logs (the mode Transaction Search needs).
resource "aws_xray_trace_segment_destination" "transaction_search" {
  count       = local.ts_enabled
  destination = "CloudWatchLogs"

  # The resource policy must exist first, else X-Ray can't write to aws/spans.
  depends_on = [aws_cloudwatch_log_resource_policy.transaction_search]
}

# 3. Index a percentage of spans as trace summaries (cost vs. search density).
resource "aws_xray_indexing_rule" "transaction_search" {
  count = local.ts_enabled
  name  = "Default"

  rule {
    probabilistic {
      desired_sampling_percentage = var.transaction_search_indexing_percentage
    }
  }

  depends_on = [aws_xray_trace_segment_destination.transaction_search]
}
