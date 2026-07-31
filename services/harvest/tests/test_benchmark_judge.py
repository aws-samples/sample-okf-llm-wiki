"""The judge: case rendering, verdict parsing, error-counts-against-the-wiki."""

from __future__ import annotations

import asyncio
import types

import pytest

from harvest.benchmark.grader import Outcome
from harvest.benchmark.judge import (
    JUDGE_SYSTEM_PROMPT,
    VERDICT_FAIL,
    VERDICT_PASS,
    JudgeCase,
    make_judge,
    render_case,
)
from harvest.benchmark.trace import SolverTrace


class _AI:
    type = "ai"

    def __init__(self, content):
        self.content = content


class _FakeReActAgent:
    """Stands in for the built ReAct agent: replies per case, tracks concurrency."""

    def __init__(self, reply_for, tracker=None):
        self._reply_for = reply_for
        self._t = tracker or {"now": 0, "max": 0}

    async def ainvoke(self, state):
        self._t["now"] += 1
        self._t["max"] = max(self._t["max"], self._t["now"])
        await asyncio.sleep(0.005)
        self._t["now"] -= 1
        case_text = state["messages"][0][1]
        reply = self._reply_for(case_text)
        if isinstance(reply, Exception):
            raise reply
        return {"messages": [_AI(reply)]}


def _attempt(run_index, outcome, reason, prediction, trace=None):
    return types.SimpleNamespace(
        run_index=run_index, outcome=outcome, reason=reason,
        prediction=prediction, trace=trace,
    )


def _case(q_id=1, check="sql", attempts=None, passed_runs=1, total_runs=3):
    return JudgeCase(
        q_id=q_id, check=check, check_label="SQL EX",
        question="How many races in 2020?", gold="SELECT COUNT(*) ...",
        attempts=attempts if attempts is not None else [
            _attempt(0, Outcome.PASS, "result sets match", "GOOD SQL"),
            _attempt(1, Outcome.FAIL, "result sets differ", "BAD SQL"),
            _attempt(2, Outcome.FAIL, "result sets differ", "BAD SQL"),
        ],
        passed_runs=passed_runs, total_runs=total_runs,
    )


def _make(reply_for, monkeypatch, tracker=None):
    fake = _FakeReActAgent(reply_for, tracker)
    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: fake
    )
    return make_judge(object(), tools=[])


# -- case rendering -------------------------------------------------------------


def test_render_case_shows_all_attempts_with_stability():
    text = render_case(_case())
    assert "How many races in 2020?" in text
    assert "Expected (gold): SELECT COUNT(*)" in text
    assert "passed 1 of 3 run(s)" in text
    # ALL attempts, passing and failing, each labeled with its run + outcome.
    assert "Attempt (run 1) — PASS" in text
    assert "Attempt (run 2) — FAIL" in text
    assert "Attempt (run 3) — FAIL" in text
    assert "Predicted: GOOD SQL" in text and "Predicted: BAD SQL" in text


def test_render_case_includes_traces_bounded_per_attempt():
    trace = SolverTrace(turns=4, tool_calls=2, files_read=["tables/races.md"])
    text = render_case(_case(attempts=[
        _attempt(0, Outcome.FAIL, "differ", "SQL", trace=trace),
    ]))
    assert "Solver trace" in text and "opened tables/races.md" in text


def test_prompt_carries_the_carried_over_lessons():
    # The annotation posture: dataset-level fixes, never gold verbatim.
    assert "NEVER restate" in JUDGE_SYSTEM_PROMPT
    # Flaky diagnosis: diff passing vs failing attempts.
    assert "diff them" in JUDGE_SYSTEM_PROMPT


# -- verdict parsing --------------------------------------------------------------


def test_judge_parses_pass_and_fail_verdicts(monkeypatch):
    def reply_for(case_text):
        if "q-pass" in case_text:
            return '```json\n{"verdict": "pass", "comment": "gold is stale"}\n```'
        return (
            '```json\n{"verdict": "fail", "comment": "join undocumented", '
            '"annotation": "document the circuits join"}\n```'
        )

    judge = _make(reply_for, monkeypatch)
    cases = [
        JudgeCase(q_id=0, check="sql", check_label="SQL EX", question="q-pass",
                  gold="G", attempts=[], passed_runs=0, total_runs=1),
        JudgeCase(q_id=1, check="sql", check_label="SQL EX", question="q-fail",
                  gold="G", attempts=[], passed_runs=0, total_runs=1),
    ]
    verdicts = asyncio.run(judge(cases))
    by_id = {v.q_id: v for v in verdicts}
    assert by_id[0].verdict == VERDICT_PASS and by_id[0].judge_error == ""
    assert by_id[1].verdict == VERDICT_FAIL
    assert by_id[1].annotation == "document the circuits join"


