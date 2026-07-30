"""Solver-trace capture: step extraction, the caps, and the judge rendering."""

from __future__ import annotations

import types

from harvest.benchmark.trace import (
    MAX_RESULT_CHARS,
    MAX_STEPS,
    MAX_THINKING_CHARS,
    MAX_TRACE_CHARS,
    STEP_TEXT,
    STEP_THINKING,
    STEP_TOOL_CALL,
    STEP_TOOL_RESULT,
    build_trace,
    render_for_judge,
)


def _ai(content, tool_calls=None):
    m = types.SimpleNamespace(content=content, type="ai")
    if tool_calls is not None:
        m.tool_calls = tool_calls
    return m


def _tool(name, content):
    return types.SimpleNamespace(content=content, type="tool", name=name)


def _human(text):
    return types.SimpleNamespace(content=text, type="human")


def _thinking(text):
    return {"type": "reasoning_content", "reasoning_content": {"text": text}}


def _messages():
    """A representative solver run: think → glob → read → answer."""
    return [
        _human("How many races in 2020?"),
        _ai(
            [_thinking("I need the races table."), {"type": "text", "text": "Looking."}],
            tool_calls=[{"name": "glob", "args": {"pattern": "**/*.md"}}],
        ),
        _tool("glob", "index.md\ntables/races.md"),
        _ai("", tool_calls=[{"name": "read_file", "args": {"file_path": "tables/races.md"}}]),
        _tool("read_file", "# races\nOne row per race. year is an int."),
        _ai("```sql\nSELECT COUNT(*) FROM races WHERE year = 2020\n```"),
    ]


def test_build_trace_captures_thinking_text_calls_and_results():
    t = build_trace(_messages(), sql="SELECT 1", error="")
    kinds = [s.kind for s in t.steps]
    assert kinds == [
        STEP_THINKING, STEP_TEXT, STEP_TOOL_CALL,   # think, say, glob
        STEP_TOOL_RESULT,                            # glob result
        STEP_TOOL_CALL, STEP_TOOL_RESULT,            # read_file + its result
        STEP_TEXT,                                   # the final answer
    ]
    assert t.steps[0].text == "I need the races table."
    assert t.steps[2].name == "glob" and t.steps[2].args == {"pattern": "**/*.md"}
    assert "tables/races.md" in t.steps[3].text
    # Counts describe the whole run; files_read lists the docs it opened.
    assert t.turns == 6 and t.tool_calls == 2
    assert t.files_read == ["tables/races.md"]
    assert t.sql == "SELECT 1" and t.error == "" and t.truncated is False


def test_build_trace_skips_the_question_and_system_prompt():
    # The question is shown next to the trace in the UI and must not be duplicated
    # into it (and the system prompt is fixed, so it carries no information).
    t = build_trace(
        [_human("secret question"), types.SimpleNamespace(content="rules", type="system"),
         _ai("done")],
        sql="",
    )
    assert [s.kind for s in t.steps] == [STEP_TEXT]
    assert "secret question" not in repr(t)


def test_build_trace_records_an_errored_run():
    t = build_trace([], sql="", error="ValidationException: boom")
    assert t.steps == [] and t.turns == 0
    assert t.error == "ValidationException: boom"


def test_build_trace_caps_step_text():
    long_thought = "x" * (MAX_THINKING_CHARS * 3)
    long_result = "y" * (MAX_RESULT_CHARS * 3)
    t = build_trace([_ai([_thinking(long_thought)]), _tool("read_file", long_result)], sql="")
    thinking, result = t.steps[0], t.steps[1]
    assert len(thinking.text) == MAX_THINKING_CHARS and thinking.truncated is True
    assert len(result.text) == MAX_RESULT_CHARS and result.truncated is True


def test_build_trace_caps_step_count_but_keeps_full_counts():
    # A confused solver can spin for dozens of turns; the trace must stay bounded
    # while turns/tool_calls still describe the WHOLE run (they drive the UI summary).
    messages = []
    for i in range(MAX_STEPS + 20):
        messages.append(_ai("", tool_calls=[{"name": "grep", "args": {"pattern": f"p{i}"}}]))
    t = build_trace(messages, sql="")
    assert len(t.steps) == MAX_STEPS
    assert t.truncated is True
    assert t.turns == MAX_STEPS + 20
    assert t.tool_calls == MAX_STEPS + 20


def test_build_trace_caps_total_characters():
    # The real backstop: few steps, each huge. A round of 100 of these must not blow
    # up the persisted artifact, so the whole-trace budget cuts in before MAX_STEPS.
    big = "z" * MAX_RESULT_CHARS
    t = build_trace([_tool("read_file", big) for _ in range(MAX_STEPS)], sql="")
    assert t.truncated is True
    assert len(t.steps) < MAX_STEPS
    assert sum(len(s.text) for s in t.steps) <= MAX_TRACE_CHARS


