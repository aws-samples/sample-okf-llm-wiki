"""Stop-streaming reconciliation: dangling-tool-call detection + checkpoint repair.

The load-bearing property: after a mid-stream stop, a persisted AIMessage with a
tool_call that never got its ToolMessage result is DANGLING — Converse/Responses
reject that history on the next turn. reconcile_cancelled_turn must append a
synthetic error ToolMessage per dangling call and write it back, so the thread
stays resumable. Driven with plain LangChain messages + a fake graph (no AWS).
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chat.cancellation import (
    build_cancellation_tool_messages,
    find_dangling_tool_calls,
    reconcile_cancelled_turn,
)


def _ai_with_tool_calls(*calls):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": n, "args": {}, "id": i, "type": "tool_call"} for (i, n) in calls
        ],
    )


# --- find_dangling_tool_calls -----------------------------------------------


def test_no_dangling_when_all_calls_answered():
    messages = [
        HumanMessage(content="hi"),
        _ai_with_tool_calls(("call_1", "read_page")),
        ToolMessage(tool_call_id="call_1", name="read_page", content="ok"),
        AIMessage(content="done"),
    ]
    assert find_dangling_tool_calls(messages) == []


def test_dangling_when_call_has_no_result():
    # Stopped after the model asked for a tool but before the result came back.
    messages = [
        HumanMessage(content="hi"),
        _ai_with_tool_calls(("call_1", "read_page"), ("call_2", "run_sql")),
        ToolMessage(tool_call_id="call_1", name="read_page", content="ok"),
        # call_2 never answered
    ]
    dangling = find_dangling_tool_calls(messages)
    assert dangling == [{"id": "call_2", "name": "run_sql"}]


def test_dangling_dedups_and_ignores_answered_anywhere():
    messages = [
        _ai_with_tool_calls(("call_1", "grep")),
        _ai_with_tool_calls(("call_1", "grep")),  # same id repeated
        _ai_with_tool_calls(("call_9", "glob")),
        ToolMessage(tool_call_id="call_9", name="glob", content="[]"),
    ]
    # call_1 is dangling (once), call_9 answered.
    assert find_dangling_tool_calls(messages) == [{"id": "call_1", "name": "grep"}]


def test_no_messages_no_dangling():
    assert find_dangling_tool_calls([]) == []


# --- build_cancellation_tool_messages ---------------------------------------


def test_build_cancellation_tool_messages_shape():
    msgs = build_cancellation_tool_messages(
        [{"id": "call_2", "name": "run_sql"}]
    )
    assert len(msgs) == 1
    tm = msgs[0]
    assert isinstance(tm, ToolMessage)
    assert tm.tool_call_id == "call_2"
    assert tm.name == "run_sql"
    assert tm.status == "error"
    assert "cancelled by user" in tm.content.lower()


# --- reconcile_cancelled_turn (fake graph) ----------------------------------


class _State:
    def __init__(self, messages):
        self.values = {"messages": messages}
        self.config = {"configurable": {"thread_id": "alice:c1"}}


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages
        self.updates = []  # captured (cfg, values) from aupdate_state

    def get_state(self, cfg):
        return _State(self._messages)

    async def aupdate_state(self, cfg, values):
        self.updates.append((cfg, values))


async def _reconcile_and_drain(graph, cfg):
    """Call the (sync) reconcile, then yield the loop so the DETACHED write task
    it scheduled gets to run before we assert on it."""
    chunks = reconcile_cancelled_turn(graph, cfg)
    await asyncio.sleep(0)  # let the background _persist() task run
    await asyncio.sleep(0)
    return chunks


def test_reconcile_appends_toolmessage_and_returns_chunks():
    g = _FakeGraph(
        [
            HumanMessage(content="hi"),
            _ai_with_tool_calls(("call_2", "run_sql")),  # dangling
        ]
    )
    cfg = {"configurable": {"thread_id": "alice:c1"}}
    chunks = asyncio.run(_reconcile_and_drain(g, cfg))

    # It persisted a repair (on the background task): one synthetic error
    # ToolMessage for the dangling call.
    assert len(g.updates) == 1
    _, values = g.updates[0]
    repaired = values["messages"]
    assert len(repaired) == 1
    assert isinstance(repaired[0], ToolMessage)
    assert repaired[0].tool_call_id == "call_2"
    assert repaired[0].status == "error"

    # And it returned (synchronously) a typed tool chunk (tool_start False + error).
    assert chunks == [
        {
            "type": "tool",
            "id": "call_2",
            "tool_name": "run_sql",
            "tool_start": False,
            "content": '{"response": "Tool invocation cancelled by user"}',
            "error": True,
        }
    ]


def test_reconcile_noop_when_nothing_dangling():
    g = _FakeGraph(
        [
            _ai_with_tool_calls(("call_1", "read_page")),
            ToolMessage(tool_call_id="call_1", name="read_page", content="ok"),
        ]
    )
    chunks = asyncio.run(_reconcile_and_drain(g, {"configurable": {}}))
    assert chunks == []
    assert g.updates == []  # no write when there's nothing to repair


def test_reconcile_falls_back_to_sync_update_state():
    # A checkpointer/graph exposing only sync update_state must still be repaired.
    class _SyncGraph:
        def __init__(self, messages):
            self._messages = messages
            self.updates = []

        def get_state(self, cfg):
            return _State(self._messages)

        def update_state(self, cfg, values):
            self.updates.append((cfg, values))

    g = _SyncGraph([_ai_with_tool_calls(("call_2", "run_sql"))])
    chunks = asyncio.run(_reconcile_and_drain(g, {"configurable": {}}))
    assert len(g.updates) == 1
    assert len(chunks) == 1


def test_reconcile_swallows_update_failure():
    class _BoomGraph:
        def get_state(self, cfg):
            return _State([_ai_with_tool_calls(("call_2", "run_sql"))])

        async def aupdate_state(self, cfg, values):
            raise RuntimeError("ddb down")

    # A failed repair must not raise (it runs on the detached task in the stop
    # path); reconcile still returns the chunks synchronously.
    chunks = asyncio.run(_reconcile_and_drain(_BoomGraph(), {"configurable": {}}))
    assert len(chunks) == 1


def test_reconcile_handles_unreadable_state():
    class _NoState:
        def get_state(self, cfg):
            raise RuntimeError("no state")

    assert asyncio.run(_reconcile_and_drain(_NoState(), {})) == []
