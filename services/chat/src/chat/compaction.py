"""Conversation compaction + context measurement for the chat agent.

The transcript the UI re-renders IS the checkpointed ``messages`` list
(``server.read_history`` folds it into turns), so compaction must never remove
messages. Instead a compaction stores a SUMMARY plus the id of the last message
it covers (``okf_compaction`` in checkpointed state); :class:`CompactionMiddleware`
rewrites every model request to ``[summary] + messages after that id`` — the
model sees the compacted context, the user keeps the whole conversation.

Three entry points share one summarizer (:func:`compact_messages`):

* AUTO, mid-turn — ``before_model`` compacts when the last measured context
  (``okf_context``, written by ``after_model`` from the call's usage) crosses
  :func:`auto_compact_threshold`. The cutoff may fall inside the running turn
  (at an assistant step), keeping the most recent steps verbatim.
* MANUAL — the ``compact`` invocation (``server``) summarizes everything up to
  the conversation's last message and writes the state via ``update_state``.
* Every model call also MEASURES the context (input tokens incl. cache, which
  ``langchain-aws`` already sums), surfaced as the UI's context gauge.

Also carries the conversation's pinned model (``okf_model``): a conversation
may switch models only within the same provider family (checkpointed reasoning
blocks aren't portable between Converse and the Responses API).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, NotRequired

try:  # langchain/langgraph are present in the runtime image + the unit venv
    from langchain.agents.middleware import AgentMiddleware, AgentState
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - only when langchain is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]
    AgentState = dict  # type: ignore[assignment,misc]
    AIMessage = HumanMessage = ToolMessage = None  # type: ignore[assignment,misc]
    _HAVE_LANGCHAIN = False

log = logging.getLogger("chat.compaction")

#: Checkpointed state channels (all plain last-value channels).
MODEL_KEY = "okf_model"  # the conversation's pinned model id
COMPACTION_KEY = "okf_compaction"  # {"summary", "through_id"} — the live compaction
COMPACTIONS_KEY = "okf_compactions"  # [COMP, ...] — the UI's divider records
CONTEXT_KEY = "okf_context"  # {"tokens", "model"} — the last measured context

#: The pinned model of threads from before the pin existed: the UI only ever
#: offered Opus 5.5, so a thread with messages but no okf_model was Opus 5.5.
LEGACY_MODEL = "global.anthropic.claude-opus-5-5"

#: additional_kwargs marker on the injected summary message (never in state).
COMPACTION_MARKER = "okf_compaction_summary"

#: Sentry's trigger rule: compact at 95% of the window, lowered so at least
#: this much headroom remains, never below half the window.
_TRIGGER_FRACTION = 0.95
_MIN_HEADROOM = 30_000
_MIN_FRACTION = 0.5

#: Auto compaction keeps this share of the window verbatim (the most recent
#: steps); manual compaction keeps nothing — it covers the whole conversation.
_KEEP_FRACTION = 0.1

#: Per-item caps when rendering the transcript the summarizer reads — tool
#: results dominate a data conversation and are re-runnable, the reasoning
#: around them is what must survive.
_TOOL_RESULT_CHARS = 2_000
_TOOL_ARGS_CHARS = 1_000

SUMMARY_PROMPT = """\
You are compacting a data-analysis conversation between a user and an AI analyst \
that answers from a data wiki (documented datasets) and, when enabled, read-only SQL. \
The summary REPLACES the conversation for the analyst's next steps, so it must let \
the analyst continue seamlessly without re-asking the user.

Write a structured summary covering:
1. The user's goals and questions, in order, and which are answered vs still open.
2. Datasets, tables, columns and wiki pages that turned out to matter (exact names).
3. Key findings and numbers, with the SQL (verbatim when short) or computation that produced them.
4. Definitions, caveats, guardrails and data-quality issues the analyst relied on or reported.
5. Decisions and preferences the user expressed (scope, filters, formats, clarifications).
6. What the analyst was doing at the cutoff and the immediate next step, if any.

