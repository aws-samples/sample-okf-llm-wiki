"""Deterministic Athena-backed EX grader — the trustworthy, zero-LLM core.

Ports the BIRD-EX comparator (``~/.claude/skills/okf-sql-benchmark/scripts/
ex_compare.py``) from SQLite to Athena: execute the gold SQL and the predicted
SQL, compare their result sets as **unordered sets of rows**
(``set(pred) == set(gold)``, rows positional within themselves — BIRD ignores
row ORDER, never column order). Athena returns every cell as a string, so
numeric-looking cells are normalized to ``Decimal`` before comparing — that
recovers BIRD's native-value semantics where ``3 == 3.0`` (a ``COUNT(*)`` gold
vs a ``SUM(...)`` prediction would otherwise be a false FAIL on formatting).
No LLM, no agent tool layer — the gold SQL lives in
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

import re
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable

# Rows come back from the source as list[dict] (header-keyed, insertion order =
# column order). We compare BIRD-style: each row is a POSITIONAL tuple of its
# cells (sorting within a row would let transposed values — gold ('Hamilton',
# 'Mercedes') vs predicted ('Mercedes','Hamilton') — pass falsely), and the set
# of those tuples ignores row order. Numeric-looking cells normalize to Decimal
# (Athena stringifies everything; BIRD compares native values where 3 == 3.0).
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
    # The solver's trace (harvest.benchmark.trace.SolverTrace), attached by the round
    # AFTER grading — the grader neither produces nor reads it. Typed loosely so the
    # deterministic grader keeps its zero-import core. None on an untraced solve.
    trace: Any = None


_SAMPLE_ROWS = 5

# A cell that is unambiguously a number AS ATHENA RENDERS ONE (plain or
# scientific, no leading zeros beyond a bare '0'). Deliberately excludes
# 'NaN'/'Infinity' (Decimal accepts 'NaN', and NaN != NaN would make identical
# result sets compare unequal forever) and identifier lookalikes like '007'
# (a varchar code, not the number 7 — Athena never renders a numeric cell
# with leading zeros).
_NUMERIC_RE = re.compile(r"^[+-]?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?$")


def _cell(value: Any) -> Any:
    """One cell's canonical comparison value.

    ``None`` (SQL NULL) maps to a sentinel distinct from the empty string so a
    genuine NULL mismatch still fails. Numeric-looking strings become
    ``Decimal`` — Athena returns every cell as text, and BIRD compares native
    values where ``3 == 3.0`` and ``2.50 == 2.5`` (equal Decimals hash equal,
    so set comparison stays sound). Everything else compares as its string.
    """
    if value is None:
        return "\x00NULL"
    s = str(value)
    if _NUMERIC_RE.match(s.strip()):
        try:
            return Decimal(s.strip())
        except InvalidOperation:  # pragma: no cover - regex should preclude this
            return s
    return s


def _canonical(rows: list[Row]) -> set[tuple[Any, ...]]:
    """Row-order-insensitive key for a result set (BIRD semantics).

    Each row → the POSITIONAL tuple of its canonical cell values (column order
    is meaningful — see :func:`_cell` and the module docstring), and the set of
    those tuples ignores row order. Note: as a set this drops true duplicate
    rows — matching BIRD's ``set(pred) == set(gold)`` exactly.
    """
    return {tuple(_cell(v) for v in row.values()) for row in rows}


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
