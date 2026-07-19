"""Benchmark round orchestration — solver fan-out → grade → adjudicate.

This is the black box behind the supervisor's single ``run_benchmark`` tool. It is
written against **injected** callables so the whole round is offline-testable with
fakes (no Bedrock, no Athena):

* ``solve(question: str) -> str`` (async) — a bundle-blind solver returns candidate
  SQL for one question. The real one is a ReAct agent rooted at the bundle
  snapshot (see :mod:`harvest.benchmark.solver`); tests pass a fake.
* ``Grader`` — the deterministic Athena EX comparator (:mod:`.grader`).
* ``adjudicate(cases) -> AdjudicationResult`` (async) — classifies FAILs as genuine
  vs noisy-gold and consolidates genuine errors into anonymous ``improvements``
  themes. The real one calls the shared model (see :mod:`.adjudicator`).

The round returns a **gold-free, question-free** dict — the aggregated-feedback
boundary — carrying only counts, KPI scores, and the anonymous ``improvements``.
Gold SQL, gold rows, question text, and per-``q_id`` failures never appear in it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from harvest.benchmark.grader import Grader, Outcome, QuestionResult
from harvest.benchmark.questions import BenchmarkQuestion
from okf_core import recursive_improvement as ri

# Grader (Athena) concurrency — separate from the solver-LLM semaphore, sized
# under the Athena workgroup's concurrent-DML limit. See docs/CONVENTIONS.md.
_DEFAULT_ATHENA_CONCURRENCY = 15


def _athena_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("OKF_BENCHMARK_ATHENA_CONCURRENCY", "")))
    except (TypeError, ValueError):
        return _DEFAULT_ATHENA_CONCURRENCY

# Injected async callables (real impls in solver.py / adjudicator.py).
Solve = Callable[[str], Awaitable[str]]


# Adjudicator classification categories — canonical here (the lowest benchmark
# module) so both tool.py and adjudicator.py agree without a circular import.
# Mirrors the okf-sql-benchmark adjudicator taxonomy.
CATEGORY_GENUINE = "GENUINE_ERROR"
CATEGORY_NOISY_GOLD = "NOISY_GOLD"
CATEGORY_AMBIGUOUS = "AMBIGUOUS"
# Distinct sentinel for a classification that ERRORED or couldn't be parsed — it is
# NOT a real verdict and must NOT be forgiven (that was the 100%-judge bug).
CATEGORY_UNKNOWN = "UNKNOWN"


@dataclass
class Verdict:
    """The adjudicator's per-FAIL classification. ``category`` is one of the
    ``CATEGORY_*`` values (GENUINE_ERROR / NOISY_GOLD / AMBIGUOUS / UNKNOWN);
    ``note`` is the gold-free gap note (empty for non-genuine)."""

    q_id: int
    category: str
    note: str = ""


@dataclass
class AdjudicationResult:
    """What the adjudicator returns for a round's FAILs.

    Three disjoint buckets over the FAILs:
    * ``genuine_error_count`` — POSITIVELY classified as a real wiki gap.
    * ``forgiven_count`` — POSITIVELY classified as noisy/broken/ambiguous gold
      (the wiki is not at fault). Only these are "forgiven" in judge accuracy.
    * everything else (errored / unparseable classification) is UNKNOWN — NOT
      forgiven. Counting an errored adjudication as "wiki is fine" is exactly how
      a broken adjudicator inflated judge accuracy to 100% at EX 0%; forgiveness
      must require positive evidence, so unknowns lower judge like genuine errors.

    ``improvements`` is the de-identified theme list from the genuine gaps — the
    ONLY free text that crosses back to the supervisor. ``verdicts`` is the
    per-question detail (q_id → category/note) used to (a) prune forgiven questions
    from later rounds and (b) build the human-facing review; it NEVER crosses the
    tool boundary to the agent.
    """

    improvements: list[str] = field(default_factory=list)
    genuine_error_count: int = 0
    forgiven_count: int = 0
    verdicts: list[Verdict] = field(default_factory=list)


# Human-facing review bucket names (a superset of the adjudicator categories — it
# also covers PASS and DISCARDED, which aren't adjudicated). Stable strings: the
# persisted review JSON and the UI tabs key off these.
BUCKET_PASSED = "passed"
BUCKET_GENUINE = "genuine_error"
BUCKET_NOISY = "noisy_gold"
BUCKET_AMBIGUOUS = "ambiguous"
BUCKET_UNKNOWN = "unknown"
BUCKET_DISCARDED = "discarded"


@dataclass
class QuestionReview:
    """One question's human-facing review row (persisted off-mount, shown in the UI).

    Carries gold + predicted SQL deliberately — this is the transparency artifact a
    HUMAN inspects to verify each verdict. It is written to an off-mount S3 key and
    served only via the Cognito-authed Control API; it NEVER enters the agent's
    result payload or filesystem (same trust boundary as the questions CSV)."""

    q_id: int
    bucket: str
    question: str
    gold_sql: str
    predicted_sql: str = ""
    note: str = ""
    reason: str = ""


@dataclass
class RoundResult:
    """One benchmark round's outcome. ``to_public_dict`` is what the tool returns
    to the supervisor (gold-free/question-free); ``review`` is the human-facing
    per-question detail (with gold) that is persisted off-mount, never returned."""

    iteration: int
    passed: int
    failed: int
    discarded: int
    ex_score: float
    judge_accuracy: float
    genuine_error_count: int
    improvements: list[str]
    target_met: bool
    # q_ids the adjudicator POSITIVELY forgave (NOISY_GOLD / AMBIGUOUS) this round —
    # dropped from the set for all later rounds (they aren't wiki defects, so
    # re-benchmarking them just wastes solver/grader budget).
    forgiven_q_ids: list[int] = field(default_factory=list)
    # Full per-question review (gold + predicted SQL). Human-facing ONLY.
    review: list["QuestionReview"] = field(default_factory=list)

    @property
    def graded(self) -> int:
        return self.passed + self.failed

    def to_public_dict(self) -> dict:
        """The gold-free payload the supervisor sees (aggregated-feedback boundary).

        Deliberately omits ``review``/``forgiven_q_ids`` — the agent gets only the
        aggregate KPIs + anonymous ``improvements``, never per-question / gold data."""
        return {
            "iteration": self.iteration,
            "ex_score": round(self.ex_score, 4),
            "judge_accuracy": round(self.judge_accuracy, 4),
            "passed": self.passed,
            "failed": self.failed,
            "discarded": self.discarded,
            "graded": self.graded,
            "target_met": self.target_met,
            "improvements": list(self.improvements),
        }

    def to_kpi_attrs(self, runtime_session_id: str) -> dict:
        """Attrs for the BENCH# DynamoDB row (durable KPI record)."""
        return {
            "iteration": self.iteration,
            "runtime_session_id": runtime_session_id,
            "ex_score": round(self.ex_score, 4),
            "judge_accuracy": round(self.judge_accuracy, 4),
            "passed": self.passed,
            "failed": self.failed,
            "discarded": self.discarded,
            "graded": self.graded,
            "genuine_error_count": self.genuine_error_count,
            "target_met": self.target_met,
        }


