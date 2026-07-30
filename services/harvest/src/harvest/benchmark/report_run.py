"""The Benchmark Studio run engine — N runs × enabled checks → one report.

Executes a standalone benchmark run (``docs/BENCHMARK_GUIDE.md``): per run *r*
of N, per enabled check, an INDEPENDENT solver round over that check's
participating questions, graded per the check's protocol (:mod:`.checks`).
A ``judge_graded`` check (Behavior) has no deterministic grade — after all
runs complete, the behavior grader rules on EVERY (question, run) attempt
independently. Then the failure-review judge reviews every failed
(question, check) of the deterministic checks once (:mod:`.judge`; behavior
pairs are excluded — their grader already IS the judge), each failed behavior
pair gets ONE question-level SYNTHESIS review (all graded runs together →
comment + consolidated annotation, outcomes untouched), and the per-check
scores (raw + judge-adjusted; a judge-graded check has no adjusted score),
the per-question stability, and the telemetry are folded into the report
document.

Written against **injected** callables so the whole engine is offline-testable
with fakes (no Bedrock, no Athena):

* ``make_solve(spec) -> async solve(question)`` — the caller binds the solver
  model + the wiki snapshot to a check's protocol; the engine builds one solver
  per check and reuses it across runs (each ``solve`` call is an independent
  ReAct run; nothing carries over).
* ``grader`` — the SQL EX grader. Its gold cache means each gold query
  executes once per REPORT, not once per run.
* ``grade_behavior(cases, on_progress) -> grades`` — the per-attempt behavior
  grader (see :mod:`.judge` ``make_behavior_grader``); required iff a
  judge-graded check is enabled.
* ``review_behavior(cases, on_progress) -> verdicts`` — the question-level
  behavior SYNTHESIS review (``make_behavior_reviewer``): one case per failed
  behavior pair, all graded runs together, producing the pair's judge block
  (comment + consolidated annotation) without touching outcomes; required iff
  a judge-graded check is enabled.
* ``judge(cases, on_progress) -> verdicts`` — see :mod:`.judge`.

The engine does NO persistence — it returns ``(report_doc, traces_doc)`` and
the runtime mode writes them (S3 + the REPORT# row). Nothing in either return
path reaches any agent: the report is human-facing, served only via the
Cognito-authed Control API. Progress events (``kind:"benchmark_progress"`` with
``check``/``run`` fields) are emitted best-effort through the injected callback.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from okf_core import benchmark_report as br
from okf_core.benchmark_questions import BenchmarkQuestion

from harvest.benchmark.checks import CHECK_SPECS, CheckSpec, make_grade_fn
from harvest.benchmark.grader import Grader, Outcome, QuestionResult
from harvest.benchmark.judge import (
    VERDICT_PASS,
    BehaviorCase,
    BehaviorGrade,
    JudgeCase,
    JudgeVerdict,
)

_DEFAULT_CONCURRENCY = 10
_DEFAULT_ATHENA_CONCURRENCY = 15


def _llm_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("OKF_BENCHMARK_MAX_CONCURRENCY", "")))
    except (TypeError, ValueError):
        return _DEFAULT_CONCURRENCY


def _athena_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("OKF_BENCHMARK_ATHENA_CONCURRENCY", "")))
    except (TypeError, ValueError):
        return _DEFAULT_ATHENA_CONCURRENCY


@dataclass
class Attempt:
    """One (question, check, run) solve+grade outcome — the report's atom."""

    q_id: int
    check: str
    run_index: int
    prediction: str = ""
    outcome: Outcome = Outcome.FAIL
    reason: str = ""
    trace: Any = None
    usage: dict = field(default_factory=dict)
    wall_ms: int = 0


# A progress callback: (phase, check, run_index, current, total) -> None.
# Contract (the UI renders these verbatim as "run · Check · Phase k/n"):
# per-run phases (solving; deterministic grading) carry their real run_index;
# CROSS-RUN phases (behavior grading, both judge reviews) pass CROSS_RUN (-1)
# — they span all runs, and stamping the last run's number on them mislabeled
# the live line. Every phase emits a (0, total) START tick so the row flips
# the moment the phase begins (a judge case can take minutes to yield its
# first completion tick, during which the row used to keep the PREVIOUS
# phase's label). ``check`` is always the check being worked (the accuracy
# failure review included) — never "".
Progress = Callable[[str, str, int, int, int], None]

