# Recursive Improvement

An optional harvest mode where the induction agent **benchmarks the bundle it
just authored against a user-supplied question set, then revises and
re-benchmarks in a loop** until a target accuracy is met or an iteration cap is
reached. The whole loop runs *inside a single harvest run* — one AgentCore
session, one held lease, one working tree edited in place — not as an external
driver that re-triggers harvests.

The design rests on two load-bearing integrity properties, both enforced by
construction rather than prompt discipline:

1. **Structural gold-blindness** — the agent that authors the docs never sees the
   answer key (gold SQL, gold rows, expected values).
2. **Aggregated, anonymous feedback** — the harness never learns *which* question
   failed or *what* it asked. The benchmark is a black box that returns a score
   plus a consolidated report of what the wiki is *missing or should improve* —
   never a per-question failure keyed to a question it could tune against.

Together these make "teaching to the test" hard by construction: to inflate the
KPI, the harness would have to write something that helps exactly the benchmark
questions but not similar ones — and it can't, because it can't see the
questions, can't see the gold, and only ever writes general facts verified
against live data. This is why the design needs **no train/test data split** (an
earlier draft's mechanism): the feedback boundary closes the overfitting hole at
a tighter seam, so every question can both drive improvement *and* measure
quality without contaminating the number.

This doc is the contract for the feature. It touches three load-bearing
surfaces — `OKFGuardMiddleware` (`services/harvest/src/harvest/okf_guard.py`),
the harvest invocation payload (`docs/CONVENTIONS.md`), and the subagent set
(`agent.py`) — so the shapes here are pinned before code.

## Why in-run, not an external loop

An earlier framing had an external driver run `harvest → benchmark → re-harvest`
by calling the Control API repeatedly. Three properties of the runtime make the
in-run design strictly simpler:

- **The lease is a per-dataset mutex.** The `HARVEST#<domain>#<dataset>/STATUS`
  row doubles as a lease (`control_api/handlers.py:1031-1094`); a second trigger
  while one is queued/running gets `409`. An external loop would have to
  serialize across terminal states and dance around the 8h stale-takeover. In
  one run, there is one lease, held the whole time — no contention.
- **A `full` harvest is destructive.** `run_full_harvest` calls
  `fsutil.clean_authored_output` (`fsutil.py:76-112`), which `rm`s every
  authored dir before rebuild. An external loop re-triggering `full` would
  *discard* each iteration's improvements. In-run, the agent edits the working
  tree in place; improvements accumulate naturally.
- **Reindex is decoupled and lag-prone.** `harvest complete` does not mean the
  S3 Vectors index has caught up (`reindex.tf:88-92`, concurrency capped at 5).
  An external benchmark reading `semantic_search` right after a harvest sees a
  stale index. In-run, the benchmark reads the **working-tree markdown
  directly** (the consistent state), sidestepping reindex entirely.

The cost of in-run: the author and the examiner now share one runtime, so the
entire integrity burden shifts onto structural blindness (below).

## The two KPIs

The user uploads a CSV with two columns: `question,gold_sql`. Both KPIs are
computed the same way the existing `okf-sql-benchmark` harness computes them, but
executed over **Athena on the harvested Glue dataset** (gold SQL and predicted
SQL target the same engine — no SQLite dual-store, no dialect ceiling):

- **Exact accuracy (EX)** — execute gold SQL and predicted SQL on Athena,
  `set(pred) == set(gold)`. Deterministic, always **re-computed** by running the
  query, never trusted from an agent self-report. This is the same set-equality
  contract as bird-bench `evaluation_ex.py` / the skill's `ex_compare.py`, ported
  from SQLite `execute` to `boto3` Athena `start_query_execution`.
  `EX = passes / (total − discarded)` (discards below).
- **Judge accuracy** — an LLM adjudicator classifies each EX *divergence* as a
  genuine doc gap vs a noisy-gold / ambiguous-question artifact, and reports a
  "genuine correctness" rate alongside raw EX. Raw EX rewards reproducing
  possibly-broken gold; without adjudication the loop would "fix" docs to match
  bad gold. The judge is what keeps the loop honest about *why* a question failed.

Both KPIs gate the loop. Which one(s) gate, and at what threshold, is
user-configured (below).

### Per-question grade: PASS / FAIL / DISCARDED

The grader assigns each question one of three outcomes:

- **PASS** — gold ran, predicted ran, `set(pred) == set(gold)`.
- **FAIL** — gold ran, but predicted was wrong or errored → a genuine wiki gap →
  feeds the aggregated feedback.
- **DISCARDED** — the **gold SQL itself cannot execute** against the actual data
  (a referenced column/table doesn't exist, a name mismatch, a binding error). The
  question is *factually unanswerable* — no wiki, however good, could make it
  gradeable, because `set(pred) == set(gold)` is uncomputable without a runnable
  gold. The grader marks it `discarded` with the Athena error as the reason.

Discards are **excluded from both numerator and denominator** of every KPI, so an
impossible question can neither be counted a failure the loop chases forever nor
dilute the score. The discard signal is **deterministic** — it's whether *gold*
binds against the schema, judged with zero LLM involvement — which keeps it
trustworthy and cheap.

Discards are **stable across rounds**: they depend on the *data* (the fixed Glue
snapshot for this run), not the wiki — the wiki can't conjure a missing column.
So a stateless round would independently re-derive the same discard set every
time. Persisting it (and filtering those questions out of subsequent rounds) is
therefore a pure *input filter* / cost optimization — it skips re-running gold
already known to be dead — and does **not** violate the round's statelessness
(it's not carried-over assessment, it's a property of the data).

## The loop

Inside one harvest run, after the initial blind authoring pass completes:

```
author bundle (blind, iteration 0 — the normal harvest)
best = { score: -1, checkpoint: none }
loop (up to max_iterations, default & hard-cap 5):
    result = run_benchmark(config)     ← black-box tool; STATELESS, gold-free out
    persist KPIs (this iteration)
    if result.score > best.score:  best = { result.score, checkpoint(working tree) }
    if gate_kpis all ≥ threshold:  break (success)
    if iteration == max:           break
    revise docs from result.improvements   ← aggregated, anonymous report
finalize:  restore best checkpoint, commit
```

**Each `run_benchmark` call is stateless** — it re-measures the *current* wiki
against the *whole* question set from scratch, with no memory of prior rounds. The
benchmark is a pure function of `(current wiki, question set)`; a rising score
across rounds is therefore real signal, not an artifact of carried-over state. The
only thing that persists across rounds is the supervisor's loop bookkeeping
(iteration count, best-so-far, stop decision) and the DISCARDED set (a data
property, not an assessment — see above). The whole question set is scored every
round (there is no held-back slice); the aggregated-feedback boundary is what
prevents that from overfitting the number.

The supervisor drives this loop from its prompt. The **middleware** enforces the
caps and blindness so the loop cannot cheat or run away (see *Middleware
monitor*). Every doc edit goes through the normal `OKFGuardMiddleware` write path
— the augmentation guard still applies (a Glue-typed doc cannot drop a `# Schema`
field or shrink `# Citations`; `guard.py:145-192`).

## Enablement — an optional, per-harvest toggle

Recursive improvement is **off by default** and enabled per-run from the **harvest
settings UI** (the same picker that chooses model + effort), not a deploy-time
switch. The presence of the `recursive_improvement` block in the invocation
payload **is** the enable signal — absent ⇒ the feature is entirely inert, and a
normal harvest is byte-for-byte unchanged. Enablement fans out to four places, and
all four must key off the *same* signal so a disabled run has zero benchmark
surface:

1. **Tool registration.** `run_benchmark` is appended to the agent's tools **only
   when enabled**. A disabled run's agent has no benchmark tool at all — it cannot
   be called, mis-called, or hallucinated into existence. (`agent.py:471-486`,
   the same conditional-append spot the `run_code` sandbox tool uses.)
2. **Prompt / skill is dynamic.** The `SUPERVISOR_PROMPT` gains a
   recursive-improvement section **only when enabled** — the loop instructions,
   the threshold/max-iter contract, and the "call `run_benchmark`, revise from its
   `improvements`, repeat" methodology. When disabled, that section is omitted
   entirely so the agent isn't told about a tool it doesn't have. Same for the
   authoring skill guidance — the RI methodology is conditionally included.
3. **Middleware tracking is conditional.** The guard counts `run_benchmark` calls,
   enforces caps, and compels-before-finalize **only when the run is a
   recursive-improvement run.** A normal harvest's guard behaves exactly as today
   (no benchmark bookkeeping, no finalize-compel). The guard is constructed with an
   `ri_enabled` flag derived from the payload.
4. **Applies to full, incremental, AND annotated modes.** The toggle is
   mode-orthogonal: a re-harvest triggered by a Glue change (`incremental`) or by
   wiki annotations (`annotated`) can *also* run the improvement loop if the
   dataset's saved settings enable it. The `recursive_improvement` block rides
   alongside the existing per-mode payload, so the loop runs after that mode's
   authoring/revision pass completes, against the same question set.

The dataset's RI settings (enabled flag, thresholds, max-iter, questions_key) are
saved with the dataset — most naturally on the `DATASET#` registry row next to
`guidance` — so the UI can persist "this dataset benchmarks against these
questions at these thresholds" and every subsequent harvest/re-harvest of that
dataset picks it up without re-uploading. The Control API reads that row and
populates the `recursive_improvement` payload block on every trigger for the
dataset (full/incremental/annotated alike) when the flag is set.

## Configuration

The invocation payload gains **optional** fields (additive — they do not touch the
existing `full`/`incremental`/`annotated` contract in `CONVENTIONS.md`). Present ⇒
recursive improvement is enabled for this run:

```json
{ "data_domain": "sales", "dataset": "orders", "mode": "full",
  "recursive_improvement": {
    "questions_key": "okf/sales/orders/.benchmark/questions.csv",
    "max_iterations": 5,
    "ex_threshold": 0.80,
    "judge_threshold": 0.90,
    "gate_kpis": ["ex", "judge"]
  } }
```

| Field | Meaning | Constraint |
|---|---|---|
| `questions_key` | S3 key of the uploaded `question,gold_sql` CSV | Under a `.benchmark/` prefix (dot-prefixed ⇒ survives `clean_authored_output`, hidden from consumers) |
| `max_iterations` | benchmark→revise rounds after iteration 0 | 2–5; **clamped to 5** at the tool boundary |
| `ex_threshold` / `judge_threshold` | target rates (over non-discarded questions) | 0.0–1.0 |
| `gate_kpis` | which KPIs must clear their threshold to stop | subset of `["ex","judge"]` |

**N (question count) is inferred from the CSV, hard-capped at 100.** If the user
uploads 105 rows, the tool takes the **first 100 in CSV order** (deterministic,
so the scored set is reproducible across rounds) and `log()`s that 5 were dropped
— silent truncation would read as "benchmarked everything." The clamp happens at
the tool boundary. (The whole capped set is scored each round; discards are then
excluded from the KPIs.)

`recursive_improvement` params that reach the model/agent are validated at the
Control API trust boundary (like `model`/`effort` today,
`handlers.py` / `app.py:429-469`) — `max_iterations` clamped, thresholds
range-checked — not trusted from the payload body.

### Benchmark model — reuse the single harvest instance

All benchmark LLM roles (solver, adjudicator) run on the **same `chat_model`
instance** the harvest already built (`agent.py:462`) — no second model, no
separate effort. Reasons this is the right simplification:

- **Token metering is free.** The instance already carries a
  `UsageForwarder(step_emitter)` (`agent.py:459`), so every benchmark call meters
  into the one cumulative total with zero extra wiring (see *Token accounting*).
- **One less moving part** — no second build, no effort-derivation helper, no risk
  of the two instances drifting.

Tradeoff, stated plainly: effort is baked into the instance at construction
(`thinking_fields(effort)`), so the benchmark runs at the **same effort as
authoring** — there is no per-call effort override on a shared instance. The
benchmark is the dominant token term (up to N solvers × 5 rounds + adjudicators)
and now pays full harvest effort per solver. The levers that keep it bounded are
the `K=10` semaphore, gold/predicted result caching, and early-stop on
no-improvement — not a lowered tier.

### CSV upload

Reuse the existing presigned `.context/`-style upload path
(`presign_context_upload`, `handlers.py:966-1013`) but pin the key under
`.benchmark/` instead of `.context/`. The 20 MiB cap and flat-filename rule carry
over. `.benchmark/` is dot-prefixed, so it is preserved by
`clean_authored_output` and hidden from the consumption MCP — the same treatment
`.context/` and `.metadata/` get.

## The black-box benchmark tool

The supervisor sees exactly one opaque tool, `run_benchmark(config)`, and never
sees anything gold-shaped **or question-shaped**. It returns a score plus a
**consolidated, anonymous** improvement report:

```json
{ "iteration": 2,
  "ex_score": 0.71, "judge_accuracy": 0.83, "threshold_met": false,
  "passed": 34, "failed": 14, "discarded": 2, "graded": 48,
  "improvements": [
    "The revenue metric doesn't state that it excludes refunds; several revenue
     questions computed gross instead of net.",
    "Order status codes are documented as strings ('active') but stored as ints
     (1); status filters produced empty results.",
    "The orders→customers join key is undocumented; joins guessed the wrong column."
  ] }
```

No gold SQL, no gold rows, no expected values — **and no per-question failure
list, no question text, no `q_id`s.** The `improvements` array is a *consolidated
theme list*: the adjudicator's genuine-error findings, grouped and de-identified
into "what the wiki is missing or should improve," so the supervisor cannot map a
fix to a specific benchmark question. This is the aggregated-feedback boundary —
the second integrity property. Behind the tool boundary, four internal roles run,
none of whose gold- or question-level state reaches the supervisor's context:

### 1. Concurrency — Python `asyncio`, not QuickJS

The internal fan-out is a plain `asyncio.gather` gated by its **own**
`asyncio.Semaphore` (`OKF_BENCHMARK_MAX_CONCURRENCY`, **default 10**), inside the
tool function. It is deliberately **not** the deepagents QuickJS `task()`
primitive, for three reasons:

- `task()` only fires when the *LLM emits it* — it cannot be called from inside a
  Python tool. Using it would make the supervisor orchestrate the benchmark in
  the open, which is the opposite of a black box.
- The middleware caps must intercept `run_benchmark` deterministically; QuickJS
  fan-out would be LLM-driven and non-deterministic.
- QuickJS fan-out burns the supervisor's `recursion_limit` step budget and shares
  the single `OKF_HARVEST_MAX_SUBAGENT_CONCURRENCY=5` semaphore with the
  authoring subagents (`agent.py:443`). An independent `asyncio.Semaphore` gives
  the benchmark its own concurrency budget without fighting authoring for slots.

QuickJS stays the primitive for the agent's *authoring* subagents (unchanged);
`asyncio` is the primitive for the benchmark tool's internal fan-out. Two
orchestration layers for two jobs — they compose because token metering rides the
*model instance*, not the dispatcher (see *Token accounting*).

**How many requests run in parallel.** The benchmark semaphore gates **in-flight
model requests directly** — each solver is one ReAct loop with exactly one model
call in flight at a time (explore turns then the terminal coercion, all
sequential). So `K = OKF_BENCHMARK_MAX_CONCURRENCY` (default 10) is the peak
concurrent Bedrock requests from the benchmark. This differs from authoring's
`task()` cap of 5, which bounds *subagents* — each a multi-turn crawl — so
authoring peaks at ~5–6 in-flight requests only as a side effect. The benchmark's
phases don't overlap, so peak is `max`, not sum: **solve → up to K**; grade → 0
(Athena only, its own ~15–20 concurrency under the workgroup DML limit);
adjudicate → up to K (only for `FAIL` cases). At N=100, K=10 means 10 sequential
waves of solvers — the dominant term in the run's wall-clock, which is why K is
env-tunable and gold-caching matters (round 2+ re-solves only changed questions).

**The real ceiling is the account Bedrock quota, and there is no global cap.**
Nothing in the codebase bounds *total* concurrent Bedrock requests across layers
— the only throttle defense is the shared client's botocore `retries` (`adaptive`
mode, `max_attempts=5`, `agent.py:236-241`). During the solve phase the supervisor
is idle (blocked on the tool), so authoring isn't competing; but K solver requests
hit the same account Opus 4.8 RPM/TPM quota. K=10 is chosen to run wider than
authoring's ~5 (solvers are I/O-bound on the model, so concurrency helps) while
staying under a shared quota — leaning on the same adaptive-retry safety net.
Raise K on a generous account; drop it if `ThrottlingException` appears.

### 2. Prediction — one isolated ReAct loop per question

Each `asyncio` coroutine drives a **full per-question agent loop**, not a single
LLM call — because the real consumer *retrieves* (reads a page, follows a link,
greps a column) before writing SQL. A single "here's the whole bundle, emit SQL"
call would measure "does dumping the bundle in context help," not the consumer's
behavior, and would blow context on any real bundle.

- A `create_react_agent` (or thin hand-rolled tool-calling loop) on the **shared
  instrumented `chat_model`** (`agent.py:462`), invoked once per question with
  **fresh message state** — no authoring history. This fresh, isolated context is
  what makes the solver a *fair examiner*: **the solver is not the supervisor.** If
  the doc-author also produced the prediction, a memorized answer passes trivially.
- Tools: bundle **read-only** tools (`read_file`/`glob`/`grep`) pointed **only at
  the authored bundle** in the in-progress working tree (`datasets/`, `tables/`,
  `references/`, `index.md`) — the consistent state mid-run. **Not**
  `semantic_search` (vector index is stale mid-run). **Not** the gold dir (guard
  read-denylist, below). **Not** `run_sql` — the solver only *writes* candidate
  SQL as text; executing against Athena would let it iterate empirically to the
  right answer, measuring persistence, not the wiki.
- **The solver is bundle-blind by design — it must NOT read `.metadata/`,
  `.context/`, or run code.** It simulates the real consumer, whose only knowledge
  source is the wiki. If the solver could read the raw Glue schema snapshot
  (`.metadata/`) or the source docs (`.context/`), it would answer from the raw
  data and bypass the wiki entirely — the score would then measure "can an agent
  query this dataset with full schema access," not "is the wiki good," which is
  the whole question. This is the opposite of the adjudicator (role 4), which
  *does* get raw-data access precisely because its job is to find what the wiki is
  missing *relative to* the raw data. Same read-denylist mechanism, mirror-image
  intent: gold is denied to both; raw data is denied to the solver, granted to the
  adjudicator.

**Structured output — terminal only.** The solver's SQL is extracted via a
one-field `with_structured_output({sql: str})` **final** call, *after* the ReAct
loop settles — so the grader gets a clean string, not a regex over prose (a
correct answer wrapped in a fenced block + comment would otherwise parse-fail →
score 0, measuring the parser, not the wiki). The schema is **not** bound during
retrieval: a "final_answer" response tool competing with the read tools makes the
model answer on turn one before reading anything, defeating the retrieval
fidelity. Use the two-phase shape (explore with read tools, then coerce) — or
`create_react_agent(response_format=...)` if the installed langgraph version runs
it as a separate terminal call (verify at build time; the explicit two-phase
version is safe regardless). An empty `sql` from a stuck solver is a scored-0
miss, not a retry-forever error.

### 3. Grading — deterministic, zero LLM

A pure Python function. For each question:

1. Execute the **gold** SQL on Athena (`start_query_execution` + poll). If it
   **errors on a schema/binding problem** (missing column/table, name mismatch) →
   `DISCARDED`, reason = the Athena error. The question is unanswerable; stop.
2. Otherwise execute the **predicted** SQL. `set(pred) == set(gold)` → `PASS`;
   predicted wrong or errors → `FAIL`.

This is the **only** role that reads the gold dir, and it is not an LLM — it emits
`{q_id, outcome, predicted_sql, divergence_sample, discard_reason?}`, never the
gold query text or results. Zero tokens. This is the piece that makes EX
trustworthy and the discard signal deterministic.

**Cost is real and must be bounded.** The whole capped set is scored every round
(no held-back slice), so each round is up to **2N Athena queries** (predicted +
gold); naively N=100 ×5 rounds ≈ 1000 executions. Two cost axes: **$ per data
scanned** ($5/TB — a non-selective predicted query on a multi-GB dataset scans GBs
*each*), and **latency + concurrency** (each is `start_query_execution` + poll;
1000 sequential blows the 8h budget, but the workgroup caps concurrent DML at
~20–25). Optimizations, by impact:

1. **Cache gold results — gold is invariant across rounds.** The gold SQL for a
   question never changes; only the predicted SQL does (the wiki changed, not the
   answer key). Execute each gold query **once**, cache its result set (in-process,
   never exposed to the agent), reuse across all rounds. Drops gold from
   `N×rounds` to `N×1`, and folds in the discard check for free (a gold that
   errored once is a stable discard — cache the verdict, never re-run it).
2. **Athena query-result reuse** on the workgroup — identical *predicted* SQL
   across rounds (a question whose relevant docs the agent didn't touch) reuses
   the result free.
3. **Per-query data-scanned cutoff** (workgroup control) + a 30s timeout — a
   pathological predicted query (cartesian join, TB scan) is aborted and **scored
   FAIL** (the honest outcome). Do **not** inject `LIMIT` — it changes the result
   set and breaks set-equality.
4. **Own concurrency cap** for the grader, distinct from the solver-LLM semaphore
   and sized under the workgroup's concurrent-DML limit.

Combined, gold-caching (+ discard-caching) + result-reuse take a realistic run
from ~1000 executions toward ~`N` gold (once) + the predicted queries that
actually changed. State the execution ceiling in the report so cost never
silently balloons.

### 4. Adjudication — diagnose against raw data, consolidate into anonymous themes

The adjudicator is the wiki-gap *diagnostician*, so unlike the solver it gets
**full raw-data access**: `.metadata/` (the Glue schema snapshot), `.context/`
(uploaded source docs), the `run_code` / code-interpreter sandbox, `run_sql` /
`sample_rows`, **and** the authored bundle. It needs to see both what the wiki
*says* and what the data *actually is* to explain a failure — "the wiki calls
this column a string, but `sample_rows` shows int codes." Granting raw data here
is safe (and necessary) because the adjudicator's output is de-identified themes,
never SQL the score depends on — it can't leak an answer into the measurement the
way a raw-data-armed solver would. Two stages, both on the shared `chat_model`:

**a. Per-divergence classification.** One call per `FAIL` divergence. It sees gold
+ predicted *internally* (to reject noisy-gold, per the noisy-gold taxonomy the
existing skill documents) and cross-checks the doc claim against live data /
schema / source docs, then emits **schema-validated** output: a category enum
(`GENUINE_ERROR` / `NOISY_GOLD` / `AMBIGUOUS` / `SHAPE_MISMATCH` / …) plus a
gold-free per-case note grounded in what it found in the raw data. Only
`GENUINE_ERROR` cases survive — noisy gold and ambiguous questions are filtered so
the loop never chases broken gold. (`DISCARDED` questions never reach adjudication
— they're removed deterministically by the grader upstream.)

**b. Consolidation into `improvements`.** A final call folds the surviving
genuine-error notes into a **short, de-identified theme list** — "the revenue
metric doesn't state it excludes refunds," not "Q14 failed." This is the
aggregated-feedback boundary in code: the per-question mapping dies here, inside
the black box, and only the themes cross to the supervisor. Grouping also means
ten questions that all stumble on the same undocumented join produce *one*
improvement item, not ten — which is both better feedback and stronger
de-identification. Because the note is grounded in raw data, the theme names the
concrete fix ("document that `status` is an int code, legend: 1=active…"), which
the supervisor can then verify-and-write through the normal authoring path.

Structured output throughout is exactly the classification/summarization job it's
for (mirrors the skill's `adjudicate_workflow`).

## Blindness is structural — a per-role access matrix

Blindness is enforced by the middleware and the runtime, never by asking the
prompt nicely. Each internal role runs under its **own** `OKFGuardMiddleware`
read-denylist — the deny set differs by role, which is what lets the solver be
bundle-blind while the adjudicator sees everything:

| Role | gold (`.benchmark/gold`) | questions | authored bundle | `.metadata/` + `.context/` + `run_code`/`run_sql` |
|---|---|---|---|---|
| **supervisor** | deny | deny | read/write | read (authoring already uses these) |
| **solver** | deny | **read** | read | **deny** (bundle-blind — measures the wiki) |
| **grader** | *direct file read, not a tool* | direct read | — | executes SQL (its job) |
| **adjudicator** | internal | internal | read | **read** (diagnoses gaps vs raw data) |

Mechanics:

- **Read-denylist extends the guard.** Today `OKFGuardMiddleware` refuses *writes*
  into `.metadata/` (`okf_guard.py:36,43-47,106-115`). The benchmark needs it to
  also refuse *reads* (`read_file`/`glob`/`grep`) of a per-role deny set — so the
  read tools join the guarded set, not just `write_file`/`edit_file`
  (`_GUARDED_TOOLS`, `okf_guard.py:30`). The deny set is a constructor param, so
  each role's guard instance is built with the right list.
- **Gold is denied to every LLM role** — solver, supervisor, adjudicator all run
  under a guard whose deny set includes `.benchmark/gold`. Only the deterministic
  grader reads it, via a *direct filesystem read inside the tool process* that
  never passes through the agent tool layer, so the read-denylist doesn't obstruct
  it.
- **Raw data (`.metadata/`, `.context/`, sandbox) is denied to the solver only.**
  The solver's guard adds these to its deny set; the adjudicator's does not. Same
  mechanism, mirror-image intent (see roles 2 and 4).
- **Attached to every subagent.** Subagent middleware *replaces* rather than
  inherits (the repeated footgun in CLAUDE.md; `agent.py:539,561`), so each
  in-tool role is constructed with its own guard explicitly. This is exactly what
  guarantees a *solver* subagent cannot `grep` the gold dir or the schema snapshot.
- **Solver ≠ supervisor.** Enforced by construction: the solver is a fresh
  `create_react_agent` invocation with no authoring context, spawned inside the
  tool.
- **Layout.** The CSV holds `question,gold_sql`. On the mount, at tool start
  separate it into a reader-visible `questions` file (questions only) and a
  **read-denied** `gold` file (the `gold_sql` column). The supervisor never reads
  *either* — even the question text is withheld from it (the aggregated-feedback
  boundary); only the in-tool solver reads `questions`.

## No data split — why the whole set is scored every round

An earlier draft held back a slice of questions (train/held-out/test) to detect
"teaching to the test." That mechanism is **dropped**, because the
aggregated-feedback boundary closes the same hole at a tighter seam:

- The classic overfitting risk is *"the loop tunes docs to the exact questions it
  measures on, so the number rises without the wiki generalizing."*
- Here the harness **cannot** do that: it never sees the questions, never sees the
  gold, and the feedback it gets back is a de-identified theme list, not a
  per-question failure. To lift the KPI it must write a **general fact** ("revenue
  excludes refunds") that is **verified against live data** — which helps any
  similar question, not just the measured one. There's no seam to overfit to.

So every question both drives improvement *and* measures quality, with no slice
sacrificed. The number reads as *"the wiki now covers what these N questions
probe"* — a fair quality measure, as good as the question set's coverage of the
dataset. Because each round is **stateless** (re-measures the current wiki from
scratch), the round-over-round trajectory is trustworthy signal.

This also sidesteps the small-N problem a split created: with no held-back gate
slice, there's no 4-question gate scoring in 25pp steps. The only small-N caution
is statistical honesty — at low N each question is worth a lot of percentage
points, so **persist the graded/discarded counts next to every KPI** (below) and,
optionally, snap a threshold to the achievable grid (`0.80` on 20 graded = "≥16").
No minimum-N gate is imposed; a tiny set just yields a coarse, clearly-labeled
number.

## KPI persistence

Per-iteration KPIs are written to the registry table as **siblings of the STATUS
row under the same partition**, correlated to the run by `runtime_session_id`:

- `pk = "HARVEST#<domain>#<dataset>"`, `sk = "BENCH#<runtime_session_id>#<iteration>"`
- attrs: `{iteration, runtime_session_id, ex_score, judge_accuracy, passed,
  failed, discarded, graded, genuine_error_count, threshold_met, created_at}`
  (`graded = passed + failed`; `ex_score = passed / graded`; discards excluded).
- a finalize row `sk = "BENCH#<runtime_session_id>#final"` carries the shipped
  iteration's numbers + `shipped_iteration` (which checkpoint finalize restored).

Design choices and why:

- **Not on the STATUS row.** `report_status` does `UpdateItem` touching only
  `status`/`updated_at`/`detail`/`model`/`effort` so it never clobbers
  `mode`/`started_at`/`runtime_session_id` (`status.py:93-126`), and the row is
  overwritten each run — it has no history slot.
- **Append-only `PutItem`, no read-modify-write.** Each `run_benchmark` call
  writes its own row (one loop, single-threaded per run, but append-only avoids
  any RMW race and mirrors how `okf-freshness` rows are written).
- **Session-id in the `sk`** solves the overwrite problem: a reader gets *this
  job's* KPIs by reading the STATUS row's `runtime_session_id` (exactly how the
  events feed correlates, `CONVENTIONS.md:191-193`) then
  `Query begins_with(sk, "BENCH#<session>")`. Prior runs' rows don't match, so
  they can't confuse the UI. Stale rows are pruned at `mark_in_progress` (or left
  — tiny).
- **Best-effort, reuses `build_registry_client()`** (`status.py:33-51`) — so **no
  new IAM** (the harvest role already writes the registry for status + guidance
  stamps). A KPI write failing must never crash the harvest, same discipline as
  `report_status`.
- **Also emit an `OKF_STEP` `kind:"benchmark"` event** per iteration so the UI
  shows KPIs *live* during the run (how the UI gets live data today); DynamoDB is
  the durable/queryable record. Two sinks, same split the STATUS row + live feed
  already use.

## Middleware monitor

`OKFGuardMiddleware` gains four responsibilities **for a recursive-improvement run
only** — all keyed off the guard's `ri_enabled` flag, so a normal harvest's guard
is unchanged. All at the tool boundary (same discipline as refusing a bad write —
`okf_guard.py:_prepare`):

- **Blinds (per role, always on when the deny set is non-empty).** Read-denylist
  applied per role: gold denied to every LLM role; raw data (`.metadata/`,
  `.context/`, sandbox) denied to the *solver* only; questions denied to the
  supervisor (see the access matrix above). Attached to every subagent.
- **Caps (only if `ri_enabled`).** Count `run_benchmark` calls; **hard-refuse the
  6th** (`max_iterations` clamped to 5). Refuse a config whose effective N exceeds
  100 (the tool clamps first, but the guard is defense-in-depth). A run that is
  *not* RI-enabled has no `run_benchmark` tool at all, so there's nothing to count.
- **Compels (only if `ri_enabled`).** Block `finalize` until the loop has actually
  run per the config (threshold met *or* max-iter reached). A normal harvest
  finalizes with no such gate — the compel exists precisely so an *enabled* run
  can't silently skip the loop.
- **Gates the result payload** — defense in depth on the tool's return value:
  scrub any gold, gold rows, question text, or per-`q_id` failure that somehow
  reaches the payload, so only the score + anonymous `improvements` themes cross
  to the supervisor (on top of the aggregated-by-construction adjudicator output).

## Token accounting

**All benchmark LLM spend rolls into the same live total the UI popover shows —
by construction, with zero extra wiring — because the benchmark reuses the one
`chat_model` instance that is already instrumented.** `UsageForwarder`
(`steps.py:657`) is a callback attached to that `chat_model` object
(`agent.py:459`); it fires `record_usage` (`steps.py:485`) on *every* invocation
of that model, "regardless of which graph/thread drives it," folding into the
cumulative `_usage` snapshot streamed as `OKF_STEP` CloudWatch lines →
`get_harvest_events` (`handlers.py:1310`) → popover. Authoring and benchmark spend
accumulate into **one** total, exactly as it already combines supervisor +
subagent turns.

Applied to the roles:

- **Blind-solver ReAct loops** → shared instrumented `chat_model` → tokens roll up
  **automatically**. The bulk of the spend, already covered.
- **Terminal `with_structured_output` coercion** → same instance → **automatic**
  (one extra call per question).
- **Adjudicator** → same instance → **automatic**.
- **Deterministic grader** → pure Python + Athena → **zero tokens**.

The **one hard rule**: every LLM call inside the black box goes through the shared
`chat_model`. A raw `boto3` `bedrock-runtime` `invoke_model`/`Converse` call
bypasses the instrumented instance and is **invisible** to the total (`steps.py`
metering is callback-bound; a raw boto3 call has no `on_llm_end`). Never use raw
boto3 for a model inference here. Escape hatch: `StepEmitter.record_usage(msg)`
(`steps.py:485`) with a `usage_metadata`-shaped object — but the clean path is
"use the shared model," and it should be a hard build constraint.

## Checkpoint and rollback

Edits are cumulative and in-place, and there is **no per-edit attribution** — a
fix for one theme can regress another (a revenue-metric edit that breaks a status
filter). Because each round re-scores the whole set statelessly, the score itself
is the rollback signal:

- Snapshot the working tree at each iteration whose score is a **new best**.
- Before `finalize`, **restore the best-scoring checkpoint** — do not necessarily
  ship the last iteration (the last round can score *lower* than an earlier one).
- Commit via the normal `finalize_bundle` (`finalize.py:22-83`, writes
  `.harvest/state.json` last), then write the `#final` KPI row with
  `shipped_iteration`. S3 versioning (`storage.tf`) is the durable backstop, but
  the in-run checkpoint is what the restore uses.

## Constraints and non-goals

- **8h session ceiling.** `(author → up to 100 solver loops → grade → adjudicate →
  revise) × 5` runs inside one AgentCore session capped at 8h
  (`agentcore_runtimes.tf:119-123`). Accepted as the real bound. The benchmark
  reuses the harvest `chat_model`, so solvers run at **full harvest effort** (no
  lowered tier) — the dominant token/latency term. The whole set is solved every
  round (no sampling — the score must be comparable round-to-round), so keep it fit
  with the **K=10 benchmark semaphore** (solver loops are I/O-bound on the model,
  so concurrency helps), **gold + predicted result caching** (unchanged questions
  cost ~nothing after round 0), and **early-stop on no-improvement** (if a round
  doesn't raise the best score, stop before max-iter; patience 1).
- **`recursion_limit` grows.** The internal loop eats the supervisor's step budget
  (full default 1000, `runner.py`); raise it for recursive-improvement runs and
  make the loop's step budget explicit. Env-configurable.
- **Serialize is free here.** One run, one lease — no `409` dance. This is a
  benefit of in-run vs external.
- **Fidelity caveat.** The in-run benchmark reads the bundle as *files*
  (`read_file`/`grep`); the real consumer reads over MCP with `semantic_search`.
  Same markdown, different retrieval. For a text-to-SQL consumer that reads pages
  this is acceptable — but the number measures "wiki-as-content," not
  "wiki-as-served." State this in any report.
- **Non-goal: an external orchestrator.** No Step Functions, no driver Lambda, no
  new DynamoDB job shape. There is zero orchestration primitive in the infra today
  and this feature deliberately adds none — the loop lives in the agent run.
- **Non-goal: general Q/A.** `gold_sql` answers only (the chosen format). Prose
  answers or expected-value answers would need a different grader and are out of
  scope.

## Integration points

| Change | Location |
|---|---|
| Per-role read-denylist (deny set as ctor param); wrap read tools in guarded set | `harvest/okf_guard.py:30,36,43-47,106-115` |
| Cap / compel / gate logic in `_prepare`, gated on `ri_enabled` | `harvest/okf_guard.py:92-136` |
| Conditionally append `run_benchmark` to `all_tools` when RI-enabled | `harvest/agent.py:471-486` (mirrors `run_code` conditional-append) |
| Solver / adjudicator on the SHARED `chat_model`; role-specific guard + tools | `harvest/agent.py:462, 531-601` |
| `SUPERVISOR_PROMPT` conditional RI section; skill guidance conditional | `harvest/prompts.py`; `services/harvest/skills/okf-authoring` |
| Grader PASS/FAIL/DISCARDED + gold/discard result cache | new; `harvest/benchmark.py` |
| Adjudicator (raw-data access) → anonymous `improvements` themes | new; `harvest/benchmark.py` |
| `recursive_improvement` payload block + validation; effort-tier derive | `docs/CONVENTIONS.md:279-361`, `control_api` `handlers.py`/`app.py:429-469` |
| Dataset RI settings on `DATASET#` row; populate payload every trigger | `control_api/handlers.py` (dataset upsert + all trigger paths) |
| CSV upload under `.benchmark/` | `control_api/handlers.py:966-1013` (presign) |
| Athena grader (set-equality, ported from `ex_compare.py`) | new; reuse `okf-sql-benchmark` comparator logic |
| Token metering — no change needed (reuses instrumented `chat_model`) | rides `steps.py:459,485,657` automatically |
| KPI rows (`BENCH#<session>#<iter>`), best-effort write | new fn in `harvest/status.py` (reuses `build_registry_client`) |
| `BENCH#` item shape + RI dataset settings as formal contract | `docs/CONVENTIONS.md:112`+ (registry table) |
| `OKF_STEP kind:"benchmark"` live event | `harvest/steps.py`, `control_api/handlers.py` `_parse_step_line` |
| `recursion_limit` raise for RI runs | `harvest/runner.py`, env knob |

## Resolved decisions

Previously-open questions, now decided (see the sections above):

- **Feedback boundary** → the harness receives a score + an anonymous, consolidated
  `improvements` theme list — never the questions, gold, or per-question failures.
  This is what removes the need for a data split. (*Aggregated feedback* / *The
  black-box benchmark tool* / *Adjudication*.)
- **Stateless rounds** → each `run_benchmark` re-measures the current wiki against
  the whole set from scratch; only loop bookkeeping + the discard set persist.
  (*The loop* / *No data split*.)
- **No train/test split** → dropped; every question scores *and* improves. (*No
  data split*.)
- **DISCARDED outcome** → gold that can't bind to the schema is factually
  unanswerable → excluded from both KPI numerator and denominator, reason
  reported, filtered from later rounds; a deterministic, data-driven (not wiki-
  driven) signal. (*Per-question grade*.)
- **KPI persistence** → DynamoDB `BENCH#<session>#<iteration>` rows on the harvest
  partition + an `OKF_STEP kind:"benchmark"` live event. (*KPI persistence*.)
- **Athena cost** → cache invariant gold results (+ discard verdicts) across
  rounds + workgroup result-reuse + per-query scan cutoff + own grader concurrency
  cap. (*Grading — deterministic*.)
- **Solver bundle-blind, adjudicator raw-data-armed** → per-role read-denylist:
  the solver is denied `.metadata/`/`.context/`/sandbox (it measures the wiki);
  the adjudicator gets them (it diagnoses gaps vs raw data). Gold denied to both.
  (*Prediction* / *Adjudication* / *access matrix*.)
- **Benchmark reuses the single harvest `chat_model` instance** → no second model,
  no separate effort (runs at full harvest effort); token metering rides the
  already-instrumented instance with zero extra wiring. (*Benchmark model* /
  *Token accounting*.)
- **Feature is optional, per-harvest** → the `recursive_improvement` block is the
  enable signal; it gates tool registration, the prompt/skill section, and the
  middleware bookkeeping. Off ⇒ a normal harvest is unchanged. Settings persist on
  the `DATASET#` row; applies to full/incremental/annotated. (*Enablement*.)

## Still open

- **`CONVENTIONS.md` write-up of the `BENCH#` row.** The item shape above must be
  added to the registry-table section (`CONVENTIONS.md:112`+) as a formal contract
  before code, since the Control API / UI will read it.
- **UI surface for KPI history + discards.** DynamoDB holds per-iteration history
  (incl. `discarded` counts + reasons); whether the harvest view renders a
  KPI-per-iteration chart and a discard list (vs just the latest live number) is a
  UI decision, not blocking the harvest-side build.
- **Consolidation granularity.** How aggressively the adjudicator groups genuine
  errors into `improvements` themes trades feedback specificity against
  de-identification strength — needs a prompt-tuning pass once real benchmark data
  exists.
- **Early-stop sensitivity.** "Stop if a round doesn't beat the best score" —
  should it allow one non-improving round before stopping (noise tolerance), or
  stop immediately? A patience of 1 is the likely default.
