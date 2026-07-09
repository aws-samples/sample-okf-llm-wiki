# Durable stack — the source of truth + long-lived identity/state.
# Split from the compute stack (infra/compute) by LIFECYCLE so a routine compute
# redeploy can never threaten the bundle bucket, the vector index, or Cognito.
#
# Contains: S3 bundle bucket (system of record), S3 Vectors bucket + index
# (derived semantic index, immutable params), Cognito user pool + client,
# DynamoDB registry + freshness tables.

terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Remote state in S3 with native lockfile (DynamoDB locking is deprecated).
  # Configure via `terraform init -backend-config=...` or edit these values.
  backend "s3" {
    key          = "okf/durable/terraform.tfstate"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
}
