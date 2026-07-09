"""Tests for the harvest live step feed: the pure label shaper + the emitter."""

from __future__ import annotations

import json

import pytest

from harvest.steps import (
    KIND_AGENT,
    KIND_SUBAGENT,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_USAGE,
    PHASE_COMPLETE,
    PHASE_ERROR,
    PHASE_START,
    STEP_MARKER,
    StepEmitter,
    UsageForwarder,
    make_log_sink,
    shape_step,
)


# --------------------------------------------------------------------------- #
# shape_step — pure label mapping (no framework needed)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tool,args,expected_label",
    [
        ("ls", {}, "Listing files"),
        ("ls", {"path": "/"}, "Listing files"),  # root -> generic label
        ("ls", {"path": "/.metadata/tables"}, "Listing .metadata/tables"),
        ("ls", {"path": "tables"}, "Listing tables"),
        ("read_file", {"file_path": "/tables/races.md"}, "Reading tables/races.md"),
        ("write_file", {"file_path": "tables/drivers.md"}, "Writing tables/drivers.md"),
        ("edit_file", {"file_path": "index.md"}, "Editing index.md"),
        ("glob", {"pattern": "*.md"}, "Searching for *.md"),
        ("grep", {"query": "grain"}, "Searching for grain"),
        ("write_todos", {}, "Planning the work"),
        (
            "sample_rows",
            {"concept_id": "tables/races"},
            "Sampling rows from tables/races",
        ),
        ("run_sql", {"query": "SELECT 1"}, "Running an Athena query"),
        (
            "get_backlinks",
            {"concept_id": "tables/races"},
            "Checking links for tables/races",
        ),
        (
            "get_links",
            {"concept_id": "tables/races"},
            "Checking links for tables/races",
        ),
        ("run_code", {"code": "print(1)"}, "Running code in the sandbox"),
    ],
)
def test_shape_step_known_tools(tool, args, expected_label):
    out = shape_step(tool, args)
    assert out["tool"] == tool
    assert out["label"] == expected_label


def test_shape_step_task_subagent():
    out = shape_step(
        "task",
        {"subagent_type": "table-author", "description": "Author tables/races\nmore"},
    )
    assert out["label"].startswith("Started table-author: Author tables/races")
    # Only the first line of the (multi-line) description is used.
    assert "more" not in out["label"]


def test_shape_step_task_without_description():
    out = shape_step("task", {"subagent_type": "reviewer"})
    assert out["label"] == "Started reviewer"


def test_shape_step_deep_path_uses_last_two_segments():
    out = shape_step("read_file", {"file_path": "/a/b/c/d/races.md"})
    assert out["label"] == "Reading d/races.md"


def test_shape_step_missing_arg_degrades_gracefully():
    assert shape_step("read_file", {})["label"] == "Reading a file"
    assert shape_step("sample_rows", None)["label"] == "Sampling table rows"


def test_shape_step_unknown_tool_is_readable():
    out = shape_step("some_new_tool", {})
    assert out["tool"] == "some_new_tool"
    assert out["label"] == "Some new tool"


def test_shape_step_empty_tool_name():
    # Total function: never returns an empty label even with no name.
    assert shape_step("", {})["label"] == "Working"


# --------------------------------------------------------------------------- #
# StepEmitter — callback hooks -> events (uses installed langchain_core)
# --------------------------------------------------------------------------- #


class _FakeMessage:
    def __init__(self, content, status=None, usage_metadata=None):
        self.content = content
        if status is not None:
            self.status = status
        # Mirror LangChain: usage_metadata is present on AIMessages that report
        # token usage, and absent (None) otherwise. Existing tests pass None so
        # they emit no usage event and their seq assertions stay exact.
        self.usage_metadata = usage_metadata


class _FakeGen:
    def __init__(self, message):
        self.message = message


class _FakeLLMResult:
    """Mimics langchain LLMResult.generations[0][0].message."""

    def __init__(self, message):
        self.generations = [[_FakeGen(message)]]


def _collect():
    events = []
    return events, events.append


def test_emitter_seq_is_monotonic_and_kinds_correct():
    events, sink = _collect()
    em = StepEmitter(sink)

    em.on_llm_end(_FakeLLMResult(_FakeMessage("I will author the races table.")))
    em.on_tool_start(
        {"name": "read_file"}, "", run_id="r1", inputs={"file_path": "tables/races.md"}
    )
    em.on_tool_end(_FakeMessage("ok", status="success"), run_id="r1")

    assert [e["seq"] for e in events] == [1, 2, 3]
    assert events[0]["kind"] == KIND_AGENT
    assert events[0]["label"] == "I will author the races table."
    assert events[1]["kind"] == KIND_TOOL_CALL
    assert events[1]["label"] == "Reading tables/races.md"
    assert events[2]["kind"] == KIND_TOOL_RESULT
    assert events[2]["ok"] is True


