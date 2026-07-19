"""Benchmark round orchestration: solve→grade→adjudicate, KPIs, gold-free output."""

from __future__ import annotations

import asyncio

from harvest.benchmark.grader import Grader, Outcome, QuestionResult
from harvest.benchmark.questions import BenchmarkQuestion
from harvest.benchmark.tool import (
    BUCKET_AMBIGUOUS,
    BUCKET_DISCARDED,
    BUCKET_GENUINE,
    BUCKET_NOISY,
    BUCKET_PASSED,
    CATEGORY_AMBIGUOUS,
    CATEGORY_GENUINE,
    CATEGORY_NOISY_GOLD,
    AdjudicationResult,
    Verdict,
    run_round,
)


def _questions(n):
    return [BenchmarkQuestion(q_id=i, question=f"Q{i}", gold_sql=f"GOLD{i}") for i in range(n)]


def _run(**kw):
    return asyncio.run(run_round(**kw))


def _fake_execute(rows_by_sql):
    def execute(sql):
        resp = rows_by_sql[sql]
        if isinstance(resp, Exception):
            raise resp
        return resp
    return execute


async def _noop_adjudicate(fails):
    # Default: blame every fail on the wiki (genuine), no themes. Return one genuine
    # Verdict per fail so the review buckets them correctly.
    return AdjudicationResult(
        genuine_error_count=len(fails),
        verdicts=[Verdict(q_id=r.q_id, category=CATEGORY_GENUINE, note="gap") for r in fails],
    )


def test_all_pass_meets_target():
    qs = _questions(3)
    # solver returns the SQL that matches gold for each question
    async def solve(question):
        idx = int(question[1:])
        return f"PRED{idx}"
    rows = {}
    for i in range(3):
        rows[f"GOLD{i}"] = [{"c": str(i)}]
        rows[f"PRED{i}"] = [{"c": str(i)}]
    grader = Grader(_fake_execute(rows))

    r = _run(
        iteration=0, questions=qs, solve=solve, grader=grader,
        adjudicate=_noop_adjudicate, concurrency=5,
    )
    assert r.passed == 3 and r.failed == 0 and r.discarded == 0
    assert r.ex_score == 1.0
    # judge 1.0 (all pass) >= fixed 0.9 target, ex > 0 → met.
    assert r.target_met is True


def test_discards_excluded_from_ex():
    qs = _questions(3)
    async def solve(question):
        return "PRED"
    rows = {
        "GOLD0": [{"c": "0"}], "GOLD1": RuntimeError("COLUMN_NOT_FOUND"),  # discard
        "GOLD2": [{"c": "2"}],
        "PRED": [{"c": "0"}],  # matches GOLD0 only
    }
    grader = Grader(_fake_execute(rows))
    r = _run(
        iteration=1, questions=qs, solve=solve, grader=grader,
        adjudicate=_noop_adjudicate, concurrency=5,
    )
    assert r.discarded == 1
    # graded = 2 (Q0 pass, Q2 fail); EX = 1/2, not 1/3.
    assert r.passed == 1 and r.failed == 1 and r.graded == 2
    assert r.ex_score == 0.5


def test_judge_accuracy_forgives_noisy_gold():
    qs = _questions(2)
    async def solve(question):
        return "WRONG"
    rows = {"GOLD0": [{"c": "0"}], "GOLD1": [{"c": "1"}], "WRONG": [{"c": "x"}]}
    grader = Grader(_fake_execute(rows))

    # Both FAIL, but the adjudicator POSITIVELY forgives both as noisy gold.
    async def forgiving(fails):
        return AdjudicationResult(
            improvements=[], genuine_error_count=0, forgiven_count=len(fails),
            verdicts=[Verdict(q_id=r.q_id, category=CATEGORY_NOISY_GOLD) for r in fails],
        )

    r = _run(
        iteration=0, questions=qs, solve=solve, grader=grader,
        adjudicate=forgiving, concurrency=5,
    )
    assert r.ex_score == 0.0  # raw EX: both fail
    assert r.judge_accuracy == 1.0  # judge: both positively forgiven
    assert r.genuine_error_count == 0
    # Forgiven questions are surfaced for pruning.
    assert set(r.forgiven_q_ids) == {0, 1}


