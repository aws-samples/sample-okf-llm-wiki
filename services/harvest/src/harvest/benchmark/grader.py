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
``run_query``, ``positional=True`` in production — see below) so it is
unit-testable with a fake and carries no boto3 import.
Two caches make the loop affordable across rounds:

* **gold cache** — gold SQL is invariant across rounds (the wiki changes, not the
  answer key), so each gold query executes at most once per run; its rows (or its
  DISCARDED verdict) are memoized by SQL text.
* **prediction cache** — an identical predicted SQL (a question the agent didn't
  affect this round) reuses its prior comparison verdict.

Errors from ``execute`` are CLASSIFIED, not treated alike: a semantic failure
(bad column, syntax) is deterministic — memoized as DISCARDED (gold) / FAIL
(predicted). A TRANSIENT service fault (throttle, 5xx, timeout) is retried with
backoff, and if it still fails the outcome keeps its shape but carries a
"grading unavailable (...)" reason and is NEVER memoized — one blip must not
stick for all N runs or masquerade as "gold doesn't execute". A result that
outgrows the row cap (``ResultCapExceeded``, matched by name) is deterministic
and classified as its own reason.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable

try:  # Only for classifying network faults — the grader itself stays boto3-free
    # and must remain importable without botocore (its zero-import core).
    import botocore.exceptions as _botocore_exceptions
except ImportError:  # pragma: no cover - botocore ships with the runtime
    _botocore_exceptions = None

# Rows arrive either as POSITIONAL sequences — the production shape
# (``run_query(..., positional=True)``), required because header-keyed dicts
# collapse duplicate SELECT labels like ``SELECT r.name, c.name`` into one cell
# (a false PASS/FAIL) — or as header-keyed dicts (insertion order = column
# order; the legacy/test shape). We compare BIRD-style: each row is a
# POSITIONAL tuple of its cells (sorting within a row would let transposed
# values — gold ('Hamilton', 'Mercedes') vs predicted ('Mercedes','Hamilton') —
# pass falsely), and the set of those tuples ignores row order. Numeric-looking
# cells normalize to Decimal (Athena stringifies everything; BIRD compares
# native values where 3 == 3.0).
Row = Any
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


def _cells(row: Row) -> Any:
    """A row's cells in column order — dict rows by insertion, sequences as-is."""
    return row.values() if isinstance(row, dict) else row


def _canonical(rows: list[Row]) -> set[tuple[Any, ...]]:
    """Row-order-insensitive key for a result set (BIRD semantics).

    Each row → the POSITIONAL tuple of its canonical cell values (column order
    is meaningful — see :func:`_cell` and the module docstring), and the set of
    those tuples ignores row order. Note: as a set this drops true duplicate
    rows — matching BIRD's ``set(pred) == set(gold)`` exactly.
    """
    return {tuple(_cell(v) for v in _cells(row)) for row in rows}


def _sample(rows: list[Row]) -> list[list[str]]:
    out: list[list[str]] = []
    for row in rows[:_SAMPLE_ROWS]:
        out.append(["" if v is None else str(v) for v in _cells(row)])
    return out


# -- error classification ------------------------------------------------------

KIND_SEMANTIC = "semantic"
KIND_TRANSIENT = "transient"
KIND_CAP = "cap"

# Service faults that say nothing about the SQL itself. Matched EXACTLY
# against the botocore error code; matched with word boundaries against the
# rendered message only when there is no code at all (an Athena FAILED state
# folds the underlying code into its StateChangeReason text — but a coded,
# deterministic error whose message merely QUOTES one of these names must
# stay semantic).
_TRANSIENT_ERROR_NAMES = (
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "RequestTimeout",
    "RequestTimeoutException",
    "InternalServerException",
    "InternalFailure",
    "InternalError",
    "SlowDown",
    "ServiceUnavailable",
    "ServiceUnavailableException",
)

