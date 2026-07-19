"""Recursive improvement — benchmark-driven, in-run harvest self-improvement.

An optional harvest capability: after the agent authors the bundle it benchmarks
it against a user-supplied ``question,gold_sql`` set, then revises and
re-benchmarks in a loop until a target accuracy is met or an iteration cap is
reached. The whole loop runs *inside one harvest run* (one AgentCore session, one
lease, one working tree edited in place). See ``docs/CONVENTIONS.md`` for the
payload + DynamoDB contract and ``docs/BENCHMARK_GUIDE.md`` for how to use it.

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
#
# The stop TARGET is intentionally NOT configurable. Recursive improvement exists
# to improve the WIKI, not to benchmark agents — exposing an adjustable target (and
# a choice of which KPI to gate on) invited that confusion. The loop always stops
# on the same fixed bar: adjudicated (judge) accuracy >= JUDGE_TARGET. So the config
# carries only enablement, the question-set key, and the iteration budget.
FIELD_ENABLED = "enabled"
FIELD_QUESTIONS_KEY = "questions_key"
FIELD_MAX_ITERATIONS = "max_iterations"

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

#: The FIXED stop target: the loop is "done" once judge (adjudicated) accuracy
#: reaches this. Not configurable — one bar for every dataset (see the field-block
#: note above). Judge accuracy = (passed + positively-forgiven FAILs) / graded.
JUDGE_TARGET = 0.9


class RecursiveImprovementConfigError(ValueError):
    """A recursive-improvement config is malformed.

    Surfaced to the Control API caller as a 400. Raised only for values that
    cannot be sensibly coerced (missing ``questions_key`` when enabled, a
    non-integer ``max_iterations``). Out-of-range ``max_iterations`` that CAN be
    coerced (too high / too low) is clamped by :func:`validate`, not rejected —
    see its docstring.
    """


def is_enabled(config: dict[str, Any] | None) -> bool:
    """True iff a config block is present AND its ``enabled`` flag is truthy.

    The presence of a validated block in the invocation payload is the runtime's
    enable signal, but the saved ``DATASET#`` settings also carry an explicit
    ``enabled`` flag so an operator can turn the feature off without deleting the
    saved questions_key. Both must hold.
    """
    return bool(config) and bool(config.get(FIELD_ENABLED, False))


def validate(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate + normalize a recursive-improvement config; return the clean block.

    Returns ``None`` when the feature is not enabled (absent block, or
    ``enabled`` falsy) — the caller then omits the block from the payload so the
    run is a normal harvest. Otherwise returns a fully-populated, bounded config:

    - ``questions_key`` is REQUIRED when enabled (raises if missing/blank).
    - ``max_iterations`` is coerced to an int and **clamped** to
      ``[MIN_ITERATIONS, MAX_ITERATIONS]`` (a request for 10 becomes 5, for 0
      becomes 2) — a benign over-ask shouldn't 400.

    The stop target is fixed (:data:`JUDGE_TARGET`), so there is nothing else to
    validate — no thresholds, no gate selection. Any such keys in the input are
    ignored, not persisted.

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

    return {
        FIELD_ENABLED: True,
        FIELD_QUESTIONS_KEY: str(questions_key).strip(),
        FIELD_MAX_ITERATIONS: max_iter,
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


def target_met(*, ex: float, judge: float) -> bool:
    """True iff the round meets the FIXED stop target: judge accuracy >= JUDGE_TARGET.

    The target is not configurable (see the field-block note) — one bar for every
    dataset. Judge (adjudicated) accuracy is the gate because the feature's purpose
    is wiki QUALITY: a FAIL the adjudicator confirms is noisy/ambiguous gold is not
    a wiki defect, so forgiving it is correct.

    **EX > 0 is an implicit floor regardless of judge.** A round with EX == 0 can
    NEVER be "target met" — a wiki that answers *nothing* correctly is never done,
    even at judge 1.0. This blocks the false success where a broken/forgiving
    adjudicator pushes judge to 1.0 at EX 0.0. Judge only ever *forgives* on top of
    real passes, so requiring at least one pass is a minimal, always-correct floor.
    """
    if ex <= 0.0:
        return False
    return judge >= JUDGE_TARGET
