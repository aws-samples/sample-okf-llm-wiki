"""Benchmark completion enforcement: pure policy + the after_model adapter.

The policy is dependency-free and covers every branch offline. The middleware
test builds against REAL langchain (available in this venv even though deepagents
isn't) to prove the jump/message shape and the hook_config wiring.
"""

from __future__ import annotations

import importlib.util
import types

import pytest

from harvest.benchmark.completion import (
    BenchmarkCompletionPolicy,
    _is_final_ai_turn,
)


# -- pure policy --------------------------------------------------------------


def _policy(*, enabled=True, max_iterations=5):
    return BenchmarkCompletionPolicy(enabled=enabled, max_iterations=max_iterations)


def test_disabled_never_enforces():
    p = _policy(enabled=False)
    d = p.decide(is_final_turn=True, rounds_run=0, latest_passed=False)
    assert d.enforce is False and d.reason == "ri-disabled"


def test_mid_flight_turn_is_never_touched():
    # A turn WITH tool calls (is_final_turn False) is the agent still working.
    p = _policy()
    d = p.decide(is_final_turn=False, rounds_run=0, latest_passed=False)
    assert d.enforce is False and d.reason == "not-final-turn"


def test_never_benchmarked_is_compelled():
    p = _policy(max_iterations=5)
    d = p.decide(is_final_turn=True, rounds_run=0, latest_passed=False)
    assert d.enforce is True and d.reason == "incomplete"
    # The zero-round message tells it to run the benchmark at all.
    assert "have not run" in d.message
    assert "run_benchmark" in d.message


def test_target_met_allows_finish():
    # THE "latest run passed → no further runs" requirement.
    p = _policy(max_iterations=5)
    d = p.decide(is_final_turn=True, rounds_run=2, latest_passed=True)
    assert d.enforce is False and d.reason == "target-met"


def test_budget_exhausted_allows_finish():
    # Ran max_iterations times, never passed → forcing more would livelock (guard
    # refuses more run_benchmark anyway). Let it finalize.
    p = _policy(max_iterations=3)
    d = p.decide(is_final_turn=True, rounds_run=3, latest_passed=False)
    assert d.enforce is False and d.reason == "budget-exhausted"


def test_ran_but_not_passed_with_budget_is_compelled():
    p = _policy(max_iterations=5)
    d = p.decide(is_final_turn=True, rounds_run=2, latest_passed=False)
    assert d.enforce is True and d.reason == "incomplete"
    # The mid-loop message references remaining budget, not "haven't run".
    assert "did NOT meet the target" in d.message
    assert "3 of 5" in d.message  # remaining of max


def test_stuck_reprompts_stop_enforcing():
    # Agent keeps trying to end WITHOUT running a new round (rounds_run frozen).
    # After _MAX_STUCK_REPROMPTS consecutive nudges we cease, so we don't spin.
    p = _policy(max_iterations=5)
    reasons = [
        p.decide(is_final_turn=True, rounds_run=1, latest_passed=False).reason
        for _ in range(6)
    ]
    assert "incomplete" in reasons
    assert reasons[-1] == "reprompt-cap"  # eventually gives up


def test_progress_resets_stuck_counter():
    # If the agent DOES run a new round between nudges, we keep helping it (the
    # stuck counter resets), rather than prematurely capping a productive loop.
    p = _policy(max_iterations=10)
    assert p.decide(is_final_turn=True, rounds_run=1, latest_passed=False).enforce
    assert p.decide(is_final_turn=True, rounds_run=1, latest_passed=False).enforce  # stuck=1
    # A new round completed → progress; counter resets, still compelling.
    d = p.decide(is_final_turn=True, rounds_run=2, latest_passed=False)
    assert d.enforce is True and d.reason == "incomplete"


# -- _is_final_ai_turn helper -------------------------------------------------


def _ai(tool_calls=None):
    m = types.SimpleNamespace(type="ai", content="")
    if tool_calls is not None:
        m.tool_calls = tool_calls
    return m


def test_final_turn_detection():
    assert _is_final_ai_turn(_ai(tool_calls=None)) is True  # no tool calls → ending
    assert _is_final_ai_turn(_ai(tool_calls=[])) is True
    assert _is_final_ai_turn(_ai(tool_calls=[{"name": "run_benchmark"}])) is False
    assert _is_final_ai_turn(types.SimpleNamespace(type="human")) is False
    assert _is_final_ai_turn(None) is False


# -- middleware adapter (real langchain) --------------------------------------

_HAVE_LANGCHAIN = importlib.util.find_spec("langchain.agents.middleware") is not None


@pytest.mark.skipif(not _HAVE_LANGCHAIN, reason="langchain not installed here")
def test_middleware_injects_jump_and_message_when_incomplete():
    from langchain.agents.middleware import AgentMiddleware

    from harvest.benchmark.completion import BenchmarkCompletionMiddleware

    # A session with one FAILED round and budget remaining.
    round0 = types.SimpleNamespace(target_met=False)
    session = types.SimpleNamespace(rounds=[round0])
    mw = BenchmarkCompletionMiddleware(_policy(max_iterations=5), session)

    # after_model is genuinely overridden and declares the model jump edge.
    assert BenchmarkCompletionMiddleware.after_model is not AgentMiddleware.after_model
    assert getattr(mw.after_model, "__can_jump_to__", None) == ["model"]

    state = {"messages": [_ai(tool_calls=None)]}  # agent trying to END
    out = mw.after_model(state, runtime=None)
    assert out is not None
    assert out["jump_to"] == "model"
    assert len(out["messages"]) == 1
    assert out["messages"][0].type == "human"
    assert "did NOT meet the target" in out["messages"][0].content


@pytest.mark.skipif(not _HAVE_LANGCHAIN, reason="langchain not installed here")
def test_middleware_lets_agent_finish_when_target_met():
    from harvest.benchmark.completion import BenchmarkCompletionMiddleware

    session = types.SimpleNamespace(rounds=[types.SimpleNamespace(target_met=True)])
    mw = BenchmarkCompletionMiddleware(_policy(max_iterations=5), session)
    out = mw.after_model({"messages": [_ai(tool_calls=None)]}, runtime=None)
    assert out is None  # no interference — the run may end


@pytest.mark.skipif(not _HAVE_LANGCHAIN, reason="langchain not installed here")
def test_middleware_ignores_mid_flight_tool_turn():
    from harvest.benchmark.completion import BenchmarkCompletionMiddleware

    session = types.SimpleNamespace(rounds=[])
    mw = BenchmarkCompletionMiddleware(_policy(max_iterations=5), session)
    # A turn WITH a tool call — the agent is still working; do not interfere.
    state = {"messages": [_ai(tool_calls=[{"name": "run_benchmark"}])]}
    assert mw.after_model(state, runtime=None) is None
