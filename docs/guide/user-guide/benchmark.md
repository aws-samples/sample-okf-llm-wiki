# Benchmark a wiki

Benchmark Studio measures how well a selected wiki snapshot supports real questions.

## Prepare a question set

Upload a UTF-8 CSV with one row per question. The file can be at most 20 MiB.

```csv
question,gold_sql,expected_behavior
Which driver has the most wins?,"SELECT ...",
How long do pit stops take?,,State that the wiki does not track pit-stop duration.
```

Use `gold_sql` for execution accuracy. Use `expected_behavior` for answer quality, honesty, or policy checks.

Each valid row needs a question and at least one nonempty gold field. Rows without both are skipped.

Benchmark Studio keeps the first 100 valid rows. Additional valid rows are reported as dropped.

You can also generate a question bank from source metadata and context documents.

## Start a benchmark

1. Select a dataset.
2. Open **Benchmark**.
3. Upload or generate a question set.
4. Choose the checks.
5. Choose whether the Behavior solver can use live SQL.
6. Choose solver and judge settings.
7. Select between one and five independent runs.
8. Select a wiki version.
9. Start the benchmark.

A benchmark does not take the harvest lease.

The **Current** version reads the mutable live tree. Wait for an active harvest to finish, or select a completed version.

A selected version pins published Markdown only. Schema metadata, context documents, and source data remain current.

Behavior reports with live SQL are not directly comparable with wiki-only reports. Accuracy solvers remain data-blind.

## Read the report

Review these results:

- Raw scores for each check
- Judge-adjusted score for Accuracy
- Pass stability across runs
- Discarded questions
- Solver traces
- Judge comments
- Token and tool telemetry

Behavior results are already judge-graded. They do not receive a second adjusted score.

A flaky question has both passing and failing runs. A discarded question could not be graded.

Inspect solver traces before assigning a cause. Tool errors, model errors, and weak discoverability can all affect results.

## Improve the wiki

Generate annotations from confirmed failures. Review and edit every annotation before filing it.

Run an annotation harvest after you approve the changes. Benchmark the new version to confirm the result.

!!! important
    Keep gold answers outside the wiki. Solvers must not see the answer key.
