# Conventions — the contract between services

Every service depends on these shapes; a mismatch is an integration bug rather
than a local one. `okf_core` is the shared library that encodes most of them —
import from it instead of re-implementing.

## S3 bundle bucket layout (source of truth)

```
okf/<data_domain>/
├── _domain/overview.md              # type: Domain (declared-domain concept doc)
└── <dataset>/
    ├── index.md                     # auto-generated (regenerate_indexes)
    ├── datasets/<dataset>.md        # type: Glue Database
    ├── tables/<table>.md            # type: Glue Table (one per table)
    ├── references/<type>/<slug>.md   # type: Reference — canonical fact-typed
    │                                 #   folders: joins/ metrics/ enums/
    │                                 #   named_sets/ glossary/ known_issues/
    │                                 #   (one doc per item; see okf-authoring skill)
    ├── .context/                     # user-uploaded source docs (persisted)
    ├── .metadata/                    # read-only Glue metadata snapshot (per run)
    └── .harvest/state.json           # commit marker (status: complete | in_progress)
```

The `_domain/overview.md` doc is a **derived** materialisation of the declared
domain's description + context. Written THROUGH the harvest mount (uid 1000) on
`PUT /domain-defs/{domain}` so the `<domain>/` directory is established with
correct ownership before any dataset-level write. `_domain` is a reserved
pseudo-dataset that `parse_bundle_key` parses normally (3 segments:
`domain/_domain/overview.md`) and reindex embeds with `type=Domain`. Hidden from
the dataset listing by `is_domain_dataset()`. Vector key:
`<domain>/_domain/overview`.

- **Concept id** is the path under `okf/<domain>/<dataset>/` minus `.md`, e.g.
  `tables/races`. Use the `okf_core.paths` helpers.
- **S3 object key** is `okf/<domain>/<dataset>/<concept_id>.md`.
- **Vector key** is `<domain>/<dataset>/<concept_id>` — the S3 key without the
  `okf/` prefix and `.md` suffix. Use `okf_core.embedding.vector_key`.
- `.context/`, `.metadata/`, and `.harvest/` are dot-prefixed and are not
  concepts. The reindex worker ignores any key with a dot-prefixed segment below
  the dataset root, and ignores `index.md` and `log.md`.
- `.metadata/` is a **read-only Glue metadata snapshot** the harvest writes ONCE
  at the start of each run (`harvest/metadata_export.py`): `index.md` (manifest),
  `database.md`, `columns.tsv` (one line per `table\tcolumn\ttype\tcomment` — the
  cross-table grep target for join/near-synonym discovery), and `tables/<t>.md`
  per table. The agent reads it with the built-in `read_file`/`glob`/`grep` (it
  replaced the old `list_concepts`/`read_concept_raw` tools); the OKF write-guard
  refuses any write into it. Live verification stays on the `sample_rows`/
  `run_sql` tools. Like `.context/`, it is a harvest INPUT and is never published,
  indexed, or embedded; `clean_authored_output` preserves it (dot-prefixed), and
  `export_metadata` rewrites it fresh each run so a dropped table leaves no stale
  sheet.
- A bundle is consumable only once `.harvest/state.json` exists with
  `status == "complete"`.

## S3 Vectors (one bucket, one index)

See `okf_core.embedding`.

- 512 dims, cosine, float32. Non-filterable metadata keys are `title`,
  `description`, `s3_key`. These are immutable in S3 Vectors.
- Filterable metadata: `data_domain`, `dataset`, `table`, `type`, `tags`.
- Embed text and metadata come from `build_embed_text`,
  `build_filterable_metadata`, and `build_non_filterable_metadata`.
- Any query that filters or returns metadata needs both
  `s3vectors:QueryVectors` and `s3vectors:GetVectors`.

## DynamoDB tables

Two tables; names come from env vars, with the defaults shown.

### `okf-registry` — domain registry, harvest status, credentials

Partition key `pk` (S), sort key `sk` (S). Item shapes:

**Declared domain.** `pk = "DOMAIN#<data_domain>"`, `sk = "META"`, attrs
`{data_domain, description, context, created_at, updated_at}`. A first-class,
operator-declared entity: domains must be declared before Glue databases can be
mapped into them. `description` is a short one-liner; `context` is richer prose
(used in the harvest prompt and exposed to agents over MCP). Listing
(`GET /domain-defs`): scans `pk begins_with "DOMAIN#"` with `sk = "META"`.
Deletion (`DELETE /domain-defs/{domain}`) is blocked (409) while `DATASET#`
mappings still exist under the same partition. On declare/update, a derived
concept doc is written through the harvest mount at
`okf/<domain>/_domain/overview.md` (see S3 layout below) so the domain is
embedded and semantically searchable.

