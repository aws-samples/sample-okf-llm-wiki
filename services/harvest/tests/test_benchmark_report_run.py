"""The report engine: runs × checks orchestration, judge-once, scores, telemetry."""

from __future__ import annotations

import asyncio

import pytest

from okf_core.benchmark_questions import (
    CHECK_BEHAVIOR,
    CHECK_SQL,
    BenchmarkQuestion,
)

from harvest.benchmark.grader import Grader
from harvest.benchmark.judge import (
    VERDICT_FAIL,
    VERDICT_PASS,
    BehaviorGrade,
    JudgeVerdict,
)
from harvest.benchmark.report_run import execute_report
from harvest.benchmark.trace import SolveResult, SolverTrace


def _q(q_id, sql="", behavior=""):
    return BenchmarkQuestion(
        q_id=q_id, question=f"Q{q_id}", gold_sql=sql, expected_behavior=behavior,
    )


def _fake_execute(rows_by_sql):
    def execute(sql):
        resp = rows_by_sql[sql]
        if isinstance(resp, Exception):
            raise resp
        return resp
    return execute


def _scripted_solver(script):
    """make_solve returning predictions from script[(check, question)] — a list
    consumed one per run, or a single value reused every run."""
    counters = {}

    def make_solve(spec):
        async def solve(question):
            key = (spec.check, question)
            value = script[key]
            if isinstance(value, list):
                i = counters.get(key, 0)
                counters[key] = i + 1
                value = value[min(i, len(value) - 1)]
            return value
        return solve

    return make_solve


async def _judge_all_fail(cases, on_progress=None):
    out = [
        JudgeVerdict(q_id=c.q_id, check=c.check, verdict=VERDICT_FAIL,
                     comment="confirmed", annotation=f"fix for q{c.q_id}/{c.check}")
        for c in cases
    ]
    if on_progress:
        on_progress(len(cases), len(cases))
    return out


def _scripted_behavior_grader(script=None, default=VERDICT_PASS, **grade_kw):
    """A fake per-attempt behavior grader: verdicts from script[(q_id, run)]."""

    async def grade(cases, on_progress=None):
        out = [
            BehaviorGrade(
                q_id=c.q_id, check=c.check, run_index=c.run_index,
                verdict=(script or {}).get((c.q_id, c.run_index), default),
                comment=f"comment q{c.q_id} r{c.run_index}",
                **grade_kw,
            )
            for c in cases
        ]
        if on_progress:
            on_progress(len(cases), len(cases))
        return out

    return grade


def _scripted_behavior_reviewer(
    comment="question-level diagnosis", annotation="", **verdict_kw
):
    """A fake synthesis reviewer; records the cases it saw on ``review.seen``."""
    seen = []

    async def review(cases, on_progress=None):
        seen.extend(cases)
        out = [
            JudgeVerdict(q_id=c.q_id, check=c.check, verdict=VERDICT_FAIL,
                         comment=comment, annotation=annotation, **verdict_kw)
            for c in cases
        ]
        if on_progress:
            on_progress(len(cases), len(cases))
        return out

    review.seen = seen
    return review


def _run(**kw):
    return asyncio.run(execute_report(**kw))


def test_single_check_single_run_all_pass():
    qs = [_q(0, sql="G0"), _q(1, sql="G1")]
    rows = {"G0": [{"c": "0"}], "G1": [{"c": "1"}],
            "P0": [{"c": "0"}], "P1": [{"c": "1"}]}
    seen_cases = []

    async def judge(cases, on_progress=None):
        seen_cases.extend(cases)
        return []

    report, traces = _run(
        report_id="r1", checks=[CHECK_SQL], runs=1, questions=qs,
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P0", (CHECK_SQL, "Q1"): "P1"}),
        grader=Grader(_fake_execute(rows)), judge=judge,
    )
    s = report["scores"][CHECK_SQL]
    assert s["raw"]["mean"] == 1.0 and s["adjusted"]["mean"] == 1.0
    assert s["passed_all_runs"] == 2 and s["confirmed_failed"] == 0
    # Fully-passing questions never reach the judge.
    assert seen_cases == []
    # No failures → no traces persisted.
    assert traces == {"report_id": "r1", "traces": []}


