"""The Behavior solver's terminal ``ask_human`` escalation.

The tool CALL is the structural ask signal: it ends the run (return_direct)
and the recorded prediction is the canonical ask rendering — authoritative
over any prose the model emits after it. Offline like every benchmark test.
"""

from __future__ import annotations

import asyncio
import types

from harvest.benchmark.solver import (
    ASKED_PREFIX,
    _extract_ask,
    make_ask_human_tool,
    make_solver,
    render_ask,
)


def _ai(content, tool_calls=None):
    m = types.SimpleNamespace(content=content, type="ai")
    m.tool_calls = tool_calls or []
    return m


def test_extract_ask_reads_the_last_ask_call_structurally():
    messages = [
        _ai("", tool_calls=[{"name": "read_file", "args": {"file_path": "index.md"}}]),
        _ai(
            "",
            tool_calls=[
                {"name": "ask_human", "args": {"questions": ["Which region?", " Which year? "]}}
            ],
        ),
        _ai("Trailing prose the model emitted anyway."),
    ]
    assert _extract_ask(messages) == ["Which region?", "Which year?"]


def test_extract_ask_returns_none_without_a_call():
    # Prose CLAIMING to ask is not an ask — only the tool call counts.
    assert _extract_ask([_ai("I would ask the user which region they mean.")]) is None
    assert _extract_ask([]) is None


def test_render_ask_is_canonical_and_handles_empty():
    out = render_ask(["Which region?", "Which year?"])
    assert out.startswith(ASKED_PREFIX)
    assert "1. Which region?" in out and "2. Which year?" in out
    assert "(no questions given)" in render_ask([])


def test_ask_human_tool_is_terminal_and_gold_blind():
    tool = make_ask_human_tool()
    assert tool.name == "ask_human"
    # return_direct is the hard stop: the ReAct loop ends at the call.
    assert tool.return_direct is True
    # Gold-blind: the description never hints at grading or expectations.
    low = tool.description.lower()
    for leak in ("expectation", "expected_behavior", "grade", "judge", "benchmark"):
        assert leak not in low, leak
    # The result tells a framework that kept going that the run is complete.
    assert "run is complete" in tool.invoke({"questions": ["Which region?"]})


class _FakeAgent:
    def __init__(self, messages):
        self._messages = messages

    async def astream(self, *_a, **_k):
        yield {"messages": self._messages}


def test_solve_records_an_ask_as_the_prediction(monkeypatch, tmp_path):
    # A solve whose messages carry an ask_human call yields the canonical ask
    # rendering as the prediction — regardless of trailing text — while a
    # normal solve still parses the last AI message.
    from harvest.benchmark import react, solver
    from harvest.benchmark.extract import extract_text

    # The offline venv has no deepagents: stub the toolset factories (the fake
    # agent never calls tools anyway) so make_solver builds without it.
    monkeypatch.setattr(solver, "make_readonly_file_tools", lambda *a, **k: [])
    monkeypatch.setattr(solver, "make_read_me_tool", lambda: object())

    asked_messages = [
        _ai("", tool_calls=[{"name": "ask_human", "args": {"questions": ["Which region?"]}}]),
        _ai("rambling after the ask"),
    ]
    monkeypatch.setattr(
        react, "make_react_agent", lambda *a, **k: _FakeAgent(asked_messages)
    )
    solve = make_solver(object(), str(tmp_path), parse=extract_text)
    result = asyncio.run(solve("Q?"))
    assert result.sql.startswith(ASKED_PREFIX)
    assert "1. Which region?" in result.sql

    answered_messages = [_ai("The wiki says 42.")]
    monkeypatch.setattr(
        react, "make_react_agent", lambda *a, **k: _FakeAgent(answered_messages)
    )
    solve = make_solver(object(), str(tmp_path), parse=extract_text)
    result = asyncio.run(solve("Q?"))
    assert result.sql == "The wiki says 42."