def test_emitter_tool_call_and_result_share_call_id():
    """The start and end of one tool call carry the same call_id (from run_id),
    so the UI can fold them into a single row even when calls interleave."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_tool_start({"name": "run_sql"}, "", run_id="abc-123", inputs={"query": "x"})
    em.on_tool_end(_FakeMessage("rows", status="success"), run_id="abc-123")
    assert events[0]["kind"] == KIND_TOOL_CALL
    assert events[1]["kind"] == KIND_TOOL_RESULT
    assert events[0]["call_id"] == "abc-123"
    assert events[1]["call_id"] == "abc-123"


def test_emitter_tool_error_status_marks_not_ok():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_tool_start({"name": "run_sql"}, "", run_id="r2", inputs={})
    em.on_tool_end(_FakeMessage("boom", status="error"), run_id="r2")
    assert events[-1]["kind"] == KIND_TOOL_RESULT
    assert events[-1]["ok"] is False
    assert events[-1]["call_id"] == "r2"


def test_emitter_on_tool_error_marks_not_ok():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_tool_start({"name": "ls"}, "", run_id="r3", inputs={})
    em.on_tool_error(RuntimeError("nope"), run_id="r3")
    assert events[-1] == {
        "seq": 2,
        "kind": KIND_TOOL_RESULT,
        "ok": False,
        "call_id": "r3",
    }


def test_emitter_drops_result_without_matching_call():
    """A result whose call was never emitted (dropped subagent, or out-of-window)
    is dropped — it carries no label, so rendering it would be a blank row."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_tool_end(_FakeMessage("orphan", status="success"), run_id="never-started")
    assert events == []


def test_emitter_skips_empty_ai_turns():
    events, sink = _collect()
    em = StepEmitter(sink)
    # A thinking-only / empty turn produces no visible content -> no event.
    em.on_llm_end(_FakeLLMResult(_FakeMessage("")))
    em.on_llm_end(_FakeLLMResult(_FakeMessage([{"type": "reasoning", "text": "hmm"}])))
    assert events == []


def test_emitter_extracts_text_blocks_only():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_llm_end(
        _FakeLLMResult(
            _FakeMessage(
                [
                    {"type": "reasoning", "text": "internal"},
                    {"type": "text", "text": "Authoring done."},
                ]
            )
        )
    )
    assert events[0]["label"] == "Authoring done."


def test_emitter_summary_is_truncated():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_llm_end(_FakeLLMResult(_FakeMessage("x " * 500)))
    assert len(events[0]["label"]) <= 200
    assert events[0]["label"].endswith("…")


def test_emitter_agent_carries_full_when_multiline():
    events, sink = _collect()
    em = StepEmitter(sink)
    md = "Here's the plan:\n\n- author races\n- author drivers\n\nThen review."
    em.on_llm_end(_FakeLLMResult(_FakeMessage(md)))
    # label is the collapsed one-liner; full preserves the markdown structure.
    assert "\n" not in events[0]["label"]
    assert events[0]["full"] == md
    assert "\n- author races" in events[0]["full"]


def test_emitter_agent_carries_full_when_longer_than_label():
    events, sink = _collect()
    em = StepEmitter(sink)
    long_line = "word " * 100  # single line, but longer than the 200-char label
    em.on_llm_end(_FakeLLMResult(_FakeMessage(long_line)))
    assert events[0]["label"].endswith("…")
    assert events[0]["full"] == long_line.strip()


def test_emitter_agent_omits_full_when_short_single_line():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_llm_end(_FakeLLMResult(_FakeMessage("Short decision.")))
    # Nothing to expand — no full field, so the UI won't offer a modal.
    assert "full" not in events[0]
    assert events[0]["label"] == "Short decision."


def test_emitter_agent_full_is_bounded():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_llm_end(_FakeLLMResult(_FakeMessage("y\n" * 10000)))
    from harvest.steps import _AGENT_FULL_MAX

    assert len(events[0]["full"]) <= _AGENT_FULL_MAX


