# Source adapter: AWS Glue Data Catalog + Amazon Athena

**Two layers of one source.** The **Glue Data Catalog** is the *metastore*
(Hive-compatible: schema + SerDe + S3 location + partition keys). **Athena** is the
*query engine* over S3. They use different SQL: Athena **DML** (SELECT/CTAS/INSERT)
is **Trino**; Athena **DDL** (CREATE EXTERNAL TABLE) is **Hive**. Most of this
adapter's traps come from that split.

## When you're on this source

Data lives as files in S3 (Parquet/ORC/JSON/CSV), queried serverlessly through
Athena, with metadata in the Glue Data Catalog. Tell-tale signs: tables are
`CREATE EXTERNAL TABLE` definitions with a `LOCATION 's3://…'` and a SerDe, queries
run in an Athena workgroup, and `aws glue get-table` resolves the metadata.

## Frontmatter conventions

- **`type`**: `glue-database` for the container (a Glue database / namespace, which
  Athena addresses as the schema); `glue-table` for the asset (Glue table = external
  metadata over S3 files). (`athena-database` / `athena-table` are acceptable
  alternates — be consistent.)
- **`resource`**: the Glue **table ARN** —
  `arn:aws:glue:{region}:{account_id}:table/{database_name}/{table_name}`
  (database: `arn:aws:glue:{region}:{account_id}:database/{database_name}`). Also
  record the physical backing as a separate `s3_location:` key
  (`s3://{bucket}/{prefix}/`), since the table is metadata over those files. The
  Athena-addressing form is `awsdatacatalog.{database}.{table}` (`awsdatacatalog` is
  the default Athena catalog for the Glue Data Catalog).
- **`dialect`**: `athena-sql-trino` (engine v3). A single string can't capture both
  halves — pin the **DML** engine and note the **Hive-DDL duality** in the body.
  Engine version is a **per-workgroup** setting (v3 = Trino, v2 = Presto); record
  the workgroup if known. Idioms below assume **v3 (Trino)**.

## Schema extraction

Glue API / CLI (the metastore side):
```
aws glue get-databases --region {region} [--catalog-id {account_id}]                 # datasets
aws glue get-tables   --database-name {db} --region {region}                          # tables: Name, TableType, StorageDescriptor, PartitionKeys
aws glue get-table    --database-name {db} --name {table} --region {region}           # full def (best for authoring)
aws glue get-partitions --database-name {db} --table-name {table} --region {region}   # registered partitions (empty under partition projection)
```
`get-table` returns `StorageDescriptor.Columns[]` (`{Name, Type, Comment}`),
`StorageDescriptor.Location` (the `s3://` path), `SerdeInfo.SerializationLibrary`,
`InputFormat`/`OutputFormat`, `BucketColumns`/`SortColumns`/`NumberOfBuckets`,
`PartitionKeys[]` (partition columns, **separate** from data columns), and
`Parameters` (table properties — where `projection.*` keys live). **Column `Type`
strings here are Hive names** (`string`, `int`, `array<string>`, `struct<…>`).

Athena SQL (the engine side):
```sql
SHOW CREATE TABLE mydb.sales;   -- full DDL: columns, PARTITIONED BY, ROW FORMAT SERDE, STORED AS, LOCATION, TBLPROPERTIES (incl. projection)
DESCRIBE mydb.sales;            -- col_name / data_type / comment; partition cols under a '# Partition Information' section. Hive type names.
SHOW PARTITIONS mydb.sales;     -- registered partitions (NOT projection-computed ones)
MSCK REPAIR TABLE mydb.sales;   -- register Hive key=value partitions found under LOCATION (ignored under projection)

-- Tabular column metadata (cleanest machine-readable). data_type here is the TRINO/DML name
-- (varchar, integer, array(varchar), row(…)), NOT the Hive name. table_catalog = 'awsdatacatalog'.
SELECT column_name, data_type, ordinal_position, is_nullable
FROM information_schema.columns
WHERE table_schema = 'mydb' AND table_name = 'sales' ORDER BY ordinal_position;
```