def _judge_accuracy(passed: int, graded: int, forgiven: int) -> float:
    """Genuine-correctness rate: PASSes plus FAILs POSITIVELY forgiven as noisy gold.

    ``judge = (passed + forgiven) / graded``, where ``forgiven`` is ONLY the FAILs
    the adjudicator positively classified as broken/ambiguous gold. A FAIL whose
    adjudication ERRORED or couldn't be parsed is NOT forgiven — it stays counted
    against the wiki. This is the fix for the 0%-EX / 100%-judge false success: a
    broken adjudicator forgives nothing, so judge can't exceed EX just because the
    reviewer fell over. judge is always >= EX (forgiveness only helps) and <= 1.
    """
    if graded <= 0:
        return 0.0
    return min(1.0, (passed + max(0, forgiven)) / graded)


# A progress callback: (phase, current, total) -> None. Best-effort — the caller
# wraps it so a feed emission never breaks a round. No-op when not provided.
Progress = Callable[[str, int, int], None]

# Emit a solve/grade progress tick at most every this fraction of the set (plus
# the final tick), so a 100-question round emits ~10 ticks per phase, not 100.
_PROGRESS_STEP_FRACTION = 0.1


def _tick_every(total: int) -> int:
    """How many completions between progress ticks (>=1)."""
    return max(1, int(total * _PROGRESS_STEP_FRACTION))


