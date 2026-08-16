"""GuardrailsGateMiddleware — read_page denied until the dataset's guardrails
are read; per-dataset tracking in checkpointed state.

Decision-logic tests drive wrap_tool_call with a minimal request stand-in (the
middleware only reads .tool_call and .state — same approach as the ask_human
tests); the integration test runs a REAL create_agent with an InMemorySaver
and two invocations on one thread, proving the state channel survives a
"resume" (turn 2 sees turn 1's guardrails read).
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chat.guardrails_gate import (
    GUARDRAILS_CONCEPT_ID,
    STATE_KEY,
    GuardrailsGateMiddleware,
    _merge_read,
    guardrails_gate_enabled,
)


class _Req:
    """The slice of ToolCallRequest the middleware reads."""

    def __init__(self, name, args, state=None):
        self.tool_call = {"name": name, "args": args, "id": "call-1"}
        self.state = state or {}


def _handler_spy():
    calls = []

    def handler(request):
        calls.append(request)
        return ToolMessage(
            content=json.dumps({"content": "# page"}),
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
        )

    return calls, handler


def _args(concept_id, domain="sales", dataset="orders"):
    return {"concept_id": concept_id, "data_domain": domain, "dataset": dataset}


# -- decision logic -------------------------------------------------------------


def test_read_page_is_denied_until_guardrails_read():
    mw = GuardrailsGateMiddleware()
    calls, handler = _handler_spy()
    out = mw.wrap_tool_call(_Req("read_page", _args("tables/orders")), handler)
    assert calls == []  # the tool never ran
    assert isinstance(out, ToolMessage) and out.status == "error"
    payload = json.loads(out.content)
    assert payload["status"] == "denied"
    # The denial teaches the fix: the exact guardrails call to make.
    assert GUARDRAILS_CONCEPT_ID in payload["error"]
    assert "sales" in payload["error"] and "orders" in payload["error"]


def test_guardrails_read_always_passes_and_marks_the_dataset():
    """No deadlock: reading the guardrails page itself is never denied, and
    completing it returns a Command carrying both the tool result and the
    state mark."""
    from langgraph.types import Command

    mw = GuardrailsGateMiddleware()
    calls, handler = _handler_spy()
    out = mw.wrap_tool_call(
        _Req("read_page", _args(GUARDRAILS_CONCEPT_ID)), handler
    )
    assert len(calls) == 1  # the real tool ran
    assert isinstance(out, Command)
    assert out.update[STATE_KEY] == {"sales/orders": True}
    (msg,) = out.update["messages"]
    assert isinstance(msg, ToolMessage) and msg.tool_call_id == "call-1"


def test_md_suffix_and_slashes_still_count_as_the_guardrails_page():
    from langgraph.types import Command

    mw = GuardrailsGateMiddleware()
    _, handler = _handler_spy()
    out = mw.wrap_tool_call(
        _Req("read_page", _args(f"/{GUARDRAILS_CONCEPT_ID}.md")), handler
    )
    assert isinstance(out, Command)


def test_failed_guardrails_read_still_marks_no_permanent_lockout():
    """A dataset without a guardrails page (legacy bundle) must not become
    unreadable: the attempt satisfies the gate even when the tool errors."""
    from langgraph.types import Command

    mw = GuardrailsGateMiddleware()

    def failing_handler(request):
        return ToolMessage(
            content="Error: NoSuchKey",
            tool_call_id=request.tool_call["id"],
            name="read_page",
        )

    out = mw.wrap_tool_call(
        _Req("read_page", _args(GUARDRAILS_CONCEPT_ID)), failing_handler
    )
    assert isinstance(out, Command)
    assert out.update[STATE_KEY] == {"sales/orders": True}


def test_read_allowed_once_state_carries_the_mark():
    mw = GuardrailsGateMiddleware()
    calls, handler = _handler_spy()
    state = {STATE_KEY: {"sales/orders": True}}
    out = mw.wrap_tool_call(
        _Req("read_page", _args("tables/orders"), state=state), handler
    )
    assert len(calls) == 1
    assert isinstance(out, ToolMessage) and out.status != "error"


def test_tracking_is_per_dataset():
    mw = GuardrailsGateMiddleware()
    _, handler = _handler_spy()
    state = {STATE_KEY: {"sales/orders": True}}
    out = mw.wrap_tool_call(
        _Req("read_page", _args("tables/races", dataset="f1"), state=state),
        handler,
    )
    assert isinstance(out, ToolMessage) and out.status == "error"


def test_browse_and_search_tools_are_never_gated():
    mw = GuardrailsGateMiddleware()
    for name in ("list_directory", "glob", "grep", "semantic_search",
                 "get_backlinks", "list_domains", "run_sql"):
        calls, handler = _handler_spy()
        out = mw.wrap_tool_call(_Req(name, _args("tables/orders")), handler)
        assert len(calls) == 1, name
        assert isinstance(out, ToolMessage) and out.status != "error"


def test_scoped_conversation_falls_back_to_the_construction_scope():
    """@-scoped runs drop data_domain/dataset from the tool args (injected in
    the tool wrapper) — the middleware must gate via the scope it was built
    with."""
    mw = GuardrailsGateMiddleware(scope={"data_domain": "sales", "dataset": "orders"})
    calls, handler = _handler_spy()
    out = mw.wrap_tool_call(
        _Req("read_page", {"concept_id": "tables/orders"}), handler
    )
    assert calls == [] and out.status == "error"
    # And the guardrails read under scope marks the SCOPED dataset.
    from langgraph.types import Command

    out = mw.wrap_tool_call(
        _Req("read_page", {"concept_id": GUARDRAILS_CONCEPT_ID}), handler
    )
    assert isinstance(out, Command)
    assert out.update[STATE_KEY] == {"sales/orders": True}


def test_unattributable_read_passes_through():
    """No args and no scope: gating on a guess would deny the wrong thing —
    the tool itself rejects the malformed call."""
    mw = GuardrailsGateMiddleware()
    calls, handler = _handler_spy()
    out = mw.wrap_tool_call(_Req("read_page", {"concept_id": "tables/x"}), handler)
    assert len(calls) == 1
    assert isinstance(out, ToolMessage)


def test_async_path_matches_sync():
    mw = GuardrailsGateMiddleware()

    async def handler(request):
        return ToolMessage(
            content="ok", tool_call_id=request.tool_call["id"], name="read_page"
        )

    denied = asyncio.run(
        mw.awrap_tool_call(_Req("read_page", _args("tables/orders")), handler)
    )
    assert denied.status == "error"
    from langgraph.types import Command

    marked = asyncio.run(
        mw.awrap_tool_call(_Req("read_page", _args(GUARDRAILS_CONCEPT_ID)), handler)
    )
    assert isinstance(marked, Command)


def test_reducer_merges_parallel_marks():
    """Two guardrails reads for different datasets in ONE parallel tool batch
    each update the channel in the same superstep — the reducer unions them
    instead of raising."""
    assert _merge_read({"a/b": True}, {"c/d": True}) == {"a/b": True, "c/d": True}
    assert _merge_read(None, {"a/b": True}) == {"a/b": True}
    assert _merge_read({"a/b": True}, None) == {"a/b": True}


def test_kill_switch_default_on():
    assert guardrails_gate_enabled({}) is True
    assert guardrails_gate_enabled({"OKF_CHAT_GUARDRAILS_GATE_ENABLED": "false"}) is False
    assert guardrails_gate_enabled({"OKF_CHAT_GUARDRAILS_GATE_ENABLED": "0"}) is False
    assert guardrails_gate_enabled({"OKF_CHAT_GUARDRAILS_GATE_ENABLED": "true"}) is True


# -- integration: state survives across turns on one thread ----------------------


def _gated_graph(checkpointer, reads):
    """A real create_agent whose model scripts: at each turn start, read the
    guardrails first IF no read succeeded yet, else read a normal page; after
    any tool result, answer."""
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool

    @tool
    def read_page(concept_id: str, data_domain: str, dataset: str) -> str:
        """Read a wiki concept page."""
        reads.append(concept_id)
        return f"# {concept_id}"

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            tool_results = [m for m in messages if isinstance(m, ToolMessage)]
            if isinstance(messages[-1], HumanMessage):
                # Turn start: first-ever turn reads guardrails, later turns
                # read a regular page (relying on the checkpointed mark).
                concept = (
                    GUARDRAILS_CONCEPT_ID if not tool_results else "tables/orders"
                )
                msg = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_page",
                            "args": _args(concept),
                            "id": f"call-{len(messages)}",
                        }
                    ],
                )
            else:
                msg = AIMessage(content="answered")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    return create_agent(
        model=ScriptedModel(),
        tools=[read_page],
        middleware=[GuardrailsGateMiddleware()],
        checkpointer=checkpointer,
    )


def test_state_survives_across_turns_on_the_same_thread():
    from langgraph.checkpoint.memory import InMemorySaver

    reads: list[str] = []
    cp = InMemorySaver()
    graph = _gated_graph(cp, reads)
    cfg = {"configurable": {"thread_id": "u:t1"}}

    # Turn 1: the guardrails read runs and marks the dataset in state.
    graph.invoke({"messages": [HumanMessage("q1")]}, cfg)
    assert reads == [GUARDRAILS_CONCEPT_ID]
    assert graph.get_state(cfg).values[STATE_KEY] == {"sales/orders": True}

    # Turn 2 (same thread, fresh invoke — a resume): the regular read is
    # ALLOWED because the checkpointer restored the mark.
    graph.invoke({"messages": [HumanMessage("q2")]}, cfg)
    assert reads == [GUARDRAILS_CONCEPT_ID, "tables/orders"]


def test_unread_dataset_is_denied_through_the_real_graph():
    from langgraph.checkpoint.memory import InMemorySaver
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool

    reads: list[str] = []

    @tool
    def read_page(concept_id: str, data_domain: str, dataset: str) -> str:
        """Read a wiki concept page."""
        reads.append(concept_id)
        return "# page"

    class OneShotModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "oneshot"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            if isinstance(messages[-1], HumanMessage):
                msg = AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_page", "args": _args("tables/orders"),
                         "id": "call-x"}
                    ],
                )
            else:
                msg = AIMessage(content="understood")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    graph = create_agent(
        model=OneShotModel(),
        tools=[read_page],
        middleware=[GuardrailsGateMiddleware()],
        checkpointer=InMemorySaver(),
    )
    cfg = {"configurable": {"thread_id": "u:t2"}}
    result = graph.invoke({"messages": [HumanMessage("q")]}, cfg)

    assert reads == []  # the tool never executed
    denials = [
        m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and "denied" in str(m.content)
    ]
    assert len(denials) == 1 and denials[0].status == "error"
