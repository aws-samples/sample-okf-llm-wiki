# Compute stack — the frequently-redeployed tier: Lambdas, API Gateway, the two
# AgentCore runtimes + their IAM roles, EventBridge/SQS wiring, CloudFront/UI.
# Reads durable-stack outputs via terraform_remote_state so a redeploy here can
# never touch the bundle bucket, vector index, or Cognito.

terraform {
  required_version = ">= 1.7"

  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
    # time_sleep: let IAM role-policy changes propagate before AgentCore's
    # CreateAgentRuntime validates the execution role (avoids a stale-snapshot
    # ValidationException that oscillates between missing s3files actions).
    time = { source = "hashicorp/time", version = "~> 0.12" }
  }

  backend "s3" {
    key          = "okf/compute/terraform.tfstate"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
}

# CloudFront + ACM for the UI must be referenced from us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

# The web-search gateway lives where the connector is offered (us-east-1,
# eu-west-1, or ap-northeast-1 — var.web_search_region), which may differ from
# var.region. Its own alias, not aws.us_east_1: that one is pinned for
# CloudFront/ACM and must not move with the connector choice.
provider "aws" {
  alias  = "web_search"
  region = var.web_search_region
}