async def _solve_all(
    questions: list[BenchmarkQuestion],
    solve: Solve,
    concurrency: int,
    progress: Progress | None = None,
) -> list[tuple[BenchmarkQuestion, str]]:
    """Run all solvers concurrently under a semaphore; return (question, sql) pairs.

    A solver that raises (or times out) yields empty SQL for that question — a
    scored-0 miss, never a crash of the whole round. ``progress`` is ticked as
    solvers COMPLETE (throttled), so the UI shows N/M solved.
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(questions)
    step = _tick_every(total)
    done = 0

    async def _one(q: BenchmarkQuestion) -> tuple[BenchmarkQuestion, str]:
        nonlocal done
        async with sem:
            try:
                sql = await solve(q.question)
            except Exception:  # noqa: BLE001 - a stuck solver is a miss, not a crash
                sql = ""
        # Count on completion (outside the sem) and tick on a step boundary / last.
        done += 1
        if progress and (done % step == 0 or done == total):
            progress("solving", done, total)
        return q, (sql or "")

    return await asyncio.gather(*[_one(q) for q in questions])


async def _grade_all(
    solved: list[tuple[BenchmarkQuestion, str]],
    grader: Grader,
    progress: Progress | None = None,
) -> list[QuestionResult]:
    """Grade all (question, sql) pairs concurrently on a bounded thread pool.

    Grading is blocking Athena I/O, so it runs in threads (the Grader is
    thread-safe). Bounded by ``OKF_BENCHMARK_ATHENA_CONCURRENCY`` to stay under the
    workgroup's concurrent-DML limit. Order is preserved to match ``solved``.
    """
    if not solved:
        return []
    sem = asyncio.Semaphore(_athena_concurrency())
    total = len(solved)
    step = _tick_every(total)
    done = 0

    async def _grade(q: BenchmarkQuestion, sql: str) -> QuestionResult:
        nonlocal done
        async with sem:
            res = await asyncio.to_thread(grader.grade, q.q_id, q.gold_sql, sql)
        done += 1
        if progress and (done % step == 0 or done == total):
            progress("grading", done, total)
        return res

    return await asyncio.gather(*[_grade(q, sql) for q, sql in solved])


def _accepts_on_progress(fn: Callable) -> bool:
    """True iff ``fn`` accepts an ``on_progress`` keyword (the real adjudicator
    does; an injected test fake taking only ``fails`` does not). Signature
    inspection — NOT a TypeError catch — so a genuine TypeError inside the
    adjudicator can't trigger a spurious double-call."""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "on_progress" in params:
        return True
    # A **kwargs-accepting callable can take it too.
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _adjudicate_with_progress(
    adjudicate: Callable[..., Awaitable[AdjudicationResult]],
    fails: list[QuestionResult],
    progress: Progress | None,
) -> AdjudicationResult:
    """Call ``adjudicate(fails)``, forwarding a reviewing-progress hook if it
    accepts one. The real adjudicator takes ``on_progress(done, total)`` and ticks
    per classified failure; a plain ``adjudicate(fails)`` (e.g. a test fake) is
    still supported — then we just emit the terminal reviewing tick."""
    if _accepts_on_progress(adjudicate):
        on_progress = None
        if progress:

            def on_progress(done: int, total: int) -> None:  # noqa: F811
                progress("reviewing", done, total)

        return await adjudicate(fails, on_progress=on_progress)

    adj = await adjudicate(fails)
    if progress:
        progress("reviewing", len(fails), len(fails))
    return adj