**Domain mapping.** `pk = "DOMAIN#<data_domain>"`, `sk = "DATASET#<dataset>"`,
attrs `{data_domain, dataset, source, glue_database, created_at}` plus optional
dataset-guidance attrs `{guidance, guidance_updated_at, guidance_applied_version}`
(shared authoring instructions; see `okf_core.guidance` + the harvest payload's
`dataset_guidance` above). Requires a
pre-existing `META` row for the same `pk` (enforced by `assert_domain_declared`
in the upsert adapter).

Optionally also carries **recursive-improvement settings** under a single nested
`recursive_improvement` map: `{enabled, questions_key, max_iterations}` (see
`docs/BENCHMARK_GUIDE.md` and the harvest payload's `recursive_improvement`
block below). The stop target is **fixed** (judge accuracy ≥ 90%), so it is NOT a
setting — legacy `ex_threshold`/`judge_threshold`/`gate_kpis` keys are ignored by
the validator. These are the
dataset's saved benchmark config: when `enabled` is true the Control API copies
this map (validated) into the harvest invocation payload on **every** trigger for
the dataset — `full`, `incremental`, and `annotated` alike — so a dataset once
configured keeps benchmarking on each re-harvest without re-uploading the CSV.
Absent or `enabled=false` ⇒ the feature is entirely inert for the dataset.

`source` is the first-class, future-extensible **source descriptor** — a nested
map `{type, ...type-specific config}` naming WHERE the dataset's data lives and
how the harvester reads it. The vocabulary lives in `okf_core.sources`
(`SUPPORTED_SOURCE_TYPES`, `DEFAULT_SOURCE_TYPE`); today the only supported type
is `glue`, whose config is `{"type": "glue", "glue_database": "<db>"}`. New
source types (Redshift, BigQuery, …) add a type + config keys there with no item-
schema migration. The flat top-level `glue_database` attribute is **also written**
as a back-compat mirror of the glue source's config: the harvest invocation
payload and the incremental scan (`incremental/store.py`, which filters on
`glue_database`) read it directly, so they need no change in lockstep. Readers go
through `okf_core.normalize_source`, which reconciles both the new nested shape
and pre-`source` rows (flat `glue_database` only) into one `{type, ...config}`
dict. The Control API validates the type on write (`PUT
/domains/{domain}/datasets/{dataset}` accepts either a `source` object or a bare
`glue_database`) and rejects any unsupported type with `400`.

