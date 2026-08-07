"""Derailment steering: the pure signals, the discipline, and the middleware.

Everything is derived from the message slice (no side state), so these tests
build synthetic conversations and assert on ``detect``. The discipline rules —
once per kind per turn, cooldown after any injection, marker messages never
opening a turn — are pinned individually: each one failing would turn steering
into nagging.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chat import steering
from chat.steering import STEERING_MARKER, Signal, SteeringMiddleware, detect


def _ai(tool_calls=None, text=""):
    calls = [
        {"name": name, "args": args, "id": f"call_{i}", "type": "tool_call"}
        for i, (name, args) in enumerate(tool_calls or [])
    ]
    return AIMessage(content=text, tool_calls=calls)


def _tool(name, content="ok", status="success"):
    return ToolMessage(content=content, name=name, tool_call_id="x", status=status)


def _reminder(kind):
    return HumanMessage(
        content=f"<system-reminder>{kind}</system-reminder>",
        additional_kwargs={STEERING_MARKER: kind},
    )


_USER = HumanMessage(content="[Scope: x] question")


# --- turn boundary -----------------------------------------------------------


def test_turn_slice_starts_at_last_genuine_user_message():
    prior = [HumanMessage(content="old"), _ai(text="old answer")]
    msgs = prior + [_USER, _ai([("grep", {"q": "a"})])]
    assert steering.turn_slice(msgs)[0] is _USER


def test_injected_reminders_do_not_open_a_turn():
    # A marker message is a HumanMessage — if it reset the slice, every
    # injection would wipe the counters that triggered it.
    msgs = [_USER, _ai([("grep", {"q": "a"})]), _reminder("silence"), _ai()]
    assert steering.turn_slice(msgs)[0] is _USER


# --- repetition ---------------------------------------------------------------


def test_exact_duplicate_tool_call_fires_repetition():
    call = ("run_sql", {"sql": "SELECT 1"})
    msgs = [_USER, _ai([call]), _tool("run_sql"), _ai([call])]
    signal = detect(msgs)
    assert signal and signal.kind == "repetition" and "`run_sql`" in signal.text


def test_different_args_are_not_repetition():
    msgs = [
        _USER,
        _ai([("run_sql", {"sql": "SELECT 1"})]),
        _tool("run_sql"),
        _ai([("run_sql", {"sql": "SELECT 2"})]),
    ]
    assert detect(msgs) is None


def test_errored_call_does_not_seed_repetition():
    """A retry after an ERRORED result is course correction, not derailment —
    e.g. the guardrails gate denies a read_page until the guardrails doc is
    read, then the model re-issues the exact same call as the denial
    instructed. Only a call that SUCCEEDED makes its repeat a repetition."""
    args = {"concept_id": "tables/orders"}
    denied = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_page", "args": args, "id": "c1", "type": "tool_call"}
        ],
    )
    denial = ToolMessage(
        content='{"status": "denied"}',
        name="read_page",
        tool_call_id="c1",
        status="error",
    )
    retry = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_page", "args": args, "id": "c2", "type": "tool_call"}
        ],
    )
    ok = ToolMessage(content="# page", name="read_page", tool_call_id="c2")
    assert detect([_USER, denied, denial, retry, ok]) is None
    # But a THIRD identical call — one HAS succeeded now — is a repetition.
    third = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_page", "args": args, "id": "c3", "type": "tool_call"}
        ],
    )
    sig = detect([_USER, denied, denial, retry, ok, third])
    assert sig is not None and sig.kind == "repetition"


def test_identical_errored_call_repeated_fires_futility_despite_interleaving():
    """The SAME failing call re-issued over and over IS derailment even when
    successful calls of another tool interleave — the successes break the
    trailing-error-streak check, and errored calls never seed the repetition
    set, so without a per-call error count this loop ran un-nudged forever
    (deny → semantic_search → identical deny → …)."""
    args = {"concept_id": "tables/orders"}
    msgs = [_USER]
    for i in range(steering.ERROR_STREAK):
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_page",
                        "args": args,
                        "id": f"e{i}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        msgs.append(
            ToolMessage(
                content='{"status": "denied"}',
                name="read_page",
                tool_call_id=f"e{i}",
                status="error",
            )
        )
        # An interleaved SUCCESS of another tool — breaks the trailing streak.
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "semantic_search",
                        "args": {"q": f"try {i}"},
                        "id": f"s{i}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        msgs.append(
            ToolMessage(content="hits", name="semantic_search", tool_call_id=f"s{i}")
        )
    sig = detect(msgs)
    assert sig is not None and sig.kind == "futility"
    assert "`read_page`" in sig.text
    # One error short of the threshold stays quiet (the instructed retry
    # after a denial must not be nudged).
    assert detect(msgs[: -4]) is None


def test_ask_human_is_exempt_from_repetition():
    call = ("ask_human", {"questions": ["q"]})
    msgs = [_USER, _ai([call]), _tool("ask_human"), _ai([call])]
    assert detect(msgs) is None


# --- futility -----------------------------------------------------------------


def _error_streak(n, tool="run_sql"):
    msgs = [_USER]
    for i in range(n):
        msgs += [_ai([(tool, {"sql": f"SELECT {i}"})]), _tool(tool, "Error: boom", "error")]
    return msgs


def test_consecutive_same_tool_errors_fire_futility():
    signal = detect(_error_streak(steering.ERROR_STREAK))
    assert signal and signal.kind == "futility" and "`run_sql`" in signal.text


def test_streak_below_threshold_is_quiet():
    assert detect(_error_streak(steering.ERROR_STREAK - 1)) is None


def test_a_success_breaks_the_streak():
    msgs = _error_streak(steering.ERROR_STREAK) + [
        _ai([("run_sql", {"sql": "SELECT ok"})]),
        _tool("run_sql", '{"rows": []}'),
    ]
    # trailing result is a success → no trailing streak (and the earlier
    # duplicate-free calls don't fire repetition either)
    assert detect(msgs) is None


def test_error_prefix_counts_without_status():
    # chat tools signal failure via an "Error: ..." body; status may be absent.
    msgs = [_USER]
    for i in range(steering.ERROR_STREAK):
        msgs += [
            _ai([("run_sql", {"sql": f"SELECT {i}"})]),
            ToolMessage(content="Error: nope", name="run_sql", tool_call_id="x"),
        ]
    signal = detect(msgs)
    assert signal and signal.kind == "futility"


# --- silence -------------------------------------------------------------------


def _silent_calls(n, with_text_at=None):
    msgs = [_USER]
    for i in range(n):
        text = "progress so far" if i == with_text_at else ""
        msgs += [_ai([("read_page", {"p": str(i)})], text=text), _tool("read_page")]
    return msgs


def test_sustained_silence_fires():
    signal = detect(_silent_calls(steering.SILENCE_CALLS))
    assert signal and signal.kind == "silence"


def test_text_resets_the_silence_counter():
    assert detect(_silent_calls(steering.SILENCE_CALLS, with_text_at=5)) is None


def test_block_content_text_counts_as_user_facing():
    msgs = _silent_calls(steering.SILENCE_CALLS - 1)
    msgs.append(
        AIMessage(
            content=[
                {"type": "reasoning_content", "reasoning_content": {"text": "hmm"}},
                {"type": "text", "text": "found it"},
            ]
        )
    )
    assert detect(msgs) is None  # reasoning is not user-facing; text is


def test_answered_ask_human_resets_the_silence_counter():
    # The user answered a clarification form mid-turn — that IS interaction,
    # so the silence count restarts there instead of firing right after.
    msgs = _silent_calls(steering.SILENCE_CALLS - 1)
    msgs += [
        _ai([("ask_human", {"questions": [{"prompt": "which?"}]})]),
        _tool("ask_human", '{"status": "answered", "answers": []}'),
    ]
    msgs += [_ai([("read_page", {"p": "after"})]), _tool("read_page")]
    assert detect(msgs) is None


def test_errored_ask_human_does_not_reset_silence():
    # A malformed question set errors WITHOUT interrupting — the user never
    # saw a form, so no interaction happened and the count keeps climbing.
    msgs = _silent_calls(steering.SILENCE_CALLS - 2)
    msgs += [
        _ai([("ask_human", {"questions": "bad"})]),
        _tool("ask_human", '{"status": "error", "error": "bad shape"}', "error"),
    ]
    msgs += [_ai([("read_page", {"p": "after"})]), _tool("read_page")]
    signal = detect(msgs)
    assert signal and signal.kind == "silence"


# --- discipline: once per kind, cooldown ----------------------------------------


def test_once_per_kind_per_turn():
    call = ("run_sql", {"sql": "SELECT 1"})
    msgs = [_USER, _ai([call]), _tool("run_sql"), _ai([call]), _reminder("repetition")]
    # cooldown satisfied by further model calls, then the SAME duplicate again:
    msgs += [_ai([call]), _tool("run_sql"), _ai(), _ai(), _ai()]
    signal = detect(msgs)
    assert signal is None or signal.kind != "repetition"


def test_cooldown_suppresses_all_kinds():
    msgs = _error_streak(steering.ERROR_STREAK)
    msgs.append(_reminder("silence"))  # some earlier injection
    msgs.append(_ai())  # only one model call since — inside the cooldown
    assert detect(msgs) is None


def test_after_cooldown_a_new_kind_may_fire():
    msgs = [_USER, _reminder("silence")]
    msgs += _error_streak(steering.ERROR_STREAK)[1:]  # ≥3 model calls follow
    signal = detect(msgs)
    assert signal and signal.kind == "futility"


# --- the middleware --------------------------------------------------------------


def test_middleware_injects_marked_reminder():
    call = ("run_sql", {"sql": "SELECT 1"})
    state = {"messages": [_USER, _ai([call]), _tool("run_sql"), _ai([call])]}
    update = SteeringMiddleware().before_model(state)
    (msg,) = update["messages"]
    assert isinstance(msg, HumanMessage)
    assert msg.content.startswith("<system-reminder>")
    assert msg.additional_kwargs[STEERING_MARKER] == "repetition"


def test_middleware_quiet_on_healthy_turn():
    state = {"messages": [_USER, _ai([("read_page", {"p": "a"})]), _tool("read_page")]}
    assert SteeringMiddleware().before_model(state) is None


def test_async_hook_matches_sync():
    # The chat supervisor runs astream → abefore_model; the framework default
    # is a no-op, so the async override must exist and behave identically.
    call = ("run_sql", {"sql": "SELECT 1"})
    state = {"messages": [_USER, _ai([call]), _tool("run_sql"), _ai([call])]}
    update = asyncio.run(SteeringMiddleware().abefore_model(state))
    assert update and update["messages"][0].additional_kwargs[STEERING_MARKER]


def test_strip_reminder_tags():
    assert (
        steering.strip_reminder_tags("<system-reminder>step back</system-reminder>")
        == "step back"
    )
    # Total on untagged / odd input — the UI never sees the envelope.
    assert steering.strip_reminder_tags("  plain note  ") == "plain note"
    assert steering.strip_reminder_tags(None) == "None"


def test_steering_enabled_kill_switch():
    assert steering.steering_enabled({}) is True
    assert steering.steering_enabled({"OKF_CHAT_STEERING_ENABLED": "false"}) is False
    assert steering.steering_enabled({"OKF_CHAT_STEERING_ENABLED": "0"}) is False


def test_signal_texts_share_the_step_back_posture():
    # All three reminders resolve toward the user (continue with a plan,
    # correct course, or ask/tell the user) — never "stop" or "start over",
    # and never override-style commands.
    for signal in (
        Signal("repetition", steering._repetition_text("run_sql")),
        Signal("futility", steering._futility_text("run_sql", 3)),
        Signal("silence", steering._silence_text(8)),
    ):
        assert "the user" in signal.text
        assert "IGNORE" not in signal.text and "STOP" not in signal.text


def test_gate_denials_for_different_pages_do_not_fire_the_trailing_streak():
    """The guardrails gate's normal first contact: several read_page DENIALS
    for DIFFERENT pages before the guardrails read. Each denial already names
    the fix, so a 'your calls keep erroring' nudge is wrong guidance (and its
    cooldown would suppress a genuine signal right after). Identical denied
    calls still fire via the per-call error count."""
    msgs = [_USER]
    for i in range(steering.ERROR_STREAK):
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_page",
                        "args": {"concept_id": f"tables/t{i}"},
                        "id": f"d{i}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        msgs.append(
            ToolMessage(
                content='{"status": "denied", "error": "Read denied: read the usage guardrails first"}',
                name="read_page",
                tool_call_id=f"d{i}",
                status="error",
            )
        )
    assert detect(msgs) is None
    # Plain (non-denial) errors of the same shape still fire the streak.
    plain = [_USER]
    for i in range(steering.ERROR_STREAK):
        plain += [
            _ai([("run_sql", {"sql": f"SELECT {i}"})]),
            _tool("run_sql", "Error: boom", "error"),
        ]
    sig = detect(plain)
    assert sig is not None and sig.kind == "futility"
