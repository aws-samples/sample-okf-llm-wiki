# Dedicated Athena query-results bucket for the harvest agent's run_sql /
# sample_rows. Without an OutputLocation (and with the default workgroup not
# enforcing one) StartQueryExecution fails — so we create a results bucket and
# default OKF_ATHENA_OUTPUT to it. General across all harvested datasets, not
# tied to any one source bucket.

resource "aws_s3_bucket" "athena_results" {
  bucket        = "${var.name_prefix}-athena-results-${local.account_id}"
  force_destroy = true # transient query results; safe to empty on destroy
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "athena_results" {
  bucket                  = aws_s3_bucket.athena_results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Expire results after 7 days — they're disposable.
resource "aws_s3_bucket_lifecycle_configuration" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id
  rule {
    id     = "expire-results"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
  }
}

locals {
  # Where Athena writes results: the caller-provided location, or the dedicated
  # results bucket created here.
  athena_output = var.athena_output_location != "" ? var.athena_output_location : "s3://${aws_s3_bucket.athena_results.id}/harvest/"

  # Computation EXECUTION on the Control API Lambda (the Run modal, canvas
  # tile runs, dial column-domains): on when the operator enabled Attested
  # Computations explicitly — or IMPLICITLY by deploying the canvas runtime.
  # The CI live-validates and saves dashboards whose tiles execute through
  # this Lambda (canvas_runtime.tf documents CI execution as independent of
  # the flag); without this, a canvas deploy with the flag off shipped
  # dashboards whose every tile returned "execution is not enabled".
  computations_execution = var.enable_attested_computations || var.canvas_image_uri != ""
}