**Harvest status.** `pk = "HARVEST#<data_domain>#<dataset>"`, `sk = "STATUS"`,
attrs `{status: queued | running | complete | failed | cancelled, mode,
started_at, updated_at, detail, runtime_session_id, model, effort}`. `model` and
`effort` record the RESOLVED LLM config the run actually used (override or
deploy-time default); the runtime stamps them on the `running` transition
(`harvest.status.report_status`), so they're empty on a still-`queued` row.
`cancelled` is a terminal
status set by the Control API's `cancel_harvest` (`POST
/harvest/{domain}/{dataset}/cancel`): it `StopRuntimeSession`s the
`runtime_session_id` and flips the row with a conditional update
(`status IN (queued, running)`) so it never clobbers a `complete`/`failed` the
runner wrote first. Being terminal, it satisfies the lease-free predicate below,
so a retrigger is immediately allowed.

This row also serves as a per-dataset harvest lease. Every path that starts a
harvest — the Control API's `trigger_harvest` and the incremental orchestrator /
nightly reconcile's `process_event` — acquires the lease with a conditional
`PutItem` before invoking the runtime:

```
attribute_not_exists(pk) OR NOT (status IN (queued, running)) OR started_at < <now − 8h>
```

If a harvest for the dataset is already in flight, the second one is refused: the
Control API returns `409`, and the incremental path returns `skipped_locked`
without recording the new Glue version, so the change is picked up again by the
next event or the nightly reconcile. This keeps two runs from writing the same
bundle directory at once (one run's `clean_authored_output` deleting files while
the other writes them). A lease older than 8 hours
(`HARVEST_LEASE_STALE_SECONDS`, the AgentCore session cap) can be taken over, so
a dead job whose final status write was lost doesn't wedge the dataset forever. A
failed invoke marks the row `failed` to release the lease.

**Harvest live step feed.** Separate from the coarse status row, the harvest
runtime narrates its progress at message granularity. As the agent runs, a
LangChain callback (`harvest.steps.StepEmitter`, attached via
`config["callbacks"]` so it also observes every sub-agent) emits one stdout line
per step: `OKF_STEP <json>` where the JSON is
`{ts, data_domain, dataset, session_id, seq, kind, label, agent, tool?, ok?, full?}`.
`kind` ∈ `agent | tool_call | tool_result | subagent | usage`; `seq` is a 1-based
monotonic counter; `label` is a human phrase (tool calls are shaped, e.g.
"Reading `tables/races`", "Started `table-author`: …") — tool RESPONSE bodies are
never emitted, only success/failure. An `agent` event also carries **`full`**
(the complete markdown of the AIMessage, whitespace preserved, bounded ~8KB) when
it exceeds the one-line `label`; the UI renders `label` as inline markdown and
opens `full` in a modal on click. `tool_call`/`tool_result` share a `call_id`
so the UI folds them into one row. **`subagent`** events power the UI's fleet
squares (the dynamic reviewer/table-author fan-out): they carry
`{phase: start|complete|error, batch, sub_id, subagent_type?}` where `batch` is
the top-level `eval` tool-call id grouping one fan-out wave (NOT the event's own
`eval_id`, a REPL-local counter that resets to `call_0` on every `eval()` and so
can't tell one wave from the next — the emitter correlates each sub-agent to the
current top-level `eval` call_id) and `sub_id` is the per-dispatch id.
They come from `langchain_quickjs`'s custom stream (the run loop uses
`.stream(stream_mode=["custom"], subgraphs=True)`, since `.invoke()` drops these
into a no-op writer). The UI grows a row of squares as sub-agents START (there is
no reliable pre-start count — the model builds the fan-out list dynamically).
**`usage`** events carry a `usage` object with the **cumulative** token counts for
the whole run — `{input, output, cache_read, cache_write, total}` (`total` =
input+output) — accumulated across EVERY model turn including sub-agents (they
emit no feed row but dominate the spend). Fields mirror LangChain's normalized
`usage_metadata` (`cache_write` is its `cache_creation`, the Anthropic
prompt-cache WRITE; `cache_read` is a cache HIT). **`input` is the FULL input
count and already INCLUDES `cache_read` + `cache_write`** (per langchain_aws
`_extract_usage_metadata`, which sums `bedrock_input + cacheRead + cacheWrite`
into `input_tokens`), so `total` = `input` + `output` and cache is a *breakdown*
of input, never additive — the UI shows cache read/write as indented "of which"
children under Input, not sibling rows (listing them alongside double-counts). Counts are absolute, so the UI
renders the latest snapshot as a running total (a missed/re-ordered poll can't
corrupt it) and shows no feed row for the event. Metering is wired differently
from the other kinds: it rides a `UsageForwarder` callback on the **shared model
instance** (`build_harvest_agent(step_emitter=…)` → `_build_model(callbacks=…)`),
NOT the run-config `StepEmitter`. This is deliberate — QuickJS `task()` sub-agents
run on their own asyncio tasks and never reach the parent run's callbacks, but
they invoke the same inherited model, so only a model-instance callback sees
every turn. (`on_llm_end` on the run-config emitter must NOT meter, or sub-agent
turns are undercounted and supervisor turns double-counted.)
AgentCore ships stdout to the runtime's CloudWatch log group, so
this reuses existing storage (no new event store). The Control API's
`GET /harvest/{domain}/{dataset}/events?since=<seq>&since_ts=<ms>` reads it back
with `FilterLogEvents`, correlating by the run's `runtime_session_id` (on the
STATUS row), and returns `{events, next, next_ts, done}` (`done` once the status
is terminal). Two cursors the UI echoes back: `since`/`next` is the `seq`
high-water mark (exact dedup); `since_ts`/`next_ts` is the highest CloudWatch
event timestamp (ms), which bounds `FilterLogEvents`' `startTime` so each live
poll scans only a recent window instead of the whole run. On first load
(`since_ts=0`) the floor is the run's `started_at`, so a viewer who opens the
page mid-run backfills the whole current run. `OKF_STEP` is a frozen marker
shared by `harvest.steps` and `control_api.handlers`.

**Benchmark KPI (recursive improvement).** `pk = "HARVEST#<data_domain>#<dataset>"`,
`sk = "BENCH#<runtime_session_id>#<iteration>"`, attrs `{iteration,
runtime_session_id, ex_score, judge_accuracy, passed, failed, discarded, graded,
genuine_error_count, target_met, created_at}`. One row per benchmark round of a
recursive-improvement run (see `docs/BENCHMARK_GUIDE.md`); `graded = passed
+ failed`, `ex_score = passed / graded` (DISCARDED questions — gold that can't
bind to the schema — are excluded from both). `target_met` is true once judge
accuracy ≥ 0.9 (the fixed target) with EX > 0. A terminal
`sk = "BENCH#<runtime_session_id>#final"` row carries the shipped iteration's
numbers. (No checkpoint/restore — the agent owns the final bundle; whatever it
authored is what ships.) Written
**append-only** (`PutItem`, never read-modify-write) best-effort by the harvest
runtime — a KPI write must never crash a harvest, same discipline as
`report_status`. The `runtime_session_id` in the `sk` scopes rows to one run: a
reader gets *this* job's KPIs by reading the STATUS row's `runtime_session_id`
(same correlation the events feed uses) then `Query begins_with(sk,
"BENCH#<session>")`, so prior runs' rows can't be confused for the current one.
These rows sit on the same partition as the dataset's `STATUS` lease but never
collide with it (distinct `sk` prefix) and carry no lease semantics. Emitted live
in parallel as an `OKF_STEP` `kind:"benchmark"` event so the UI shows KPIs mid-run
(the CloudWatch feed is the live view; this row is the durable/queryable record).

**MCP credential.** `pk = "CRED#<client_id>"`, `sk = "META"`, attrs
`{name, client_id, created_by?, created_at}`. Metadata only — the client secret
is returned once at creation and never stored. This backs the credentials UI
(list and revoke); the credential itself is a Cognito M2M app client.
`created_by` is the owner, stamped from the caller's verified JWT identity
(`email`, falling back to `sub`), not the request body. Revoking
(`DELETE /credentials/{client_id}`) requires a matching `CRED#` row — so an
arbitrary app client, such as the public SPA login client, can't be deleted — and
when a caller identity is present it must equal `created_by`.

