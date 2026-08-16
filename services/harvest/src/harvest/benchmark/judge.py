"""The judge — the run's LLM review roles, sharing one diagnostician toolset.

Two distinct jobs live here:

**Failure review** (:func:`make_judge`) — one mandatory pass over every failed
(question, check) of a DETERMINISTICALLY-graded check. Runs ONCE, after ALL N
runs complete (never per-run). Per question, only its failed checks reach the
judge (failed = at least one failing attempt across the N runs);
fully-passing questions never do. The judge sees **all N attempts, passing and
failing** — for flaky questions the diff between a solve that worked and one
that didn't is the diagnosis ("three runs used the documented join; two never
found it") — plus each attempt's solver trace.

**Behavior grading** (:func:`make_behavior_grader`) — the Behavior check has
no deterministic grade at all: the judge IS the grader, ruling on every
(question, run) attempt independently against the row's free-form
``expected_behavior``. Because the grader is already the judge, behavior
pairs never enter the OVERTURN review (the judge would be overturning itself)
and there is no judge-adjusted score for the check.

**Behavior synthesis review** (:func:`make_behavior_reviewer`) — after the
per-run gradings, each behavior question with ≥1 failing run gets ONE
question-level review that sees all N graded attempts (with their per-run
rulings) together — the cross-run diff the independent gradings deliberately
never had. It produces the question-level ``comment`` and ONE consolidated
``annotation``; it NEVER changes outcomes (its verdict is structurally
``fail`` — the pair failed; there is nothing to overturn).

Both roles hold the same diagnostician toolset the RI adjudicator had:
read-only file tools over the wiki/.metadata/.context tree and live
run_sql/sample_rows.

Output contract, shared by both roles — DELIVERED by calling the
``submit_verdict`` tool (args are the output, structured by construction; a
legacy fenced-JSON final message still parses as a fallback):

    {"verdict": "pass" | "fail", "comment": "...", "annotation": "..."}

* review ``pass`` — overturned: not the wiki's fault (bad gold, ambiguous
  question, over-strict grading). Counts toward the judge-adjusted score.
  Behavior ``pass`` — the run satisfied the expectation.
* ``fail`` — confirmed / expectation violated: ``comment`` is mandatory;
  ``annotation`` is filled when there is room for wiki improvement — a
  dataset-level doc fix a human can review and apply.

One lesson from the old adjudicator carries over: an unparseable/errored review
**counts against the wiki** (never silently forgiven — the 0%-EX/100%-judge
false-success fix). Deferred agent-framework imports keep the module importable
offline.
"""

from __future__ import annotations

import asyncio
import re
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from harvest.benchmark.extract import extract_json, message_text
from harvest.benchmark.grader import Outcome
from harvest.benchmark.trace import render_for_judge

log = logging.getLogger("harvest.benchmark.judge")

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"

_DEFAULT_CONCURRENCY = 10

# Whole-case budget for rendered solver traces: split across the N attempts so
# a 5-run case stays the same order of size as a 1-run case.
_CASE_TRACE_BUDGET = 9000
_MIN_ATTEMPT_TRACE = 1200


def _concurrency() -> int:
    try:
        return max(1, int(os.environ.get("OKF_BENCHMARK_MAX_CONCURRENCY", "")))
    except (TypeError, ValueError):
        return _DEFAULT_CONCURRENCY


@dataclass
class JudgeCase:
    """One failed (question, check) with every attempt across the N runs.

    ``attempts`` items are duck-typed records carrying ``run_index``,
    ``outcome`` (:class:`~harvest.benchmark.grader.Outcome`), ``reason``,
    ``prediction`` and ``trace`` (see ``report_run.Attempt``). ``gold`` is that
    check's gold cell — the judge is the one role allowed to see it.
    """

    q_id: int
    check: str
    check_label: str
    question: str
    gold: str
    attempts: list[Any] = field(default_factory=list)
    passed_runs: int = 0
    total_runs: int = 0


@dataclass
class JudgeVerdict:
    """The judge's ruling on one case.

    ``verdict`` is ``pass`` (overturned — not the wiki's fault) or ``fail``
    (confirmed). ``judge_error`` is non-empty when the review itself errored or
    couldn't be parsed — such a case is ALWAYS a ``fail`` (an errored review is
    not evidence the wiki is fine), and the error is surfaced on the report so
    a human can see the judge fell over rather than mistaking it for a ruling.
    """

    q_id: int
    check: str
    verdict: str
    comment: str = ""
    annotation: str = ""
    judge_error: str = ""
    # Token fold over the review's messages (report telemetry; {} on error).
    usage: dict = field(default_factory=dict)


