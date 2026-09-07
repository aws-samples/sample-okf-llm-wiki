# Configure data sources

Data Wiki supports AWS Glue and Amazon Redshift.

## AWS Glue

Glue mappings use a Glue database as the dataset. The harvest runtime reads metadata and queries evidence through Amazon Athena.

Confirm these items:

- The database exists in the deployment Region.
- Athena can query its tables.
- The harvest data role can read table data.
- Metadata roles can describe the database and tables.

Glue changes can use the incremental freshness path.

## Amazon Redshift

Redshift support is disabled by default. Enable it for the compute stack:

```bash
export TF_VAR_enable_redshift=true
./scripts/deploy.sh compute
./scripts/deploy.sh ui
```

A Redshift mapping identifies:

- Provisioned cluster or Serverless workgroup
- Database
- Secrets Manager secret
- Data Wiki domain and dataset

The secret must contain a read-only database user. Use the configured secret name prefix.

Redshift datasets use the Redshift Data API. They do not receive Glue change events.

## Add another source type

A source implementation provides metadata, prompt labels, evidence queries, and credential policy.

It must also define registration fields and source-specific IAM grants. Keep downstream bundle and search contracts source-neutral.

## Security guidance

Grant read-only access to the smallest useful source scope. Keep source credentials separate from bundle write permissions.

Use database controls for row and column restrictions. Do not depend on prompt instructions for data access control.