Listing: `list_domains` queries `pk begins_with "DOMAIN#"` AND
`sk begins_with "DATASET#"` (tightened so `META` rows are excluded);
`list_declared_domains` scans with `sk = "META"`;
`list_credentials` scans `pk begins_with "CRED#"`.

### `okf-freshness` — reindex and incremental dedup state

Partition key `pk` (S), sort key `sk` (S). Item shapes:

**Reindex dedup.** `pk = "VEC#<vector_key>"`, `sk = "SEQ"`, attrs
`{last_sequencer, updated_at}`. S3 `object.sequencer` values compare
lexicographically per key, so an event at or below `last_sequencer` is a
duplicate or replay and is ignored. `last_sequencer` is advanced (conditional
`PutItem`) only after the embed and `PutVectors`/`DeleteVectors` succeed, never
before — otherwise a transient failure would leave the marker ahead of the work,
and the SQS retry would skip the record as a duplicate and silently drop the
vector.

**Table version.** `pk = "TABLE#<data_domain>#<dataset>#<table>"`,
`sk = "VERSION"`, attrs `{version_id, update_time, last_seen_at}`. The
incremental path uses this to confirm a real change before re-harvesting.

### `okf-annotations` — user feedback on the wiki

Partition key `pk` (S), sort key `sk` (S). A separate table (not registry/
freshness) so its DynamoDB **TTL sweep** — on `expires_at` — can never reap a
durable row; the worst a stray `expires_at` can do is delete an annotation.

**Annotation.** `pk = "ANNO#<data_domain>#<dataset>#<user_sub>"`,
`sk = "<concept_id>#<annotation_id>"`, attrs `{data_domain, dataset, concept_id,
annotation_id, author?, quote, prefix?, suffix?, block_line?, note, status,
outcome?, resolution?, created_at, updated_at, expires_at?}`.

**Isolation is structural.** The author's immutable Cognito `sub` is baked into
the partition key, so a user's `Query` can only ever return their OWN annotations
— there is no cross-user read path (readers pass `user_sub` from the verified JWT,
never the body). `sub` (not `email`) is used because it never changes and is
`#`-delimiter-safe. `author` is the human-facing label (email) for display only.

**Anchoring is a quote, not a coordinate.** `quote` is the selected passage; the
UI grows `prefix`/`suffix` (see `okf_core.annotations.normalize_text` / the UI's
`minimalUniqueContext`) only until the `(prefix+quote+suffix)` window is unique in
the doc, so two identical quotes on a page are distinguishable. `block_line` is a
body-relative source-line HINT (from react-markdown's `node.position`, stamped as
`data-sl`) the agent can jump near — never the source of truth. A re-harvest
rewrites the doc, so any coordinate would go stale; the quote is what survives.

**Lifecycle.** `status` ∈ `open | in_review | resolved`; `outcome` (set with
`resolved`) ∈ `applied | rejected | orphaned`. `expires_at` (epoch seconds, 7-day
TTL — `okf_core.annotations.HISTORY_TTL_SECONDS`) is set ONLY at resolution, so an
open/in_review annotation never expires. The Control API's `run` pre-flight
(`POST /harvest/{domain}/{dataset}/annotations/run`) takes the per-dataset lease,
then for each of the caller's open annotations loads the target doc from S3 and
re-anchors the `quote` (`is_orphaned`): a note whose passage is gone is
auto-resolved `orphaned` (with `ORPHAN_RESOLUTION_MESSAGE`) and the agent never
sees it. If EVERY open note orphans (or none are open), the run is **skipped** —
the status row is set `complete` and the runtime is NOT invoked. Otherwise the
survivors are flipped `in_review` and sent in the `annotated` payload; on invoke
failure the Control API reverts them to `open` so no feedback is stranded. After
the run, the harvest RUNNER (not the agent — it has no DynamoDB tools) reconciles
the agent's on-mount verdict file to `resolved` with `outcome`+`resolution`, and
reverts any survivor the agent didn't rule on back to `open`.