def test_judge_does_not_forgive_unadjudicated_fails():
    # THE FALSE-SUCCESS FIX at the round level: if the adjudicator forgives NOTHING
    # (e.g. it errored on every failure → forgiven_count 0), judge must equal EX,
    # NOT jump to 100%. Regression guard for the EX 0% / judge 100% screenshot.
    qs = _questions(2)
    async def solve(question):
        return "WRONG"
    rows = {"GOLD0": [{"c": "0"}], "GOLD1": [{"c": "1"}], "WRONG": [{"c": "x"}]}
    grader = Grader(_fake_execute(rows))

    async def broken(fails):  # adjudicator produced no positive verdicts
        return AdjudicationResult(genuine_error_count=0, forgiven_count=0)

    r = _run(
        iteration=0, questions=qs, solve=solve, grader=grader,
        adjudicate=broken, concurrency=5,
    )
    assert r.ex_score == 0.0
    assert r.judge_accuracy == 0.0  # NOT 1.0 — nothing was positively forgiven
    assert r.target_met is False
    assert r.forgiven_q_ids == []


def test_public_dict_is_gold_and_question_free():
    qs = _questions(2)
    async def solve(question):
        return "P"
    rows = {"GOLD0": [{"c": "0"}], "GOLD1": [{"c": "1"}], "P": [{"c": "0"}]}
    grader = Grader(_fake_execute(rows))

    async def adjudicate(fails):
        return AdjudicationResult(
            improvements=["document that status is an int code"],
            genuine_error_count=len(fails),
            verdicts=[Verdict(q_id=r.q_id, category=CATEGORY_GENUINE, note="gap") for r in fails],
        )

    r = _run(
        iteration=2, questions=qs, solve=solve, grader=grader,
        adjudicate=adjudicate, concurrency=5,
    )
    blob = repr(r.to_public_dict())
    # No gold SQL, no gold values, no question text, no q_id leakage.
    assert "GOLD" not in blob
    assert "Q0" not in blob and "Q1" not in blob
    assert "q_id" not in blob
    # The per-question review must NOT be in the agent-facing dict.
    assert "review" not in r.to_public_dict()
    assert "forgiven" not in blob
    # Only the anonymous improvement theme is present.
    assert "status is an int code" in blob


def test_review_covers_all_buckets_with_gold():
    # The human-facing review (NOT agent-facing) carries every question with its
    # bucket + gold + predicted SQL. One PASS, one genuine FAIL, one noisy FAIL,
    # one ambiguous FAIL, one DISCARD.
    qs = _questions(5)
    async def solve(question):
        return {"Q0": "P0", "Q1": "W1", "Q2": "W2", "Q3": "W3", "Q4": "P4"}[question]
    rows = {
        "GOLD0": [{"c": "0"}], "P0": [{"c": "0"}],           # Q0 PASS
        "GOLD1": [{"c": "1"}], "W1": [{"c": "x"}],           # Q1 FAIL→genuine
        "GOLD2": [{"c": "2"}], "W2": [{"c": "y"}],           # Q2 FAIL→noisy
        "GOLD3": [{"c": "3"}], "W3": [{"c": "z"}],           # Q3 FAIL→ambiguous
        "GOLD4": RuntimeError("COLUMN_NOT_FOUND"), "P4": [{"c": "4"}],  # Q4 DISCARD
    }
    grader = Grader(_fake_execute(rows))

    async def adjudicate(fails):
        cat = {1: CATEGORY_GENUINE, 2: CATEGORY_NOISY_GOLD, 3: CATEGORY_AMBIGUOUS}
        return AdjudicationResult(
            genuine_error_count=1, forgiven_count=2,
            verdicts=[Verdict(q_id=r.q_id, category=cat[r.q_id], note="n") for r in fails],
        )

    r = _run(
        iteration=0, questions=qs, solve=solve, grader=grader,
        adjudicate=adjudicate, concurrency=5,
    )
    by_id = {rv.q_id: rv for rv in r.review}
    assert by_id[0].bucket == BUCKET_PASSED
    assert by_id[1].bucket == BUCKET_GENUINE
    assert by_id[2].bucket == BUCKET_NOISY
    assert by_id[3].bucket == BUCKET_AMBIGUOUS
    assert by_id[4].bucket == BUCKET_DISCARDED
    # Gold + predicted SQL present on the human-facing review row.
    assert by_id[1].gold_sql == "GOLD1" and by_id[1].predicted_sql == "W1"
    assert by_id[0].question == "Q0"
    # Only noisy + ambiguous are forgiven for pruning (not genuine, not pass/discard).
    assert set(r.forgiven_q_ids) == {2, 3}


