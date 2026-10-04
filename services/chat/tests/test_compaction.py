"""Compaction + context measurement + the model-family pin (chat.compaction)."""

from __future__ import annotations

import json
from typing import Iterator

import pytest

from chat import compaction as cp

OPUS = "global.anthropic.claude-opus-5-5"
SONNET = "global.anthropic.claude-sonnet-5-5"
SOL = "global.openai.gpt-6.1-sol"


# --- pure helpers ----------------------------------------------------------------


def test_auto_compact_threshold_keeps_sentrys_headroom_rule():
    # 1M window: 95% leaves 50K headroom, so 95% stands.
    assert cp.auto_compact_threshold(1_000_000) == 950_000
    # 200K window: 95% would leave 10K, so it lowers to keep 30K.
    assert cp.auto_compact_threshold(200_000) == 170_000
    # Tiny window: never below half.
    assert cp.auto_compact_threshold(40_000) == 20_000


def test_trigger_fraction_env_is_clamped():
    assert cp.compact_trigger_fraction({"OKF_CHAT_COMPACT_TRIGGER": "0.8"}) == 0.8
    assert cp.compact_trigger_fraction({"OKF_CHAT_COMPACT_TRIGGER": "0.2"}) == 0.5
    assert cp.compact_trigger_fraction({"OKF_CHAT_COMPACT_TRIGGER": "2"}) == 0.95
    assert cp.compact_trigger_fraction({"OKF_CHAT_COMPACT_TRIGGER": "x"}) == 0.95


def test_model_family():
    assert cp.model_family(OPUS) == cp.model_family(SONNET) == "anthropic"
    assert cp.model_family(SOL) == cp.model_family("gpt-6-luna") == "openai"


def test_context_report_shape():
    assert cp.context_report(None, OPUS) is None
    report = cp.context_report(250_000, OPUS, compactions=2)
    assert report == {
        "tokens": 250_000,
        "window": 1_000_000,
        "percent": 25.0,
        "threshold": 950_000,
        "model": OPUS,
        "compactions": 2,
    }


