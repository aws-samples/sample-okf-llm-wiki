# The Attested-Computations EXECUTION ceiling, defined ONCE.
#
# Consumers merge these via source_policy_documents, each behind its own
# gate: the consumption MCP runtime and the Control API Lambda (both
# var.enable_attested_computations). Before this file each kept a hand-synced
# copy of the same statements, and the copies had already drifted (the
# Control API's Glue read lacked GetDatabases/GetTables).
#
# The ceiling is read-only by construction: catalog-wide Glue/Athena READ,
# write only to the dedicated Athena results bucket, broad source-data READ
# (a Glue table's storage location can be any bucket) with no Put anywhere on
# source. Only the wiki's frozen statements — typed, validated literals
# substituted by the platform — ever reach an engine; no caller SQL.
# (Chat SQL has a separate grant with its own flag and sids: it executes
# MODEL-authored SQL under a different trust story — see agentcore_iam.tf.)

data "aws_iam_policy_document" "computations_execution_core" {
  statement {
    sid = "ComputationsGlueRead"
    actions = [
      "glue:GetDatabase", "glue:GetDatabases",
      "glue:GetTable", "glue:GetTables",
      "glue:GetPartitions", "glue:GetPartition", "glue:BatchGetPartition",
      "glue:GetTableVersions",
    ]
    resources = ["*"]
  }
  statement {
    sid = "ComputationsAthenaQuery"
    actions = [
      "athena:StartQueryExecution", "athena:GetQueryExecution",
      "athena:GetQueryResults", "athena:StopQueryExecution",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "ComputationsAthenaResultsWrite"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.athena_results.arn, "${aws_s3_bucket.athena_results.arn}/*"]
  }
  statement {
    sid       = "ComputationsTableDataRead"
    actions   = ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["*"]
  }
}

# Lake Formation-governed catalogs: the query engine calls GetDataAccess for
# LF-vended short-lived S3 creds. Merged only when the consumer's gate AND
# var.enable_lakeformation hold.
data "aws_iam_policy_document" "computations_execution_lakeformation" {
  statement {
    sid       = "ComputationsLakeFormationDataAccess"
    actions   = ["lakeformation:GetDataAccess"]
    resources = ["*"]
  }
}

# Redshift-runtime computations execute via the Data API against the mapping
# row's own connection descriptor (actions not ARN-scopable); the secret read
# is NAME-PREFIX-scoped like every other Redshift consumer.
data "aws_iam_policy_document" "computations_execution_redshift" {
  statement {
    sid = "ComputationsRedshiftDataApi"
    actions = [
      "redshift-data:ExecuteStatement",
      "redshift-data:DescribeStatement",
      "redshift-data:GetStatementResult",
      "redshift-data:CancelStatement",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "ComputationsRedshiftSecretRead"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.redshift_secret_resources
  }
}