# Network faults are transient by nature. botocore's (ReadTimeoutError,
# ConnectTimeoutError, EndpointConnectionError, ConnectionClosedError, …) all
# derive from these two botocore bases — they are NOT builtin TimeoutError/
# ConnectionError subclasses, and ReadTimeoutError/ConnectionClosedError carry
# ``response=None``, so they must be matched by isinstance, never through the
# ClientError response shape.
_NETWORK_ERROR_TYPES: tuple[type[Exception], ...] = (
    (TimeoutError, ConnectionError)
    if _botocore_exceptions is None
    else (
        TimeoutError,
        ConnectionError,
        _botocore_exceptions.ConnectionError,
        _botocore_exceptions.HTTPClientError,
    )
)

_TRANSIENT_ATTEMPTS = 3
_TRANSIENT_BACKOFF_S = 1.0


def _error_kind(e: Exception) -> str:
    """Classify an ``execute`` error: KIND_TRANSIENT retries and never memoizes;
    KIND_CAP (``ResultCapExceeded``, matched by name so the grader stays free of
    source-layer imports) and KIND_SEMANTIC are deterministic and memoized.

    Must never raise: this runs inside the retry/memoization handlers, where a
    classifier crash escapes as the grade itself and sticks a one-off blip in
    the caches — any internal error defaults to SEMANTIC.
    """
    try:
        if type(e).__name__ == "ResultCapExceeded":
            return KIND_CAP
        # botocore's ReadTimeoutError/ConnectionClosedError carry
        # ``response=None`` — a bare getattr default is not enough.
        resp = getattr(e, "response", None) or {}
        code = str((resp.get("Error") or {}).get("Code") or "")
        if code in _TRANSIENT_ERROR_NAMES:
            return KIND_TRANSIENT
        if isinstance(e, _NETWORK_ERROR_TYPES):
            return KIND_TRANSIENT
        if not code:
            # Message matching only when the service gave NO code — a coded
            # error is authoritative even if its message quotes a transient
            # name; word boundaries keep lookalike identifiers semantic.
            text = f"{type(e).__name__}: {e}"
            if any(
                re.search(rf"\b{re.escape(name)}\b", text)
                for name in _TRANSIENT_ERROR_NAMES
            ):
                return KIND_TRANSIENT
        return KIND_SEMANTIC
    except Exception:  # noqa: BLE001 - see docstring: the classifier never raises
        return KIND_SEMANTIC


def _error_label(e: Exception) -> str:
    """The error's headline name — the botocore code when present (a raw
    ``ClientError``'s type name says nothing), else the exception type.
    ``response`` may be present-but-None (botocore network faults)."""
    resp = getattr(e, "response", None) or {}
    code = str((resp.get("Error") or {}).get("Code") or "")
    return code or type(e).__name__


