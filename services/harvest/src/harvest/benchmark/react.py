"""The benchmark ReAct agent factory — ``create_agent`` + Bedrock prompt caching.

Every benchmark LLM role (the solver, the judge's three hats, the annotation
aggregator) is a plain ReAct loop, and a ReAct loop re-sends its ENTIRE
conversation — system prompt, tool schemas, every page already read — on every
turn. This factory builds those loops with LangChain's ``create_agent`` and
attaches ``BedrockPromptCachingMiddleware`` (the chat agent's setup, see
``chat/server.py``): on a Bedrock Converse Claude model the middleware passes
cache settings via ``model_settings`` and ``ChatBedrockConverse`` inserts the
``cachePoint`` blocks at request time, so the static prefix AND the growing
conversation bill as cache READS (~0.1x input) turn over turn instead of full
price. On a Mantle GPT model the middleware warns once and no-ops — the
Responses API already caches prefixes implicitly server-side. The usage
plumbing (``steps.UsageForwarder``, report telemetry) already meters
``cache_read``/``cache_creation``, so cache traffic shows up without changes.

One behavioral delta vs the ``langgraph.prebuilt.create_react_agent`` these
roles used before: ``create_agent`` has no remaining-steps apology message —
hitting ``recursion_limit`` RAISES ``GraphRecursionError`` from
invoke/stream. Callers own that: the solver maps it to its step-budget error
(and streams so the partial trace survives), the judge hats already convert
any exception into a ``judge_error`` verdict, and the aggregator ships
whatever its store holds.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("harvest.benchmark.react")

try:  # langchain is only present in the runtime image (and the chat venv)
    from langchain.agents.middleware import AgentMiddleware, hook_config
    from langchain.messages import HumanMessage

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - exercised only when langchain is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]
    HumanMessage = None  # type: ignore[assignment]

    def hook_config(*_a: Any, **_k: Any):  # type: ignore[misc]
        """No-op stand-in so the class body imports where langchain is absent."""

        def _decorate(fn):
            return fn

        return _decorate

    _HAVE_LANGCHAIN = False


def make_react_agent(
    model: Any,
    tools: list[Any],
    system_prompt: str,
    *,
    extra_middleware: list[Any] | None = None,
) -> Any:
    """Build one prompt-cached ReAct agent (deferred imports; see module doc).

    ``extra_middleware`` appends role-specific middleware after the caching
    middleware — e.g. the judge hats' :class:`SubmitToolNudgeMiddleware`.
    """
    from langchain.agents import create_agent
    from langchain_aws.middleware import BedrockPromptCachingMiddleware

    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=[BedrockPromptCachingMiddleware(), *(extra_middleware or [])],
    )


def _is_final_ai_turn(msg: Any) -> bool:
    """True iff ``msg`` is an AI message with NO tool calls (the agent is ending)."""
    if msg is None:
        return False
    if getattr(msg, "type", "") not in ("ai", "assistant"):
        return False
    return not getattr(msg, "tool_calls", None)


def count_nudges(messages: list, nudge: str) -> int:
    """How many prior injections of ``nudge`` the conversation carries.

    Text-extracted (not a bare ``==`` on ``content``) because a provider round
    trip can re-shape a human message's string content into a block list
    (``[{"type": "text", "text": ...}]``), which used to make the count miss
    and the middleware nudge forever.
    """
    from harvest.benchmark.extract import message_text

    return sum(
        1
        for m in messages or []
        if getattr(m, "type", "") == "human"
        and message_text(getattr(m, "content", "")) == nudge
    )


def called_tool(messages: list, tool_name: str) -> bool:
    """True iff any AI message in ``messages`` called ``tool_name``."""
    for m in messages or []:
        if getattr(m, "type", "") not in ("ai", "assistant"):
            continue
        for tc in getattr(m, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name == tool_name:
                return True
    return False


class SubmitToolNudgeMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Steer an agent that tries to finish without calling its submit tool.

    The judge hats deliver their ruling through a tool call (structured by
    construction — the tool-call args ARE the output, no fence parsing), but
    models drift: they investigate well and then narrate the verdict as prose.
    This hooks ``after_model``; when the model emits a tool-call-free final
    message and ``tool_name`` was never called in this conversation, it injects
    ``nudge`` as a human message and jumps back to the model — at most
    ``max_nudges`` times, then it lets the run end (the caller's unparseable-
    output path is the backstop, so a model that ignores two nudges can't spin
    against the recursion limit).

    CONCURRENCY: one agent instance serves many cases concurrently, so no
    per-conversation state lives on ``self`` — the nudge count is recovered
    from the conversation itself (nudge messages carry a fixed text).
    """

    def __init__(self, tool_name: str, nudge: str, *, max_nudges: int = 2):
        super().__init__()
        self.tool_name = tool_name
        self.nudge = nudge
        self.max_nudges = max(0, int(max_nudges))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):  # type: ignore[override]
        messages = (state or {}).get("messages") or []
        last = messages[-1] if messages else None
        if not _is_final_ai_turn(last):
            return None  # still working — never interfere mid-flight
        if called_tool(messages, self.tool_name):
            return None  # delivered (validity is the extractor's concern)
        nudges = count_nudges(messages, self.nudge)
        if nudges >= self.max_nudges:
            log.warning(
                "Agent ended without calling %s after %d nudge(s); giving up.",
                self.tool_name,
                nudges,
            )
            return None
        return {"jump_to": "model", "messages": [HumanMessage(content=self.nudge)]}


def is_recursion_limit(exc: Exception) -> bool:
    """True iff ``exc`` is langgraph's recursion-limit error (matched by name so
    this module imports without langgraph installed)."""
    return type(exc).__name__ == "GraphRecursionError"
