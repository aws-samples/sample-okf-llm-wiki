"""End-to-end regression: drive a REAL deepagents graph through the REAL
StepEmitter and assert the feed contains ONLY the top-level supervisor's steps —
never a sub-agent's internal model turns or tool calls.

This is the test that would have caught the recurring "I see agent and tool calls
from sub-agents" bug. The unit tests in test_steps.py feed synthetic metadata to
the emitter; this test feeds the emitter the ACTUAL callback stream langchain +
deepagents produce, so it locks in the real contract:

  * LangChain passes ``metadata`` (with the nested ``langgraph_checkpoint_ns``)
    only to the START callbacks; the END callbacks get an empty metadata dict.
    So the sub-agent discriminator must be evaluated at start and paired to the
    end by ``run_id`` — which is precisely what StepEmitter does.

Skipped automatically if deepagents/langchain aren't importable (a leaner test
env), so it never breaks the offline suite.
"""

from __future__ import annotations

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("langchain")

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from harvest.steps import KIND_AGENT, KIND_TOOL_CALL, KIND_TOOL_RESULT, StepEmitter


@tool
def peek(x: str) -> str:
    """A trivial tool the model can call; echoes its input."""
    return f"peeked:{x}"


class _ScriptedToolModel(BaseChatModel):
    """A minimal tool-calling fake model: yields scripted AIMessages in call
    order and supports ``bind_tools`` (which ``create_agent`` requires). The
    single shared cursor advances across the whole graph — supervisor and
    sub-agent turns alike — mirroring one real model serving every node."""

    script: list = []
    _cursor: dict = {"i": 0}

    @property
    def _llm_type(self) -> str:  # pragma: no cover - identity only
        return "scripted-tool-model"

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002 - tools encoded in script
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        i = self._cursor["i"]
        msg = (
            self.script[i] if i < len(self.script) else AIMessage(content="(exhausted)")
        )
        self._cursor["i"] = i + 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _model(script: list[AIMessage]) -> _ScriptedToolModel:
    m = _ScriptedToolModel()
    m.script = script
    m._cursor = {"i": 0}
    return m


def test_feed_excludes_subagent_internals_end_to_end():
    from deepagents import create_deep_agent

    # Execution order (one shared model cursor across the whole graph):
    #  1. supervisor -> dispatch the `worker` sub-agent via the `task` tool
    #  2. worker     -> call `peek` (a SUB-AGENT-internal tool call)
    #  3. worker     -> return text  (a SUB-AGENT-internal model turn)
    #  4. supervisor -> call `peek` itself (a TOP-LEVEL tool call)
    #  5. supervisor -> final text (a TOP-LEVEL model turn)
    script = [
        AIMessage(
            content="Dispatching a worker to do the thing.",
            tool_calls=[
                {
                    "name": "task",
                    "args": {"description": "do it", "subagent_type": "worker"},
                    "id": "call_task_1",
                }
            ],
        ),
        AIMessage(
            content="SUBAGENT SHOULD NOT APPEAR — working inside the sub-agent.",
            tool_calls=[
                {"name": "peek", "args": {"x": "inside-sub"}, "id": "call_sub_peek"}
            ],
        ),
        AIMessage(content="SUBAGENT SHOULD NOT APPEAR — sub-agent done."),
        AIMessage(
            content="Supervisor now checking the result.",
            tool_calls=[
                {"name": "peek", "args": {"x": "top-level"}, "id": "call_top_peek"}
            ],
        ),
        AIMessage(content="All done, bundle authored."),
    ]

    worker = {
        "name": "worker",
        "description": "does one thing",
        "system_prompt": "You are a worker.",
        "tools": [peek],
    }
    agent = create_deep_agent(
        model=_model(script),
        tools=[peek],
        system_prompt="You are the supervisor.",
        subagents=[worker],
    )

    events: list[dict] = []
    emitter = StepEmitter(events.append)
    agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        {"callbacks": [emitter], "recursion_limit": 50},
    )

    labels = [e.get("label", "") for e in events]

    # NOTHING from inside the sub-agent leaks — neither its model turns…
    assert not any("SUBAGENT SHOULD NOT APPEAR" in lbl for lbl in labels), labels
    # …nor its internal tool call ("Reading …"/"peek" fired under the nested ns).
    # The only tool_call we keep is the top-level `task` dispatch + the top-level
    # `peek`; the sub-agent's own `peek` must be gone.
    tool_calls = [e for e in events if e["kind"] == KIND_TOOL_CALL]
    tool_labels = [e["label"] for e in tool_calls]
    # Exactly two top-level tool calls survive: the `task` dispatch and the
    # supervisor's own peek. The sub-agent's peek is dropped.
    assert len(tool_calls) == 2, tool_labels
    assert any(lbl.startswith("Started worker") for lbl in tool_labels), tool_labels

    # Every emitted tool_result pairs to an emitted tool_call (no orphan blank
    # rows) — the sub-agent's peek result must have been dropped with its call.
    call_ids = {e["call_id"] for e in tool_calls}
    results = [e for e in events if e["kind"] == KIND_TOOL_RESULT]
    assert results, "expected the top-level tool results to be emitted"
    assert all(r["call_id"] in call_ids for r in results), [
        r["call_id"] for r in results
    ]

    # The supervisor's own decisions DO narrate the feed.
    agent_labels = [e["label"] for e in events if e["kind"] == KIND_AGENT]
    assert any("Dispatching a worker" in lbl for lbl in agent_labels), agent_labels
    assert any("All done" in lbl for lbl in agent_labels), agent_labels


def test_top_level_only_run_narrates_normally():
    """A run with NO sub-agents still emits the supervisor's turns + tools — the
    fix must not over-filter the common case."""
    from deepagents import create_deep_agent

    script = [
        AIMessage(
            content="Let me look something up.",
            tool_calls=[{"name": "peek", "args": {"x": "top"}, "id": "c1"}],
        ),
        AIMessage(content="Found it, done."),
    ]
    agent = create_deep_agent(
        model=_model(script),
        tools=[peek],
        system_prompt="You are the supervisor.",
    )
    events: list[dict] = []
    agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        {"callbacks": [StepEmitter(events.append)], "recursion_limit": 50},
    )
    kinds = [e["kind"] for e in events]
    assert KIND_AGENT in kinds
    assert KIND_TOOL_CALL in kinds
    assert KIND_TOOL_RESULT in kinds
    agent_labels = [e["label"] for e in events if e["kind"] == KIND_AGENT]
    assert any("Found it" in lbl for lbl in agent_labels), agent_labels
