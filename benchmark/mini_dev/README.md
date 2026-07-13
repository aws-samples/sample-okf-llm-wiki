# BIRD mini_dev — an OKF text-to-SQL benchmark

Can an AI agent answer text-to-SQL questions with **no access to the database
schema** — only an [Open Knowledge Format (OKF)](../../README.md) bundle read
over an MCP server? This benchmark measures exactly that, on
[BIRD mini_dev](https://github.com/bird-bench/mini_dev): 500 curated questions
across 11 relational databases.

The setup is deliberately **leaderboard-faithful**. It matches the official
mini_dev inference on every axis — same questions, SQLite dialect, "evidence"
hint, chain-of-thought, and the **unmodified** bird-bench grader on the original
SQLite databases — and changes exactly one thing:

| Axis | Official mini_dev | This benchmark |
|---|---|---|
| Questions | mini_dev 500 | mini_dev 500 |
| SQL dialect | SQLite | SQLite |
| Evidence (External Knowledge) | ON | ON |
| Chain-of-thought | ON | ON |
| Grading | bird-bench `evaluation_ex.py` on SQLite | same, **unmodified** |
| **Knowledge source** | raw `CREATE TABLE` schema + samples | **OKF bundle only, over MCP** |

Because grading runs on the original SQLite databases (where all 500 gold
queries execute and floats are bit-identical), the maximum score is 100 — the
same ceiling as the leaderboard.

**Result:** an agent grounded only on the OKF wiki scored **EX = 74.0**, above
every published mini_dev entry (best prior: TA + GPT-4o at 63.0), using ~4.5 wiki
reads per question. See [RESULTS.md](RESULTS.md).

> **The data is not in this repo.** BIRD's databases, questions, gold SQL, and
> the bird-bench evaluators are downloaded by `fetch_data.py` from the official
> source (CC-BY-SA 4.0). Nothing from BIRD is vendored here.

---

## How it works

```
 fetch_data.py        load_all.py         (OKF harvest)        generate_workflow.js      run_eval.sh
┌──────────────┐    ┌─────────────┐    ┌────────────────┐    ┌────────────────────┐   ┌──────────────┐
│ download 500 │    │ 11 SQLite   │    │ harvester reads│    │ 1 agent / question │   │ bird-bench   │
│ Qs + gold +  │──▶ │ DBs → S3    │──▶ │ Glue, authors  │──▶ │ reads bundle over  │──▶│ evaluation_  │
│ SQLite DBs + │    │ Parquet +   │    │ an OKF bundle  │    │ MCP, writes SQLite │   │ ex.py on the │
│ evaluators   │    │ Glue tables │    │ per database   │    │ SQL (no schema)    │   │ SQLite DBs   │
└──────────────┘    └─────────────┘    └────────────────┘    └────────────────────┘   └──────────────┘
```

The Glue-load step exists because OKF bundles are **harvested from a live data
catalog** — that is the product. So we replicate each BIRD database into Glue,
let the OKF harvester author a knowledge bundle from it, and then test whether an
agent can reconstruct the schema/semantics from the bundle alone.

Grading, though, happens on the original SQLite databases with BIRD's own
evaluator, so the number is directly comparable to the published leaderboard.

## Files

| File | Role |
|---|---|
| `fetch_data.py` | download BIRD mini_dev (SQLite DBs, questions, gold, difficulty) + the bird-bench evaluators into git-ignored `data/` and `evaluation/` |
| `config.py` | the 11 `db_id → (Glue db, bucket slug)` map + region; no account-specific values |
| `load_bird_to_glue.py` | load one SQLite DB → S3 Parquet + a Glue database (one EXTERNAL_TABLE per source table) |
| `load_all.py` | driver: load all 11 (or one) BIRD databases into Glue, with an Athena `COUNT(*)` verify |
| `mcp_query.py` | thin CLI exposing all 9 consumption MCP tools (`list_domains`, `list_declared_domains`, `search_domains`, `list_directory`, `read_page`, `glob`, `grep`, `get_backlinks`, `semantic_search`); env-configured, self-mints the M2M token |
| `prep.py` | build the `_ws/` workspace: a gold-free `gen_questions.json` (generators) + `grade_questions.json` (grader only) |
| `generate_workflow.js` | the run: one agent per question reads the OKF bundle over MCP and writes SQLite SQL to `_ws/preds/q<idx>.sql` |
| `assemble_predictions.py` | collect the per-question SQL into bird-bench's predictions JSON |
| `run_eval.sh` | grade with the unmodified bird-bench evaluators on the SQLite DBs → EX + Soft-F1 |
| `requirements.txt` | Python deps (boto3, pyarrow, func-timeout, and the evaluators' psycopg2/pymysql imports) |

`data/`, `_ws/`, and `evaluation/` are git-ignored (downloaded / generated).

## Reproduce it

### 0. Prerequisites

- An OKF stack deployed from this repo (see the [top-level README](../../README.md)
  and `scripts/deploy.sh`) — you need the Glue catalog, the harvester, and the
  consumption MCP running in your own AWS account.
- Authenticated AWS CLI with permission to create S3 buckets + Glue databases in
  your region, and to run Athena queries.
- Python 3.10+.
- **[Claude Code](https://claude.ai/code) with the Workflow tool** — the 500
  generation agents are fanned out by the `Workflow` tool (`generate_workflow.js`).
  - **Model:** our published numbers used **Claude Opus**. The workflow script
    does *not* pin a model, so each generation agent inherits your Claude Code
    **session model** — set your session to Opus to reproduce the numbers in
    [RESULTS.md](RESULTS.md) (any capable model runs, but the score will differ).
  - **Reasoning effort:** pinned inside `generate_workflow.js` (`effort: 'xhigh'`),
    so it does *not* depend on your session's effort level. The reference run in
    [RESULTS.md](RESULTS.md) (EX 74.0) used `xhigh`; a lighter `high` run scores
    ~2 points lower. Change the one `effort:` line to trade accuracy for speed/cost.

```bash
cd benchmark/mini_dev
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Fetch the data (nothing is vendored)

```bash
python3 fetch_data.py
# downloads minidev.zip (~800 MB) + the bird-bench evaluators.
# If the official URL is unreachable, download the zip yourself and pass:
#   python3 fetch_data.py --zip /path/to/minidev.zip
```

### 2. Load the 11 databases into Glue

```bash
python3 load_all.py              # all 11 → S3 Parquet + Glue, with Athena verify
# or one at a time:  python3 load_all.py --db formula_1
```

Each database lands in its own bucket `okf-bird-<slug>-<account>` and a Glue
database named after the BIRD `db_id` (the one exception: `european_football_2`
loads as Glue db `european_football`).

### 3. Harvest an OKF bundle per database

Register each Glue database as an OKF dataset under the `bird` data domain and
run a harvest (via your deployed Control API / UI). When harvesting is done,
confirm a bundle is readable over MCP:

```bash
# point the MCP client at your deployed runtime (values from your stack):
export OKF_MCP_RUNTIME_ARN="arn:aws:bedrock-agentcore:<region>:<acct>:runtime/okf_consumption-XXXX"
export OKF_TOKEN_ENDPOINT="https://<cognito-domain>.auth.<region>.amazoncognito.com/oauth2/token"
export OKF_M2M_CLIENT_ID="<machine app-client id>"
export OKF_USER_POOL_ID="<cognito user pool id>"
# (get the runtime ARN with:  cd infra/compute && terraform output consumption_runtime_arn)

python3 mcp_query.py ls --domain bird --dataset formula_1        # should list tables/ references/ ...
```

### 4. Build the workspace + generate predictions

```bash
python3 prep.py --out _ws        # 500 questions -> gold-free gen file + grader file
```

Then run the generation workflow with the Claude Code **Workflow** tool:

```
scriptPath: benchmark/mini_dev/generate_workflow.js
args: { "ws": "<abs>/benchmark/mini_dev/_ws" }
```

One agent per question reads its bundle over MCP and writes `_ws/preds/q<idx>.sql`.
(Restrict to a subset with `args.idxs: [0,1,2]` while iterating.)

### 5. Grade

```bash
python3 assemble_predictions.py --out _ws      # -> evaluation/predict_mini_dev.json
./run_eval.sh                                  # EX + Soft-F1 via bird-bench, on the SQLite DBs
```

`run_eval.sh` prints the EX table by difficulty and writes the full log to
`evaluation/okf_eval_result.txt`.

## Integrity / leakage guard

The generator agents read `gen_questions.json`, which contains only the question,
the evidence hint, and the dataset name — **never the gold SQL**. The gold lives
in `grade_questions.json` (grader-only) and in BIRD's own `mini_dev_sqlite_gold.sql`
(used directly by the evaluator). To sanity-check the grader itself, feed the gold
SQL as the predictions: it scores ~99.6 (two queries exceed BIRD's own 30 s
timeout), confirming the grader is byte-for-byte the official one.

## Notes

- **Grading is on SQLite, not Athena.** We load into Glue only so the OKF
  harvester can author bundles; the score comes from bird-bench's evaluator on
  the original SQLite databases, so there is no Athena/Trino dialect ceiling.
- **MySQL/PostgreSQL variants** shipped in `minidev.zip` are out of scope — this
  harness is the 500 SQLite questions only.
- BIRD mini_dev and the bird-bench evaluators are © bird-bench, CC-BY-SA 4.0.
