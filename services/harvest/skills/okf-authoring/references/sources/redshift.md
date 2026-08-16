# Source adapter: Amazon Redshift

## When you're on this source

The connection is to a Redshift provisioned cluster endpoint (host ends in
`.redshift.amazonaws.com`, port 5439) or a Redshift Serverless workgroup, queried
over the PostgreSQL wire protocol, the Redshift Data API (`aws redshift-data`), or
a JDBC/ODBC Redshift driver. Tell-tale signs: a Postgres-like dialect that also
exposes Redshift-only catalog views (`SVV_TABLE_INFO`, `PG_TABLE_DEF`,
`STV_`/`STL_`/`SVL_` system tables), `DISTKEY`/`SORTKEY`/`DISTSTYLE` in DDL, the
`SUPER` type, or external schemas backed by Redshift Spectrum / AWS Glue.

## Frontmatter conventions

- **`type`**: `redshift.database` for the container (a database within a cluster/
  namespace); `redshift.table` for a native table / materialized view / view;
  `redshift.external_table` for a Spectrum / federated / Glue-backed external table
  (data in S3 or a federated source, not Redshift storage — cost and extraction
  semantics differ, so keep it a distinct type).
- **`resource`**: Redshift has **no** REST URI and **no** table- or schema-level
  ARN (IAM defines ARNs only for `cluster`, `dbname`, `dbuser`, `dbgroup`, and
  Serverless `workgroup`/`namespace` — there is **no `dbtable` resource type**).
  Use a connection-URI-plus-dotted-path:
  `redshift://<cluster-endpoint>:5439/<database>#<schema>.<table>` (Serverless:
  `redshift://<workgroup>.<account-id>.<region>.redshift-serverless.amazonaws.com:5439/<database>#<schema>.<table>`).
  Do **not** fabricate a `dbtable:` ARN. You may additionally record the real
  cluster/dbname ARN for governance:
  `arn:aws:redshift:<region>:<account>:cluster:<name>`.
- **`dialect`**: `amazon-redshift`. Optionally add `engine_variant: provisioned`
  or `serverless` (some system views/limits differ). Redshift SQL is **based on
  PostgreSQL but is NOT Postgres-compatible** — columnar MPP, no secondary
  indexes, no efficient single-row DML, several Postgres types/functions missing.
  Do **not** assert the old "PostgreSQL 8.0.2" lineage as authoritative; current
  docs only say "based on PostgreSQL."

## Schema extraction

API / CLI (no driver; IAM/Secrets auth):
```
aws redshift-data list-tables    --cluster-identifier <c> --database <db> --db-user <u>
aws redshift-data describe-table --cluster-identifier <c> --database <db> --db-user <u> --schema <s> --table <t>
# Serverless: replace --cluster-identifier/--db-user with --workgroup-name; auth via --secret-arn
# Arbitrary metadata SQL: aws redshift-data execute-statement --sql "…"  then  get-statement-result
```
`describe-table` returns a `ColumnList` (per column: `name`, `typeName`, `length`,
`precision`, `scale`, `nullable`, `isCaseSensitive`, `columnDefault`, …).

In-SQL — preferred modern command, gives types **and** sort/dist keys at once:
```sql
SHOW COLUMNS FROM TABLE <database>.<schema>.<table> [LIKE 'pattern'] [LIMIT n];
-- columns: database_name, schema_name, table_name, column_name, ordinal_position,
-- column_default, is_nullable, data_type, character_maximum_length, numeric_precision,
-- numeric_scale, remarks, sort_key_type (COMPOUND/INTERLEAVED), sort_key (signed; negative=interleaved),
-- dist_key, encoding, collation
```
General-purpose column harvester (spans local + external + late-binding views):
```sql
SELECT table_schema, table_name, column_name, ordinal_position, data_type,
       character_maximum_length, numeric_precision, numeric_scale, is_nullable
FROM svv_columns WHERE table_schema='<schema>' AND table_name='<table>' ORDER BY ordinal_position;
```
Redshift-specific layout (distkey/sortkey/encoding) — **search_path-limited**:
```sql
SET search_path TO '$user','public','<schema>';   -- REQUIRED; returns nothing otherwise
SELECT "column", type, encoding, distkey, sortkey, "notnull"
FROM pg_table_def WHERE schemaname='<schema>' AND tablename='<table>';
```