**Cheap row count / cost (for grain verification):** there is no free metadata row
count — `count(*)` scans S3 and is billed by bytes, so **always filter partition
columns** (`WHERE dt = '2024-01-01'`). Read actual bytes scanned from
`DataScannedInBytes` via `aws athena get-query-execution`. Sample without a full
scan: `SELECT * FROM mydb.sales TABLESAMPLE BERNOULLI(1)`.

## Type vocabulary

The **same column** has a Hive name (stored, what DDL/`get-table` show) and a Trino
name (what DML/`information_schema` show). Write both when they differ; the Hive
form is what's stored.

| Concept | Hive (DDL) | Trino (DML) |
|---|---|---|
| string | `string` | `varchar`, `varchar(n)` |
| 32-bit int | `int` | `integer` |
| 32-bit float | `float` | `real` |
| binary | `binary` | `varbinary` |
| array | `array<element>` | type-name `array(element)`; **literal/declaration `ARRAY[…]`** |
| map | `map<k,v>` | `map(k,v)` |
| struct | `struct<field:type,…>` | `row(field type, …)` |

Shared (same both sides): `boolean`, `tinyint`, `smallint`, `bigint`, `double`,
`decimal(p,s)`, `char`, `date`, `timestamp`. **DML-only** (no Hive/DDL form):
`time`, `time with time zone`, `timestamp with time zone`, `interval …`, `json`,
`uuid`, `ipaddress`.

> Trino renders array two ways — the **type-name string** is `array(varchar)` (what
> `information_schema.columns` and CASTs emit), but the **DML literal/declaration**
> form AWS documents is `ARRAY[…]` (square brackets). `MAP(…)`/`ROW(…)` use parens
> in both. Don't write `array(...)` where a literal `ARRAY[...]` is meant.

## Identifier quoting (the headline trap)

- **DML (Trino):** identifiers in **double quotes**, literals in **single quotes**.
  ```sql
  SELECT "user"."name", "date" FROM "mydb"."sales" WHERE country = 'US';
  ```
  In `GROUP BY`, nested/struct columns **must** be double-quoted in v3
  (`GROUP BY "user"."name"`).
- **DDL (Hive):** identifiers in **backticks**.
  ```sql
  CREATE EXTERNAL TABLE `mydb`.`sales` (`id` int, `name` string) … LOCATION 's3://bucket/sales/';
  ```
- Glue stores database/table/column names **lowercase** (Hive compat), so
  identifiers are effectively case-insensitive.

## Idioms

- **Array access (UNNEST)** — Trino uses `UNNEST` in a CROSS JOIN, **not** Hive
  `LATERAL VIEW explode`:
  `SELECT t.id, item FROM mydb.orders t CROSS JOIN UNNEST(t.line_items) AS x(item)`.
  `WITH ORDINALITY` adds a position column. Arrays are **1-indexed** (`arr[1]`);
  `element_at(arr, n)` is null-safe.
- **Struct / map** — struct fields by dot (`user.name`, double-quote reserved
  names); map values `my_map['key']` or `element_at(my_map, 'key')`.
- **JSON** — `json_parse()` to parse; `json_extract(j, '$.path')` returns JSON,
  `json_extract_scalar(j, '$.path')` returns varchar. Or map JSON files to typed
  columns at CREATE TABLE time via the Hive `JsonSerDe`.
- **Cost / scan control** — billed by S3 bytes scanned: (1) always predicate on
  PARTITION columns to prune prefixes, (2) SELECT only needed columns (columnar
  Parquet/ORC reads fewer chunks), (3) prefer Parquet/ORC over CSV/JSON. `LIMIT`
  alone does not reduce bytes on a full scan of row formats.
- **Sampling** — `TABLESAMPLE BERNOULLI(pct)` (row-level) or `SYSTEM(pct)`
  (split-level, cheaper, coarser) — Trino syntax.
- **Approximate aggregates** — `approx_distinct(col)`, `approx_percentile(col, p)`
  for cheap cardinality/quantiles (note `approx_percentile` changed implementation
  in v3 and returns different results than v2).
- **Materialization** — `CREATE TABLE … WITH (format='PARQUET', partitioned_by=ARRAY['dt'], external_location='s3://…') AS SELECT …`
  (CTAS) registers a new Glue table; `UNLOAD` writes output to S3 without a table.

## Gotchas (worth a `# Gotchas` note in table docs)

