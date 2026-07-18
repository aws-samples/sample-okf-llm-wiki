"""Recursive improvement — benchmark-driven, in-run harvest self-improvement.

An optional harvest capability: after the agent authors the bundle it benchmarks
it against a user-supplied ``question,gold_sql`` set, then revises and
re-benchmarks in a loop until a target accuracy is met or an iteration cap is
reached. The whole loop runs *inside one harvest run* (one AgentCore session, one
lease, one working tree edited in place). See ``docs/RECURSIVE_IMPROVEMENT.md`` for
the full design and ``docs/CONVENTIONS.md`` for the payload + DynamoDB contract.

This module owns only the **pure invariants** — no AWS, no agent deps — so the
Control API (validator + settings persistence) and the harvest runtime (config
consumption) agree on exactly one shape:

- the ``DATASET#`` row attribute + the invocation-payload key (``recursive_improvement``);
- the config field names, their bounds, and a validator that clamps/rejects;
- the two hard caps (``MAX_ITERATIONS`` = 5, ``MAX_QUESTIONS`` = 100);
- the ``BENCH#`` KPI sort-key builder and the EX arithmetic (discards excluded).

The DynamoDB reads/writes live in the Control API + harvest ``status`` module; the
benchmark loop itself lives in ``harvest.benchmark``. Both import from here.
"""

from __future__ import annotations

from typing import Any

# -- registry attribute + payload key ----------------------------------------

#: The single key under which recursive-improvement config lives, both as an
#: attribute on the ``DATASET#`` mapping row (the dataset's saved settings) and as
#: the invocation-payload block. One spelling, shared end to end.
CONFIG_KEY = "recursive_improvement"

# Config field names (inside the CONFIG_KEY map). Kept here so the Control API
# writer/reader, the payload builder, and the runtime consumer agree.
FIELD_ENABLED = "enabled"
FIELD_QUESTIONS_KEY = "questions_key"
FIELD_MAX_ITERATIONS = "max_iterations"
FIELD_EX_THRESHOLD = "ex_threshold"
FIELD_JUDGE_THRESHOLD = "judge_threshold"
FIELD_GATE_KPIS = "gate_kpis"

# -- hard bounds -------------------------------------------------------------

#: Iteration cap. The loop runs at most this many benchmark->revise rounds after
#: the initial authoring pass. A request may ask for fewer (min 2) but never more;
#: values above are CLAMPED (not rejected), values below the floor are raised.
MIN_ITERATIONS = 2
MAX_ITERATIONS = 5

#: Question cap. At most this many questions are benchmarked, regardless of how
#: many the CSV holds (the harvest tool takes the first ``MAX_QUESTIONS`` in CSV
#: order). Enforced again at the tool boundary; named here so both agree.
MAX_QUESTIONS = 100

#: The KPIs that can gate the loop's stop decision. ``gate_kpis`` is a subset.
GATE_KPI_EX = "ex"
GATE_KPI_JUDGE = "judge"
VALID_GATE_KPIS: tuple[str, ...] = (GATE_KPI_EX, GATE_KPI_JUDGE)

#: Default stop thresholds + gate when a field is omitted from an otherwise-valid
#: config. Conservative: gate on EX alone unless the caller opts into judge too.
DEFAULT_EX_THRESHOLD = 0.8
DEFAULT_JUDGE_THRESHOLD = 0.9
DEFAULT_GATE_KPIS: tuple[str, ...] = (GATE_KPI_EX,)


class RecursiveImprovementConfigError(ValueError):
    """A recursive-improvement config is malformed or out of range.

    Surfaced to the Control API caller as a 400. Raised only for values that
    cannot be sensibly coerced (missing ``questions_key`` when enabled, a
    non-numeric threshold, an unknown ``gate_kpis`` entry). Out-of-range numeric
    values that CAN be coerced (``max_iterations`` too high, a threshold slightly
    over 1.0) are clamped by :func:`validate`, not rejected — see its docstring.
    """


def is_enabled(config: dict[str, Any] | None) -> bool:
    """True iff a config block is present AND its ``enabled`` flag is truthy.

    The presence of a validated block in the invocation payload is the runtime's
    enable signal, but the saved ``DATASET#`` settings also carry an explicit
    ``enabled`` flag so an operator can turn the feature off without deleting the
    saved thresholds/questions_key. Both must hold.
    """
    return bool(config) and bool(config.get(FIELD_ENABLED, False))


