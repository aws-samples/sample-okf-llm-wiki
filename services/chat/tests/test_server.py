"""Server behavior for the Sparky-style typed-chunk contract:

- identity/isolation helpers (sub decode, thread-id namespacing, scope extract),
- ``process_stream_data``: LangGraph astream part -> typed chunk(s)
  (text / think / tool start / tool result),
- ``stream_run``: a full streamed turn over a REAL ``create_agent`` graph with a
  scripted streaming fake model + moto DynamoDBSaver, proving the typed chunks,
  the per-user namespaced thread id, and the terminal ``end`` marker,
- ``read_history`` / ``delete_history`` over the checkpointer,
- ``stream_run`` surfaces a bad model + an agent exception as an ``error`` chunk
  followed by ``end`` (the client never hangs).
"""

from __future__ import annotations

import asyncio
import json

import jwt
import pytest

from chat import server

from .fakes import CHAT_CATALOG


# --- identity / isolation helpers -------------------------------------------


def test_decode_sub_reads_unverified_claim():
    token = jwt.encode({"sub": "user-abc"}, "k" * 32, algorithm="HS256")
    assert server.decode_sub(f"Bearer {token}") == "user-abc"


def test_decode_sub_missing_header_raises():
    with pytest.raises(server.MissingHeader):
        server.decode_sub(None)


def test_decode_sub_no_sub_claim_raises():
    token = jwt.encode({"email": "x@y.z"}, "k" * 32, algorithm="HS256")
    with pytest.raises(server.MissingHeader):
        server.decode_sub(f"Bearer {token}")


def test_namespaced_thread_id():
    assert server.namespaced_thread_id("alice", "conv1") == "alice:conv1"
    assert server.namespaced_thread_id("bob", "conv1") != server.namespaced_thread_id(
        "alice", "conv1"
    )


@pytest.mark.parametrize(
    "inp,expected",
    [
        ({"dataset_scope": {"data_domain": "d", "dataset": "ds"}}, {"data_domain": "d", "dataset": "ds"}),
        ({"datasetScope": {"data_domain": "d", "dataset": "ds"}}, {"data_domain": "d", "dataset": "ds"}),
        ({"dataset_scope": {"data_domain": "d"}}, None),  # partial
        ({}, None),
        (None, None),
    ],
)
def test_extract_scope(inp, expected):
    assert server.extract_scope(inp) == expected


def test_scoped_prompt_prefixes_when_scoped():
    out = server.scoped_prompt("how many races?", {"data_domain": "bird", "dataset": "formula_1"})
    assert out.startswith("[Scope: the dataset bird/formula_1.")
    assert out.endswith("how many races?")
    # the user's text is preserved verbatim after the preamble
    assert "how many races?" in out


def test_scoped_prompt_noop_when_unscoped():
    assert server.scoped_prompt("hello", None) == "hello"


def test_strip_scope_prefix_roundtrips():
    original = "how many races?"
    scoped = server.scoped_prompt(original, {"data_domain": "bird", "dataset": "formula_1"})
    assert server.strip_scope_prefix(scoped) == original
    # a message with no preamble is unchanged (incl. one that merely mentions scope)
    assert server.strip_scope_prefix("no preamble here") == "no preamble here"
    assert server.strip_scope_prefix("what is the [Scope: x] syntax?") == (
        "what is the [Scope: x] syntax?"
    )


# --- process_stream_data: astream part -> typed chunk(s) --------------------


def _updates_with_tool_call():
    from langchain_core.messages import AIMessage

    msg = AIMessage(
        content="",
        tool_calls=[{"name": "read_page", "args": {"concept_id": "orders"}, "id": "call_1", "type": "tool_call"}],
    )
    return ("updates", {"model": {"messages": [msg]}})


def test_process_stream_data_tool_start_from_updates():
    mode, data = _updates_with_tool_call()
    out = server.process_stream_data(mode, data)
    assert out == [
        {
            "type": "tool",
            "id": "call_1",
            "tool_name": "read_page",
            "tool_start": True,
            "content": {"concept_id": "orders"},
            "error": False,
        }
    ]