- **DDL=Hive, DML=Trino** — a column shown as `string` / `array<string>` /
  `struct<…>` by `get-table` or `SHOW CREATE TABLE` is the **same column**
  `information_schema` reports as `varchar` / `array(varchar)` / `row(…)`. Document
  both so they're not mistaken for different columns.
- **Identifier-quoting duality** — double quotes (DML) vs backticks (DDL) is the
  most common syntax error. Capture the right quoting per statement type.
- **Tables are EXTERNAL metadata over S3** — a Glue table is schema + SerDe +
  LOCATION, not stored data. Row counts/freshness depend on the S3 objects. Record
  the `SerializationLibrary` (ParquetHiveSerDe, OrcSerde, OpenCSVSerde, JsonSerDe)
  and file format — they govern type interpretation and predicate pushdown.
- **Partitioning drives cost** — partition columns (Glue `PartitionKeys` /
  `PARTITIONED BY`) are the primary lever for limiting bytes scanned. New S3
  prefixes aren't queryable until registered via `MSCK REPAIR TABLE` (Hive
  key=value layout) or `ALTER TABLE ADD PARTITION` — or via partition projection.
- **Partition projection hides partitions** — with `'projection.enabled'='true'`
  (+ per-column `projection.*` TBLPROPERTIES), Athena computes partitions in-memory
  and **ignores** catalog partition metadata: `SHOW PARTITIONS` and
  `get-partitions` return nothing, and out-of-range partition values return **zero
  rows with no error**. Note whether a table uses projection.
- **Glue lowercases names** — mixed-case source columns are folded to lowercase;
  don't author docs assuming case-sensitive column names.
- **Bucketing, not DISTKEY/SORTKEY** — Athena's analog of distribution/sort is
  Hive/Spark bucketing (`BucketColumns` + `NumberOfBuckets`, `SortColumns`). In v3
  you must set `('bucketing_format'='spark')` in TBLPROPERTIES if data was written
  by Spark's hash, else Athena assumes the Hive hash and may misread buckets.
- **`awsdatacatalog` and federated sources** — Glue is exposed to Athena as catalog
  `awsdatacatalog`. Athena can also query external Hive metastores and federated
  sources (RDS/DynamoDB via Lambda connectors) under other catalog names — those
  are **not** in Glue, so `aws glue get-table` won't resolve them. Note the catalog
  when a table isn't in `awsdatacatalog`.
- **Engine version changes behavior** — v2 (Presto) → v3 (Trino) breaking changes:
  `CONCAT` needs ≥2 args; struct→JSON cast now emits field names; varchar→decimal
  coercion now fails; `SKIP` became a reserved word (DML); `log()` arg order
  changed to `log(base, value)`; timestamp precision is validated (always
  precision 3 / millisecond). Pin the version; flag that idioms assume v3.

## Citations

- https://docs.aws.amazon.com/athena/latest/ug/data-types.html
- https://docs.aws.amazon.com/athena/latest/ug/engine-versions-reference-0003.html
- https://docs.aws.amazon.com/athena/latest/ug/querying-glue-catalog.html
- https://docs.aws.amazon.com/athena/latest/ug/querying-glue-catalog-listing-columns.html
- https://docs.aws.amazon.com/athena/latest/ug/partition-projection.html
- https://docs.aws.amazon.com/cli/latest/reference/glue/get-table.html
- https://docs.aws.amazon.com/service-authorization/latest/reference/list_awsglue.html
- https://trino.io/docs/current/language/types.html

## Verify before you rely on it

- **Glue table/database ARN templates** are consistent with AWS convention but the
  service-authorization Resource-types table wasn't read verbatim — confirm against
  the live "Resource types defined by AWS Glue" table (note the *catalog* is a
  separate resource, not embedded in the table ARN path).
- The `'projection.enabled'='true'` property name and per-column `projection.*` keys
  live on the partition-projection setup subpage — verify spelling/casing there.
- `DESCRIBE`'s `# Partition Information` header and `SHOW CREATE TABLE` output weren't
  re-read from a dedicated page this session.
- Engine identity (v3 = Trino), the v2→v3 breaking changes, the DDL-Hive/DML-Trino
  type duality, partition-projection behavior, and `information_schema.columns`
  output were all confirmed verbatim.
