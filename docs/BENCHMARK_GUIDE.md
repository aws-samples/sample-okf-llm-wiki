# Benchmark Studio — user guide

Data Wiki can **measure how well a dataset's wiki answers real questions.** You
upload a question set, configure a run (which checks, which models, how many
independent runs, which wiki version), start it, and read a persisted report —
including suggested wiki annotations you review and apply.

Benchmarking is **standalone and human-led**: it never runs inside a harvest,
there is no automatic improve-and-rescore loop, and no stop target. (The old
in-harvest "recursive improvement" loop is retired — a harvest only authors;
you decide when and what to measure.)

This guide is the **how-to**. The `okf_*`/`OKF_` prefix refers to the Open
Knowledge Format; the payload, `REPORT#` row, and artifact shapes are specified
in [`CONVENTIONS.md`](./CONVENTIONS.md).

---

## What a run does (in one paragraph)

Independent "solver" agents each answer one question using **only the wiki**
(they cannot see the database schema or your answer key), once per enabled
check, in each of N independent runs. Their answers are graded per check —
result-set equality for **Accuracy (SQL EX)** (BIRD semantics: row order
ignored, column order within a row meaningful, numeric cells compared by
value so `3` and `3.0` match); for **Behavior** there is no
deterministic grade at all: the **judge** rules on every run independently
against your free-form expectation (with the real schema, live data, and the
solver's own step-by-step trace in hand). After all runs finish, the same
judge (always on) reviews every failed Accuracy question once and rules each
failure *confirmed* (the wiki's fault, with a comment and often a suggested
doc fix) or *overturned* (bad gold, ambiguous question). Behavior failures
skip that overturn review (their grader already was the judge) — instead,
each failed Behavior question gets ONE **synthesis review**: the judge reads
all N graded runs together (the cross-run diff the independent gradings never
saw), writes the question-level diagnosis, and consolidates the per-run
suggestions into one annotation. The report persists
everything: scores (raw + judge-adjusted for Accuracy, mean ± spread),
per-question stability, every attempt's output and steps, the judge's
verdicts, and telemetry.

Two properties make the score trustworthy:

- **The solvers never see your gold.** The answer key lives off the agents'
  reach; only the deterministic grader and the judge read it.
- **Runs are independent** — each question is re-solved from scratch per run,
  so flaky questions (passed some runs, not others) stand out as exactly the
  docs that exist but are buried or unclear.

---

## Step 1 — Prepare a questions CSV

One CSV, one row per question, **one gold column per check**:

```csv
question,gold_sql,expected_behavior
Which driver has the most wins?,"SELECT ...",
How long do pit stops take?,,Should say the wiki does not track pit-stop durations — not invent a number.
Delete last season's results.,,Should refuse: the wiki is read-only for consumers.
```

- A question **participates in a check iff its gold cell for that check is
  non-blank.** One CSV drives both checks; a two-column
  `question,gold_sql` file keeps working (Accuracy only).
- **`gold_sql`** (Accuracy / SQL EX) must run on Athena/Trino against this
  dataset — a gold that doesn't execute is DISCARDED (excluded from the
  score). Porting from SQLite? Translate the dialect first
  (`benchmark/translate_gold_to_athena.py` is a worked example).
- **`expected_behavior`** (Behavior) is **free-form prose**: what the agent
  *should do* — answer with a specific fact, say something isn't tracked
  instead of inventing it, refuse, honor a policy the wiki states, cite a
  caveat — any nuance you can write down. The solver never sees it; the judge
  grades **every run** against it. Use it to test for hallucinations and
  policy adherence, not just correctness.
- **Up to 100 questions** are used (first 100 valid rows, in file order).
  Header spellings are case-insensitive with a few synonyms; when two accepted
  spellings appear, resolution is deterministic (priority order). Unrecognized
  columns are ignored (a retired `gold_answer` column no longer resolves).

Upload it on the **Benchmark** tab (gauge icon). The file lands off the agents'
reach; the page immediately shows per-check counts ("sql: 62, behavior: 25") —
exactly what each check would grade.

---

## Step 2 — Configure and start a run

On the same page:

1. **Checks to run** — any subset of Accuracy (SQL EX) / Behavior (≥ 1).
   Each enabled check gets its own independent solver round per run — results
   are never confounded by sharing one solve. The Behavior solver is the
   closest simulation of a real consumer: its prompt teaches the wiki's
   structure (start at `index.md`, table docs under `tables/`, …) and it
   answers in free-form prose. **Behavior solver: live SQL** (optional,
   default off) additionally hands it read-only `run_sql` against the live
   data — an even truer simulation of an agent with query access; the wiki
   still leads (names, joins, caveats, policies come from the docs) and SQL
   verifies/executes. Accuracy solvers always stay SQL-blind (they could
   brute-force EX otherwise). Reports carry the flag ("live SQL" badge) —
   scores aren't comparable across different settings of it.
2. **Solver model + effort** — the consumer being simulated. Benchmark with the
   model your agents will actually run on (often cheaper than the authoring
   model). **Judge model + effort** — the reviewer; usually keep it strong. The
   annotation aggregator (step 4) inherits the judge's model.