def test_tool_start_folds_scope_into_location_tool_args():
    # Scoped conversation: data_domain/dataset are dropped from the model's schema
    # and injected server-side, so the streamed args lack them → the UI showed
    # "undefined/undefined". process_stream_data folds the scope back in.
    mode, data = _updates_with_tool_call()
    scope = {"data_domain": "bird", "dataset": "formula_1"}
    out = server.process_stream_data(mode, data, scope)
    assert out[0]["content"] == {
        "concept_id": "orders",
        "data_domain": "bird",
        "dataset": "formula_1",
    }


def test_tool_start_does_not_fold_scope_into_non_location_tool():
    # list_domains takes no location; scope must NOT be stamped onto it.
    from langchain_core.messages import AIMessage

    msg = AIMessage(
        content="",
        tool_calls=[{"name": "list_domains", "args": {}, "id": "c1", "type": "tool_call"}],
    )
    out = server.process_stream_data(
        "updates", {"model": {"messages": [msg]}},
        {"data_domain": "bird", "dataset": "formula_1"},
    )
    assert out[0]["content"] == {}  # untouched


def test_tool_start_scope_does_not_overwrite_model_supplied_location():
    # If the model DID pass a location (unscoped conversation, or cross-dataset
    # lookup), we never clobber it with the conversation scope.
    from langchain_core.messages import AIMessage

    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_page",
                "args": {"concept_id": "x", "data_domain": "other", "dataset": "ds2"},
                "id": "c1",
                "type": "tool_call",
            }
        ],
    )
    out = server.process_stream_data(
        "updates", {"model": {"messages": [msg]}},
        {"data_domain": "bird", "dataset": "formula_1"},
    )
    assert out[0]["content"]["data_domain"] == "other"
    assert out[0]["content"]["dataset"] == "ds2"


def test_parse_scope_prefix_roundtrips_with_scoped_prompt():
    scoped = server.scoped_prompt(
        "how many races?", {"data_domain": "bird", "dataset": "formula_1"}
    )
    assert server.parse_scope_prefix(scoped) == {
        "data_domain": "bird",
        "dataset": "formula_1",
    }
    # No prefix → None.
    assert server.parse_scope_prefix("plain question") is None


def test_process_stream_data_text_chunk():
    from langchain_core.messages import AIMessageChunk

    out = server.process_stream_data("messages", (AIMessageChunk(content="Hello"), {}))
    assert out == {"type": "text", "content": "Hello"}


def test_process_stream_data_empty_text_dropped():
    from langchain_core.messages import AIMessageChunk

    assert server.process_stream_data("messages", (AIMessageChunk(content=""), {})) is None


def test_process_stream_data_reasoning_block_is_think():
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(
        content=[{"type": "reasoning_content", "reasoning_content": {"text": "Let me look."}}]
    )
    out = server.process_stream_data("messages", (chunk, {}))
    assert out == {"type": "think", "content": "Let me look."}


def test_process_stream_data_gpt_reasoning_summary_is_think():
    # GPT (Responses v1) reasoning shape: {"type":"reasoning","summary":[{text}]}.
    # The summary items are concatenated into one think chunk.
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(
        content=[
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "First, "},
                    {"type": "summary_text", "text": "consider the joins."},
                ],
            }
        ]
    )
    out = server.process_stream_data("messages", (chunk, {}))
    assert out == {"type": "think", "content": "First, consider the joins."}


def test_process_stream_data_gpt_reasoning_delta_dict_summary():
    # A streaming delta may carry `summary` as a single dict rather than a list.
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(
        content=[{"type": "reasoning", "summary": {"text": "partial"}}]
    )
    out = server.process_stream_data("messages", (chunk, {}))
    assert out == {"type": "think", "content": "partial"}


def test_process_stream_data_structured_text_block():
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(content=[{"type": "text", "text": "answer"}])
    out = server.process_stream_data("messages", (chunk, {}))
    assert out == {"type": "text", "content": "answer"}


def test_process_stream_data_tool_result_parses_json():
    from langchain_core.messages import ToolMessage

    tm = ToolMessage(content='{"matches": []}', name="grep", tool_call_id="call_9")
    out = server.process_stream_data("messages", (tm, {}))
    assert out == {
        "type": "tool",
        "id": "call_9",
        "tool_name": "grep",
        "tool_start": False,
        "content": {"matches": []},
        "error": False,
    }


def test_process_stream_data_tool_result_error_status():
    from langchain_core.messages import ToolMessage

    tm = ToolMessage(content="boom", name="read_page", tool_call_id="c1", status="error")
    out = server.process_stream_data("messages", (tm, {}))
    assert out["error"] is True
    assert out["content"] == "boom"  # non-JSON left raw