def _conversation():
    """user, AI(tool call), tool, AI answer, user, AI answer — with ids."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    return [
        HumanMessage(content="How many orders?", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{"name": "run_sql", "args": {"sql": "SELECT 1"}, "id": "c1"}],
        ),
        ToolMessage(content="42", tool_call_id="c1", name="run_sql", id="t1"),
        AIMessage(content="There are 42 orders.", id="a2"),
        HumanMessage(content="And by month?", id="h2"),
        AIMessage(content="Monthly: ...", id="a3"),
    ]


def test_model_view_replaces_the_covered_prefix_with_the_summary():
    msgs = _conversation()
    view = cp.model_view(msgs, {"summary": "S", "through_id": "a2"})
    assert [getattr(m, "id", None) for m in view[1:]] == ["h2", "a3"]
    assert view[0].additional_kwargs[cp.COMPACTION_MARKER] is True
    assert "S" in view[0].content
    # No compaction -> untouched; an unknown through_id degrades to full history.
    assert cp.model_view(msgs, None) is msgs
    assert cp.model_view(msgs, {"summary": "S", "through_id": "gone"}) is msgs


def test_model_view_never_leads_with_an_orphaned_tool_result():
    msgs = _conversation()
    view = cp.model_view(msgs, {"summary": "S", "through_id": "a1"})
    assert [getattr(m, "id", None) for m in view[1:]] == ["a2", "h2", "a3"]


class _Summarizer:
    def __init__(self):
        self.prompts: list[str] = []

    def invoke(self, messages, config=None):
        from langchain_core.messages import AIMessage

        self.prompts.append(messages[0].content)
        return AIMessage(content=f"SUMMARY #{len(self.prompts)}")


def test_manual_compaction_covers_everything():
    msgs = _conversation()
    summarizer = _Summarizer()
    compaction, through = cp.compact_messages(summarizer, msgs, None, keep_tokens=0)
    assert through == len(msgs) - 1
    assert compaction == {"summary": "SUMMARY #1", "through_id": "a3"}
    prompt = summarizer.prompts[0]
    assert "USER: How many orders?" in prompt
    assert "ANALYST CALLED run_sql" in prompt and "TOOL RESULT (run_sql): 42" in prompt
    # Nothing new since -> nothing to compact.
    assert cp.compact_messages(summarizer, msgs, compaction, keep_tokens=0) is None


def test_a_second_compaction_folds_the_previous_summary_in():
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = _conversation()
    summarizer = _Summarizer()
    first, _ = cp.compact_messages(summarizer, msgs, None, keep_tokens=0)
    msgs += [HumanMessage(content="Top customer?", id="h3"), AIMessage(content="ACME", id="a4")]
    second, _ = cp.compact_messages(summarizer, msgs, first, keep_tokens=0)
    assert second["through_id"] == "a4"
    assert "SUMMARY #1" in summarizer.prompts[1]  # the previous summary
    assert "How many orders?" not in summarizer.prompts[1]  # already covered


def test_auto_cutoff_keeps_a_recent_tail_at_a_step_boundary():
    msgs = _conversation()
    through = cp.choose_through(msgs, 0, keep_tokens=cp._approx_tokens(msgs[4:]))
    assert msgs[through].id == "a2"  # the tail starts at the next user turn


def test_cutoff_never_ends_on_a_memory_recall():
    from langchain_core.messages import AIMessage, HumanMessage

    from chat.memory import MEMORY_MARKER

    msgs = [
        HumanMessage(content="q1", id="h1"),
        AIMessage(content="a1", id="a1"),
        HumanMessage(content="q2", id="h2"),
        HumanMessage(content="recall", id="m2", additional_kwargs={MEMORY_MARKER: "recall"}),
        AIMessage(content="a2", id="a2"),
    ]
    through = cp.choose_through(msgs, 0, keep_tokens=cp._approx_tokens(msgs[4:]))
    assert msgs[through].id == "h2"  # not the strippable recall


def test_turn_index_ignores_harness_notes():
    from langchain_core.messages import HumanMessage

    from chat.steering import STEERING_MARKER

    msgs = _conversation()
    msgs.insert(3, HumanMessage(content="nudge", additional_kwargs={STEERING_MARKER: True}))
    assert cp.turn_index_of(msgs, 4) == 0
    assert cp.turn_index_of(msgs, len(msgs) - 1) == 1


# --- the middleware in a real graph ------------------------------------------------


def _recording_model(replies, seen):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

    replies = list(replies)

    class Recording(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "recording"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            from langchain_core.messages import SystemMessage

            # Record the conversation the model got (minus the system prompt).
            seen.append([m for m in messages if not isinstance(m, SystemMessage)])
            tokens = replies.pop(0)
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=f"answer {len(seen)}",
                            usage_metadata={
                                "input_tokens": tokens,
                                "output_tokens": 10,
                                "total_tokens": tokens + 10,
                            },
                        )
                    )
                ]
            )

        def _stream(self, messages, stop=None, run_manager=None, **kw) -> Iterator[ChatGenerationChunk]:
            result = self._generate(messages)
            msg = result.generations[0].message
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=msg.content, usage_metadata=msg.usage_metadata)
            )

    return Recording()


def _graph(model, summarizer=None):
    from langgraph.checkpoint.memory import InMemorySaver

    from chat.graph import build_graph

    return build_graph(
        model,
        [],
        InMemorySaver(),
        middleware=[cp.CompactionMiddleware(OPUS, summarizer=summarizer)],
    )


def test_after_model_records_the_measured_context():
    seen: list = []
    graph = _graph(_recording_model([1234], seen))
    cfg = {"configurable": {"thread_id": "t"}}
    graph.invoke({"messages": [{"role": "user", "content": "q"}], cp.MODEL_KEY: OPUS}, cfg)
    values = graph.get_state(cfg).values
    assert values[cp.CONTEXT_KEY]["tokens"] == 1234
    assert values[cp.CONTEXT_KEY]["model"] == OPUS
    # The reading remembers where it was taken (the estimate counts what follows).
    assert values[cp.CONTEXT_KEY]["last_id"] == values["messages"][-1].id
    assert values[cp.MODEL_KEY] == OPUS


def test_auto_compaction_fires_over_the_threshold_and_the_model_sees_the_summary():
    seen: list = []
    summarizer = _Summarizer()
    # Turn 1 measures 960K (over Opus's 950K threshold); turn 2's call then
    # runs on the compacted view.
    graph = _graph(_recording_model([960_000, 5_000], seen), summarizer=lambda: summarizer)
    cfg = {"configurable": {"thread_id": "t"}}
    # ~150K tokens of question: bigger than the 100K tail auto compaction keeps.
    graph.invoke({"messages": [{"role": "user", "content": "q1 " + "x" * 600_000}]}, cfg)
    graph.invoke({"messages": [{"role": "user", "content": "q2"}]}, cfg)

    values = graph.get_state(cfg).values
    records = values[cp.COMPACTIONS_KEY]
    assert len(records) == 1
    assert records[0]["source"] == "auto" and records[0]["phase"] == "during"
    # pre_tokens is the ESTIMATE: the 960K reading + the new question.
    assert 960_000 < records[0]["pre_tokens"] < 960_100 and records[0]["turn"] == 1
    # The transcript keeps every message; the model got summary + tail.
    assert len(values["messages"]) == 4
    second_call = seen[1]
    assert second_call[0].additional_kwargs.get(cp.COMPACTION_MARKER)
    assert [m.content for m in second_call[1:]] == ["answer 1", "q2"]
    assert "q1" in summarizer.prompts[0]


def test_a_failed_auto_compaction_never_fails_the_turn():
    seen: list = []

    class Broken:
        def invoke(self, messages, config=None):
            raise RuntimeError("bedrock down")

    graph = _graph(_recording_model([960_000, 5_000], seen), summarizer=lambda: Broken())
    cfg = {"configurable": {"thread_id": "t"}}
    graph.invoke({"messages": [{"role": "user", "content": "q1 " + "x" * 600_000}]}, cfg)
    graph.invoke({"messages": [{"role": "user", "content": "q2"}]}, cfg)
    assert len(seen[1]) == 3  # full history, uncompacted
    assert cp.COMPACTIONS_KEY not in graph.get_state(cfg).values


# --- the server: family pin, context chunks, the compact invocation ------------------


@pytest.fixture
def app(monkeypatch):
    """The real server over a CompactionMiddleware graph + in-memory checkpoints."""
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver

    from chat import server
    from chat.graph import build_graph

    catalog = [
        {"model": m, "label": m, "efforts": ["low", "high"], "default_effort": "high"}
        for m in (OPUS, SONNET, SOL)
    ]
    saver = InMemorySaver()
    seen: list = []
    replies = [1_000, 2_000, 3_000, 4_000]

    def build_agent(model, effort, scope, checkpointer, features=None, user_sub="", policy_checker=None):
        return build_graph(
            _recording_model(replies, seen),
            [],
            saver,
            middleware=[cp.CompactionMiddleware(model)],
        )

    class Config:
        checkpoint_table = "unused"
        threads_table = "unused"
        region = "us-east-1"
        checkpoint_ttl_seconds = None
        default_model = OPUS

        def resolve_model_effort(self, model, effort):
            from okf_core.harvest_models import validate_model_effort

            return validate_model_effort(catalog, model or OPUS, effort)

    summarizer = _Summarizer()
    import chat.config

    monkeypatch.setattr(chat.config, "build_chat_model", lambda cfg, m, e, **kw: summarizer)
    monkeypatch.setattr(server, "make_checkpointer", lambda cfg: saver)
    index_rows: list[dict] = []
    client = TestClient(
        server.build_app(
            chat_config=Config(),
            build_agent=build_agent,
            index_writer=lambda **row: index_rows.append(row),
        )
    )
    client.index_rows = index_rows
    return client, summarizer


def _post(client, thread, input_):
    import jwt

    from chat.server import SESSION_HEADER

    token = jwt.encode({"sub": "alice"}, "k" * 32, algorithm="HS256")
    return client.post(
        "/invocations",
        json={"input": input_},
        headers={"Authorization": f"Bearer {token}", SESSION_HEADER: thread},
    )


def _chunks(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


THREAD = "conv-" + "0" * 32


def test_send_streams_context_and_the_end_marker_carries_it(app):
    client, _ = app
    chunks = _chunks(_post(client, THREAD, {"type": "send", "prompt": "q", "model_id": OPUS}).text)
    context = [c for c in chunks if c.get("type") == "context"]
    assert context and context[-1]["context"]["tokens"] == 1_000
    assert chunks[-1]["end"] is True
    assert chunks[-1]["context"]["window"] == 1_000_000


def test_a_started_conversation_cannot_switch_provider_family(app):
    client, _ = app
    _post(client, THREAD, {"type": "send", "prompt": "q", "model_id": OPUS})
    # Claude -> Claude is fine ...
    ok = _chunks(_post(client, THREAD, {"type": "send", "prompt": "q2", "model_id": SONNET}).text)
    assert not any(c.get("type") == "error" for c in ok)
    # ... Claude -> GPT is refused.
    refused = _chunks(_post(client, THREAD, {"type": "send", "prompt": "q3", "model_id": SOL}).text)
    assert refused[0]["error_code"] == "model_family_locked"
    assert refused[-1]["end"] is True
    # The refused request never rewrote the sidebar row's model.
    assert [row["model"] for row in client.index_rows] == [OPUS, SONNET]


def test_compact_invocation_summarizes_and_history_reports_it(app):
    client, summarizer = app
    empty = _post(client, THREAD, {"type": "compact"}).json()
    assert empty["compacted"] is False and empty["reason"] == "nothing_to_compact"

    _post(client, THREAD, {"type": "send", "prompt": "q", "model_id": OPUS})
    result = _post(client, THREAD, {"type": "compact"}).json()
    assert result["compacted"] is True
    assert result["compaction"]["source"] == "manual"
    assert result["compaction"]["phase"] == "after" and result["compaction"]["turn"] == 0
    assert result["context"]["compactions"] == 1
    assert len(summarizer.prompts) == 1

    history = _post(client, THREAD, {"type": "get_session_history"}).json()
    assert len(history["history"]) == 1  # the transcript is intact
    assert history["model"] == OPUS
    assert history["compactions"] == [result["compaction"]]
    assert history["context"]["compactions"] == 1

    again = _post(client, THREAD, {"type": "compact"}).json()
    assert again["reason"] == "nothing_to_compact"


def test_the_auto_summary_never_streams_as_the_answer():
    # The summarizer runs INSIDE the graph's callback context: without the
    # nostream tag its tokens surface in stream_mode="messages" as if they were
    # the agent's answer (and their usage as the context reading).
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    seen: list = []
    summarizer = GenericFakeChatModel(messages=iter([AIMessage(content="SUMMARY-LEAK")]))
    graph = _graph(_recording_model([960_000, 5_000], seen), summarizer=lambda: summarizer)
    cfg = {"configurable": {"thread_id": "t"}}
    graph.invoke({"messages": [{"role": "user", "content": "q1 " + "x" * 600_000}]}, cfg)
    streamed = [
        chunk.content
        for chunk, _meta in graph.stream(
            {"messages": [{"role": "user", "content": "q2"}]}, cfg, stream_mode="messages"
        )
    ]
    assert graph.get_state(cfg).values.get(cp.COMPACTIONS_KEY)  # it did compact
    assert not any("SUMMARY-LEAK" in str(c) for c in streamed)


def test_auto_compaction_counts_what_was_appended_since_the_last_reading():
    # 900K measured (under the 950K threshold) + a ~200K-token question: the
    # ESTIMATE crosses, so the next call is compacted before it can overflow.
    seen: list = []
    summarizer = _Summarizer()
    graph = _graph(_recording_model([900_000, 5_000], seen), summarizer=lambda: summarizer)
    cfg = {"configurable": {"thread_id": "t"}}
    graph.invoke({"messages": [{"role": "user", "content": "q1 " + "x" * 600_000}]}, cfg)
    graph.invoke({"messages": [{"role": "user", "content": "q2 " + "y" * 800_000}]}, cfg)
    records = graph.get_state(cfg).values[cp.COMPACTIONS_KEY]
    assert len(records) == 1 and records[0]["pre_tokens"] > 950_000
    assert seen[1][0].additional_kwargs.get(cp.COMPACTION_MARKER)


def test_a_context_overflow_is_compacted_and_retried_once():
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    seen: list = []

    class Overflowing(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "overflowing"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            seen.append(list(messages))
            if len(seen) == 2:  # turn 2's first attempt
                raise ValueError("ValidationException: Input is too long for requested model.")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content=f"answer {len(seen)}",
                usage_metadata={"input_tokens": 100, "output_tokens": 1, "total_tokens": 101},
            ))])

    summarizer = _Summarizer()
    graph = _graph(Overflowing(), summarizer=lambda: summarizer)
    cfg = {"configurable": {"thread_id": "t"}}
    graph.invoke({"messages": [{"role": "user", "content": "q1"}]}, cfg)
    graph.invoke({"messages": [{"role": "user", "content": "q2"}]}, cfg)
    values = graph.get_state(cfg).values
    # The retry went out compacted, and the compaction was persisted.
    assert any(m.additional_kwargs.get(cp.COMPACTION_MARKER) for m in seen[2])
    assert values[cp.COMPACTION_KEY]["summary"] == "SUMMARY #1"
    assert len(values[cp.COMPACTIONS_KEY]) == 1


def test_personal_context_survives_compaction_verbatim():
    from langchain_core.messages import AIMessage, HumanMessage

    from chat.memory import MEMORY_MARKER

    msgs = [
        HumanMessage(content="q1", id="h1"),
        HumanMessage(content="ABOUT THE USER: Dana, finance", id="p1",
                     additional_kwargs={MEMORY_MARKER: "personal"}),
        AIMessage(content="a1", id="a1"),
        HumanMessage(content="q2", id="h2"),
        AIMessage(content="a2", id="a2"),
    ]
    summarizer = _Summarizer()
    compaction, _ = cp.compact_messages(summarizer, msgs, None, keep_tokens=0)
    assert "Dana" not in summarizer.prompts[0]  # kept verbatim, not summarized
    view = cp.model_view(msgs, compaction)
    assert view[0].id == "p1" and view[1].additional_kwargs.get(cp.COMPACTION_MARKER)