def test_checks_run_independent_rounds():
    q = _q(0, sql="G", behavior="Should cite the doc.")
    rows = {"G": [{"c": "1"}], "SQLPRED": [{"c": "1"}]}
    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL, CHECK_BEHAVIOR], runs=1,
        questions=[q],
        make_solve=_scripted_solver({
            (CHECK_SQL, "Q0"): "SQLPRED",
            (CHECK_BEHAVIOR, "Q0"): "The doc says X.",
        }),
        grader=Grader(_fake_execute(rows)),
        judge=_judge_all_fail,
        grade_behavior=_scripted_behavior_grader(),
        review_behavior=_scripted_behavior_reviewer(),
    )
    assert set(report["scores"]) == {CHECK_SQL, CHECK_BEHAVIOR}
    assert all(report["scores"][c]["raw"]["mean"] == 1.0 for c in report["scores"])
    row = report["questions"][0]
    assert set(row["checks"]) == {CHECK_SQL, CHECK_BEHAVIOR}
    # Per-check golds surface on the human-facing detail.
    assert row["checks"][CHECK_BEHAVIOR]["gold"] == "Should cite the doc."


def test_participation_filters_each_round():
    # Q0 sql-only, Q1 behavior-only: each round grades only its participants.
    qs = [_q(0, sql="G0"), _q(1, behavior="Should refuse.")]
    rows = {"G0": [{"c": "0"}], "P0": [{"c": "0"}]}
    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL, CHECK_BEHAVIOR], runs=1, questions=qs,
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P0",
                                     (CHECK_BEHAVIOR, "Q1"): "I refuse."}),
        grader=Grader(_fake_execute(rows)),
        judge=_judge_all_fail,
        grade_behavior=_scripted_behavior_grader(),
        review_behavior=_scripted_behavior_reviewer(),
    )
    assert report["scores"][CHECK_SQL]["participating"] == 1
    assert report["scores"][CHECK_BEHAVIOR]["participating"] == 1
    q0_row = next(r for r in report["questions"] if r["q_id"] == 0)
    assert list(q0_row["checks"]) == [CHECK_SQL]


def test_flaky_question_judged_once_with_all_attempts():
    # 3 runs, passes once — the judge sees ONE case with all three attempts.
    q = _q(0, sql="G")
    rows = {"G": [{"c": "1"}], "P_OK": [{"c": "1"}], "P_BAD": [{"c": "2"}]}
    seen = []

    async def judge(cases, on_progress=None):
        seen.extend(cases)
        return [JudgeVerdict(q_id=c.q_id, check=c.check, verdict=VERDICT_FAIL,
                             comment="c") for c in cases]

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=3, questions=[q],
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): ["P_OK", "P_BAD", "P_BAD"]}),
        grader=Grader(_fake_execute(rows)), judge=judge,
    )
    assert len(seen) == 1
    case = seen[0]
    assert case.passed_runs == 1 and case.total_runs == 3
    assert len(case.attempts) == 3
    assert case.gold == "G"  # the judge is the one role that sees gold
    s = report["scores"][CHECK_SQL]
    assert s["flaky"] == 1 and s["passed_all_runs"] == 0
    # Stability histogram: one question passed 1 of 3.
    assert report["stability"][CHECK_SQL] == {"0": 0, "1": 1, "2": 0, "3": 0}


def test_overturned_pair_lifts_adjusted_score_in_every_run():
    q = _q(0, sql="G")
    rows = {"G": [{"c": "1"}], "P": [{"c": "9"}]}

    async def judge(cases, on_progress=None):
        return [JudgeVerdict(q_id=c.q_id, check=c.check, verdict=VERDICT_PASS,
                             comment="broken gold") for c in cases]

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=2, questions=[q],
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P"}),
        grader=Grader(_fake_execute(rows)), judge=judge,
    )
    s = report["scores"][CHECK_SQL]
    assert s["raw"]["per_run"] == [0.0, 0.0]
    assert s["adjusted"]["per_run"] == [1.0, 1.0]
    assert s["overturned"] == 1 and s["confirmed_failed"] == 0