**Cheap row count + table design (for grain verification — no scan):**
```sql
SELECT "schema", "table", diststyle, sortkey1, sortkey_num,
       tbl_rows, estimated_visible_rows, size, unsorted, stats_off, skew_rows
FROM svv_table_info WHERE "schema"='<schema>' AND "table"='<table>';
```
`tbl_rows`/`estimated_visible_rows` give an approximate count without a scan —
prefer over `SELECT COUNT(*)`. Caveats: `SVV_TABLE_INFO` is **superuser-only
unless `SELECT` is granted**, and returns **no rows for empty tables**. (`size` is
in 1-MB blocks. `"table"` is a reserved word — double-quote it.)

External (Spectrum / Glue) tables use different views:
```sql
SELECT schemaname, tablename, columnname, external_type, columnnum, part_key, is_nullable
FROM svv_external_columns WHERE schemaname='<ext_schema>' AND tablename='<table>';
-- part_key > 0 => partition key (in key order); 0 => not partitioned.
-- also SVV_EXTERNAL_TABLES (S3 location, format, SerDe), SVV_EXTERNAL_PARTITIONS (values + S3 path)
```

## Type vocabulary

Write the canonical name; common aliases in parentheses:
`SMALLINT` (INT2), `INTEGER` (INT, INT4), `BIGINT` (INT8), `DECIMAL` (NUMERIC),
`REAL` (FLOAT4), `DOUBLE PRECISION` (FLOAT8, FLOAT), `BOOLEAN` (BOOL),
`CHAR` (CHARACTER, NCHAR, BPCHAR), `VARCHAR` (CHARACTER VARYING, NVARCHAR, TEXT),
`DATE`, `TIME`, `TIMETZ`, `TIMESTAMP`, `TIMESTAMPTZ`,
`INTERVAL YEAR TO MONTH`, `INTERVAL DAY TO SECOND`, `SUPER`, `HLLSKETCH`,
`VARBYTE` (VARBINARY, BINARY VARYING), `GEOMETRY`, `GEOGRAPHY`.

## Identifier quoting

Identifiers use **double quotes**; string literals use **single quotes** (Postgres
style). Double-quote reserved-word identifiers — system views expose columns named
`"table"`, `"column"`, `"notnull"`.
```sql
SELECT "column", "table" FROM pg_table_def WHERE schemaname = 'public' AND "notnull" = true;
```
**Case:** by default **both** unquoted **and** double-quoted identifiers are
case-insensitive and folded to lowercase — double-quoting alone does **not**
preserve case. Mixed-case identifiers require `enable_case_sensitive_identifier=true`
(then you double-quote to reference the mixed-case name). Double quotes are about
reserved words/special chars, not case.

## Idioms

- **Semi-structured (SUPER + PartiQL)** — JSON/Avro/Ion live in the `SUPER` type;
  navigate with dot (`col.data.events`) and bracket (`col.events[0]`). Unnest arrays
  by putting the path in `FROM`: `FROM customer c, c.c_orders o, o.o_lineitems l`
  (add `AS alias AT idx` for the index). Build/parse with `JSON_PARSE('…')`; inspect
  with `JSON_TYPEOF()`; flat JSON in VARCHAR can use `JSON_EXTRACT_PATH_TEXT()`.
  Casts use `::` (`col.status::varchar`).
- **Lax SUPER semantics** — navigation into SUPER returns NULL (not an error) for
  missing attributes / out-of-bounds / invalid casts; type-mismatched equality
  returns FALSE, ordering returns NULL. Use a `CASE`+`JSON_TYPEOF` pattern when you
  need deterministic behavior.
- **Cheap counts / cost** — prefer `SVV_TABLE_INFO.tbl_rows` over `COUNT(*)`.
  There is no dry-run byte estimate; control cost by filtering on the leading
  SORTKEY column (zone-map pruning) and the DISTKEY; for Spectrum, filter partition
  columns (billed per S3 bytes scanned, so Parquet + pruning matter).
