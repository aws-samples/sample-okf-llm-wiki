# Data sources — Glue + Redshift, and how to add another

Data Wiki authors an OKF bundle from a **data source** and serves it over MCP.
Two sources are implemented today:

- **AWS Glue Data Catalog** (metadata) queried through **Amazon Athena** (row
  samples + verification SQL) — the original, reference implementation.
- **Amazon Redshift** (provisioned clusters or Serverless) — metadata AND data
  via the **Redshift Data API** over the `SVV_*` catalog views. Connection
  details live on each mapping (a *self-describing* source descriptor), not in
  deploy config.

The harvest runtime dispatches on the mapping's source `type` through a
source-neutral `Source` protocol; the registry data model, the UI, the chat SQL
tool, and the authoring skill are all source-aware. This doc maps how the Glue
implementation works, which seams a new source plugs into, the
[security model](#security-model-redshift) for the Redshift path, and the
[recipe](#recipe-adding-a-new-data-source) for adding a source (BigQuery,
RDS, …).

## The concept model

OKF maps onto AWS as **`data domain → dataset → table`**:

| OKF concept | AWS (Glue) | Bundle location |
|---|---|---|
| data domain | operator-declared grouping | `okf/<domain>/_domain/overview.md` |
| dataset | one Glue **database** | `okf/<domain>/<dataset>/datasets/<dataset>.md` |
| table | one Glue **table** | `okf/<domain>/<dataset>/tables/<table>.md` |

One Glue database is one dataset; each Glue table is one `table` concept. This
mapping is produced by `GlueAthenaSource.list_concepts`
(`services/harvest/src/harvest/glue_source.py:107`), which emits `ConceptRef`s with
id tuples `("datasets", <db>)` / `("tables", <name>)` and the **frozen** frontmatter
`type` strings **`Glue Database`** and **`Glue Table`**. Those exact strings are
asserted in the harvest prompt (`prompts.py:97`) and routed on by the augmentation
guard (`okf_core/guard.py:160`) — they are part of the contract, not cosmetic.

By convention the **dataset name equals the Glue database name**. This is enforced
at registration (`control_api/app.py:310` rejects `dataset != glue_database`) and
relied on at invocation time (the harvest payload carries only `dataset`, and the
runtime resolves it to a same-named Glue database). This is a Glue-specific
assumption a new source must relax — see the recipe.

## The current Glue + Athena implementation (live path)

A harvest flows registration → invocation → metadata read → authoring → change
detection. Glue/Athena coupling is concentrated in `services/harvest/` plus two
pure modules in `okf_core/`. `okf_aws/` is **not** involved in Glue.

### 1. Registration (`control_api`)

`PUT /domains/{domain}/datasets/{dataset}` (`app.py:294`, `_r_upsert_domain`):

- Accepts either a first-class `source` object or a bare `glue_database`, reconciled
  by `okf_core.normalize_source` (`app.py:300`).
- Enforces the Glue-specific `dataset == glue_database` rule (`app.py:310`).
- Probes existence with `assert_glue_database_exists` (`app.py:320`,
  `handlers.py:120`), a `glue.get_tables` call.
- Writes the DynamoDB `DATASET#` row via `upsert_domain_mapping`
  (`handlers.py:377`): `pk=DOMAIN#<domain>`, `sk=DATASET#<dataset>`, attrs
  `{data_domain, dataset, source={M:{type,glue_database}}, glue_database, created_at}`.
  The flat `glue_database` is written **in addition** to the nested `source` as a
  back-compat mirror the harvest payload and incremental scan read directly.

Glue-specific endpoint: `GET /glue/databases` → `list_glue_databases`
(`handlers.py:99`) feeds the UI picker.

### 2. Invocation

The Control API's `trigger_harvest` calls `InvokeAgentRuntime` with a payload that
carries `data_domain` / `dataset` / `mode` (+ guidance, RI settings) but **not** the
source type (see CONVENTIONS.md "Harvest invocation payload"). The runtime
entrypoint (`harvest/entrypoint.py:70`) then calls `build_source(dataset)`, which
assumes the dataset name is a Glue database.

### 3. Metadata read + snapshot

- `GlueAthenaSource.read_concept` (`glue_source.py:153`) turns the Glue JSON
  (`StorageDescriptor.Columns`, `Location`, `PartitionKeys`, `TableType`,
  `Parameters`, `UpdateTime`, `VersionId`) into the internal metadata dict every
  downstream step consumes. `_database_arn`/`_table_arn` (`glue_source.py:81`) build
  the `arn:aws:glue:…` `resource` URIs.
- `metadata_export.py` snapshots **all** metadata once at run start into the
  read-only `.metadata/` dir — `index.md`, `database.md`, `tables/<t>.md`, and
  `columns.tsv` (one `table\tcolumn\ttype\tcomment` line per column, the grep target
  for join/near-synonym discovery). The *structure* is reusable across sources; the
  headings ("Glue table metadata", "Glue table Parameters") and the row-count-hint
  logic (`_ROWCOUNT_PARAM_KEYS`, `metadata_export.py:52`) are Glue-worded.
- `okf_core/hive_types.py` flattens Hive `Column.Type` strings
  (`struct<…>`, `array<…>`, `map<…>`, `decimal(p,s)`) into `FlatField(name, type,
  depth)` rows. This is the one module that understands **Hive/Glue type grammar**;
  a source with different types needs its own flattener.

### 4. Query engine (Athena)

- `sample_rows` (`glue_source.py:199`) generates Trino SQL
  (`SELECT * FROM "<db>"."<table>" LIMIT n`); `run_query` (`glue_source.py:215`)
  runs the full Athena `start`/`poll`/`get_query_results` lifecycle and preserves
  SQL `NULL` (`None`) vs empty string (`""`).
- The agent's only two live source tools, `sample_rows` and `run_sql`, wrap this
  (`source_tools.py`, `make_source_tools(source)`).
- The recursive-improvement **benchmark grader** takes an injected `execute`
  callable with "the same contract as `GlueAthenaSource.run_query`"
  (`benchmark/grader.py:95`) — already source-neutral by injection.

### 5. Credentials (`clients.py`)

`build_source` (`clients.py:203`) constructs `boto3.client("glue"/"athena")` from
**per-invocation down-scoped** STS creds: `_session_policy` (`clients.py:36`) pins
Glue actions to `database/<db>` + `table/<db>/*` and Athena to one workgroup, then
`build_scoped_session` assumes `OKF_HARVEST_DATA_ROLE_ARN` with that inline policy.
In local/test runs (`OKF_HARVEST_DATA_ROLE_ARN` unset) it falls open to ambient
creds. Env: `OKF_GLUE_CATALOG_ID`, `OKF_ATHENA_WORKGROUP`, `OKF_ATHENA_OUTPUT`,
`OKF_ENABLE_LAKEFORMATION`.

### 6. Change detection (`incremental`)

The most Glue-hardwired component. An EventBridge rule
(`infra/compute/incremental.tf:44`, pattern `source: ["aws.glue"]`, `detail-type:
["Glue Data Catalog Table State Change"]`) → SQS → `incremental/handler.py`
(`process_event` reads `databaseName`/`tableName`/`changedPartitions`), which
resolves the database to a dataset via `store.resolve_domain` (matching the flat
`glue_database` attribute), confirms a real change against the stored Glue
`VersionId`, computes a column diff, and triggers a scoped re-harvest. A nightly
`reconcile.py` sweep is the safety net. **Redshift has no equivalent Glue-catalog
event source.**

### 7. Downstream — already source-agnostic

`reindex`, `consumption_mcp`, and the `okf_core` modules `embedding.py`,
`index_gen.py`, `paths.py`, `domain.py` operate on the authored bundle (S3 markdown
+ vectors) and treat the concept `type` as opaque. They need **no** change for a new
source. The augmentation guard (`guard.py`) once enumerated the two Glue `type`
strings inline; it now routes on `okf_core.concept_types.is_schema_bearing_type`
(see below), so a new source's concept types are protected by registering them, not
by editing the guard.

## The abstraction seams already in place

Three seams were built ahead of the runtime work:

1. **Registry vocabulary** — `okf_core/sources.py` defines the nested
   `source = {type, ...config}` descriptor with `build_glue_source`,
   `normalize_source` (reconciles new nested + legacy flat rows), `validate_source`
   (has a placeholder branch at `sources.py:111` for the next type), and
   `SUPPORTED_SOURCE_TYPES = (glue,)`. Adding a type here is a no-migration change.
2. **UI** — `ui/src/views/MappingsView.jsx:44` models a source-type dropdown
   (currently `disabled`, "Only AWS Glue is supported today"); `ui/src/lib/api.js`
   already POSTs the nested `source` object.
3. **Authoring skill** — `services/harvest/skills/okf-authoring/references/sources/`
   holds per-backend adapters (`athena-glue.md`), an "adding a new adapter" recipe
   (`index.md`), and a **complete `redshift.md` adapter** already written (system
   views, DISTKEY/SORTKEY, `SUPER`, resource-URI form). The methodology layer is
   already multi-source-aware.

(That was the starting point; the runtime work below closed the gap — the
runtime now dispatches on `source["type"]`, and `clients.build_source` reads the
database from the descriptor instead of assuming `dataset == glue_database`.)

### Runtime seams

The harvest runtime has the source-neutral boundary the recipe below needs:

- **`Source` protocol** (`harvest/source_base.py`) — the metadata-reader +
  query-engine contract, plus the shared `ConceptRef` and a `SourceMetadataProfile`
  (per-source `.metadata/` labels). `GlueAthenaSource` implements it, and the runner,
  agent, `metadata_export`, and tools now depend on `Source`, not the concrete class.
- **Concept-type registry** (`okf_core/concept_types.py`) — owns the Glue `type`
  constants (`GLUE_DATABASE_TYPE`/`GLUE_TABLE_TYPE`) and `SCHEMA_BEARING_TYPES` +
  `is_schema_bearing_type()`. `glue_source` emits these constants; `guard.py` routes
  on the predicate. A new source registers its concept types here.
- **Pluggable `.metadata/` labels** — `metadata_export` derives every heading from
  the source's `SourceMetadataProfile` (label, catalog name, resource-label,
  row-count keys), so the snapshot reads correctly per backend with no code fork.
