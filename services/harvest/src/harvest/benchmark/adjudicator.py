"""The adjudicator — decides which FAILs are genuine wiki gaps, then de-identifies.

Unlike the solver, the adjudicator is the wiki-gap *diagnostician*, so it gets
FULL live-data access via the source tools (Athena ``run_sql`` — which can also
inspect the schema with ``DESCRIBE``/``SHOW COLUMNS`` — and ``sample_rows``) — it
must see both what the wiki says and what the data actually is to explain a
failure. Granting raw data is safe here because its output is de-identified
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
from harvest.benchmark.tool import AdjudicationResult

_DEFAULT_CONCURRENCY = 10


def _concurrency() -> int:
    """Adjudicator fan-out width — the LLM knob, shared with the solver."""
    try:
        return max(1, int(os.environ.get("OKF_BENCHMARK_MAX_CONCURRENCY", "")))
    except (TypeError, ValueError):
        return _DEFAULT_CONCURRENCY

# Categories mirror the okf-sql-benchmark adjudicator taxonomy.
CATEGORY_GENUINE = "GENUINE_ERROR"
CATEGORY_NOISY_GOLD = "NOISY_GOLD"
CATEGORY_AMBIGUOUS = "AMBIGUOUS"
_GENUINE = {CATEGORY_GENUINE}

CLASSIFY_SYSTEM_PROMPT = """\
You are adjudicating why a text-to-SQL agent (which had ONLY a data wiki, not the \
raw schema) got a question wrong. For each case you see the agent's predicted SQL \
and the divergence. You have FULL live-data access via two tools:
- `run_sql(query)` — run any read-only Athena/Trino SQL against the real dataset. \
Use it to inspect the schema (e.g. `DESCRIBE <table>`, `SHOW COLUMNS FROM <table>`, \
`SELECT DISTINCT <col> ... LIMIT` for code legends) and to check what the data \
actually contains.
- `sample_rows(concept_id)` — a few sample rows for a table concept id like \
`tables/races` (NOT a file path — do not pass `.metadata/...`).
Use these to check what the data actually is before you judge.

Classify the failure into exactly one category:
- GENUINE_ERROR: the wiki is missing or wrong about something the agent needed \
(an undocumented column/join/code-legend/unit/filter). A better wiki would have \
prevented this. This is the only category that should drive a wiki fix.
- NOISY_GOLD: the gold query is itself wrong/odd against the real data, so the \
agent's answer is arguably fine — the wiki is not at fault.
- AMBIGUOUS: the question is under-specified; multiple reasonable SQL answers \
exist and the divergence is not a wiki gap.

For a GENUINE_ERROR, write a `gap` note describing the DOC-LEVEL fix, grounded in \
what you verified in the data — e.g. "docs don't state `status` is an int code \
(1=active); the agent filtered status='active' and got nothing." Do NOT mention \
the specific question or gold — describe the wiki gap itself.

When done investigating, output ONLY a fenced JSON object (nothing after it):
```json
{"category": "GENUINE_ERROR", "gap": "docs don't state ..."}
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
    raw_data_tools: list[Any],
) -> Callable[[list[QuestionResult]], Awaitable[AdjudicationResult]]:
    """Build an async ``adjudicate(fails) -> AdjudicationResult``.

    ``chat_model`` is the shared instrumented model. ``raw_data_tools`` are the
    source tools (``run_sql``/``sample_rows``) the classifier uses to verify claims
    against live data — including schema inspection via ``DESCRIBE``/``SHOW
    COLUMNS`` through ``run_sql`` (it has no filesystem tool, so it does not read
    the ``.metadata/`` snapshot directly).
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

        built["classifier"] = create_react_agent(
            chat_model,
            tools=raw_data_tools,
            prompt=CLASSIFY_SYSTEM_PROMPT,
        )
        return built

    async def _classify_one(classifier: Any, r: QuestionResult) -> tuple[str, str]:
        """Classify one failure → (category, gap). Never raises — a classifier
        error degrades to AMBIGUOUS (treated as not-a-wiki-gap)."""
        case = (
            f"Predicted SQL:\n{r.predicted_sql}\n\n"
            f"Divergence: {r.reason}\n"
            f"predicted rowcount={r.pred_rowcount}, gold rowcount={r.gold_rowcount}\n"
            f"predicted sample (first rows): {r.pred_sample}"
        )
        try:
            out = await classifier.ainvoke({"messages": [("user", case)]})
        except Exception:  # noqa: BLE001 - a stuck classifier is not a crash
            return CATEGORY_AMBIGUOUS, ""
        verdict = extract_json(_last_ai_text(out.get("messages", [])), default={})
        if not isinstance(verdict, dict):
            return CATEGORY_AMBIGUOUS, ""
        category = verdict.get("category") or CATEGORY_AMBIGUOUS
        gap = str(verdict.get("gap") or "").strip()
        return category, gap

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

        async def _one(r: QuestionResult) -> tuple[str, str]:
            nonlocal done
            async with sem:
                verdict = await _classify_one(classifier, r)
            done += 1
            if on_progress:
                on_progress(done, total)
            return verdict

        verdicts = await asyncio.gather(*[_one(r) for r in fails])

        genuine_gaps = [gap for cat, gap in verdicts if cat in _GENUINE and gap]
        noisy = sum(1 for cat, _ in verdicts if cat not in _GENUINE)

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
            genuine_error_count=len(genuine_gaps),
            noisy_or_ambiguous=noisy,
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
