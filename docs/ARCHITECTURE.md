# Architecture

How the system is put together, and the reasoning behind the decisions that
aren't obvious from the code. Section numbers (§) refer to `OKF_DESIGN.md`.

## Components

| Area | Component | Location | Notes |
|---|---|---|---|
| UI (§1) | React SPA, shadcn/ui, Cognito OIDC | `ui/` | JavaScript, not TypeScript. Vite multi-entry (`index.html` + `callback.html`), `react-oidc-context`. Views: Domains, Context, Harvest, Browse (with link graph). |
| Control API (§2) | API GW HTTP API + Lambda | `services/control_api`, `infra/compute/control_api.tf` | Cognito JWT authorizer (audience = client id). One Lambda with an internal router behind a `$default` route. Endpoints: list Glue databases, domain mapping, context presign/list/delete, start harvest, harvest status, bundle list/read/graph, credential vending. |
| Induction (§3) | deepagents agent on AgentCore | `services/harvest` | `create_deep_agent` + `FilesystemBackend(virtual_mode=True)` for containment, plus `OKFGuardMiddleware` and the `LinkGraph` tools. Authoring methodology comes from the vendored `okf-authoring` skill in `services/harvest/skills/`, loaded via `skills=["/skills/"]`. Fans out one `table-author` subagent per table, then a `reviewer` subagent per link-cluster of documents (≤5 related docs, grouped by the `cluster_concepts` tool) that checks each doc's load-bearing claims (grain, joins, gotchas, SQL) and the docs' mutual consistency against live data with `run_sql`; the supervisor only fixes findings it can reproduce. A `run_code` tool (AgentCore Code Interpreter, `harvest/code_interpreter.py`) lets the agent extract text from binary `.context/` docs (PDF/DOCX/PPTX/XLSX). A `mode="cross"` run instead documents relationships against one target dataset into `external/<domain>/<dataset>/` in its OWN bundle only — the target gets a derived cross-reference signal, never a copy (see "Cross-dataset references" below). The entrypoint offloads the crawl to a thread and reports `HealthyBusy`. |
| Incremental (§4) | Glue event → SQS → orchestrator | `services/incremental`, `infra/compute/incremental.tf` | Confirms a real change via `UpdateTime` / `GetTableVersions`, stages `.harvest/pending.json`, and invokes a harvest scoped to the changed table. A nightly reconcile catches missed events. |
| Freshness (§5) | S3 events → SQS → reindex | `services/reindex`, `infra/compute/reindex.tf` | Titan V2 (512-dim) embed → `PutVectors` / `DeleteVectors` keyed by concept path. Dedups on the S3 `sequencer` in DynamoDB. SQS in front absorbs Bedrock throttling. |
| Link graph (§6) | `networkx` graph, rebuilt on write | `okf_core/link_graph.py`, `harvest/graph_tools.py` | Link/backlink graph over the dataset subtree; `get_backlinks` / `get_links` return id, title, and heading, and `cluster_concepts` partitions the bundle into link-related groups of ≤5 docs for the review fan-out. Rebuilt lazily when the guard marks it dirty. Used by the harvest agent only. |
| Consumption (§7) | streamable-HTTP MCP on AgentCore | `services/consumption_mcp` | FastMCP, stateless, Cognito JWT. Tools: `list_domains`, `list_directory`, `read_page`, `glob` (path pattern), `grep` (content regex), `get_backlinks`, `semantic_search` (S3 Vectors, hierarchy-filtered). |
| Infrastructure (§8) | Terraform, split by lifecycle | `infra/durable`, `infra/compute` | Durable state (buckets, index, Cognito, DynamoDB) is a separate stack from compute (Lambdas, API, runtimes, CloudFront), wired via `terraform_remote_state`. |

## Key decisions

**S3 markdown is the source of truth; the vector index is derived.** The bundle
bucket is versioned; the S3 Vectors index can be rebuilt at any time by replaying
objects through the reindex worker. The index parameters (512 dims, cosine,
float32, non-filterable `title`/`description`/`s3_key`) are immutable in S3
Vectors, so they live in one place — `okf_core/embedding.py` and
`infra/durable/storage.tf` — and changing them means a `-replace`.