# The three judge hats hold the SAME toolset (studio._judge_toolset), so its
# description is ONE shared block — the per-prompt copies had already started
# drifting (wording diverged across hats while the tools stayed identical).
# The `.traces/` block is SEPARATE and deliberately composed only into the two
# hats that have trace files: the behavior GRADER runs before ``before_judge``
# lays the traces down (its case's trace rides the user message instead), and
# a primer naming files the agent can't find teaches it to make failing calls.
_JUDGE_TOOLS_BLOCK = """\
WIKI + source files:
- `read_file(path)` — read a file by path. The WIKI docs the agent had are under \
`tables/`, `references/`, `datasets/`, `index.md`. The schema snapshot is under \
`.metadata/` (e.g. `.metadata/tables/<name>.md`, `.metadata/columns.tsv`). \
Uploaded source docs are under `.context/`.
- `glob(pattern)` / `grep(pattern)` / `ls(path)` — find and search across those \
files (e.g. `grep` the wiki for a column name to see whether/how it's documented).
- `run_code(code)` — WHEN AVAILABLE: Python in an isolated sandbox holding the \
`.context/` files under `/tmp/okf_context/`. Use it to extract text from binary \
uploads (PDF/DOCX/PPTX/XLSX) that `read_file` only base64-encodes — e.g. check \
whether an uploaded spec states the fact the wiki missed.
Live DATA:
- `run_sql(query)` — read-only SQL against the real dataset; also `DESCRIBE \
<table>`, `SHOW COLUMNS FROM <table>`, `SELECT DISTINCT <col> ... LIMIT` for \
code legends.
- `sample_rows(concept_id)` — a few sample rows for a table concept id like \
`tables/races` (a concept id, NOT a file path).
"""

_JUDGE_TRACES_BLOCK = """\
Solve traces on disk:
- EVERY attempt's full solve trace — every question, every run, passing and \
failing alike — is under `.traces/<check>/q<id>-run<n>.md` (e.g. \
`.traces/sql/q007-run2.md`). The per-case summaries in your prompt elide long \
steps; `read_file` a trace file for the full text, and `grep` ACROSS them for \
systemic patterns no single case shows ("did ANY run find the doc that answers \
this?", "do all failures pick the same wrong column?").
"""

JUDGE_SYSTEM_PROMPT = (
    """\
You are judging why a text-to-SQL agent (which had ONLY the data wiki — the \
authored markdown docs, not the raw schema) failed a benchmark check. For each \
case you see the question, the expected (gold) answer for that check, and EVERY \
attempt across the independent runs — passing and failing — each with its \
grading reason and the agent's own solve trace. Unlike that agent, you can see \
everything, via these read-only tools:

"""
    + _JUDGE_TOOLS_BLOCK
    + _JUDGE_TRACES_BLOCK
    + """
Your method for each case:
1. From the failing attempts, identify what the agent got wrong (a column, join \
key, code legend, unit, filter, grain).
2. Check the DATA (`run_sql`/`sample_rows`) to learn what is actually true — \
including whether the GOLD itself is right against the data.
3. Check the WIKI (`read_file`/`grep`) to see whether the docs the agent had \
state that truth — clearly and findably.
4. Read the attempts' solve traces as EVIDENCE about the docs, not as a verdict: \
if some runs passed and others failed, diff them — what did the passing run \
read that the failing one didn't? An attempt that opened the right doc and \
still failed points at unclear wording; one that searched the natural terms \
and never found the doc points at a findability gap; one that opened no files \
says nothing about the docs.

Then rule:
- "pass" (OVERTURN) — the wiki is NOT at fault: the gold is wrong or odd \
against the real data, the question is genuinely under-specified, or the \
grading was over-strict for an arguably-correct answer.
- "fail" (CONFIRM) — the wiki failed the consumer. `comment` is MANDATORY: say \
why, concretely. When there is room for wiki improvement, ALSO fill \
`annotation` with the doc-level fix, phrased as dataset-level guidance an \
author can apply (e.g. "state explicitly that pit-stop durations are not \
tracked in this dataset", "tables/results.md should document that `status` is \
an int code, 1=active") — name the page when the failure implicates a specific \
one. NEVER restate the benchmark question or the gold answer/SQL verbatim; \
describe the missing or unclear FACT. Leave `annotation` empty when the \
failure isn't actionable in the docs.

When done investigating, deliver the ruling by calling \
`submit_verdict(verdict, comment, annotation)` — that tool call IS your \
output; plain text is never read as a verdict. Submit once, then finish."""
)


