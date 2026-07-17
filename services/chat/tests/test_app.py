"""End-to-end app tests via FastAPI TestClient with an injected build_agent and
a real DynamoDBSaver on moto. Proves, over the Sparky-style wire contract:

- CORS headers on the SSE StreamingResponse + the /ping + OPTIONS routes,
- missing auth -> an ``error`` chunk (not a 500),
- a full ``send`` turn streams typed chunks + a terminal ``end`` and persists its
  checkpoint under the sub-NAMESPACED thread id (per-user isolation),
- ``get_session_history`` reads that conversation back as chatTurns,
- ``delete_history`` purges it,
- ``prepare`` short-circuits without building an agent or writing an index row.
"""

from __future__ import annotations

import json
from typing import Iterator

import boto3
import jwt
from moto import mock_aws

from .fakes import CHAT_CATALOG

REGION = "us-east-1"
CHECKPOINT_TABLE = "okf-chat-checkpoints"
THREADS_TABLE = "okf-chat"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def _token(sub: str) -> str:
    return jwt.encode({"sub": sub}, "k" * 32, algorithm="HS256")


def _create_checkpoint_table(ddb):
    ddb.create_table(
        TableName=CHECKPOINT_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _scripted_graph(checkpointer):
    """A real create_agent graph: model streams a tool call then answer text."""
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
    from langchain_core.tools import tool

    @tool
    def read_page(concept_id: str) -> str:
        """Read a wiki concept page."""
        return "PAGE BODY"

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="fb"))])

        def _stream(self, messages, stop=None, run_manager=None, **kw) -> Iterator[ChatGenerationChunk]:
            has_tool = any(isinstance(m, ToolMessage) for m in messages)
            if not has_tool:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=[],
                        tool_call_chunks=[
                            {"name": "read_page", "args": '{"concept_id":"orders"}', "id": "call_1", "index": 0}
                        ],
                    )
                )
            else:
                yield ChatGenerationChunk(message=AIMessageChunk(content="Done."))

    return create_agent(model=ScriptedModel(), tools=[read_page], checkpointer=checkpointer)


class _StubConfig:
    checkpoint_table = CHECKPOINT_TABLE
    threads_table = THREADS_TABLE
    region = REGION
    checkpoint_ttl_seconds = None

    def resolve_model_effort(self, model, effort):
        from okf_core.harvest_models import validate_model_effort

        return validate_model_effort(CHAT_CATALOG, model or "us.anthropic.claude-opus-4-8", effort)


def _capturing_index_writer(sink):
    def writer(*, user_sub, thread_id, title, model, effort, dataset_scope):
        sink.append(
            {
                "user_sub": user_sub,
                "thread_id": thread_id,
                "title": title,
                "model": model,
                "effort": effort,
                "dataset_scope": dataset_scope,
            }
        )

    return writer


def _build_app(index_sink=None):
    from chat import server

    return server.build_app(
        chat_config=_StubConfig(),
        build_agent=lambda model, effort, scope, cp, features=None: _scripted_graph(cp),
        index_writer=_capturing_index_writer(index_sink if index_sink is not None else []),
    )


def _client(index_sink=None):
    from fastapi.testclient import TestClient

    return TestClient(_build_app(index_sink=index_sink))


def _send(prompt="hi", **extra):
    return {"input": {"type": "send", "prompt": prompt, **extra}}


def _headers(sub="alice", session="conv-000000000000000000000000000000"):
    return {
        SESSION_HEADER: session,
        "Authorization": f"Bearer {_token(sub)}",
    }


def _sse_chunks(text: str):
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: ") :]))
    return out


# --- infra routes -----------------------------------------------------------


def test_ping_ok():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        r = _client().get("/ping")
        assert r.status_code == 200
        assert r.json()["status"] == "Healthy"


def test_options_preflight_has_cors():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        r = _client().options("/invocations")
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"


def test_invocations_missing_auth_returns_error_chunk_with_cors():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        c = _client()
        r = c.post("/invocations", json=_send(), headers={SESSION_HEADER: "s" * 40})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["access-control-allow-origin"] == "*"
        assert r.headers["x-accel-buffering"] == "no"
        chunks = _sse_chunks(r.text)
        assert chunks[0]["type"] == "error"
        assert chunks[-1]["end"] is True


# --- a full send turn -------------------------------------------------------