def test_mean_and_spread_across_runs():
    # 2 questions × 3 runs; runs are per-run over the SAME set. Script: Q0
    # always right, Q1 right in run 0 only → per-run raw = [1.0, 0.5, 0.5].
    qs = [_q(0, sql="G0"), _q(1, sql="G1")]
    rows = {"G0": [{"c": "0"}], "G1": [{"c": "1"}], "P0": [{"c": "0"}],
            "P1_OK": [{"c": "1"}], "P1_BAD": [{"c": "9"}]}
    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=3, questions=qs,
        make_solve=_scripted_solver({
            (CHECK_SQL, "Q0"): "P0",
            (CHECK_SQL, "Q1"): ["P1_OK", "P1_BAD", "P1_BAD"],
        }),
        grader=Grader(_fake_execute(rows)), judge=_judge_all_fail,
    )
    s = report["scores"][CHECK_SQL]
    assert s["raw"]["per_run"] == [1.0, 0.5, 0.5]
    assert s["raw"]["mean"] == round(2.0 / 3, 4)
    assert s["raw"]["spread"] == 0.5


def test_sql_discards_excluded_from_scores_and_never_judged():
    qs = [_q(0, sql="BROKEN_GOLD"), _q(1, sql="G1")]
    rows = {"BROKEN_GOLD": RuntimeError("COLUMN_NOT_FOUND"),
            "G1": [{"c": "1"}], "P": [{"c": "1"}]}
    seen = []

    async def judge(cases, on_progress=None):
        seen.extend(cases)
        return []

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=2, questions=qs,
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P", (CHECK_SQL, "Q1"): "P"}),
        grader=Grader(_fake_execute(rows)), judge=judge,
    )
    s = report["scores"][CHECK_SQL]
    assert s["discarded"] == 1 and s["graded"] == 1
    assert s["raw"]["per_run"] == [1.0, 1.0]  # the discard never dilutes
    assert seen == []  # an unanswerable question is not a wiki failure to review
    q0 = next(r for r in report["questions"] if r["q_id"] == 0)
    assert q0["checks"][CHECK_SQL]["discarded"] is True


class _Throttle(Exception):
    """A botocore-shaped ClientError carrying a transient error code."""

    def __init__(self):
        super().__init__("rate exceeded")
        self.response = {"Error": {"Code": "ThrottlingException"}}


def _throttle_then_rows(gold_sql, pred_rows, attempts=3):
    """An execute whose gold throttles through ``attempts`` calls (one run's
    full retry budget → a transient, NON-memoized DISCARD), then succeeds."""
    calls = {"n": 0}

    def execute(sql):
        if sql == gold_sql:
            calls["n"] += 1
            if calls["n"] <= attempts:
                raise _Throttle()
            return [{"c": "1"}]
        return pred_rows

    return execute


def test_transient_discard_beside_a_pass_counts_as_passed():
    # Reproduced: runs=2, the gold throttles through run 0's whole retry
    # budget (transient DISCARD, deliberately not memoized), run 1 re-executes
    # and the prediction PASSes. The [DISCARDED, PASS] pair used to tally as
    # confirmed_failed AND flaky with no judge review — a question whose only
    # graded run passed rendered as a red "Wiki gap".
    q = _q(0, sql="G")
    seen = []

    async def judge(cases, on_progress=None):
        seen.extend(cases)
        return []

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=2, questions=[q],
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P"}),
        grader=Grader(_throttle_then_rows("G", [{"c": "1"}]),
                      sleep=lambda _s: None),
        judge=judge,
    )
    s = report["scores"][CHECK_SQL]
    assert s["passed_all_runs"] == 1
    assert s["confirmed_failed"] == 0 and s["flaky"] == 0
    assert s["failed_all_runs"] == 0 and s["discarded"] == 0
    assert s["graded"] == 1
    assert seen == []  # nothing failed → nothing for the judge
    # Per-run scores keep their graded-only denominators: run 0 graded nothing
    # (0.0 by the graded=0 convention), run 1 passed everything.
    assert s["raw"]["per_run"] == [0.0, 1.0]
    row = report["questions"][0]["checks"][CHECK_SQL]
    assert row["discarded"] is False  # mixed pair, not an all-discarded one
    assert [a["outcome"] for a in row["attempts"]] == ["DISCARDED", "PASS"]


