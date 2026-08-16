"""``AskHumanMiddleware`` — turns an ``ask_human`` tool call into a graph interrupt.

The **middleware owns the human-in-the-loop pause**, not the tool. When the model
calls ``ask_human``, this middleware's ``awrap_tool_call`` intercepts it, validates
the question set, and raises LangGraph ``interrupt(payload)`` — which checkpoints
the graph and stops the run. The chat server sees the ``__interrupt__`` in the
stream, emits an ``ask_human`` chunk (the questions) to the UI, and later resumes
with ``Command(resume=answers)``; the middleware's ``interrupt(...)`` call then
RETURNS those answers, which we fold into the tool result so the model reads them
and continues.

This is the same pattern proven for the (currently unused) chart interrupt on the
installed langgraph/langchain: async tool calls go down ``awrap_tool_call``, one
call → one pending interrupt, resume via ``Command(resume=…)``. We implement only
the async variant because the chat supervisor runs via ``graph.astream`` — but a
sync ``wrap_tool_call`` is added too so a sync driver (tests) can't hit
``NotImplementedError``.

Deferred langchain imports keep the module importable in the unit venv.
"""

from __future__ import annotations

import json
from typing import Any

from chat.ask_human import AskHumanError, normalize_answers, normalize_questions

try:  # langchain is present in the runtime image + the unit venv
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import ToolMessage

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - only when langchain is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]
    ToolMessage = None  # type: ignore[assignment]
    _HAVE_LANGCHAIN = False

_ASK_HUMAN_TOOL = "ask_human"


class AskHumanMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Intercept ``ask_human`` and pause the graph until the user answers.

    Attach to the chat agent's middleware list (see ``server.build_agent``). Only
    the ``ask_human`` tool is touched; every other tool passes straight through.
    """

    def wrap_tool_call(self, request, handler):  # type: ignore[override]
        """Sync path (invoke) — mirrors the async one so a sync driver won't raise.

        ``interrupt`` works on both graph drivers; the decision logic is pure, so we
        reuse :meth:`_intercept` and only fall back to the downstream handler for
        non-``ask_human`` tools.
        """
        outcome = self._intercept(request)
        return outcome if outcome is not None else handler(request)

    async def awrap_tool_call(self, request, handler):  # type: ignore[override]
        """Async path (astream) — the chat supervisor's real path."""
        outcome = self._intercept(request)
        return outcome if outcome is not None else await handler(request)

    def _intercept(self, request):
        """Return a ToolMessage for an ``ask_human`` call (via an interrupt round
        trip), or None to let the downstream handler run the real tool.

        A malformed question set does NOT interrupt — it returns an error
        ToolMessage so the model can re-issue a valid ``ask_human`` call. A valid
        set raises ``interrupt(payload)``: the graph pauses here; on resume the
        interrupt returns the user's answers, which we normalize + fold into the
        tool result the model reads.
        """
        if request.tool_call["name"] != _ASK_HUMAN_TOOL:
            return None

        args = request.tool_call.get("args") or {}
        try:
            questions = normalize_questions(args.get("questions"))
        except AskHumanError as e:
            return self._tool_message(
                request, {"status": "error", "error": str(e)}
            )

        # Deferred import: interrupt must be called INSIDE the graph execution, and
        # keeping it lazy lets this module import where langgraph isn't installed.
        from langgraph.types import interrupt

        # PAUSE: the graph checkpoints here; the server emits the questions to the
        # UI. On Command(resume=answers) this returns those answers.
        raw_answers = interrupt({"type": _ASK_HUMAN_TOOL, "questions": questions})

        answers = normalize_answers(raw_answers, questions)
        return self._tool_message(
            request,
            {
                "status": "answered",
                "answers": answers,
                "note": (
                    "The user answered your clarifying questions (above). Use these "
                    "answers to continue; do not ask them again."
                ),
            },
        )

    def _tool_message(self, request, payload: dict[str, Any]):
        """Build the ToolMessage returned in place of running the tool."""
        return ToolMessage(
            content=json.dumps(payload),
            tool_call_id=request.tool_call["id"],
            name=_ASK_HUMAN_TOOL,
            status="error" if payload.get("status") == "error" else "success",
        )
