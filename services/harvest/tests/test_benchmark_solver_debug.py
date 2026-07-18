"""Solver observability: _emit_solver_debug emits a compact, gold-free event."""

from __future__ import annotations

import types

from harvest.benchmark.solver import _emit_solver_debug


def _ai(content, tool_calls=None):
    m = types.SimpleNamespace(content=content, type="ai")
    if tool_calls is not None:
        m.tool_calls = tool_calls
    return m


def test_emit_counts_turns_tool_calls_and_sql():
    events = []
    messages = [
        _ai("", tool_calls=[{"name": "glob"}, {"name": "read_file"}]),
        _ai("", tool_calls=[{"name": "read_file"}]),
        _ai("```sql\nSELECT 1\n```"),
    ]
    _emit_solver_debug(events.append, messages=messages, sql="SELECT 1", error="")
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "benchmark_solver"
    assert e["turns"] == 3
    assert e["tool_calls"] == 3
    assert e["sql_len"] == len("SELECT 1")
    assert e["sql_preview"] == "SELECT 1"
    assert e["error"] == ""


def test_emit_reports_zero_reads_and_empty_sql():
    # The exact "solver answered blind" signature: no tool calls, empty SQL.
    events = []
    messages = [_ai("I cannot answer.")]
    _emit_solver_debug(events.append, messages=messages, sql="", error="")
    e = events[0]
    assert e["tool_calls"] == 0
    assert e["sql_len"] == 0
    assert e["sql_preview"] == ""


def test_emit_captures_error():
    events = []
    _emit_solver_debug(events.append, messages=[], sql="", error="ValidationException: boom")
    e = events[0]
    assert e["turns"] == 0
    assert e["error"] == "ValidationException: boom"


def test_emit_truncates_long_sql_preview():
    events = []
    long = "SELECT " + "x" * 500
    _emit_solver_debug(events.append, messages=[_ai(long)], sql=long, error="")
    assert len(events[0]["sql_preview"]) == 200
    assert events[0]["sql_len"] == len(long)


def test_emit_noop_without_callback():
    # No emitter → no crash, nothing emitted.
    _emit_solver_debug(None, messages=[_ai("x")], sql="x", error="")


def test_emit_swallows_callback_error():
    def boom(_):
        raise RuntimeError("sink down")

    # Observability must never break a solve.
    _emit_solver_debug(boom, messages=[_ai("x")], sql="x", error="")