def test_transient_discard_beside_a_fail_is_confirmed_with_a_judge_case():
    q = _q(0, sql="G")
    seen = []

    async def judge(cases, on_progress=None):
        seen.extend(cases)
        return [JudgeVerdict(q_id=c.q_id, check=c.check, verdict=VERDICT_FAIL,
                             comment="confirmed") for c in cases]

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=2, questions=[q],
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P"}),
        grader=Grader(_throttle_then_rows("G", [{"c": "9"}]),
                      sleep=lambda _s: None),
        judge=judge,
    )
    s = report["scores"][CHECK_SQL]
    assert s["confirmed_failed"] == 1 and s["failed_all_runs"] == 1
    assert s["flaky"] == 0 and s["passed_all_runs"] == 0 and s["discarded"] == 0
    assert len(seen) == 1  # the failed pair reached the judge


def test_no_participating_questions_raises_loudly():
    with pytest.raises(ValueError):
        _run(
            report_id="r1", checks=[CHECK_BEHAVIOR], runs=1,
            questions=[_q(0, sql="G")],  # sql-only set, behavior-only run
            make_solve=_scripted_solver({}), grader=None,
            judge=_judge_all_fail,
            grade_behavior=_scripted_behavior_grader(),
        )


def test_behavior_without_a_grader_raises_loudly():
    # A judge-graded check with no behavior grader/reviewer is a wiring bug —
    # the run must fail loudly, never publish ungraded attempts as a report.
    with pytest.raises(ValueError, match="judge-graded"):
        _run(
            report_id="r1", checks=[CHECK_BEHAVIOR], runs=1,
            questions=[_q(0, behavior="Should refuse.")],
            make_solve=_scripted_solver({(CHECK_BEHAVIOR, "Q0"): "no"}),
            grader=None, judge=_judge_all_fail,
        )
    with pytest.raises(ValueError, match="judge-graded"):
        _run(
            report_id="r1", checks=[CHECK_BEHAVIOR], runs=1,
            questions=[_q(0, behavior="Should refuse.")],
            make_solve=_scripted_solver({(CHECK_BEHAVIOR, "Q0"): "no"}),
            grader=None, judge=_judge_all_fail,
            grade_behavior=_scripted_behavior_grader(),  # reviewer still missing
        )


