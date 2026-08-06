"""Turn-level derailment steering — deterministic ``<system-reminder>`` injection.

Family B of the chat agent's static steering design (Family A is the SQL
anomaly detector in ``chat.sql_anomalies``). A middleware watches the current
turn's message slice before every model call and, when a derailment signature
fires, appends a steering reminder as a ``HumanMessage`` — which the Converse
adapter merges into the tool-result user message (``langchain-aws`` merges a
human message that follows a user-role entry), so the wire shape stays valid
and the prompt-cache prefix is untouched.

The three signals, in priority order:

- **repetition** — the model re-issued an EXACT tool call it already made this
  turn (same tool, same args). Its result is already in context; re-running it
  means the model has lost track.
- **futility** — the last N calls to the same tool all errored. Retrying
  variations is the death spiral; the docs are the way out.
- **silence** — N model calls without any user contact: no user-facing text
  and no answered ``ask_human`` form (an answered form IS interaction and
  resets the count). Heads-down digging is normal early; unbounded, it's a
  hole.

Discipline (agreed with the operator): once per signal kind per turn, a
cooldown of a few model calls after ANY injection so the correction has room
to work, and everything derived from the message slice itself — no side
state — so the checks are replay-safe across checkpoint resumes.

Injected messages carry ``additional_kwargs[STEERING_MARKER] = <kind>``: the
detector uses the marker for once-per-kind/cooldown accounting, and the
server's history builder skips marked messages so they never render as user
bubbles on reload.

Thresholds are env-tunable (``OKF_CHAT_STEERING_*``); ``steering_enabled``
gates the middleware at agent build time (kill switch, default on).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

try:  # langchain is present in the runtime image + the unit venv
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - only when langchain is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]
    AIMessage = HumanMessage = ToolMessage = None  # type: ignore[assignment]
    _HAVE_LANGCHAIN = False

#: additional_kwargs key marking a harness-injected steering message. The value
#: is the signal kind ("repetition" / "futility" / "silence").
STEERING_MARKER = "okf_steering"

#: Every marker that identifies a harness-INJECTED HumanMessage: steering
#: nudges plus the behavioural policy notes (``chat.policy_check.POLICY_MARKER``
#: — the string is duplicated here rather than imported to avoid a module
#: cycle; a test pins the two equal). None of these may open a turn slice,
#: reset the counters, or render as a user bubble.
_INJECTED_MARKER_KEYS = (STEERING_MARKER, "okf_policy")

# ask_human legitimately repeats (a re-ask after a malformed set) and is owned
# by its own middleware — excluded from repetition tracking.
_REPETITION_EXEMPT = frozenset({"ask_human"})

# Tools whose SUCCESSFUL result is itself user interaction: an answered
# ask_human means the user just read a form and replied, so the silence count
# restarts there. An errored result (a malformed set the user never saw) does
# not reset.
_SILENCE_RESET_TOOLS = frozenset({"ask_human"})


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


#: Consecutive same-tool errors before the futility reminder fires.
ERROR_STREAK = _env_int("OKF_CHAT_STEERING_ERROR_STREAK", 3)
#: Model calls without user-facing text before the silence reminder fires.
SILENCE_CALLS = _env_int("OKF_CHAT_STEERING_SILENCE_CALLS", 10)
#: Model calls after ANY injection before another reminder may fire.
COOLDOWN_CALLS = _env_int("OKF_CHAT_STEERING_COOLDOWN_CALLS", 3)

_REMINDER_OPEN = "<system-reminder>"
_REMINDER_CLOSE = "</system-reminder>"


def strip_reminder_tags(text: Any) -> str:
    """The reminder body without its ``<system-reminder>`` envelope.

    The tags are the MODEL-facing framing (harness channel, not prose); the UI
    shows only the note itself — used by the server when emitting a ``steer``
    chunk (live stream and history rebuild alike).
    """
    s = text if isinstance(text, str) else str(text)
    s = s.strip()
    if s.startswith(_REMINDER_OPEN):
        s = s[len(_REMINDER_OPEN) :]
    if s.endswith(_REMINDER_CLOSE):
        s = s[: -len(_REMINDER_CLOSE)]
    return s.strip()


def steering_enabled(env: dict[str, str] | None = None) -> bool:
    """Deploy-time kill switch (``OKF_CHAT_STEERING_ENABLED``, default on)."""
    raw = (env if env is not None else os.environ).get(
        "OKF_CHAT_STEERING_ENABLED", "true"
    )
    return str(raw).strip().lower() not in ("false", "0", "no", "off")


@dataclass(frozen=True)
class Signal:
    """One fired derailment signal: its dedupe kind and the reminder body."""

    kind: str
    text: str


def _repetition_text(tool: str) -> str:
    return (
        f"You have already made this exact `{tool}` call earlier in this turn; "
        "its result is above in the conversation. Re-read that result rather "
        "than re-running it. Step back in your thinking: restate what the user "
        "asked, list what is established so far, then continue with a specific "
        "plan, correct course, or ask the user."
    )


def _futility_text(tool: str, streak: int) -> str:
    return (
        f"The last {streak} `{tool}` calls errored. Rather than retrying "
        "variations, step back: re-read the relevant table docs for exact "
        "column names, check the join docs and usage guardrails, and re-derive "
        "the query from the docs. If the docs cannot settle it, tell the user "
        "what you tried and what is blocking."
    )


def _silence_text(calls: int) -> str:
    return (
        f"You have made {calls} model calls this turn without producing any "
        "user-facing text. Take stock in your thinking: restate the question, "
        "summarize what is established with evidence, and either finish with "
        "the answer, state the one specific step that remains, or ask the user "
        "the one thing you need."
    )


def _is_steering(msg: Any) -> bool:
    """True for ANY injected message (steering or policy note) — see markers."""
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return any(kwargs.get(k) for k in _INJECTED_MARKER_KEYS)


def _is_genuine_user(msg: Any) -> bool:
    return isinstance(msg, HumanMessage) and not _is_steering(msg)


def _has_user_text(msg: Any) -> bool:
    """True when an AIMessage carries user-facing text (not just tool calls).

    Converse content is a block list (``{"type": "text", ...}`` alongside
    ``reasoning_content``); GPT content is a plain string. Reasoning blocks are
    NOT user-facing text.
    """
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip()
            for b in content
        )
    return False


def _is_error_result(msg: Any) -> bool:
    """The chat tools signal failure as ``status='error'`` or an ``Error:`` body."""
    if getattr(msg, "status", None) == "error":
        return True
    content = getattr(msg, "content", None)
    return isinstance(content, str) and content.startswith("Error:")


def turn_slice(messages: list[Any]) -> list[Any]:
    """The messages of the CURRENT turn: from the last genuine user message on.

    Injected steering messages are HumanMessages too, but marker-carrying ones
    never open a turn — otherwise each injection would reset the very counters
    that triggered it.
    """
    for i in range(len(messages) - 1, -1, -1):
        if _is_genuine_user(messages[i]):
            return messages[i:]
    return list(messages)


def detect(messages: list[Any]) -> Signal | None:
    """The highest-priority NOVEL derailment signal for the current turn, if any.

    Pure function of the message list (replay-safe): once-per-kind and the
    post-injection cooldown are derived from the marker messages already in the
    slice, not from side state.
    """
    window = turn_slice(messages)

    fired_kinds: set[str] = set()
    last_reminder_idx: int | None = None
    for idx, msg in enumerate(window):
        if isinstance(msg, HumanMessage) and _is_steering(msg):
            kind = msg.additional_kwargs.get(STEERING_MARKER)
            if kind:  # a policy note has no steering kind but still cools down
                fired_kinds.add(kind)
            last_reminder_idx = idx
    if last_reminder_idx is not None:
        calls_since = sum(
            1 for m in window[last_reminder_idx:] if isinstance(m, AIMessage)
        )
        if calls_since < COOLDOWN_CALLS:
            return None

    # repetition — exact duplicate (tool, args) call within the turn. Only
    # calls whose result SUCCEEDED seed the set: re-issuing a call that
    # errored is course correction, not derailment (the guardrails gate
    # denies a read_page until references/usage_guardrails is read — the
    # retry after reading it is exactly what the denial instructed, and the
    # denial is NOT "its result is above"). Identical FAILING retries are
    # the futility signal's job below, not repetition's.
    results_by_id: dict[str, Any] = {}
    for m in window:
        if isinstance(m, ToolMessage):
            call_id = getattr(m, "tool_call_id", None)
            if call_id:
                results_by_id[call_id] = m
    seen_calls: set[tuple[str, str]] = set()
    for msg in window:
        if not isinstance(msg, AIMessage):
            continue
        for tc in msg.tool_calls or []:
            name = tc.get("name") or ""
            if name in _REPETITION_EXEMPT:
                continue
            key = (name, json.dumps(tc.get("args") or {}, sort_keys=True, default=str))
            if key in seen_calls and "repetition" not in fired_kinds:
                return Signal("repetition", _repetition_text(name))
            result = results_by_id.get(tc.get("id") or "")
            if result is None or not _is_error_result(result):
                seen_calls.add(key)

    # futility — trailing streak of errored results from the same tool.
    streak_tool: str | None = None
    streak = 0
    for msg in reversed(window):
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", None) or ""
        if not _is_error_result(msg) or (streak_tool is not None and name != streak_tool):
            break
        streak_tool = name
        streak += 1
    if streak >= ERROR_STREAK and streak_tool and "futility" not in fired_kinds:
        return Signal("futility", _futility_text(streak_tool, streak))

    # silence — model calls since the user last saw something: assistant text
    # or an answered ask_human form (its result IS user interaction).
    calls_since_text = 0
    for msg in window:
        if isinstance(msg, AIMessage):
            calls_since_text = 0 if _has_user_text(msg) else calls_since_text + 1
        elif (
            isinstance(msg, ToolMessage)
            and (getattr(msg, "name", None) or "") in _SILENCE_RESET_TOOLS
            and not _is_error_result(msg)
        ):
            calls_since_text = 0
    if calls_since_text >= SILENCE_CALLS and "silence" not in fired_kinds:
        return Signal("silence", _silence_text(calls_since_text))

    return None


class SteeringMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Inject a derailment ``<system-reminder>`` before a model call when needed.

    Attach to the chat agent's middleware list (see ``server.build_agent``).
    The appended HumanMessage lands after the ToolMessages, so the Converse
    adapter merges it into the tool-result user message — a valid wire shape
    that leaves the cached prefix intact. Both sync and async hooks are
    implemented because the framework's defaults are no-ops, and the chat
    supervisor runs the async path (``astream``).
    """

    def before_model(self, state, runtime=None):  # type: ignore[override]
        return self._steer(state)

    async def abefore_model(self, state, runtime=None):  # type: ignore[override]
        return self._steer(state)

    def _steer(self, state) -> dict[str, Any] | None:
        try:
            messages = list(state.get("messages") or [])
        except AttributeError:  # a state object without .get — nothing to do
            return None
        signal = detect(messages)
        if signal is None:
            return None
        return {
            "messages": [
                HumanMessage(
                    content=f"<system-reminder>{signal.text}</system-reminder>",
                    additional_kwargs={STEERING_MARKER: signal.kind},
                )
            ]
        }
