"""Agent-side RI wiring: enablement gating, guard budget, session construction.

deepagents isn't installed in the test venv, so we don't call build_harvest_agent
end to end (it calls create_deep_agent). Instead we exercise the RI decision logic
and the session builder with fakes — create_react_agent (langgraph) IS available,
and the solver's deepagents.backends import is lazy (per round), so
_build_benchmark_session builds without deepagents.
"""

from __future__ import annotations

import types

import harvest.agent as agent
from harvest.benchmark.questions import BenchmarkQuestion
from okf_core import recursive_improvement as ri


class _FakeModel:
    """Stands in for the shared chat_model: only the methods the builders call."""

    def with_structured_output(self, schema):
        return self

    def bind_tools(self, *a, **k):
        return self


def _cfg(**over):
    base = {"enabled": True, "questions_key": "k"}
    base.update(over)
    return ri.validate(base)


def test_disabled_config_yields_no_budget():
    # is_enabled False → the guard gets no benchmark budget (feature inert).
    assert ri.is_enabled(None) is False
    assert ri.is_enabled({"enabled": False}) is False


def test_enabled_config_budget_is_max_iterations():
    cfg = _cfg(max_iterations=3)
    # The budget the guard would receive equals the clamped max_iterations.
    assert cfg[ri.FIELD_MAX_ITERATIONS] == 3


def test_build_benchmark_session_wires_run_identifiers():
    from langchain_core.tools import tool

    @tool
    def run_sql(query: str) -> dict:
        """A stand-in source tool for the adjudicator to bind."""
        return {}

    # Fake source exposing run_query (the grader's execute) + a real source tool.
    source = types.SimpleNamespace(run_query=lambda sql: [])
    questions = [BenchmarkQuestion(0, "Q0", "G0")]
    persisted = []

    session = agent._build_benchmark_session(
        ri_config=_cfg(),
        run={"data_domain": "sport", "dataset": "f1", "runtime_session_id": "sess-Z"},
        questions=questions,
        chat_model=_FakeModel(),
        source=source,
        source_tools=[run_sql],
        dataset_root=agent.Path("/tmp/x"),
        step_emitter=None,
        persist_kpi=lambda it, attrs: persisted.append(it),
    )
    assert session.data_domain == "sport"
    assert session.dataset == "f1"
    assert session.runtime_session_id == "sess-Z"
    assert len(session.questions) == 1
    # No emitter passed → emit_event wired to None (no live events).
    assert session._emit_event is None
    # The grader is wired to the source's run_query.
    assert session._grader is not None
