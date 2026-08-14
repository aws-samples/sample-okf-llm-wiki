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


def test_process_stream_data_tool_pending_from_first_fragment():
    # The FIRST streamed fragment of a tool call carries name+id; the server
    # must announce the call immediately (tool_pending) so the UI can react at
    # invocation start — for render_chart the args ARE the chart code and take
    # seconds to generate, and the args-complete start (updates mode) only
    # lands after that. Later fragments carry name=None -> no re-announce.
    from langchain_core.messages import AIMessageChunk

    first = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": "render_chart", "args": '{"co', "id": "call_1", "index": 0}
        ],
    )
    out = server.process_stream_data("messages", (first, {}))
    assert out == {"type": "tool_pending", "id": "call_1", "tool_name": "render_chart"}

    delta = AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": None, "args": 'de":', "id": None, "index": 0}],
    )
    assert server.process_stream_data("messages", (delta, {})) is None


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


def test_process_stream_data_splits_policy_flag_from_run_sql_result():
    # A run_sql result carrying the query-time policy reminder yields TWO
    # chunks: the tool result (reminder stripped, payload JSON-parsed) and a
    # "policy" chunk holding just the finding lines for the shield step.
    from langchain_core.messages import ToolMessage

    from chat.policy_check import (
        _QUERY_CLOSING,
        _QUERY_SUBJECT,
        compose_policy_reminder,
    )

    reminder = compose_policy_reminder(
        [{"policy_id": "P013", "why": "collapsed the grain.",
          "condition": "a collapse happens", "action": "state the rule",
          "source": "references/known_issues/k.md"}],
        "bird/formula_1",
        subject=_QUERY_SUBJECT,
        closing=_QUERY_CLOSING,
    )
    tm = ToolMessage(
        content='{"rows": []}' + "\n\n" + reminder,
        name="run_sql",
        tool_call_id="c7",
    )
    out = server.process_stream_data("messages", (tm, {}))
    assert isinstance(out, list) and len(out) == 2
    tool, flag = out
    assert tool["type"] == "tool" and tool["content"] == {"rows": []}
    assert flag["type"] == "policy"
    assert flag["content"].startswith("- [P013]")
    assert "Do not mention" not in flag["content"]  # model-facing framing dropped


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

    def build_agent(model, effort, scope, checkpointer, features=None, user_sub="",
                    policy_checker=None):
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

    def build_agent(model, effort, scope, checkpointer, features=None, user_sub="",
                    policy_checker=None):
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


def test_history_renders_steering_as_steer_events_not_user_turns():
    # A steering <system-reminder> (chat.steering) is persisted as a
    # HumanMessage in the checkpoint — it must NOT open a phantom turn or
    # render as a user bubble on reload; it re-emerges as the same "steer"
    # event the live stream emitted (tags stripped), in stream order, so the
    # thinking timeline survives a history reload.
    from langchain_core.messages import AIMessage, HumanMessage

    from chat.steering import STEERING_MARKER

    msgs = [
        HumanMessage(content="real question"),
        AIMessage(content="digging"),
        HumanMessage(
            content="<system-reminder>step back</system-reminder>",
            additional_kwargs={STEERING_MARKER: "silence"},
        ),
        AIMessage(content="the answer"),
    ]
    turns = server._messages_to_turns(msgs)
    assert len(turns) == 1  # the marker message opened no second turn
    assert turns[0]["userMessage"] == "real question"
    events = turns[0]["aiMessage"]
    texts = [e["content"] for e in events if e.get("type") == "text"]
    assert texts == ["digging", "the answer"]
    # The reminder re-emerges as a steer event, between the two AI texts.
    steers = [e for e in events if e.get("type") == "steer"]
    assert [s["content"] for s in steers] == ["step back"]  # tags stripped
    order = [e.get("type") for e in events if e.get("type") in ("text", "steer")]
    assert order == ["text", "steer", "text"]


