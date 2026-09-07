# Prerequisites

Prepare your workstation and AWS account before deployment.

## Local tools

Install these tools:

- A current AWS CLI v2
- Terraform CLI 1.7 or later
- Docker with `buildx` support
- Node.js 24
- npm
- Python 3.12 or later
- `jq`
- Git

The repository includes a `.mise.toml` file. You can use `mise install` to install the pinned Node.js version.

The deployment uses AWS CLI commands for credential export, AgentCore, Amazon ECR Public, and Amazon S3 Files.

Verify the tools:

```bash
aws --version
terraform version
docker version
docker buildx version
node --version
npm --version
python3 --version
jq --version
git --version
```

## AWS credentials

Configure credentials for the account that will host Data Wiki. The deployment script uses the standard AWS credential provider chain.

Confirm access:

```bash
aws sts get-caller-identity
```

The deployment identity must create IAM, networking, storage, compute, Cognito, and CloudFront resources.

!!! tip
    Use a short-lived role or AWS IAM Identity Center session. Refresh the session before a full deployment.

## AWS service access

Confirm that your account and selected Regions support these services:

- Amazon Bedrock
- Amazon Bedrock AgentCore
- Amazon S3
- Amazon S3 Files
- Amazon S3 Vectors
- Amazon Elastic Container Registry
- Amazon ECR Public
- AWS Lambda
- Amazon API Gateway
- Amazon Cognito
- Amazon DynamoDB
- Amazon CloudFront
- Amazon EventBridge
- Amazon Simple Queue Service
- Amazon CloudWatch
- AWS Secrets Manager
- AWS Glue and Amazon Athena for Glue sources
- Amazon Redshift when Redshift support is enabled

Enable access to every configured Bedrock model. The model catalogs are defined in `infra/compute/variables.tf`.

Also enable Titan Text Embeddings V2. Semantic indexing uses `amazon.titan-embed-text-v2:0`.

AgentCore Memory uses the model configured by `chat_memory_model` in `infra/durable/agent_memory.tf`.

## Data source

Prepare at least one source that the harvest runtime can read.

For AWS Glue, confirm that Amazon Athena can query the database. For Amazon Redshift, prepare a read-only database user.

If AWS Lake Formation governs the Glue catalog, complete the extra grants in [Use AWS Lake Formation](../configuration/lake-formation.md).

## Next step

[Deploy Data Wiki](deploy.md).
