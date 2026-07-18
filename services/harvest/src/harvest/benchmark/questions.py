"""Parse + cap the benchmark question set — deterministic, in-memory, gold-safe.

The user uploads a ``question,gold_sql`` CSV. This module parses it into
``BenchmarkQuestion`` records that live only in the ``run_benchmark`` tool
process's memory — the gold SQL is NEVER written to the agent-visible mount, which
is what makes gold-blindness physical (see ``docs/RECURSIVE_IMPROVEMENT.md``).

Two invariants enforced here:

* **Hard cap at ``MAX_QUESTIONS`` (100).** If the CSV holds more, the FIRST
  ``MAX_QUESTIONS`` rows in file order are taken (deterministic → reproducible
  scored set across rounds), and the drop count is reported so truncation is never
  silent.
* **Stable ``q_id`` = file order.** The 0-based row index is the question's id
  everywhere downstream (grader, KPIs, adjudicator), so it never reorders.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from okf_core.recursive_improvement import MAX_QUESTIONS

# Accepted header spellings (case-insensitive, trimmed). The contract is
# `question,gold_sql`; we also accept a couple of obvious synonyms for the gold
# column so a hand-authored CSV isn't rejected on a trivial header mismatch.
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
    """Parsed question set + how many rows were dropped by the cap."""

    questions: list[BenchmarkQuestion]
    total_in_csv: int
    dropped: int


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
    or asked) but still count toward file position — ``q_id`` is the index among
    the KEPT rows, assigned in file order, so the scored set is stable. The cap is
    applied AFTER skipping blanks, taking the first ``max_questions`` valid rows.
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
