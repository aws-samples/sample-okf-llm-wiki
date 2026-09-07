# Getting started

This section helps you deploy Data Wiki and publish your first knowledge bundle.

## Setup path

1. Install the required local tools.
2. Configure AWS credentials.
3. Run the deployment script.
4. Sign in to the web console.
5. Create a data domain.
6. Register a dataset.
7. Add useful context documents.
8. Run a full harvest.
9. Review the published bundle.

The first deployment builds three ARM64 container images. It also creates the AWS infrastructure with Terraform.

Deployment time depends on image builds and AWS resource creation. Harvest time depends on the size and complexity of your dataset.

## Before you begin

Choose an AWS Region that supports the required services. Confirm access to the selected Amazon Bedrock models.

Prepare at least one source:

- An AWS Glue database that Amazon Athena can query.
- An Amazon Redshift provisioned cluster or Serverless workgroup.

## Next step

Review the [prerequisites](prerequisites.md).
