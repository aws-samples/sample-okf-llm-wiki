"""harvest.subagent_io — the QuickJS ``task()`` I/O forwarding shim.

Unit tests exercise the shim against duck-typed fakes (no langchain import
needed — that IS the design); the install test drives the REAL
``langchain_quickjs._repl.call_subagent_task_tool`` end-to-end with a fake
task tool and asserts the enriched event order on the wire.
"""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from harvest.subagent_io import (
    _TaskToolShim,
    _result_text,
    install_quickjs_io_forwarding,
)


class _Msg:
    def __init__(self, content: Any) -> None:
        self.content = content


class _Cmd:
    def __init__(self, update: Any) -> None:
        self.update = update


# ---------------------------------------------------------------------------
# _result_text — final-answer extraction across the deepagents return shapes
# ---------------------------------------------------------------------------


def test_result_text_command_shape_walks_back_to_last_nonempty():
    cmd = _Cmd({"messages": [_Msg("the real answer"), _Msg("")]})
    assert _result_text(cmd) == "the real answer"


def test_result_text_plain_string_and_message():
    assert _result_text("cannot invoke subagent nope") == (
        "cannot invoke subagent nope"
    )
    assert _result_text(_Msg("direct message")) == "direct message"


def test_result_text_block_content():
    msg = _Msg([{"type": "text", "text": "part one"}, {"type": "tool_use"}, "two"])
    assert _result_text(_Cmd({"messages": [msg]})) == "part one two"


def test_result_text_garbage_degrades_to_empty():
    assert _result_text(None) == ""
    assert _result_text(_Cmd("not a dict")) == ""
    assert _result_text(_Cmd({"messages": "not a list"})) == ""
    assert _result_text(object()) == ""


# ---------------------------------------------------------------------------
# _TaskToolShim — event emission around the delegated arun
# ---------------------------------------------------------------------------


class _Inner:
    name = "task"

    def __init__(self, result: Any) -> None:
        self._result = result
        self.payloads: list[Any] = []

    async def arun(self, payload: Any, **kwargs: Any) -> Any:
        self.payloads.append(payload)
        return self._result


def _payload(runtime: Any, description: str = "Author tables/cards.md") -> dict:
    return {
        "description": description,
        "subagent_type": "table-author",
        "runtime": runtime,
    }


def test_shim_emits_input_then_result_with_the_ptc_id():
    events: list[dict] = []
    inner = _Inner(_Cmd({"messages": [_Msg("done: cards.md")]}))
    rt = SimpleNamespace(tool_call_id="ptc_task_ab12cd34", stream_writer=events.append)

    shim = _TaskToolShim(inner)
    assert shim.name == "task"
    out = asyncio.run(shim.arun(_payload(rt)))

    assert out is inner._result  # the result passes through untouched
    assert events == [
        {
            "type": "subagent",
            "phase": "input",
            "id": "ptc_task_ab12cd34",
            "input": "Author tables/cards.md",
        },
        {
            "type": "subagent",
            "phase": "result",
            "id": "ptc_task_ab12cd34",
            "result": "done: cards.md",
        },
    ]


def test_shim_passes_unknown_attributes_through_to_the_wrapped_tool():
    """langchain-quickjs is pinned with no upper bound — a future release
    touching any attribute beyond .name/.arun must reach the real tool, not
    AttributeError every dispatch."""
    inner = _Inner(_Cmd({"messages": [_Msg("x")]}))
    inner.description = "dispatch a subagent"
    inner.args_schema = {"description": str}
    shim = _TaskToolShim(inner)
    assert shim.description == "dispatch a subagent"
    assert shim.args_schema == {"description": str}
    with pytest.raises(AttributeError):
        _ = shim.does_not_exist_anywhere


def test_shim_without_id_or_writer_is_silent():
    events: list[dict] = []
    inner = _Inner(_Cmd({"messages": [_Msg("answer")]}))

    # No tool_call_id on the runtime → no correlation key → no events.
    rt = SimpleNamespace(tool_call_id=None, stream_writer=events.append)
    out = asyncio.run(_TaskToolShim(inner).arun(_payload(rt)))
    assert out is inner._result
    assert events == []

    # No stream_writer → nowhere to emit; still returns the result.
    rt = SimpleNamespace(tool_call_id="ptc_task_x")
    out = asyncio.run(_TaskToolShim(inner).arun(_payload(rt)))
    assert out is inner._result