PHASE_SOLVING = "solving"
PHASE_GRADING = "grading"
PHASE_JUDGING = "judging"

# run_index sentinel for phases that span all runs (rendered with no run part).
CROSS_RUN = -1

# Emit a tick at most every this fraction of a round (plus the final tick).
_PROGRESS_STEP_FRACTION = 0.1


def _tick_every(total: int) -> int:
    return max(1, int(total * _PROGRESS_STEP_FRACTION))


def _split_solve_result(out: Any) -> tuple[str, Any, dict, int]:
    """Normalize a solver's return into (prediction, trace, usage, wall_ms).

    The real solver returns a ``SolveResult``; a test fake may return a bare
    string. Anything else degrades to an empty prediction.
    """
    if isinstance(out, str):
        return out, None, {}, 0
    sql = getattr(out, "sql", None)
    if isinstance(sql, str):
        return (
            sql,
            getattr(out, "trace", None),
            dict(getattr(out, "usage", None) or {}),
            int(getattr(out, "wall_ms", 0) or 0),
        )
    return "", None, {}, 0


async def _solve_round(
    questions: list[BenchmarkQuestion],
    solve: Callable[[str], Awaitable[Any]],
    *,
    check: str,
    run_index: int,
    concurrency: int,
    progress: Progress | None,
) -> list[Attempt]:
    """Fan out one check's solver round for one run (bounded, crash-proof)."""
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(questions)
    step = _tick_every(total)
    done = 0
    if progress and total:
        progress(PHASE_SOLVING, check, run_index, 0, total)  # start tick

    async def _one(q: BenchmarkQuestion) -> Attempt:
        nonlocal done
        async with sem:
            try:
                prediction, trace, usage, wall_ms = _split_solve_result(
                    await solve(q.question)
                )
            except Exception:  # noqa: BLE001 - a stuck solver is a miss, not a crash
                prediction, trace, usage, wall_ms = "", None, {}, 0
        done += 1
        if progress and (done % step == 0 or done == total):
            progress(PHASE_SOLVING, check, run_index, done, total)
        return Attempt(
            q_id=q.q_id, check=check, run_index=run_index,
            prediction=prediction, trace=trace, usage=usage, wall_ms=wall_ms,
        )

    return await asyncio.gather(*[_one(q) for q in questions])


async def _grade_round(
    attempts: list[Attempt],
    questions_by_id: dict[int, BenchmarkQuestion],
    grade: Callable[[BenchmarkQuestion, str], QuestionResult],
    *,
    spec: CheckSpec,
    run_index: int,
    progress: Progress | None,
) -> None:
    """Grade one round's attempts in place (Athena-bounded when the check needs it)."""
    total = len(attempts)
    step = _tick_every(total)
    done = 0

    if not spec.uses_athena:
        # Pure label compares — no I/O, no semaphore.
        for a in attempts:
            _apply_grade(a, grade(questions_by_id[a.q_id], a.prediction))
            done += 1
            if progress and (done % step == 0 or done == total):
                progress(PHASE_GRADING, spec.check, run_index, done, total)
        return

    sem = asyncio.Semaphore(_athena_concurrency())
    if progress and total:
        # Start tick — Athena grading can take a while before its first
        # completion; without this the row lingers on the solve phase's label.
        progress(PHASE_GRADING, spec.check, run_index, 0, total)

    async def _one(a: Attempt) -> None:
        nonlocal done
        async with sem:
            result = await asyncio.to_thread(
                grade, questions_by_id[a.q_id], a.prediction
            )
        _apply_grade(a, result)
        done += 1
        if progress and (done % step == 0 or done == total):
            progress(PHASE_GRADING, spec.check, run_index, done, total)

    await asyncio.gather(*[_one(a) for a in attempts])


