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
    # Cloud Control provider — ONLY for the AgentCore Memory resource
    # (agent_memory.tf): hashicorp/aws doesn't expose the per-record metadata
    # schema yet, awscc (generated from the CloudFormation schema) does.
    # Same credential chain/region as the aws provider.
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.0"
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

provider "awscc" {
  region = var.region
}
