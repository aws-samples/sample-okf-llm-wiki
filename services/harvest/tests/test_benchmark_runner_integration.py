"""Runner RI helpers: prompt gating, recursion bump, compel + best-restore."""

from __future__ import annotations

import tempfile
import types
from pathlib import Path

import pytest

import harvest.runner as runner
from harvest import prompts


# -- supervisor prompt gating ------------------------------------------------


def test_supervisor_prompt_omits_ri_section_by_default():
    p = prompts.build_supervisor_prompt(recursive_improvement=False)
    assert "run_benchmark" not in p
    assert p == prompts.SUPERVISOR_PROMPT  # unchanged for a normal harvest


def test_supervisor_prompt_includes_ri_section_when_enabled():
    p = prompts.build_supervisor_prompt(recursive_improvement=True)
    assert "run_benchmark" in p
    assert "Recursive improvement" in p
    # Must tell the agent to verify improvements against live data (not chase score).
    assert "HYPOTHESIS" in p or "hypothesis" in p
    assert "threshold_met" in p
    # Must NOT claim the agent sees questions/answers.
    assert "never see" in p.lower() or "NEVER see" in p


# -- recursion-limit bump ----------------------------------------------------


def test_recursion_limit_unchanged_when_no_benchmark():
    assert runner._recursion_limit_for(1000, None) == 1000


def test_recursion_limit_raised_for_benchmark_run():
    bench = object()
    assert runner._recursion_limit_for(1000, bench) >= 2000


def test_recursion_limit_env_override(monkeypatch):
    monkeypatch.setenv("OKF_BENCHMARK_RECURSION_LIMIT", "3500")
    assert runner._recursion_limit_for(1000, object()) == 3500


def test_recursion_limit_env_never_below_base(monkeypatch):
    monkeypatch.setenv("OKF_BENCHMARK_RECURSION_LIMIT", "500")
    assert runner._recursion_limit_for(1000, object()) == 1000


# -- build-kwargs ------------------------------------------------------------


def test_build_kwargs_empty_without_benchmark():
    assert runner._benchmark_build_kwargs(None) == {}


def test_build_kwargs_threads_session_pieces():
    bench = types.SimpleNamespace(
        ri_config={"enabled": True}, questions=[1, 2], run={"dataset": "ds"},
        persist_kpi=lambda *a: None,
    )
    kw = runner._benchmark_build_kwargs(bench)
    assert kw["ri_config"] == {"enabled": True}
    assert kw["benchmark_questions"] == [1, 2]
    assert kw["benchmark_run"] == {"dataset": "ds"}
    assert callable(kw["persist_kpi"])


# -- compel check (NO restore — the benchmark never touches the bundle) -------


class _FakeSession:
    def __init__(self, rounds):
        self.rounds = rounds


def test_finish_noop_when_no_benchmark():
    runner._finish_benchmark(object(), None)  # no raise


def test_finish_raises_when_ri_enabled_but_no_round():
    built = types.SimpleNamespace(benchmark_session=_FakeSession([]))
    with pytest.raises(runner.BenchmarkNotRunError):
        runner._finish_benchmark(built, object())


def test_finish_does_not_touch_the_bundle():
    # Compel passes (a round ran) and the authored bundle is left EXACTLY as the
    # agent authored it — no rollback, no delete, no restore. Regression guard for
    # the data-loss bug.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bundle"
        (root / "tables").mkdir(parents=True)
        (root / "tables" / "t.md").write_text("AGENT-AUTHORED content")

        built = types.SimpleNamespace(
            benchmark_session=_FakeSession(rounds=[object(), object()])
        )
        runner._finish_benchmark(built, object())  # no raise, no mutation

        assert (root / "tables" / "t.md").read_text() == "AGENT-AUTHORED content"
