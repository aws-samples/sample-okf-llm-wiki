# Architecture

How the system is put together, and the reasoning behind the decisions that
aren't obvious from the code. Section numbers (§) refer to `OKF_DESIGN.md`.

## Components

| Area | Component | Location | Notes |
|---|---|---|---|
| UI (§1) | React SPA, shadcn/ui, Cognito OIDC | `ui/` | JavaScript, not TypeScript. Vite multi-entry (`index.html` + `callback.html`), `react-oidc-context`. Views: Domains, Context, Harvest, Browse (with link graph). |
| Control API (§2) | API GW HTTP API + Lambda | `services/control_api`, `infra/compute/control_api.tf` | Cognito JWT authorizer (audience = client id). One Lambda with an internal router behind a `$default` route. Endpoints: list Glue databases, domain mapping, context presign/list/delete, start harvest, harvest status, bundle list/read/graph, credential vending. |
| Induction (§3) | deepagents agent on AgentCore | `services/harvest` | `create_deep_agent` + `FilesystemBackend(virtual_mode=True)` for containment, plus `OKFGuardMiddleware` and the `LinkGraph` tools. Authoring methodology comes from the vendored `okf-authoring` skill in `services/harvest/skills/`, loaded via `skills=["/skills/"]`. Fans out one `table-author` subagent per table, then a `reviewer` subagent per document that checks each load-bearing claim (grain, joins, gotchas, SQL) against live data with `run_sql`; the supervisor only fixes findings it can reproduce. A `run_code` tool (AgentCore Code Interpreter, `harvest/code_interpreter.py`) lets the agent extract text from binary `.context/` docs (PDF/DOCX/PPTX/XLSX). The entrypoint offloads the crawl to a thread and reports `HealthyBusy`. |
| Incremental (§4) | Glue event → SQS → orchestrator | `services/incremental`, `infra/compute/incremental.tf` | Confirms a real change via `UpdateTime` / `GetTableVersions`, stages `.harvest/pending.json`, and invokes a harvest scoped to the changed table. A nightly reconcile catches missed events. |
| Freshness (§5) | S3 events → SQS → reindex | `services/reindex`, `infra/compute/reindex.tf` | Titan V2 (512-dim) embed → `PutVectors` / `DeleteVectors` keyed by concept path. Dedups on the S3 `sequencer` in DynamoDB. SQS in front absorbs Bedrock throttling. |
| Link graph (§6) | `networkx` graph, rebuilt on write | `okf_core/link_graph.py`, `harvest/graph_tools.py` | Link/backlink graph over the dataset subtree; `get_backlinks` / `get_links` return id, title, and heading. Rebuilt lazily when the guard marks it dirty. Used by the harvest agent only. |
| Consumption (§7) | streamable-HTTP MCP on AgentCore | `services/consumption_mcp` | FastMCP, stateless, Cognito JWT. Tools: `list_domains`, `list_directory`, `read_page`, `glob` (path pattern), `grep` (content regex), `get_backlinks`, `semantic_search` (S3 Vectors, hierarchy-filtered). |
| Infrastructure (§8) | Terraform, split by lifecycle | `infra/durable`, `infra/compute` | Durable state (buckets, index, Cognito, DynamoDB) is a separate stack from compute (Lambdas, API, runtimes, CloudFront), wired via `terraform_remote_state`. |

## Key decisions

**S3 markdown is the source of truth; the vector index is derived.** The bundle
bucket is versioned; the S3 Vectors index can be rebuilt at any time by replaying
objects through the reindex worker. The index parameters (512 dims, cosine,
float32, non-filterable `title`/`description`/`s3_key`) are immutable in S3
Vectors, so they live in one place — `okf_core/embedding.py` and
`infra/durable/storage.tf` — and changing them means a `-replace`.

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
