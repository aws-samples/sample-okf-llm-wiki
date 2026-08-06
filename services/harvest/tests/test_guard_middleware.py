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
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
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
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
    )
    mw = _mw(_AllowEngine())  # engine would allow, but the path check fires first
    called = {"n": 0}

    def handler(r):
        called["n"] += 1
        return "SHOULD NOT RUN"

    req = _request(
        name="write_file",
        file_path=".metadata/tables/races.md",
        content="---\ntype: Glue Table\n---\n",
    )
    result = mw.wrap_tool_call(req, handler)
    assert called["n"] == 0  # short-circuited before the handler
    assert isinstance(result, dict) and result["status"] == "error"
    assert "read-only" in result["content"]


def test_edit_into_metadata_dir_also_refused(monkeypatch):
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
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


def test_delete_tool_refused_without_allow_delete(monkeypatch):
    # deepagents ≥0.7 exposes a recursive `delete` fs tool to every agent whose
    # backend supports it. The DEFAULT guard (authoring sub-agents) refuses it
    # unconditionally, for ANY path or extension, before the engine is even
    # consulted — only the supervisor's allow_delete variant may remove a doc.
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
    )
    mw = _mw(_AllowEngine())  # engine would allow writes; delete never reaches it
    for path in ("tables/races.md", "notes.txt", "external/crm/customers"):
        req = _request(name="delete", file_path=path)
        result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
        assert isinstance(result, dict) and result["status"] == "error"
        assert "delete" in result["content"]


def _delete_mw(**kw):
    return OKFGuardMiddleware(
        _AllowEngine(), read_current=lambda _p: None, allow_delete=True, **kw
    )


def test_allow_delete_permits_retiring_one_markdown_doc(monkeypatch):
    # The supervisor's variant: a stale `.md` concept doc (a table dropped from
    # the source) may be retired, so the handler actually runs.
    _tm_standin(monkeypatch)
    mw = _delete_mw()
    req = _request(name="delete", file_path="tables/dropped_table.md")
    assert mw.wrap_tool_call(req, lambda r: "DELETED") == "DELETED"


def test_allow_delete_refuses_a_directory_path(monkeypatch):
    # `delete` is RECURSIVE: a directory path would take the whole subtree.
    # That blast radius is exactly what the blanket refusal existed to prevent,
    # so even the supervisor may only name a single .md file.
    _tm_standin(monkeypatch)
    mw = _delete_mw()
    for path in ("tables", "references/joins", "notes.txt", ""):
        req = _request(name="delete", file_path=path)
        result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
        assert isinstance(result, dict) and result["status"] == "error", path
        assert "ONE `.md`" in result["content"]


def test_allow_delete_refuses_dot_directories(monkeypatch):
    # .metadata/ (snapshot), .context/ (user uploads) and .harvest/ (state) are
    # the run's INPUTS — never the agent's to remove, even with delete allowed.
    _tm_standin(monkeypatch)
    mw = _delete_mw()
    for path in (
        ".metadata/tables/races.md",
        ".context/spec.md",
        ".harvest/state.json",
        "/.metadata/index.md",
        "tables/../.context/notes.md",
    ):
        req = _request(name="delete", file_path=path)
        result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
        assert isinstance(result, dict) and result["status"] == "error", path
        assert "dot-directory" in result["content"]


def test_read_only_beats_allow_delete(monkeypatch):
    # A read-only sub-agent can never delete, whatever flags it was built with.
    _tm_standin(monkeypatch)
    mw = OKFGuardMiddleware(
        _AllowEngine(),
        read_current=lambda _p: None,
        read_only=True,
        allow_delete=True,
    )
    req = _request(name="delete", file_path="tables/races.md")
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert isinstance(result, dict) and result["status"] == "error"


def test_allow_delete_still_honors_the_writable_prefix(monkeypatch):
    # Cross-mode confinement applies to deletes too, not just writes.
    _tm_standin(monkeypatch)
    mw = _delete_mw(writable_prefix="external/crm/customers")
    inside = _request(name="delete", file_path="external/crm/customers/joins/a__b.md")
    assert mw.wrap_tool_call(inside, lambda r: "DELETED") == "DELETED"
    outside = _request(name="delete", file_path="tables/races.md")
    result = mw.wrap_tool_call(outside, lambda r: "SHOULD NOT RUN")
    assert isinstance(result, dict) and result["status"] == "error"
    assert "outside this run's writable subtree" in result["content"]