Be specific and complete; omit pleasantries. Do not invent anything not in the transcript.
{previous}
<transcript>
{transcript}
</transcript>"""


def compact_trigger_fraction(env: dict[str, str] | None = None) -> float:
    """``OKF_CHAT_COMPACT_TRIGGER`` (fraction of the window), default 0.95."""
    raw = (env if env is not None else os.environ).get("OKF_CHAT_COMPACT_TRIGGER", "")
    try:
        value = float(raw) if raw else _TRIGGER_FRACTION
    except ValueError:
        value = _TRIGGER_FRACTION
    return min(max(value, _MIN_FRACTION), _TRIGGER_FRACTION)


def auto_compact_threshold(window: int, env: dict[str, str] | None = None) -> int:
    """Token count at which auto compaction fires for a ``window``-token model."""
    fraction = compact_trigger_fraction(env)
    if window * (1 - fraction) < _MIN_HEADROOM:
        fraction = max(_MIN_FRACTION, 1 - _MIN_HEADROOM / window)
    return int(window * fraction)


def model_family(model: str | None) -> str:
    """``"openai"`` or ``"anthropic"`` — the unit a conversation is pinned to."""
    from okf_aws.model_factory import is_openai_model

    return "openai" if model and is_openai_model(model) else "anthropic"


def context_report(
    tokens: int | None, model: str, compactions: int = 0
) -> dict[str, Any] | None:
    """The UI's CTX payload; None until a model call has been measured."""
    if not tokens:
        return None
    from okf_aws.model_factory import context_window

    window = context_window(model)
    return {
        "tokens": int(tokens),
        "window": window,
        "percent": round(100.0 * tokens / window, 1),
        "threshold": auto_compact_threshold(window),
        "model": model,
        "compactions": compactions,
    }


def usage_context_tokens(usage: dict | None) -> int | None:
    """One model call's context size: its input tokens. ``langchain-aws``
    already folds cache reads/writes into ``input_tokens`` (as does the
    Responses API), so no cache arithmetic here."""
    if not usage:
        return None
    tokens = usage.get("input_tokens")
    return int(tokens) if tokens else None


# --- the model-visible view ----------------------------------------------------


def _memory_marker(msg: Any) -> Any:
    from chat.memory import MEMORY_MARKER

    return (getattr(msg, "additional_kwargs", None) or {}).get(MEMORY_MARKER)


def _is_memory_recall(msg: Any) -> bool:
    """A PER-TURN recall injection — stripped by the next turn, so never a
    summary boundary and never worth summarizing."""
    return _memory_marker(msg) == "recall"


def _is_personal_context(msg: Any) -> bool:
    """The user's personal context — injected ONCE per thread and meant to stay
    in history, so it survives every compaction verbatim (see model_view)."""
    return _memory_marker(msg) == "personal"


def summary_message(summary: str) -> Any:
    return HumanMessage(
        content=(
            "<conversation_summary>\nEarlier turns of this conversation were "
            "compacted. This summary replaces them:\n\n"
            f"{summary}\n</conversation_summary>"
        ),
        additional_kwargs={COMPACTION_MARKER: True},
    )


def model_view(messages: list[Any], compaction: dict | None) -> list[Any]:
    """``[personal context] + [summary] + messages after the compaction's
    through_id``. The once-per-thread personal context is never summarized away:
    later turns don't re-inject it.

    A through_id that no longer exists (it never should — compaction only ever
    points at messages nothing removes) degrades to the full history: an
    uncompacted request is expensive, a request missing context is wrong.
    """
    if not compaction or not compaction.get("summary"):
        return messages
    through = compaction.get("through_id")
    if through is None:
        return messages
    for i, msg in enumerate(messages):
        if getattr(msg, "id", None) == through:
            tail = messages[i + 1 :]
            # A tool result can't lead the request — its call was summarized.
            while tail and isinstance(tail[0], ToolMessage):
                tail = tail[1:]
            personal = [m for m in messages[: i + 1] if _is_personal_context(m)]
            return [*personal, summary_message(compaction["summary"]), *tail]
    log.warning("compaction through_id %s not found; sending full history", through)
    return messages