3. **Independent runs (1–5)** — N runs turn a point sample into mean ± spread
   and per-question stability. 3 is a good default.
4. **Wiki version** — current, or any published bundle version from the version
   history. **Pinning pins the WIKI, not the DATA**: grading always executes
   against live Athena, so compare versions by running both *now*, not against
   a months-old report.
5. **Start benchmark.**

A run takes **no harvest lease** — it writes nothing to the wiki, so it runs
concurrently with harvests and with other benchmark runs. The report list shows
live progress: per-run phases read `r/N · Check · Solving k/n` (and
`· Grading k/n` for Accuracy), while the cross-run phases that follow —
Behavior grading over all runs, then the judge reviews — drop the run part
(`Behavior · Grading k/(N×n)`, `Accuracy · Judging k/failures`), since they
span every run. A failed run shows its error inline. Reports persist until
you delete them (a run stuck "running" because its container died becomes
deletable once its last heartbeat is over 8 hours old).

**Cost note:** expect N × (enabled checks) × questions solver calls, plus one
judge review per failed Accuracy question, plus **one judge grading per
Behavior attempt** (N runs × behavior questions — the judge is the grader
there, so Behavior is the token-heavier check at high N) plus one synthesis
review per failed Behavior question, plus Athena executions for SQL EX (gold
executes once per report, cached). Every solver/judge/aggregator agent runs
with Bedrock prompt caching attached (on Claude models each ReAct turn's
growing conversation bills as cache reads; on GPT the Responses API caches
implicitly), so long explorations cost far less than raw input pricing
suggests. The report's telemetry shows the actual token spend by role.

---

## Step 3 — Read the report

Click a completed run. **Summary** shows, per check:

- **Raw score** — mean across the N runs (± spread, the min–max range).
  `raw = passed / graded`; DISCARDED questions are excluded entirely.
- **Judge-adjusted score** (Accuracy only) — raw plus the failures the judge
  overturned (bad gold / ambiguous question — not the wiki's fault). Always
  ≥ raw. Behavior has no adjusted score: its raw outcomes already carry the
  judge's authority, and it never overturns itself.
- **Breakdown** — passed every run / flaky / overturned / confirmed failed /
  discarded, plus the **stability distribution** (how many questions passed
  N/N vs k/N vs 0/N). Flaky questions are the most actionable class: the wiki
  *has* the information, but it's buried or unclear.
- **Telemetry** — solver/judge token spend, per-tool call distribution
  (`read_me`/`read_file`/`glob`/`grep`/`ls`), average solve time.

**Detailed** lists every question: per-check outcome chips with stability,
expanding to the gold, every attempt's output and grading reason, the judge's
verdict + comment (+ suggested annotation), and every attempt's
**step-by-step trace** (what it reasoned, searched, and read — passing runs
included), the same evidence the judge used.

---

## Step 4 — Generate and apply annotations

Failures the judge confirms often carry a **suggested annotation** — a
dataset-level doc fix ("state explicitly that pit-stop durations are not
tracked"). From the report header:

1. **Generate annotations** — an aggregator agent dedupes/merges the judge's
   suggestions into a final set (several questions tripping on one undocumented
   join → one annotation), verifying any doc targets exist. It runs on the
   judge's model; progress shows on the report.
2. **Review** the final set: select, edit, deselect. Nothing exists until you
   choose it — the de-identification boundary is *you* (annotations are wiki
   guidance, never Q/A pairs or gold to memorize).
3. **File** the selected annotations (they become normal annotations,
   provenance `benchmark`) and optionally **start the annotation harvest**
   right away — the standard annotation run folds them into the wiki.
4. Benchmark the new version and compare reports to prove the delta.

---

## Tuning (advanced)

Environment variables on the harvest runtime (defaults are fine for most
datasets; full descriptions in [`CONVENTIONS.md`](./CONVENTIONS.md)):

| Env var | Default | What it does |
|---|---|---|
| `OKF_BENCHMARK_MAX_CONCURRENCY` | `10` | Peak concurrent solver (and judge) model requests. Lower on `ThrottlingException`. |
| `OKF_BENCHMARK_ATHENA_CONCURRENCY` | `15` | Peak concurrent grading queries. Keep under the Athena workgroup's concurrent-DML limit. |

---

## Troubleshooting

- **"Invalid question set" on upload** — a missing question/gold column or
  bad UTF-8 (the error says what's missing).
- **"No question participates in the selected checks"** — the CSV has no gold
  cells for the checks you enabled; add the matching gold column.
- **Lots of DISCARDED (SQL EX)** — your gold SQL isn't running on Athena;
  translate it to Trino against this dataset's tables.
- **Behavior failing on answers that look fine** — read the judge's per-run
  comment (it's the grading reason on each attempt) and the question-level
  synthesis in the judge box (it names the cross-run pattern): the expectation
  may demand something the answer skipped (an acknowledgment, a citation, a
  refusal). Vague expectations grade vaguely — write the demand explicitly.
- **The run failed immediately** — the row's error says why (no published wiki
  docs, unknown pinned version, unreadable CSV). Failures are loud by design.
- **High raw score variance (spread)** — the wiki answers inconsistently; look
  at the flaky questions' traces to see what the passing runs read that the
  failing ones didn't.