def test_behavior_is_judge_graded_per_attempt_with_synthesis_review():
    # 2 runs: run 0 passes, run 1 fails. Every attempt gets its own ruling
    # (the grader's comment becomes the attempt's reason); the failed pair
    # then gets ONE synthesis review over ALL graded runs, which provides the
    # pair-level judge block (comment + consolidated annotation) — and the
    # behavior pair NEVER reaches the overturn-review judge.
    q = _q(0, behavior="Should say pit stops are not tracked.")
    overturn_reviewed = []

    async def judge(cases, on_progress=None):
        overturn_reviewed.extend(cases)
        return []

    reviewer = _scripted_behavior_reviewer(
        comment="flaky: run 2 invented a duration; run 1 correctly declined",
        annotation="document that pit stops are not tracked",
    )
    report, _ = _run(
        report_id="r1", checks=[CHECK_BEHAVIOR], runs=2, questions=[q],
        make_solve=_scripted_solver({(CHECK_BEHAVIOR, "Q0"): ["ok", "made it up"]}),
        grader=None, judge=judge,
        grade_behavior=_scripted_behavior_grader(
            script={(0, 0): VERDICT_PASS, (0, 1): VERDICT_FAIL},
        ),
        review_behavior=reviewer,
    )
    assert overturn_reviewed == []  # no self-overturning review
    # The synthesis reviewer saw ONE case carrying ALL graded attempts, with
    # the expectation as its gold and the per-run comments as reasons.
    assert len(reviewer.seen) == 1
    case = reviewer.seen[0]
    assert case.gold == "Should say pit stops are not tracked."
    assert len(case.attempts) == 2 and case.passed_runs == 1
    assert case.attempts[1].reason == "comment q0 r1"
    s = report["scores"][CHECK_BEHAVIOR]
    assert s["raw"]["per_run"] == [1.0, 0.0]
    assert s["adjusted"] is None  # no judge-adjusted score for a judge-graded check
    assert s["overturned"] == 0 and s["confirmed_failed"] == 1 and s["flaky"] == 1
    row = report["questions"][0]["checks"][CHECK_BEHAVIOR]
    assert [a["outcome"] for a in row["attempts"]] == ["PASS", "FAIL"]
    assert row["attempts"][0]["reason"] == "comment q0 r0"
    assert row["attempts"][1]["reason"] == "comment q0 r1"
    judge_block = row["judge"]
    assert judge_block["verdict"] == VERDICT_FAIL
    assert judge_block["comment"] == (
        "flaky: run 2 invented a duration; run 1 correctly declined"
    )
    assert judge_block["annotation"] == "document that pit stops are not tracked"
    # The reviewer's consolidated annotation flows into the candidates.
    assert report["annotations"]["candidates"] == [
        {"q_id": 0, "check": CHECK_BEHAVIOR,
         "annotation": "document that pit stops are not tracked"}
    ]


def test_behavior_all_pass_has_no_judge_block_and_no_review():
    q = _q(0, behavior="Should refuse.")
    reviewer = _scripted_behavior_reviewer(annotation="never used")
    report, _ = _run(
        report_id="r1", checks=[CHECK_BEHAVIOR], runs=2, questions=[q],
        make_solve=_scripted_solver({(CHECK_BEHAVIOR, "Q0"): "I refuse."}),
        grader=None, judge=_judge_all_fail,
        grade_behavior=_scripted_behavior_grader(),
        review_behavior=reviewer,
    )
    assert reviewer.seen == []  # nothing failed → nothing to summarize
    row = report["questions"][0]["checks"][CHECK_BEHAVIOR]
    assert row["judge"] is None
    assert report["annotations"]["candidates"] == []
    # Passing attempts still carry the judge's comment as the reason.
    assert row["attempts"][0]["reason"] == "comment q0 r0"


def test_behavior_grading_error_counts_against_the_wiki():
    q = _q(0, behavior="Should refuse.")

    async def grade(cases, on_progress=None):
        return [
            BehaviorGrade(q_id=c.q_id, check=c.check, run_index=c.run_index,
                          verdict=VERDICT_FAIL, comment="review fell over",
                          judge_error="boom")
            for c in cases
        ]

    report, _ = _run(
        report_id="r1", checks=[CHECK_BEHAVIOR], runs=1, questions=[q],
        make_solve=_scripted_solver({(CHECK_BEHAVIOR, "Q0"): "hm"}),
        grader=None, judge=_judge_all_fail, grade_behavior=grade,
        review_behavior=_scripted_behavior_reviewer(
            judge_error="summary fell over"
        ),
    )
    s = report["scores"][CHECK_BEHAVIOR]
    assert s["raw"]["mean"] == 0.0  # never silently forgiven
    row = report["questions"][0]["checks"][CHECK_BEHAVIOR]
    assert "review error: boom" in row["attempts"][0]["reason"]
    # The judge block is the SYNTHESIS review's — a fallen-over summary
    # surfaces its own error without touching the settled outcomes.
    assert row["judge"]["judge_error"] == "summary fell over"


