"""Graceful stop-streaming reconciliation for the chat agent.

When a user stops a turn mid-stream (the browser aborts the SSE fetch), the
LangGraph run is cancelled part-way. If the model had already emitted an
``AIMessage`` with ``tool_calls`` but the tools hadn't finished, the persisted
checkpoint is left with a DANGLING tool call — an ``AIMessage`` whose
``tool_calls`` have no matching ``ToolMessage``. Bedrock Converse (and the OpenAI
Responses API) REJECT such a history on the NEXT turn: a ``tool_use`` block
requires a corresponding ``tool_result``. Left unrepaired, one stop wedges the
whole conversation.

This module repairs the checkpoint after a cancellation: it reads the persisted
state, finds every tool call with no result, and schedules a write-back of a
synthetic error ``ToolMessage`` ("cancelled by user") for each on a DETACHED task
(``aupdate_state``, or sync ``update_state``) — so the next turn resumes from a
valid history. Ported from Sparky's ``cancellation_handler.py`` but adapted to our
stack: we reconcile from the PERSISTED graph state (LangGraph already wrote the
partial turn) rather than an in-memory buffer; we append the missing ToolMessages
(what Converse/Responses actually require) rather than editing tool_use blocks; and
the write is detached + GC-held (like Sparky) so it outlives the streaming task
being torn down.

Kept import-light (only ``langchain_core.messages``, imported lazily) so the
module loads in the unit venv; the checkpointer/graph are injected.
"""

from __future__ import annotations

import asyncio
from typing import Any

_CANCELLED_CONTENT = '{"response": "Tool invocation cancelled by user"}'

# Detached repair tasks, held so the event loop doesn't GC them before they finish
# (the streaming task that spawned them is being torn down). Sparky uses the same
# guard set. Discarded via a done-callback.
_repair_tasks: set = set()


def find_dangling_tool_calls(messages: list[Any]) -> list[dict[str, str]]:
    """Return ``[{id, name}]`` for tool calls in ``messages`` that have no result.

    Walks the message list, collecting every ``AIMessage`` tool-call id and every
    ``ToolMessage`` (result) tool_call_id; a call with no matching result is
    dangling. Order-independent and dedups by id (a call already answered anywhere
    in the history is not dangling).
    """
    from langchain_core.messages import AIMessage, ToolMessage

    answered: set[str] = set()
    calls: dict[str, str] = {}  # id -> name, first occurrence wins
    for msg in messages:
        if isinstance(msg, ToolMessage):
            if msg.tool_call_id:
                answered.add(msg.tool_call_id)
        elif isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                tc_name = (
                    tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                )
                if tc_id and tc_id not in calls:
                    calls[tc_id] = tc_name or "tool"
    return [
        {"id": tc_id, "name": name}
        for tc_id, name in calls.items()
        if tc_id not in answered
    ]


def build_cancellation_tool_messages(dangling: list[dict[str, str]]) -> list[Any]:
    """Synthetic error ``ToolMessage``s (one per dangling call) marking it cancelled."""
    from langchain_core.messages import ToolMessage

    return [
        ToolMessage(
            tool_call_id=d["id"],
            name=d["name"],
            status="error",
            content=_CANCELLED_CONTENT,
        )
        for d in dangling
    ]


def cancellation_tool_chunks(dangling: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Typed ``tool`` chunks (tool_start False + error) for the dangling calls —
    the same shape ``process_stream_data`` emits for a tool result, so the UI can
    close out the cancelled tool cards."""
    return [
        {
            "type": "tool",
            "id": d["id"],
            "tool_name": d["name"],
            "tool_start": False,
            "content": _CANCELLED_CONTENT,
            "error": True,
        }
        for d in dangling
    ]


def reconcile_cancelled_turn(graph: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Repair a conversation's checkpoint after a mid-stream cancellation.

    Reads the persisted state for ``cfg``'s thread and finds any dangling tool
    calls (a tool_use with no tool_result). If there are none, returns ``[]`` — a
    stop during pure text/reasoning leaves a valid history, nothing to repair. If
    there are, it schedules the write-back (synthetic ``cancelled`` ToolMessages)
    on a DETACHED background task and returns the typed chunks for those
    cancellations.

    Why detached: this is called from a streaming generator that is ITSELF being
    cancelled (the browser aborted the fetch). An inline ``await`` on the write
    would race the generator's teardown and could be cancelled before DynamoDB
    persists the fix — leaving the very dangling state we're trying to remove. A
    background task, held in ``_repair_tasks`` against GC, outlives the generator
    so the repair actually lands. Best-effort throughout: never raises, so a repair
    problem can't wedge the stop path.
    """
    try:
        state = graph.get_state(cfg)
    except Exception:  # noqa: BLE001 - no readable state → nothing to repair
        return []
    messages = (state.values or {}).get("messages", []) if state else []
    dangling = find_dangling_tool_calls(messages)
    if not dangling:
        return []

    tool_messages = build_cancellation_tool_messages(dangling)

    async def _persist() -> None:
        try:
            aupdate = getattr(graph, "aupdate_state", None)
            if aupdate is not None:
                await aupdate(cfg, {"messages": tool_messages})
            else:
                graph.update_state(cfg, {"messages": tool_messages})
        except Exception:  # noqa: BLE001 - couldn't persist; don't wedge the stop
            pass

    try:
        task = asyncio.create_task(_persist())
        _repair_tasks.add(task)
        task.add_done_callback(_repair_tasks.discard)
    except RuntimeError:
        # No running loop (shouldn't happen in the async stream path) — fall back to
        # a best-effort synchronous write so the repair still happens.
        try:
            if getattr(graph, "aupdate_state", None) is None:
                graph.update_state(cfg, {"messages": tool_messages})
        except Exception:  # noqa: BLE001
            pass

    return cancellation_tool_chunks(dangling)
