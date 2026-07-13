// "agent + OKF" generation for BIRD mini_dev — one agent per question.
//
// Each agent answers its question using ONLY the OKF knowledge bundle, read live
// over the consumption MCP (mcp_query.py). No raw schema is handed to the agent —
// it derives every table, column, join, and value format from the bundle. This
// mirrors the official mini_dev inference (llm/src/prompt.py, run_gpt.sh) on every
// other axis so the score is leaderboard-comparable:
//   - dialect: SQLite  (graded by bird-bench evaluation_ex.py on the real BIRD DBs)
//   - evidence (External Knowledge): ON  (use_knowledge='True' on the leaderboard)
//   - chain-of-thought: ON  (cot='True')
//   - returns ONLY the SQL, starting from SELECT
// The ONE swap vs the leaderboard is the knowledge source: OKF bundle over MCP,
// instead of raw CREATE-TABLE schema + sample rows.
//
// Each agent writes its final SQL to _ws/preds/q<idx>.sql. Afterward, outside this
// workflow, assemble the predictions JSON and grade with bird-bench's scripts:
//   python3 assemble_predictions.py --out _ws
//   ./run_eval.sh
//
// Prerequisite: the OKF bundles must already be harvested for data_domain "bird",
// one dataset per Glue database (verify: python3 mcp_query.py ls --domain bird
// --dataset formula_1). And the mcp_query.py env vars must be set (see its docstring).
//
// Invoke with the Workflow tool:
//   scriptPath: benchmark/mini_dev/generate_workflow.js
//   args: { ws: "<abs>/benchmark/mini_dev/_ws", idxs: [..optional subset..] }

export const meta = {
  name: 'minidev-okf-generate',
  description: 'agent+OKF: one agent per mini_dev question reads the OKF bundle over live MCP and emits SQLite SQL (evidence+CoT ON)',
  phases: [{ title: 'Generate (agent + OKF)' }],
}

const WS = (args && args.ws) || 'benchmark/mini_dev/_ws'
const MCP = 'benchmark/mini_dev/mcp_query.py'
// LEAKAGE GUARD: generators read gen_questions.json (question + evidence +
// dataset, NO gold). grade_questions.json holds the gold and is for the grader
// ONLY — an agent must never open it.
const GEN = `${WS}/gen_questions.json`
const PREDS_DIR = (args && args.preds_dir) || 'preds'

const targets = (args && args.idxs) || Array.from({ length: 500 }, (_, i) => i)

function genPrompt(idx) {
  // Mirrors the leaderboard prompt shape (question + External Knowledge + CoT +
  // "return only SQL"), with the schema block replaced by OKF-bundle reading.
  return `You are a text-to-SQL expert. Produce ONE valid SQLite query that answers the question. You will be graded by execution on the real SQLite database (BIRD execution accuracy), so the query must run on SQLite and return exactly the right rows.

STEP 1 — read your assignment. Open ${GEN} and read the entry keyed "${idx}". Use:
  - its "question" string, and
  - its "evidence" string as "-- External Knowledge" (USE it — it is provided, exactly as the BIRD benchmark provides it to models).
Note "dataset" (the OKF dataset name to query over MCP). "data_domain" is "bird". This file contains NO answer SQL — you must derive the query yourself from the OKF bundle.

STEP 2 — learn the database SOLELY from its OKF knowledge bundle (this is the only knowledge source; there is NO raw schema handed to you). Use the live MCP client with progressive disclosure:
  python3 ${MCP} ls         --domain bird --dataset <dataset>              # bundle root (index of tables/ references/)
  python3 ${MCP} ls         --domain bird --dataset <dataset> --path tables
  python3 ${MCP} read       --domain bird --dataset <dataset> --path <file.md>
  python3 ${MCP} glob       --domain bird --dataset <dataset> --pattern "tables/*.md"
  python3 ${MCP} grep       --domain bird --dataset <dataset> --pattern "<term>"
  python3 ${MCP} backlinks  --domain bird --dataset <dataset> --path <concept>    # what links TO this concept (reveals joins/references)
  python3 ${MCP} search     --domain bird --dataset <dataset> --query "<natural language>"
Read the dataset overview, the relevant tables/<t>.md (columns, types, keys, grain, value semantics, encodings), and references/ (joins, metrics, value formats). Use backlinks to discover which tables reference a given table. Derive every table name, column name, join path, and value format from the bundle. Do NOT query any database directly; do NOT read any gold/answer file.

STEP 3 — think step by step, then write ONE SQLite SELECT that answers the question.
  - Valid SQLite dialect (IIF, STRFTIME, SUBSTR, INSTR, CAST(... AS REAL), CASE, etc.).
  - Identifiers are case-insensitive in SQLite, so the bundle's lowercase names are fine.
  - Return exactly the column(s) the question asks for.

STEP 4 — write ONLY the final SQL (one statement, starting from SELECT; no markdown fences, no comments, no explanation) to ${WS}/${PREDS_DIR}/q${idx}.sql. Overwrite if present. Then reply with just the file path.`
}

log(`agent+OKF (SQLite, evidence+CoT) for ${targets.length} questions -> ${WS}/${PREDS_DIR}/`)
const res = await parallel(targets.map(idx => () =>
  agent(genPrompt(idx), {
    label: `gen:okf:${idx}`,
    phase: 'Generate (agent + OKF)',
    // Reference run (RESULTS.md, EX 74.0) used xhigh. Drop to 'high' for a
    // ~2-point-lower but faster/cheaper run.
    effort: 'xhigh',
  })
))
const done = res.filter(Boolean).length
log(`Generation: ${done}/${targets.length} agents returned. Next: assemble_predictions.py + run_eval.sh`)
return { generated: done, of: targets.length, ws: WS }