def test_send_streams_typed_chunks_and_persists_namespaced_checkpoint():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        from langgraph_checkpoint_aws import DynamoDBSaver

        client_thread = "conv-abc0000000000000000000000000000"
        c = _client()
        r = c.post(
            "/invocations",
            json=_send("list orders", model_id="us.anthropic.claude-opus-4-8", effort="high"),
            headers=_headers("alice", client_thread),
        )
        assert r.status_code == 200
        chunks = _sse_chunks(r.text)
        # a tool start, a tool result, answer text, and a terminal end
        assert any(ch.get("type") == "tool" and ch.get("tool_start") for ch in chunks)
        assert any(ch.get("type") == "tool" and ch.get("tool_start") is False for ch in chunks)
        assert any(ch.get("type") == "text" for ch in chunks)
        assert chunks[-1]["end"] is True

        # checkpoint persisted under the sub-NAMESPACED id, NOT the bare client id
        cp = DynamoDBSaver(table_name=CHECKPOINT_TABLE, region_name=REGION)
        assert cp.get_tuple(
            {"configurable": {"thread_id": f"alice:{client_thread}", "checkpoint_ns": ""}}
        ) is not None
        assert cp.get_tuple(
            {"configurable": {"thread_id": client_thread, "checkpoint_ns": ""}}
        ) is None


def test_two_users_same_client_thread_are_isolated():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        from langgraph_checkpoint_aws import DynamoDBSaver

        c = _client()
        shared = "conv-shared00000000000000000000000000"
        for sub in ("alice", "bob"):
            c.post(
                "/invocations",
                json=_send("hi", model_id="us.anthropic.claude-opus-4-8", effort="high"),
                headers=_headers(sub, shared),
            )
        cp = DynamoDBSaver(table_name=CHECKPOINT_TABLE, region_name=REGION)
        assert cp.get_tuple(
            {"configurable": {"thread_id": f"alice:{shared}", "checkpoint_ns": ""}}
        ) is not None
        assert cp.get_tuple(
            {"configurable": {"thread_id": f"bob:{shared}", "checkpoint_ns": ""}}
        ) is not None


def test_send_calls_index_writer_with_resolved_metadata():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        sink: list = []
        c = _client(index_sink=sink)
        c.post(
            "/invocations",
            json=_send(
                "hello",
                model_id="us.anthropic.claude-opus-4-8",
                effort="high",
                dataset_scope={"data_domain": "sales", "dataset": "orders"},
            ),
            headers=_headers("alice", "conv-idx0000000000000000000000000000000"),
        )
        assert len(sink) == 1
        row = sink[0]
        assert row["user_sub"] == "alice"
        assert row["thread_id"] == "conv-idx0000000000000000000000000000000"
        assert row["title"] == "hello"
        assert row["model"] == "us.anthropic.claude-opus-4-8"
        assert row["dataset_scope"] == {"data_domain": "sales", "dataset": "orders"}


# --- history read / delete --------------------------------------------------


