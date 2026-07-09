"""OKFGuardMiddleware must work in BOTH sync and async tool paths.

The dynamic-subagent task() fan-out runs subagents concurrently, driving
deepagents down the async tool path — so the guard needs awrap_tool_call, else
the first guarded write raises NotImplementedError (the runtime crash this
guards against). langchain isn't installed in the test venv (the middleware base
falls back to ``object``), so these exercise our own methods directly with fakes.
"""

from __future__ import annotations

import asyncio
import types

import harvest.okf_guard as okf_guard
from harvest.okf_guard import OKFGuardMiddleware


class _AllowEngine:
    """Guard engine stub: write allowed with a normalized content rewrite."""

    def guard_write_file(self, content, existing):
        return types.SimpleNamespace(
            allow=True, new_content=content + "\n<normalized>", message=None
        )

    def guard_edit_file(self, old, new, existing):
        return types.SimpleNamespace(allow=True, new_content=None, message="")


class _DenyEngine:
    def guard_write_file(self, content, existing):
        return types.SimpleNamespace(
            allow=False, new_content=None, message="nope: missing title"
        )

    def guard_edit_file(self, old, new, existing):
        return types.SimpleNamespace(allow=False, new_content=None, message="nope")


def _request(name="write_file", **args):
    args.setdefault("file_path", "tables/races.md")
    return types.SimpleNamespace(tool_call={"name": name, "args": args, "id": "call-1"})


def _mw(engine):
    return OKFGuardMiddleware(engine, read_current=lambda _p: None)


def test_async_method_exists():
    # The whole point: the async variant must be defined (else NotImplementedError).
    assert hasattr(OKFGuardMiddleware, "awrap_tool_call")


def test_awrap_awaits_handler_on_allow():
    mw = _mw(_AllowEngine())
    req = _request(content="---\ntype: Glue Table\n---\n")
    awaited = {"n": 0}

    async def handler(r):
        awaited["n"] += 1
        return "WROTE"

    result = asyncio.run(mw.awrap_tool_call(req, handler))
    assert result == "WROTE"
    assert awaited["n"] == 1
    # Normalization rewrite still applied via the shared _prepare path.
    assert req.tool_call["args"]["content"].endswith("<normalized>")


def test_sync_and_async_share_normalization():
    mw = _mw(_AllowEngine())
    req = _request(content="body")
    mw.wrap_tool_call(req, lambda r: "ok")
    assert req.tool_call["args"]["content"].endswith("<normalized>")


def test_awrap_refuses_without_calling_handler(monkeypatch):
    # Provide a ToolMessage stand-in (langchain absent in the test venv).
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id: {"content": content, "id": tool_call_id},
    )
    mw = _mw(_DenyEngine())
    req = _request(content="bad")
    called = {"n": 0}

    async def handler(r):
        called["n"] += 1
        return "SHOULD NOT RUN"

    result = asyncio.run(mw.awrap_tool_call(req, handler))
    assert called["n"] == 0  # short-circuited, handler never awaited
    assert "nope" in result["content"]


def test_awrap_passthrough_for_non_markdown():
    mw = _mw(_DenyEngine())  # would deny, but a .txt path isn't guarded
    req = _request(name="write_file", file_path="notes.txt", content="x")

    async def handler(r):
        return "PASSED"

    assert asyncio.run(mw.awrap_tool_call(req, handler)) == "PASSED"


def test_metadata_dir_is_read_only(monkeypatch):
    # Any write into the .metadata/ snapshot is refused (it's a read-only input).
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id: {"content": content, "id": tool_call_id},
    )
    mw = _mw(_AllowEngine())  # engine would allow, but the path check fires first
    called = {"n": 0}

    def handler(r):
        called["n"] += 1
        return "SHOULD NOT RUN"

    for path in (
        ".metadata/tables/races.md",
        "/.metadata/columns.tsv",
        ".metadata/index.md",
    ):
        req = _request(name="write_file", file_path=path, content="x")
        result = mw.wrap_tool_call(req, handler)
        assert isinstance(result, dict), f"{path} should be refused"
        assert "read-only" in result["content"]
    assert called["n"] == 0  # handler never ran for any metadata path


def test_edit_into_metadata_dir_also_refused(monkeypatch):
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id: {"content": content, "id": tool_call_id},
    )
    mw = _mw(_AllowEngine())
    req = _request(
        name="edit_file",
        file_path=".metadata/tables/races.md",
        old_string="a",
        new_string="b",
    )
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert isinstance(result, dict)
    assert "read-only" in result["content"]