def test_solver_traces_and_telemetry_fold_into_the_report():
    q = _q(0, behavior="Should cite the doc.")
    trace = SolverTrace(
        turns=4, tool_calls=2, tool_counts={"read_file": 1, "grep": 1},
        files_read=["tables/t.md"],
    )

    def make_solve(spec):
        async def solve(question):
            return SolveResult(
                sql="an answer", trace=trace,
                usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                wall_ms=500,
            )
        return solve

    report, traces = _run(
        report_id="r1", checks=[CHECK_BEHAVIOR], runs=2, questions=[q],
        make_solve=make_solve, grader=None, judge=_judge_all_fail,
        grade_behavior=_scripted_behavior_grader(
            default=VERDICT_FAIL,
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        ),
        review_behavior=_scripted_behavior_reviewer(
            usage={"input_tokens": 30, "output_tokens": 10, "total_tokens": 40},
        ),
    )
    t = report["telemetry"]
    assert t["solver"]["solves"] == 2
    assert t["solver"]["tokens"]["total_tokens"] == 240
    assert t["solver"]["tool_counts"] == {"grep": 2, "read_file": 2}
    assert t["solver"]["avg_wall_ms"] == 500
    # Behavior is all judge work: one grading per ATTEMPT (2 runs → 2 cases)
    # plus ONE synthesis review for the failed pair → 3 cases, tokens folded.
    assert t["judge"]["cases"] == 3
    assert t["judge"]["tokens"]["total_tokens"] == 70
    # Per-check split for the UI's total-vs-check telemetry filters.
    per = t["by_check"][CHECK_BEHAVIOR]
    assert per["solves"] == 2
    assert per["tool_counts"] == {"grep": 2, "read_file": 2}
    assert per["judge_cases"] == 3
    assert per["judge_tokens"]["total_tokens"] == 70
    # Every attempt's trace persists, keyed (q_id, check, run).
    assert [(r["q_id"], r["check"], r["run"]) for r in traces["traces"]] == [
        (0, CHECK_BEHAVIOR, 0), (0, CHECK_BEHAVIOR, 1),
    ]
    assert traces["traces"][0]["files_read"] == ["tables/t.md"]


def test_annotation_candidates_come_from_confirmed_fails_only():
    qs = [_q(0, sql="G0"), _q(1, sql="G1")]
    rows = {"G0": [{"c": "0"}], "G1": [{"c": "1"}],
            "A0": [{"c": "9"}], "A1": [{"c": "9"}]}

    async def judge(cases, on_progress=None):
        out = []
        for c in cases:
            if c.q_id == 0:
                out.append(JudgeVerdict(q_id=0, check=c.check, verdict=VERDICT_FAIL,
                                        comment="c", annotation="state X in the docs"))
            else:
                out.append(JudgeVerdict(q_id=1, check=c.check, verdict=VERDICT_PASS,
                                        comment="bad gold", annotation="should not appear"))
        return out

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=1, questions=qs,
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "A0",
                                     (CHECK_SQL, "Q1"): "A1"}),
        grader=Grader(_fake_execute(rows)), judge=judge,
    )
    anns = report["annotations"]
    assert anns["status"] == "idle" and anns["final"] == []
    assert anns["candidates"] == [
        {"q_id": 0, "check": CHECK_SQL, "annotation": "state X in the docs"}
    ]