# --------------------------------------------------------------------------- #
# Verdict delivery: a SUBMIT TOOL, not fence-parsed prose
# --------------------------------------------------------------------------- #
#
# The judge hats used to end with a fenced JSON object that we parsed out of
# the final message — and models drifted: a thorough investigation followed by
# a narrated verdict the parser couldn't read, graded as "unparseable". The
# ruling is now DELIVERED by calling a tool (`submit_verdict` for the two
# verdict-bearing hats, `submit_review` for the synthesis reviewer): the
# tool-call args are the output, structured by construction — the same move as
# the aggregator's `write_final_annotation`. Extraction reads the LAST such
# tool call from the conversation (a re-submission supersedes), and the old
# fence-parse remains as a compatibility fallback. A
# :class:`~harvest.benchmark.react.SubmitToolNudgeMiddleware` nudges an agent
# that tries to finish without calling the tool — at most twice, then the
# existing unparseable-output path rules (fail with ``judge_error``), so a
# deaf model can never spin.

SUBMIT_VERDICT_TOOL = "submit_verdict"
SUBMIT_REVIEW_TOOL = "submit_review"

_VERDICT_NUDGE = (
    "You have not delivered a ruling yet — plain text is NOT read as your "
    "verdict. Call `submit_verdict(verdict, comment, annotation)` now with "
    "your final ruling; then finish."
)
_REVIEW_NUDGE = (
    "You have not delivered the diagnosis yet — plain text is NOT read as "
    "your review. Call `submit_review(comment, annotation)` now with the "
    "question-level diagnosis; then finish."
)


def _make_submit_verdict_tool() -> Any:
    """The ruling-delivery tool for the verdict-bearing hats (judge + grader).

    Validates eagerly so a malformed call is corrected IN the loop (the tool
    message says what to fix) instead of surfacing post-hoc as a judge_error.
    """
    from langchain_core.tools import tool

    @tool
    def submit_verdict(verdict: str, comment: str, annotation: str = "") -> str:
        """Deliver your final ruling — calling this tool IS your output (plain
        text is not read). `verdict` is "pass" or "fail"; `comment` is
        mandatory on fail (say concretely why); `annotation` optionally
        carries the dataset-level doc fix, else leave it empty."""
        v = (verdict or "").strip().lower()
        if v not in (VERDICT_PASS, VERDICT_FAIL):
            return (
                f"rejected: verdict must be '{VERDICT_PASS}' or "
                f"'{VERDICT_FAIL}' (got {verdict!r}) — call submit_verdict again"
            )
        if v == VERDICT_FAIL and not (comment or "").strip():
            return (
                "rejected: a fail verdict requires a concrete comment — "
                "call submit_verdict again"
            )
        return "verdict recorded — you can finish now"

    return submit_verdict


def _make_submit_review_tool() -> Any:
    """The diagnosis-delivery tool for the synthesis reviewer (no verdict)."""
    from langchain_core.tools import tool

    @tool
    def submit_review(comment: str, annotation: str = "") -> str:
        """Deliver the question-level diagnosis — calling this tool IS your
        output (plain text is not read). `comment` is mandatory; `annotation`
        optionally carries ONE consolidated dataset-level doc fix, else leave
        it empty."""
        if not (comment or "").strip():
            return "rejected: the comment is mandatory — call submit_review again"
        return "review recorded — you can finish now"

    return submit_review


def _submitted_args(messages: list, tool_name: str) -> dict | None:
    """The LAST ``tool_name`` call's args in ``messages``, or None.

    Reverse scan so a corrected re-submission supersedes an earlier (possibly
    rejected) one. Args arrive as the model's structured tool-call payload —
    no fence parsing, no JSON repair.
    """
    for m in reversed(messages or []):
        if getattr(m, "type", "") not in ("ai", "assistant"):
            continue
        for tc in reversed(getattr(m, "tool_calls", None) or []):
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name != tool_name:
                continue
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
            return args if isinstance(args, dict) else None
    return None


