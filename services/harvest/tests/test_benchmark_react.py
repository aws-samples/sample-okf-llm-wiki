"""The cached ReAct factory: prompt caching attached, budget semantics kept."""

from __future__ import annotations

import asyncio
import importlib.util
import types

import pytest

from harvest.benchmark.react import is_recursion_limit

_HAVE_LANGCHAIN = (
    importlib.util.find_spec("langchain") is not None
    and importlib.util.find_spec("langchain_aws") is not None
)


def test_is_recursion_limit_matches_by_name():
    class GraphRecursionError(Exception):  # noqa: N818 - mirrors langgraph's name
        pass

    assert is_recursion_limit(GraphRecursionError("Recursion limit of 40 reached"))
    assert not is_recursion_limit(ValueError("Recursion limit of 40 reached"))


@pytest.mark.skipif(
    not _HAVE_LANGCHAIN, reason="langchain/langchain_aws not installed here"
)
def test_factory_attaches_prompt_caching_middleware(monkeypatch):
    # Every benchmark ReAct role must ride BedrockPromptCachingMiddleware — a
    # ReAct loop re-sends its whole conversation each turn, and without cache
    # points a Converse Claude model bills all of it at full input price.
    import langchain.agents as la

    captured: dict = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return "AGENT"

    monkeypatch.setattr(la, "create_agent", fake_create_agent)
    from harvest.benchmark.react import make_react_agent

    agent = make_react_agent("MODEL", ["TOOL"], "PROMPT")
    assert agent == "AGENT"
    assert captured["model"] == "MODEL"
    assert captured["tools"] == ["TOOL"]
    assert captured["system_prompt"] == "PROMPT"
    names = [type(m).__name__ for m in captured["middleware"]]
    assert "BedrockPromptCachingMiddleware" in names


def test_solver_maps_recursion_limit_to_budget_error(monkeypatch):
    # create_agent RAISES GraphRecursionError at the step budget (the prebuilt
    # agent's "Sorry, need more steps" apology is gone). The solver must record
    # the budget error — never grade the partial text as an answer — and KEEP
    # the messages streamed so far (the trace is what makes the failure
    # diagnosable, and the values-stream capture is what preserves it).
    class GraphRecursionError(Exception):
        pass

    class _Agent:
        def astream(self, *_a, **_k):
            async def gen():
                yield {
                    "messages": [
                        types.SimpleNamespace(
                            content="", type="ai", tool_calls=[{"name": "read_me"}]
                        )
                    ]
                }
                raise GraphRecursionError("Recursion limit of 40 reached")

            return gen()

    from harvest.benchmark import solver as solver_mod

    monkeypatch.setattr(
        solver_mod, "make_readonly_file_tools", lambda _root: [], raising=True
    )
    monkeypatch.setattr(solver_mod, "make_read_me_tool", lambda: "TOOL", raising=True)
    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: _Agent()
    )

    events: list[dict] = []
    solve = solver_mod.make_solver(object(), "/nowhere", events.append)
    result = asyncio.run(solve("How many wins?"))

    assert result.sql == ""
    assert len(events) == 1
    assert "step budget" in events[0]["error"]
    # The pre-crash turn survived into the observability event (and the trace).
    assert events[0]["turns"] == 1
    assert events[0]["tool_calls"] == 1


def test_solver_extends_its_toolset_with_extra_tools(monkeypatch):
    # behavior_live_sql hands the Behavior solver run_sql via extra_tools; the
    # grant must land in the agent's toolset alongside the read-only tools.
    captured: dict = {}

    def fake_factory(model, tools, prompt):
        captured["tools"] = tools
        return types.SimpleNamespace()

    from harvest.benchmark import solver as solver_mod

    monkeypatch.setattr(
        solver_mod, "make_readonly_file_tools", lambda _root: ["FILES"], raising=True
    )
    monkeypatch.setattr(
        solver_mod, "make_read_me_tool", lambda: "PRIMER", raising=True
    )
    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", fake_factory
    )
    solver_mod.make_solver(object(), "/nowhere", extra_tools=["RUN_SQL"])
    assert captured["tools"] == ["FILES", "PRIMER", "RUN_SQL"]
    # And without the grant, nothing extra rides along.
    solver_mod.make_solver(object(), "/nowhere")
    assert captured["tools"] == ["FILES", "PRIMER"]


def _msg(type_, content="", tool_calls=None):
    m = types.SimpleNamespace(type=type_, content=content)
    m.tool_calls = tool_calls or []
    return m


@pytest.mark.skipif(
    not _HAVE_LANGCHAIN, reason="langchain/langchain_aws not installed here"
)
def test_nudge_middleware_steers_then_gives_up_after_two():
    # An agent ending without its submit tool gets the nudge (jump back to the
    # model) — but at most TWICE, then it may end (the caller's unparseable
    # path is the backstop; a deaf model must not spin against the recursion
    # limit). The count lives in the MESSAGES, not on self — one agent
    # instance judges many cases concurrently.
    from harvest.benchmark.react import SubmitToolNudgeMiddleware

    mw = SubmitToolNudgeMiddleware("submit_verdict", "NUDGE: call the tool")
    ending = _msg("ai", "here is my verdict in prose")

    out = mw.after_model({"messages": [ending]}, None)
    assert out is not None and out["jump_to"] == "model"
    assert out["messages"][0].content == "NUDGE: call the tool"

    nudge = _msg("human", "NUDGE: call the tool")
    out = mw.after_model({"messages": [ending, nudge, ending]}, None)
    assert out is not None  # second nudge still fires

    out = mw.after_model(
        {"messages": [ending, nudge, ending, nudge, ending]}, None
    )
    assert out is None  # two nudges spent — let it end


@pytest.mark.skipif(
    not _HAVE_LANGCHAIN, reason="langchain/langchain_aws not installed here"
)
def test_nudge_middleware_stays_out_of_the_way():
    from harvest.benchmark.react import SubmitToolNudgeMiddleware

    mw = SubmitToolNudgeMiddleware("submit_verdict", "NUDGE")
    # Mid-flight (the model is calling tools) — never interfere.
    working = _msg("ai", tool_calls=[{"name": "read_file", "args": {}}])
    assert mw.after_model({"messages": [working]}, None) is None
    # Delivered — the tool was called earlier in the conversation.
    submitted = _msg(
        "ai", tool_calls=[{"name": "submit_verdict", "args": {"verdict": "pass"}}]
    )
    ending = _msg("ai", "done")
    assert mw.after_model({"messages": [submitted, ending]}, None) is None
