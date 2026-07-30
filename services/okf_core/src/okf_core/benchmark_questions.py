"""Parse + cap the Benchmark Studio question set.

The user uploads a CSV with one row per question and **one gold column per
check** (``docs/BENCHMARK_GUIDE.md``):

```csv
question,gold_sql,expected_behavior
Which driver has the most wins?,"SELECT ...",
How long do pit stops take?,,Should say the wiki does not track pit-stop durations — not invent a number.
```

A question **participates in a check iff its gold cell for that check is
non-blank** — so one CSV drives both checks (Accuracy / SQL EX, Behavior), and
yesterday's two-column ``question,gold_sql`` files keep working unchanged
(they simply only participate in SQL EX). Unrecognized columns are ignored,
so a CSV carrying extra columns (including a retired ``gold_answer``) uploads
fine.

This is the ONE parser both the benchmark runtime (which loads the set to
grade) and the Control API (which validates the upload + reports per-check
counts to the UI) share — so the counts the UI shows are exactly what a run
will grade. It lives in ``okf_core`` (no AWS / agent deps) precisely so both
services can import it.

Invariants enforced here:

* **Hard cap at ``MAX_QUESTIONS`` (100).** If the CSV holds more valid rows, the
  FIRST ``MAX_QUESTIONS`` in file order are taken (deterministic → reproducible
  graded set across runs), and the drop count is reported so truncation is never
  silent. The cap has no per-check quota.
* **Stable ``q_id`` = position among kept rows** (file order), so it never
  reorders across solver / grader / judge / report.
* **Deterministic header resolution.** Accepted spellings are PRIORITY TUPLES,
  first match wins — a CSV carrying two accepted spellings of one column
  resolves identically in every process. (The old ``set`` + ``next()`` pick
  varied with hash seed across processes.)
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

# -- checks -------------------------------------------------------------------

#: Check identifiers — stable strings: the run config, the report JSON, the
#: progress events, and the UI all key off these.
CHECK_SQL = "sql"
CHECK_BEHAVIOR = "behavior"
ALL_CHECKS: tuple[str, ...] = (CHECK_SQL, CHECK_BEHAVIOR)

#: Question cap. At most this many questions are used, regardless of how many
#: the CSV holds (the first ``MAX_QUESTIONS`` valid rows in file order).
MAX_QUESTIONS = 100

# Accepted header spellings (case-insensitive, trimmed), in PRIORITY ORDER —
# tuples, not sets, so resolution is deterministic when a CSV carries more than
# one accepted spelling. The behavior names are deliberately tight and disjoint
# from the gold-SQL set, so no header can resolve to two columns.
_QUESTION_HEADERS: tuple[str, ...] = ("question", "nl", "nl_question")
_GOLD_SQL_HEADERS: tuple[str, ...] = ("gold_sql", "gold", "sql", "query")
_BEHAVIOR_HEADERS: tuple[str, ...] = ("expected_behavior", "behavior", "expectation")


@dataclass(frozen=True)
class BenchmarkQuestion:
    """One benchmark item. ``q_id`` is the 0-based file-order index (stable).

    A blank gold field means the question does not participate in that check;
    :meth:`checks` lists the ones it does. ``expected_behavior`` is free-form
    prose — what the agent SHOULD do (answer correctly, refuse, cite a caveat,
    honor a policy, not hallucinate…) — graded by the judge, never shown to
    the solver.
    """

    q_id: int
    question: str
    gold_sql: str = ""
    expected_behavior: str = ""

    def gold_for(self, check: str) -> str:
        """The gold cell backing ``check`` ("" = doesn't participate)."""
        return {
            CHECK_SQL: self.gold_sql,
            CHECK_BEHAVIOR: self.expected_behavior,
        }.get(check, "")

    def checks(self) -> tuple[str, ...]:
        """The checks this question participates in (non-blank gold cells)."""
        return tuple(c for c in ALL_CHECKS if self.gold_for(c))


@dataclass
class LoadResult:
    """Parsed question set + cap accounting + per-check participation counts."""

    questions: list[BenchmarkQuestion]
    total_in_csv: int  # valid rows found (blanks excluded), before the cap
    dropped: int  # valid rows beyond the cap that were not used
    # How many KEPT questions participate in each check — what the UI shows as
    # "sql: 62, behavior: 25" and what a run would actually grade.
    check_counts: dict[str, int] = field(default_factory=dict)


class BenchmarkCSVError(ValueError):
    """The CSV is unparseable or missing required columns."""


def _resolve_column(
    lookup: dict[str, str], headers: tuple[str, ...]
) -> str | None:
    """First accepted spelling present in the CSV header, in priority order."""
    for h in headers:
        if h in lookup:
            return lookup[h]
    return None


def _resolve_columns(
    fieldnames: list[str] | None,
) -> tuple[str, str | None, str | None]:
    """Map the header to (question, gold_sql?, expected_behavior?) columns.

    The question column is required, plus AT LEAST ONE gold column — a CSV that
    can't drive any check is rejected here, with the accepted spellings named.
    """
    if not fieldnames:
        raise BenchmarkCSVError("CSV has no header row")
    lookup = {(name or "").strip().lower(): name for name in fieldnames}
    q_col = _resolve_column(lookup, _QUESTION_HEADERS)
    sql_col = _resolve_column(lookup, _GOLD_SQL_HEADERS)
    behavior_col = _resolve_column(lookup, _BEHAVIOR_HEADERS)
    if q_col is None or (sql_col is None and behavior_col is None):
        raise BenchmarkCSVError(
            "CSV must have a question column (one of "
            f"{list(_QUESTION_HEADERS)}) and at least one gold column: "
            f"gold SQL (one of {list(_GOLD_SQL_HEADERS)}) or expected behavior "
            f"(one of {list(_BEHAVIOR_HEADERS)}); got headers {fieldnames}"
        )
    return q_col, sql_col, behavior_col


def load_questions(csv_text: str, *, max_questions: int = MAX_QUESTIONS) -> LoadResult:
    """Parse ``csv_text`` into capped, stable-id ``BenchmarkQuestion`` records.

    A row is VALID iff its question is non-blank AND at least one gold cell is
    non-blank; other rows are skipped (they can't be asked or graded in any
    check). ``q_id`` is the index among the KEPT rows, assigned in file order.
    The cap is applied AFTER skipping invalid rows, taking the first
    ``max_questions`` valid ones.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    q_col, sql_col, behavior_col = _resolve_columns(reader.fieldnames)

    valid: list[tuple[str, str, str]] = []
    for row in reader:
        question = (row.get(q_col) or "").strip()
        gold_sql = (row.get(sql_col) or "").strip() if sql_col else ""
        behavior = (row.get(behavior_col) or "").strip() if behavior_col else ""
        if not question or not (gold_sql or behavior):
            continue
        valid.append((question, gold_sql, behavior))

    total = len(valid)
    kept = valid[:max_questions]
    questions = [
        BenchmarkQuestion(q_id=i, question=q, gold_sql=s, expected_behavior=b)
        for i, (q, s, b) in enumerate(kept)
    ]
    check_counts = {
        check: sum(1 for q in questions if q.gold_for(check)) for check in ALL_CHECKS
    }
    return LoadResult(
        questions=questions,
        total_in_csv=total,
        dropped=max(0, total - len(questions)),
        check_counts=check_counts,
    )
