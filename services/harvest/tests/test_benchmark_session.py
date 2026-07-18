"""BenchmarkSession: per-round advance, KPI persistence, best-round selection."""

from __future__ import annotations

import tempfile
from pathlib import Path

from harvest.benchmark.grader import Grader
from harvest.benchmark.questions import BenchmarkQuestion
from harvest.benchmark.runner import BenchmarkSession
from harvest.benchmark.tool import AdjudicationResult
from okf_core import recursive_improvement as ri


def _bundle(root: Path):
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "t.md").write_text("# t")
    (root / ".metadata").mkdir()
    (root / ".metadata" / "columns.tsv").write_text("SECRET")


def _cfg():
    return ri.validate({"enabled": True, "questions_key": "k", "gate_kpis": ["ex"]})


def _fake_execute(rows):
    def execute(sql):
        resp = rows[sql]
        if isinstance(resp, Exception):
            raise resp
        return resp
    return execute


async def _adjudicate(fails):
    return AdjudicationResult(genuine_error_count=len(fails))


def _session(tmp, *, predicted_by_round, persisted, events):
    """Build a session whose solver returns SQL keyed by (round, question)."""
    qs = [BenchmarkQuestion(q_id=i, question=f"Q{i}", gold_sql=f"G{i}") for i in range(2)]
    rows = {"G0": [{"c": "0"}], "G1": [{"c": "1"}], "P_right0": [{"c": "0"}],
            "P_right1": [{"c": "1"}], "P_wrong": [{"c": "z"}]}
    grader = Grader(_fake_execute(rows))
    calls = {"round": 0}

    def make_solver(snap_dir):
        # Assert the snapshot is bundle-only (no .metadata leaked).
        assert not (Path(snap_dir) / ".metadata").exists()
        this_round = calls["round"]
        calls["round"] += 1

        async def solve(question):
            return predicted_by_round[this_round][question]
        return solve

    return BenchmarkSession(
        data_domain="d", dataset="ds", dataset_root=str(tmp),
        runtime_session_id="sess-1", config=_cfg(), questions=qs,
        make_solver=make_solver, grader=grader, adjudicate=_adjudicate,
        persist_kpi=lambda it, attrs: persisted.append((it, attrs)),
        emit_event=lambda ev: events.append(ev),
        concurrency=4,
    )


def test_round_advances_and_persists():
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        _bundle(Path(tmp))
        persisted, events = [], []
        sess = _session(
            tmp,
            predicted_by_round={
                0: {"Q0": "P_right0", "Q1": "P_wrong"},   # 1/2
                1: {"Q0": "P_right0", "Q1": "P_right1"},   # 2/2
            },
            persisted=persisted, events=events,
        )
        r0 = asyncio.run(sess.run_next())
        r1 = asyncio.run(sess.run_next())

        assert r0["iteration"] == 0 and r0["ex_score"] == 0.5
        assert r1["iteration"] == 1 and r1["ex_score"] == 1.0
        # KPI persisted + live event emitted per round.
        assert [it for it, _ in persisted] == [0, 1]
        assert [e["kind"] for e in events] == ["benchmark", "benchmark"]
        # Public dict carries no gold/question text.
        assert "G0" not in repr(r0) and "Q0" not in repr(r0)


def test_best_round_picks_highest_ex_earliest_on_tie():
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        _bundle(Path(tmp))
        persisted, events = [], []
        sess = _session(
            tmp,
            predicted_by_round={
                0: {"Q0": "P_right0", "Q1": "P_right1"},   # 1.0
                1: {"Q0": "P_right0", "Q1": "P_wrong"},    # 0.5 (regression)
                2: {"Q0": "P_right0", "Q1": "P_right1"},   # 1.0 again
            },
            persisted=persisted, events=events,
        )
        for _ in range(3):
            asyncio.run(sess.run_next())
        best = sess.best_round()
        assert best.ex_score == 1.0
        assert best.iteration == 0  # earliest of the tied-best rounds


def test_threshold_met_flag_in_public_dict():
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        _bundle(Path(tmp))
        persisted, events = [], []
        sess = _session(
            tmp,
            predicted_by_round={0: {"Q0": "P_right0", "Q1": "P_right1"}},
            persisted=persisted, events=events,
        )
        r = asyncio.run(sess.run_next())
        # ex=1.0 >= default 0.8 threshold, gate=["ex"] → met.
        assert r["threshold_met"] is True