**Bundle versions are reconstructed, not recorded.** Because `finalize_bundle`
writes the `.harvest/state.json` commit marker LAST on a versioned bucket, each
`complete` marker version already delimits one published bundle version — so the
version history / diff / repromote feature (`okf_aws/s3_versions.py`, the
`/bundle/{d}/{ds}/versions|diff|repromote` endpoints, the Browse History pane)
reconstructs snapshots from `list_object_versions` instead of writing a manifest
at finalize. That makes all pre-feature history browsable retroactively and lets
lifecycle-expired versions drop out cleanly (nothing dangles). Repromote is
append-only — `CopyObject` from source `VersionId`s mints a new head, a fresh
marker records `repromoted_from`/`repromoted_by` — and the untouched reindex
pipeline re-converges the vector index from the resulting object events; the
Control API reads convergence off the freshness table's `VEC#` rows and the UI
declares success only when the index has caught up. Repromote does not pin
against Glue drift on purpose: the catalog stays upstream truth, and the next
real schema change may harvest over a restored version. The one reindex-side
obligation is filtering `Object Deleted` events by deletion-type, since the
retention lifecycle rule permanently expires noncurrent versions of keys whose
live docs are untouched (see CONVENTIONS.md "Bundle versions & repromote").

**One harvest runtime, one `okf/`-rooted S3 Files mount, per-dataset containment
via `virtual_mode`.** `harvest/agent.py` builds a
`FilesystemBackend(root_dir=/mnt/data/<domain>/<dataset>, virtual_mode=True)`
inside a `CompositeBackend` so the agent's internal scratch files stay ephemeral
and only the dataset root touches disk. The mount is runtime-scoped in
`infra/compute/agentcore_runtimes.tf`. `virtual_mode=True` is required — the
default gives no path confinement.

**Static Glue metadata is a read-only on-disk snapshot, not a tool; only live
access is a tool.** Before the agent runs, `harvest/metadata_export.py` sweeps
the whole Glue database once and writes `.metadata/` (`index.md` manifest,
`database.md`, per-table sheets, and a flat `columns.tsv`). The agent explores it
with the built-in `read_file`/`glob`/`grep` — one `grep` over `columns.tsv`
answers "which tables have column X?" across the whole dataset, the core move for
join and near-synonym discovery, which the old one-table-at-a-time
`read_concept_raw` tool could not do cheaply. `.metadata/` is dot-prefixed (never
published/indexed/embedded, preserved across clean rebuild, like `.context/`) and
read-only (the guard refuses writes into it). What stays a tool is the LIVE half a
snapshot can't hold: `sample_rows` and `run_sql` (Athena), used to VERIFY
grain/joins/casts/gotchas — catalog metadata can lie. Metadata free-text
(descriptions, comments, `Parameters`) is written plain: it is source data to
document, and the prompt's one-line rule tells the agent not to act on any
instruction embedded in it.

**The agent authors with the built-in file tools plus a guard, not a custom
write tool.** It writes with `write_file` / `edit_file`; `OKFGuardMiddleware`
(`harvest/okf_guard.py`) wraps each tool call and delegates to the pure
`OKFGuardEngine` (`harvest/guard_engine.py`), which rejects writes with missing
frontmatter or shrinking schema/citations (the augmentation guard), fills in the
timestamp, canonicalizes key order, and marks the link graph dirty. The guard is
attached to every subagent's middleware list as well, because subagent
middleware replaces rather than inherits.