def _apply_grade(attempt: Attempt, result: QuestionResult) -> None:
    """Copy a grade onto its attempt, folding the divergence detail into reason.

    The rowcounts/sample the SQL grader captures are what let the judge reason
    about HOW the result sets diverged — appended to the reason (the attempt's
    one free-text slot) instead of adding SQL-only fields to the generic shape.
    """
    attempt.outcome = result.outcome
    reason = result.reason
    if result.outcome is Outcome.FAIL and result.pred_rowcount is not None:
        detail = f"predicted rowcount={result.pred_rowcount}"
        if result.gold_rowcount is not None:
            detail += f", gold rowcount={result.gold_rowcount}"
        if result.pred_sample:
            detail += f"; predicted sample: {result.pred_sample}"
        reason = f"{reason} ({detail})"
    if result.outcome is Outcome.DISCARDED and result.discard_reason:
        reason = f"{reason}: {result.discard_reason}"
    attempt.reason = reason


def _apply_behavior_grades(
    attempts: list[Attempt], grades: list[BehaviorGrade]
) -> None:
    """Copy each behavior grade onto its attempt (outcome + reason).

    The judge's comment IS the attempt's reason — it's what the report shows
    per run. A review that errored keeps its fail outcome with the error
    folded into the reason, so a human never mistakes a fallen-over judge for
    a real ruling.
    """
    by_key = {(g.q_id, g.check, g.run_index): g for g in grades}
    for a in attempts:
        g = by_key.get((a.q_id, a.check, a.run_index))
        if g is None:
            continue
        a.outcome = Outcome.PASS if g.verdict == VERDICT_PASS else Outcome.FAIL
        reason = g.comment or (
            "the judge passed the run"
            if g.verdict == VERDICT_PASS
            else "the judge failed the run"
        )
        if g.judge_error:
            reason = f"{reason} (review error: {g.judge_error})"
        a.reason = reason


def _build_judge_cases(
    attempts: list[Attempt],
    questions_by_id: dict[int, BenchmarkQuestion],
    total_runs: int,
    *,
    judge_graded: bool = False,
) -> list[JudgeCase]:
    """Group attempts by (q_id, check); a failed pair becomes one judge case.

    Failed = at least one FAIL attempt across the runs. Fully-passing pairs
    never reach the judge; all-DISCARDED pairs (SQL EX gold that can't execute
    — deterministic via the gold cache, so never mixed with FAILs) are excluded
    from scores upstream and aren't wiki failures to review. The case carries
    ALL attempts, passing and failing — the flaky diff is the diagnosis.

    ``judge_graded`` selects which checks' pairs to build: False → the
    deterministic checks' failure-review (overturn) cases; True → the
    judge-graded checks' SYNTHESIS-review cases (same shape — for a behavior
    pair the attempt ``reason`` is already that run's grading comment, so the
    rendered case reads as "runs + rulings").
    """
    by_pair: dict[tuple[int, str], list[Attempt]] = {}
    for a in attempts:
        by_pair.setdefault((a.q_id, a.check), []).append(a)

    cases: list[JudgeCase] = []
    for (q_id, check), pair_attempts in sorted(by_pair.items()):
        if CHECK_SPECS[check].judge_graded is not judge_graded:
            continue
        if not any(a.outcome is Outcome.FAIL for a in pair_attempts):
            continue
        q = questions_by_id[q_id]
        spec = CHECK_SPECS[check]
        cases.append(
            JudgeCase(
                q_id=q_id,
                check=check,
                check_label=spec.label,
                question=q.question,
                gold=q.gold_for(check),
                attempts=sorted(pair_attempts, key=lambda a: a.run_index),
                passed_runs=sum(
                    1 for a in pair_attempts if a.outcome is Outcome.PASS
                ),
                total_runs=total_runs,
            )
        )
    return cases