def test_progress_phases_carry_check_and_run():
    q = _q(0, behavior="Should refuse.")
    ticks = []

    def progress(phase, check, run_index, current, total):
        ticks.append((phase, check, run_index, current, total))

    _run(
        report_id="r1", checks=[CHECK_BEHAVIOR], runs=2, questions=[q],
        make_solve=_scripted_solver({(CHECK_BEHAVIOR, "Q0"): "no"}),
        grader=None, judge=_judge_all_fail,
        grade_behavior=_scripted_behavior_grader(),
        review_behavior=_scripted_behavior_reviewer(), progress=progress,
    )
    phases = {t[0] for t in ticks}
    # Behavior grading ticks as "grading" (per attempt, after all runs); no
    # failure-review phase runs when every pair is judge-graded.
    assert phases == {"solving", "grading"}
    solving_runs = {t[2] for t in ticks if t[0] == "solving"}
    assert solving_runs == {0, 1}
    assert all(t[1] == CHECK_BEHAVIOR for t in ticks)
    # Every phase announces itself with a START tick (0, total) so the live
    # line flips the moment the phase begins.
    assert ("solving", CHECK_BEHAVIOR, 0, 0, 1) == ticks[0]
    # The grading pass covers every attempt across the runs (2 here) and is a
    # CROSS-RUN phase: run_index is the -1 sentinel (no run part in the UI),
    # not the last run's number (which mislabeled the live line).
    grading = [t for t in ticks if t[0] == "grading"]
    assert {t[2] for t in grading} == {-1}
    assert grading[0][3:] == (0, 2)  # start tick
    assert grading[-1][3:] == (2, 2)


def test_judge_review_progress_carries_its_check():
    # The accuracy failure review used to tick with check="" — the one phase
    # where the human most wants to know what the judge is judging.
    q = _q(0, sql="G")
    rows = {"G": [{"c": "1"}], "P": [{"c": "2"}]}  # wrong prediction → judged
    ticks = []

    def progress(phase, check, run_index, current, total):
        ticks.append((phase, check, run_index, current, total))

    _run(
        report_id="r1", checks=[CHECK_SQL], runs=1, questions=[q],
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P"}),
        grader=Grader(_fake_execute(rows)),
        judge=_judge_all_fail, progress=progress,
    )
    judging = [t for t in ticks if t[0] == "judging"]
    assert judging, "the failed pair must reach the judge"
    assert all(t[1] == CHECK_SQL for t in judging)
    assert {t[2] for t in judging} == {-1}  # cross-run: no run part
    assert judging[0][3:] == (0, 1)  # start tick


def test_public_surfaces_note_gold_stays_human_facing():
    # The report/traces docs DO carry gold (human-facing artifacts) — but the
    # progress events, the only live surface, never do.
    q = _q(0, sql="SECRET_GOLD")
    rows = {"SECRET_GOLD": [{"c": "1"}], "P": [{"c": "2"}]}
    ticks = []

    def progress(phase, check, run_index, current, total):
        ticks.append((phase, check, run_index, current, total))

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=1, questions=[q],
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P"}),
        grader=Grader(_fake_execute(rows)),
        judge=_judge_all_fail, progress=progress,
    )
    assert "SECRET_GOLD" not in repr(ticks)
    assert "SECRET_GOLD" in repr(report)  # deliberately present, human-only


def test_solver_crash_reason_survives_grading():
    # An engine-level solver exception used to leave prediction=""/trace=None
    # with NO reason — indistinguishable from a solver that answered nothing.
    q = _q(0, sql="G")
    rows = {"G": [{"c": "1"}]}

    def make_solve(spec):
        async def solve(question):
            raise RuntimeError("bedrock exploded")
        return solve

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=1, questions=[q],
        make_solve=make_solve, grader=Grader(_fake_execute(rows)),
        judge=_judge_all_fail,
    )
    attempt = report["questions"][0]["checks"][CHECK_SQL]["attempts"][0]
    assert attempt["outcome"] == "FAIL"
    assert "empty predicted SQL" in attempt["reason"]
    assert "solver error: RuntimeError: bedrock exploded" in attempt["reason"]


def test_solver_crash_reason_survives_behavior_grading():
    q = _q(0, behavior="Should refuse.")

    def make_solve(spec):
        async def solve(question):
            raise TimeoutError("model unreachable")
        return solve

    report, _ = _run(
        report_id="r1", checks=[CHECK_BEHAVIOR], runs=1, questions=[q],
        make_solve=make_solve, grader=None, judge=_judge_all_fail,
        grade_behavior=_scripted_behavior_grader(default=VERDICT_FAIL),
        review_behavior=_scripted_behavior_reviewer(),
    )
    attempt = report["questions"][0]["checks"][CHECK_BEHAVIOR]["attempts"][0]
    assert "comment q0 r0" in attempt["reason"]  # the judge's ruling leads
    assert "solver error: TimeoutError: model unreachable" in attempt["reason"]


