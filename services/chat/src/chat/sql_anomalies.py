"""Deterministic anomaly checks on ``run_sql`` results → a ``<system-reminder>``.

The static steering tier for the chat agent's SQL path: every ``run_sql``
result is scanned by cheap, pure checks for the LOUD mechanical failure
signatures — the ones the wiki documents and the system prompt's
``<result_skepticism>`` block teaches the model to diagnose:

- exact duplicate rows           → join fan-out / skipped dedup recipe
- repeated all-9s / -1 values    → enum sentinel aggregated as real data
- one value dwarfing the rest    → fan-out, a sentinel, or genuinely skewed
- a mostly-NULL column           → conditionally-populated column
- negatives in a count-like col  → measure semantics worth re-reading
- zero rows                      → misqualification / horizon / filter traps

Detection runs on EVERY result (microseconds over ≤ max_rows in-memory rows);
INJECTION is budgeted by the caller (``sql.make_sql_tool``): at most
``MAX_INJECTIONS_PER_TURN`` reminders per turn, deduped by finding kind, so a
skewed-but-legitimate dataset can't nag on every query. Findings state
OBSERVATIONS, never verdicts, and each carries only its own targeted hint; the
constant closing clause explicitly licenses a verified surprising number so the
reminder can't be read as "re-run until it looks right" (the anti
result-shopping rule in ``<result_skepticism>``).

The detector is TOTAL on well-formed results: minimum-support floors make
0/1-row results and non-numeric columns no-ops rather than bad math (no
median-of-one), and the caller wraps the call fail-open — anomaly detection
must never be the reason a successful query fails.

Thresholds are env-tunable (``OKF_CHAT_SQL_ANOMALY_*``); the intended
calibration source is Benchmark Studio transcripts, not intuition.
"""

from __future__ import annotations

import os
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


#: Per-turn injection budget (all finding kinds combined, incl. zero_rows).
MAX_INJECTIONS_PER_TURN = _env_int("OKF_CHAT_SQL_ANOMALY_MAX_PER_TURN", 2)

# Disproportion needs real numeric support and an untruncated result (on a
# truncated sample the true max may not be present).
_MIN_NUMERIC = _env_int("OKF_CHAT_SQL_ANOMALY_MIN_NUMERIC", 5)
_RATIO = _env_float("OKF_CHAT_SQL_ANOMALY_RATIO", 1000.0)
_TOP_SHARE = _env_float("OKF_CHAT_SQL_ANOMALY_TOP_SHARE", 0.95)

# Sentinel: the same all-9s (≥3 digits) or -1 value repeated across rows.
_SENTINEL_REPEATS = _env_int("OKF_CHAT_SQL_ANOMALY_SENTINEL_REPEATS", 3)
_SENTINEL_RE = re.compile(r"^(?:-1|9{3,})(?:\.0+)?$")

# NULL share needs enough rows behind it to mean anything.
_NULL_SHARE = _env_float("OKF_CHAT_SQL_ANOMALY_NULL_SHARE", 0.8)
_MIN_ROWS_NULL = _env_int("OKF_CHAT_SQL_ANOMALY_MIN_ROWS_NULL", 10)

# Duplicate rows: single-column results legitimately repeat values (SELECT
# status FROM t), so require ≥2 columns and a non-trivial duplicate share.
_DUP_SHARE = _env_float("OKF_CHAT_SQL_ANOMALY_DUP_SHARE", 0.1)

# Only clearly count-like names — amount/total legitimately go negative
# (refunds, adjustments), so they are deliberately NOT matched.
_COUNT_LIKE_RE = re.compile(r"(?:^|_)(?:count|cnt|qty|quantity|num)(?:_|$)", re.I)

#: Most findings listed in one reminder (bounds the injected text).
_MAX_FINDINGS_LISTED = 4


@dataclass(frozen=True)
class Finding:
    """One fired check: a dedupe ``kind``, the observation, its targeted hint."""

    kind: str
    observation: str
    hint: str


_CLOSER = (
    "Verify the mechanics once; if they check out, these numbers are the "
    "answer — report them plainly."
)

_ZERO_ROWS_TEXT = (
    "This query returned zero rows. Common mechanical causes before concluding "
    '"no data": a misqualified table name ("db"."table"), a horizon mismatch '
    "between measures (see the guardrails' data horizons), an over-tight "
    "filter, or a value spelled differently than the data (check the enum and "
    "named_sets docs). Verify the query once; if the result is genuinely "
    "empty, that is the answer — report it plainly."
)