class Grader:
    """Grades predicted SQL against gold via an injected ``execute`` callable.

    ``execute`` runs one SQL string and returns rows — POSITIONAL sequences in
    production (``run_query(..., positional=True)``; header-keyed dicts also
    accepted, see the module docstring) — or raises on any Athena error. A
    raised exception on GOLD ⇒ DISCARDED; on PREDICTED ⇒ FAIL — except a
    TRANSIENT service fault, which is retried and, if it persists, yields a
    non-memoized "grading unavailable" verdict (see :func:`_error_kind`).
    """

    def __init__(self, execute: Execute, *, sleep: Callable[[float], None] = time.sleep):
        self._execute = execute
        self._sleep = sleep  # injectable so tests don't pay real backoff
        # gold SQL text -> (rows, discard_reason, discard_headline). A non-empty
        # discard_reason means the gold itself failed; rows is then None and the
        # headline is the human-facing reason line (classified by error kind).
        self._gold_cache: dict[str, tuple[list[Row] | None, str, str]] = {}
        # (gold_sql, predicted_sql) -> QuestionResult verdict (q_id re-stamped).
        self._pred_cache: dict[tuple[str, str], QuestionResult] = {}
        # Caches are shared across concurrent grade() calls (the round grades many
        # questions in parallel). A lock guards the dict reads/writes; the actual
        # Athena execution happens OUTSIDE the lock so queries still run concurrently
        # — the lock only serializes the fast cache bookkeeping.
        self._lock = threading.Lock()

    def _execute_with_retry(self, sql: str) -> list[Row]:
        """Run one query, retrying TRANSIENT faults with linear backoff.

        Semantic and cap errors raise immediately (they are deterministic); a
        transient fault that survives every attempt raises its last error for
        the caller to classify.
        """
        for attempt in range(1, _TRANSIENT_ATTEMPTS + 1):
            try:
                return self._execute(sql)
            except Exception as e:  # noqa: BLE001 - classified below
                if _error_kind(e) != KIND_TRANSIENT or attempt >= _TRANSIENT_ATTEMPTS:
                    raise
                self._sleep(_TRANSIENT_BACKOFF_S * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _run_gold(self, gold_sql: str) -> tuple[list[Row] | None, str, str]:
        """Execute gold once (memoized, concurrency-safe).

        Returns ``(rows, discard_reason, discard_headline)``. Only DETERMINISTIC
        verdicts (success, semantic failure, cap) are memoized — a transient
        fault yields a one-off "grading unavailable" verdict and the next run
        re-executes the gold.
        """
        with self._lock:
            if gold_sql in self._gold_cache:
                return self._gold_cache[gold_sql]
        try:
            rows = self._execute_with_retry(gold_sql)
            verdict: tuple[list[Row] | None, str, str] = (rows, "", "")
        except Exception as e:  # noqa: BLE001 - gold that can't run is a DISCARD
            kind = _error_kind(e)
            if kind == KIND_TRANSIENT:
                # NOT memoized, NOT conflated with "gold doesn't execute".
                return (
                    None,
                    f"grading unavailable ({_error_label(e)}: {e})",
                    f"gold grading unavailable ({_error_label(e)})",
                )
            headline = (
                f"gold {e}"  # "gold result exceeds <cap> rows"
                if kind == KIND_CAP
                else "gold SQL does not execute against the data"
            )
            verdict = (None, f"{type(e).__name__}: {e}", headline)
        with self._lock:
            # Another thread may have filled it while we executed; last write wins
            # (same gold → same verdict, so it's harmless).
            self._gold_cache[gold_sql] = verdict
        return verdict

    def grade(self, q_id: int, gold_sql: str, predicted_sql: str) -> QuestionResult:
        """Grade one question. Deterministic; caches gold + prediction verdicts."""
        predicted_sql = (predicted_sql or "").strip()

        gold_rows, discard_reason, discard_headline = self._run_gold(gold_sql)
        if discard_reason:
            # Gold is unrunnable → the question is unanswerable, regardless of the
            # prediction. DISCARDED (excluded from KPIs). The headline separates
            # "gold doesn't execute" from a transient/cap grading failure.
            return QuestionResult(
                q_id=q_id,
                outcome=Outcome.DISCARDED,
                predicted_sql=predicted_sql,
                discard_reason=discard_reason,
                reason=discard_headline,
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
            pred_rows = self._execute_with_retry(predicted_sql)
        except Exception as e:  # noqa: BLE001 - predicted that errors is a FAIL
            kind = _error_kind(e)
            if kind == KIND_CAP:
                reason = f"predicted {e}"  # "predicted result exceeds <cap> rows"
            elif kind == KIND_TRANSIENT:
                reason = f"grading unavailable ({_error_label(e)}: {e})"
            else:
                reason = f"predicted SQL raised: {type(e).__name__}: {e}"
            result = QuestionResult(
                q_id=q_id,
                outcome=Outcome.FAIL,
                predicted_sql=predicted_sql,
                reason=reason,
                gold_rowcount=len(gold_rows),
            )
            if kind != KIND_TRANSIENT:  # a blip must not stick for all N runs
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
