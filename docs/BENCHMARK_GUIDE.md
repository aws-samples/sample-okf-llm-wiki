# Benchmark & auto-improve — user guide

Data Wiki can **grade a dataset's wiki against real questions and let the harvester
keep improving it until it's good.** You upload a set of questions with their
correct SQL answers; when the feature is on, every harvest of that dataset runs a
loop: score the wiki → find what's missing → revise the docs → re-score, until the
answers are good enough or it runs out of iterations.

This guide is the **how-to**. The `okf_*`/`OKF_` prefix refers to the Open
Knowledge Format; the DynamoDB item shapes, payload block, and env vars are
specified in [`CONVENTIONS.md`](./CONVENTIONS.md).

---

## What it does (in one paragraph)

After the normal authoring pass, the harvest agent benchmarks the wiki it just
wrote. Independent "solver" agents each answer one question using **only the wiki**
(they cannot see the database schema or your answer key), and their SQL is graded
against the live data. Failures are reviewed by an adjudicator that decides whether
each one is a **genuine wiki gap** (something the docs should have said) or just
noise. The genuine gaps come back as an anonymous list of improvements; the agent
revises the docs and runs the benchmark again. The wiki that ships is whatever the
agent leaves at the end.

Two properties make the score trustworthy:

- **The agent never sees your questions or gold SQL.** The answer key lives off the
  harvest filesystem, and the only feedback that crosses back is a de-identified
  "what to improve" list — so the agent can't teach to the test, it can only make
  the docs genuinely better.
- **The wiki is the only thing carried between rounds.** Each round re-measures the
  current docs from scratch, so the round-over-round trajectory is real signal.

---

## Step 1 — Prepare a questions CSV

A CSV with two columns: the natural-language **question** and its correct **gold
SQL** against this dataset.

```csv
question,gold_sql
How many races were held in 2020?,SELECT COUNT(*) FROM races WHERE year = 2020
Which driver has the most wins?,"SELECT d.forename, d.surname FROM ..."
```

- **Headers** are case-insensitive and a few synonyms are accepted:
  question column = `question` / `nl` / `nl_question`; gold column = `gold_sql` /
  `gold` / `sql` / `query`.
- **Up to 100 questions** are used. If the CSV has more valid rows, the **first
  100 in file order** are taken (deterministic, so the scored set is reproducible)
  and the extras are dropped with a log note.
