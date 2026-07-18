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
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from harvest.benchmark.grader import Grader, Outcome, QuestionResult
from harvest.benchmark.questions import BenchmarkQuestion
from okf_core import recursive_improvement as ri

# Injected async callables (real impls in solver.py / adjudicator.py).
Solve = Callable[[str], Awaitable[str]]


@dataclass
class AdjudicationResult:
    """What the adjudicator returns for a round's FAILs.

    ``genuine_error_count`` is the number of FAILs judged a real wiki gap;
    ``noisy_or_ambiguous`` is the rest (broken/ambiguous gold — the wiki is
    effectively correct). ``improvements`` is the de-identified theme list — the
    ONLY free text that crosses back to the supervisor.
    """

    improvements: list[str] = field(default_factory=list)
    genuine_error_count: int = 0
    noisy_or_ambiguous: int = 0


@dataclass
class RoundResult:
    """One benchmark round's outcome. ``to_public_dict`` is what the tool returns
    to the supervisor (gold-free/question-free)."""

    iteration: int
    passed: int
    failed: int
    discarded: int
    ex_score: float
    judge_accuracy: float
    genuine_error_count: int
    improvements: list[str]
    threshold_met: bool

    @property
    def graded(self) -> int:
        return self.passed + self.failed

    def to_public_dict(self) -> dict:
        """The gold-free payload the supervisor sees (aggregated-feedback boundary)."""
        return {
            "iteration": self.iteration,
            "ex_score": round(self.ex_score, 4),
            "judge_accuracy": round(self.judge_accuracy, 4),
            "passed": self.passed,
            "failed": self.failed,
            "discarded": self.discarded,
            "graded": self.graded,
            "threshold_met": self.threshold_met,
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
            "threshold_met": self.threshold_met,
        }


def _judge_accuracy(passed: int, graded: int, genuine_errors: int) -> float:
    """Genuine-correctness rate: treat noisy-gold/ambiguous FAILs as correct.

    Of the graded questions, the ones the wiki got *genuinely* wrong are the
    genuine-error FAILs; everything else (PASS, or a FAIL blamed on broken/ambiguous
    gold) counts as the wiki being effectively correct. So
    ``judge = (graded - genuine_errors) / graded``. This is always >= raw EX
    (it forgives noisy-gold), which is the point — raw EX rewards matching possibly
    broken gold; judge accuracy is the honest quality signal.
    """
    if graded <= 0:
        return 0.0
    return max(0.0, (graded - genuine_errors)) / graded


async def _solve_all(
    questions: list[BenchmarkQuestion],
    solve: Solve,
    concurrency: int,
) -> list[tuple[BenchmarkQuestion, str]]:
    """Run all solvers concurrently under a semaphore; return (question, sql) pairs.

    A solver that raises (or times out) yields empty SQL for that question — a
    scored-0 miss, never a crash of the whole round.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(q: BenchmarkQuestion) -> tuple[BenchmarkQuestion, str]:
        async with sem:
            try:
                sql = await solve(q.question)
            except Exception:  # noqa: BLE001 - a stuck solver is a miss, not a crash
                sql = ""
            return q, (sql or "")

    return await asyncio.gather(*[_one(q) for q in questions])


async def run_round(
    *,
    iteration: int,
    questions: list[BenchmarkQuestion],
    config: dict,
    solve: Solve,
    grader: Grader,
    adjudicate: Callable[[list[QuestionResult]], Awaitable[AdjudicationResult]],
    concurrency: int,
    runtime_session_id: str = "",
) -> RoundResult:
    """Score the whole question set once (stateless), then adjudicate the failures.

    Steps: solve all (concurrent, bundle-blind) → grade each (deterministic Athena
    set-equality, discards excluded) → adjudicate FAILs (genuine vs noisy-gold,
    consolidate into anonymous themes) → compute EX + judge KPIs + threshold.
    """
    solved = await _solve_all(questions, solve, concurrency)
    graded = [grader.grade(q.q_id, q.gold_sql, sql) for q, sql in solved]

    passed = sum(1 for r in graded if r.outcome is Outcome.PASS)
    failed = sum(1 for r in graded if r.outcome is Outcome.FAIL)
    discarded = sum(1 for r in graded if r.outcome is Outcome.DISCARDED)
    graded_n = passed + failed

    fails = [r for r in graded if r.outcome is Outcome.FAIL]
    adj = await adjudicate(fails) if fails else AdjudicationResult()

    ex = ri.ex_score(passed, graded_n)
    judge = _judge_accuracy(passed, graded_n, adj.genuine_error_count)
    met = ri.thresholds_met(config, ex=ex, judge=judge)

    return RoundResult(
        iteration=iteration,
        passed=passed,
        failed=failed,
        discarded=discarded,
        ex_score=ex,
        judge_accuracy=judge,
        genuine_error_count=adj.genuine_error_count,
        improvements=list(adj.improvements),
        threshold_met=met,
    )