def test_process_stream_updates_emits_steer_chunks():
    # The SteeringMiddleware's injection surfaces as its node's update carrying
    # a marked HumanMessage — the translator emits a "steer" chunk with the
    # <system-reminder> envelope stripped. Unmarked human messages are ignored.
    from langchain_core.messages import HumanMessage

    from chat.steering import STEERING_MARKER

    data = {
        "SteeringMiddleware.before_model": {
            "messages": [
                HumanMessage(
                    content="<system-reminder>re-read the docs</system-reminder>",
                    additional_kwargs={STEERING_MARKER: "futility"},
                )
            ]
        }
    }
    chunks = server.process_stream_data("updates", data)
    assert chunks == [{"type": "steer", "content": "re-read the docs"}]

    unmarked = {"node": {"messages": [HumanMessage(content="hello")]}}
    assert server.process_stream_data("updates", unmarked) is None


def test_process_stream_updates_emits_policy_chunks_for_behavioural_notes():
    # A BehaviouralPolicyMiddleware injection carries the POLICY marker — it
    # becomes a typed "policy" chunk holding just the finding lines (the same
    # shield step as the query-time split), never a "steer".
    from langchain_core.messages import HumanMessage

    from chat.policy_check import (
        _STEPS_CLOSING,
        _STEPS_SUBJECT,
        POLICY_MARKER,
        compose_policy_reminder,
    )

    note = compose_policy_reminder(
        [{"policy_id": "P002", "why": "computed on an ambiguity.",
          "condition": "an ambiguous term is computed on", "action": "ask first",
          "source": "references/usage_guardrails.md"}],
        "bird/formula_1",
        subject=_STEPS_SUBJECT,
        closing=_STEPS_CLOSING,
    )
    data = {
        "BehaviouralPolicyMiddleware.before_model": {
            "messages": [
                HumanMessage(content=note, additional_kwargs={POLICY_MARKER: "behavioural"})
            ]
        }
    }
    chunks = server.process_stream_data("updates", data)
    assert len(chunks) == 1 and chunks[0]["type"] == "policy"
    assert chunks[0]["content"].startswith("- [P002]")
    assert "Do not mention" not in chunks[0]["content"]


def test_history_renders_policy_notes_as_policy_events_not_user_turns():
    # The behavioural note is a HumanMessage in the checkpoint — it must not
    # open a phantom turn; it re-emerges as the same "policy" event the live
    # stream emitted, so the shield step survives a history reload.
    from langchain_core.messages import AIMessage, HumanMessage

    from chat.policy_check import POLICY_MARKER

    msgs = [
        HumanMessage(content="real question"),
        AIMessage(content="digging"),
        HumanMessage(
            content="<system-reminder>\nAutomated policy screening flagged x:\n"
                    "- [P002] computed on an ambiguity.\nclosing\n</system-reminder>",
            additional_kwargs={POLICY_MARKER: "behavioural"},
        ),
        AIMessage(content="the answer"),
    ]
    turns = server._messages_to_turns(msgs)
    assert len(turns) == 1  # the marker message opened no second turn
    events = turns[0]["aiMessage"]
    flags = [e for e in events if e.get("type") == "policy"]
    assert [f["content"] for f in flags] == ["- [P002] computed on an ambiguity."]
    order = [e.get("type") for e in events if e.get("type") in ("text", "policy")]
    assert order == ["text", "policy", "text"]


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
    # With a live run active, history must remove the trailing turn whose user
    # message matches the run's — resume renders it fresh from the buffer.
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
    # Without an active run: both turns (the in-flight one has only an end sentinel).
    full = server.read_history(build_agent, object(), "alice:c")["history"]
    assert [t["userMessage"] for t in full] == ["q1", "q2-inflight"]
    # With the matching active run: the in-flight half-turn is removed.
    dropped = server.read_history(
        build_agent, object(), "alice:c", inflight_user_message="q2-inflight"
    )["history"]
    assert [t["userMessage"] for t in dropped] == ["q1"]


def test_read_history_drops_inflight_turn_stopped_mid_tool():
    # LangGraph checkpoints at each node boundary, so a turn interrupted after the
    # model issued a tool call (but before an answer) has an AIMessage(tool_calls)
    # with NO text. The identity match must still drop it (resume replays it).
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
        build_agent, object(), "alice:c", inflight_user_message="q2-inflight"
    )["history"]
    assert [t["userMessage"] for t in dropped] == ["q1"]
    # Without an active run the turn is kept (nothing will replay it).
    full = server.read_history(build_agent, object(), "alice:c")["history"]
    assert [t["userMessage"] for t in full] == ["q1", "q2-inflight"]


