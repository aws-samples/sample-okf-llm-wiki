"""Solver observability: _emit_solver_debug emits a compact, gold-free event."""

from __future__ import annotations

import types

from harvest.benchmark.solver import _emit_solver_debug, _field, _vpath


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


# -- backend result-shape helpers (the 'dict has no attribute path' fix) ------


def test_field_reads_typeddict_items():
    # deepagents FileInfo / GrepMatch are dicts — this is the exact shape that
    # crashed every solver ("'dict' object has no attribute 'path'").
    assert _field({"path": "tables/races.md"}, "path") == "tables/races.md"
    assert _field({"path": "t.md", "line": 12, "text": "one row"}, "line") == "12"
    assert _field({"path": "t.md", "line": 12, "text": "one row"}, "text") == "one row"


def test_field_tolerates_object_items_and_missing():
    obj = types.SimpleNamespace(path="x.md")
    assert _field(obj, "path") == "x.md"
    assert _field({"path": "x"}, "missing") == ""
    assert _field(obj, "missing") == ""


def test_vpath_adds_leading_slash():
    assert _vpath("tables/races.md") == "/tables/races.md"
    assert _vpath("/tables/races.md") == "/tables/races.md"
    assert _vpath("") == "/"
    assert _vpath("/") == "/"


# -- real-backend smoke test (only where deepagents is installed) -------------

import importlib.util  # noqa: E402

import pytest  # noqa: E402

_HAVE_DEEPAGENTS = importlib.util.find_spec("deepagents") is not None


@pytest.mark.skipif(
    not _HAVE_DEEPAGENTS,
    reason="deepagents not installed here (offline test venv is --no-deps)",
)
def test_solver_read_tools_work_against_real_backend(tmp_path):
    # Exercises the ACTUAL FilesystemBackend result shapes (FileInfo/GrepMatch are
    # TypedDicts, read() returns a string) — the contract the '.path' crash
    # violated. This is the regression guard for "'dict' object has no attribute
    # 'path'"; it runs in the runtime image / any env with deepagents installed.
    from deepagents.backends import FilesystemBackend

    from harvest.benchmark.solver import _field, _vpath

    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "races.md").write_text("# races\none row per race\n")
    (tmp_path / "index.md").write_text("# index\n")

    b = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    # glob → FileInfo dicts → paths
    paths = [_field(m, "path") for m in (b.glob("**/*.md").matches or [])]
    assert any("races.md" in p for p in paths)
    # read → string content (NOT read_file().content)
    txt = b.read(_vpath("tables/races.md"))
    assert "one row per race" in txt
    # grep → GrepMatch dicts → path:line: text
    hits = [
        f"{_field(m, 'path')}:{_field(m, 'line')}: {_field(m, 'text')}"
        for m in (b.grep("race").matches or [])
    ]
    assert any("races.md" in h for h in hits)
