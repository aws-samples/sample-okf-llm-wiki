# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Data Wiki turns AWS Glue databases into portable knowledge bundles and serves them to AI agents over MCP. An LLM reads a Glue catalog, authors markdown docs describing each dataset (tables, joins, metrics, known issues), keeps them in sync as the catalog changes, and exposes them to agents over an MCP server.

Bundles are **Open Knowledge Format (OKF)** bundles — a directory of markdown files with YAML frontmatter. The `okf`/`OKF_` prefix on identifiers, resource names, and env vars refers to the *format*, not the product. OKF's model maps onto AWS as `data domain → dataset (Glue database) → table`.

## Read these first

The repo is documentation-heavy and the docs are authoritative. Before making changes, read the relevant one:

- **`docs/CONVENTIONS.md`** — the contract between services: S3 bundle layout, DynamoDB item shapes, the harvest lease, the harvest invocation payload, and every `OKF_*` env var. A mismatch here is an integration bug that ripples across services. **Changes to these shapes affect every component** — keep them intact.
- **`docs/ARCHITECTURE.md`** — how the seven components fit and the non-obvious reasoning behind them.
- **`docs/API_REFERENCE.md`** — the exact third-party API shapes (deepagents, AgentCore, S3 Vectors, Titan/Glue/Athena, Terraform `aws ~> 6.0`, react-oidc) the code was written against. These are the details that are easy to get wrong.

## Architecture

Seven Python services under `services/`, two Terraform stacks under `infra/`, a React SPA in `ui/`, and a Claude Code plugin in `okf-mcp/`.