- **Sampling** — no `TABLESAMPLE`. Idiom is `ORDER BY RANDOM() LIMIT n` (full sort,
  expensive) or `WHERE RANDOM() < 0.01` (cheap probabilistic).
- **Pagination** — `LIMIT`/`OFFSET`. Redshift ignores the JDBC `maxRows` param —
  always use `LIMIT`.

## Gotchas (worth a `# Gotchas` note in table docs)

- **Postgres-derived but NOT Postgres-compatible** — no secondary indexes, no
  efficient single-row UPDATE/DELETE, some PG types/functions absent. Pin the
  dialect so the agent doesn't borrow Postgres-only idioms.
- **DISTKEY / DISTSTYLE shape good queries** — capture `diststyle` per table
  (`EVEN` | `KEY(col)` | `ALL` | `AUTO(…)`). KEY co-locates rows by that column
  (joins/aggregations on it avoid redistribution); ALL replicates to every node
  (small dims); EVEN is round-robin. State the DISTKEY — it tells the agent which
  join keys are cheap.
- **SORTKEY: compound vs interleaved** — record the sort columns and type. COMPOUND
  prunes only when filters lead with the first column; INTERLEAVED weights multiple
  columns equally. In `PG_TABLE_DEF` the sortkey integer is positive for compound,
  alternating-sign (abs = position) for interleaved; `SHOW COLUMNS` exposes
  `sort_key_type` directly. Sort keys enable zone-map pruning — list them as the
  recommended filter columns.
- **VARCHAR/CHAR length is in BYTES, not characters** — a UTF-8 multibyte char takes
  up to 4 bytes, so four 3-byte CJK chars need `VARCHAR(12)`; CHAR doesn't support
  multibyte at all. Document declared lengths as byte limits to flag truncation
  risk.
- **External/federated tables differ** — not in native catalog views; use the
  `SVV_EXTERNAL_*` views. Hive-style partition keys, data in S3, billed per scan, a
  Hive-vs-Redshift type duality. Flag whether an asset is native or external.
- **Extraction can silently return nothing** — `SVV_TABLE_INFO` needs superuser/
  GRANT and skips empty tables; `PG_TABLE_DEF` only sees schemas in `search_path`.

## Citations

- https://docs.aws.amazon.com/redshift/latest/dg/c_Supported_data_types.html
- https://docs.aws.amazon.com/redshift/latest/dg/r_SVV_TABLE_INFO.html
- https://docs.aws.amazon.com/redshift/latest/dg/r_SVV_COLUMNS.html
- https://docs.aws.amazon.com/redshift/latest/dg/r_PG_TABLE_DEF.html
- https://docs.aws.amazon.com/redshift/latest/dg/r_SHOW_COLUMNS.html
- https://docs.aws.amazon.com/redshift/latest/dg/r_names.html
- https://docs.aws.amazon.com/redshift/latest/dg/query-super.html
- https://docs.aws.amazon.com/redshift/latest/dg/c_redshift-and-postgres-sql.html
- https://docs.aws.amazon.com/redshift-data/latest/APIReference/API_DescribeTable.html
- https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonredshift.html

## Verify before you rely on it

- The `PG_TABLE_DEF` sortkey sign-encoding (positive=compound, alternating=interleaved)
  was confirmed for `SHOW COLUMNS` (interleaved example shows `-1`) but the
  `PG_TABLE_DEF` page itself wasn't re-read this session — spot-check.
- Sampling guidance (no `TABLESAMPLE`; `RANDOM()` idioms) and "Redshift ignores JDBC
  `maxRows`" are dialect lore, not confirmed on a cited page.
- Type vocabulary, `SVV_TABLE_INFO`/`SVV_COLUMNS`/`SHOW COLUMNS` columns, the Data
  API `ColumnMetadata` fields, the no-`dbtable`-ARN fact, and the case-folding rule
  were all confirmed verbatim against the cited docs.