def _check_scores(
    attempts: list[Attempt],
    verdicts_by_pair: dict[tuple[int, str], JudgeVerdict],
    *,
    check: str,
    runs: int,
) -> dict[str, Any]:
    """One check's score block: raw + judge-adjusted per run, mean ± spread.

    Per run r: ``raw_r = passed_r / graded_r`` where graded excludes DISCARDED;
    ``adjusted_r`` additionally counts a failing attempt as passed when its
    (question, check) was OVERTURNED by the judge. Overturning is pair-level:
    the judge rules once over all attempts, so every failing attempt of an
    overturned pair is forgiven in every run.

    A judge-graded check has NO adjusted score (``adjusted: None``): its raw
    outcomes already carry the judge's authority, and no overturn pass runs
    over them — ``overturned`` is structurally 0 there.
    """
    check_attempts = [a for a in attempts if a.check == check]
    raw_per_run: list[float] = []
    adjusted_per_run: list[float] = []
    for r in range(runs):
        run_attempts = [a for a in check_attempts if a.run_index == r]
        graded = [a for a in run_attempts if a.outcome is not Outcome.DISCARDED]
        passed = sum(1 for a in graded if a.outcome is Outcome.PASS)
        overturned_fails = sum(
            1
            for a in graded
            if a.outcome is Outcome.FAIL
            and verdicts_by_pair.get((a.q_id, a.check), None) is not None
            and verdicts_by_pair[(a.q_id, a.check)].verdict == VERDICT_PASS
        )
        raw_per_run.append(br.score(passed, len(graded)))
        adjusted_per_run.append(
            br.adjusted_score(passed, overturned_fails, len(graded))
        )
    raw_mean, raw_spread = br.mean_and_spread(raw_per_run)
    adj_mean, adj_spread = br.mean_and_spread(adjusted_per_run)

    # Pair-level tallies for the breakdown tiles.
    pairs: dict[int, list[Attempt]] = {}
    for a in check_attempts:
        pairs.setdefault(a.q_id, []).append(a)
    passed_all = flaky = failed_all = discarded = overturned = confirmed = 0
    for q_id, pair in pairs.items():
        if all(a.outcome is Outcome.DISCARDED for a in pair):
            discarded += 1
            continue
        wins = sum(1 for a in pair if a.outcome is Outcome.PASS)
        if wins == len(pair):
            passed_all += 1
            continue
        verdict = verdicts_by_pair.get((q_id, check))
        if verdict is not None and verdict.verdict == VERDICT_PASS:
            overturned += 1
        else:
            confirmed += 1
        if wins > 0:
            flaky += 1
        else:
            failed_all += 1

    return {
        "check": check,
        "label": CHECK_SPECS[check].label,
        "participating": len(pairs),
        "graded": len(pairs) - discarded,
        "discarded": discarded,
        "raw": {
            "mean": round(raw_mean, 4),
            "spread": round(raw_spread, 4),
            "per_run": [round(v, 4) for v in raw_per_run],
        },
        "adjusted": (
            None
            if CHECK_SPECS[check].judge_graded
            else {
                "mean": round(adj_mean, 4),
                "spread": round(adj_spread, 4),
                "per_run": [round(v, 4) for v in adjusted_per_run],
            }
        ),
        "passed_all_runs": passed_all,
        "flaky": flaky,
        "failed_all_runs": failed_all,
        "overturned": overturned,
        "confirmed_failed": confirmed,
    }


def _stability(attempts: list[Attempt], *, check: str, runs: int) -> dict[str, int]:
    """Histogram of pass counts per question: ``{"0": n0, ..., "<N>": nN}``.

    String keys so the JSON round-trips without int-coercion surprises.
    DISCARDED pairs are excluded (they were never gradeable).
    """
    pairs: dict[int, int] = {}
    discarded_ids: set[int] = set()
    for a in attempts:
        if a.check != check:
            continue
        if a.outcome is Outcome.DISCARDED:
            discarded_ids.add(a.q_id)
        pairs.setdefault(a.q_id, 0)
        if a.outcome is Outcome.PASS:
            pairs[a.q_id] += 1
    histogram = {str(k): 0 for k in range(runs + 1)}
    for q_id, wins in pairs.items():
        if q_id in discarded_ids:
            continue
        histogram[str(wins)] = histogram.get(str(wins), 0) + 1
    return histogram


