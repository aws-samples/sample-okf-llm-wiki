"""Benchmark round orchestration: solve→grade→adjudicate, KPIs, gold-free output."""

from __future__ import annotations

import asyncio

from harvest.benchmark.grader import Grader, Outcome, QuestionResult
from harvest.benchmark.questions import BenchmarkQuestion
from harvest.benchmark.tool import AdjudicationResult, run_round
from okf_core import recursive_improvement as ri


def _questions(n):
    return [BenchmarkQuestion(q_id=i, question=f"Q{i}", gold_sql=f"GOLD{i}") for i in range(n)]


def _cfg(**over):
    base = {"enabled": True, "questions_key": "k"}
    base.update(over)
    return ri.validate(base)


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
    # Default: blame every fail on the wiki (genuine), no themes.
    return AdjudicationResult(genuine_error_count=len(fails))


def test_all_pass_meets_threshold():
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
        iteration=0, questions=qs, config=_cfg(ex_threshold=0.8, gate_kpis=["ex"]),
        solve=solve, grader=grader, adjudicate=_noop_adjudicate, concurrency=5,
    )
    assert r.passed == 3 and r.failed == 0 and r.discarded == 0
    assert r.ex_score == 1.0
    assert r.threshold_met is True


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
        iteration=1, questions=qs, config=_cfg(), solve=solve, grader=grader,
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
            improvements=[], genuine_error_count=0, forgiven_count=len(fails)
        )

    r = _run(
        iteration=0, questions=qs, config=_cfg(), solve=solve, grader=grader,
        adjudicate=forgiving, concurrency=5,
    )
    assert r.ex_score == 0.0  # raw EX: both fail
    assert r.judge_accuracy == 1.0  # judge: both positively forgiven
    assert r.genuine_error_count == 0


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
        iteration=0, questions=qs, config=_cfg(), solve=solve, grader=grader,
        adjudicate=broken, concurrency=5,
    )
    assert r.ex_score == 0.0
    assert r.judge_accuracy == 0.0  # NOT 1.0 — nothing was positively forgiven
    assert r.threshold_met is False


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
        )

    r = _run(
        iteration=2, questions=qs, config=_cfg(), solve=solve, grader=grader,
        adjudicate=adjudicate, concurrency=5,
    )
    blob = repr(r.to_public_dict())
    # No gold SQL, no gold values, no question text, no q_id leakage.
    assert "GOLD" not in blob
    assert "Q0" not in blob and "Q1" not in blob
    assert "q_id" not in blob
    # Only the anonymous improvement theme is present.
    assert "status is an int code" in blob


def test_solver_exception_is_a_miss_not_a_crash():
    qs = _questions(2)
    async def solve(question):
        if question == "Q1":
            raise RuntimeError("solver blew up")
        return "P0"
    rows = {"GOLD0": [{"c": "0"}], "GOLD1": [{"c": "1"}], "P0": [{"c": "0"}]}
    grader = Grader(_fake_execute(rows))
    r = _run(
        iteration=0, questions=qs, config=_cfg(), solve=solve, grader=grader,
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
        iteration=0, questions=qs, config=_cfg(), solve=solve, grader=grader,
        adjudicate=_noop_adjudicate, concurrency=4,
    )
    assert active["max"] <= 4  # never more than the semaphore allows


def test_kpi_attrs_shape():
    qs = _questions(1)
    async def solve(question):
        return "P"
    grader = Grader(_fake_execute({"GOLD0": [{"c": "0"}], "P": [{"c": "0"}]}))
    r = _run(
        iteration=3, questions=qs, config=_cfg(), solve=solve, grader=grader,
        adjudicate=_noop_adjudicate, concurrency=5, runtime_session_id="sess-X",
    )
    attrs = r.to_kpi_attrs("sess-X")
    assert attrs["iteration"] == 3
    assert attrs["runtime_session_id"] == "sess-X"
    assert attrs["graded"] == 1
    assert set(attrs) >= {
        "ex_score", "judge_accuracy", "passed", "failed", "discarded",
        "graded", "genuine_error_count", "threshold_met",
    }