def test_errored_review_is_a_confirmed_fail_never_forgiven(monkeypatch):
    judge = _make(lambda _t: RuntimeError("boom"), monkeypatch)
    verdicts = asyncio.run(judge([_case()]))
    v = verdicts[0]
    assert v.verdict == VERDICT_FAIL
    assert "boom" in v.judge_error
    assert v.annotation == ""


def test_unparseable_and_unknown_verdicts_fail_with_judge_error(monkeypatch):
    replies = iter(["no json at all", '```json\n{"verdict": "maybe"}\n```'])
    judge = _make(lambda _t: next(replies), monkeypatch)
    verdicts = asyncio.run(judge([_case(q_id=0), _case(q_id=1)]))
    assert all(v.verdict == VERDICT_FAIL for v in verdicts)
    assert all(v.judge_error for v in verdicts)


def test_bare_fail_gets_a_placeholder_comment(monkeypatch):
    judge = _make(lambda _t: '```json\n{"verdict": "fail"}\n```', monkeypatch)
    v = asyncio.run(judge([_case()]))[0]
    assert v.verdict == VERDICT_FAIL and v.comment  # never a silent empty fail


def test_cases_are_judged_concurrently_and_progress_ticks(monkeypatch):
    tracker = {"now": 0, "max": 0}
    judge = _make(
        lambda _t: '```json\n{"verdict": "pass", "comment": "c"}\n```',
        monkeypatch, tracker,
    )
    ticks = []
    cases = [_case(q_id=i) for i in range(8)]
    asyncio.run(judge(cases, on_progress=lambda d, t: ticks.append((d, t))))
    assert tracker["max"] > 1  # actually fanned out
    assert ticks[-1] == (8, 8)


def test_empty_case_list_short_circuits(monkeypatch):
    called = {"n": 0}

    def reply_for(_t):
        called["n"] += 1
        return "{}"

    judge = _make(reply_for, monkeypatch)
    assert asyncio.run(judge([])) == []
    assert called["n"] == 0


def test_prompt_names_the_on_disk_trace_tree():
    # The judge is told every attempt's full trace is grep-able under .traces/.
    assert ".traces/<check>/q<id>-run<n>.md" in JUDGE_SYSTEM_PROMPT
    assert "passing and" in JUDGE_SYSTEM_PROMPT


def _ai_with_tool_call(name, args, content="ignore this prose"):
    return types.SimpleNamespace(
        content=content, type="ai", tool_calls=[{"name": name, "args": args}]
    )


def test_verdict_comes_from_the_submit_tool_call(monkeypatch):
    # The ruling is DELIVERED by the submit_verdict tool call — its args are
    # the output, and they win over whatever the final text says (the exact
    # drift that used to grade as "unparseable verdict").
    from harvest.benchmark.judge import SUBMIT_VERDICT_TOOL

    class _Agent:
        async def ainvoke(self, _state):
            return {
                "messages": [
                    _ai_with_tool_call(
                        SUBMIT_VERDICT_TOOL,
                        {"verdict": "pass", "comment": "gold is stale",
                         "annotation": ""},
                    ),
                    _AI("I have finished my investigation. Great question!"),
                ]
            }

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: _Agent()
    )
    judge = make_judge(object(), tools=[])
    verdicts = asyncio.run(judge([_case()]))
    assert verdicts[0].verdict == "pass"
    assert verdicts[0].comment == "gold is stale"
    assert verdicts[0].judge_error == ""


def test_last_submission_supersedes_and_bad_args_still_fail_loudly(monkeypatch):
    from harvest.benchmark.judge import SUBMIT_VERDICT_TOOL

    class _Agent:
        async def ainvoke(self, _state):
            return {
                "messages": [
                    _ai_with_tool_call(
                        SUBMIT_VERDICT_TOOL, {"verdict": "maybe", "comment": "x"}
                    ),
                    _ai_with_tool_call(
                        SUBMIT_VERDICT_TOOL,
                        {"verdict": "fail", "comment": "join key undocumented"},
                    ),
                ]
            }

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: _Agent()
    )
    judge = make_judge(object(), tools=[])
    verdicts = asyncio.run(judge([_case()]))
    # The corrected re-submission (last call) is the ruling.
    assert verdicts[0].verdict == "fail"
    assert verdicts[0].comment == "join key undocumented"


