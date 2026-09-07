# Deploy Data Wiki

Use the deployment script to build images and create the AWS resources.

## Obtain the source

Clone the repository, then enter its root directory:

```bash
git clone <repository-url>
cd sample-okf-llm-wiki
```

## Run the full deployment

Run:

```bash
./scripts/deploy.sh
```

The first run asks for these values:

- AWS Region
- Terraform state bucket
- Resource name prefix
- Initial administrator email and name

The script stores the values in `scripts/.deployment.config`. This file is excluded from Git.

## Deployment stages

The full deployment runs these stages:

1. Create durable storage, Cognito, and DynamoDB resources.
2. Build and push the runtime container images.
3. Create compute resources and API endpoints.
4. Add the CloudFront URLs to Cognito.
5. Build and upload the web console.

Run one stage when you only need to update part of the system:

```bash
./scripts/deploy.sh <durable|images|compute|cognito-urls|ui>
```

## Open the console

Print the deployed endpoints:

```bash
./scripts/deploy.sh summary
```

Open the console URL. Sign in with the temporary password that Amazon Cognito sent to the administrator.

!!! important
    Change the temporary password when Cognito prompts you. Store operator credentials in your approved password manager.

## Next step

[Run your first harvest](first-harvest.md).
