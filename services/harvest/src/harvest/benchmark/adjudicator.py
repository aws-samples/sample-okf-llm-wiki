"""The adjudicator — decides which FAILs are genuine wiki gaps, then de-identifies.

Unlike the solver (which is deliberately bundle-blind — wiki markdown only), the
adjudicator is the wiki-gap *diagnostician*, so it sees EVERYTHING the solver
couldn't, on the REAL dataset mount:

* read-only file tools (``read_file``/``glob``/``grep``/``ls``) over the live mount
  — reaching the authored WIKI docs (what the solver had), the ``.metadata/`` Glue
  schema snapshot, and the ``.context/`` uploaded source docs;
* live-data tools — ``run_sql`` (also ``DESCRIBE``/``SHOW COLUMNS`` for schema) and
  ``sample_rows``;
* ``run_code`` — the code-interpreter sandbox, to extract text from BINARY
  ``.context/`` files (PDF/DOCX/…).

It gets no ``glue`` catalog tool and no write tools (read-only — it never mutates
the bundle). Seeing both what the wiki *says* AND what the data/schema/source docs
*actually are* is what lets it point at a REAL gap ("``tables/results.md`` doesn't
state ``status`` is an int code, but the data shows 1=active…") instead of merely
*inferring* one from the solver's mistake — the failure mode when it could only see
the solver's SQL. Granting raw data is safe because its output is de-identified
themes, never SQL the score depends on.

Two stages, both on the shared instrumented model (tokens meter for free):

1. **Classify** each FAIL: genuine wiki gap vs noisy/broken gold vs ambiguous.
   Only genuine gaps count toward ``genuine_error_count`` (which lowers judge
   accuracy) and feed the improvement themes.
2. **Consolidate** the genuine notes into a short, anonymous ``improvements`` theme
   list — the ONLY free text that crosses the black-box boundary to the
   supervisor. Grouping means ten questions failing on one undocumented join
   yield ONE theme, both better feedback and stronger de-identification.

Deferred agent-framework imports keep the module importable in the test venv; the
callable returned matches the ``adjudicate`` contract the round orchestrator wants.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

from harvest.benchmark.extract import extract_json, message_text
from harvest.benchmark.grader import QuestionResult
from harvest.benchmark.tool import (
    CATEGORY_AMBIGUOUS,
    CATEGORY_GENUINE,
    CATEGORY_NOISY_GOLD,
    CATEGORY_UNKNOWN,
    AdjudicationResult,
    Verdict,
)

_DEFAULT_CONCURRENCY = 10


def _concurrency() -> int:
    """Adjudicator fan-out width — the LLM knob, shared with the solver."""
    try:
        return max(1, int(os.environ.get("OKF_BENCHMARK_MAX_CONCURRENCY", "")))
    except (TypeError, ValueError):
        return _DEFAULT_CONCURRENCY

# Category constants are canonical in tool.py (imported above) so tool.py and this
# module agree without a circular import.
_GENUINE = {CATEGORY_GENUINE}
# The categories that positively mean "the wiki is not at fault" (forgiven in
# judge accuracy). UNKNOWN is deliberately absent — an errored review is not
# evidence the wiki is fine.
_FORGIVEN = {CATEGORY_NOISY_GOLD, CATEGORY_AMBIGUOUS}

CLASSIFY_SYSTEM_PROMPT = """\
You are adjudicating why a text-to-SQL agent (which had ONLY the data wiki — the \
authored markdown docs, not the raw schema) got a question wrong. For each case \
you see the agent's predicted SQL and the divergence. Unlike that agent, you can \
see EVERYTHING, on the real dataset, via these read-only tools:

