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

    def guard_write_file(self, content, existing, rel_path=None):
        return types.SimpleNamespace(
            allow=True, new_content=content + "\n<normalized>", message=None
        )

    def guard_edit_file(self, old, new, existing, rel_path=None):
        return types.SimpleNamespace(allow=True, new_content=None, message="")


class _DenyEngine:
    def guard_write_file(self, content, existing, rel_path=None):
        return types.SimpleNamespace(
            allow=False, new_content=None, message="nope: missing title"
        )

    def guard_edit_file(self, old, new, existing, rel_path=None):
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


def test_delete_is_wired_to_every_supervisor_guard_but_no_subagent():
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    # ONE guard instance carries allow_delete — the supervisor's, in EVERY
    # mode (a scoped run must retire a DROPPED table's doc; cross deletes are
    # confined to the pair subtree by writable_prefix). It must be
    # unconditional: no `if full_harvest else guard` demotion.
    assert "allow_delete=True," in src
    assert "main_middleware = [ main_guard," in " ".join(src.split())
    assert "else guard" not in src
    # Sub-agent specs keep the plain `guard`, which refuses delete.
    assert src.count('"middleware": [guard, tool_errors, prompt_cache]') >= 2


def test_supervisor_prompts_explain_when_to_delete():
    from harvest import prompts

    p = prompts.SUPERVISOR_PROMPT
    assert "`delete`" in p
    norm = " ".join(p.split())
    # It is the stale-doc remedy, backlinks first, one .md file, never a dir.
    assert "stale-table-doc" in norm
    assert "get_backlinks" in norm
    assert "never a directory" in norm
    # The scoped supervisors (maintenance + annotation) get the tool too, so
    # their prompts must say when to reach for it (a DROPPED table's doc).
    for scoped in (
        prompts.build_maintenance_supervisor_prompt(),
        prompts.build_annotation_supervisor_prompt(results_rel=".harvest/annotation_results.json"),
    ):
        s = " ".join(scoped.split())
        assert "`delete`" in s
        assert "DROPPED" in s
        assert "get_backlinks" in s
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
    # The read-only delete refusal must NOT tell the agent to use edit_file —
    # that instruction would just earn a second refusal from the read-only
    # branch. It says report-in-reply instead.
    req = _request(name="delete")
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert "edit_file" not in result["content"]
    assert "READ-ONLY" in result["content"]
    assert "reply" in result["content"]
    # Non-guarded tools (reads) are untouched.
    req = _request(name="read_file")
    assert mw.wrap_tool_call(req, lambda r: "READ") == "READ"


# --------------------------------------------------------------------------- #
# write_allowlist — the fix-author variant: writes only to the paths of the
# review cluster bound to the CURRENT dispatch (run_review's contextvar), and
# FAIL CLOSED when nothing is bound.
# --------------------------------------------------------------------------- #


def test_write_allowlist_confines_the_fixer(monkeypatch):
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
    )
    allowed = {"value": frozenset({"tables/races.md"})}
    mw = OKFGuardMiddleware(
        _AllowEngine(),
        read_current=lambda _p: None,
        write_allowlist=lambda: allowed["value"],
    )
    # In-cluster write proceeds (and still runs the engine's OKF checks).
    req = _request(file_path="tables/races.md", content="x")
    assert mw.wrap_tool_call(req, lambda r: "WROTE") == "WROTE"
    assert req.tool_call["args"]["content"].endswith("<normalized>")

    # Out-of-cluster write refused with the propagation-note instruction —
    # including a non-.md path (a fixer writes nothing outside its cluster)
    # and a dot-segment path aimed at an allowed doc.
    for path in ("tables/results.md", "notes.txt", "tables/../datasets/x.md"):
        req = _request(name="edit_file", file_path=path, old_string="a", new_string="b")
        result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
        assert isinstance(result, dict) and result["status"] == "error"
        assert "PROPAGATION NOTES" in result["content"]

    # Fail closed: no cluster bound (the contextvar default) = every write
    # refused, with a message that says why.
    allowed["value"] = None
    req = _request(file_path="tables/races.md", content="x")
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert result["status"] == "error"
    assert "no review cluster is bound" in result["content"]

    # Reads are untouched either way.
    req = _request(name="read_file", file_path="tables/results.md")
    assert mw.wrap_tool_call(req, lambda r: "READ") == "READ"


# --------------------------------------------------------------------------- #
# SubagentDispatchGuard — the model may not dispatch workflow-only sub-agent
# types (fix-author) itself; run_review's direct task-tool calls bypass this
# middleware by construction.
# --------------------------------------------------------------------------- #

from harvest.okf_guard import SubagentDispatchGuard  # noqa: E402