def test_read_history_drops_inflight_turn_with_committed_text():
    # THE refresh-duplicate bug: a multi-step in-flight turn can already have
    # committed answer TEXT in the checkpoint (prose emitted before/between tool
    # calls — LangGraph checkpoints at node boundaries). The old shape heuristic
    # ("has text = completed") kept it in history while resume replayed the same
    # turn from the buffer → shown twice. Identity matching drops it regardless
    # of how much of the turn has checkpointed.
    from langchain_core.messages import AIMessage, HumanMessage

    class _Graph:
        def get_state(self, cfg):
            class _S:
                values = {
                    "messages": [
                        HumanMessage(content="q1"),
                        AIMessage(content="a1"),
                        HumanMessage(content="q2-inflight"),
                        # mid-run: prose + a tool call already checkpointed, the
                        # run is still producing the rest of the turn
                        AIMessage(
                            content="Here is the trend so far...",
                            tool_calls=[
                                {"name": "render_chart", "args": {},
                                 "id": "call_1", "type": "tool_call"}
                            ],
                        ),
                    ]
                }

            return _S()

    build_agent = lambda *a, **k: _Graph()  # noqa: E731
    dropped = server.read_history(
        build_agent, object(), "alice:c", inflight_user_message="q2-inflight"
    )["history"]
    assert [t["userMessage"] for t in dropped] == ["q1"]


def test_read_history_keeps_prior_turn_when_inflight_not_checkpointed():
    # The active run's own human message may not have hit the checkpoint yet —
    # the trailing turn is then a PRIOR completed turn and must be kept (no
    # identity match), even though a run is active.
    from langchain_core.messages import AIMessage, HumanMessage

    class _Graph:
        def get_state(self, cfg):
            class _S:
                values = {
                    "messages": [
                        HumanMessage(content="q1"),
                        AIMessage(content="a1"),
                    ]
                }

            return _S()

    build_agent = lambda *a, **k: _Graph()  # noqa: E731
    kept = server.read_history(
        build_agent, object(), "alice:c", inflight_user_message="q2-just-sent"
    )["history"]
    assert [t["userMessage"] for t in kept] == ["q1"]


def test_read_history_answer_human_resume_drops_nothing():
    # answer_human continuations run with user_message="" — the paused turn's
    # checkpointed prefix (question + pre-interrupt content) must STAY in
    # history; the buffer appends only the continuation. An empty in-flight
    # message therefore never drops a turn.
    from langchain_core.messages import AIMessage, HumanMessage

    class _Graph:
        def get_state(self, cfg):
            class _S:
                values = {
                    "messages": [
                        HumanMessage(content="q1"),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {"name": "ask_human", "args": {},
                                 "id": "call_1", "type": "tool_call"}
                            ],
                        ),
                    ]
                }

            return _S()

    build_agent = lambda *a, **k: _Graph()  # noqa: E731
    kept = server.read_history(
        build_agent, object(), "alice:c", inflight_user_message=""
    )["history"]
    assert [t["userMessage"] for t in kept] == ["q1"]


# --- optional SQL tool gating (deploy flag AND per-run opt-in) ---------------


class _SqlFactoryConfig:
    """A ChatConfig-ish stub for make_agent_factory: only the fields it reads."""

    def __init__(self, sql_enabled):
        self.sql_enabled = sql_enabled
        self.athena_catalog = "AwsDataCatalog"
        self.athena_output = "s3://x/"
        self.athena_workgroup = "wg"
        self.sql_max_rows = 200