- **Second source implemented** (Phase C) — `harvest/redshift_source.py`'s
  `RedshiftSource` implements the `Source` protocol via the **Redshift Data API**
  (`execute_statement` → `describe_statement` → `get_statement_result`) over the
  `SVV_*` catalog views, emits the `Redshift Database`/`Redshift Table`/`Redshift
  External Table` concept types (registered in `concept_types.py`), and carries a
  Redshift `SourceMetadataProfile` (connection-URI resource, `tbl_rows` row-count
  hint). Unit-tested offline with an in-memory Data API fake (`FakeRedshiftData`).
- **Runtime source selection + credentials** (Phase D) — the `source` descriptor
  (`{type, ...config}`) rides the harvest invocation payload from all three trigger
  sites (Control API `trigger_harvest`, the annotation run, the incremental
  handler). `harvest.clients.build_source` dispatches on `source["type"]`:
  `_build_glue_source` (reads the DB from the descriptor, no longer assuming
  `dataset == glue_database`) or `_build_redshift_source` (a scoped `redshift-data`
  client via `_redshift_session_policy`; connection routing read from the
  descriptor). Absent descriptor → back-compat default to a glue source named by the
  dataset. `okf_core.sources` now carries the `redshift` type + `redshift_database`
  key. Both source paths keep the fail-open-to-ambient dev behavior.

