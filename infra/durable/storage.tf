data "aws_caller_identity" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  bundle_name = var.bundle_bucket_name != "" ? var.bundle_bucket_name : "${var.name_prefix}-bundles-${local.account_id}"
  vector_name = var.vector_bucket_name != "" ? var.vector_bucket_name : "${var.name_prefix}-vectors-${local.account_id}"
}

# --- S3 bundle bucket: the system of record (OKF markdown) -------------------

resource "aws_s3_bucket" "bundles" {
  bucket = local.bundle_name
  tags   = var.tags
}

# Versioning so a bad harvest/publish can be rolled back — the bundle bucket is
# the durable truth; the vector index is always rebuildable from it.
resource "aws_s3_bucket_versioning" "bundles" {
  bucket = aws_s3_bucket.bundles.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "bundles" {
  bucket                  = aws_s3_bucket.bundles.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bundles" {
  bucket = aws_s3_bucket.bundles.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

# Route ALL bundle-bucket S3 events to the default EventBridge bus. Enabling this
# sends every event type; the reindex EventBridge rule (compute stack) filters to
# .md Object Created/Deleted. Only ONE notification config is allowed per bucket,
# so the EventBridge path is used (independent stacks can't clobber it).
resource "aws_s3_bucket_notification" "bundles" {
  bucket      = aws_s3_bucket.bundles.id
  eventbridge = true
}

# --- Bundle-write restriction (threat #26) -----------------------------------
# OPT-IN (var.restrict_bundle_writers, default OFF). Denies PutObject/DeleteObject
# under the authored prefix okf/* to any principal NOT in var.bundle_writer_role_arns
# so a stray/compromised role can't drop a crafted markdown file that the reindex
# pipeline would then embed (index poisoning). Block Public Access already stops
# anonymous writes; this narrows the AUTHENTICATED writer set to the harvest +
# control-api roles.
#
# DEFAULT OFF ON PURPOSE: bundle writes go THROUGH the S3 Files mount, and without
# CloudTrail S3 data events the effective PutObject principal (harvest role vs the
# s3files service principal) can't be confirmed. A wrong allow-list here Denies the
# LIVE harvest's own writes. Validate the principal (see var docs), populate
# var.bundle_writer_role_arns, THEN set var.restrict_bundle_writers = true.
#
# NotPrincipal + Deny is used (not Allow) so it layers on top of the roles' own
# identity policies without having to re-grant them here. aws:PrincipalArn in the
# condition keeps the mount's service-principal writes working if that is the
# actual writer and its role is listed.
data "aws_iam_policy_document" "bundles_restrict" {
  count = var.restrict_bundle_writers ? 1 : 0
  statement {
    sid       = "DenyBundleWritesExceptAuthors"
    effect    = "Deny"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.bundles.arn}/okf/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotLike"
      variable = "aws:PrincipalArn"
      values   = var.bundle_writer_role_arns
    }
  }
}

resource "aws_s3_bucket_policy" "bundles" {
  count  = var.restrict_bundle_writers ? 1 : 0
  bucket = aws_s3_bucket.bundles.id
  policy = data.aws_iam_policy_document.bundles_restrict[0].json
}

# --- S3 Vectors: the derived semantic index (immutable params) ---------------

resource "aws_s3vectors_vector_bucket" "vectors" {
  vector_bucket_name = local.vector_name
  tags               = var.tags
}

# dimension / distance_metric / data_type / metadata_configuration are IMMUTABLE
# (every one forces a new resource): a change means -replace and a full re-embed.
# Frozen at 512 / cosine / float32 with the three non-filterable keys.
resource "aws_s3vectors_index" "concepts" {
  vector_bucket_name = aws_s3vectors_vector_bucket.vectors.vector_bucket_name
  index_name         = var.vector_index_name
  data_type          = "float32"
  dimension          = 512
  distance_metric    = "cosine"

  metadata_configuration {
    non_filterable_metadata_keys = ["title", "description", "s3_key"]
  }
}
