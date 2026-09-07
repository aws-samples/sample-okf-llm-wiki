# Use AWS Lake Formation

Enable AWS Lake Formation support when it governs the Glue tables that Data Wiki will harvest.

## Enable the integration

Set the compute variable:

```bash
export TF_VAR_enable_lakeformation=true
./scripts/deploy.sh compute
```

This change adds `lakeformation:GetDataAccess` to the harvest data role.

!!! important
    IAM permission is not enough. You must also grant Lake Formation permissions for each mapped dataset.

## Grant data access

Choose one data access model for the harvest data role.

For unrestricted table reads, grant:

- `SELECT` on the tables
- `DESCRIBE` on the tables
- `DESCRIBE` on the database

For restricted reads, grant `SELECT` through a Lake Formation data cells filter. Do not also grant table-wide `SELECT`.

Grant metadata roles `DESCRIBE`. These roles include the control API, incremental worker, and optional reconcile worker.

The following example grants unrestricted table-wide access:

```bash
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=<harvest-data-role-arn> \
  --resource '{"Table":{"DatabaseName":"<database>","TableWildcard":{}}}' \
  --permissions SELECT DESCRIBE
```

## Register data locations

Register each S3 table location with Lake Formation:

```bash
aws lakeformation register-resource \
  --resource-arn arn:aws:s3:::<bucket>/<prefix> \
  --use-service-linked-role
```

## Protect sensitive data

Use Lake Formation row and column filters when the harvest must not read sensitive table data.

These filters constrain future table-data reads. They do not hide Glue catalog metadata from the authoring process.

Filters do not remove data from existing pages, object versions, or traces. Remove sensitive historical content through the relevant retention and deletion workflows.

## Troubleshoot access

An IAM-allowed request can still fail when a Lake Formation grant is missing.

Check database grants, table grants, and S3 location registration. Also confirm that the caller uses the expected role.