- **Registration, infra, and UI** (Phase E) — dataset **registration** is now
  source-generic: `PUT /domains/{d}/datasets/{ds}` accepts any supported `source`,
  `assert_source_registrable` applies per-source rules (Glue keeps
  `dataset == glue_database` + a live probe; Redshift allows a distinct dataset name
  and no probe), and `upsert_domain_mapping` stores the descriptor generically
  (flat `glue_database` mirror for Glue only). Terraform adds `var.enable_redshift`
  — the single feature flag — which gates the Redshift IAM grants on both the
  harvest data role and the Control API role (no deploy-time connection config; the
  mapping is self-describing). The UI mapping dialog offers Redshift and posts a
  `redshift` source object.
- **Dynamic Redshift connection selection** (Phase E+) — a Redshift mapping is
  **self-describing**: the descriptor carries `cluster_identifier`/`workgroup_name`
  + `secret_arn`, so any cluster/workgroup in the account is harvestable without
  redeploying env. The UI fills this from two read endpoints —
  `GET /redshift/clusters` (lists provisioned clusters + Serverless workgroups via
  the control plane) and `GET /redshift/databases?cluster=…|workgroup=…&secret_arn=…`
  (lists databases within a chosen target via the Data API `ListDatabases`).
  `harvest.clients._build_redshift_source` reads the connection ENTIRELY from the
  descriptor — there is no deploy-time connection env. A db-only descriptor
  (no cluster/workgroup + secret) is rejected at registration
  (`assert_source_registrable` → 400), so an unharvestable mapping never reaches
  the runtime; a legacy stored row without a connection still fails cleanly
  (`RedshiftSource` raises).