def _factory_tool_names(
    monkeypatch,
    *,
    sql_enabled,
    features,
    has_athena=True,
    has_redshift_data=False,
    scope=None,
    registry_item=None,
    web_search=False,
):
    """Build a graph via make_agent_factory, capturing the tool names it wired.

    Stubs the heavy collaborators (model build, consumption tools, build_graph) so
    the test exercises ONLY the gating/dispatch logic: does run_sql get added, and
    with which engine? ``registry_item`` is the mapping row ``_sql_scope`` reads
    for a ``scope``-carrying run (a resource-API item: plain Python values).
    """
    captured = {}

    monkeypatch.setattr(server, "make_agent_factory", server.make_agent_factory)
    # Patch the deferred imports the factory does inside its body.
    import chat.config as chat_config_mod
    import chat.graph as chat_graph_mod
    import chat.tools as chat_tools_mod

    monkeypatch.setattr(chat_config_mod, "build_chat_model", lambda *a, **k: object())

    class _FakeRegistryTable:
        def get_item(self, Key=None, **kw):
            return {"Item": registry_item} if registry_item else {}

    class _FakeToolsImpl:
        ddb = _FakeRegistryTable()

    monkeypatch.setattr(
        chat_tools_mod, "build_consumption_tools", lambda **k: _FakeToolsImpl()
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
        captured["tool_descriptions"] = {
            t.name: getattr(t, "description", "") for t in tools
        }
        captured["prompt"] = system_prompt
        captured["middleware"] = [type(m).__name__ for m in (middleware or [])]
        return object()

    monkeypatch.setattr(chat_graph_mod, "build_graph", _fake_build_graph)

    # Web search is deploy-gated only; stub the engine build so no gateway client
    # (or SigV4 transport) is constructed here.
    import chat.web_search as chat_web_search_mod

    monkeypatch.setattr(
        chat_web_search_mod,
        "build_web_search_engine",
        lambda cfg: object() if web_search else None,
    )
    monkeypatch.setattr(
        chat_web_search_mod, "make_web_search_tool", lambda engine: _T("web_search")
    )

    clients = {"s3": object(), "s3vectors": object(), "bedrock_runtime": object(), "ddb": object()}
    if has_athena:
        clients["athena"] = object()
    if has_redshift_data:
        clients["redshift_data"] = object()

    factory = server.make_agent_factory(_SqlFactoryConfig(sql_enabled), object(), clients)
    factory("us.anthropic.claude-opus-4-8", "high", scope, object(), features=features)
    return captured


def test_sql_tool_added_when_enabled_and_opted_in(monkeypatch):
    cap = _factory_tool_names(monkeypatch, sql_enabled=True, features={"sql"})
    assert "run_sql" in cap["tools"]
    # a SQL-aware system prompt is used for the turn
    assert cap["prompt"] is not None and "run_sql" in cap["prompt"]


def test_sql_tool_absent_without_opt_in(monkeypatch):
    from chat.graph import SYSTEM_PROMPT

    cap = _factory_tool_names(monkeypatch, sql_enabled=True, features=set())
    assert "run_sql" not in cap["tools"]
    # Base prompt: no SQL block appended — only the day-granular <current_date>
    # suffix rides after it (see graph.with_current_date).
    assert cap["prompt"].startswith(SYSTEM_PROMPT)
    assert "<sql_tool>" not in cap["prompt"]
    assert cap["prompt"].rstrip().endswith("</current_date>")


def test_sql_tool_absent_when_deploy_disabled(monkeypatch):
    from chat.graph import SYSTEM_PROMPT

    # Opted in by the client, but the deploy flag is off (and no athena client) —
    # the tool must NOT be wired (the browser can't self-grant SQL).
    cap = _factory_tool_names(
        monkeypatch, sql_enabled=False, features={"sql"}, has_athena=False
    )
    assert "run_sql" not in cap["tools"]
    assert cap["prompt"].startswith(SYSTEM_PROMPT)
    assert "<sql_tool>" not in cap["prompt"]


def test_web_search_tool_wired_on_every_run_when_deploy_enabled(monkeypatch):
    from chat.graph import WEB_SEARCH_BLOCK

    # Deploy-gated ONLY: no features opt-in, yet the tool + its prompt block ride
    # the run (unlike run_sql, it reads no source data).
    cap = _factory_tool_names(
        monkeypatch, sql_enabled=False, features=set(), web_search=True
    )
    assert "web_search" in cap["tools"]
    assert WEB_SEARCH_BLOCK in cap["prompt"]


def test_web_search_tool_absent_when_no_gateway(monkeypatch):
    cap = _factory_tool_names(monkeypatch, sql_enabled=False, features=set())
    assert "web_search" not in cap["tools"]
    assert "web_search" not in cap["prompt"]


def test_web_search_block_precedes_the_sql_block(monkeypatch):
    from chat.graph import SQL_BLOCK, WEB_SEARCH_BLOCK

    # Order is a prompt-CACHING property: the deployment-constant block must come
    # before the per-run one so base+web stays a shared prefix across turns.
    cap = _factory_tool_names(
        monkeypatch, sql_enabled=True, features={"sql"}, web_search=True
    )
    assert {"web_search", "run_sql"} <= set(cap["tools"])
    assert cap["prompt"].index(WEB_SEARCH_BLOCK) < cap["prompt"].index(SQL_BLOCK)


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


# --- per-source SQL engine dispatch (the @-scope's registry mapping) ----------

_RS_SCOPE = {"data_domain": "sales", "dataset": "orders_analytics"}
_RS_ITEM = {
    "glue_database": None,
    "source": {
        "type": "redshift",
        "redshift_database": "warehouse",
        "cluster_identifier": "prod-cluster",
        "secret_arn": "arn:aws:secretsmanager:eu-west-1:1:secret:okf-x",
    },
}


def test_sql_redshift_scope_gets_redshift_engine(monkeypatch):
    # A run @-scoped to a Redshift-backed dataset gets run_sql pinned to that
    # mapping's connection, with the Redshift prompt block + tool description.
    cap = _factory_tool_names(
        monkeypatch,
        sql_enabled=True,
        features={"sql"},
        has_redshift_data=True,
        scope=_RS_SCOPE,
        registry_item=_RS_ITEM,
    )
    assert "run_sql" in cap["tools"]
    assert "Redshift" in cap["tool_descriptions"]["run_sql"]
    assert "`warehouse`" in cap["tool_descriptions"]["run_sql"]
    assert "Redshift" in cap["prompt"]


def test_sql_redshift_scope_without_redshift_deploy_gets_no_tool(monkeypatch):
    # Redshift-scoped run on a deployment WITHOUT enable_redshift: no redshift
    # client -> NO SQL tool at all. It must never fall back to Athena (wrong
    # backend/dialect for the dataset's queries).
    cap = _factory_tool_names(
        monkeypatch,
        sql_enabled=True,
        features={"sql"},
        has_athena=True,
        has_redshift_data=False,
        scope=_RS_SCOPE,
        registry_item=_RS_ITEM,
    )
    assert "run_sql" not in cap["tools"]
    assert "run_sql" not in cap["prompt"]  # no SQL block either


def test_sql_redshift_scope_with_incomplete_mapping_gets_no_tool(monkeypatch):
    # A legacy db-only redshift row (no target/secret) can't connect -> no tool.
    cap = _factory_tool_names(
        monkeypatch,
        sql_enabled=True,
        features={"sql"},
        has_redshift_data=True,
        scope=_RS_SCOPE,
        registry_item={"source": {"type": "redshift", "redshift_database": "warehouse"}},
    )
    assert "run_sql" not in cap["tools"]


def test_sql_glue_scope_still_gets_athena_engine(monkeypatch):
    # A glue-mapped scope keeps the catalog-wide Athena engine (with the scope's
    # real glue_database as the default DB — asserted via the Athena description).
    cap = _factory_tool_names(
        monkeypatch,
        sql_enabled=True,
        features={"sql"},
        has_redshift_data=True,
        scope={"data_domain": "sales", "dataset": "orders"},
        registry_item={
            "glue_database": "orders",
            "source": {"type": "glue", "glue_database": "orders"},
        },
    )
    assert "run_sql" in cap["tools"]
    assert "Athena" in cap["tool_descriptions"]["run_sql"]
    assert "run_sql" in cap["prompt"] and "Redshift" not in cap["prompt"]


def test_read_history_end_event_carries_token_stats():
    """Checkpointed AIMessages retain usage_metadata; the fold sums it per turn
    onto the end event — so the UI's usage gauge works on reloaded history,
    including the cacheDetails shape that zeroes cache_creation."""
    import asyncio as _asyncio

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import START, MessagesState, StateGraph

    cp = InMemorySaver()

    def _respond(state):
        return {
            "messages": [
                AIMessage(
                    content="a1",
                    usage_metadata={
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                        "input_token_details": {
                            "cache_read": 40,
                            "cache_creation": 0,
                            "ephemeral_5m_input_tokens": 25,
                            "ephemeral_1h_input_tokens": 5,
                        },
                    },
                ),
                AIMessage(
                    content="a2",
                    usage_metadata={
                        "input_tokens": 50,
                        "output_tokens": 7,
                        "total_tokens": 57,
                        "input_token_details": {"cache_read": 10, "cache_creation": 3},
                    },
                ),
            ]
        }

    g = StateGraph(MessagesState)
    g.add_node("respond", _respond)
    g.add_edge(START, "respond")
    graph = g.compile(checkpointer=cp)
    _asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="q")]},
            {"configurable": {"thread_id": "alice:conv-u"}},
        )
    )

    def build_agent(model, effort, scope, checkpointer, features=None, user_sub="",
                    policy_checker=None):
        return graph

    out = server.read_history(build_agent, cp, "alice:conv-u")
    turns = out["history"]
    assert len(turns) == 1
    end = turns[0]["aiMessage"][-1]
    assert end["end"] is True
    stats = end["token_stats"]
    assert stats["input_tokens"] == 150
    assert stats["output_tokens"] == 17
    assert stats["cache_read_input_tokens"] == 50
    # 25+5 (ephemeral buckets beat the zeroed cache_creation) + 3 (native shape)
    assert stats["cache_creation_input_tokens"] == 33