# --- a full streamed turn over a real create_agent graph --------------------


def _scripted_graph(checkpointer):
    """A real create_agent graph whose model streams: reasoning -> tool call,
    then (after the tool result) answer text. Proves the whole astream path."""
    from typing import Iterator as _It

    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
    from langchain_core.tools import tool

    @tool
    def read_page(concept_id: str) -> str:
        """Read a wiki concept page."""
        return "# orders\n\n| a | b |\n|---|---|\n| 1 | 2 |"

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="fallback"))])

        def _stream(self, messages, stop=None, run_manager=None, **kw) -> _It[ChatGenerationChunk]:
            has_tool = any(isinstance(m, ToolMessage) for m in messages)
            if not has_tool:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=[{"type": "reasoning_content", "reasoning_content": {"text": "look it up"}}]
                    )
                )
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=[],
                        tool_call_chunks=[
                            {"name": "read_page", "args": '{"concept_id":"orders"}', "id": "call_1", "index": 0}
                        ],
                    )
                )
            else:
                for tok in ["Here ", "is ", "the answer."]:
                    yield ChatGenerationChunk(message=AIMessageChunk(content=tok))

    return create_agent(model=ScriptedModel(), tools=[read_page], checkpointer=checkpointer)


class _StubConfig:
    """Minimal ChatConfig stand-in: validates (model, effort) against CHAT_CATALOG."""

    def resolve_model_effort(self, model, effort):
        from okf_core.harvest_models import validate_model_effort

        return validate_model_effort(CHAT_CATALOG, model or "us.anthropic.claude-opus-4-8", effort)


async def _collect(agen):
    return [chunk async for chunk in agen]


def _chunks(frames):
    """Parse SSE `data: {json}\\n\\n` frames back into chunk dicts."""
    out = []
    for f in frames:
        assert f.startswith("data: ")
        out.append(json.loads(f[len("data: ") :].strip()))
    return out


def test_stream_run_emits_typed_chunks_and_end_marker():
    from langgraph.checkpoint.memory import InMemorySaver

    cp = InMemorySaver()
    seen = {}

    def build_agent(model, effort, scope, checkpointer, features=None):
        seen["model"], seen["effort"], seen["scope"] = model, effort, scope
        seen["features"] = features
        return _scripted_graph(checkpointer)

    frames = asyncio.run(
        _collect(
            server.stream_run(
                {"type": "send", "prompt": "list orders", "model_id": "us.anthropic.claude-opus-4-8", "effort": "high"},
                "alice",
                "conv-123",
                chat_config=_StubConfig(),
                build_agent=build_agent,
                checkpointer=cp,
            )
        )
    )
    chunks = _chunks(frames)
    types = [c.get("type") or ("end" if c.get("end") else None) for c in chunks]

    # reasoning, a tool start, a tool result, answer text, then a terminal end.
    assert "think" in types
    assert any(c.get("type") == "tool" and c.get("tool_start") for c in chunks)
    assert any(c.get("type") == "tool" and c.get("tool_start") is False for c in chunks)
    assert any(c.get("type") == "text" for c in chunks)
    assert chunks[-1].get("end") is True

    # tool start carries the parsed args; result carries the page body.
    start = next(c for c in chunks if c.get("type") == "tool" and c.get("tool_start"))
    assert start["tool_name"] == "read_page"
    assert start["content"] == {"concept_id": "orders"}
    result = next(c for c in chunks if c.get("type") == "tool" and c.get("tool_start") is False)
    assert "orders" in json.dumps(result["content"])

    # answer text assembled in order
    text = "".join(c["content"] for c in chunks if c.get("type") == "text")
    assert "the answer." in text


def test_stream_run_writes_index_row_after_validation():
    from langgraph.checkpoint.memory import InMemorySaver

    sink = []

    def index_writer(**kw):
        sink.append(kw)

    frames = asyncio.run(
        _collect(
            server.stream_run(
                {
                    "type": "send",
                    "prompt": "hello wiki",
                    "model_id": "us.anthropic.claude-opus-4-8",
                    "effort": "high",
                    "dataset_scope": {"data_domain": "sales", "dataset": "orders"},
                },
                "alice",
                "conv-idx",
                chat_config=_StubConfig(),
                build_agent=lambda *a, **k: _scripted_graph(InMemorySaver()),
                checkpointer=InMemorySaver(),
                index_writer=index_writer,
            )
        )
    )
    assert len(sink) == 1
    row = sink[0]
    assert row["user_sub"] == "alice"
    assert row["thread_id"] == "conv-idx"
    assert row["title"] == "hello wiki"
    assert row["model"] == "us.anthropic.claude-opus-4-8"
    assert row["dataset_scope"] == {"data_domain": "sales", "dataset": "orders"}


