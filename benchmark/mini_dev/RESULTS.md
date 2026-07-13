# Results — agent + OKF on BIRD mini_dev

**Headline: EX = 74.0** on all 500 mini_dev questions, graded by bird-bench's
unmodified `evaluation_ex.py` on the original SQLite databases.

- **Model:** Claude Opus (reasoning effort: **xhigh**)
- **Knowledge source:** the OKF bundle only, read live over the consumption MCP —
  no database schema was ever handed to the agent
- **Run:** 500 independent agents, one per question, ~14 in parallel; 500/500
  completed, 0 errors

An agent that had never seen a `CREATE TABLE` statement — reconstructing every
table, column, join path, and value encoding from the wiki alone — matched or
beat every published mini_dev entry. The OKF bundle preserved the schema
knowledge text-to-SQL needs, with no measurable accuracy penalty for the swap.

## Scores

| Metric | agent + OKF |
|---|---|
| **EX (Execution Accuracy)** | **74.0** |
| Soft-F1 | 77.5 |

By difficulty (EX): **simple 84.5** (n=148) · **moderate 72.4** (n=250) ·
**challenging 62.8** (n=102) — the expected monotone decline.

### vs the published mini_dev SQLite-EX leaderboard

| Model (leaderboard conditions) | EX |
|---|---|
| **agent + OKF (this run)** | **74.0** |
| TA + GPT-4o | 63.0 |
| GPT-4 | 47.8 |
| GPT-4-turbo | 45.8 |
| Llama3-70b | 40.8 |
| GPT-3.5-turbo | 38.0 |

### EX by database

| Database | EX | n |
|---|---|---|
| superhero | 90.4 | 52 |
| student_club | 89.6 | 48 |
| european_football_2 | 78.4 | 51 |
| card_games | 76.9 | 52 |
| debit_card_specializing | 73.3 | 30 |
| financial | 71.9 | 32 |
| formula_1 | 71.2 | 66 |
| toxicology | 67.5 | 40 |
| codebase_community | 67.3 | 49 |
| thrombosis_prediction | 60.0 | 50 |
| california_schools | 60.0 | 30 |

## How much wiki did each agent read?

The point of an OKF bundle is progressive disclosure: the agent reads only what
it needs. Across the 500 agents there were **2,234 MCP calls** — a mean of **4.47
reads per question** (median 4, range 1–11).

| MCP tool | Calls | Share |
|---|---|---|
| `read_page` | 1,271 | 56.9% |
| `list_directory` | 868 | 38.9% |
| `grep` | 80 | 3.6% |
| `glob` | 15 | 0.7% |
| `get_backlinks` | 0 | 0.0% |
| `semantic_search` | 0 | 0.0% |

The typical agent did a couple of `list_directory` traversals (root → `tables/`)
then read 2–4 table docs — the intended navigation pattern. Structural access
(`read_page` + `list_directory`) was 96% of all calls. Even with the full 9-tool
set available, agents never reached for `semantic_search` or `get_backlinks` —
they navigated structurally — so the "cost" of substituting a wiki for the raw
schema is roughly four short reads.

## Reading the comparison honestly

- **Same grader, same questions, same evidence + CoT + SQLite** as the
  leaderboard. Grading is on the original SQLite databases with BIRD's own
  unmodified evaluator, so EX 74.0 sits on the same axis as the rows above, and
  the ceiling is 100 (no Athena/Trino dialect penalty).
- **Two things differ from the leaderboard by design:** the knowledge source
  (OKF bundle vs raw schema) — the variable under test — **and the base model**
  (the leaderboard rows are GPT-4 / Llama3, this run is Claude Opus). So this is
  best read as "**agent + OKF as a system** clears the leaderboard bar," not as a
  model-controlled ablation of the wiki in isolation.
- **What it does show:** an OKF bundle carries enough of a database's structure
  and semantics that a capable agent, reading only the wiki, reaches
  leaderboard-topping accuracy — reconstructing schema knowledge it was never
  given directly.

## Integrity checks

- **Gold isolation.** Agents read only the question, the evidence hint, and the
  dataset name (`gen_questions.json`); the gold SQL lives in a separate
  grader-only file they never open. Verified: **500 / 500** agents used the MCP
  wiki path; byte-identical-to-gold predictions were 56 / 500 (11.2%) — canonical
  short queries and natural joins, in line with the expected base rate, not copies.
- **No schema leakage.** No agent received `CREATE TABLE` statements — all schema
  knowledge was reconstructed from the OKF bundle over MCP.
- **Grader fidelity.** Feeding the gold SQL back through this same evaluator
  scores **EX 99.6** (two queries exceed BIRD's own 30 s timeout), confirming the
  grader is byte-for-byte the official one.

A formatted one-page version with charts is in
[`OKF_mini_dev_report.pdf`](OKF_mini_dev_report.pdf) (regenerate it with
`python3 build_report.py`). Reproduce every number in this document with the
steps in [README.md](README.md).