_HINT_DUP = (
    "check for join fan-out vs the documented cardinality (references/joins/) "
    "or a skipped dedup recipe (references/recipes/)"
)
_HINT_SENTINEL = (
    "check the column's enum doc — this value may mean unknown/not-applicable "
    "and need excluding"
)
_HINT_DISPROPORTION = (
    "possible join fan-out, a sentinel value inflating the aggregate, or "
    "genuinely skewed data"
)
_HINT_NULL = (
    "likely a conditionally populated column — check the schema row's "
    "population rule"
)
_HINT_NEGATIVE = (
    "check the measure's definition — adjustments/returns may be legitimate"
)


def _numbers(values: list[str]) -> list[float]:
    nums: list[float] = []
    for v in values:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    return nums


def detect(result: dict[str, Any]) -> list[Finding]:
    """All fired checks for one ``{columns, rows, row_count, truncated}`` result.

    Pure and total: a 0-row result yields exactly the ``zero_rows`` finding, a
    1-row result yields nothing (every check has a minimum-support floor), and
    unparseable values simply don't participate. Cell values are the engines'
    ``str | None`` shape.
    """
    rows: list[dict[str, Any]] = result.get("rows") or []
    columns: list[str] = result.get("columns") or []
    truncated = bool(result.get("truncated"))

    if not rows:
        return [Finding("zero_rows", "the query returned zero rows", "")]

    findings: list[Finding] = []
    n = len(rows)

    # Duplicate full rows — the fan-out signature.
    if n >= 2 and len(columns) >= 2:
        keys = Counter(tuple(str(r.get(c)) for c in columns) for r in rows)
        dup_rows = n - len(keys)
        if dup_rows >= 2 and dup_rows / n >= _DUP_SHARE:
            findings.append(
                Finding(
                    "duplicate_rows",
                    f"{dup_rows} of {n} rows are exact duplicates",
                    _HINT_DUP,
                )
            )

    for col in columns:
        values = [r.get(col) for r in rows]
        nulls = sum(1 for v in values if v is None)
        present = [v for v in values if v is not None]

        if n >= _MIN_ROWS_NULL and nulls / n >= _NULL_SHARE:
            findings.append(
                Finding(
                    "null_heavy",
                    f'column "{col}" is {round(100 * nulls / n)}% NULL',
                    _HINT_NULL,
                )
            )

        sentinels = Counter(
            v.strip()
            for v in present
            if isinstance(v, str) and _SENTINEL_RE.match(v.strip())
        )
        for value, count in sentinels.most_common(1):
            if count >= _SENTINEL_REPEATS:
                findings.append(
                    Finding(
                        "sentinel",
                        f'column "{col}": {count} rows carry the value '
                        f"{value}, a common sentinel pattern",
                        _HINT_SENTINEL,
                    )
                )

        nums = _numbers(present)
        if not truncated and len(nums) >= _MIN_NUMERIC:
            top = max(nums)
            median = statistics.median(nums)
            total = sum(nums)
            ratio_hit = median > 0 and top / median >= _RATIO
            share_hit = (
                all(x >= 0 for x in nums) and total > 0 and top / total >= _TOP_SHARE
            )
            if ratio_hit or share_hit:
                observation = (
                    f'column "{col}": the top value is ~{round(top / median):,}× '
                    "the median"
                    if ratio_hit
                    else f'column "{col}": one value is {round(100 * top / total)}% '
                    "of the column's total"
                )
                findings.append(Finding("disproportion", observation, _HINT_DISPROPORTION))

        if _COUNT_LIKE_RE.search(col):
            negatives = sum(1 for x in nums if x < 0)
            if negatives:
                findings.append(
                    Finding(
                        "negative_measure",
                        f'column "{col}" (count-like) has {negatives} negative '
                        "value(s)",
                        _HINT_NEGATIVE,
                    )
                )

    return findings


def compose(findings: list[Finding]) -> str:
    """One ``<system-reminder>`` block from the fired findings.

    Zero-rows gets its own template (the checklist differs); everything else
    is the bundled observation—hint list under the constant closer. Only the
    findings that FIRED appear — never a recitation of checks that passed.
    """
    if any(f.kind == "zero_rows" for f in findings):
        return f"<system-reminder>{_ZERO_ROWS_TEXT}</system-reminder>"
    parts = [f"{f.observation} — {f.hint}" for f in findings[:_MAX_FINDINGS_LISTED]]
    body = (
        "Anomaly check on this result (within the returned sample): "
        + "; ".join(parts)
        + f". {_CLOSER}"
    )
    return f"<system-reminder>{body}</system-reminder>"