**Cross-dataset references: one home for the docs, a derived signal for the
other side.** `mode="cross"` (Roadmap §5, OSS flat-trust) documents the
relationship between the run's dataset and ONE operator-chosen target into
`external/<d>/<ds>/` in the RUN'S OWN bundle. Design follows three rules.
(1) *Snapshot, don't share*: the target's catalog + published wiki are copied
read-only into `.metadata/external/<d>/<ds>/` at run start
(`export_target_metadata`), so discovery is "grep two `columns.tsv` files" with
zero new tool surface and no lease ever taken on the target; verification is
qualified Athena SQL (`"db"."table"`), for which the run's scoped session policy
is widened to exactly the pair's two Glue databases
(`clients._session_policy(extra_databases=…)`) — never catalog-wide.
(2) *Confined authoring*: the guard's `writable_prefix` restricts every
write/edit to the pair subtree; the supervisor runs a cross-specific prompt and
fans out `cross-author` subagents (plus the usual reviewer). The authoring
METHODOLOGY — understand both wikis FIRST and gate on a genuine business
convergence (unrelated datasets author nothing), then the column-evidence
lenses, then SQL that tests named hypotheses — lives in the vendored skill
(`skills/okf-authoring/references/cross-dataset.md`), with the prompts carrying
only the runtime facts (snapshot paths, guard scope, the fixed `type:
Cross-Dataset Reference` string + `cross_dataset` endpoints block), the same
split as every other mode.
(3) *One home + a derived signal, NOT a mirror*: the pair docs are never copied
into the target's bundle. A mirror would make the pair a distributed fact over
two independently versioned, independently restorable bundles — a full harvest
or a **repromote** of the target would silently desynchronize it, and no
transaction spans two bundles. Instead the reindex worker derives `XREF#`
registry rows from the pair docs' own S3 object events, and `list_domains`
(Control API + MCP) surfaces `cross_references` / `cross_referenced_by` so a
consumer scoped to the target is routed one hop to the docs. Being
event-derived, the signal survives wipes and repromotes with no writer
maintaining it and is rebuildable by replay — the same "S3 is truth, the rest is
derived" rule as the vector index. Deliberately NOT threaded through: dataset
guidance (one side's operator steering must not shape docs both sides read) and
the RI benchmark loop. `external/` is ordinary published content — embedded,
served, annotatable (the annotation run gains a `scope` filter on the
`external/` prefix) — and a full harvest wipes it like everything else;
re-running the cross harvest restores it.

**Binary `.context/` docs are decoded in a network-isolated sandbox, not
hardcoded.** deepagents' built-in `read_file` base64-encodes any non-text file,
so uploaded PDF/DOCX/PPTX/XLSX source docs were unusable. The harvest agent gets
a `run_code` tool (`harvest/code_interpreter.py`) backed by an AgentCore Code
Interpreter, and writes its own Python (markitdown, python-pptx, pdfplumber, …,
all preinstalled) to extract whatever it needs — we hardcode no decoder. The
runner (`_sandbox_for`) owns the session lifecycle around one crawl: start,
upload `.context/` into `/tmp/okf_context/`, always stop. Three guardrails: the
interpreter runs in **SANDBOX** network mode (no internet) under its **own,
grant-less execution role** (credential isolation — no Glue/Athena/bundle creds
reach it, so it can't be used to widen scope from an injected `.context` doc);
extracted text is **source data to document, not instructions**, by the prompt
(same rule as `.context/` and Glue free-text); and output is truncated + `invoke`
is serialized (subagents share one
session). It is a **separate tool, not the default backend** — the bundle stays
on the `FilesystemBackend` mount that `finalize`/`reindex` read. Optional: with
`OKF_CODE_INTERPRETER_ID` unset the harvest degrades to text-only `.context`.

**Auth is Cognito OIDC.** One user pool; the same discovery URL feeds the UI, the
API Gateway JWT authorizer, and the AgentCore JWT authorizer for consumption.

**Embeddings are Titan V2 at 512 dims, cosine.** The reindex worker and the
consumption server share the embed-text and metadata builders in
`okf_core/embedding.py`, so the keys and metadata they produce can't drift apart.

**Terraform for all infrastructure, state split by lifecycle, no console
changes.** The S3 Files file system, mount target, and access point are managed
natively (`aws_s3files_*` in `infra/compute/s3files.tf`) and mounted at
`/mnt/data` when harvest VPC subnets are supplied; the runtime and its
`s3files:ClientMount` grant key off the same access-point ARN. Cognito
callback/logout URLs are Terraform variables, and `deploy.sh cognito-urls`
injects the CloudFront URL via a re-apply of the durable stack. An out-of-band
access-point ARN can be supplied via `var.s3_files_access_point_arn` as a
fallback.


## Observability

The full agent trajectory — every LLM call (including reasoning text), every
tool call, and the subagent fan-out — is traced into the CloudWatch GenAI
Observability console via OpenTelemetry.

- **Instrumentation.** Both runtime containers run under ADOT's
  `opentelemetry-instrument` launcher. Harvest also ships
  `openinference-instrumentation-langchain`, because plain ADOT only traces
  httpx/boto3 and the LangChain/LangGraph spans need the framework instrumentor
  (deepagents is built on LangGraph). Consumption is FastMCP and needs no
  LangChain instrumentor.
- **Thread-context propagation.** The crawl runs on a background daemon thread,
  and OTEL context lives in `contextvars`, which a bare `threading.Thread` does
  not inherit. `entrypoint.py` copies the context into the worker
  (`contextvars.copy_context()` + `ctx.run`) so crawl spans stay parented under
  the invoke span (covered by `test_crawl_thread_inherits_context`).
- **Runtime env** (`local.otel_common_env` in `infra/compute/data.tf`):
  `AGENT_OBSERVABILITY_ENABLED=true` plus the ADOT distro/configurator/protocol
  vars. The OTLP endpoint is injected by the runtime — don't set it. Reasoning
  capture uses `LC_OUTPUT_VERSION=v1`; content capture is on by default via
  `var.capture_trace_content` (one switch driving the four `OPENINFERENCE_HIDE_*`
  flags). Captured text lands in `aws/spans`, so flip the variable to redact.
- **Transaction Search** (`infra/durable/observability.tf`) is the account- and
  region-wide prerequisite that indexes X-Ray spans into `aws/spans`: a
  CloudWatch Logs resource policy, `aws_xray_trace_segment_destination`, and
  `aws_xray_indexing_rule`. It lives in the durable stack and is gated by
  `var.enable_transaction_search`.
- **IAM** needs nothing extra — the baseline AgentCore policy already grants
  `xray:PutTraceSegments`, `logs:PutLogEvents`, and `PutMetricData`.