def test_get_session_history_reads_back_turns():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        c = _client()
        thread = "conv-hist000000000000000000000000000000"
        c.post(
            "/invocations",
            json=_send("list orders", model_id="us.anthropic.claude-opus-4-8", effort="high"),
            headers=_headers("alice", thread),
        )
        r = c.post(
            "/invocations",
            json={"input": {"type": "get_session_history"}},
            headers=_headers("alice", thread),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["history"][0]["userMessage"] == "list orders"


def test_scoped_turn_history_shows_original_text_not_scope_preamble():
    # A scoped send injects a [Scope: …] preamble on the HUMAN message; on reload
    # the user's bubble must show ONLY what they typed, not our preamble.
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        c = _client()
        thread = "conv-scoped0000000000000000000000000000"
        c.post(
            "/invocations",
            json=_send(
                "how many races?",
                model_id="us.anthropic.claude-opus-4-8",
                effort="high",
                dataset_scope={"data_domain": "bird", "dataset": "formula_1"},
            ),
            headers=_headers("alice", thread),
        )
        r = c.post(
            "/invocations",
            json={"input": {"type": "get_session_history"}},
            headers=_headers("alice", thread),
        )
        assert r.status_code == 200
        user_msg = r.json()["history"][0]["userMessage"]
        assert user_msg == "how many races?"
        assert "[Scope:" not in user_msg


def test_delete_history_purges_checkpoint():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        from langgraph_checkpoint_aws import DynamoDBSaver

        c = _client()
        thread = "conv-del0000000000000000000000000000000"
        c.post(
            "/invocations",
            json=_send("hi", model_id="us.anthropic.claude-opus-4-8", effort="high"),
            headers=_headers("alice", thread),
        )
        cp = DynamoDBSaver(table_name=CHECKPOINT_TABLE, region_name=REGION)
        assert cp.get_tuple(
            {"configurable": {"thread_id": f"alice:{thread}", "checkpoint_ns": ""}}
        ) is not None

        r = c.post(
            "/invocations",
            json={"input": {"type": "delete_history"}},
            headers=_headers("alice", thread),
        )
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert cp.get_tuple(
            {"configurable": {"thread_id": f"alice:{thread}", "checkpoint_ns": ""}}
        ) is None


# --- prepare keep-warm ------------------------------------------------------


def test_prepare_short_circuits_without_building_agent_or_index():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        from chat import server
        from fastapi.testclient import TestClient

        def _boom_build(*a, **k):
            raise AssertionError("prepare must not build an agent")

        sink: list = []
        app = server.build_app(
            chat_config=_StubConfig(),
            build_agent=_boom_build,
            index_writer=_capturing_index_writer(sink),
        )
        r = TestClient(app).post(
            "/invocations",
            json={"input": {"type": "prepare"}},
            headers=_headers("alice", "conv-1"),
        )
        assert r.status_code == 200
        chunks = _sse_chunks(r.text)
        assert chunks[-1]["end"] is True
        assert sink == []  # keep-warm never touches the conversation index


# --- resume / stop ----------------------------------------------------------


def test_resume_with_no_active_stream_returns_no_active_marker():
    # Returning to a thread with nothing streaming → a no_active_stream marker so
    # the client falls back to loading history.
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        from chat import live_streams

        live_streams.reset()
        c = _client()
        r = c.post(
            "/invocations",
            json={"input": {"type": "resume"}},
            headers=_headers("alice", "conv-none000000000000000000000000000000"),
        )
        assert r.status_code == 200
        chunks = _sse_chunks(r.text)
        assert any(ch.get("type") == "no_active_stream" for ch in chunks)
        assert chunks[-1]["end"] is True


def test_resume_after_completed_turn_returns_no_active_stream():
    # Resume is for a STILL-STREAMING turn. Once a send has completed, the run is no
    # longer active → resume returns no_active_stream so the client falls back to
    # loading the finished answer from history (get_session_history), not the buffer.
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        from chat import live_streams

        live_streams.reset()
        c = _client()
        thread = "conv-resume0000000000000000000000000000"
        s = c.post(
            "/invocations",
            json=_send("list orders", model_id="us.anthropic.claude-opus-4-8", effort="high"),
            headers=_headers("alice", thread),
        )
        assert s.status_code == 200
        assert _sse_chunks(s.text)[-1]["end"] is True  # the send completed

        r = c.post(
            "/invocations",
            json={"input": {"type": "resume"}},
            headers=_headers("alice", thread),
        )
        assert r.status_code == 200
        chunks = _sse_chunks(r.text)
        assert any(ch.get("type") == "no_active_stream" for ch in chunks)
        assert chunks[-1]["end"] is True


def test_unknown_request_type_is_rejected_not_run_as_a_turn():
    # An unknown `type` must NOT fall through to the model (that's how a resume/stop
    # against an out-of-sync backend produced phantom empty replies). It's rejected.
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        from chat import server
        from fastapi.testclient import TestClient

        def _boom_build(*a, **k):
            raise AssertionError("unknown type must not build/run an agent")

        app = server.build_app(
            chat_config=_StubConfig(),
            build_agent=lambda model, effort, scope, cp, features=None: _boom_build(),
            index_writer=_capturing_index_writer([]),
        )
        r = TestClient(app).post(
            "/invocations",
            json={"input": {"type": "totally_unknown"}},
            headers=_headers("alice", "conv-x0000000000000000000000000000000000"),
        )
        assert r.status_code == 200
        chunks = _sse_chunks(r.text)
        assert chunks[0]["type"] == "error"
        assert chunks[-1]["end"] is True


def test_stop_returns_envelope_when_nothing_running():
    with mock_aws():
        _create_checkpoint_table(boto3.client("dynamodb", region_name=REGION))
        from chat import live_streams

        live_streams.reset()
        c = _client()
        r = c.post(
            "/invocations",
            json={"input": {"type": "stop"}},
            headers=_headers("alice", "conv-stop0000000000000000000000000000000"),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "stop"
        assert body["stopped"] is False
