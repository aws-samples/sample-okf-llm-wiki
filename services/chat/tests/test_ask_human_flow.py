"""ask_human end-to-end wiring: middleware interrupt + server chunk emit + resume.

The middleware OWNS the interrupt (not the tool). We prove: a valid ask_human call
raises langgraph.interrupt with the normalized questions; a malformed call returns
an error ToolMessage instead (no interrupt); the server translates the resulting
__interrupt__ into an ask_human chunk; and the answer path drives Command(resume).
"""

from __future__ import annotations

import json
import types

import pytest

from chat import server
from chat.ask_human_middleware import AskHumanMiddleware


class _Req:
    """Minimal ToolCallRequest stand-in (the middleware only reads .tool_call)."""

    def __init__(self, name, args, call_id="call_1"):
        self.tool_call = {"name": name, "args": args, "id": call_id}


def _handler_marker():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return "downstream-ran"

    return handler, calls


def test_middleware_passes_through_non_ask_human():
    mw = AskHumanMiddleware()
    handler, calls = _handler_marker()
    out = mw.wrap_tool_call(_Req("read_page", {"concept_id": "x"}), handler)
    assert out == "downstream-ran"
    assert calls["n"] == 1


def test_middleware_raises_interrupt_on_valid_ask_human(monkeypatch):
    # Stub langgraph.types.interrupt so we can prove it's called with the normalized
    # payload WITHOUT a running graph (and capture what would be surfaced).
    seen = {}

    def fake_interrupt(payload):
        seen["payload"] = payload
        # Simulate resume: interrupt() returns the user's answers.
        return [{"id": "grain", "answer": "Weekly"}]

    import langgraph.types as lgt

    monkeypatch.setattr(lgt, "interrupt", fake_interrupt)

    mw = AskHumanMiddleware()
    handler, calls = _handler_marker()
    req = _Req(
        "ask_human",
        {"questions": [{"id": "grain", "prompt": "Which grain?", "kind": "single",
                        "options": ["Daily", "Weekly"]}]},
    )
    out = mw.wrap_tool_call(req, handler)

    # The interrupt fired with the normalized questions (allow_other added).
    assert seen["payload"]["type"] == "ask_human"
    assert seen["payload"]["questions"][0]["allow_other"] is True
    # Downstream tool never ran (middleware short-circuited).
    assert calls["n"] == 0
    # The (resumed) answers came back as a tool result the model reads.
    body = json.loads(out.content)
    assert body["status"] == "answered"
    assert body["answers"][0]["answer"] == "Weekly"
    assert out.name == "ask_human"


def test_middleware_bad_args_return_error_not_interrupt(monkeypatch):
    # A malformed ask_human must NOT interrupt — it returns an error ToolMessage so
    # the model can re-issue a valid call.
    def boom(_):
        raise AssertionError("interrupt must not be called for bad args")

    import langgraph.types as lgt

    monkeypatch.setattr(lgt, "interrupt", boom)

    mw = AskHumanMiddleware()
    handler, _ = _handler_marker()
    out = mw.wrap_tool_call(_Req("ask_human", {"questions": []}), handler)
    body = json.loads(out.content)
    assert body["status"] == "error"
    assert out.status == "error"


# --- server: interrupt updates are DROPPED (chunk built from state) ----------


def test_process_stream_data_drops_interrupt_updates():
    # The ask_human chunk is now built AFTER the stream from checkpoint state (it
    # carries interrupt ids), so a raw __interrupt__ update is dropped here.
    intr = types.SimpleNamespace(value={"type": "ask_human", "questions": [{"id": "a"}]})
    assert server.process_stream_data("updates", {"__interrupt__": (intr,)}) is None


# --- server: ask_human chunk built from graph state (with ids) ---------------


class _FakeInterrupt:
    def __init__(self, iid, value):
        self.id = iid
        self.value = value


class _FakeTask:
    def __init__(self, interrupts):
        self.interrupts = interrupts


class _FakeGraph:
    """Minimal graph whose get_state exposes pending interrupts across tasks."""

    def __init__(self, tasks):
        self._tasks = tasks

    def get_state(self, _cfg):
        return types.SimpleNamespace(tasks=self._tasks)


def _graph_with_interrupts(*groups):
    # groups: list of (interrupt_id, questions) → one interrupt each, one task.
    tasks = [
        _FakeTask([_FakeInterrupt(iid, {"type": "ask_human", "questions": qs})])
        for iid, qs in groups
    ]
    return _FakeGraph(tasks)


def test_ask_human_chunk_from_state_single():
    g = _graph_with_interrupts(("i1", [{"id": "grain", "prompt": "Which?"}]))
    chunk = server._ask_human_chunk_from_state(g, {})
    assert chunk["type"] == "ask_human"
    assert chunk["interrupt_ids"] == ["i1"]
    # Each question is tagged with its owning interrupt id.
    assert chunk["questions"][0]["interrupt_id"] == "i1"
    assert chunk["questions"][0]["id"] == "grain"


def test_ask_human_chunk_from_state_consolidates_multiple():
    g = _graph_with_interrupts(
        ("i1", [{"id": "a", "prompt": "A?"}]),
        ("i2", [{"id": "b", "prompt": "B?"}]),
    )
    chunk = server._ask_human_chunk_from_state(g, {})
    assert chunk["interrupt_ids"] == ["i1", "i2"]
    ids = {q["id"]: q["interrupt_id"] for q in chunk["questions"]}
    assert ids == {"a": "i1", "b": "i2"}


def test_ask_human_chunk_from_state_none_when_not_paused():
    assert server._ask_human_chunk_from_state(_FakeGraph([]), {}) is None


# --- server: resume map (id-keyed, required for multi-interrupt) --------------


def test_build_resume_map_routes_answers_by_interrupt_id():
    g = _graph_with_interrupts(("i1", [{"id": "a"}]), ("i2", [{"id": "b"}]))
    answers = [
        {"id": "a", "answer": "AA", "interrupt_id": "i1"},
        {"id": "b", "answer": "BB", "interrupt_id": "i2"},
    ]
    m = server._build_resume_map(g, {}, answers)
    assert set(m) == {"i1", "i2"}
    assert m["i1"][0]["answer"] == "AA"
    assert m["i2"][0]["answer"] == "BB"


def test_build_resume_map_untagged_answers_go_to_sole_interrupt():
    g = _graph_with_interrupts(("i1", [{"id": "a"}]))
    m = server._build_resume_map(g, {}, [{"id": "a", "answer": "AA"}])
    assert m == {"i1": [{"id": "a", "answer": "AA"}]}


def test_build_resume_map_none_when_no_pending_interrupt():
    # THE phantom-turn guard: resuming a non-paused graph must return None so the
    # caller ends cleanly instead of injecting a fresh turn.
    assert server._build_resume_map(_FakeGraph([]), {}, [{"id": "a", "answer": "x"}]) is None