# --- choosing the cutoff -------------------------------------------------------


def _approx_tokens(messages: list[Any]) -> int:
    from langchain_core.messages.utils import count_tokens_approximately

    return count_tokens_approximately(messages) if messages else 0


def choose_through(
    messages: list[Any], start: int, keep_tokens: int
) -> int | None:
    """Index of the last message to summarize, or None when nothing qualifies.

    The kept tail must begin at a turn start (a user message) or an assistant
    step — never at a tool result whose call would be summarized away — and
    hold at most ``keep_tokens`` (approx). The summarized prefix ends at a
    message nothing removes (never a per-turn memory recall, which the next
    turn strips), so ``through_id`` stays resolvable.
    """
    def candidate(msg: Any) -> bool:
        if isinstance(msg, AIMessage):
            return True
        return isinstance(msg, HumanMessage) and not _memory_marker(msg)

    # One pass from the end: the earliest candidate whose tail still fits.
    chosen = None
    latest = None
    tail_tokens = 0
    for i in range(len(messages) - 1, start, -1):
        tail_tokens += _approx_tokens([messages[i]])
        if not candidate(messages[i]):
            continue
        if latest is None:
            latest = i
        if tail_tokens > keep_tokens:
            break
        chosen = i
    if chosen is None:
        if latest is None:
            return None
        chosen = latest  # keep only the latest step
    through = chosen - 1
    while through >= start and _is_memory_recall(messages[through]):
        through -= 1
    return through if through >= start else None


def _start_index(messages: list[Any], compaction: dict | None) -> int:
    """First index the next compaction may cover (after the current one)."""
    through = (compaction or {}).get("through_id")
    if through is not None:
        for i, msg in enumerate(messages):
            if getattr(msg, "id", None) == through:
                return i + 1
    return 0


# --- the summarizer --------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f" …[{len(text) - limit} chars cut]"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def render_transcript(messages: list[Any]) -> str:
    """Provider-neutral text transcript (reasoning blocks dropped — they aren't
    portable and the visible answers carry the conclusions)."""
    from chat.server import strip_scope_prefix

    lines: list[str] = []
    for msg in messages:
        if _memory_marker(msg):  # recall is stale; personal is kept verbatim
            continue
        if isinstance(msg, HumanMessage):
            kwargs = getattr(msg, "additional_kwargs", None) or {}
            if kwargs.get(COMPACTION_MARKER):
                continue
            text = _text_of(msg.content)
            if is_user_turn(msg):
                lines.append(f"USER: {strip_scope_prefix(text)}")
            else:
                lines.append(f"HARNESS NOTE: {text}")
        elif isinstance(msg, AIMessage):
            text = _text_of(msg.content)
            if text:
                lines.append(f"ANALYST: {text}")
            for tc in msg.tool_calls or []:
                args = json.dumps(tc.get("args") or {}, default=str)
                lines.append(f"ANALYST CALLED {tc.get('name')}: {_clip(args, _TOOL_ARGS_CHARS)}")
        elif isinstance(msg, ToolMessage):
            lines.append(
                f"TOOL RESULT ({msg.name}): {_clip(_text_of(msg.content), _TOOL_RESULT_CHARS)}"
            )
    return "\n\n".join(lines)


def summarize(model: Any, messages: list[Any], previous: str | None) -> str:
    """One summarizer call over a provider-neutral transcript."""
    prev = (
        f"\nA previous compaction summarized the conversation before this transcript; "
        f"fold it in:\n<previous_summary>\n{previous}\n</previous_summary>\n"
        if previous
        else ""
    )
    from langgraph.constants import TAG_NOSTREAM

    prompt = SUMMARY_PROMPT.format(previous=prev, transcript=render_transcript(messages))
    # The auto path runs INSIDE the agent graph's callback context: untagged,
    # LangGraph's messages stream would deliver the summary (and its usage) to
    # the user as the agent's own answer.
    reply = model.invoke([HumanMessage(content=prompt)], config={"tags": [TAG_NOSTREAM]})
    summary = _text_of(reply.content).strip()
    if not summary:
        raise RuntimeError("summarizer returned no text")
    return summary