def test_run_computation_is_always_bound_even_without_sql():
    """run_computation rides EVERY run — no deploy flag, no per-run SQL opt-in
    (the sanctioned path must not cost the raw-SQL opt-in); run_sql stays
    opt-in-gated. Same harness as the render_chart always-wired test."""
    import chat.config as chat_config_mod
    import chat.graph as chat_graph_mod
    import chat.tools as chat_tools
    from chat.config import ChatConfig
    from consumption_mcp.tools import ConsumptionConfig

    from .fakes import FakeConsumptionTools

    captured = {}

    def fake_build_graph(model, tools, checkpointer, *, system_prompt=None, middleware=None):
        captured["names"] = [t.name for t in tools]
        captured["prompt"] = system_prompt
        return object()

    cfg = ChatConfig(
        bundle_bucket="b", vector_bucket="v", vector_index="i",
        registry_table="r", checkpoint_table="cp", threads_table="th",
        catalog=[], sql_enabled=False,
    )
    cons_cfg = ConsumptionConfig(
        bundle_bucket="b", vector_bucket="v", vector_index="i", registry_table="r"
    )
    orig = (
        chat_graph_mod.build_graph,
        chat_config_mod.build_chat_model,
        chat_tools.build_consumption_tools,
    )
    try:
        chat_graph_mod.build_graph = fake_build_graph
        chat_config_mod.build_chat_model = lambda *a, **k: object()
        chat_tools.build_consumption_tools = lambda **kw: FakeConsumptionTools()
        build_agent = server.make_agent_factory(
            cfg, cons_cfg,
            {"s3": None, "s3vectors": None, "bedrock_runtime": None, "ddb": None},
        )
        # NO features (no SQL opt-in) and sql_enabled=False: still bound.
        build_agent("us.anthropic.claude-opus-4-8", "high", None, object(), features=set())
    finally:
        (
            chat_graph_mod.build_graph,
            chat_config_mod.build_chat_model,
            chat_tools.build_consumption_tools,
        ) = orig

    assert "run_computation" in captured["names"]
    assert "run_sql" not in captured["names"]  # ad-hoc SQL stays opt-in
    # The prompt block rides along so the model knows the tool exists.
    assert "<computations_tool>" in captured["prompt"]
