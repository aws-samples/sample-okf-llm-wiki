"""Solver traces — what the bundle-blind solver actually DID, per question.

A solver is an isolated ReAct graph: it reasons, greps/reads wiki docs, and settles
on SQL. Until now only the SQL survived, so both consumers of a failure were
guessing from the SQL alone:

* the **adjudicator** (the judge) could not tell "the wiki never says this"
  (GENUINE_ERROR) from "the wiki says it plainly and the solver never opened that
  doc" (AMBIGUOUS — not a wiki gap) — the exact distinction its taxonomy turns on;
* the **human** reviewing a round had no way to see why an answer went wrong.

So each solve is captured as a bounded :class:`SolverTrace`: the reasoning, the tool
calls with their arguments, a preview of each tool result, and the files read. It is
rendered compactly into the judge's prompt (:func:`render_for_judge`) and persisted
off-mount for the UI (the report's companion ``traces.json`` — see
:mod:`.report_store`).

**Everything here is bounded.** A 100-question round × up to 5 rounds must not blow
up the persisted artifact or the judge's context, so steps, per-step text, and the
whole trace each have a hard cap and the result records that it was truncated. This
module is pure (no agent/AWS imports) so it is unit-testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harvest.benchmark.extract import message_text, reasoning_text

# Step kinds. Stable strings: the persisted JSON and the UI switch on these.
STEP_THINKING = "thinking"
STEP_TEXT = "text"
STEP_TOOL_CALL = "tool_call"
STEP_TOOL_RESULT = "tool_result"

# Caps. Per-step first, then a whole-trace character budget as the real backstop —
# a confused solver can spend 40 turns reading big docs, and 100 of those per round
# is what would make the artifact (and the judge prompt) unaffordable.
MAX_STEPS = 40
MAX_THINKING_CHARS = 700
MAX_TEXT_CHARS = 700
MAX_RESULT_CHARS = 500
MAX_ARG_CHARS = 200
MAX_TRACE_CHARS = 8000
MAX_FILES_READ = 40

# The judge sees a tighter rendering than the UI: it is one of ~N failures in a
# round, each classified by its own model call.
JUDGE_MAX_CHARS = 3500

# Tool-call argument names that identify a file the solver READ (for files_read).
# Only read_file's arg qualifies: ls takes `path` (a directory listing is not a
# doc read — counting it as one made the judge's load-bearing "answered blind"
# signal unreachable for any solver that listed a directory first).
_PATH_ARGS = ("file_path",)


@dataclass
class TraceStep:
    """One step of a solver's run: a thought, a said line, a tool call, or a result.

    ``args`` is only set for ``tool_call`` (the call's arguments, each value capped);
    ``name`` only for the tool kinds. ``truncated`` means ``text`` was cut to a cap —
    surfaced so a reader knows the elision is ours, not the model's.
    """

    kind: str
    name: str = ""
    args: dict = field(default_factory=dict)
    text: str = ""
    truncated: bool = False


@dataclass
class SolverTrace:
    """One solver's bounded, replayable record for one question.

    ``turns``/``tool_calls`` are the FULL counts (they describe the whole run even
    when ``steps`` was truncated), ``files_read`` the distinct doc paths the solver
    opened, ``sql`` the SQL it settled on, and ``error`` any exception that killed
    the run. ``truncated`` is True when steps were dropped to stay inside the caps.
    """

    steps: list[TraceStep] = field(default_factory=list)
    turns: int = 0
    tool_calls: int = 0
    # Per-tool-name call counts over the WHOLE run (like turns/tool_calls, these
    # describe the full run even when ``steps`` was truncated) — the report's
    # per-tool distribution telemetry comes from folding these.
    tool_counts: dict[str, int] = field(default_factory=dict)
    files_read: list[str] = field(default_factory=list)
    sql: str = ""
    error: str = ""
    truncated: bool = False


@dataclass
class SolveResult:
    """What the real solver returns: the prediction plus how it got there.

    ``sql`` is the check's prediction string (SQL text).
    ``usage`` is the solve's token fold (``{input_tokens, output_tokens,
    total_tokens}``) and ``wall_ms`` its wall time — the report's telemetry.
    The round orchestrator accepts EITHER this or a bare string, so an injected
    test fake can stay ``async def solve(q) -> str``.
    """

    sql: str
    trace: SolverTrace | None = None
    usage: dict = field(default_factory=dict)
    wall_ms: int = 0


def build_trace(messages: list, *, sql: str = "", error: str = "") -> SolverTrace:
    """Turn a finished ReAct message list into a bounded :class:`SolverTrace`.

    Walks the messages in order, emitting a step per reasoning block, per assistant
    text, per tool call, and per tool result. Deliberately tolerant of message shape
    (LangChain objects, ``SimpleNamespace``, dicts) — a trace is observability, so a
    provider shape change must degrade to a thinner trace, never raise into a solve.

    On overflow the FIRST steps are kept: the opening moves (what it searched for,
    which docs it opened) are what explain a wrong answer, and the settled SQL is
    carried on ``sql`` regardless, so nothing load-bearing is lost.
    """
    steps: list[TraceStep] = []
    files_read: list[str] = []
    tool_calls = 0
    tool_counts: dict[str, int] = {}
    budget = MAX_TRACE_CHARS
    truncated = False

    for message in messages or []:
        kind = str(getattr(message, "type", "") or "")
        if kind in ("human", "system"):
            # The question is shown alongside the trace; the system prompt is fixed.
            continue

        pending: list[TraceStep] = []
        if kind == "tool":
            pending.append(
                _step(
                    STEP_TOOL_RESULT,
                    name=str(getattr(message, "name", "") or ""),
                    text=message_text(message),
                    cap=MAX_RESULT_CHARS,
                )
            )
        else:
            thinking = reasoning_text(message)
            if thinking.strip():
                pending.append(
                    _step(STEP_THINKING, text=thinking, cap=MAX_THINKING_CHARS)
                )
            said = message_text(message)
            if said.strip():
                pending.append(_step(STEP_TEXT, text=said, cap=MAX_TEXT_CHARS))
            for call in _tool_calls(message):
                tool_calls += 1
                name, args = _call_name_and_args(call)
                if name:
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                pending.append(_step(STEP_TOOL_CALL, name=name, args=args))
                for path in _paths_in(args):
                    if path not in files_read and len(files_read) < MAX_FILES_READ:
                        files_read.append(path)

        # Past a cap we stop COLLECTING steps but keep walking the messages, so
        # ``turns``/``tool_calls``/``files_read`` still describe the whole run.
        for step in pending:
            if len(steps) >= MAX_STEPS or budget <= 0:
                truncated = True
                continue
            steps.append(step)
            budget -= _step_cost(step)

    return SolverTrace(
        steps=steps,
        turns=len(messages or []),
        tool_calls=tool_calls,
        tool_counts=tool_counts,
        files_read=files_read,
        sql=sql or "",
        error=error or "",
        truncated=truncated,
    )


def render_for_judge(trace: Any, *, max_chars: int = JUDGE_MAX_CHARS) -> str:
    """Render a trace as compact text for the adjudicator's classify prompt.

    Returns "" for a missing/empty trace so the caller can simply omit the section
    (older runs and injected fakes carry no trace). The rendering is deliberately
    terse — the judge needs to see WHICH docs the solver opened and what it
    concluded, not to re-read the docs through the trace (it has its own file tools,
    rooted at the real mount, for that).
    """
    if trace is None:
        return ""
    steps = list(getattr(trace, "steps", None) or [])
    turns = int(getattr(trace, "turns", 0) or 0)
    calls = int(getattr(trace, "tool_calls", 0) or 0)
    files = list(getattr(trace, "files_read", None) or [])
    error = str(getattr(trace, "error", "") or "")
    if not steps and not error and not turns:
        return ""

    header = f"{turns} turn(s), {calls} tool call(s)"
    if files:
        header += "; opened " + ", ".join(files)
    else:
        # The "answered blind" signature — load-bearing for the judge: a solver
        # that never opened a doc says nothing about whether the doc was right.
        # Unconditional on the call count: an ls/glob/grep-only solve read no
        # doc either, and used to fall through both branches losing the signal.
        header += "; opened NO wiki files"
    lines = [f"Solver trace ({header}):"]
    if error:
        lines.append(f"  ! the solver run errored: {error}")

    used = sum(len(line) for line in lines)
    elided = bool(getattr(trace, "truncated", False))
    for i, step in enumerate(steps, start=1):
        line = f"  {i}. {_render_step(step)}"
        if used + len(line) > max_chars:
            elided = True
            break
        lines.append(line)
        used += len(line)
    if elided:
        lines.append("  … (trace truncated)")
    return "\n".join(lines)


def render_markdown(
    trace: Any,
    *,
    question: str,
    check: str,
    run_index: int,
    outcome: str = "",
    reason: str = "",
    prediction: str = "",
) -> str:
    """Render one attempt's trace as a standalone markdown file.

    This is the judge's ``.traces/`` tree: unlike :func:`render_for_judge`
    (a one-line-per-step prompt summary), this keeps every kept step's WHOLE
    captured text, so the judge's ``grep`` across the files matches content
    the inline rendering elides. Gold-free by construction — the solver never
    saw any gold, so nothing here can leak one.
    """
    lines = [
        f"# Solve trace — {check}, run {run_index + 1}",
        "",
        f"Question: {question}",
    ]
    if outcome:
        lines.append(f"Outcome: {outcome}" + (f" — {reason}" if reason else ""))
    if prediction:
        lines += ["", "Prediction:", "```", prediction, "```"]

    steps = list(getattr(trace, "steps", None) or [])
    files = list(getattr(trace, "files_read", None) or [])
    turns = int(getattr(trace, "turns", 0) or 0)
    calls = int(getattr(trace, "tool_calls", 0) or 0)
    error = str(getattr(trace, "error", "") or "")
    lines += ["", f"{turns} turn(s), {calls} tool call(s)."]
    lines.append(
        "Files read: "
        + (", ".join(files) if files else "NONE — answered without opening any wiki file.")
    )
    if error:
        lines.append(f"Run error: {error}")

    lines += ["", "## Steps", ""]
    for i, step in enumerate(steps, start=1):
        kind = str(getattr(step, "kind", "") or "")
        name = str(getattr(step, "name", "") or "")
        text = str(getattr(step, "text", "") or "")
        if kind == STEP_TOOL_CALL:
            lines.append(f"{i}. [call] {name}({_render_args(getattr(step, 'args', None))})")
        else:
            label = {STEP_TOOL_RESULT: "result", STEP_THINKING: "thought"}.get(
                kind, "said"
            )
            lines.append(f"{i}. [{label}]")
            lines.append(text if text else "(empty)")
        if getattr(step, "truncated", False):
            lines.append("… (step truncated at capture)")
        lines.append("")
    if bool(getattr(trace, "truncated", False)):
        lines.append("… (trace truncated at capture)")
    return "\n".join(lines).rstrip() + "\n"


def _render_step(step: Any) -> str:
    kind = str(getattr(step, "kind", "") or "")
    name = str(getattr(step, "name", "") or "")
    text = _one_line(str(getattr(step, "text", "") or ""))
    if kind == STEP_TOOL_CALL:
        return f"{name}({_render_args(getattr(step, 'args', None))})"
    if kind == STEP_TOOL_RESULT:
        return f"→ {name or 'result'}: {text}" if text else f"→ {name or 'result'}: (empty)"
    if kind == STEP_THINKING:
        return f"[thought] {text}"
    return f"[said] {text}"


def _render_args(args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    return ", ".join(f"{k}={_one_line(str(v))!r}" for k, v in args.items())


def _one_line(text: str) -> str:
    """Collapse whitespace — a trace line must stay one line in the prompt."""
    return " ".join(text.split())


def _step(kind: str, *, name: str = "", text: str = "", args: dict | None = None,
          cap: int | None = None) -> TraceStep:
    body, truncated = (text, False) if cap is None else _truncate(text, cap)
    return TraceStep(
        kind=kind,
        name=name,
        args=dict(args or {}),
        text=body,
        truncated=truncated,
    )


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    s = text or ""
    if len(s) <= cap:
        return s, False
    return s[:cap], True


def _step_cost(step: TraceStep) -> int:
    """Roughly the characters a step contributes to the whole-trace budget."""
    return len(step.text) + len(step.name) + sum(
        len(str(k)) + len(str(v)) for k, v in (step.args or {}).items()
    )


def _tool_calls(message: Any) -> list:
    calls = getattr(message, "tool_calls", None)
    if not calls and isinstance(message, dict):
        calls = message.get("tool_calls")
    return list(calls or [])


def _call_name_and_args(call: Any) -> tuple[str, dict]:
    """A tool call's name + capped args, tolerating dict or object shape."""
    if isinstance(call, dict):
        name, raw = call.get("name"), call.get("args")
    else:
        name, raw = getattr(call, "name", ""), getattr(call, "args", None)
    args: dict = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            args[str(key)] = _truncate(_one_line(str(value)), MAX_ARG_CHARS)[0]
    return str(name or ""), args


def _paths_in(args: dict) -> list[str]:
    """The file path(s) a tool call names, for the trace's ``files_read`` list."""
    return [str(args[a]) for a in _PATH_ARGS if args.get(a)]
