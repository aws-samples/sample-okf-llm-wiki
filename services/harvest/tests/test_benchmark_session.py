"""BenchmarkSession: per-round advance, KPI persistence, best-round selection."""

from __future__ import annotations

import tempfile
from pathlib import Path

from harvest.benchmark.grader import Grader
from harvest.benchmark.questions import BenchmarkQuestion
from harvest.benchmark.runner import BenchmarkSession
from harvest.benchmark.tool import CATEGORY_GENUINE, AdjudicationResult, Verdict
from okf_core import recursive_improvement as ri


def _bundle(root: Path):
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "t.md").write_text("# t")
    (root / ".metadata").mkdir()
    (root / ".metadata" / "columns.tsv").write_text("SECRET")


def _cfg():
    return ri.validate({"enabled": True, "questions_key": "k"})


def _fake_execute(rows):
    def execute(sql):
        resp = rows[sql]
        if isinstance(resp, Exception):
            raise resp
        return resp
    return execute


async def _adjudicate(fails):
    # Blame every fail on the wiki (genuine) so nothing is pruned by default.
    return AdjudicationResult(
        genuine_error_count=len(fails),
        verdicts=[Verdict(q_id=r.q_id, category=CATEGORY_GENUINE, note="gap") for r in fails],
    )


def _session(tmp, *, predicted_by_round, persisted, events, adjudicate=_adjudicate,
             reviews=None):
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

    def persist_review(iteration, review):
        if reviews is not None:
            reviews.append((iteration, review))

    return BenchmarkSession(
        data_domain="d", dataset="ds", dataset_root=str(tmp),
        runtime_session_id="sess-1", config=_cfg(), questions=qs,
        make_solver=make_solver, grader=grader, adjudicate=adjudicate,
        persist_kpi=lambda it, attrs: persisted.append((it, attrs)),
        persist_review=persist_review,
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
        # KPI persisted per round.
        assert [it for it, _ in persisted] == [0, 1]
        # One round-done "benchmark" KPI event per round...
        done = [e for e in events if e["kind"] == "benchmark"]
        assert len(done) == 2
        # ...plus live "benchmark_progress" ticks (solving/grading phases).
        progress = [e for e in events if e["kind"] == "benchmark_progress"]
        assert progress, "expected progress ticks"
        assert {e["phase"] for e in progress} <= {"solving", "grading", "reviewing"}
        assert all("current" in e and "total" in e for e in progress)
        # Public dict carries no gold/question text.
        assert "G0" not in repr(r0) and "Q0" not in repr(r0)


def test_round_snapshots_are_always_deleted_no_checkpoint_retained():
    # The benchmark keeps NO checkpoint: every round's throwaway snapshot dir is
    # deleted, and the session exposes no best_snapshot/best_round/restore API.
    import asyncio
    import glob as _glob

    with tempfile.TemporaryDirectory() as tmp:
        _bundle(Path(tmp))
        persisted, events = [], []
        sess = _session(
            tmp,
            predicted_by_round={
                0: {"Q0": "P_right0", "Q1": "P_right1"},
                1: {"Q0": "P_right0", "Q1": "P_wrong"},
            },
            persisted=persisted, events=events,
        )
        asyncio.run(sess.run_next())
        asyncio.run(sess.run_next())
        # No leftover per-round snapshot dirs, and no checkpoint/restore surface.
        assert _glob.glob("/tmp/okf-bench-*") == [] or all(
            not Path(p).exists() for p in _glob.glob("/tmp/okf-bench-*")
        )
        assert not hasattr(sess, "best_snapshot")
        assert not hasattr(sess, "best_round")
        assert not hasattr(sess, "restore")
        # The live bundle on the mount is UNTOUCHED by the benchmark.
        assert (Path(tmp) / "tables" / "t.md").read_text() == "# t"


def test_target_met_flag_in_public_dict():
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
        # judge=1.0 (all pass) >= fixed 0.9 target, ex > 0 → met.
        assert r["target_met"] is True
        # The agent-facing dict never carries per-question review or forgiven ids.
        assert "review" not in r and "forgiven_q_ids" not in r


def test_forgiven_questions_pruned_from_next_round():
    # Questions the adjudicator forgives (NOISY_GOLD/AMBIGUOUS) must be dropped from
    # the active set so the NEXT round doesn't re-benchmark them.
    import asyncio

    from harvest.benchmark.tool import CATEGORY_NOISY_GOLD

    with tempfile.TemporaryDirectory() as tmp:
        _bundle(Path(tmp))
        persisted, events = [], []

        async def forgive_q1(fails):
            # Forgive q_id 1 (noisy), leave q_id 0 as a genuine gap.
            v = []
            for r in fails:
                cat = CATEGORY_NOISY_GOLD if r.q_id == 1 else CATEGORY_GENUINE
                v.append(Verdict(q_id=r.q_id, category=cat, note="n"))
            return AdjudicationResult(
                genuine_error_count=sum(1 for x in v if x.category == CATEGORY_GENUINE),
                forgiven_count=sum(1 for x in v if x.category == CATEGORY_NOISY_GOLD),
                verdicts=v,
            )

        sess = _session(
            tmp,
            predicted_by_round={
                0: {"Q0": "P_wrong", "Q1": "P_wrong"},  # both fail round 0
                1: {"Q0": "P_wrong"},                    # only Q0 remains round 1
            },
            persisted=persisted, events=events, adjudicate=forgive_q1,
        )
        asyncio.run(sess.run_next())
        assert {q.q_id for q in sess.questions} == {0}  # Q1 pruned
        r1 = asyncio.run(sess.run_next())
        assert r1["graded"] == 1  # only Q0 benchmarked in round 1


def test_review_is_persisted_per_round():
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        _bundle(Path(tmp))
        persisted, events, reviews = [], [], []
        sess = _session(
            tmp,
            predicted_by_round={0: {"Q0": "P_right0", "Q1": "P_wrong"}},
            persisted=persisted, events=events, reviews=reviews,
        )
        asyncio.run(sess.run_next())
        assert len(reviews) == 1
        it, review = reviews[0]
        assert it == 0
        # Review carries all graded questions with gold — human-facing only.
        assert {rv.q_id for rv in review} == {0, 1}
        assert any(rv.gold_sql for rv in review)
        # The round-done event advertises a review artifact exists.
        done = [e for e in events if e["kind"] == "benchmark"]
        assert done and done[0]["has_review"] is True


def test_has_review_false_when_no_session_id():
    # Without a runtime_session_id the review isn't fetchable (the review URL needs
    # a {session} segment), so has_review must be False even though a review exists.
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        _bundle(Path(tmp))
        events = []
        qs = [BenchmarkQuestion(q_id=0, question="Q0", gold_sql="G0")]
        rows = {"G0": [{"c": "0"}], "P_wrong": [{"c": "z"}]}
        grader = Grader(_fake_execute(rows))

        def make_solver(_snap):
            async def solve(_q):
                return "P_wrong"
            return solve

        sess = BenchmarkSession(
            data_domain="d", dataset="ds", dataset_root=str(tmp),
            runtime_session_id="",  # no session
            config=_cfg(), questions=qs,
            make_solver=make_solver, grader=grader, adjudicate=_adjudicate,
            persist_kpi=lambda *a: None,
            persist_review=lambda *a: None,  # succeeds, but session is blank
            emit_event=lambda ev: events.append(ev),
            concurrency=2,
        )
        asyncio.run(sess.run_next())
        done = [e for e in events if e["kind"] == "benchmark"]
        assert done and done[0]["has_review"] is False