# Claude 5's tool-call dialect can BLEED into the delivered output: observed on
# Fable 5, the model writes its internal XML parameter markup either inside a
# JSON string arg ("comment": "…</comment>\n<parameter name=\"annotation\">…")
# or as the whole final message (a text-form tool call the JSON fallback can't
# read). These regexes recover from both: `_XML_PARAM_RE` lifts
# `<parameter name="x">value` segments, `_XML_MARKER_RE` finds/strips the
# markers themselves.
_XML_PARAM_RE = re.compile(
    r'<parameter\s+name="([a-zA-Z_]+)">\s*(.*?)\s*'
    r"(?=<parameter\s+name=|</parameter>|</invoke>|</function_calls>|$)",
    re.S,
)
_XML_MARKER_RE = re.compile(
    r"</?(?:parameter|invoke|function_calls|antml[\w:]*|comment|annotation|verdict)\b[^>]*>"
)


def _repair_param_bleed(parsed: dict) -> dict:
    """Split XML parameter markup that bled INTO a delivered string value.

    ``{"comment": "…wiki at fault.</comment><parameter name=\"annotation\">fix…"}``
    becomes ``{"comment": "…wiki at fault.", "annotation": "fix…"}`` — the
    trapped fields fill only keys that are absent/empty (a properly delivered
    value always wins), and the markers themselves never reach the report.
    """
    out = dict(parsed)
    harvested: dict[str, str] = {}
    for key, val in list(out.items()):
        if not isinstance(val, str):
            continue
        m = _XML_MARKER_RE.search(val)
        if not m:
            continue
        for pk, pv in _XML_PARAM_RE.findall(val):
            harvested.setdefault(pk, _XML_MARKER_RE.sub("", pv).strip())
        out[key] = val[: m.start()].rstrip()
    for key, val in harvested.items():
        if val and not str(out.get(key) or "").strip():
            out[key] = val
    return out


def _xmlish_fields(text: str | None) -> dict | None:
    """Parse a text-form (XML-dialect) tool call: the whole ruling as markup."""
    if not text or "<parameter" not in text:
        return None
    fields = {
        k: _XML_MARKER_RE.sub("", v).strip() for k, v in _XML_PARAM_RE.findall(text)
    }
    return fields if ("verdict" in fields or "comment" in fields) else None


def _invalid_submitted_raw(messages: list, tool_name: str) -> str | None:
    """The LAST ``tool_name`` INVALID tool call's raw args string, if any.

    A malformed args payload lands in ``invalid_tool_calls`` (raw string, never
    parsed) and the graph treats the turn as final — without this the ruling
    the model DID write is simply lost.
    """
    for m in reversed(messages or []):
        if getattr(m, "type", "") not in ("ai", "assistant"):
            continue
        for tc in reversed(getattr(m, "invalid_tool_calls", None) or []):
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name != tool_name:
                continue
            raw = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
            if isinstance(raw, str) and raw.strip():
                return raw
    return None


def _parse_ruling(messages: list, tool_name: str) -> dict | None:
    """A hat's delivered output, most-structured source first.

    1. the submit tool's parsed args (the contract);
    2. an INVALID submit call's raw args (tolerant JSON, then XML-dialect);
    3. the final text (legacy fenced JSON, then XML-dialect text-form call).
    Whatever arrives is passed through :func:`_repair_param_bleed` so Claude 5
    XML bleed inside string values never reaches the report.
    """
    parsed = _submitted_args(messages, tool_name)
    if parsed is None:
        raw = _invalid_submitted_raw(messages, tool_name)
        if raw:
            parsed = extract_json(raw, default=None)
            if not isinstance(parsed, dict):
                parsed = _xmlish_fields(raw)
    if parsed is None:
        text = _last_ai_text(messages)
        parsed = extract_json(text, default=None)
        if not isinstance(parsed, dict):
            parsed = _xmlish_fields(text)
    return _repair_param_bleed(parsed) if isinstance(parsed, dict) else None


def render_case(case: JudgeCase) -> str:
    """Render one case as the judge's user message (bounded).

    Every attempt appears with its run number, outcome, grading reason,
    prediction, and a trace rendering that shares :data:`_CASE_TRACE_BUDGET`
    across the attempts — so a 5-run case doesn't quintuple the prompt.
    """
    lines = [
        f"Question: {case.question}",
        f"Check: {case.check_label} ({case.check})",
        f"Expected (gold): {case.gold}",
        f"Stability: passed {case.passed_runs} of {case.total_runs} run(s).",
    ]
    per_attempt = max(
        _MIN_ATTEMPT_TRACE, _CASE_TRACE_BUDGET // max(1, len(case.attempts))
    )
    for a in case.attempts:
        outcome = getattr(a, "outcome", None)
        outcome_s = outcome.value if isinstance(outcome, Outcome) else str(outcome)
        lines.append("")
        lines.append(
            f"Attempt (run {getattr(a, 'run_index', 0) + 1}) — {outcome_s}: "
            f"{getattr(a, 'reason', '')}"
        )
        prediction = str(getattr(a, "prediction", "") or "")
        if prediction:
            lines.append(f"Predicted: {prediction}")
        trace = render_for_judge(getattr(a, "trace", None), max_chars=per_attempt)
        if trace:
            lines.append(trace)
    return "\n".join(lines)