def _coerce_threshold(value: Any, *, field: str, default: float) -> float:
    """Coerce a threshold to a float in [0, 1]; clamp out-of-range, default None."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise RecursiveImprovementConfigError(
            f"{field} must be a number in [0, 1], got {value!r}"
        ) from exc
    # Clamp rather than reject: a caller asking for 1.2 clearly means "as high as
    # possible" and a negative clearly means "no floor" — coerce to the valid edge.
    return max(0.0, min(1.0, f))


def validate(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate + normalize a recursive-improvement config; return the clean block.

    Returns ``None`` when the feature is not enabled (absent block, or
    ``enabled`` falsy) — the caller then omits the block from the payload so the
    run is a normal harvest. Otherwise returns a fully-populated, bounded config:

    - ``questions_key`` is REQUIRED when enabled (raises if missing/blank).
    - ``max_iterations`` is coerced to an int and **clamped** to
      ``[MIN_ITERATIONS, MAX_ITERATIONS]`` (a request for 10 becomes 5, for 0
      becomes 2) — a benign over-ask shouldn't 400.
    - ``ex_threshold`` / ``judge_threshold`` are coerced to floats and clamped to
      ``[0, 1]``; omitted → the module defaults.
    - ``gate_kpis`` must be a non-empty subset of ``VALID_GATE_KPIS`` (raises on an
      unknown entry); omitted → ``DEFAULT_GATE_KPIS``.

    Raises :class:`RecursiveImprovementConfigError` (→ 400) only for values that
    can't be coerced. This is the Control API trust boundary; the runtime trusts
    the validated block (consistent with how ``model``/``effort`` are handled).
    """
    if not is_enabled(config):
        return None
    assert config is not None  # is_enabled guarantees this

    questions_key = config.get(FIELD_QUESTIONS_KEY)
    if not questions_key or not str(questions_key).strip():
        raise RecursiveImprovementConfigError(
            f"{CONFIG_KEY} is enabled but {FIELD_QUESTIONS_KEY} is missing"
        )

    max_iter = _coerce_iterations(config.get(FIELD_MAX_ITERATIONS))
    ex_threshold = _coerce_threshold(
        config.get(FIELD_EX_THRESHOLD), field=FIELD_EX_THRESHOLD, default=DEFAULT_EX_THRESHOLD
    )
    judge_threshold = _coerce_threshold(
        config.get(FIELD_JUDGE_THRESHOLD),
        field=FIELD_JUDGE_THRESHOLD,
        default=DEFAULT_JUDGE_THRESHOLD,
    )
    gate_kpis = _validate_gate_kpis(config.get(FIELD_GATE_KPIS))

    return {
        FIELD_ENABLED: True,
        FIELD_QUESTIONS_KEY: str(questions_key).strip(),
        FIELD_MAX_ITERATIONS: max_iter,
        FIELD_EX_THRESHOLD: ex_threshold,
        FIELD_JUDGE_THRESHOLD: judge_threshold,
        FIELD_GATE_KPIS: list(gate_kpis),
    }


def _coerce_iterations(value: Any) -> int:
    """Coerce max_iterations to an int clamped to [MIN_ITERATIONS, MAX_ITERATIONS]."""
    if value is None:
        return MAX_ITERATIONS
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise RecursiveImprovementConfigError(
            f"{FIELD_MAX_ITERATIONS} must be an integer, got {value!r}"
        ) from exc
    return max(MIN_ITERATIONS, min(MAX_ITERATIONS, n))


def _validate_gate_kpis(value: Any) -> tuple[str, ...]:
    """Validate gate_kpis is a non-empty subset of VALID_GATE_KPIS; default if None."""
    if value is None:
        return DEFAULT_GATE_KPIS
    if not isinstance(value, (list, tuple)) or not value:
        raise RecursiveImprovementConfigError(
            f"{FIELD_GATE_KPIS} must be a non-empty list, subset of {list(VALID_GATE_KPIS)}"
        )
    unknown = [k for k in value if k not in VALID_GATE_KPIS]
    if unknown:
        raise RecursiveImprovementConfigError(
            f"{FIELD_GATE_KPIS} has unknown entries {unknown}; "
            f"allowed: {list(VALID_GATE_KPIS)}"
        )
    # De-dupe while preserving order (a caller passing ["ex","ex"] is harmless).
    seen: dict[str, None] = {}
    for k in value:
        seen.setdefault(k, None)
    return tuple(seen)


# -- benchmark KPI row shape (BENCH# sort key + EX arithmetic) ----------------

#: Sort-key prefix for a benchmark KPI row on the HARVEST# partition.
BENCH_SK_PREFIX = "BENCH#"

#: The literal used in place of an iteration number for the terminal summary row.
FINAL_ITERATION = "final"


def bench_sk(runtime_session_id: str, iteration: int | str) -> str:
    """Build the ``BENCH#<runtime_session_id>#<iteration>`` sort key.

    ``iteration`` is the 0-based round number, or :data:`FINAL_ITERATION` for the
    terminal summary row. Scoping by ``runtime_session_id`` means a reader queries
    exactly one run's rows (``begins_with(sk, "BENCH#<session>")``), so a prior
    run's rows never mingle with the current one's.
    """
    return f"{BENCH_SK_PREFIX}{runtime_session_id}#{iteration}"


def bench_sk_query_prefix(runtime_session_id: str) -> str:
    """The ``begins_with`` prefix that selects all KPI rows for one run."""
    return f"{BENCH_SK_PREFIX}{runtime_session_id}#"


def ex_score(passed: int, graded: int) -> float:
    """Exact-match accuracy = passed / graded, or 0.0 when nothing was graded.

    ``graded`` is ``passed + failed`` — DISCARDED questions (gold that can't bind
    to the schema) are excluded from BOTH numerator and denominator upstream, so
    they never reach here. An all-discarded round grades 0 questions and scores
    0.0 (there is nothing to be right about).
    """
    if graded <= 0:
        return 0.0
    return passed / graded


def thresholds_met(
    config: dict[str, Any],
    *,
    ex: float,
    judge: float,
) -> bool:
    """True iff every KPI named in ``gate_kpis`` clears its threshold.

    ``config`` is a validated block (from :func:`validate`). Only the gated KPIs
    must clear — an ``ex``-only gate ignores the judge score entirely. An empty
    gate set can never be satisfied (there's nothing to clear), so the loop would
    run to ``max_iterations``; :func:`validate` guarantees a non-empty set.
    """
    gate = config.get(FIELD_GATE_KPIS) or []
    if not gate:
        return False
    if GATE_KPI_EX in gate and ex < config.get(FIELD_EX_THRESHOLD, DEFAULT_EX_THRESHOLD):
        return False
    if GATE_KPI_JUDGE in gate and judge < config.get(
        FIELD_JUDGE_THRESHOLD, DEFAULT_JUDGE_THRESHOLD
    ):
        return False
    return True