# Top-level vs sub-agent is told apart ONLY by a nested langgraph checkpoint
# namespace (contains "|"). A top-level tool's on_tool_start carries a non-empty
# single-segment checkpoint_ns (e.g. "tools:uuid"), so that key must NOT be used
# as the discriminator — doing so dropped every top-level tool CALL.
_TOP_META = {"langgraph_checkpoint_ns": "tools:abc"}  # single segment
_SUB_META = {"langgraph_checkpoint_ns": "tools:abc|model:def"}  # nested


def test_emitter_keeps_top_level_tool_call_and_result():
    """Regression: a top-level tool's on_tool_start has a non-empty (single-
    segment) namespace; it MUST be kept, and its result paired to it."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_tool_start(
        {"name": "run_sql"},
        "",
        run_id="t1",
        inputs={"query": "SELECT 1"},
        metadata=_TOP_META,
    )
    em.on_tool_end(
        _FakeMessage("rows", status="success"), run_id="t1", metadata=_TOP_META
    )
    assert [e["kind"] for e in events] == [KIND_TOOL_CALL, KIND_TOOL_RESULT]
    assert (
        events[0]["tool"] == "run_sql"
        and events[0]["label"] == "Running an Athena query"
    )
    assert events[1]["ok"] is True and events[1]["call_id"] == events[0]["call_id"]


def test_emitter_drops_subagent_tool_call_and_result():
    """A sub-agent's internal tool call (nested ns) is dropped — AND so is its
    result, even if the result's metadata lost the nesting (pairing by call_id)."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_tool_start(
        {"name": "read_file"},
        "",
        run_id="s1",
        inputs={"file_path": "x.md"},
        metadata=_SUB_META,
    )
    # Result arrives with NO nesting in metadata (the asymmetry that leaked before)
    # — must still be dropped because its call was never emitted.
    em.on_tool_end(_FakeMessage("data", status="success"), run_id="s1", metadata={})
    assert events == []


# -- model-turn subagent filtering: the START/END metadata asymmetry --------- #
#
# LangChain passes `metadata` (carrying the nested langgraph namespace) to the
# START callbacks ONLY; on_llm_end receives an EMPTY metadata dict (verified
# against the installed langchain_core — see test_steps_integration.py). So the
# emitter classifies a model turn at on_chat_model_start (by run_id) and the
# metadata-less on_llm_end pairs back to it. These tests exercise that exact
# handshake — feeding metadata to on_llm_end (as older tests did) tests a
# contract that never happens.


def test_emitter_drops_subagent_internal_ai_turns():
    """A model turn that STARTED inside a sub-agent (nested ns at start) is
    dropped at end — even though on_llm_end sees no metadata."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_chat_model_start({}, [], run_id="m1", metadata=_SUB_META)
    # End hook carries NO metadata (the real contract) — must still be dropped.
    em.on_llm_end(
        _FakeLLMResult(_FakeMessage("subagent thinking out loud")), run_id="m1"
    )
    assert events == []


def test_emitter_keeps_top_level_when_not_nested():
    """A top-level model turn (single-segment ns at start, no '|') is kept."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_chat_model_start(
        {}, [], run_id="m2", metadata={"langgraph_checkpoint_ns": "model:abc"}
    )
    em.on_llm_end(_FakeLLMResult(_FakeMessage("Supervisor decision.")), run_id="m2")
    assert len(events) == 1 and events[0]["label"] == "Supervisor decision."


def test_emitter_keeps_top_level_turn_with_no_start_seen():
    """Defensive: if on_chat_model_start was never observed for a run (e.g. an
    early turn before wiring, or a completion-model quirk), on_llm_end defaults to
    KEEPING it — better a rare top-level-looking extra line than silent loss of
    the supervisor's narration. Sub-agent turns always have a start (that's where
    the fan-out mints them), so they're still filtered."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_llm_end(_FakeLLMResult(_FakeMessage("Unpaired turn.")), run_id="orphan")
    assert len(events) == 1 and events[0]["label"] == "Unpaired turn."


def test_emitter_on_llm_start_also_classifies_subagent():
    """The completion-model path (on_llm_start) records sub-agent runs too, so a
    plain-LLM sub-agent turn is filtered identically to a chat-model one."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_llm_start({}, ["prompt"], run_id="m3", metadata=_SUB_META)
    em.on_llm_end(_FakeLLMResult(_FakeMessage("subagent completion turn")), run_id="m3")
    assert events == []