def test_shim_writer_failure_never_breaks_the_dispatch():
    def boom(_event: dict) -> None:
        raise RuntimeError("stream closed")

    inner = _Inner(_Cmd({"messages": [_Msg("answer")]}))
    rt = SimpleNamespace(tool_call_id="ptc_task_x", stream_writer=boom)
    out = asyncio.run(_TaskToolShim(inner).arun(_payload(rt)))
    assert out is inner._result


def test_shim_skips_empty_texts():
    events: list[dict] = []
    inner = _Inner(_Cmd({"messages": [_Msg("")]}))
    rt = SimpleNamespace(tool_call_id="ptc_task_x", stream_writer=events.append)
    asyncio.run(_TaskToolShim(inner).arun(_payload(rt, description="  ")))
    assert events == []


def test_shim_blocks_workflow_only_dispatches_from_eval():
    """fix-author cannot be dispatched from eval task() — the shim is the one
    interception point for that path (agent middleware never sees it). The
    refusal raises BEFORE any dispatch or event."""
    events: list[dict] = []
    inner = _Inner(_Cmd({"messages": [_Msg("should never run")]}))
    rt = SimpleNamespace(tool_call_id="ptc_task_x", stream_writer=events.append)
    payload = {
        "description": "fix tables/a",
        "subagent_type": "fix-author",
        "runtime": rt,
    }
    with pytest.raises(RuntimeError, match="run_review"):
        asyncio.run(_TaskToolShim(inner).arun(payload))
    assert inner.payloads == []  # never dispatched
    assert events == []  # no lifecycle noise for a refused dispatch


def test_shim_propagates_inner_errors_after_input_event():
    """A raising dispatch still surfaces to the library (which emits its own
    error lifecycle event) — the shim adds only the input enrichment."""
    events: list[dict] = []

    class _Raising:
        name = "task"

        async def arun(self, payload: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider 400")

    rt = SimpleNamespace(tool_call_id="ptc_task_x", stream_writer=events.append)
    with pytest.raises(RuntimeError, match="provider 400"):
        asyncio.run(_TaskToolShim(_Raising()).arun(_payload(rt)))
    assert [e["phase"] for e in events] == ["input"]


# ---------------------------------------------------------------------------
# install_quickjs_io_forwarding — through the real library
# ---------------------------------------------------------------------------


def test_install_forwards_through_the_real_library():
    pytest.importorskip("langchain_quickjs")
    from langchain_core.messages import ToolMessage
    from langchain_quickjs import _repl
    from langgraph.types import Command

    original = _repl.call_subagent_task_tool
    try:
        assert install_quickjs_io_forwarding() is True
        assert install_quickjs_io_forwarding() is True  # idempotent, no re-wrap
        patched = _repl.call_subagent_task_tool
        assert patched is not original

        events: list[dict] = []

        @dataclass
        class Runtime:  # dataclass: the library rewraps via dataclasses.replace
            tool_call_id: str | None = None
            stream_writer: Any = None

        class FakeTaskTool:
            name = "task"

            async def arun(self, payload: Any, **kwargs: Any) -> Any:
                return Command(
                    update={
                        "messages": [
                            ToolMessage("reviewed: ok", tool_call_id="ignored")
                        ]
                    }
                )

        out = asyncio.run(
            patched(
                FakeTaskTool(),
                description="Adversarially verify tables/cards and tables/sets",
                subagent_type="reviewer",
                response_schema=None,
                runtime=Runtime(tool_call_id="call_eval_1", stream_writer=events.append),
                label="reviewer wave 1",
            )
        )
        assert out == "reviewed: ok"

        phases = [e["phase"] for e in events]
        assert phases == ["start", "input", "result", "complete"]
        # Every event carries the SAME freshly minted per-dispatch id.
        ids = {e["id"] for e in events}
        assert len(ids) == 1
        assert ids.pop().startswith("ptc_task_")
        assert events[1]["input"].startswith("Adversarially verify")
        assert events[2]["result"] == "reviewed: ok"
        # The library's own events are untouched (grouping + truncation intact).
        assert events[0]["eval_id"] == "call_eval_1"
        assert events[0]["label"] == "reviewer wave 1"
    finally:
        _repl.call_subagent_task_tool = original
