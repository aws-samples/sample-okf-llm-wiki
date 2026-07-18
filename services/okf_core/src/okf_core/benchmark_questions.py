"""Parse + cap the recursive-improvement benchmark question set.

The user uploads a ``question,gold_sql`` CSV. This is the ONE parser both the
harvest runtime (which loads the set to benchmark) and the Control API (which
validates the upload + reports the extracted count to the UI) share — so the
count the UI shows is exactly the count the runtime will benchmark. It lives in
``okf_core`` (no AWS / agent deps) precisely so both services can import it.

Two invariants enforced here:

* **Hard cap at ``MAX_QUESTIONS`` (100).** If the CSV holds more valid rows, the
  FIRST ``MAX_QUESTIONS`` in file order are taken (deterministic → reproducible
  scored set across rounds), and the drop count is reported so truncation is never
  silent.
* **Stable ``q_id`` = position among kept rows** (file order), so it never
  reorders across grader / KPI / adjudicator.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from okf_core.recursive_improvement import MAX_QUESTIONS

# Accepted header spellings (case-insensitive, trimmed). The contract is
# `question,gold_sql`; we also accept a couple of obvious synonyms for each column
# so a hand-authored CSV isn't rejected on a trivial header mismatch.
_QUESTION_HEADERS = {"question", "nl", "nl_question"}
_GOLD_HEADERS = {"gold_sql", "gold", "sql", "query"}


@dataclass(frozen=True)
class BenchmarkQuestion:
    """One benchmark item. ``q_id`` is the 0-based file-order index (stable)."""

    q_id: int
    question: str
    gold_sql: str


@dataclass
class LoadResult:
    """Parsed question set + how many valid rows were dropped by the cap."""

    questions: list[BenchmarkQuestion]
    total_in_csv: int  # valid rows found (blanks excluded), before the cap
    dropped: int  # valid rows beyond the cap that were not used


class BenchmarkCSVError(ValueError):
    """The CSV is unparseable or missing a required column."""


def _resolve_columns(fieldnames: list[str] | None) -> tuple[str, str]:
    """Map the CSV header to (question_col, gold_col); raise if either is absent."""
    if not fieldnames:
        raise BenchmarkCSVError("CSV has no header row")
    lookup = {(name or "").strip().lower(): name for name in fieldnames}
    q_col = next((lookup[h] for h in _QUESTION_HEADERS if h in lookup), None)
    g_col = next((lookup[h] for h in _GOLD_HEADERS if h in lookup), None)
    if q_col is None or g_col is None:
        raise BenchmarkCSVError(
            "CSV must have a question column (one of "
            f"{sorted(_QUESTION_HEADERS)}) and a gold-SQL column (one of "
            f"{sorted(_GOLD_HEADERS)}); got headers {fieldnames}"
        )
    return q_col, g_col


def load_questions(csv_text: str, *, max_questions: int = MAX_QUESTIONS) -> LoadResult:
    """Parse ``csv_text`` into capped, stable-id ``BenchmarkQuestion`` records.

    Rows with a blank question OR blank gold_sql are skipped (they can't be graded
    or asked). ``q_id`` is the index among the KEPT rows, assigned in file order,
    so the scored set is stable. The cap is applied AFTER skipping blanks, taking
    the first ``max_questions`` valid rows.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    q_col, g_col = _resolve_columns(reader.fieldnames)

    valid: list[tuple[str, str]] = []
    for row in reader:
        question = (row.get(q_col) or "").strip()
        gold = (row.get(g_col) or "").strip()
        if not question or not gold:
            continue
        valid.append((question, gold))

    total = len(valid)
    kept = valid[:max_questions]
    questions = [
        BenchmarkQuestion(q_id=i, question=q, gold_sql=g)
        for i, (q, g) in enumerate(kept)
    ]
    return LoadResult(
        questions=questions,
        total_in_csv=total,
        dropped=max(0, total - len(questions)),
    )
