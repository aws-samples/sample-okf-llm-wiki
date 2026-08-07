"""Forward QuickJS ``task()`` sub-agent I/O onto the custom stream.

langchain_quickjs emits ``{type:'subagent', phase:'start'|'complete'|'error'}``
lifecycle events for every JS ``task()`` dispatch, but (verified through
0.3.5) the ``start`` event truncates the dispatch brief to 200 chars and the
``complete`` event carries only ``duration_ms`` — the sub-agent's final answer
goes back to the JS REPL and never rides the stream. The UI's fleet drill-in
(Input/Output tabs on a square) needs both, so this module shims the ONE choke
point every ``task()`` dispatch passes through
(``langchain_quickjs._repl.call_subagent_task_tool``) to emit two extra
custom-stream events, correlated by the same per-dispatch ``ptc_task_*`` id
the library mints:

    {type:'subagent', phase:'input',  id, input:  <full dispatch brief>}
    {type:'subagent', phase:'result', id, result: <final answer text>}

``input`` fires right after the library's own ``start`` (the id is only
available once the library has stamped it into the payload runtime), and
``result`` right before its ``complete``. ``StepEmitter.emit_subagent_event``
folds both into a ``phase:"update"`` step-feed event; the library's own event
stream is untouched (its contract explicitly tells consumers to tolerate
unknown phases).

Best-effort by design: :func:`install_quickjs_io_forwarding` is a no-op (with
a log line) when the library's internals have moved, and every emission is
swallowed on failure — losing the drill-in's I/O must never break a dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("okf.harvest.subagent_io")

# Marker attribute stamped on the patched function so a second install (e.g.
# two agents built in one process) is a no-op instead of a double-wrap.
_INSTALLED_MARKER = "_okf_io_forwarding"


def _content_text(content: Any) -> str:
    """Plain text of a message ``content`` (string or list-of-blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return " ".join(p for p in parts if p)
    return ""


def _result_text(result: Any) -> str:
    """Best-effort final-answer text of a deepagents ``task`` tool result.

    The tool returns ``Command(update={"messages": [ToolMessage(answer, ...)]})``
    (stable across deepagents 0.6–0.7); a refused dispatch returns a plain
    string. Duck-typed on ``.update`` / ``.content`` so no langchain import is
    needed and a shape drift degrades to "" rather than raising.
    """
    if isinstance(result, str):
        return result
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        messages = update.get("messages")
        if isinstance(messages, list):
            for entry in reversed(messages):
                text = _content_text(getattr(entry, "content", None))
                if text:
                    return text
    return _content_text(getattr(result, "content", None))


def _emit(runtime: Any, event: dict[str, Any]) -> None:
    """Emit one enrichment event on the runtime's custom-stream writer.

    Any failure is swallowed: observability must never break the dispatch."""
    writer = getattr(runtime, "stream_writer", None)
    if writer is None:
        return
    try:
        writer(event)
    except Exception:  # noqa: BLE001 — never break the dispatch
        logger.debug("Failed to emit subagent I/O event", exc_info=True)


class _TaskToolShim:
    """Duck-typed stand-in for the deepagents ``task`` tool.

    ``call_subagent_task_tool`` touches exactly two attributes: ``.name`` (to
    mint the ``ptc_task_*`` id) and ``.arun(payload)``. Both delegate to the
    real tool; ``arun`` additionally emits the I/O enrichment events. The
    per-dispatch id is read off ``payload["runtime"].tool_call_id`` — the
    library re-wraps the runtime with the freshly minted id *before* calling
    ``arun``, which is exactly why the interception happens here and not in
    an outer wrapper (outside ``arun`` the id does not exist yet).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, attr: str) -> Any:
        # Full passthrough: today the library touches only .name and .arun,
        # but langchain-quickjs is pinned with no upper bound — a future
        # release reading any other tool attribute must hit the real tool,
        # not an AttributeError that breaks every dispatch. (Only called for
        # attributes not defined on the shim itself.)
        return getattr(self._inner, attr)

    @property
    def name(self) -> str:
        return self._inner.name

    async def arun(self, payload: Any, **kwargs: Any) -> Any:
        # Workflow-only sub-agent types (fix-author) cannot be dispatched from
        # eval task() — this shim is the ONE interception point for that path
        # (agent middleware never sees eval-borne dispatches). run_review's
        # dispatches call the task tool directly, bypassing this shim. The
        # raise surfaces to the JS promise as an instructive error.
        from harvest.dispatch_policy import (
            WORKFLOW_ONLY_DISPATCH_MSG,
            WORKFLOW_ONLY_SUBAGENTS,
        )

        sub_type = payload.get("subagent_type") if isinstance(payload, dict) else None
        if sub_type in WORKFLOW_ONLY_SUBAGENTS:
            raise RuntimeError(WORKFLOW_ONLY_DISPATCH_MSG.format(sub=sub_type))

        runtime = payload.get("runtime") if isinstance(payload, dict) else None
        sub_id = getattr(runtime, "tool_call_id", None)
        if isinstance(payload, dict) and isinstance(sub_id, str) and sub_id:
            brief = payload.get("description")
            if isinstance(brief, str) and brief.strip():
                _emit(
                    runtime,
                    {
                        "type": "subagent",
                        "phase": "input",
                        "id": sub_id,
                        "input": brief,
                    },
                )
        result = await self._inner.arun(payload, **kwargs)
        try:
            text = _result_text(result)
        except Exception:  # noqa: BLE001 — never break the dispatch
            text = ""
        # A context-extractor's digest is ALSO persisted to .harvest/context/
        # for run_review's fidelity phase (record() filters by type and is
        # fail-soft) — this shim is the fan-out path every extractor rides.
        try:
            from harvest.context_digests import record

            brief = payload.get("description") if isinstance(payload, dict) else None
            record(sub_type, brief if isinstance(brief, str) else None, text)
        except Exception:  # noqa: BLE001 — observability must not break dispatch
            logger.debug("Failed to record extractor digest", exc_info=True)
        if isinstance(sub_id, str) and sub_id and text.strip():
            _emit(
                runtime,
                {
                    "type": "subagent",
                    "phase": "result",
                    "id": sub_id,
                    "result": text,
                },
            )
        return result


def install_quickjs_io_forwarding() -> bool:
    """Patch ``langchain_quickjs._repl.call_subagent_task_tool`` (idempotent).

    Returns True when the forwarding is active (installed now or already),
    False when langchain_quickjs is absent or its internals have moved — in
    which case the fleet drill-in simply falls back to its "not captured"
    placeholder, exactly the pre-shim behavior.
    """
    try:
        from langchain_quickjs import _repl
    except Exception:  # noqa: BLE001 — optional dep / internals moved
        logger.info("langchain_quickjs not importable; sub-agent I/O forwarding off")
        return False
    original = getattr(_repl, "call_subagent_task_tool", None)
    if original is None:
        logger.warning(
            "langchain_quickjs._repl.call_subagent_task_tool not found; "
            "sub-agent I/O forwarding off (library internals moved?)"
        )
        return False
    if getattr(original, _INSTALLED_MARKER, False):
        return True

    async def patched(task_tool: Any, **call_kwargs: Any) -> Any:
        return await original(_TaskToolShim(task_tool), **call_kwargs)

    setattr(patched, _INSTALLED_MARKER, True)
    _repl.call_subagent_task_tool = patched
    logger.info("Sub-agent I/O forwarding installed on langchain_quickjs")
    return True