def make_judge(
    chat_model: Any,
    tools: list[Any] | Callable[[], list[Any]],
) -> Callable[..., Awaitable[list[JudgeVerdict]]]:
    """Build an async ``judge(cases, on_progress=None) -> list[JudgeVerdict]``.

    ``chat_model`` is the run's configured judge model (its own instrumented
    instance, so judge tokens meter as their own scope). ``tools`` is the
    diagnostician toolset — a list or a zero-arg factory (deferred so building
    the file tools, which imports deepagents, happens at first judge use;
    mirrors the solver). Cases are judged CONCURRENTLY under the shared LLM
    semaphore, and a case whose review raises or returns nothing parseable
    comes back as ``fail`` with ``judge_error`` set — never forgiven, never a
    crash of the phase.
    """
    built: dict[str, Any] = {}

    def _ensure_built():
        if built:
            return built
        from harvest.benchmark.checks import with_run_date
        from harvest.benchmark.react import SubmitToolNudgeMiddleware, make_react_agent

        resolved = tools() if callable(tools) else tools
        built["agent"] = make_react_agent(
            chat_model,
            [*resolved, _make_submit_verdict_tool()],
            with_run_date(JUDGE_SYSTEM_PROMPT),
            extra_middleware=[
                SubmitToolNudgeMiddleware(SUBMIT_VERDICT_TOOL, _VERDICT_NUDGE)
            ],
        )
        return built

    async def _judge_one(agent: Any, case: JudgeCase) -> JudgeVerdict:
        from harvest.benchmark.solver import fold_usage

        try:
            out = await agent.ainvoke({"messages": [("user", render_case(case))]})
        except Exception as e:  # noqa: BLE001 - an errored review counts against the wiki
            return JudgeVerdict(
                q_id=case.q_id, check=case.check, verdict=VERDICT_FAIL,
                comment="the judge review errored; treated as a confirmed failure",
                judge_error=f"{type(e).__name__}: {e}",
            )
        messages = out.get("messages", [])
        usage = fold_usage(messages)
        parsed = _parse_ruling(messages, SUBMIT_VERDICT_TOOL)
        if not isinstance(parsed, dict):
            return JudgeVerdict(
                q_id=case.q_id, check=case.check, verdict=VERDICT_FAIL,
                comment="the judge returned no parseable verdict; treated as a confirmed failure",
                judge_error="unparseable verdict", usage=usage,
            )
        verdict = str(parsed.get("verdict") or "").strip().lower()
        comment = str(parsed.get("comment") or "").strip()
        annotation = str(parsed.get("annotation") or "").strip()
        if verdict not in (VERDICT_PASS, VERDICT_FAIL):
            return JudgeVerdict(
                q_id=case.q_id, check=case.check, verdict=VERDICT_FAIL,
                comment=comment
                or "the judge returned an unknown verdict; treated as a confirmed failure",
                annotation=annotation,
                judge_error=f"unknown verdict {verdict!r}", usage=usage,
            )
        if verdict == VERDICT_FAIL and not comment:
            # The contract makes the comment mandatory on fail; a bare fail is
            # still a fail, but flag the gap so the report shows it.
            comment = "(the judge confirmed the failure but gave no comment)"
        return JudgeVerdict(
            q_id=case.q_id, check=case.check, verdict=verdict,
            comment=comment, annotation=annotation, usage=usage,
        )

    async def judge(
        cases: list[JudgeCase],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[JudgeVerdict]:
        if not cases:
            return []
        agent = _ensure_built()["agent"]
        sem = asyncio.Semaphore(_concurrency())
        total = len(cases)
        done = 0

        async def _one(case: JudgeCase) -> JudgeVerdict:
            nonlocal done
            async with sem:
                verdict = await _judge_one(agent, case)
            done += 1
            if on_progress:
                on_progress(done, total)
            return verdict

        return await asyncio.gather(*[_one(c) for c in cases])

    return judge


# --------------------------------------------------------------------------- #
# Behavior grading — the judge as the grader, one ruling per (question, run)
# --------------------------------------------------------------------------- #


@dataclass
class BehaviorCase:
    """One (question, run) attempt of a judge-graded check, to be graded.

    ``attempt`` is the duck-typed record carrying ``prediction``/``trace``
    (see ``report_run.Attempt``); ``expected`` is the row's free-form
    ``expected_behavior`` — the one gold cell the judge may see.
    """

    q_id: int
    check: str
    run_index: int
    question: str
    expected: str
    attempt: Any = None


@dataclass
class BehaviorGrade:
    """The judge's ruling on one behavior attempt.

    ``verdict`` is ``pass`` (the run satisfied the expectation) or ``fail``.
    ``judge_error`` non-empty means the review itself errored/was unparseable —
    always a ``fail`` (an errored review is not evidence the run behaved),
    surfaced so a human sees the judge fell over rather than a real ruling.
    """

    q_id: int
    check: str
    run_index: int
    verdict: str
    comment: str = ""
    annotation: str = ""
    judge_error: str = ""
    usage: dict = field(default_factory=dict)


BEHAVIOR_JUDGE_PROMPT = (
    """\
You are grading ONE run of a wiki-consumer agent against a free-form \
EXPECTED-BEHAVIOR specification written by the dataset's owner. The agent had \
ONLY the data wiki (the authored markdown docs — not the raw schema, not the \
data). You see its final answer and its full solve trace, and unlike the agent \
you can see everything, via these read-only tools:

"""
    # No _JUDGE_TRACES_BLOCK: this hat grades BEFORE before_judge lays the
    # trace files down — its one attempt's trace rides the user message.
    + _JUDGE_TOOLS_BLOCK
    + """
The expectation is free-form: it may demand a correct value, a refusal, \
honoring a stated policy, acknowledging that something isn't tracked, citing a \
caveat — any nuance the owner wrote. The agent may also have ended its run by \
calling its `ask_human` escalation tool — its recorded answer then opens with \
"[The agent ended the run by asking the user for clarification]" followed by \
its questions. Grade an ask like any answer: an expectation that calls for \
clarification is satisfied by a well-aimed ask (the right missing dimension, \
specific questions), and an expectation the wiki lets the agent answer \
directly is FAILED by an unnecessary ask. Grade THIS run against it:

1. Break the expectation into its individual demands.
2. Read the agent's final answer and its trace (what it opened, what it claimed).
3. VERIFY the load-bearing claims: check the wiki for what the docs actually \
say, and the live data when factual correctness is part of the expectation. An \
answer that sounds right but asserts facts the wiki never states is a \
hallucination — a fail whenever the expectation demands groundedness.

Then rule:
- "pass" — the run satisfies the expectation (all of its demands, judged \
reasonably, not hyper-literally).
- "fail" — the run violates it (hallucinated, ignored a policy, invented data, \
missed a required acknowledgment, materially wrong). `comment` is MANDATORY: \
what the agent did vs what was expected, concretely. When the failure traces \
to the DOCS (missing/unclear/misleading wiki content), ALSO fill `annotation` \
with the dataset-level doc fix an author can apply; leave it empty when the \
agent alone is at fault. NEVER restate the benchmark question or the \
expectation verbatim in the annotation; describe the missing or unclear FACT.

When done investigating, deliver the ruling by calling \
`submit_verdict(verdict, comment, annotation)` — that tool call IS your \
output; plain text is never read as a verdict. Submit once, then finish."""
)


def render_behavior_case(case: BehaviorCase) -> str:
    """Render one behavior attempt as the judge's user message (bounded).

    A single attempt per case, so the whole trace budget goes to it — the
    trace is the evidence (what the agent opened and claimed), and behavior
    rulings hinge on it more than SQL reviews do.
    """
    lines = [
        f"User request: {case.question}",
        f"Expected behavior: {case.expected}",
        f"Run: {case.run_index + 1}",
        "",
        "Agent's final answer:",
        str(getattr(case.attempt, "prediction", "") or "(empty)"),
    ]
    trace = render_for_judge(
        getattr(case.attempt, "trace", None), max_chars=_CASE_TRACE_BUDGET
    )
    if trace:
        lines.append("")
        lines.append(trace)
    return "\n".join(lines)


def make_behavior_grader(
    chat_model: Any,
    tools: list[Any] | Callable[[], list[Any]],
) -> Callable[..., Awaitable[list[BehaviorGrade]]]:
    """Build an async ``grade(cases, on_progress=None) -> list[BehaviorGrade]``.

    Same construction as :func:`make_judge` (judge model, deferred toolset,
    shared concurrency), different contract: one ruling per (question, run)
    attempt, independent per case — grading run 3 never sees run 1 (no
    cross-run anchoring; the failure review is where cross-run diffing
    belongs). An errored/unparseable review is a ``fail`` with ``judge_error``
    set — counts against the wiki, never a crash of the phase.
    """
    built: dict[str, Any] = {}

    def _ensure_built():
        if built:
            return built
        from harvest.benchmark.checks import with_run_date
        from harvest.benchmark.react import SubmitToolNudgeMiddleware, make_react_agent

        resolved = tools() if callable(tools) else tools
        built["agent"] = make_react_agent(
            chat_model,
            [*resolved, _make_submit_verdict_tool()],
            with_run_date(BEHAVIOR_JUDGE_PROMPT),
            extra_middleware=[
                SubmitToolNudgeMiddleware(SUBMIT_VERDICT_TOOL, _VERDICT_NUDGE)
            ],
        )
        return built

    async def _grade_one(agent: Any, case: BehaviorCase) -> BehaviorGrade:
        from harvest.benchmark.solver import fold_usage

        try:
            out = await agent.ainvoke(
                {"messages": [("user", render_behavior_case(case))]}
            )
        except Exception as e:  # noqa: BLE001 - an errored review counts against the wiki
            return BehaviorGrade(
                q_id=case.q_id, check=case.check, run_index=case.run_index,
                verdict=VERDICT_FAIL,
                comment="the behavior review errored; treated as a failure",
                judge_error=f"{type(e).__name__}: {e}",
            )
        messages = out.get("messages", [])
        usage = fold_usage(messages)
        parsed = _parse_ruling(messages, SUBMIT_VERDICT_TOOL)
        if not isinstance(parsed, dict):
            return BehaviorGrade(
                q_id=case.q_id, check=case.check, run_index=case.run_index,
                verdict=VERDICT_FAIL,
                comment="the behavior review returned no parseable verdict; treated as a failure",
                judge_error="unparseable verdict", usage=usage,
            )
        verdict = str(parsed.get("verdict") or "").strip().lower()
        comment = str(parsed.get("comment") or "").strip()
        annotation = str(parsed.get("annotation") or "").strip()
        if verdict not in (VERDICT_PASS, VERDICT_FAIL):
            return BehaviorGrade(
                q_id=case.q_id, check=case.check, run_index=case.run_index,
                verdict=VERDICT_FAIL,
                comment=comment
                or "the behavior review returned an unknown verdict; treated as a failure",
                annotation=annotation,
                judge_error=f"unknown verdict {verdict!r}", usage=usage,
            )
        if verdict == VERDICT_FAIL and not comment:
            comment = "(the judge failed the run but gave no comment)"
        return BehaviorGrade(
            q_id=case.q_id, check=case.check, run_index=case.run_index,
            verdict=verdict, comment=comment, annotation=annotation, usage=usage,
        )

    async def grade(
        cases: list[BehaviorCase],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[BehaviorGrade]:
        if not cases:
            return []
        agent = _ensure_built()["agent"]
        sem = asyncio.Semaphore(_concurrency())
        total = len(cases)
        done = 0

        async def _one(case: BehaviorCase) -> BehaviorGrade:
            nonlocal done
            async with sem:
                grade_ = await _grade_one(agent, case)
            done += 1
            if on_progress:
                on_progress(done, total)
            return grade_

        return await asyncio.gather(*[_one(c) for c in cases])

    return grade


# --------------------------------------------------------------------------- #
# Behavior synthesis review — one question-level summary over the graded runs
# --------------------------------------------------------------------------- #

BEHAVIOR_REVIEW_PROMPT = (
    """\
You are writing the QUESTION-LEVEL summary for one Behavior benchmark case. A \
wiki-consumer agent (which had ONLY the data wiki, not the raw schema or data) \
answered this question once per independent run, and each run was already \
graded pass/fail against the owner's free-form expected behavior. Those \
per-run rulings are FINAL — you cannot change them. Your job is the synthesis \
the independent gradings could not do: read ALL runs together and produce the \
question-level diagnosis and ONE consolidated doc fix. You can see everything, \
via these read-only tools:

"""
    + _JUDGE_TOOLS_BLOCK
    + _JUDGE_TRACES_BLOCK
    + """
Method:
1. Read the expectation, then every run: its answer, its ruling and the \
grader's comment, and its trace.
2. DIFF the runs — what did a passing run read or say that the failing ones \
didn't? Is the failure systemic (every run) or flaky (some runs)? Same root \
cause in every failing run, or several distinct ones?
3. Verify against the wiki and live data where the diagnosis needs it.

When done, deliver the diagnosis by calling \
`submit_review(comment, annotation)` — that tool call IS your output; plain \
text is never read as a review. Submit once, then finish.
- `comment` (MANDATORY): the question-level diagnosis, concretely — name the \
pattern (systemic vs flaky, agent-fault vs docs-fault) and what went wrong.
- `annotation`: when the failures trace to the DOCS, ONE consolidated \
dataset-level doc fix an author can apply — merge the per-run suggestions; \
leave it empty when the agent alone is at fault. NEVER restate the question \
or the expectation verbatim; describe the missing or unclear FACT."""
)


def make_behavior_reviewer(
    chat_model: Any,
    tools: list[Any] | Callable[[], list[Any]],
) -> Callable[..., Awaitable[list[JudgeVerdict]]]:
    """Build an async ``review(cases, on_progress=None) -> list[JudgeVerdict]``.

    Cases are the same :class:`JudgeCase` shape the failure review uses (all N
    attempts, ``gold`` = the expected behavior, attempt ``reason`` = that
    run's grading comment) — one case per behavior pair with ≥1 failing run.
    The returned verdicts are structurally ``fail`` (the pair DID fail; this
    pass never overturns) and exist to carry the question-level ``comment`` +
    consolidated ``annotation``. An errored/unparseable review degrades to a
    verdict with ``judge_error`` set — the outcomes are already settled, so
    nothing is forgiven or lost except the summary itself.
    """
    built: dict[str, Any] = {}

    def _ensure_built():
        if built:
            return built
        from harvest.benchmark.checks import with_run_date
        from harvest.benchmark.react import SubmitToolNudgeMiddleware, make_react_agent

        resolved = tools() if callable(tools) else tools
        built["agent"] = make_react_agent(
            chat_model,
            [*resolved, _make_submit_review_tool()],
            with_run_date(BEHAVIOR_REVIEW_PROMPT),
            extra_middleware=[
                SubmitToolNudgeMiddleware(SUBMIT_REVIEW_TOOL, _REVIEW_NUDGE)
            ],
        )
        return built

    async def _review_one(agent: Any, case: JudgeCase) -> JudgeVerdict:
        from harvest.benchmark.solver import fold_usage

        try:
            out = await agent.ainvoke({"messages": [("user", render_case(case))]})
        except Exception as e:  # noqa: BLE001 - a fallen-over summary is surfaced, not fatal
            return JudgeVerdict(
                q_id=case.q_id, check=case.check, verdict=VERDICT_FAIL,
                comment="the behavior summary review errored — read the per-run comments",
                judge_error=f"{type(e).__name__}: {e}",
            )
        messages = out.get("messages", [])
        usage = fold_usage(messages)
        parsed = _parse_ruling(messages, SUBMIT_REVIEW_TOOL)
        if not isinstance(parsed, dict):
            return JudgeVerdict(
                q_id=case.q_id, check=case.check, verdict=VERDICT_FAIL,
                comment="the behavior summary review returned nothing parseable — read the per-run comments",
                judge_error="unparseable summary", usage=usage,
            )
        comment = str(parsed.get("comment") or "").strip()
        annotation = str(parsed.get("annotation") or "").strip()
        if not comment:
            comment = "(the summary review gave no comment — read the per-run comments)"
        return JudgeVerdict(
            q_id=case.q_id, check=case.check, verdict=VERDICT_FAIL,
            comment=comment, annotation=annotation, usage=usage,
        )

    async def review(
        cases: list[JudgeCase],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[JudgeVerdict]:
        if not cases:
            return []
        agent = _ensure_built()["agent"]
        sem = asyncio.Semaphore(_concurrency())
        total = len(cases)
        done = 0

        async def _one(case: JudgeCase) -> JudgeVerdict:
            nonlocal done
            async with sem:
                verdict = await _review_one(agent, case)
            done += 1
            if on_progress:
                on_progress(done, total)
            return verdict

        return await asyncio.gather(*[_one(c) for c in cases])

    return review


def _last_ai_text(messages: list) -> str:
    """Text of the last AI message (adaptive-thinking blocks stripped)."""
    for msg in reversed(messages):
        if getattr(msg, "type", "") in ("ai", "assistant"):
            text = message_text(msg)
            if text.strip():
                return text
    return ""