def test_stream_run_empty_prompt_never_invokes_model():
    # An empty/whitespace prompt reaching the send path must NOT build an agent or
    # invoke the model — it just emits a clean end. (This is what produced the
    # phantom "accidental send" replies when a resume request fell through.)
    def build_agent(*a, **k):
        raise AssertionError("empty prompt must not build/run the agent")

    for bad in ("", "   ", "\n\n"):
        frames = asyncio.run(
            _collect(
                server.stream_run(
                    {"type": "send", "prompt": bad, "model_id": "us.anthropic.claude-opus-4-8", "effort": "high"},
                    "alice",
                    "conv-empty",
                    chat_config=_StubConfig(),
                    build_agent=build_agent,
                    checkpointer=object(),
                )
            )
        )
        chunks = _chunks(frames)
        assert chunks == [{"end": True}]  # only a clean end, nothing else


def test_stream_run_rejects_unknown_model_with_error_chunk():
    def build_agent(*a, **k):
        raise AssertionError("build_agent must not be called for an invalid model")

    frames = asyncio.run(
        _collect(
            server.stream_run(
                {"type": "send", "prompt": "hi", "model_id": "openai.evil-model", "effort": "high"},
                "alice",
                "conv-bad",
                chat_config=_StubConfig(),
                build_agent=build_agent,
                checkpointer=object(),
            )
        )
    )
    chunks = _chunks(frames)
    assert chunks[0]["type"] == "error"
    assert chunks[-1]["end"] is True
    # no index row would have been written (writer is None here) and no agent built


def test_stream_run_surfaces_agent_exception_as_error_then_end():
    class _BoomGraph:
        def astream(self, *a, **k):
            raise RuntimeError("bedrock exploded")

        def get_state(self, cfg):
            return None

    frames = asyncio.run(
        _collect(
            server.stream_run(
                {"type": "send", "prompt": "hi", "model_id": "us.anthropic.claude-opus-4-8", "effort": "high"},
                "alice",
                "conv-boom",
                chat_config=_StubConfig(),
                build_agent=lambda *a, **k: _BoomGraph(),
                checkpointer=object(),
            )
        )
    )
    chunks = _chunks(frames)
    assert any(c.get("type") == "error" and "bedrock exploded" in c["message"] for c in chunks)
    assert chunks[-1]["end"] is True