def _fold_usage(usages: list[dict]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for u in usages:
        for key in totals:
            totals[key] += int((u or {}).get(key) or 0)
    return totals


def _solver_stats(attempts: list[Attempt]) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    for a in attempts:
        for name, n in (getattr(a.trace, "tool_counts", None) or {}).items():
            tool_counts[name] = tool_counts.get(name, 0) + int(n)
    solves = len(attempts)
    total_wall = sum(a.wall_ms for a in attempts)
    return {
        "solves": solves,
        "tokens": _fold_usage([a.usage for a in attempts]),
        "tool_counts": dict(sorted(tool_counts.items())),
        "tool_calls": sum(
            int(getattr(a.trace, "tool_calls", 0) or 0) for a in attempts
        ),
        "avg_wall_ms": int(total_wall / solves) if solves else 0,
        "total_wall_ms": total_wall,
    }


def _telemetry(
    attempts: list[Attempt],
    verdicts: list[JudgeVerdict],
    behavior_grades: list[BehaviorGrade],
) -> dict[str, Any]:
    """The report's telemetry block: tokens by role, tool distribution, wall time.

    Behavior grades are judge work (same model, same toolset), so their cases
    and tokens fold into the judge role — a behavior run's "judge cases" is
    its graded attempts. ``by_check`` carries the same solver stats SPLIT per
    check (plus that check's judge cases/tokens) — the UI's telemetry widgets
    offer a total-vs-per-check filter, and the checks run different protocols,
    so their tool mixes and costs genuinely differ.
    """
    judge_usages = [v.usage for v in verdicts] + [g.usage for g in behavior_grades]
    telemetry: dict[str, Any] = {
        "solver": _solver_stats(attempts),
        "judge": {
            "cases": len(verdicts) + len(behavior_grades),
            "tokens": _fold_usage(judge_usages),
        },
        "by_check": {},
    }
    for check in sorted({a.check for a in attempts}):
        check_verdicts = [v for v in verdicts if v.check == check]
        check_grades = [g for g in behavior_grades if g.check == check]
        telemetry["by_check"][check] = {
            **_solver_stats([a for a in attempts if a.check == check]),
            "judge_cases": len(check_verdicts) + len(check_grades),
            "judge_tokens": _fold_usage(
                [v.usage for v in check_verdicts] + [g.usage for g in check_grades]
            ),
        }
    return telemetry


def _question_detail(
    questions: list[BenchmarkQuestion],
    attempts: list[Attempt],
    verdicts_by_pair: dict[tuple[int, str], JudgeVerdict],
    checks: list[str],
) -> list[dict[str, Any]]:
    """The Detailed tab's rows: per question, per participating check, every attempt.

    Attempt entries stay compact (prediction/outcome/reason + telemetry); the
    step-by-step traces live in the separate traces document, flagged here with
    ``has_trace`` so the UI knows a row is openable.
    """
    by_pair: dict[tuple[int, str], list[Attempt]] = {}
    for a in attempts:
        by_pair.setdefault((a.q_id, a.check), []).append(a)

    rows: list[dict[str, Any]] = []
    for q in questions:
        checks_block: dict[str, Any] = {}
        for check in checks:
            pair = sorted(
                by_pair.get((q.q_id, check), []), key=lambda a: a.run_index
            )
            if not pair:
                continue  # the question doesn't participate in this check
            verdict = verdicts_by_pair.get((q.q_id, check))
            checks_block[check] = {
                "gold": q.gold_for(check),
                "passed_runs": sum(1 for a in pair if a.outcome is Outcome.PASS),
                "total_runs": len(pair),
                "discarded": all(a.outcome is Outcome.DISCARDED for a in pair),
                "attempts": [
                    {
                        "run": a.run_index,
                        "outcome": a.outcome.value,
                        "reason": a.reason,
                        "prediction": a.prediction,
                        "wall_ms": a.wall_ms,
                        "tokens": int((a.usage or {}).get("total_tokens") or 0),
                        "has_trace": a.trace is not None,
                    }
                    for a in pair
                ],
                "judge": (
                    {
                        "verdict": verdict.verdict,
                        "comment": verdict.comment,
                        "annotation": verdict.annotation,
                        "judge_error": verdict.judge_error,
                    }
                    if verdict is not None
                    else None
                ),
            }
        if checks_block:
            rows.append(
                {"q_id": q.q_id, "question": q.question, "checks": checks_block}
            )
    return rows


def _traces_doc(report_id: str, attempts: list[Attempt]) -> dict[str, Any]:
    """The lazily-fetched traces document: EVERY attempt's trace, pass or fail.

    A passing run's trace is evidence too (what a solve that WORKED read — the
    diff against a failing run is the diagnosis), so nothing is dropped here.
    The size bound is the per-trace caps in :mod:`.trace` (steps, per-step
    text, whole-trace budget), which keep even a 100 × 2 × 5 report's document
    in the single-digit MB range.
    """
    from dataclasses import asdict

    rows = []
    for a in sorted(attempts, key=lambda x: (x.q_id, x.check, x.run_index)):
        if a.trace is None:
            continue
        rows.append(
            {"q_id": a.q_id, "check": a.check, "run": a.run_index, **asdict(a.trace)}
        )
    return {"report_id": report_id, "traces": rows}


async def execute_report(
    *,
    report_id: str,
    checks: list[str],
    runs: int,
    questions: list[BenchmarkQuestion],
    make_solve: Callable[[CheckSpec], Callable[[str], Awaitable[Any]]],
    grader: Grader | None,
    judge: Callable[..., Awaitable[list[JudgeVerdict]]],
    grade_behavior: Callable[..., Awaitable[list[BehaviorGrade]]] | None = None,
    review_behavior: Callable[..., Awaitable[list[JudgeVerdict]]] | None = None,
    config_recap: dict[str, Any] | None = None,
    progress: Progress | None = None,
    concurrency: int | None = None,
    before_judge: Callable[[list[Attempt]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the whole benchmark: N runs × checks → judge once → (report, traces).

    ``config_recap`` is echoed into the report verbatim (models, version, the
    question-set counts) — the engine doesn't interpret it. ``before_judge``
    (optional) is called with EVERY attempt once all runs complete AND all
    grading (deterministic + behavior) is done, before the failure-review
    phase — the studio uses it to lay the solve traces (with their true
    outcomes) into the judge's file tree so the review can ``grep`` across
    them. Raises nothing for per-question trouble (a stuck solver is a miss,
    an errored judge review is a confirmed fail); a genuinely broken setup (no
    participating questions for any enabled check, or a judge-graded check
    without a behavior grader) raises ``ValueError`` so the runtime fails the
    report LOUDLY rather than publishing an empty success.
    """
    llm_concurrency = concurrency or _llm_concurrency()
    questions_by_id = {q.q_id: q for q in questions}

    participating: dict[str, list[BenchmarkQuestion]] = {
        check: [q for q in questions if q.gold_for(check)] for check in checks
    }
    if not any(participating.values()):
        raise ValueError(
            "no questions participate in any enabled check — nothing to benchmark"
        )
    judge_graded_checks = [
        c for c in checks if CHECK_SPECS[c].judge_graded and participating[c]
    ]
    if judge_graded_checks and (grade_behavior is None or review_behavior is None):
        raise ValueError(
            f"check(s) {judge_graded_checks} are judge-graded but no behavior "
            "grader/reviewer was provided"
        )

    solvers = {check: make_solve(CHECK_SPECS[check]) for check in checks}
    grade_fns = {
        check: make_grade_fn(CHECK_SPECS[check], grader=grader)
        for check in checks
        if not CHECK_SPECS[check].judge_graded
    }

    attempts: list[Attempt] = []
    for run_index in range(runs):
        for check in checks:
            round_questions = participating[check]
            if not round_questions:
                continue
            spec = CHECK_SPECS[check]
            round_attempts = await _solve_round(
                round_questions,
                solvers[check],
                check=check,
                run_index=run_index,
                concurrency=llm_concurrency,
                progress=progress,
            )
            if not spec.judge_graded:
                await _grade_round(
                    round_attempts,
                    questions_by_id,
                    grade_fns[check],
                    spec=spec,
                    run_index=run_index,
                    progress=progress,
                )
            attempts.extend(round_attempts)

    # Judge-graded checks: the judge rules on EVERY attempt, independently —
    # grading run 3 never sees run 1 (no cross-run anchoring), which is also
    # why this runs BEFORE before_judge lays the trace files down: the
    # behavior grader gets each case's own trace inline, not the whole tree.
    behavior_grades: list[BehaviorGrade] = []
    for check in judge_graded_checks:
        b_cases = [
            BehaviorCase(
                q_id=a.q_id,
                check=a.check,
                run_index=a.run_index,
                question=questions_by_id[a.q_id].question,
                expected=questions_by_id[a.q_id].gold_for(check),
                attempt=a,
            )
            for a in attempts
            if a.check == check
        ]
        b_progress = None
        if progress and b_cases:
            # CROSS_RUN: this grades every run's attempts together — a run
            # number here would mislabel the live line.
            progress(PHASE_GRADING, check, CROSS_RUN, 0, len(b_cases))

            def b_progress(done: int, total: int, _check=check) -> None:  # noqa: F811
                progress(PHASE_GRADING, _check, CROSS_RUN, done, total)

        behavior_grades.extend(
            await grade_behavior(b_cases, on_progress=b_progress)
        )
    _apply_behavior_grades(attempts, behavior_grades)

    if before_judge is not None:
        before_judge(attempts)

    cases = _build_judge_cases(attempts, questions_by_id, runs)
    judge_progress = None
    if progress and cases:
        # Label the failure review with the check it reviews (today always the
        # deterministic checks' — "" left the live line check-less exactly when
        # the human most wants to know what the judge is judging).
        review_checks = {c.check for c in cases}
        judged_check = review_checks.pop() if len(review_checks) == 1 else ""
        progress(PHASE_JUDGING, judged_check, CROSS_RUN, 0, len(cases))

        def judge_progress(done: int, total: int) -> None:  # noqa: F811
            progress(PHASE_JUDGING, judged_check, CROSS_RUN, done, total)

    verdicts = await judge(cases, on_progress=judge_progress) if cases else []

    # Behavior SYNTHESIS review: one question-level case per behavior pair
    # with ≥1 failing run, seeing all graded attempts together — the cross-run
    # diff the independent gradings deliberately never had. Outcomes are
    # settled; these verdicts exist for the question-level comment and the
    # consolidated annotation (there is nothing to overturn). Runs after
    # before_judge so it can grep the on-disk traces like the failure review.
    review_cases = _build_judge_cases(
        attempts, questions_by_id, runs, judge_graded=True
    )
    review_progress = None
    if progress and review_cases:
        review_check = review_cases[0].check
        progress(PHASE_JUDGING, review_check, CROSS_RUN, 0, len(review_cases))

        def review_progress(done: int, total: int) -> None:  # noqa: F811
            progress(PHASE_JUDGING, review_check, CROSS_RUN, done, total)

    review_verdicts = (
        await review_behavior(review_cases, on_progress=review_progress)
        if review_cases
        else []
    )

    all_verdicts = [*verdicts, *review_verdicts]
    verdicts_by_pair = {(v.q_id, v.check): v for v in all_verdicts}

    scores = {
        check: _check_scores(
            attempts, verdicts_by_pair, check=check, runs=runs
        )
        for check in checks
        if participating[check]
    }
    stability = {
        check: _stability(attempts, check=check, runs=runs)
        for check in checks
        if participating[check]
    }
    candidates = [
        {"q_id": v.q_id, "check": v.check, "annotation": v.annotation}
        for v in all_verdicts
        if v.verdict != VERDICT_PASS and v.annotation
    ]

    report = {
        "report_id": report_id,
        "config": dict(config_recap or {}),
        "scores": scores,
        "stability": stability,
        "questions": _question_detail(
            questions, attempts, verdicts_by_pair, checks
        ),
        "telemetry": _telemetry(attempts, all_verdicts, behavior_grades),
        "annotations": {
            "status": br.AGG_IDLE,
            "candidates": candidates,
            "final": [],
        },
    }
    return report, _traces_doc(report_id, attempts)