**Shared libraries (import from these, don't re-implement):**
- `okf_core/` — pure-Python OKF primitives, no AWS or agent deps. Owns the source-of-truth invariants: `paths.py` (concept id ↔ S3 key), `embedding.py` (vector key + embed text/metadata builders), `document.py`, `link_graph.py`, `guard.py`, `session.py` (`runtime_session_id`), `index_gen.py`, `hive_types.py`.
- `okf_aws/` — shared boto3 helpers: Titan embed, S3 Vectors, S3 keys.

**Runtime services:**
- `harvest/` — induction. A `deepagents` agent on AgentCore reads Glue, samples via Athena, and authors the bundle. All Glue metadata is snapshotted once at run start into a read-only `.metadata/` dir (`metadata_export.py`) that the agent explores with `read_file`/`glob`/`grep` (a `columns.tsv` grep drives cross-table join/near-synonym discovery); only live `sample_rows`/`run_sql` remain as tools. Uses `FilesystemBackend(virtual_mode=True)` inside a `CompositeBackend` for per-dataset path confinement, `OKFGuardMiddleware` to enforce frontmatter/augmentation rules on every write (and to keep `.metadata/` read-only), and fans out a `table-author` subagent per table then a `reviewer` subagent per doc. Authoring methodology is the vendored skill in `services/harvest/skills/okf-authoring/`.
- `consumption_mcp/` — stateless streamable-HTTP MCP server (FastMCP) on AgentCore. Tools: `list_domains`, `list_directory`, `read_page`, `glob`, `grep`, `get_backlinks`, `semantic_search`.
- `control_api/` — Cognito-authed REST (API GW HTTP API + one Lambda with an internal router). Registers datasets, presigns context uploads, starts/checks harvests, reads bundles, vends/revokes MCP credentials.
- `reindex/` — S3 object events → Titan embed → S3 Vectors `PutVectors`/`DeleteVectors`. Dedups on the S3 `sequencer`.
- `incremental/` — Glue change event → confirm real change → scoped re-harvest of the changed table; nightly reconcile catches missed events.

**Two invariants worth internalizing before touching harvest/reindex:**
1. **S3 markdown is the source of truth; the vector index is derived** and can be rebuilt by replaying objects through reindex. Index params (512 dims, cosine, float32) are immutable in S3 Vectors and live in exactly two places: `okf_core/embedding.py` and `infra/durable/storage.tf`.
2. **The harvest status row doubles as a per-dataset lease** (conditional `PutItem`) so two harvests never write the same bundle dir at once. See CONVENTIONS.md — the Control API returns `409` and the incremental path returns `skipped_locked` when the lease is held.

**Infra split by lifecycle:** `infra/durable/` (S3 buckets, S3 Vectors index, Cognito, DynamoDB) is a separate stack from `infra/compute/` (Lambdas, API GW, AgentCore runtimes + IAM, EventBridge/SQS, CloudFront), wired via `terraform_remote_state`. All infra is Terraform, `hashicorp/aws ~> 6.0`, native throughout — no console changes.

## Common commands

### Python tests (fully offline — no AWS account, no live calls)

`moto` mocks S3/DynamoDB; s3vectors, bedrock-runtime, glue, athena, and agentcore are injected fakes.

```bash
# One-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e services/okf_core -e services/okf_aws                       # shared libs
pip install -e services/harvest -e services/reindex -e services/incremental \
            -e services/control_api -e services/consumption_mcp --no-deps
pip install pytest "moto[s3,dynamodb]"

# Run everything (every service's unit tests + the offline E2E harvest test)
./scripts/run_tests.sh
```

Each service is tested **from its own directory** (`pythonpath = ["src"]`, `testpaths = ["tests"]` in its pyproject). To run one service or one test:

```bash
cd services/harvest && python -m pytest tests -q                    # one service
cd services/harvest && python -m pytest tests/test_runner_status.py -q
cd services/harvest && python -m pytest tests/test_runner_status.py::test_name -q
python -m pytest tests -q      # from repo root: the offline E2E (tests/test_e2e_harvest_offline.py)
```

`tests/test_e2e_harvest_offline.py` drives the non-LLM half of the pipeline (Glue source → guard engine → link-graph impact → `finalize_bundle`) against a fake F1-shaped source and asserts a valid OKF bundle. Bedrock, S3 Vectors, and AgentCore hosting are **not** exercised — they need a real account.

### UI

```bash
cd ui
npm ci                 # (or npm install first run)
npm run dev:env        # writes ui/.env.local from the deployed compute stack outputs
npm run dev            # http://localhost:5173 (whitelisted in Cognito for OIDC)
npm run build
npm run lint           # eslint
npm run format         # prettier
```

The SPA is **JavaScript, not TypeScript**. Vite multi-entry (`index.html` + `callback.html`), shadcn/ui + Tailwind, `react-oidc-context` against the real deployed Cognito.

### Terraform validation

```bash
cd infra/durable && terraform init -backend=false && terraform validate
cd infra/compute && terraform init -backend=false && terraform validate
```

### Deploy

`./scripts/deploy.sh` runs the full 5-stage pipeline (prompts once, saved to `scripts/.deployment.config`). Stages can run individually — useful when iterating on one layer:

```bash
./scripts/deploy.sh <durable|images|compute|cognito-urls|ui|dev-env|summary|destroy>
```

Requires authenticated AWS CLI, Terraform, Docker (buildx for ARM64), node/npm, jq, and a pre-created versioned S3 bucket for TF state. `images` builds/pushes the ARM64 harvest + consumption containers to ECR; `destroy` deletes the bundle bucket + vectors (prompts for confirmation).

## Working notes

- **CI-equivalent check** for a change: run `./scripts/run_tests.sh`, plus `cd ui && npm ci && npm run build`, plus `terraform validate` on both stacks (this is what CONTRIBUTING documents as the local suite).
- The harvest model, thinking effort, timeouts, and subagent concurrency are all env-configurable (`OKF_HARVEST_*`) — see the env var table in CONVENTIONS.md before hardcoding anything.
- The `OKFGuardMiddleware` must be attached to **every subagent's** middleware list, not just the main agent — subagent middleware replaces rather than inherits (a repeated footgun; see ARCHITECTURE.md and API_REFERENCE.md §1).
- Inbound MCP auth is **scope-based** (`okf-mcp/invoke`), not a client allowlist, so newly vended machine credentials work with no infra change. Cognito M2M `client_credentials` tokens carry no `aud`, which is why the authorizer can't use `allowedAudience`.