def test_judge_toolset_gains_submit_verdict_and_nudge_middleware(monkeypatch):
    captured = {}

    def fake_factory(model, tools, prompt, *, extra_middleware=None):
        captured["tools"] = tools
        captured["middleware"] = extra_middleware
        return _FakeReActAgent(lambda _t: '```json\n{"verdict": "pass"}\n```')

    monkeypatch.setattr("harvest.benchmark.react.make_react_agent", fake_factory)
    judge = make_judge(object(), tools=["WIKI_TOOL"])
    asyncio.run(judge([_case()]))
    names = [getattr(t, "name", t) for t in captured["tools"]]
    assert "WIKI_TOOL" in names and "submit_verdict" in names
    assert [type(m).__name__ for m in captured["middleware"]] == [
        "SubmitToolNudgeMiddleware"
    ]


def test_param_bleed_in_tool_args_is_repaired(monkeypatch):
    # Observed on Fable 5 (report r20260730t141847, q4): the model bleeds its
    # XML tool-call dialect INTO a JSON string arg — the annotation arrives
    # trapped inside `comment` behind `</comment><parameter name="annotation">`.
    from harvest.benchmark.judge import SUBMIT_VERDICT_TOOL

    bled = (
        "the wiki failed the consumer.</comment>\n"
        '<parameter name="annotation">tables/molecule.md should document the grain'
    )

    class _Agent:
        async def ainvoke(self, _state):
            return {
                "messages": [
                    _ai_with_tool_call(
                        SUBMIT_VERDICT_TOOL, {"verdict": "fail", "comment": bled}
                    )
                ]
            }

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: _Agent()
    )
    judge = make_judge(object(), tools=[])
    v = asyncio.run(judge([_case()]))[0]
    assert v.verdict == "fail"
    assert v.comment == "the wiki failed the consumer."
    assert v.annotation == "tables/molecule.md should document the grain"
    assert "<parameter" not in v.comment + v.annotation


def test_text_form_xml_tool_call_is_recovered(monkeypatch):
    # The 8 hard failures: the ruling arrives as a TEXT-form tool call in the
    # model's XML dialect (no native tool call, not JSON) — previously graded
    # "unparseable verdict".
    class _Agent:
        async def ainvoke(self, _state):
            return {
                "messages": [
                    _AI(
                        'I will now submit.\n<invoke name="submit_verdict">\n'
                        '<parameter name="verdict">pass</parameter>\n'
                        '<parameter name="comment">gold counts NULLs; the wiki is right</parameter>\n'
                        "</invoke>"
                    )
                ]
            }

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: _Agent()
    )
    judge = make_judge(object(), tools=[])
    v = asyncio.run(judge([_case()]))[0]
    assert v.verdict == "pass"
    assert v.comment == "gold counts NULLs; the wiki is right"
    assert v.judge_error == ""


def test_invalid_tool_call_raw_args_are_recovered(monkeypatch):
    # Malformed args land in invalid_tool_calls as a raw string (never parsed,
    # graph treats the turn as final) — the ruling the model wrote must not be
    # lost.
    from harvest.benchmark.judge import SUBMIT_VERDICT_TOOL

    bad = types.SimpleNamespace(content="", type="ai", tool_calls=[])
    bad.invalid_tool_calls = [
        {
            "name": SUBMIT_VERDICT_TOOL,
            "args": '{"verdict": "fail", "comment": "join key undocumented"}',
            "error": "not valid JSON per provider",
        }
    ]

    class _Agent:
        async def ainvoke(self, _state):
            return {"messages": [bad]}

    monkeypatch.setattr(
        "harvest.benchmark.react.make_react_agent", lambda *a, **k: _Agent()
    )
    judge = make_judge(object(), tools=[])
    v = asyncio.run(judge([_case()]))[0]
    assert v.verdict == "fail"
    assert v.comment == "join key undocumented"


def test_behavior_judge_knows_the_ask_outcome():
    # The Behavior solver can end a run via its terminal ask_human tool; the
    # grader must recognize the canonical rendering and grade it BOTH ways
    # (a well-aimed ask satisfies a clarification expectation; an unnecessary
    # ask on a directly-answerable question fails).
    from harvest.benchmark.judge import BEHAVIOR_JUDGE_PROMPT
    from harvest.benchmark.solver import ASKED_PREFIX

    assert "ask_human" in BEHAVIOR_JUDGE_PROMPT
    assert ASKED_PREFIX in BEHAVIOR_JUDGE_PROMPT
    assert "unnecessary ask" in BEHAVIOR_JUDGE_PROMPT