- Rows with a blank question **or** blank gold SQL are skipped.
- **Gold SQL must run on Athena/Trino**, against this dataset's Glue tables — not
  SQLite or another dialect. A gold query that doesn't execute is counted
  **DISCARDED** (see [Reading the results](#step-4--read-the-results)); it can't be
  graded, so it's excluded from the score. If you're porting a question set from a
  SQLite-based benchmark, translate the dialect first. (`benchmark/formula_1_questions_athena.csv`
  in this repo is a worked example — a BIRD `formula_1` set translated to Trino.)

The gold SQL only needs to return the **right answer**; it doesn't need to match
how the wiki would phrase a query.

---

## Step 2 — Upload it and turn the benchmark on

In the UI, open the **Benchmark** tab (gauge icon) and pick the dataset in the
sidebar.

1. **Upload questions CSV.** The file is stored **off the harvest mount** (under a
   `benchmark/…` key the authoring agent can't read), then parsed with the same
   parser the harvest runtime uses — so you immediately see *"N questions will be
   benchmarked"* (and a note if it was capped from a larger file, or an error if
   the format is wrong). Re-uploading replaces the set.
2. **Status → Enabled.**
3. **Max iterations (2–5)** — how many benchmark→improve rounds the harvester may
   run before it has to stop. Values outside 2–5 are clamped.
4. **Save settings.**

The setting is saved on the dataset, so it applies to **every** subsequent harvest
of that dataset (full, incremental re-harvest, and annotation runs) until you turn
it off — no need to re-upload.

There is **no accuracy target to set.** The goal is fixed: the loop stops once the
reviewed answers are **~90% good** (see the goal below). The point of the feature
is a better wiki, not a tunable score.

---

## Step 3 — Run a harvest

Trigger a harvest for the dataset as usual (Harvest tab). When the benchmark is
enabled you'll see extra rows in the live feed:

- **`Benchmark · Round N/M — Solving / Grading / Reviewing`** with a progress bar,
  as each round works through its phases.
- A **round-done** row with the KPIs (EX, judge, passed/graded, discarded) and a
  **Target met / Below target** badge.

The harvester will not finish the run until it has benchmarked at least once and
either met the target or spent its iteration budget — it's re-prompted to keep
going if it tries to stop early.

**Cost note:** the benchmark reuses the run's single model instance, so its token
usage folds into the harvest's normal usage total. A run with the benchmark on does
more work (N solvers × up to M rounds, plus grading and review), so expect it to
take longer and cost more than a plain harvest.

---

## Step 4 — Read the results

**KPIs** (on each round-done row, and persisted per round):

- **EX (exact match)** — fraction of graded questions whose wiki-derived SQL
  produced exactly the right result set. `EX = passed / graded`.
- **Judge accuracy** — the "genuine correctness" rate: passes, plus failures the
  reviewer confirmed were *not* the wiki's fault (bad or ambiguous questions), over
  graded. Judge is always ≥ EX. **This is the stop gate:** the loop is done once
  **judge ≥ 90%** (with at least one real pass, so a wiki that answers nothing
  correctly can never be declared "done").

**Click a completed round** to open the **review** — every question grouped into
tabs by what the reviewer decided. Each card shows the question, the reviewer's
note, and the wiki-derived vs. expected SQL side by side. (This detail, including
the gold SQL, is shown only here — the harvester never sees it.)

| Tab | Meaning |
|---|---|
| **Passed** | The wiki led to SQL that matched the expected answer. |
| **Genuine gaps** | The reviewer confirmed the docs were missing/wrong about something the answer needed. These drive the `improvements`. |
| **Noisy gold** | The expected answer itself looks wrong against the data → not the wiki's fault. Dropped from later rounds. |
| **Ambiguous** | The question is under-specified (or the fact was already documented) → not a wiki gap. Dropped from later rounds. |
| **Unclassified** | The reviewer couldn't reach a verdict. Counts against the wiki until resolved. |
| **Discarded** | The **gold SQL itself couldn't run** against the data (dialect/schema mismatch) → unanswerable, excluded from the score. Fix or remove these questions in the CSV. |

Questions the reviewer marks **noisy** or **ambiguous** are pruned from later
rounds (they're not wiki defects), so the graded set can shrink between rounds —
focus on the improvements and the trajectory, not the raw counts.

---

## How the loop stops

A run's benchmark loop ends at the **first** of:

- **Target met** — judge accuracy ≥ 90% (with EX > 0). The wiki is good enough.
- **Iteration budget spent** — it ran `max_iterations` rounds without meeting the
  target. The wiki ships as-is with whatever improvements landed.

Either way, **the wiki ships exactly as the agent left it** — there is no automatic
rollback to a higher-scoring earlier round. If a revision made things worse, the
agent is expected to fix or revert it before finishing.

---

## Turning it off

Set **Status → Off** on the Benchmark tab and Save. The saved questions CSV and
settings are kept (so you can re-enable without re-uploading), but harvests run as
normal with no benchmarking.

---

## Tuning (advanced)

These environment variables on the harvest runtime tune the benchmark; defaults are
fine for most datasets. Full descriptions are in [`CONVENTIONS.md`](./CONVENTIONS.md).

| Env var | Default | What it does |
|---|---|---|
| `OKF_BENCHMARK_MAX_CONCURRENCY` | `10` | How many solvers (and reviewers) run at once. This is the peak concurrent model requests from the benchmark — lower it if you hit `ThrottlingException`. |
| `OKF_BENCHMARK_ATHENA_CONCURRENCY` | `15` | How many grader queries run against Athena at once. Keep under the Athena workgroup's concurrent-DML limit. |
| `OKF_BENCHMARK_RECURSION_LIMIT` | (raised default) | LangGraph step budget for a benchmark run, since the in-run loop consumes extra steps. |

---

## Troubleshooting

- **"Invalid question set" on upload** — the CSV is missing a question or gold-SQL
  column (check the accepted header spellings above) or isn't valid UTF-8 CSV.
- **Lots of DISCARDED** — your gold SQL isn't running on Athena. It's likely
  written for a different SQL dialect/schema; translate it to Trino against this
  dataset's Glue tables.
- **Many questions land in Ambiguous / judge is high but EX is low** — often the
  solver returns *extra columns* beyond what a question asks, so set-equality
  grading fails even when the core answer is right, and the reviewer forgives it as
  "ambiguous." Tightening the questions (ask for specific columns) reduces this.
- **The benchmark never seems to stop early** — it won't finish before running at
  least one round; if the target isn't met it runs to `max_iterations`. Lower
  `max_iterations` to cap the work.