CRUD: `GET|POST /annotations/{domain}/{dataset}` and
`DELETE /annotations/{domain}/{dataset}/{annotation_id}?concept=<id>` (the concept
id has slashes, so it rides in the query string, not a path segment).

## Harvest invocation payload

`InvokeAgentRuntime(agentRuntimeArn=<harvest arn>, runtimeSessionId=<per-dataset
id>, payload=json.dumps({...}).encode())`, where the payload is either:

```json
{ "data_domain": "sales", "dataset": "orders", "mode": "full",
  "model": "openai.gpt-5.6-sol", "effort": "xhigh",
  "domain_description": "Revenue & order pipelines",
  "domain_context": "Covers all B2C sales; refunds excluded." }
```

(`model`/`effort` optional — see below.) Or, for an incremental run:

```json
{ "data_domain": "sales", "dataset": "orders", "mode": "incremental",
  "changed_table": "customers",
  "diff": { "added": [], "removed": [], "retyped": [] },
  "domain_description": "Revenue & order pipelines",
  "domain_context": "Covers all B2C sales; refunds excluded." }
```

or, for an annotation run (apply a user's wiki feedback in place):

```json
{ "data_domain": "sales", "dataset": "orders", "mode": "annotated",
  "user_sub": "<cognito sub>",
  "annotations": [
    { "annotation_id": "…", "concept_id": "tables/orders",
      "quote": "one row per order", "prefix": "", "suffix": "",
      "block_line": 12, "note": "grain is per line-item, not per order" }
  ],
  "domain_description": "Revenue & order pipelines",
  "domain_context": "Covers all B2C sales; refunds excluded.",
  "dataset_guidance": "Ignore the staging_* tables; status is decoded in the dictionary.",
  "dataset_guidance_version": "2026-07-17T09:00:00+00:00" }
```

`dataset_guidance` (optional, on every mode) is the dataset's shared authoring
guidance — persistent, editable operator instructions (registry
`DATASET#` row: `guidance`, `guidance_updated_at`, `guidance_applied_version`).
It steers the harvest prompt; on a SUCCESSFUL run the runner stamps
`guidance_applied_version = dataset_guidance_version` so the guidance clears its
DIRTY state (`okf_core.guidance.is_dirty`). An `annotated` run is invoked when
there are live annotations **or** the guidance is dirty — so editing guidance and
re-running applies it even with zero annotations (a guidance-only re-harvest,
`annotations: []`).

`recursive_improvement` (optional, on `full`/`incremental`/`annotated`) enables
the benchmark-driven improvement loop for the run (see
`docs/BENCHMARK_GUIDE.md`). Its **presence is the enable signal** — absent
⇒ the feature is entirely inert and the harvest is unchanged. The block:

```json
{ "data_domain": "sales", "dataset": "orders", "mode": "full",
  "recursive_improvement": {
    "questions_key": "benchmark/sales/orders/questions.csv",
    "max_iterations": 5 } }
```

`questions_key` is the S3 key of the uploaded `question,gold_sql` CSV. It lives
under the **off-mount** `benchmark/<domain>/<dataset>/` prefix — deliberately NOT
under `okf/` (which the harvest S3 Files mount is rooted at), so the gold SQL is
invisible to every LLM role's file tools (supervisor + authoring subagents
included, not just the solver). The runner fetches it via boto3 `GetObject` into
the benchmark tool's process memory; it is never written to the mount. This
requires `s3:GetObject` on `<bundle-bucket>/benchmark/*` for the harvest runtime
role. `max_iterations` is the
benchmark→revise round budget (2–5, **clamped to 5** by the Control API). The stop
**target is fixed** — judge (adjudicated) accuracy ≥ 90% with EX > 0 — and is not
configurable (the point is to improve the wiki, not to tune a benchmark score).
The Control API validates this block at the trust boundary — clamps
`max_iterations`, requires `questions_key` when enabled — and ignores any legacy
`ex_threshold`/`judge_threshold`/`gate_kpis` keys. It sources the block
from the dataset's saved `recursive_improvement` settings on the `DATASET#` row
(so it rides along on every trigger for a configured dataset). Question count is
inferred from the CSV and **hard-capped at 100** (first 100 in CSV order) inside
the harvest tool. Questions the reviewer classifies as noisy/ambiguous gold are
**pruned** from later rounds (they're not wiki defects). All benchmark LLM roles
reuse the run's single `chat_model` instance (no separate model/effort), so
benchmark token usage folds into the run's cumulative total automatically.

**Per-round review artifact (off-mount, human-facing).** Each round the harvest
runtime writes one JSON to `benchmark/<domain>/<dataset>/reviews/<runtime_session_id>/<iteration>.json`
(same off-mount prefix as the CSV — it carries gold SQL, so no LLM role may read
it). Shape: `{iteration, counts: {<bucket>: n}, questions: [{q_id, bucket,
question, gold_sql, predicted_sql, note, reason}]}`, where `bucket` ∈ `passed`,
`genuine_error`, `noisy_gold`, `ambiguous`, `unknown`, `discarded`. It is served
ONLY to the UI via the Cognito-authed Control API route `GET
/benchmark/{domain}/{dataset}/reviews/{session}/{iteration}` (the round-done
`kind:"benchmark"` feed event sets `has_review` + `runtime_session_id` so the UI
knows to offer it). It is NEVER part of the `run_benchmark` tool return — the agent
still sees only aggregate KPIs + anonymous `improvements`.

or, for writing/refreshing a domain's concept doc through the mount:

```json
{ "data_domain": "sales", "mode": "write_domain_doc",
  "description": "Revenue & order pipelines",
  "context": "Covers all B2C sales; refunds excluded." }
```

The `annotated` payload carries only the LIVE annotations (the Control API's
pre-flight already resolved any orphans) plus the `user_sub` needed to reconstruct
each annotation's DynamoDB key for the runner's write-back. The agent assesses
each note against live data, edits the doc when it holds up (augmentation guard
applies), and writes a per-annotation `{outcome, comment}` verdict to
`.harvest/annotation_results.json` on the mount; the runner reconciles that to the
`okf-annotations` table. It reuses the incremental path's scoped, in-place
approach (no `clean_authored_output`) and the deterministic per-dataset session id.

`domain_description` and `domain_context` are optional enrichment keys added by
the Control API (and the incremental orchestrator) from the `DOMAIN#/META` row.
They are threaded into the harvest prompt so authoring is domain-aware. The
`write_domain_doc` mode writes `<mount>/<domain>/_domain/overview.md` through
the mount (uid 1000 safe) and returns synchronously.

`model` and `effort` are optional per-harvest overrides for the LLM (chosen in
the UI's harvest-settings picker; `full`/`incremental` only). When present the
runtime uses them; when absent it falls back to the deploy-time `OKF_HARVEST_MODEL`
/ `OKF_HARVEST_EFFORT` env. The Control API **validates the pair against the model
catalog** (`OKF_HARVEST_MODEL_CATALOG`, from `var.harvest_model_catalog`) before
invoking — an unknown model or an effort not offered for that model is a `400`,
and `effort` without `model` is a `400`. This is the trust boundary: `model`
reaches `bedrock:InvokeModel`, and the runtime deliberately does not allow-list
effort itself. The catalog (a JSON array of `{model, label, efforts,
default_effort}`) is the single source of truth, shared by the Control API
(validation, raw JSON env) and the UI (`VITE_HARVEST_MODEL_CATALOG`, base64 —
see below) and defined in `okf_core.harvest_models`.

Build `runtimeSessionId` with `okf_core.runtime_session_id(...)`, not a bare
`"<domain>__<dataset>"` — AgentCore requires 33–256 characters, so the helper
appends a sha256 suffix to a readable `okf-<domain>-<dataset>-` prefix.

- **Incremental** uses a deterministic id (`runtime_session_id(domain, dataset)`)
  for one session per dataset and microVM affinity. It re-authors the changed
  table and its backlinks in place and leaves the rest of the bundle alone.
- **Full** uses a fresh id per trigger (`unique_token=uuid4().hex`), because a
  one-shot batch job wants a new microVM with a clean S3 Files mount rather than
  reattaching to a warm one (AgentCore reuses a microVM per session id until it
  stops). A full harvest is a clean rebuild: `run_full_harvest` marks the bundle
  in-progress, then `fsutil.clean_authored_output` deletes all prior authored
  output (`datasets/`, `tables/`, `references/`, `index.md`, `log.md`) before the
  agent re-authors. A table dropped from Glue leaves no stale doc, and its vector
  is pruned through the S3 write-through → `ObjectRemoved` → reindex
  `DeleteVectors`. `.context/` (user input) and `.harvest/` (the commit marker)
  are preserved. The rule is: delete every top-level entry whose name does not
  start with `.`.

## Environment variables

| Variable | Meaning |
|---|---|
| `AWS_REGION` | region for all clients |
| `OKF_ACCOUNT_ID` | account id (for building Glue ARNs) |
| `OKF_BUNDLE_BUCKET` | S3 bundle bucket name |
| `OKF_VECTOR_BUCKET` | S3 Vectors bucket name |
| `OKF_VECTOR_INDEX` | S3 Vectors index name |
| `OKF_REGISTRY_TABLE` | DynamoDB registry table (default `okf-registry`) |
| `OKF_FRESHNESS_TABLE` | DynamoDB freshness table (default `okf-freshness`) |
| `OKF_ANNOTATIONS_TABLE` | DynamoDB annotations table (default `okf-annotations`) — user-scoped wiki feedback + the harvest runner's resolution write-back |
| `OKF_HARVEST_RUNTIME_ARN` | AgentCore harvest runtime ARN |
| `OKF_ATHENA_OUTPUT` / `OKF_ATHENA_WORKGROUP` | Athena results |
| `OKF_MOUNT_PATH` | S3 Files mount (default `/mnt/data`) |
| `OKF_CODE_INTERPRETER_ID` | AgentCore Code Interpreter id backing the harvest agent's `run_code` tool (extracts text from binary `.context/` docs). A network-isolated SANDBOX-mode interpreter. Unset → harvest runs without `run_code` (text-only `.context` reading) |
| `OKF_ENABLE_LAKEFORMATION` | Set (`"true"`) when the harvested Glue catalog is Lake Formation-governed → adds `lakeformation:GetDataAccess` to the harvest data role's per-invocation session policy so LF can vend S3 creds for governed table data. Set by `var.enable_lakeformation`; requires adopter-side LF grants + data-location registration (see `docs/LAKE_FORMATION.md`). Unset → plain IAM catalog access |
| `OKF_HARVEST_MODEL` | harvest model id — the **fallback default** used when a harvest request omits `model` (default `us.anthropic.claude-opus-4-8`). An `anthropic.*` id runs on the Bedrock **Converse** API (`ChatBedrockConverse`); an `openai.*` / `gpt-*` id (e.g. `openai.gpt-5.6-sol`) runs on the Bedrock **Mantle** OpenAI-compatible endpoint (`ChatOpenAI`, bearer-token auth via `aws_bedrock_token_generator`). The prefix selects the provider; see `agent._build_model` |
| `OKF_HARVEST_MODEL_CATALOG` | (Control API) JSON array of `{model, label, efforts, default_effort}` — the models + efforts the UI picker offers and the Control API validates a per-harvest `model`/`effort` against. From `var.harvest_model_catalog`; unset → `okf_core.harvest_models.DEFAULT_CATALOG`. The UI receives the same catalog **base64-encoded** as `VITE_HARVEST_MODEL_CATALOG` (base64 so it survives `deploy.sh`'s `eval "export k=v"`) |
| `OKF_HARVEST_MANTLE_REGION` | AWS region for the Bedrock Mantle endpoint when `OKF_HARVEST_MODEL` is a GPT id (default `us-east-2`). **Independent of `AWS_REGION`** — GPT-5.x on Mantle is only in us-east-2/us-west-2, while the harvest runtime may deploy elsewhere. Drives both the Mantle base URL and the region the bearer token is minted for. Ignored on the Converse path |
| `OKF_HARVEST_MANTLE_USE_RESPONSES_API` | selects the Mantle API surface (default `true` → OpenAI **Responses** API on the `/openai/v1` path, which is what GPT-5.x requires). Set `false` for a gpt-oss model (Chat Completions on `/v1`). GPT path only |
| `OKF_HARVEST_MANTLE_BASE_URL` | override for the Mantle base URL (default `https://bedrock-mantle.<region>.api.aws/openai/v1` for Responses, `.../v1` for Chat Completions; region from `OKF_HARVEST_MANTLE_REGION`). GPT path only |
| `OKF_HARVEST_MANTLE_READ_TIMEOUT` / `OKF_HARVEST_MANTLE_MAX_ATTEMPTS` | httpx read timeout (s) and retry budget for the `ChatOpenAI` Mantle client (defaults `600` / `5`, mirroring the Converse knobs). The botocore `OKF_HARVEST_BEDROCK_*` knobs do NOT apply to the GPT path |
| `OKF_HARVEST_EFFORT` | reasoning effort. On Converse, passed verbatim to Bedrock `output_config.effort` (default `xhigh`; valid values are model-specific). On the GPT path it maps onto OpenAI's `reasoning_effort` scale — verbatim on GPT-5.6 (which added `max` above `xhigh`), so `low`/`medium`/`high`/`xhigh`/`max` all pass through unchanged. Which efforts a given model accepts is model-specific (an older GPT id rejects `max`); the model catalog is the trust boundary that only offers a level a model supports |
| `OKF_HARVEST_MAX_TOKENS` | harvest model max output tokens. Default is provider-aware when unset: `128000` for Converse (Opus 4.8), `32000` for GPT. An explicit value always wins |
| `OKF_HARVEST_MAX_SUBAGENT_CONCURRENCY` | how many dynamic subagents run at once on a `task()` fan-out (default `5`). This lowers langchain_quickjs's per-REPL `task()` semaphore, so a `Promise.all` keeps at most this many crawls in flight and queues the rest. It is not `config.max_concurrency` — the fan-out is a QuickJS `Promise.all`, not a LangGraph batch, so only the semaphore bounds it. |
| `OKF_HARVEST_BEDROCK_READ_TIMEOUT` | botocore read timeout in seconds for the harvest bedrock-runtime client (default `600`). Botocore's 60s default is too low: one xhigh Opus 4.8 turn can generate for minutes, and a slow Converse response would otherwise raise `ReadTimeoutError` and fail the harvest. |
| `OKF_HARVEST_BEDROCK_CONNECT_TIMEOUT` | botocore connect timeout in seconds (default `10`) |
| `OKF_HARVEST_BEDROCK_MAX_ATTEMPTS` | botocore `retries.max_attempts` in adaptive mode (default `5`); retries transient throttles and timeouts instead of failing the run |
| `OKF_BENCHMARK_MAX_CONCURRENCY` | how many benchmark solver ReAct loops run at once inside the `run_benchmark` tool (default `10`). Its own `asyncio.Semaphore`, independent of the `task()` subagent cap — each solver is one in-flight model request at a time, so this is the peak concurrent Bedrock requests from the benchmark. All hit the same account model quota (the shared client's adaptive retry absorbs throttles); raise on generous quota, lower on `ThrottlingException`. Recursive-improvement runs only. |
| `OKF_BENCHMARK_ATHENA_CONCURRENCY` | how many benchmark grader queries (predicted/gold SQL) run against Athena at once (default `15`); size under the Athena workgroup's concurrent-DML limit. Recursive-improvement runs only |
| `OKF_BENCHMARK_RECURSION_LIMIT` | LangGraph `recursion_limit` for a recursive-improvement supervisor run (default higher than the standard `full` limit, since the in-run loop consumes extra steps). Unset → the standard harvest recursion limit |
| `OKF_USER_POOL_ID` | Cognito user pool id (the Control API vends and revokes M2M app clients in this pool) |
| `OKF_MCP_SCOPE` | the custom scope (`okf-mcp/invoke`) granted to vended M2M clients; must match the consumption authorizer's `allowed_scopes` |
| `OKF_HARVEST_LOG_GROUP` | the harvest runtime's CloudWatch log group the Control API reads to serve the live step feed (`GET /harvest/{domain}/{dataset}/events`). Derived by Terraform as `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` (overridable via `var.harvest_log_group`). Unset/incorrect → the feed returns an empty batch; status polling is unaffected |

## HTTP and auth

- Control API and MCP requests carry `Authorization: Bearer <Cognito token>`.
- The API Gateway HTTP API JWT authorizer uses audience = app client id, issuer =
  `https://cognito-idp.<region>.amazonaws.com/<poolId>`.
- The consumption MCP AgentCore authorizer uses
  `discoveryUrl = <issuer>/.well-known/openid-configuration`. Inbound trust is
  scope-based (`allowedScopes = ["okf-mcp/invoke"]`), not a client allowlist, so a
  newly vended machine client is accepted with no infra change. `allowedAudience`
  is unusable here because Cognito M2M `client_credentials` access tokens carry no
  `aud`.

### MCP machine credentials (apps and agents)

- An `okf-mcp` resource server defines the `invoke` scope, giving the full scope
  string `okf-mcp/invoke`. The web SPA also carries this scope, so human sessions
  pass the same authorizer check.
- The Control API vends credentials as Cognito M2M app clients
  (`client_credentials` grant, `GenerateSecret=true`, scope `okf-mcp/invoke`):
  `POST /credentials {name}` returns `{client_id, client_secret}` once;
  `GET /credentials` returns metadata from the registry;
  `DELETE /credentials/{client_id}` deletes the app client and revokes it
  immediately. This needs IAM `cognito-idp:{Create,Delete,Describe}UserPoolClient`
  on the pool.
- To get a token, an app POSTs to the Cognito token endpoint with HTTP basic auth
  `client_id:client_secret` and body
  `grant_type=client_credentials&scope=okf-mcp/invoke`, then sends the resulting
  access token as `Authorization: Bearer <token>` to the MCP server. Tokens are
  short-lived (60 minutes) and meant to be cached; the token endpoint is capped at
  150 RPS per account and Region.