def compact_messages(
    model: Any,
    messages: list[Any],
    compaction: dict | None,
    *,
    keep_tokens: int,
) -> tuple[dict, int] | None:
    """Summarize what the current compaction doesn't cover yet, up to a cutoff.

    Returns ``(new_compaction, through_index)``, or None when there is nothing
    to compact. ``keep_tokens=0`` covers everything (manual compaction).
    """
    start = _start_index(messages, compaction)
    if keep_tokens <= 0:
        through = len(messages) - 1
        while through >= start and _is_memory_recall(messages[through]):
            through -= 1
        if through < start:
            return None
    else:
        through = choose_through(messages, start, keep_tokens)
        if through is None:
            return None
    covered = messages[start : through + 1]
    # Manual compaction needs something answered to be worth a summary; an
    # auto one may cover a lone (huge) user message.
    if keep_tokens <= 0 and not any(isinstance(m, (AIMessage, ToolMessage)) for m in covered):
        return None
    summary = summarize(model, covered, (compaction or {}).get("summary"))
    return {"summary": summary, "through_id": messages[through].id}, through


def is_user_turn(msg: Any) -> bool:
    """A real user message — the same rule ``server._messages_to_turns`` opens
    a turn by (harness-injected memory / policy / steering notes don't)."""
    if not isinstance(msg, HumanMessage):
        return False
    from chat.memory import MEMORY_MARKER
    from chat.policy_check import POLICY_MARKER
    from chat.steering import STEERING_MARKER

    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return not any(kwargs.get(m) for m in (MEMORY_MARKER, POLICY_MARKER, STEERING_MARKER))


def turn_index_of(messages: list[Any], index: int) -> int:
    """Which UI turn (0-based) a message index belongs to."""
    turn = -1
    for msg in messages[: index + 1]:
        if is_user_turn(msg):
            turn += 1
    return max(turn, 0)


# --- the middleware ------------------------------------------------------------------


if _HAVE_LANGCHAIN:

    class CompactionState(AgentState):  # type: ignore[misc]
        okf_model: NotRequired[str]
        okf_compaction: NotRequired[dict]
        okf_compactions: NotRequired[list]
        okf_context: NotRequired[dict]

else:  # pragma: no cover - only when langchain is absent
    CompactionState = dict  # type: ignore[assignment,misc]


#: Error text a provider returns when a request exceeds the context window
#: (Converse ValidationException / Responses API context_length_exceeded).
_OVERFLOW_HINTS = (
    "too long",
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "too many tokens",
)


def is_context_overflow(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _OVERFLOW_HINTS)


class CompactionMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Measure context per model call, auto-compact over the threshold, and
    send every model call the compacted view (see the module doc).

    The trigger is an ESTIMATE of the next request: the last measured reading
    plus an approximate count of everything appended since (tool results, the
    reply itself) — a large tool result must not overflow the window before the
    reading catches up. A request the provider still rejects as too long is
    compacted and retried once; that compaction is persisted by the next
    ``after_model``.

    ``summarizer`` builds the model the summary runs on (called lazily, only
    when a compaction actually fires). Attach to EVERY chat graph — the history
    read builds one too, and its state schema must carry these channels.
    """

    state_schema = CompactionState

    def __init__(self, model: str, summarizer: Any = None):
        super().__init__()
        self._model = model
        self._summarizer = summarizer
        from okf_aws.model_factory import context_window

        self._window = context_window(model)
        self._threshold = auto_compact_threshold(self._window)
        # An overflow-retry compaction awaiting persistence by after_model.
        self._pending: dict | None = None

    def _compact(self, messages: list[Any], current: dict | None, measured: int) -> dict | None:
        """One auto compaction → the state update, or None (nothing / failed)."""
        try:
            result = compact_messages(
                self._summarizer(),
                messages,
                current,
                keep_tokens=int(self._window * _KEEP_FRACTION),
            )
        except Exception:  # noqa: BLE001 - a failed compaction must not fail the turn
            log.warning("auto compaction failed; continuing uncompacted", exc_info=True)
            return None
        if result is None:
            return None
        compaction, through = result
        post = _approx_tokens(model_view(messages, compaction))
        record = {
            "source": "auto",
            "turn": turn_index_of(messages, len(messages) - 1),
            "phase": "during",
            "pre_tokens": int(measured),
            "post_tokens": int(post),
        }
        log.info("auto compaction: ~%d -> ~%d tokens (through message %d)", measured, post, through)
        return {
            "compaction": compaction,
            "record": record,
            "context": {"tokens": post, "model": self._model, "last_id": _last_id(messages)},
        }

    @staticmethod
    def _as_update(state, done: dict) -> dict:
        return {
            COMPACTION_KEY: done["compaction"],
            COMPACTIONS_KEY: [*(state.get(COMPACTIONS_KEY) or []), done["record"]],
            CONTEXT_KEY: done["context"],
        }

    # -- measurement ---------------------------------------------------------

    def after_model(self, state, runtime):  # type: ignore[override]
        messages = state.get("messages") or []
        update: dict = {}
        if self._pending is not None:
            update = self._as_update(state, self._pending)
            self._pending = None
        last = messages[-1] if messages else None
        tokens = usage_context_tokens(getattr(last, "usage_metadata", None))
        if tokens:
            update[CONTEXT_KEY] = {
                "tokens": tokens,
                "model": self._model,
                "last_id": _last_id(messages),
            }
        return update or None

    async def aafter_model(self, state, runtime):  # type: ignore[override]
        return self.after_model(state, runtime)

    # -- auto compaction -------------------------------------------------------

    def estimate(self, state) -> int:
        """The next request's size: last reading + what was appended since."""
        context = state.get(CONTEXT_KEY) or {}
        measured = int(context.get("tokens") or 0)
        if not measured:
            return 0
        messages = state.get("messages") or []
        last_id = context.get("last_id")
        added: list[Any] = []
        if last_id is not None:
            for i in range(len(messages) - 1, -1, -1):
                if getattr(messages[i], "id", None) == last_id:
                    added = messages[i + 1 :]
                    break
        return measured + _approx_tokens(added)

    def before_model(self, state, runtime):  # type: ignore[override]
        if self._summarizer is None:
            return None
        estimated = self.estimate(state)
        if estimated < self._threshold:
            return None
        done = self._compact(state.get("messages") or [], state.get(COMPACTION_KEY), estimated)
        return self._as_update(state, done) if done else None

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        import asyncio

        return await asyncio.to_thread(self.before_model, state, runtime)

    # -- the compacted view ------------------------------------------------------

    def _rewrite(self, request, compaction: dict | None = None):
        compaction = compaction or (request.state or {}).get(COMPACTION_KEY)
        if not compaction:
            return request
        return request.override(messages=model_view(list(request.messages), compaction))

    def _overflow_compaction(self, request) -> dict | None:
        if self._summarizer is None:
            return None
        state = request.state or {}
        messages = state.get("messages") or list(request.messages)
        done = self._compact(messages, state.get(COMPACTION_KEY), self.estimate(state))
        if done is not None:
            self._pending = done  # persisted by the next after_model
        return done

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        try:
            return handler(self._rewrite(request))
        except Exception as exc:
            if not is_context_overflow(exc):
                raise
            done = self._overflow_compaction(request)
            if done is None:
                raise
            log.warning("context overflow; compacted and retrying once")
            return handler(self._rewrite(request, done["compaction"]))

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        import asyncio

        try:
            return await handler(self._rewrite(request))
        except Exception as exc:
            if not is_context_overflow(exc):
                raise
            done = await asyncio.to_thread(self._overflow_compaction, request)
            if done is None:
                raise
            log.warning("context overflow; compacted and retrying once")
            return await handler(self._rewrite(request, done["compaction"]))


def _last_id(messages: list[Any]) -> str | None:
    return getattr(messages[-1], "id", None) if messages else None