def test_emitter_subagent_run_record_is_cleared_after_end():
    """The run_id record is discarded at on_llm_end so the set can't grow
    unbounded across a long crawl (one entry per in-flight sub-agent turn)."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_chat_model_start({}, [], run_id="m4", metadata=_SUB_META)
    em.on_llm_end(_FakeLLMResult(_FakeMessage("gone")), run_id="m4")
    assert em._subagent_llm_runs == set()


def test_emitter_never_raises_when_sink_throws():
    def boom(_event):
        raise ValueError("sink down")

    em = StepEmitter(boom)
    # Must not propagate — observation is best-effort.
    em.on_tool_start({"name": "ls"}, "", inputs={})
    em.on_llm_end(_FakeLLMResult(_FakeMessage("hi")))


# --------------------------------------------------------------------------- #
# Token usage metering (KIND_USAGE) — cumulative across all model turns
#
# Metering is driven by StepEmitter.record_usage, called from the MODEL-instance
# callback (UsageForwarder), NOT on_llm_end on the run config. This is the fix
# for the undercount: QuickJS task() sub-agents run on their own asyncio tasks
# and never reach the run-config callback, but they DO invoke the shared model,
# so the model-instance callback sees them. These tests drive record_usage /
# UsageForwarder directly, mirroring that path.
# --------------------------------------------------------------------------- #


def _usage(inp, out, *, cache_read=0, cache_creation=0):
    """A LangChain-shaped usage_metadata dict (cache split under details)."""
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "input_token_details": {
            "cache_read": cache_read,
            "cache_creation": cache_creation,
        },
    }


def test_record_usage_emits_cumulative_snapshot():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.record_usage(_FakeMessage("Authoring.", usage_metadata=_usage(100, 20)))
    usage_events = [e for e in events if e["kind"] == KIND_USAGE]
    assert len(usage_events) == 1
    u = usage_events[0]["usage"]
    assert u == {
        "input": 100,
        "output": 20,
        "cache_read": 0,
        "cache_write": 0,
        "total": 120,
    }


def test_record_usage_accumulates_across_turns():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.record_usage(_FakeMessage("t1", usage_metadata=_usage(100, 20)))
    em.record_usage(_FakeMessage("t2", usage_metadata=_usage(50, 10)))
    snapshots = [e["usage"] for e in events if e["kind"] == KIND_USAGE]
    # Each snapshot is the running cumulative total (input+output summed).
    assert snapshots[0]["total"] == 120
    assert snapshots[1] == {
        "input": 150,
        "output": 30,
        "cache_read": 0,
        "cache_write": 0,
        "total": 180,
    }


def test_record_usage_maps_anthropic_cache_split():
    """cache_read = a prompt-cache HIT; cache_write = LangChain's cache_creation."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.record_usage(
        _FakeMessage(
            "x", usage_metadata=_usage(80, 5, cache_read=60, cache_creation=20)
        )
    )
    u = [e for e in events if e["kind"] == KIND_USAGE][0]["usage"]
    assert u["cache_read"] == 60
    assert u["cache_write"] == 20


def test_record_usage_no_event_when_turn_reports_none():
    """A turn with no usage_metadata (thinking-only / provider omission) emits no
    usage event."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.record_usage(_FakeMessage("Decision."))  # usage_metadata=None
    assert events == []


def test_record_usage_ignores_zero_only_turn():
    """An all-zero usage dict adds nothing and emits no snapshot."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.record_usage(_FakeMessage("", usage_metadata=_usage(0, 0)))
    assert events == []


def test_on_llm_end_does_not_meter_usage():
    """Regression: on_llm_end (run-config callback) must NOT meter — doing so
    would MISS QuickJS sub-agent turns (undercount) AND double-count the ones it
    does see. It emits only the agent text row; metering is the model callback."""
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_llm_end(
        _FakeLLMResult(_FakeMessage("Decision.", usage_metadata=_usage(100, 20)))
    )
    assert [e["kind"] for e in events] == [KIND_AGENT]
    assert all(e["kind"] != KIND_USAGE for e in events)


def test_usage_forwarder_meters_every_turn_regardless_of_dispatch():
    """UsageForwarder (the model-instance callback) forwards each turn's usage to
    the emitter — this is the path that catches sub-agent turns the run-config
    callback never sees. It takes an LLMResult (what on_llm_end receives)."""
    events, sink = _collect()
    em = StepEmitter(sink)
    fwd = UsageForwarder(em)
    # A supervisor turn and a sub-agent turn both hit the SAME shared model, so
    # both reach the forwarder — the cumulative total spans both.
    fwd.on_llm_end(
        _FakeLLMResult(_FakeMessage("supervisor", usage_metadata=_usage(100, 20)))
    )
    fwd.on_llm_end(
        _FakeLLMResult(_FakeMessage("subagent", usage_metadata=_usage(500, 40)))
    )
    snapshots = [e["usage"] for e in events if e["kind"] == KIND_USAGE]
    assert len(snapshots) == 2
    assert snapshots[-1]["total"] == 660  # 100+20 + 500+40


