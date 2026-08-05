"""Benchmark Studio — the pure run/report invariants shared across services.

A *benchmark run* is a standalone, human-triggered evaluation of a dataset's
wiki (no harvest, no lease, no improve-and-rescore loop — the RI loop this
replaces is retired). The user configures which checks to run, the solver and
judge models, the number of independent runs N, and the wiki version to
target; the outcome is a persisted *report*.

This module owns only the **pure invariants** — no AWS, no agent deps — so the
Control API (validator + report routes), the benchmark runtime (executor), and
the UI-facing docs agree on exactly one shape:

- the run-config field names, their bounds, and a validator that clamps/rejects;
- the ``REPORT#`` DynamoDB sort-key builders (the report *index* row — flat
  scalars only, structure lives in the S3 JSON);
- the S3 key builders for the report artifacts (off-mount, gold-carrying:
  ``benchmark/<domain>/<dataset>/reports/<report_id>/…``);
- the report statuses and the score arithmetic (raw + judge-adjusted).

There is deliberately NO stop target and NO ``target_met`` here — a report is a
measurement, not a gate; there is no loop to stop.
"""

from __future__ import annotations

import re
from typing import Any

from okf_core.benchmark_questions import ALL_CHECKS

# -- run config ----------------------------------------------------------------

#: Field names inside a run config (the POST body, the REPORT# row's config
#: summary, and the invocation payload block all use these spellings).
FIELD_CHECKS = "checks"
FIELD_RUNS = "runs"
FIELD_SOLVER_MODEL = "solver_model"
FIELD_SOLVER_EFFORT = "solver_effort"
FIELD_JUDGE_MODEL = "judge_model"
FIELD_JUDGE_EFFORT = "judge_effort"
#: The pinned wiki version's marker VersionId; "" = the current live bundle.
FIELD_VERSION_ID = "version_id"
FIELD_QUESTIONS_KEY = "questions_key"
#: The questions CSV's S3 VersionId, captured when the run started (the bundle
#: bucket is versioned) so a re-upload mid-run can't swap the graded set.
#: Absent/"" on older payloads → the runtime reads the latest object.
FIELD_QUESTIONS_VERSION_ID = "questions_version_id"
#: Behavior-check option: give the Behavior solver read-only `run_sql` against
#: the live dataset (a truer consumer simulation — real agents have SQL).
#: Default False = the classic wiki-only solver. NEVER applies to the SQL EX
#: check, whose solver must stay data-blind (live queries would let it iterate
#: empirically to the answer, measuring persistence instead of the wiki).
#: Reports carry the flag in their config — scores are not comparable across
#: different settings of it.
FIELD_BEHAVIOR_LIVE_SQL = "behavior_live_sql"

#: Independent-run bounds. N exists to turn a point sample into mean ± spread;
#: more than 5 runs buys spread precision nobody reads at 3-5× the Athena and
#: token cost. Uniform across checks (a per-check N would complicate the
#: stability story the report tells).
MIN_RUNS = 1
MAX_RUNS = 5
DEFAULT_RUNS = 3


class BenchmarkRunConfigError(ValueError):
    """A benchmark run config is malformed (→ 400 at the Control API)."""


def validate_checks(checks: Any) -> list[str]:
    """Normalize + validate the enabled-checks list (≥1, all known, deduped).

    Order is normalized to :data:`ALL_CHECKS` order so reports and progress
    events render checks in one stable order everywhere.
    """
    if not isinstance(checks, (list, tuple)) or not checks:
        raise BenchmarkRunConfigError(
            f"{FIELD_CHECKS} must be a non-empty list drawn from {list(ALL_CHECKS)}"
        )
    requested = {str(c).strip().lower() for c in checks}
    unknown = requested - set(ALL_CHECKS)
    if unknown:
        raise BenchmarkRunConfigError(
            f"unknown check(s) {sorted(unknown)}; offered: {list(ALL_CHECKS)}"
        )
    return [c for c in ALL_CHECKS if c in requested]


def coerce_runs(value: Any) -> int:
    """Coerce N to an int clamped to [MIN_RUNS, MAX_RUNS] (default when absent)."""
    if value is None or value == "":
        return DEFAULT_RUNS
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkRunConfigError(
            f"{FIELD_RUNS} must be an integer, got {value!r}"
        ) from exc
    return max(MIN_RUNS, min(MAX_RUNS, n))