- **Chat SQL dispatch** — the chat agent's optional `run_sql` tool picks its
  engine from the conversation's `@`-scoped mapping: a Redshift-backed dataset
  gets a Data API engine pinned to that mapping's connection (Redshift dialect
  prompt/description); everything else keeps the catalog-wide Athena engine. A
  Redshift scope on a deployment without `var.enable_redshift` gets NO SQL tool
  rather than a wrong-backend Athena fallback. See `docs/CHAT_AGENT.md` §14b.

The one remaining Glue-only surface is the **`incremental` change-detection
trigger**: it fires on `aws.glue` catalog events and resolves by Glue database name,
so it only covers Glue datasets. A Redshift dataset carries no flat `glue_database`
mirror and is intentionally skipped by that path (`iter_dataset_mappings`) — it is
refreshed by full/scheduled harvests instead. Redshift has no equivalent
catalog-event source; a polling-based Redshift freshness path would be future work.

## Security model (Redshift)

The Redshift path has a different containment story than Glue/Athena — three
things carry it:

1. **Read-only is the SECRET'S DB USER, not IAM.** Athena reads are bounded by
   IAM (the roles carry no write grants). Redshift SQL executes with the SQL
   privileges of the DB user inside the mapping's Secrets Manager secret — IAM
   cannot make a Data API statement read-only. The harvest agent's `run_sql` and
   the chat `run_sql` both send model-authored SQL, so **provision each
   connection secret with a least-privilege, read-only database user** (e.g.
   `GRANT SELECT` / `GRANT USAGE` only). The chat tool additionally rejects
   non-`SELECT`/`WITH`/… statements up front, but that guard is defense in
   depth, not the boundary.
2. **Secrets are scoped by name prefix.** Per-mapping secrets can't be
   enumerated at deploy time, so the `secretsmanager:GetSecretValue` grants
   (harvest data role, Control API role, chat role) are scoped to the
   `var.redshift_secret_name_prefix` pattern (default `okf-`) instead of `"*"`.
   Only secrets deliberately named for this system are usable — name connection
   secrets accordingly (e.g. `okf-warehouse-ro`). The harvest additionally pins
   its per-invocation STS session policy to the ONE secret of the run
   (`clients._redshift_session_policy`).
3. **The pickers exercise (never reveal) secrets.** `GET /redshift/databases`
   connects with whatever prefix-matching secret ARN the console user supplies.
   The secret VALUE is never returned, but any authenticated console user can
   *use* any `okf-`-prefixed secret to list databases — the prefix is the blast
   radius. Auth grants are deliberately minimal: no
   `redshift:GetClusterCredentials` / `redshift-serverless:GetCredentials`
   anywhere (secret auth is the only wired mode; temp-credential grants would be
   dead privilege).

## Coupling scorecard

| Component | Status | To become multi-source |
|---|---|---|
| `okf_core/sources.py` | **Done (Phases C/D)** — `glue` + `redshift` types, config keys, validate branches | Add `SOURCE_TYPE_<X>` + config keys + validate branch |
| `okf_core/concept_types.py` | **Done (Phases B/C)** — type registry + `is_schema_bearing_type` (Glue + Redshift) | Register the new source's concept types |
| `okf_core/guard.py` | **Done (Phase B)** — routes on the predicate | — |
| `okf_core` embedding/index/paths/domain | Agnostic | — |
| `control_api` registration item shape | **Done (Phase E)** — `upsert_domain_mapping` stores any `source` generically | — |
| `control_api` registration guards | **Done (Phase E)** — `assert_source_registrable` (per-source rules) | Add a branch for the new source |
| `control_api` `/glue/databases` (UI picker) | Glue-specific (a picker convenience) | Add a per-source picker if the source enumerates |
| Harvest invocation payload | **Done (Phase D)** — carries the `source` descriptor from all 3 trigger sites | — |
| `harvest/source_base.py` | **Done (Phase A)** — `Source` protocol + `ConceptRef` + `SourceMetadataProfile` | Implement `Source` for the new backend |
| `harvest/glue_source.py` | Implements `Source`; emits shared type constants | (reference implementation) |
| `harvest/redshift_source.py` | **Done (Phase C)** — `RedshiftSource` (Redshift Data API) | (reference implementation) |
| `harvest/clients.py` | **Done (Phase D)** — `build_source` dispatches on `type`; per-source session policy | Add a `_build_<x>_source` + session policy |
| `harvest/metadata_export.py` | **Done (Phase B)** — labels driven by `SourceMetadataProfile` | Provide a profile on the new source |
| `okf_core/hive_types.py` | Hive-only | Add a per-source type flattener |
| `harvest/prompts.py` | **Done** — token-filled per run from the source's `SourcePromptProfile` | Provide a `prompt_profile` on the new source |
| chat `run_sql` (`chat/sql.py`, `server.py`) | **Done** — engine dispatched on the @-scope's source descriptor (Athena default, Redshift pinned) | Add an engine + dispatch branch if the source supports live SQL |
| `incremental` + `infra/compute/incremental.tf` | **Glue-only by design** — `aws.glue` event trigger; non-glue rows skipped | No Redshift catalog-event source — full/scheduled harvests only |
| `reindex`, `consumption_mcp` | Agnostic | — |
| Terraform IAM | **Done (Phase E)** — `var.enable_redshift` gates the per-source grants (no connection env; mappings self-describe) | Add gated per-source grants |
| UI (`MappingsView.jsx`, `api.js`) | **Done (Phase E)** — Redshift enabled (db + dataset-name inputs) | Add the source's connection fields |