def test_stop_run_repairs_checkpoint_and_publishes_cancelled_end():
    # A user STOP after a tool call was issued but before its result. The run is a
    # detached registry task; stop_run() cancels it, which fires on_cancel →
    # checkpoint repair + a cancelled end marker published to the buffer. A
    # subscriber (a reconnect / the original) then sees the cancelled chunks.
    from langchain_core.messages import AIMessage, HumanMessage

    from chat import live_streams

    live_streams.reset()

    class _StuckGraph:
        """Streams a tool-start, then blocks forever — until cancelled (the stop)."""

        def __init__(self):
            self.updates = []

        async def astream(self, *a, **k):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {"name": "run_sql", "args": {"sql": "SELECT 1"},
                                     "id": "call_1", "type": "tool_call"}
                                ],
                            )
                        ]
                    }
                },
            )
            await asyncio.Event().wait()  # block until the task is cancelled
            yield ("messages", None)  # never reached

        def get_state(self, cfg):
            class _S:
                values = {
                    "messages": [
                        HumanMessage(content="how many?"),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"name": "run_sql", "args": {"sql": "SELECT 1"},
                                 "id": "call_1", "type": "tool_call"}
                            ],
                        ),
                    ]
                }
                config = {"configurable": {"thread_id": "alice:conv-cancel"}}

            return _S()

        async def aupdate_state(self, cfg, values):
            self.updates.append(values)

    graph = _StuckGraph()

    async def _run():
        # Start the run (subscribe in the background so the runner task advances).
        gen = server.stream_run(
            {"type": "send", "prompt": "how many?",
             "model_id": "us.anthropic.claude-opus-4-8", "effort": "high"},
            "alice",
            "conv-cancel",
            chat_config=_StubConfig(),
            build_agent=lambda *a, **k: graph,
            checkpointer=object(),
        )
        frames: list = []

        async def drain():
            async for f in gen:
                frames.append(f)

        task = asyncio.create_task(drain())
        # Let the run emit the tool-start + block.
        for _ in range(5):
            await asyncio.sleep(0)
        # Explicit stop (the ONLY thing that cancels now).
        result = await server.stop_run("alice", "conv-cancel")
        assert result == {"type": "stop", "stopped": True}
        await task  # the subscriber's stream ends after the cancelled end marker
        # Let the detached checkpoint-repair write land.
        for _ in range(3):
            await asyncio.sleep(0)
        return frames

    frames = asyncio.run(_run())
    chunks = _chunks(frames)

    # tool-start streamed; then a synthetic cancelled tool result + cancelled end.
    assert any(c.get("type") == "tool" and c.get("tool_start") for c in chunks)
    cancelled_tool = [
        c for c in chunks
        if c.get("type") == "tool" and c.get("tool_start") is False and c.get("error")
    ]
    assert cancelled_tool and cancelled_tool[0]["id"] == "call_1"
    assert chunks[-1].get("end") is True
    assert chunks[-1].get("cancelled") is True

    # The checkpoint was repaired: a ToolMessage appended for the dangling call.
    assert len(graph.updates) == 1
    repaired = graph.updates[0]["messages"]
    assert repaired[0].tool_call_id == "call_1"
    assert repaired[0].status == "error"


# --- history read / delete over the checkpointer ----------------------------


def test_read_history_folds_messages_into_turns():
    from langgraph.checkpoint.memory import InMemorySaver

    cp = InMemorySaver()

    def build_agent(model, effort, scope, checkpointer, features=None):
        return _scripted_graph(checkpointer)

    # Run one turn to populate the checkpoint, then read it back.
    asyncio.run(
        _collect(
            server.stream_run(
                {"type": "send", "prompt": "list orders", "model_id": "us.anthropic.claude-opus-4-8", "effort": "high"},
                "alice",
                "conv-h",
                chat_config=_StubConfig(),
                build_agent=build_agent,
                checkpointer=cp,
            )
        )
    )
    data = server.read_history(build_agent, cp, "alice:conv-h")
    turns = data["history"]
    assert len(turns) == 1
    assert turns[0]["userMessage"] == "list orders"
    # the AI events include reasoning, a tool start, a tool result, text, and end.
    ai = turns[0]["aiMessage"]
    assert ai[-1] == {"end": True}
    assert any(e.get("type") == "tool" and e.get("tool_start") for e in ai)
    assert any(e.get("type") == "text" for e in ai)
    # REASONING must survive a history reload (the "reasoning gone on resume" bug):
    # the scripted model emits a reasoning_content block, persisted in the AIMessage
    # content and rebuilt here as a think event.
    assert any(e.get("type") == "think" and e.get("content") for e in ai)


def test_read_history_empty_for_unknown_thread():
    from langgraph.checkpoint.memory import InMemorySaver

    cp = InMemorySaver()
    data = server.read_history(lambda *a, **k: _scripted_graph(cp), cp, "alice:nope")
    assert data == {"history": []}


def test_read_history_surfaces_pending_ask_when_paused():
    # A conversation PAUSED at an ask_human interrupt (durable in the checkpoint)
    # must surface pending_ask on reload so a page refresh re-renders the QA form.
    import types

    class _Intr:
        id = "i1"
        value = {"type": "ask_human", "questions": [{"id": "grain", "prompt": "Which?"}]}

    class _PausedGraph:
        def get_state(self, _cfg):
            return types.SimpleNamespace(
                values={"messages": []}, tasks=[types.SimpleNamespace(interrupts=[_Intr()])]
            )

    data = server.read_history(lambda *a, **k: _PausedGraph(), object(), "alice:paused")
    assert "pending_ask" in data
    assert data["pending_ask"]["type"] == "ask_human"
    assert data["pending_ask"]["interrupt_ids"] == ["i1"]
    assert data["pending_ask"]["questions"][0]["interrupt_id"] == "i1"