def test_dispatch_guard_refuses_fix_author_sync_and_async(monkeypatch):
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
    )
    mw = SubagentDispatchGuard()
    req = types.SimpleNamespace(
        tool_call={
            "name": "task",
            "args": {"subagent_type": "fix-author", "description": "fix stuff"},
            "id": "call-1",
        }
    )
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert result["status"] == "error"
    assert "run_review" in result["content"]
    assert "edit_file" in result["content"]

    async def handler(r):
        return "SHOULD NOT RUN"

    result = asyncio.run(mw.awrap_tool_call(req, handler))
    assert result["status"] == "error"


def test_dispatch_guard_passes_other_dispatches_and_tools():
    mw = SubagentDispatchGuard()
    for name, args in (
        ("task", {"subagent_type": "reviewer"}),
        ("task", {"subagent_type": "table-author"}),
        ("write_file", {"file_path": "tables/x.md"}),
    ):
        req = types.SimpleNamespace(
            tool_call={"name": name, "args": args, "id": "call-1"}
        )
        assert mw.wrap_tool_call(req, lambda r: "RAN") == "RAN"


def test_dispatch_guard_is_wired_to_the_main_agent():
    import inspect

    from harvest import agent as ag

    src = inspect.getsource(ag.build_harvest_agent)
    assert "SubagentDispatchGuard()," in src


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


def test_writes_into_any_dot_dir_are_refused(monkeypatch):
    # .harvest/ (review clustering, recorded digests, the commit marker) and
    # .context/ (user uploads) are workflow state/inputs — a write into either
    # (ANY extension: clusters.json is not markdown) must be refused like
    # .metadata/, not silently allowed past the .md-only checks.
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
    )
    mw = _mw(_AllowEngine())
    for name, extra in (
        ("write_file", {"content": "{}"}),
        ("edit_file", {"old_string": "a", "new_string": "b"}),
    ):
        for path in (
            ".harvest/review/clusters.json",
            ".harvest/context/digest-01.md",
            ".harvest/state.json",
            ".context/spec.md",
            "tables/../.harvest/state.json",
        ):
            req = _request(name=name, file_path=path, **extra)
            result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
            assert isinstance(result, dict) and result["status"] == "error", (name, path)
            assert "reserved dot-directory" in result["content"]


def test_allow_delete_refuses_a_directory_named_like_a_doc(tmp_path, monkeypatch):
    # The name check alone doesn't prove "one .md FILE": the backend mkdirs
    # parents on write, so `tables/x.md` can EXIST as a directory — and delete
    # is recursive. The guard stats the path through the engine's link-graph
    # root and refuses directories regardless of their name.
    _tm_standin(monkeypatch)
    (tmp_path / "tables" / "x.md").mkdir(parents=True)  # a DIRECTORY named x.md
    engine = _AllowEngine()
    engine.link_graph = types.SimpleNamespace(root=tmp_path)
    mw = OKFGuardMiddleware(engine, read_current=lambda _p: None, allow_delete=True)
    req = _request(name="delete", file_path="tables/x.md")
    result = mw.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert isinstance(result, dict) and result["status"] == "error"
    assert "DIRECTORY" in result["content"]
    # A real file of the same shape still deletes.
    (tmp_path / "tables" / "y.md").write_text("---\ntype: T\n---\n")
    req = _request(name="delete", file_path="tables/y.md")
    assert mw.wrap_tool_call(req, lambda r: "DELETED") == "DELETED"


def test_annotation_results_file_is_the_one_writable_dot_path(monkeypatch):
    # The annotation supervisor is REQUIRED to write its verdict file under
    # .harvest/ (the runner reconciles it to DynamoDB) — the dot-dir write
    # refusal must exempt exactly that path, or every annotation run would
    # silently revert its notes to open. Pinned to the runner's constant so
    # the two literals can't drift.
    from harvest.runner import ANNOTATION_RESULTS_REL

    assert okf_guard._ANNOTATION_RESULTS_REL == ANNOTATION_RESULTS_REL
    monkeypatch.setattr(
        okf_guard,
        "ToolMessage",
        lambda content, tool_call_id, status="success": {"content": content, "id": tool_call_id, "status": status},
    )
    mw = _mw(_AllowEngine())
    req = _request(
        name="write_file",
        file_path=ANNOTATION_RESULTS_REL,
        content='[{"annotation_id": "a1"}]',
    )
    # Not markdown, not refused: passes through to the handler.
    assert mw.wrap_tool_call(req, lambda r: "WROTE") == "WROTE"
    # Read-only agents still cannot write it.
    ro = OKFGuardMiddleware(_AllowEngine(), read_current=lambda _p: None, read_only=True)
    result = ro.wrap_tool_call(req, lambda r: "SHOULD NOT RUN")
    assert isinstance(result, dict) and result["status"] == "error"