def test_usage_forwarder_never_raises_on_bad_response():
    """The forwarder must never propagate into a model call — a malformed
    response is swallowed."""
    events, sink = _collect()
    em = StepEmitter(sink)
    fwd = UsageForwarder(em)
    fwd.on_llm_end(object())  # no .generations — must not raise
    assert events == []


# --------------------------------------------------------------------------- #
# make_log_sink — structured OKF_STEP line
# --------------------------------------------------------------------------- #


def test_log_sink_emits_marked_json_line(caplog):
    sink = make_log_sink(
        data_domain="sales", dataset="orders", session_id="okf-sales-orders-x"
    )
    with caplog.at_level("INFO", logger="okf.harvest.steps"):
        sink(
            {
                "seq": 3,
                "kind": KIND_TOOL_CALL,
                "tool": "run_sql",
                "label": "Running an Athena query",
                "agent": "main",
            }
        )

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert msg.startswith(STEP_MARKER + " ")
    payload = json.loads(msg[len(STEP_MARKER) + 1 :])
    assert payload["data_domain"] == "sales"
    assert payload["dataset"] == "orders"
    assert payload["session_id"] == "okf-sales-orders-x"
    assert payload["seq"] == 3
    assert payload["label"] == "Running an Athena query"
    assert payload["ts"]  # ISO timestamp stamped server-side


def test_log_sink_end_to_end_through_emitter(caplog):
    sink = make_log_sink(data_domain="sales", dataset="orders", session_id="sid")
    em = StepEmitter(sink)
    with caplog.at_level("INFO", logger="okf.harvest.steps"):
        em.on_tool_start({"name": "ls"}, "", inputs={})
        em.on_tool_end(_FakeMessage("ok", status="success"))

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    call = json.loads(lines[0][len(STEP_MARKER) + 1 :])
    result = json.loads(lines[1][len(STEP_MARKER) + 1 :])
    assert call["kind"] == KIND_TOOL_CALL and call["label"] == "Listing files"
    assert result["kind"] == KIND_TOOL_RESULT and result["ok"] is True


# --------------------------------------------------------------------------- #
# Sub-agent fleet: emitter methods (start/complete/error from the custom stream)
# --------------------------------------------------------------------------- #


def test_emit_subagent_start_carries_label_and_batch():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.emit_subagent_event(
        {
            "type": "subagent",
            "phase": "start",
            "id": "ptc_task_ab12",
            "eval_id": "call_eval_1",
            "subagent_type": "reviewer",
            "label": "verify races",
        }
    )
    assert events[0]["kind"] == KIND_SUBAGENT
    assert events[0]["phase"] == PHASE_START
    assert events[0]["batch"] == "call_eval_1"
    assert events[0]["sub_id"] == "ptc_task_ab12"
    assert events[0]["label"] == "reviewer: verify races"
    assert events[0]["subagent_type"] == "reviewer"


def test_emit_subagent_complete_and_error():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.emit_subagent_event(
        {"type": "subagent", "phase": "complete", "id": "x", "eval_id": "b"}
    )
    em.emit_subagent_event(
        {"type": "subagent", "phase": "error", "id": "y", "eval_id": "b"}
    )
    assert events[0]["phase"] == PHASE_COMPLETE and events[0]["sub_id"] == "x"
    assert events[1]["phase"] == PHASE_ERROR and events[1]["sub_id"] == "y"


def test_emit_subagent_ignores_unknown_phase():
    events, sink = _collect()
    em = StepEmitter(sink)
    em.emit_subagent_event({"type": "subagent", "phase": "heartbeat", "id": "z"})
    assert events == []


def test_subagent_events_share_seq_space_with_steps():
    # Fleet events use the SAME monotonic seq as step events, so a single ?since
    # cursor pages the whole feed in order.
    events, sink = _collect()
    em = StepEmitter(sink)
    em.on_tool_start({"name": "ls"}, "", run_id="r", inputs={})
    em.emit_subagent_event(
        {"type": "subagent", "phase": "start", "id": "p1", "eval_id": "b"}
    )
    em.emit_subagent_event(
        {"type": "subagent", "phase": "complete", "id": "p1", "eval_id": "b"}
    )
    assert [e["seq"] for e in events] == [1, 2, 3]