def test_read_history_no_pending_ask_when_not_paused():
    from langgraph.checkpoint.memory import InMemorySaver

    cp = InMemorySaver()
    data = server.read_history(lambda *a, **k: _scripted_graph(cp), cp, "alice:nope")
    assert "pending_ask" not in data


def test_delete_history_calls_checkpointer():
    calls = []

    class _CP:
        def delete_thread(self, tid):
            calls.append(tid)

    out = server.delete_history(_CP(), "alice:conv-x")
    assert calls == ["alice:conv-x"]
    assert out["deleted"] is True


# --- resume: replay a live turn without duplicating it ----------------------


def test_resume_prepends_user_message_and_replays_live_buffer():
    from chat import live_streams

    live_streams.reset()

    async def main():
        gate = asyncio.Event()

        async def src():
            yield {"type": "text", "content": "partial answer"}
            await gate.wait()
            yield {"end": True}

        live_streams.start("alice:conv-r", src(), user_message="how many races?")
        await asyncio.sleep(0)  # let the first chunk buffer

        # resume_run leads with the in-flight user message, then the buffered +
        # live chunks.
        frames = []

        async def drain():
            async for f in server.resume_run("alice", "conv-r"):
                frames.append(f)

        task = asyncio.create_task(drain())
        await asyncio.sleep(0)
        gate.set()
        await task
        return frames

    chunks = _chunks(asyncio.run(main()))
    # The in-flight question came first (so the client renders the whole turn)…
    assert chunks[0] == {"type": "user_message", "content": "how many races?"}
    # …then the buffered partial answer, then the live end.
    assert any(c.get("type") == "text" and c["content"] == "partial answer" for c in chunks)
    assert chunks[-1].get("end") is True


def test_resume_inactive_thread_emits_no_active_marker():
    from chat import live_streams

    live_streams.reset()
    chunks = _chunks(asyncio.run(_collect(server.resume_run("alice", "conv-none"))))
    assert chunks[0]["type"] == "no_active_stream"
    assert chunks[-1]["end"] is True


def test_read_history_drops_inflight_half_turn_when_live():
    # With a live run active, get_session_history(drop_inflight=True) must remove a
    # trailing turn whose assistant reply is still empty — resume renders it instead.
    from langchain_core.messages import AIMessage, HumanMessage

    class _Graph:
        def get_state(self, cfg):
            class _S:
                values = {
                    "messages": [
                        HumanMessage(content="q1"),
                        AIMessage(content="a1"),
                        HumanMessage(content="q2-inflight"),  # no assistant reply yet
                    ]
                }

            return _S()

    build_agent = lambda *a, **k: _Graph()  # noqa: E731
    # Without drop: both turns (the in-flight one has only an end sentinel).
    full = server.read_history(build_agent, object(), "alice:c")["history"]
    assert [t["userMessage"] for t in full] == ["q1", "q2-inflight"]
    # With drop: the in-flight half-turn is removed.
    dropped = server.read_history(
        build_agent, object(), "alice:c", drop_inflight=True
    )["history"]
    assert [t["userMessage"] for t in dropped] == ["q1"]


def test_read_history_drops_inflight_turn_stopped_mid_tool():
    # LangGraph checkpoints at each node boundary, so a turn interrupted after the
    # model issued a tool call (but before an answer) has an AIMessage(tool_calls)
    # with NO text. drop_inflight must still drop it (resume replays it in full) —
    # dropping only zero-event turns would keep it AND let resume duplicate it.
    from langchain_core.messages import AIMessage, HumanMessage

    class _Graph:
        def get_state(self, cfg):
            class _S:
                values = {
                    "messages": [
                        HumanMessage(content="q1"),
                        AIMessage(content="a1"),
                        HumanMessage(content="q2-inflight"),
                        # answer not produced yet — only a tool call is checkpointed
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"name": "run_sql", "args": {"sql": "SELECT 1"},
                                 "id": "call_1", "type": "tool_call"}
                            ],
                        ),
                    ]
                }

            return _S()

    build_agent = lambda *a, **k: _Graph()  # noqa: E731
    dropped = server.read_history(
        build_agent, object(), "alice:c", drop_inflight=True
    )["history"]
    assert [t["userMessage"] for t in dropped] == ["q1"]
    # A completed turn (has answer text) is never dropped, even with a tool call.
    full = server.read_history(build_agent, object(), "alice:c")["history"]
    assert [t["userMessage"] for t in full] == ["q1", "q2-inflight"]


