# Amazon S3 Files: the shared, runtime-scoped filesystem the harvest agent
# mounts at /mnt/data, rooted at okf/. Fully managed in Terraform via the native
# aws_s3files_* resources (file system + mount target + access point), so there
# are NO click-ops steps for the mount.
#
# The file system exposes the bundle bucket over NFSv4.2/TLS; the access point's
# root_directory pins the mount at the `okf/` prefix; the mount target places it
# in the harvest runtime's VPC subnet. Created only when VPC subnets are
# provided (a mount target requires a subnet); when absent the harvest runtime
# comes up PUBLIC with no mount (fine for validate/plan and non-harvest testing).

# S3 Files is enabled whenever we have subnets to place a mount target in —
# either the auto-created VPC (vpc.tf) or user-supplied subnets.
locals {
  s3files_enabled = length(local.effective_subnet_ids) > 0
}

# --- Service role S3 Files assumes to reach the bundle bucket ---------------
# S3 Files reuses the EFS service principal (elasticfilesystem.amazonaws.com).

data "aws_iam_policy_document" "s3files_assume" {
  count = local.s3files_enabled ? 1 : 0
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["elasticfilesystem.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:s3files:${var.region}:${local.account_id}:file-system/*"]
    }
  }
}

data "aws_iam_policy_document" "s3files_bucket" {
  count = local.s3files_enabled ? 1 : 0

  statement {
    sid       = "S3BucketPermissions"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:ListBucketVersions"]
    resources = [local.d.bundle_bucket_arn]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [local.account_id]
    }
  }
  statement {
    sid    = "S3ObjectPermissions"
    effect = "Allow"
    actions = [
      "s3:AbortMultipartUpload", "s3:DeleteObject*",
      "s3:GetObject*", "s3:List*", "s3:PutObject*",
    ]
    resources = ["${local.d.bundle_bucket_arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "s3files" {
  count              = local.s3files_enabled ? 1 : 0
  name               = "${var.name_prefix}-s3files"
  assume_role_policy = data.aws_iam_policy_document.s3files_assume[0].json
  tags               = var.tags
}

resource "aws_iam_role_policy" "s3files" {
  count  = local.s3files_enabled ? 1 : 0
  name   = "s3files-bucket-access"
  role   = aws_iam_role.s3files[0].id
  policy = data.aws_iam_policy_document.s3files_bucket[0].json
}

# --- The S3 Files file system over the bundle bucket ------------------------

resource "aws_s3files_file_system" "bundle" {
  count    = local.s3files_enabled ? 1 : 0
  bucket   = local.d.bundle_bucket_arn
  role_arn = aws_iam_role.s3files[0].arn
  # The bundle bucket is versioned + not empty over its life — acknowledge the
  # "existing bucket" warning so the file system provisions non-interactively.
  accept_bucket_warning = true
  tags                  = var.tags
}

# One mount target per subnet (the runtime's private subnets). Keyed by a static
# index (local.mount_target_subnets) so for_each is plan-safe even when the
# subnet ids come from the auto-created VPC (apply-time values).
resource "aws_s3files_mount_target" "bundle" {
  for_each        = local.mount_target_subnets
  file_system_id  = aws_s3files_file_system.bundle[0].id
  subnet_id       = each.value
  security_groups = local.effective_sg_ids
}

# NOTE: do NOT pre-create an `okf/` marker object here. S3 Files only applies
# the access point's `creation_permissions` (below) when it AUTO-CREATES a
# MISSING root directory at mount time. A pre-existing `okf/` object makes the
# root exist first, so creation_permissions are skipped and the root ends up
# owned by root/default — then the access point (which forces all ops to uid
# 1000) can't write into it ("Permission denied" creating subdirs). Leaving the
# prefix absent lets S3 Files create /okf owned by 1000:1000 on first mount.

# Access point rooted at okf/ — this is what the runtime mounts at /mnt/data, so
# each session sees the OKF tree directly (per-dataset containment is applied
# in-process by the deepagents FilesystemBackend).
resource "aws_s3files_access_point" "okf" {
  count          = local.s3files_enabled ? 1 : 0
  file_system_id = aws_s3files_file_system.bundle[0].id

  root_directory {
    path = "/okf"
    # Auto-create the root dir on first mount if it's missing, owned by the
    # mount's POSIX identity — WITHOUT this, S3 Files refuses to create a
    # non-existent root and the NFS mount is denied.
    creation_permissions {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  # POSIX identity the mount presents; harmless default for a single-tenant mount.
  posix_user {
    uid = 1000
    gid = 1000
  }

  tags       = var.tags
  depends_on = [aws_s3files_mount_target.bundle]
}

# The ARN the harvest runtime's filesystem_configuration consumes. Prefer the
# TF-created access point; fall back to a manually-supplied ARN if one is set.
locals {
  # PLAN-KNOWN predicate: will the harvest runtime get an S3 Files mount? Depends
  # only on subnet counts + a var (never an apply-time attribute), so it's safe
  # in count/for_each/dynamic-block conditions.
  harvest_has_fs = local.s3files_enabled || var.s3_files_access_point_arn != ""

  # The ARN mounted by the runtime. Apply-time when s3files is TF-managed; only
  # ever used as an attribute VALUE, never as a for_each/count key.
  harvest_access_point_arn = local.s3files_enabled ? aws_s3files_access_point.okf[0].arn : var.s3_files_access_point_arn

  # The parent file-system ARN — the RESOURCE for the s3files mount statement
  # (per the canonical policy: file-system arn + AccessPointArn condition). For a
  # TF-managed FS we have it directly; for a manually-supplied access-point ARN,
  # strip the "/access-point/<id>" suffix to get the file-system arn.
  harvest_fs_arn = local.s3files_enabled ? aws_s3files_file_system.bundle[0].arn : replace(var.s3_files_access_point_arn, "/\\/access-point\\/.*/", "")
}