## Recipe: adding a new data source

Ordered so each step is independently testable offline; the Redshift source is
the worked example throughout. Steps marked ✅ are already built (the shared
machinery exists; a new source only plugs into it).

1. ✅ **Registry vocabulary** (done for Glue + Redshift) — in `okf_core/sources.py`
   add `SOURCE_TYPE_<X>`, its config keys, a `build_<x>_source`, and a
   `validate_source` branch; extend `SUPPORTED_SOURCE_TYPES`; add tests to
   `okf_core/tests/test_sources.py`. `build_redshift_source` is the worked example.
2. ✅ **`Source` protocol** (done, Phase A) — `harvest/source_base.py` defines the
   metadata-reader + query-engine contract, `ConceptRef`, and `SourceMetadataProfile`.
   The runner/agent/export/tools depend on `Source`, not the concrete class. A new
   source just implements this protocol.
3. ✅ **Parameterized Glue-shaped bits** (done, Phase B) — concept `type` strings live
   in `okf_core/concept_types.py` (register the new source's types there); `guard.py`
   routes on `is_schema_bearing_type`; `.metadata/` labels come from the source's
   `SourceMetadataProfile`; the harvest prompts fill their source facts (dialect,
   adapter, `type` strings, resource form) from the source's `SourcePromptProfile`.
   Still per-source: the type flattener (`hive_types.py` is Hive-only — add one
   for the new source if its type grammar nests).
4. ✅ **Implement the source** (done for Redshift, Phase C) — a new
   `harvest/<x>_source.py` implementing `Source` (including a `metadata_profile` and
   its concept types registered in step 3); follow the matching
   `skills/.../references/sources/<x>.md` adapter for `type` values, `resource`
   form, type vocabulary, and quoting. `harvest/redshift_source.py` +
   `tests/test_redshift_source.py` are the worked example (Redshift Data API over
   `SVV_*` views, with an in-memory `FakeRedshiftData`).
5. ✅ **Source selection + credentials** (done, Phase D) — the `source` object rides
   the harvest invocation payload from all three trigger sites, and
   `clients.build_source` dispatches on `source["type"]` with a per-source scoped
   session policy (`_build_redshift_source` + `_redshift_session_policy` are the
   worked example). Fail-open-to-ambient dev path preserved.
6. ✅ **Registration + infra** (done, Phase E) — registration is source-generic:
   add a branch to `assert_source_registrable` for the new source's rules (Glue
   requires `dataset == glue_database` + a live probe; Redshift allows a distinct
   dataset name, no probe). `upsert_domain_mapping` already stores any descriptor.
   Add gated Terraform IAM grants + runtime env (mirror `var.enable_redshift` in
   `agentcore_iam.tf` / `agentcore_runtimes.tf` / `variables.tf`) and a UI branch in
   `MappingsView.jsx` + `api.js` (Redshift is the worked example).
7. **Change detection** — the `incremental` path is Glue-only (it fires on
   `aws.glue` catalog events). A source with no equivalent catalog-event stream
   (Redshift) is intentionally excluded by `iter_dataset_mappings` (it filters on
   the flat `glue_database` mirror, which only Glue writes) and is refreshed by
   full/scheduled harvests instead. Add a source-specific freshness trigger only if
   the backend offers one.

## Related docs

- `docs/CONVENTIONS.md` — the registry item shape, the `source` descriptor, and the
  harvest invocation payload.
- `docs/ARCHITECTURE.md` — how the seven components fit.
- `services/harvest/skills/okf-authoring/references/sources/index.md` — the
  authoring-side adapter recipe.