# --- optional SQL tool gating (deploy flag AND per-run opt-in) ---------------


class _SqlFactoryConfig:
    """A ChatConfig-ish stub for make_agent_factory: only the fields it reads."""

    def __init__(self, sql_enabled):
        self.sql_enabled = sql_enabled
        self.athena_catalog = "AwsDataCatalog"
        self.athena_output = "s3://x/"
        self.athena_workgroup = "wg"
        self.sql_max_rows = 200


def _factory_tool_names(monkeypatch, *, sql_enabled, features, has_athena=True):
    """Build a graph via make_agent_factory, capturing the tool names it wired.

    Stubs the heavy collaborators (model build, consumption tools, build_graph) so
    the test exercises ONLY the gating logic: does run_sql get added?
    """
    captured = {}

    monkeypatch.setattr(server, "make_agent_factory", server.make_agent_factory)
    # Patch the deferred imports the factory does inside its body.
    import chat.config as chat_config_mod
    import chat.graph as chat_graph_mod
    import chat.tools as chat_tools_mod

    monkeypatch.setattr(chat_config_mod, "build_chat_model", lambda *a, **k: object())
    monkeypatch.setattr(
        chat_tools_mod, "build_consumption_tools", lambda **k: object()
    )

    class _T:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(
        chat_tools_mod,
        "make_agent_tools",
        lambda impl, dataset_scope=None: [_T("read_page"), _T("grep")],
    )

    def _fake_build_graph(model, tools, cp, system_prompt=None, middleware=None):
        captured["tools"] = [t.name for t in tools]
        captured["prompt"] = system_prompt
        captured["middleware"] = [type(m).__name__ for m in (middleware or [])]
        return object()

    monkeypatch.setattr(chat_graph_mod, "build_graph", _fake_build_graph)

    clients = {"s3": object(), "s3vectors": object(), "bedrock_runtime": object(), "ddb": object()}
    if has_athena:
        clients["athena"] = object()

    factory = server.make_agent_factory(_SqlFactoryConfig(sql_enabled), object(), clients)
    factory("us.anthropic.claude-opus-4-8", "high", None, object(), features=features)
    return captured


def test_sql_tool_added_when_enabled_and_opted_in(monkeypatch):
    cap = _factory_tool_names(monkeypatch, sql_enabled=True, features={"sql"})
    assert "run_sql" in cap["tools"]
    # a SQL-aware system prompt is used for the turn
    assert cap["prompt"] is not None and "run_sql" in cap["prompt"]


def test_sql_tool_absent_without_opt_in(monkeypatch):
    cap = _factory_tool_names(monkeypatch, sql_enabled=True, features=set())
    assert "run_sql" not in cap["tools"]
    assert cap["prompt"] is None  # default prompt (no SQL mention)


def test_sql_tool_absent_when_deploy_disabled(monkeypatch):
    # Opted in by the client, but the deploy flag is off (and no athena client) —
    # the tool must NOT be wired (the browser can't self-grant SQL).
    cap = _factory_tool_names(
        monkeypatch, sql_enabled=False, features={"sql"}, has_athena=False
    )
    assert "run_sql" not in cap["tools"]
    assert cap["prompt"] is None


def test_ask_human_tool_and_middleware_always_wired(monkeypatch):
    # ask_human is unconditional (like render_chart), and AskHumanMiddleware — which
    # OWNS the interrupt — must be attached to the graph regardless of features.
    cap = _factory_tool_names(monkeypatch, sql_enabled=False, features=set())
    assert "ask_human" in cap["tools"]
    assert "AskHumanMiddleware" in cap["middleware"]


def test_prompt_caching_middleware_always_wired(monkeypatch):
    # Bedrock prompt caching rides EVERY chat agent (Sparky's setup): the
    # middleware passes cache settings via model_settings and
    # ChatBedrockConverse inserts the cachePoint blocks at request time, so the
    # tool schemas + static system prompt + prior turns become cache reads on
    # every tool-loop iteration. First in the list, before AskHumanMiddleware.
    cap = _factory_tool_names(monkeypatch, sql_enabled=False, features=set())
    assert cap["middleware"][0] == "BedrockPromptCachingMiddleware"
    assert "AskHumanMiddleware" in cap["middleware"]