# -- report identity -----------------------------------------------------------

#: Report ids are minted by the Control API (a time-prefixed token, sortable in
#: the DDB sk) and become an S3 path segment, a DDB sort key, and a URL path
#: segment — so the charset is locked down here, where every consumer imports.
_REPORT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


def is_valid_report_id(report_id: Any) -> bool:
    return isinstance(report_id, str) and bool(_REPORT_ID_RE.fullmatch(report_id))


def new_report_id(*, now_compact: str, token: str) -> str:
    """Build ``r<UTC compact timestamp>-<token>`` (caller supplies both parts).

    ``now_compact`` is ``YYYYMMDDTHHMMSS``-style (lowercased here); ``token`` a
    short random hex. Time-prefixed so REPORT# rows sort chronologically.
    """
    rid = f"r{now_compact}-{token}".lower()
    if not is_valid_report_id(rid):
        raise BenchmarkRunConfigError(f"generated report id {rid!r} is invalid")
    return rid


# -- DynamoDB index row --------------------------------------------------------

#: Sort-key prefix for a report index row on the ``HARVEST#<d>#<ds>`` partition.
REPORT_SK_PREFIX = "REPORT#"


def report_sk(report_id: str) -> str:
    return f"{REPORT_SK_PREFIX}{report_id}"


def report_sk_query_prefix() -> str:
    """The ``begins_with`` prefix that selects a dataset's report rows."""
    return REPORT_SK_PREFIX


#: Report lifecycle. ``queued`` is written by the Control API with the row;
#: the runtime moves it to ``running`` then ``complete``/``failed`` (loudly —
#: a run that can't fetch/parse its questions FAILS the report, it never
#: silently degrades).
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

#: The annotation-aggregation sub-lifecycle (a field on the report, not a row
#: status): idle until the user asks, then running/complete/failed.
AGG_IDLE = "idle"
AGG_RUNNING = "running"
AGG_COMPLETE = "complete"
AGG_FAILED = "failed"

# -- S3 artifacts ----------------------------------------------------------------


def reports_prefix(data_domain: str, dataset: str) -> str:
    """The off-mount S3 prefix for a dataset's benchmark reports.

    Under ``benchmark/`` (NOT the ``okf/`` mount prefix) — a report carries
    gold SQL/answers, so no LLM role may read it; it is served only via the
    Cognito-authed Control API.
    """
    return f"benchmark/{data_domain}/{dataset}/reports/"


def report_prefix(data_domain: str, dataset: str, report_id: str) -> str:
    return f"{reports_prefix(data_domain, dataset)}{report_id}/"


def report_key(data_domain: str, dataset: str, report_id: str) -> str:
    """The report document: config, per-question detail, judge output, telemetry."""
    return f"{report_prefix(data_domain, dataset, report_id)}report.json"


def traces_key(data_domain: str, dataset: str, report_id: str) -> str:
    """The companion solver-traces document (large; fetched lazily by the UI)."""
    return f"{report_prefix(data_domain, dataset, report_id)}traces.json"


# -- score arithmetic ------------------------------------------------------------


def score(passed: int, graded: int) -> float:
    """Raw accuracy = passed / graded, or 0.0 when nothing was graded.

    ``graded`` excludes DISCARDED questions (SQL EX gold that can't execute) —
    they are removed from both numerator and denominator upstream.
    """
    if graded <= 0:
        return 0.0
    return passed / graded


def adjusted_score(passed: int, overturned: int, graded: int) -> float:
    """Judge-adjusted accuracy = (passed + overturned) / graded.

    ``overturned`` counts only cases the judge POSITIVELY ruled ``pass`` (not
    the wiki's fault). An errored/unparseable review is a confirmed fail and
    never lands here — forgiveness requires positive evidence.
    """
    if graded <= 0:
        return 0.0
    return min(1.0, (passed + max(0, overturned)) / graded)


def mean_and_spread(values: list[float]) -> tuple[float, float]:
    """(mean, spread) across the N runs' per-run scores; spread = max - min.

    Spread (the range) rather than a standard deviation: with N ≤ 5 a stddev
    suggests statistical rigor the sample can't carry, while "scores ranged
    from .62 to .71" is exactly what a reader should take away.
    """
    if not values:
        return 0.0, 0.0
    return sum(values) / len(values), max(values) - min(values)
