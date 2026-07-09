"""The runner drains the agent stream and surfaces the sub-agent fleet.

Offline: we drive ``_run_agent`` with a fake agent whose ``.stream()`` yields the
same ``(namespace, mode, chunk)`` 3-tuples LangGraph produces under
``stream_mode=["custom"], subgraphs=True`` — the custom stream carrying
langchain_quickjs ``SubagentStreamEvent``s. The UI grows the squares row from
real ``start`` events (there is no reliable pre-start "planned" count), so the
runner just forwards each subagent lifecycle event to the emitter.
"""

from __future__ import annotations

import harvest.runner as runner
from harvest.steps import PHASE_COMPLETE, PHASE_ERROR, PHASE_START


class _RecordingEmitter:
    """Captures every sub-agent lifecycle event the runner hands it."""

    def __init__(self):
        self.events = []

    def emit_subagent_event(self, event):
        self.events.append(
            {
                "phase": event.get("phase"),
                "batch": event.get("eval_id"),
                "sub_id": event.get("id"),
            }
        )


class _StreamAgent:
    """Fake agent whose .stream() replays a scripted list of stream tuples."""

    def __init__(self, items):
        self._items = items
        self.stream_kwargs = None

    def stream(self, _inputs, _config, **kwargs):
        self.stream_kwargs = kwargs
        return iter(self._items)


def test_run_agent_uses_list_stream_mode_and_subgraphs():
    """The stream MUST be driven with a list stream_mode + subgraphs=True (a tuple
    would silently change the yielded shape)."""
    agent = _StreamAgent([])
    runner._run_agent(agent, "prompt", {"recursion_limit": 10}, _RecordingEmitter())
    assert agent.stream_kwargs["stream_mode"] == ["custom"]
    assert isinstance(agent.stream_kwargs["stream_mode"], list)
    assert agent.stream_kwargs["subgraphs"] is True


def test_run_agent_forwards_subagent_lifecycle():
    items = [
        (
            (),
            "custom",
            {
                "type": "subagent",
                "phase": "start",
                "id": "ptc_a",
                "eval_id": "call_1",
                "subagent_type": "reviewer",
                "label": "v races",
            },
        ),
        (
            (),
            "custom",
            {
                "type": "subagent",
                "phase": "start",
                "id": "ptc_b",
                "eval_id": "call_1",
                "subagent_type": "reviewer",
                "label": "v results",
            },
        ),
        (
            (),
            "custom",
            {
                "type": "subagent",
                "phase": "complete",
                "id": "ptc_a",
                "eval_id": "call_1",
            },
        ),
        (
            (),
            "custom",
            {"type": "subagent", "phase": "error", "id": "ptc_b", "eval_id": "call_1"},
        ),
    ]
    em = _RecordingEmitter()
    runner._run_agent(_StreamAgent(items), "prompt", {"recursion_limit": 10}, em)

    starts = [e for e in em.events if e["phase"] == PHASE_START]
    assert {e["sub_id"] for e in starts} == {"ptc_a", "ptc_b"}
    assert all(e["batch"] == "call_1" for e in em.events)
    assert any(
        e["phase"] == PHASE_COMPLETE and e["sub_id"] == "ptc_a" for e in em.events
    )
    assert any(e["phase"] == PHASE_ERROR and e["sub_id"] == "ptc_b" for e in em.events)


def test_run_agent_ignores_non_subagent_custom_events():
    """Other writers can share the custom stream — only type==subagent is a fleet
    event."""
    items = [
        ((), "custom", {"type": "something_else", "data": 1}),
        ((), "custom", {"not": "a dict-with-type"}),
        ((), "updates", {"agent": {"messages": []}}),  # updates ignored now
    ]
    em = _RecordingEmitter()
    runner._run_agent(_StreamAgent(items), "prompt", {"recursion_limit": 10}, em)
    assert em.events == []


def test_run_agent_falls_back_to_invoke_without_emitter():
    """No emitter (steps unavailable) -> plain invoke(), no streaming."""

    class _InvokeAgent:
        def __init__(self):
            self.invoked = False

        def invoke(self, _inputs, _config):
            self.invoked = True
            return {"messages": []}

        def stream(self, *_a, **_k):
            raise AssertionError("must not stream when emitter is None")

    agent = _InvokeAgent()
    runner._run_agent(agent, "prompt", {"recursion_limit": 10}, None)
    assert agent.invoked is True


def test_run_agent_reraises_stream_error():
    class _BoomAgent:
        def stream(self, *_a, **_k):
            raise ValueError("crawl exploded")

    import pytest

    with pytest.raises(ValueError, match="crawl exploded"):
        runner._run_agent(
            _BoomAgent(), "prompt", {"recursion_limit": 10}, _RecordingEmitter()
        )