async def run_round(
    *,
    iteration: int,
    questions: list[BenchmarkQuestion],
    solve: Solve,
    grader: Grader,
    adjudicate: Callable[[list[QuestionResult]], Awaitable[AdjudicationResult]],
    concurrency: int,
    runtime_session_id: str = "",
    progress: Progress | None = None,
) -> RoundResult:
    """Score the whole question set once (stateless), then adjudicate the failures.

    Steps: solve all (concurrent, bundle-blind) → grade each (deterministic Athena
    set-equality, discards excluded) → adjudicate FAILs (genuine vs noisy-gold,
    consolidate into anonymous themes) → compute EX + judge KPIs + the FIXED target
    → build the per-question review + forgiven-q_id list. ``progress(phase, current,
    total)`` is ticked through the phases for the UI.
    """
    solved = await _solve_all(questions, solve, concurrency, progress)
    graded = await _grade_all(solved, grader, progress)

    passed = sum(1 for r in graded if r.outcome is Outcome.PASS)
    failed = sum(1 for r in graded if r.outcome is Outcome.FAIL)
    discarded = sum(1 for r in graded if r.outcome is Outcome.DISCARDED)
    graded_n = passed + failed

    fails = [r for r in graded if r.outcome is Outcome.FAIL]
    if fails:
        if progress:
            progress("reviewing", 0, len(fails))
        adj = await _adjudicate_with_progress(adjudicate, fails, progress)
    else:
        adj = AdjudicationResult()

    ex = ri.ex_score(passed, graded_n)
    # Forgive ONLY positively-classified noisy gold — not errored/unknown
    # adjudications (which stay counted against the wiki). See _judge_accuracy.
    judge = _judge_accuracy(passed, graded_n, adj.forgiven_count)
    met = ri.target_met(ex=ex, judge=judge)

    review = _build_review(questions, graded, adj.verdicts)
    forgiven_q_ids = [
        v.q_id for v in adj.verdicts if v.category in (CATEGORY_NOISY_GOLD, CATEGORY_AMBIGUOUS)
    ]

    return RoundResult(
        iteration=iteration,
        passed=passed,
        failed=failed,
        discarded=discarded,
        ex_score=ex,
        judge_accuracy=judge,
        genuine_error_count=adj.genuine_error_count,
        improvements=list(adj.improvements),
        target_met=met,
        forgiven_q_ids=forgiven_q_ids,
        review=review,
    )


# Map each adjudicator category to its human-facing review bucket.
_CATEGORY_TO_BUCKET = {
    CATEGORY_GENUINE: BUCKET_GENUINE,
    CATEGORY_NOISY_GOLD: BUCKET_NOISY,
    CATEGORY_AMBIGUOUS: BUCKET_AMBIGUOUS,
    CATEGORY_UNKNOWN: BUCKET_UNKNOWN,
}


def _build_review(
    questions: list[BenchmarkQuestion],
    graded: list[QuestionResult],
    verdicts: list["Verdict"],
) -> list[QuestionReview]:
    """Assemble the per-question human-facing review (gold + predicted + verdict).

    This is the ONLY place all three sources meet: the question text + gold SQL
    (from ``questions``), the graded outcome + predicted SQL (from ``graded``), and
    the adjudicator verdict (from ``verdicts``). PASS/DISCARDED aren't adjudicated,
    so they get their outcome-derived bucket; FAILs get their verdict's bucket
    (UNKNOWN if the adjudicator didn't return one for that q_id)."""
    q_by_id = {q.q_id: q for q in questions}
    verdict_by_id = {v.q_id: v for v in verdicts}
    reviews: list[QuestionReview] = []
    for r in graded:
        q = q_by_id.get(r.q_id)
        # Only FAILs are adjudicated, so ONLY a FAIL takes its verdict's bucket +
        # note. PASS/DISCARDED get their outcome bucket and NO note — a stray verdict
        # for a non-FAIL q_id (shouldn't happen, but don't trust it) must not attach a
        # mismatched note to a passed/discarded row.
        note = ""
        if r.outcome is Outcome.PASS:
            bucket = BUCKET_PASSED
        elif r.outcome is Outcome.DISCARDED:
            bucket = BUCKET_DISCARDED
        else:  # FAIL → its adjudicator verdict bucket (UNKNOWN if unreviewed)
            v = verdict_by_id.get(r.q_id)
            bucket = _CATEGORY_TO_BUCKET.get(v.category if v else "", BUCKET_UNKNOWN)
            note = v.note if v else ""
        reviews.append(
            QuestionReview(
                q_id=r.q_id,
                bucket=bucket,
                question=q.question if q else "",
                gold_sql=q.gold_sql if q else "",
                predicted_sql=r.predicted_sql,
                note=note,
                reason=r.discard_reason or r.reason,
            )
        )
    return reviews