def test_zero_graded_check_is_unambiguous_in_report():
    # Every gold broken → all pairs DISCARDED. The score block must say so
    # (graded=0, discarded=n) rather than presenting the raw 0.0 as a measured
    # score — the UI keys off graded.
    qs = [_q(0, sql="BROKEN"), _q(1, sql="BROKEN")]
    rows = {"BROKEN": RuntimeError("COLUMN_NOT_FOUND"), "P": [{"c": "1"}]}
    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=1, questions=qs,
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P", (CHECK_SQL, "Q1"): "P"}),
        grader=Grader(_fake_execute(rows)), judge=_judge_all_fail,
    )
    s = report["scores"][CHECK_SQL]
    assert s["graded"] == 0 and s["discarded"] == 2
    assert s["raw"]["mean"] == 0.0  # the shape is kept; graded=0 disambiguates


def test_bare_string_solver_return_is_tolerated():
    q = _q(0, sql="G")
    rows = {"G": [{"c": "1"}], "P": [{"c": "1"}]}

    def make_solve(spec):
        async def solve(question):
            return "P"  # a bare string, not a SolveResult
        return solve

    report, _ = _run(
        report_id="r1", checks=[CHECK_SQL], runs=1, questions=[q],
        make_solve=make_solve, grader=Grader(_fake_execute(rows)),
        judge=_judge_all_fail,
    )
    assert report["scores"][CHECK_SQL]["raw"]["mean"] == 1.0


def test_passing_attempts_traces_are_persisted_and_openable():
    # ALL attempts keep their traces — a passing run's trace is evidence too.
    q = _q(0, behavior="Should refuse.")
    trace = SolverTrace(turns=2, tool_calls=1, files_read=["tables/t.md"])

    def make_solve(spec):
        async def solve(question):
            return SolveResult(sql="I refuse.", trace=trace)
        return solve

    report, traces = _run(
        report_id="r1", checks=[CHECK_BEHAVIOR], runs=2, questions=[q],
        make_solve=make_solve, grader=None, judge=_judge_all_fail,
        grade_behavior=_scripted_behavior_grader(),
        review_behavior=_scripted_behavior_reviewer(),
    )
    row = report["questions"][0]["checks"][CHECK_BEHAVIOR]
    assert row["passed_runs"] == 2
    assert all(a["has_trace"] for a in row["attempts"])
    assert [(r["q_id"], r["run"]) for r in traces["traces"]] == [(0, 0), (0, 1)]


def test_before_judge_hook_sees_every_attempt_with_behavior_outcomes():
    # The hook fires AFTER behavior grading, so the trace files it lays down
    # for the failure review carry every attempt's TRUE outcome.
    qs = [_q(0, sql="G0"), _q(1, behavior="Should refuse.")]
    rows = {"G0": [{"c": "0"}], "P0": [{"c": "0"}]}
    seen = {}

    def before_judge(attempts):
        seen["pairs"] = sorted(
            (a.q_id, a.check, a.run_index, a.outcome.value) for a in attempts
        )

    _run(
        report_id="r1", checks=[CHECK_SQL, CHECK_BEHAVIOR], runs=2, questions=qs,
        make_solve=_scripted_solver({(CHECK_SQL, "Q0"): "P0",
                                     (CHECK_BEHAVIOR, "Q1"): "I refuse."}),
        grader=Grader(_fake_execute(rows)),
        judge=_judge_all_fail, before_judge=before_judge,
        grade_behavior=_scripted_behavior_grader(),
        review_behavior=_scripted_behavior_reviewer(),
    )
    assert seen["pairs"] == [
        (0, CHECK_SQL, 0, "PASS"), (0, CHECK_SQL, 1, "PASS"),
        (1, CHECK_BEHAVIOR, 0, "PASS"), (1, CHECK_BEHAVIOR, 1, "PASS"),
    ]