WIKI + source files (the live mount):
- `read_file(path)` — read a file by path. The WIKI docs the agent had are under \
`tables/`, `references/`, `datasets/`, `index.md`. The Glue SCHEMA snapshot is \
under `.metadata/` (e.g. `.metadata/tables/<name>.md`, `.metadata/columns.tsv`). \
The uploaded SOURCE docs are under `.context/`.
- `glob(pattern)` / `grep(pattern)` / `ls(path)` — find and search across those \
files (e.g. `grep` the wiki for a column name to see whether/how it's documented).
Live DATA:
- `run_sql(query)` — read-only Athena/Trino SQL against the real dataset; also \
`DESCRIBE <table>`, `SHOW COLUMNS FROM <table>`, `SELECT DISTINCT <col> ... LIMIT` \
for code legends.
- `sample_rows(concept_id)` — a few sample rows for a table concept id like \
`tables/races` (a concept id, NOT a file path).
- `run_code(code)` — a sandbox to extract text from BINARY `.context/` files \
(PDF/DOCX/PPTX/XLSX), already present under `/tmp/okf_context/`.

Your method for each failure:
1. From the predicted SQL, identify what the agent got wrong (a column, join key, \
code legend, unit, filter, grain).
2. Check the DATA (`run_sql`/`sample_rows`) to learn what is actually true.
3. Check the WIKI (`read_file`/`grep` under `tables/`/`references/`) to see \
whether the docs the agent had actually state that truth — clearly and findably. \
Consult `.metadata/`/`.context/` if you need to confirm ground truth.

Classify the failure into exactly one category:
- GENUINE_ERROR: you CONFIRMED, by reading the wiki, that it is missing or wrong \
about something the agent needed, AND the data confirms the correct fact. A better \
wiki would have prevented this. Only assign this when the wiki genuinely lacks (or \
misstates) the fact — NOT when the fact is already documented and the agent simply \
missed it. This is the only category that drives a wiki fix.
- NOISY_GOLD: the gold query is itself wrong/odd against the real data, so the \
agent's answer is arguably fine — the wiki is not at fault.
- AMBIGUOUS: the question is under-specified; multiple reasonable SQL answers \
exist and the divergence is not a wiki gap. Also use this when the fact WAS already \
correctly documented in the wiki (so it is not a wiki gap to fix).

For a GENUINE_ERROR, write a `gap` note describing the DOC-LEVEL fix, grounded in \
BOTH what the data shows AND what you found missing in the wiki — e.g. \
"`tables/results.md` doesn't state `status` is an int code (data shows 1=active); \
the agent filtered status='active' and got nothing." Do NOT mention the specific \
question or gold — describe the wiki gap itself.

When done investigating, output ONLY a fenced JSON object (nothing after it):
```json
{"category": "GENUINE_ERROR", "gap": "tables/... doesn't state ..."}
```
`gap` may be "" for NOISY_GOLD / AMBIGUOUS."""

CONSOLIDATE_SYSTEM_PROMPT = """\
You are consolidating a list of verified wiki gaps into a short, de-identified \
improvement list for the wiki author. Group gaps that share a root cause into ONE \
item (e.g. several questions tripping on the same undocumented join → one item). \
Each item names the concrete doc-level fix, phrased as guidance to the author. Do \
NOT reference specific benchmark questions, gold queries, or counts — only what \
the wiki should say.

Output ONLY a fenced JSON object (nothing after it):
```json
{"improvements": ["document that ...", "state that ..."]}
```"""


def make_adjudicator(
    chat_model: Any,
    tools: list[Any] | Callable[[], list[Any]],
) -> Callable[[list[QuestionResult]], Awaitable[AdjudicationResult]]:
    """Build an async ``adjudicate(fails) -> AdjudicationResult``.

    ``chat_model`` is the shared instrumented model. ``tools`` is the classifier's
    FULL read-only toolset over the REAL dataset mount — the read-only file tools
    (``read_file``/``glob``/``grep``/``ls``, reaching the authored wiki plus the
    ``.metadata/`` schema snapshot and ``.context/`` source docs), the live-data
    tools (``run_sql``/``sample_rows``), and ``run_code`` (binary ``.context/``
    extraction). This is what lets the classifier CONFIRM a wiki gap by reading the
    docs the solver had, rather than merely inferring one from the solver's SQL. No
    Glue catalog tool and no write tools are passed (read-only diagnostician).

    ``tools`` may be a plain list OR a zero-arg factory returning one. A factory is
    used so building the file tools (which imports ``deepagents.backends``) is
    DEFERRED to first adjudication — session construction stays framework-light and
    importable in the offline test venv, mirroring the solver's per-round backend.
    """
    # Built lazily on first use so session construction stays framework-light
    # (mirrors the solver): create_react_agent needs a real model, which we only
    # have at run time. No response_format — the classifier emits a fenced JSON
    # verdict we parse (structured-output prefill is rejected under adaptive
    # thinking); the consolidator is a plain model call for the same reason.
    built: dict[str, Any] = {}

    def _ensure_built():
        if built:
            return built
        from langgraph.prebuilt import create_react_agent

        resolved_tools = tools() if callable(tools) else tools
        built["classifier"] = create_react_agent(
            chat_model,
            tools=resolved_tools,
            prompt=CLASSIFY_SYSTEM_PROMPT,
        )
        return built

    async def _classify_one(classifier: Any, r: QuestionResult) -> Verdict:
        """Classify one failure → a Verdict (q_id, category, note). Never raises —
        an error or an unparseable reply is CATEGORY_UNKNOWN, NOT a real verdict, so
        it is never forgiven in judge accuracy (an errored review is not evidence the
        wiki is fine — that was the 0%-EX / 100%-judge false-success bug)."""
        case = (
            f"Predicted SQL:\n{r.predicted_sql}\n\n"
            f"Divergence: {r.reason}\n"
            f"predicted rowcount={r.pred_rowcount}, gold rowcount={r.gold_rowcount}\n"
            f"predicted sample (first rows): {r.pred_sample}"
        )
        try:
            out = await classifier.ainvoke({"messages": [("user", case)]})
        except Exception:  # noqa: BLE001 - a stuck classifier is UNKNOWN, not a crash
            return Verdict(q_id=r.q_id, category=CATEGORY_UNKNOWN)
        verdict = extract_json(_last_ai_text(out.get("messages", [])), default=None)
        if not isinstance(verdict, dict) or not verdict.get("category"):
            return Verdict(q_id=r.q_id, category=CATEGORY_UNKNOWN)
        category = verdict.get("category")
        # An unrecognized category string is also UNKNOWN (don't silently forgive).
        if category not in _GENUINE and category not in _FORGIVEN:
            return Verdict(q_id=r.q_id, category=CATEGORY_UNKNOWN)
        gap = str(verdict.get("gap") or "").strip()
        return Verdict(q_id=r.q_id, category=category, note=gap)

    async def adjudicate(
        fails: list[QuestionResult],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> AdjudicationResult:
        if not fails:
            return AdjudicationResult()

        classifier = _ensure_built()["classifier"]
        # Classify the failures CONCURRENTLY (bounded by the LLM concurrency knob) —
        # each is an independent ReAct call, so a sequential loop over 48 failures
        # is needlessly slow. Tick on_progress as each completes so the UI advances.
        sem = asyncio.Semaphore(_concurrency())
        total = len(fails)
        done = 0

        async def _one(r: QuestionResult) -> Verdict:
            nonlocal done
            async with sem:
                verdict = await _classify_one(classifier, r)
            done += 1
            if on_progress:
                on_progress(done, total)
            return verdict

        verdicts = await asyncio.gather(*[_one(r) for r in fails])

        # Three disjoint buckets. genuine = positively a wiki gap; forgiven =
        # positively noisy/ambiguous gold; UNKNOWN (errored/unparseable) falls into
        # neither — it is NOT forgiven, so it counts against the wiki in judge accuracy.
        genuine_count = sum(1 for v in verdicts if v.category in _GENUINE)
        forgiven_count = sum(1 for v in verdicts if v.category in _FORGIVEN)
        genuine_gaps = [v.note for v in verdicts if v.category in _GENUINE and v.note]

        improvements: list[str] = []
        if genuine_gaps:
            folded = await chat_model.ainvoke(
                [
                    ("system", CONSOLIDATE_SYSTEM_PROMPT),
                    ("user", "Verified wiki gaps:\n- " + "\n- ".join(genuine_gaps)),
                ]
            )
            parsed = extract_json(folded, default={})
            raw = parsed.get("improvements") if isinstance(parsed, dict) else None
            improvements = [str(s).strip() for s in (raw or []) if str(s).strip()]

        return AdjudicationResult(
            improvements=improvements,
            genuine_error_count=genuine_count,
            forgiven_count=forgiven_count,
            verdicts=list(verdicts),
        )

    return adjudicate


def _last_ai_text(messages: list) -> str:
    """Text of the last AI message (adaptive-thinking blocks stripped)."""
    for msg in reversed(messages):
        if getattr(msg, "type", "") in ("ai", "assistant"):
            text = message_text(msg)
            if text.strip():
                return text
    return ""
