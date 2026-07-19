"""Deterministic Athena-backed EX grader — the trustworthy, zero-LLM core.

Ports the BIRD-EX comparator (``~/.claude/skills/okf-sql-benchmark/scripts/
ex_compare.py``) from SQLite to Athena: execute the gold SQL and the predicted
SQL, compare their result sets as **unordered sets of rows**
(``set(pred) == set(gold)``). No LLM, no agent tool layer — the gold SQL lives in
the tool-process memory here and never touches the agent-visible mount, which is
what makes gold-blindness physical (see ``docs/CONVENTIONS.md``).

Three outcomes per question (``PASS`` / ``FAIL`` / ``DISCARDED``):

* **DISCARDED** — the GOLD SQL itself can't execute (missing column/table, name
  mismatch, any bind/exec error). The question is factually unanswerable; no wiki
  could make it gradeable. Excluded from BOTH KPI numerator and denominator.
* **FAIL** — gold ran, predicted was wrong or errored → a genuine wiki gap.
* **PASS** — gold ran, predicted ran, result sets are set-equal.

The grader is injected an ``execute(sql) -> rows`` callable (the harvest source's
``run_query``) so it is unit-testable with a fake and carries no boto3 import.
Two caches make the loop affordable across rounds:

* **gold cache** — gold SQL is invariant across rounds (the wiki changes, not the
  answer key), so each gold query executes at most once per run; its rows (or its
  DISCARDED verdict) are memoized by SQL text.
* **prediction cache** — an identical predicted SQL (a question the agent didn't
  affect this round) reuses its prior comparison verdict.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

# Rows come back from the source as list[dict] (header-keyed). We compare on a
# canonical, order-insensitive form: a multiset of value-tuples per row, with the
# COLUMNS also order-insensitive within a row (BIRD compares row value-sets, and
# a text-to-SQL answer that selects the right values in a different column order
# is correct). Cells are stringified for stable hashing (Athena returns strings).
Row = dict[str, Any]
Execute = Callable[[str], list[Row]]


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    DISCARDED = "DISCARDED"


@dataclass
class QuestionResult:
    """One graded question. ``predicted_sql`` is kept for the adjudicator; the
    gold SQL and gold rows are NEVER stored on this object (they must not leak
    past the grader)."""

    q_id: int
    outcome: Outcome
    predicted_sql: str = ""
    reason: str = ""
    # Small, gold-free samples for the adjudicator to reason over (predicted side
    # only + row counts). Bounded so a huge result can't bloat memory/logs.
    pred_rowcount: int | None = None
    gold_rowcount: int | None = None
    pred_sample: list[list[str]] = field(default_factory=list)
    discard_reason: str = ""


_SAMPLE_ROWS = 5


def _canonical(rows: list[Row]) -> set[tuple[str, ...]]:
    """Order-insensitive multiset key for a result set.

    Each row → a *sorted* tuple of its stringified cell values (column order
    doesn't matter), and the set of those tuples ignores row order. ``None``
    (SQL NULL) is distinguished from the empty string so a genuine NULL mismatch
    still fails. Note: as a set this drops true duplicate rows — matching BIRD's
    ``set(pred) == set(gold)`` exactly.
    """
    canon: set[tuple[str, ...]] = set()
    for row in rows:
        cells = tuple(sorted("\x00NULL" if v is None else str(v) for v in row.values()))
        canon.add(cells)
    return canon


def _sample(rows: list[Row]) -> list[list[str]]:
    out: list[list[str]] = []
    for row in rows[:_SAMPLE_ROWS]:
        out.append(["" if v is None else str(v) for v in row.values()])
    return out


class Grader:
    """Grades predicted SQL against gold via an injected ``execute`` callable.

    ``execute`` runs one SQL string and returns rows (list[dict]) or raises on any
    Athena error — the same contract as ``GlueAthenaSource.run_query``. A raised
    exception on GOLD ⇒ DISCARDED; on PREDICTED ⇒ FAIL.
    """

    def __init__(self, execute: Execute):
        self._execute = execute
        # gold SQL text -> (rows, discard_reason). discard_reason non-empty means
        # the gold itself failed; rows is then None.
        self._gold_cache: dict[str, tuple[list[Row] | None, str]] = {}
        # (gold_sql, predicted_sql) -> QuestionResult verdict (q_id re-stamped).
        self._pred_cache: dict[tuple[str, str], QuestionResult] = {}
        # Caches are shared across concurrent grade() calls (the round grades many
        # questions in parallel). A lock guards the dict reads/writes; the actual
        # Athena execution happens OUTSIDE the lock so queries still run concurrently
        # — the lock only serializes the fast cache bookkeeping.
        self._lock = threading.Lock()

    def _run_gold(self, gold_sql: str) -> tuple[list[Row] | None, str]:
        """Execute gold once (memoized, concurrency-safe). Returns (rows, discard_reason)."""
        with self._lock:
            if gold_sql in self._gold_cache:
                return self._gold_cache[gold_sql]
        try:
            rows = self._execute(gold_sql)
            verdict: tuple[list[Row] | None, str] = (rows, "")
        except Exception as e:  # noqa: BLE001 - gold that can't run is a DISCARD
            verdict = (None, f"{type(e).__name__}: {e}")
        with self._lock:
            # Another thread may have filled it while we executed; last write wins
            # (same gold → same verdict, so it's harmless).
            self._gold_cache[gold_sql] = verdict
        return verdict

    def grade(self, q_id: int, gold_sql: str, predicted_sql: str) -> QuestionResult:
        """Grade one question. Deterministic; caches gold + prediction verdicts."""
        predicted_sql = (predicted_sql or "").strip()

        gold_rows, discard_reason = self._run_gold(gold_sql)
        if discard_reason:
            # Gold is unrunnable → the question is unanswerable, regardless of the
            # prediction. DISCARDED (excluded from KPIs).
            return QuestionResult(
                q_id=q_id,
                outcome=Outcome.DISCARDED,
                predicted_sql=predicted_sql,
                discard_reason=discard_reason,
                reason="gold SQL does not execute against the data",
            )
        assert gold_rows is not None

        if not predicted_sql:
            # A stuck solver that produced nothing is a genuine miss (FAIL), not a
            # discard — the wiki failed to enable an answer.
            return QuestionResult(
                q_id=q_id,
                outcome=Outcome.FAIL,
                predicted_sql="",
                reason="empty predicted SQL",
                gold_rowcount=len(gold_rows),
            )

        cache_key = (gold_sql, predicted_sql)
        with self._lock:
            cached = self._pred_cache.get(cache_key)
        if cached is not None:
            # Re-stamp the q_id (same SQL can recur under a different question).
            return QuestionResult(
                q_id=q_id,
                outcome=cached.outcome,
                predicted_sql=predicted_sql,
                reason=cached.reason,
                pred_rowcount=cached.pred_rowcount,
                gold_rowcount=cached.gold_rowcount,
                pred_sample=cached.pred_sample,
            )

        try:
            pred_rows = self._execute(predicted_sql)
        except Exception as e:  # noqa: BLE001 - predicted that errors is a FAIL
            result = QuestionResult(
                q_id=q_id,
                outcome=Outcome.FAIL,
                predicted_sql=predicted_sql,
                reason=f"predicted SQL raised: {type(e).__name__}: {e}",
                gold_rowcount=len(gold_rows),
            )
            with self._lock:
                self._pred_cache[cache_key] = result
            return result

        ok = _canonical(pred_rows) == _canonical(gold_rows)
        result = QuestionResult(
            q_id=q_id,
            outcome=Outcome.PASS if ok else Outcome.FAIL,
            predicted_sql=predicted_sql,
            reason="result sets match" if ok else "result sets differ (set inequality)",
            pred_rowcount=len(pred_rows),
            gold_rowcount=len(gold_rows),
            pred_sample=_sample(pred_rows),
        )
        with self._lock:
            self._pred_cache[cache_key] = result
        return result
