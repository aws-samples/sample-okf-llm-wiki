# Integrating with AWS Lake Formation

By default OKF harvests plain Glue/Athena catalogs using IAM permissions only. If
the catalog you harvest is governed by **AWS Lake Formation (LF)**, LF becomes the
binding data-access gate: a principal needs *both* IAM permission **and** an LF
grant to read a table. This guide covers what the solution does for you and the
grants you must add.

## What the solution does (one flag)

Set `enable_lakeformation = true` in the compute stack. This adds
`lakeformation:GetDataAccess` to the harvest **data role** — in both its identity
policy (`infra/compute/agentcore_iam.tf`) and the per-invocation session policy
(`services/harvest/src/harvest/clients.py`, gated by `OKF_ENABLE_LAKEFORMATION`) —
so Athena/Glue can obtain LF-vended, short-lived S3 credentials for governed table
data. This is the **only** change the solution can make on its own.

```hcl
# terraform.tfvars (compute stack)
enable_lakeformation = true
```

It is **necessary but not sufficient** — LF grants and data-location registration
are account/catalog state the adopter owns. Without the steps below, harvests fail
with an LF authorization error *even though the IAM policy is correct*.

## What you must grant (per mapped dataset)

Only one role reads data (`okf-harvest-data`); the rest are metadata-only. In LF
mode, `glue:GetTable(s)` responses are **filtered** to what the caller has
`DESCRIBE` on, so the metadata roles need `DESCRIBE` or the dataset looks empty and
the harvest never starts.

| Role | Needs | Why |
|------|-------|-----|
| `<prefix>-harvest-data` | **`SELECT` + `DESCRIBE`** | runs Athena `run_sql`/`sample_rows`; reads table data |
| `<prefix>-control-api-role` | **`DESCRIBE`** | `GetTables` dataset-exists check |
| `<prefix>-incremental-role` | **`DESCRIBE`** | `GetTable`/`GetTableVersions` change detection |
| `<prefix>-reconcile-role` *(if `enable_reconcile`)* | **`DESCRIBE`** | nightly version re-scan |

### 1. Grant LF permissions

```bash
ACCT=<account-id>; DB=<dataset_glue_db>

# Data role — read data + metadata on every table in the dataset's database
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::$ACCT:role/<prefix>-harvest-data \
  --resource "{\"Table\":{\"DatabaseName\":\"$DB\",\"TableWildcard\":{}}}" \
  --permissions SELECT DESCRIBE
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::$ACCT:role/<prefix>-harvest-data \
  --resource "{\"Database\":{\"Name\":\"$DB\"}}" \
  --permissions DESCRIBE

# Metadata-only roles — DESCRIBE (repeat for control-api, incremental, reconcile)
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::$ACCT:role/<prefix>-control-api-role \
  --resource "{\"Table\":{\"DatabaseName\":\"$DB\",\"TableWildcard\":{}}}" \
  --permissions DESCRIBE
```

If you run **LF Tag-Based Access Control (LF-TBAC)**, grant on an `LFTag` resource
instead of per-database — one grant then covers every table carrying the tag, and
new datasets are governed automatically as they inherit the tag.

### 2. Register the table data location

So LF can vend S3 credentials for reads (this is what lets you rely on LF instead
of the role's broad `s3:GetObject`):

```bash
aws lakeformation register-resource \
  --resource-arn arn:aws:s3:::<table-data-bucket>/<prefix> \
  --use-service-linked-role
```

### 3. (Recommended) Mask sensitive columns / filter rows

The real benefit: grant `SELECT` with an LF **data-cells filter** that excludes or
masks PII columns. Then even a prompt-injected `SELECT *` in `run_sql` cannot pull
those fields into sample rows, observability traces, or the authored bundle —
enforced at the data source, not by prompt discipline. (Directly strengthens
threats #6/#7/#15 in `threat-model.md`.)

## Notes & gotchas

- **LF is AND'd with IAM, not a replacement.** Keep the role's existing
  `glue:Get*` / `athena:*` grants — IAM must still allow the *action*; LF then
  decides the *data*.
- **Silent-looking failure.** LF-on + missing grant → access-denied on a table the
  IAM policy clearly allows. Rule of thumb: whenever you map a new dataset, grant
  the roles LF permissions on that database (or tag it for LF-TBAC).
- **Migration.** LF **hybrid access mode** (`IAMAllowedPrincipals` on a table) lets
  the OKF roles keep working via plain IAM until you enforce LF per-table — a
  gentler path than flipping the whole catalog at once.
- **Once registered + granted**, you may remove the harvest data role's broad
  `TableDataRead` (`s3:GetObject "*"`) grant, since reads go through LF-vended
  credentials. Leave it if some mapped datasets remain non-LF.
- **Unaffected by LF:** the S3 bundle bucket, S3 Vectors index, Cognito, the
  CloudFront/SPA surface, and the code-interpreter sandbox — none are Glue-governed
  data, so LF has no bearing on them. Glue *metadata* free-text (comments,
  descriptions, `Parameters`) still flows into the authoring prompt as source
  DATA; the prompt's "document it, don't act on embedded instructions" rule
  governs it — LF governs data reads, not that text.
```