def test_build_trace_tolerates_unknown_message_shapes():
    # A provider/framework shape change must thin the trace, never raise into a solve.
    t = build_trace([{"weird": True}, _ai("ok"), None], sql="S")
    assert t.sql == "S"
    assert any(s.kind == STEP_TEXT for s in t.steps)


def test_render_for_judge_lists_turns_calls_and_files():
    text = render_for_judge(build_trace(_messages(), sql="SELECT 1"))
    assert "6 turn(s), 2 tool call(s)" in text
    assert "opened tables/races.md" in text
    assert "glob(pattern='**/*.md')" in text
    assert "[thought] I need the races table." in text


def test_render_for_judge_flags_a_solver_that_opened_nothing():
    # The "answered blind" signature — the judge must not blame the docs for it.
    text = render_for_judge(build_trace([_ai("I cannot answer.")], sql=""))
    assert "opened NO wiki files" in text


def test_ls_is_not_a_file_read():
    # ls takes `path` but a directory LISTING is not a doc read — counting it
    # used to make the answered-blind signal unreachable for any solver that
    # listed a directory first.
    t = build_trace(
        [_ai("", tool_calls=[{"name": "ls", "args": {"path": "/"}}]), _tool("ls", "index.md")],
        sql="",
    )
    assert t.files_read == []
    assert t.tool_calls == 1


def test_render_for_judge_flags_blind_solve_even_with_tool_calls():
    # A glob/grep/ls-only solve read no doc either: the blind signature must
    # fire on empty files_read regardless of the call count (it used to fall
    # through both header branches when calls > 0).
    messages = [
        _ai("", tool_calls=[{"name": "ls", "args": {"path": "/"}}]),
        _tool("ls", "index.md\ntables/"),
        _ai("", tool_calls=[{"name": "grep", "args": {"pattern": "races"}}]),
        _tool("grep", "tables/races.md:3: races"),
        _ai("The answer is 42."),
    ]
    text = render_for_judge(build_trace(messages, sql=""))
    assert "opened NO wiki files" in text


def test_render_for_judge_reports_an_error():
    text = render_for_judge(build_trace([], sql="", error="Throttling: slow down"))
    assert "errored: Throttling: slow down" in text


def test_render_for_judge_is_bounded():
    messages = [_tool("read_file", "w" * MAX_RESULT_CHARS) for _ in range(MAX_STEPS)]
    text = render_for_judge(build_trace(messages, sql=""), max_chars=500)
    assert len(text) < 1200  # the last line may overshoot the budget, not by much
    # Truncation is announced ONCE, however many caps were hit (step cap + budget).
    assert text.count("trace truncated") == 1


def test_render_for_judge_empty_without_a_trace():
    # Older runs / injected fakes carry no trace: the caller omits the section.
    assert render_for_judge(None) == ""
    assert render_for_judge(build_trace([], sql="")) == ""


def test_render_for_judge_keeps_each_step_on_one_line():
    # Multi-line tool output must not break the numbered-step rendering.
    t = build_trace([_tool("read_file", "line one\nline two\nline three")], sql="")
    body = [ln for ln in render_for_judge(t).splitlines() if ln.startswith("  1.")]
    assert len(body) == 1
    assert "line one line two line three" in body[0]


def test_render_markdown_full_fidelity():
    from harvest.benchmark.trace import SolverTrace, TraceStep, render_markdown

    trace = SolverTrace(
        steps=[
            TraceStep(kind="thinking", text="I should check the frpm doc first."),
            TraceStep(kind="tool_call", name="grep", args={"pattern": "eligible"}),
            TraceStep(kind="tool_result", text="tables/frpm.md:41: | `percent…"),
            TraceStep(kind="text", text="```sql\nSELECT 1\n```", truncated=True),
        ],
        turns=4,
        tool_calls=1,
        files_read=["tables/frpm.md"],
    )
    md = render_markdown(
        trace,
        question="What is the eligible free rate?",
        check="sql",
        run_index=1,
        outcome="FAIL",
        reason="row counts differ",
        prediction="SELECT 1",
    )
    assert "# Solve trace — sql, run 2" in md
    assert "Question: What is the eligible free rate?" in md
    assert "Outcome: FAIL — row counts differ" in md
    assert "Prediction:" in md and "SELECT 1" in md
    assert "Files read: tables/frpm.md" in md
    # Full step text survives (render_for_judge would one-line/elide these).
    assert "[thought]" in md and "I should check the frpm doc first." in md
    assert "[call] grep(pattern='eligible')" in md
    assert "[result]" in md and "tables/frpm.md:41" in md
    assert "[said]" in md
    assert "… (step truncated at capture)" in md


def test_render_markdown_marks_blind_solvers():
    from harvest.benchmark.trace import SolverTrace, render_markdown

    md = render_markdown(
        SolverTrace(turns=1, tool_calls=0),
        question="Q",
        check="sql",
        run_index=0,
    )
    assert "NONE — answered without opening any wiki file." in md
