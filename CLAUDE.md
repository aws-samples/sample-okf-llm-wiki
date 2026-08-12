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
- **`docs/SNAPSHOT_EVIDENCE.md`** — the two deterministic snapshot-time passes (column profiles, relationship evidence sheets): nomination heuristics, size-gate/sampling statistics, verdicts, caching. Read before touching `profile.py`, `relationships.py`, or `probes.py`.

## Architecture

Seven Python services under `services/`, two Terraform stacks under `infra/`, a React SPA in `ui/`, and a Claude Code plugin in `okf-mcp/`.

**Shared libraries (import from these, don't re-implement):**
- `okf_core/` — pure-Python OKF primitives, no AWS or agent deps. Owns the source-of-truth invariants: `paths.py` (concept id ↔ S3 key), `embedding.py` (vector key + embed text/metadata builders), `document.py`, `link_graph.py`, `guard.py`, `session.py` (`runtime_session_id`), `index_gen.py`, `hive_types.py`.
- `okf_aws/` — shared boto3 helpers: Titan embed, S3 Vectors, S3 keys.

**Runtime services:**
- `harvest/` — induction. A `deepagents` agent on AgentCore reads Glue, samples via Athena, and authors the bundle. All Glue metadata is snapshotted once at run start into a read-only `.metadata/` dir (`metadata_export.py`) that the agent explores with `read_file`/`glob`/`grep` (a `columns.tsv` grep drives cross-table join/near-synonym discovery, and `.metadata/profile/<table>.md` column profiles — budget-capped, sampled-above-threshold with INDICATIVE marking, fingerprint-cached across runs — pre-answer the null/enum/range probes; `.metadata/relationships/` evidence sheets — `harvest/relationships.py`, probing mechanically-enumerated join/grain candidates at snapshot time via the same SQL cores as `validate_join`/`check_grain` (`harvest/probes.py`), budget/pair/size-capped, fingerprint-cached, with a name-blind KMV value-sketch nominator for renamed keys — pre-answer the join and grain probes, so authors read verdict sheets (HOLDS/WEAK/SUSPECT/REFUTED/TYPE MISMATCH, orphan samples) and probe live only what no sheet covers); the live tools are `sample_rows`/`run_sql` (row-capped, scan-stats-reporting) plus the deterministic probes `check_grain`/`validate_join` and engine-gated `explain_sql`; every supervisor gets the no-arg `get_stats` inventory (`okf_core/stats.py` + `harvest/stats_tool.py` — doc counts per concept type with ZEROS for known reference subtypes, plus tables-vs-snapshot coverage; counts only, no judgment — supervisors only, full/scoped/cross); every non-cross supervisor also gets the no-arg `lint_bundle` tool (cross runs are pair-confined so bundle-wide errors would be unfixable; scoped/annotation runs use it as a final check on the docs they touched — pre-existing errors elsewhere are named in the summary, not fixed) (`okf_core/lint.py` + `harvest/lint_tool.py`) — step-isolated whole-bundle checks (snapshot-table coverage, required docs, frontmatter, links, join-key existence/type-compat, plus EXPLAIN over runnable non-templated ```sql fences, skipped when the engine lacks it) that the workflow runs twice — once all authoring is done, before the review fan-out (step 6a), and as the final gate after reviewer fixes are applied (step 8) — fixing to zero errors each time; its report rides the live step feed as the `lint` field and renders in a HarvestView modal. Uses `FilesystemBackend(virtual_mode=True)` inside a `CompositeBackend` for per-dataset path confinement, `OKFGuardMiddleware` to enforce frontmatter/augmentation rules on every write (and to keep `.metadata/` read-only, allow the deepagents-0.7 `delete` tool for the SUPERVISOR in every mode (one stale `.md` doc — e.g. a table dropped from the catalog — never a directory or a dot-dir; cross-mode deletes stay confined to the pair subtree; the step feed labels each delete with the full doc path) while refusing it for every sub-agent, hard-enforce read-only for the reviewer/context-extractor via its `read_only` variant, and confine each `fix-author` dispatch to its own cluster's files via the `write_allowlist` variant), and fans out a `table-author` subagent per table. The adversarial review is ONE deterministic `run_review` tool call (`harvest/review.py`, full-harvest supervisor only): it computes/persists the link clusters itself (`.harvest/review/clusters.json`), dispatches one read-only `reviewer` per cluster in parallel, pipes findings into a cluster-confined `fix-author`, surfaces every dispatch as fleet squares, writes `report-<id>.md` per call, and returns a bounded summary; the supervisor only applies cross-cluster propagation notes and retries failed ids via `run_review(cluster_ids=[...])`. When `context-extractor`s ran, the same call ends with a context-fidelity phase: every extractor's digest is auto-recorded verbatim at dispatch time (`.harvest/context/digest-NN.md` — `context_digests.py`, fed from the quickjs shim + static task path; deleted by `finalize_bundle` after the commit marker), paired two-per-`context-reviewer` (`x1..xN` ids, retryable like clusters), audited against the bundle for semantic loss, and confirmed losses fixed by a `fix-author` allowlisted to the whole bundle minus the supervisor-owned hubs (all pair reviews run in parallel first, then fixes strictly one at a time — overlapping scopes). Authoring methodology is the vendored skill in `services/harvest/skills/okf-authoring/`. A `mode="cross"` run documents relationships against one target dataset into `external/<domain>/<dataset>/` in ITS OWN bundle only (target snapshotted read-only into `.metadata/external/`, writes guard-confined to the pair subtree, nothing ever written into the target); the target's discoverability is a reindex-derived `XREF#` signal surfaced by `list_domains` — see CONVENTIONS.md "Cross-dataset references".
  The harvest runtime also hosts **Benchmark Studio** (`mode="benchmark"` / `mode="aggregate_annotations"` — `harvest/benchmark/studio.py`): standalone, human-led wiki evaluation (two checks: Accuracy/SQL EX — deterministic result-set equality — and Behavior — free-form `expected_behavior` graded by the judge per run, plus one per-question synthesis review over failed pairs, no adjusted score; N independent runs; an always-on judge; persisted reports on `REPORT#` rows + off-mount S3 JSON). NOT a harvest — no lease, no mount, nothing written to the bundle; the harvester itself can no longer benchmark (the in-run RI loop is retired). A third mode, `mode="generate_questions"` (`harvest/benchmark/qgen.py` + `okf_core/qbank.py`), synthesizes a question bank from the dataset's GROUND TRUTH only (`.metadata/` + `.context/` — never the authored wiki, which is the system under test): a deterministic allocator (count 20–100 × check ratio × tier mix × dimensions) feeds per-dimension author agents whose `submit_question` tool validates at submit time (dedup, business-language lint, live gold execution under the grading caps) and whose quota middleware forbids finishing with slots neither filled nor forfeited; results land on `QBANK#` rows + an off-mount artifact, reviewed in the UI and downloaded or applied as `questions.csv` (replace; versioned bucket). See CONVENTIONS.md "Benchmark Studio" + docs/BENCHMARK_GUIDE.md.
- `consumption_mcp/` — stateless streamable-HTTP MCP server (FastMCP) on AgentCore. Tools: `read_me` (a how-to-use-the-wiki primer — agents call it first), `list_domains`, `list_directory`, `read_page`, `glob`, `grep`, `get_backlinks`, `semantic_search`.
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
pip install pytest "moto[s3,dynamodb]" "markdown-it-py>=3.0"  # md-it: annotation orphan renderer-oracle test

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
npm run dev            # http://localhost:5173 (allowlisted in Cognito for OIDC)
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
- The `OKFGuardMiddleware` (authoring subagents) and `ToolErrorMiddleware` (every subagent — it converts a raising tool into a `ToolMessage(status="error")` so one bad call can't abort the run) must be attached to **every relevant subagent's** middleware list, not just the main agent — subagent middleware replaces rather than inherits (a repeated footgun; see ARCHITECTURE.md and API_REFERENCE.md §1).
- Inbound MCP auth is **scope-based** (`okf-mcp/invoke`), not a client allowlist, so newly vended machine credentials work with no infra change. Cognito M2M `client_credentials` tokens carry no `aud`, which is why the authorizer can't use `allowedAudience`.