def test_delete_is_wired_to_the_supervisor_guard_only():
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    # A separate guard instance carries allow_delete, used by the MAIN
    # middleware only; sub-agent specs keep the plain `guard`.
    assert "allow_delete=True," in src
    assert "main_middleware = [main_guard," in src
    assert src.count('"middleware": [guard, tool_errors, prompt_cache]') >= 2
    # Full harvests only (scoped/cross prompts never mention the tool).
    assert "full_harvest = cross_target is None and supervisor_prompt is None" in src


def test_supervisor_prompt_explains_when_to_delete():
    from harvest import prompts

    p = prompts.SUPERVISOR_PROMPT
    assert "`delete`" in p
    norm = " ".join(p.split())
    # It is the stale-doc remedy, backlinks first, one .md file, never a dir.
    assert "stale-table-doc" in norm
    assert "get_backlinks" in norm
    assert "never a directory" in norm
    # Sub-agents' prompts must NOT advertise a tool they cannot use.
    for other in (prompts.TABLE_AUTHOR_PROMPT, prompts.REVIEWER_PROMPT):
        assert "`delete`" not in other


def test_read_only_guard_refuses_all_writes(monkeypatch):
    # The reviewer/context-extractor variant: verify-and-report agents get a
    # guard that refuses EVERY write/edit at the tool boundary (deepagents
    # hands every sub-agent the backend's write tools, so read-only can't be
    # left to the prompt). Reads/other tools pass through untouched.
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
    )
    mw = OKFGuardMiddleware(
        _AllowEngine(), read_current=lambda _p: None, read_only=True
    )
    for name, args in (
        ("write_file", {"content": "x"}),
        ("edit_file", {"old_string": "a", "new_string": "b"}),
        ("delete", {}),
    ):
        req = _request(name=name, **args)
        result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
        assert isinstance(result, dict) and result["status"] == "error"
    # Non-guarded tools (reads) are untouched.
    req = _request(name="read_file")
    assert mw.wrap_tool_call(req, lambda r: "READ") == "READ"


# --------------------------------------------------------------------------- #
# ToolErrorMiddleware — a raising tool must become an error ToolMessage, not a
# crashed harvest (the PermissionError-on-the-mount incident).
# --------------------------------------------------------------------------- #

from harvest.okf_guard import ToolErrorMiddleware  # noqa: E402


def _tm_standin(monkeypatch):
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {
            "content": content,
            "id": tool_call_id,
            "status": status,
        },
    )


def test_tool_raise_becomes_error_message_sync(monkeypatch):
    _tm_standin(monkeypatch)
    mw = ToolErrorMiddleware()
    req = _request(name="write_file", content="x")

    def handler(r):
        raise PermissionError(13, "Permission denied", "/mnt/data/x.md")

    result = mw.wrap_tool_call(req, handler)
    assert result["status"] == "error"
    assert "PermissionError" in result["content"]
    assert "write_file" in result["content"]
    assert result["id"] == "call-1"


def test_tool_raise_becomes_error_message_async(monkeypatch):
    # The QuickJS task() fan-out drives the ASYNC tool path — the safety net
    # must cover it too (same requirement as the guard's awrap_tool_call).
    _tm_standin(monkeypatch)
    mw = ToolErrorMiddleware()
    req = _request(name="edit_file", old_string="a", new_string="b")

    async def handler(r):
        raise OSError("stale file handle")

    result = asyncio.run(mw.awrap_tool_call(req, handler))
    assert result["status"] == "error"
    assert "OSError" in result["content"]


def test_tool_success_passes_through(monkeypatch):
    _tm_standin(monkeypatch)
    mw = ToolErrorMiddleware()
    req = _request()
    assert mw.wrap_tool_call(req, lambda r: "WROTE") == "WROTE"

    async def handler(r):
        return "WROTE"

    assert asyncio.run(mw.awrap_tool_call(req, handler)) == "WROTE"


def test_langgraph_control_flow_exceptions_propagate(monkeypatch):
    # Interrupt/Command exceptions are the graph's own mechanics — converting
    # one to a ToolMessage would break routing, so they must re-raise.
    _tm_standin(monkeypatch)

    class FakeInterrupt(Exception):
        pass

    FakeInterrupt.__module__ = "langgraph.errors"
    mw = ToolErrorMiddleware()
    req = _request()

    def handler(r):
        raise FakeInterrupt("interrupt")

    try:
        mw.wrap_tool_call(req, handler)
        raise AssertionError("control-flow exception was swallowed")
    except FakeInterrupt:
        pass


def test_guard_refusal_carries_error_status(monkeypatch):
    # Guard rejections are failed calls the model must self-correct — they now
    # carry status="error" so the model (and the feed's ok flag) treat them so.
    _tm_standin(monkeypatch)
    mw = _mw(_DenyEngine())
    req = _request(content="bad")
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert result["status"] == "error"
    assert "nope" in result["content"]