def test_review_note_only_from_bucket_consistent_verdict():
    # A stray verdict for a PASS q_id must NOT attach its note to the passed row —
    # only FAILs are adjudicated, so PASS/DISCARDED carry no note.
    qs = _questions(1)
    async def solve(question):
        return "P0"
    rows = {"GOLD0": [{"c": "0"}], "P0": [{"c": "0"}]}  # Q0 PASSES
    grader = Grader(_fake_execute(rows))

    async def adjudicate(fails):
        # Return a (spurious) verdict for q_id 0 even though it passed + wasn't a fail.
        return AdjudicationResult(
            verdicts=[Verdict(q_id=0, category=CATEGORY_GENUINE, note="stray note")]
        )

    r = _run(
        iteration=0, questions=qs, solve=solve, grader=grader,
        adjudicate=adjudicate, concurrency=2,
    )
    row = r.review[0]
    assert row.bucket == BUCKET_PASSED
    assert row.note == ""  # the stray verdict's note is NOT attached to a PASS


def test_solver_exception_is_a_miss_not_a_crash():
    qs = _questions(2)
    async def solve(question):
        if question == "Q1":
            raise RuntimeError("solver blew up")
        return "P0"
    rows = {"GOLD0": [{"c": "0"}], "GOLD1": [{"c": "1"}], "P0": [{"c": "0"}]}
    grader = Grader(_fake_execute(rows))
    r = _run(
        iteration=0, questions=qs, solve=solve, grader=grader,
        adjudicate=_noop_adjudicate, concurrency=5,
    )
    # Q0 passes; Q1's crashed solver → empty SQL → FAIL, round still completes.
    assert r.passed == 1 and r.failed == 1


def test_concurrency_is_bounded():
    qs = _questions(20)
    active = {"now": 0, "max": 0}

    async def solve(question):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.005)
        active["now"] -= 1
        return "P"

    rows = {f"GOLD{i}": [{"c": "0"}] for i in range(20)}
    rows["P"] = [{"c": "0"}]
    grader = Grader(_fake_execute(rows))
    _run(
        iteration=0, questions=qs, solve=solve, grader=grader,
        adjudicate=_noop_adjudicate, concurrency=4,
    )
    assert active["max"] <= 4  # never more than the semaphore allows


def test_kpi_attrs_shape():
    qs = _questions(1)
    async def solve(question):
        return "P"
    grader = Grader(_fake_execute({"GOLD0": [{"c": "0"}], "P": [{"c": "0"}]}))
    r = _run(
        iteration=3, questions=qs, solve=solve, grader=grader,
        adjudicate=_noop_adjudicate, concurrency=5, runtime_session_id="sess-X",
    )
    attrs = r.to_kpi_attrs("sess-X")
    assert attrs["iteration"] == 3
    assert attrs["runtime_session_id"] == "sess-X"
    assert attrs["graded"] == 1
    assert set(attrs) >= {
        "ex_score", "judge_accuracy", "passed", "failed", "discarded",
        "graded", "genuine_error_count", "target_met",
    }
