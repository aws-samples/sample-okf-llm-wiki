"""The adjudicator — decides which FAILs are genuine wiki gaps, then de-identifies.

Unlike the solver, the adjudicator is the wiki-gap *diagnostician*, so it gets
FULL raw-data access (Athena ``run_sql``/``sample_rows`` + the ``.metadata/``
schema snapshot) — it must see both what the wiki says and what the data actually
is to explain a failure. Granting raw data is safe here because its output is
de-identified themes, never SQL the score depends on.

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

from typing import Any, Awaitable, Callable

from harvest.benchmark.grader import QuestionResult
from harvest.benchmark.tool import AdjudicationResult

# Categories mirror the okf-sql-benchmark adjudicator taxonomy.
CATEGORY_GENUINE = "GENUINE_ERROR"
CATEGORY_NOISY_GOLD = "NOISY_GOLD"
CATEGORY_AMBIGUOUS = "AMBIGUOUS"
_GENUINE = {CATEGORY_GENUINE}

CLASSIFY_SYSTEM_PROMPT = """\
You are adjudicating why a text-to-SQL agent (which had ONLY a data wiki, not the \
raw schema) got a question wrong. For each case you see the question, the agent's \
predicted SQL, and the divergence. You have FULL raw-data access: `run_sql`, \
`sample_rows`, and the `.metadata/` Glue schema snapshot — use them to check what \
the data actually is.

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
the specific question or gold — describe the wiki gap itself."""

CONSOLIDATE_SYSTEM_PROMPT = """\
You are consolidating a list of verified wiki gaps into a short, de-identified \
improvement list for the wiki author. Group gaps that share a root cause into ONE \
item (e.g. several questions tripping on the same undocumented join → one item). \
Each item names the concrete doc-level fix, phrased as guidance to the author. Do \
NOT reference specific benchmark questions, gold queries, or counts — only what \
the wiki should say. Return the deduped, grouped list of improvement strings."""


def _classification_model():
    from pydantic import BaseModel, Field

    class Classification(BaseModel):
        category: str = Field(
            description="one of GENUINE_ERROR, NOISY_GOLD, AMBIGUOUS"
        )
        gap: str = Field(
            default="",
            description="for GENUINE_ERROR only: the doc-level wiki gap, "
            "grounded in verified data, with no reference to the question/gold",
        )

    return Classification


def _consolidation_model():
    from pydantic import BaseModel, Field

    class Consolidation(BaseModel):
        improvements: list[str] = Field(
            default_factory=list,
            description="deduped, grouped, anonymous doc-level improvement themes",
        )

    return Consolidation


def make_adjudicator(
    chat_model: Any,
    raw_data_tools: list[Any],
) -> Callable[[list[QuestionResult]], Awaitable[AdjudicationResult]]:
    """Build an async ``adjudicate(fails) -> AdjudicationResult``.

    ``chat_model`` is the shared instrumented model. ``raw_data_tools`` are the
    source tools (``run_sql``/``sample_rows``) the classifier uses to verify claims
    against live data; ``.metadata/`` is read via the harvest agent's own file
    tools when present (the adjudicator runs against the real mount, not the
    snapshot).
    """
    # Built lazily on first use so session construction stays framework-light
    # (mirrors the solver): create_react_agent needs a real model, which we only
    # have at run time, not necessarily at wiring time.
    built: dict[str, Any] = {}

    def _ensure_built():
        if built:
            return built
        from langgraph.prebuilt import create_react_agent

        built["classifier"] = create_react_agent(
            chat_model,
            tools=raw_data_tools,
            prompt=CLASSIFY_SYSTEM_PROMPT,
            response_format=_classification_model(),
        )
        built["consolidator"] = chat_model.with_structured_output(
            _consolidation_model()
        )
        return built

    async def adjudicate(fails: list[QuestionResult]) -> AdjudicationResult:
        if not fails:
            return AdjudicationResult()

        b = _ensure_built()
        classifier = b["classifier"]
        consolidator = b["consolidator"]
        genuine_gaps: list[str] = []
        noisy = 0
        for r in fails:
            case = (
                f"Predicted SQL:\n{r.predicted_sql}\n\n"
                f"Divergence: {r.reason}\n"
                f"predicted rowcount={r.pred_rowcount}, gold rowcount={r.gold_rowcount}\n"
                f"predicted sample (first rows): {r.pred_sample}"
            )
            out = await classifier.ainvoke({"messages": [("user", case)]})
            verdict = out.get("structured_response")
            category = getattr(verdict, "category", CATEGORY_AMBIGUOUS)
            if category in _GENUINE:
                gap = (getattr(verdict, "gap", "") or "").strip()
                if gap:
                    genuine_gaps.append(gap)
            else:
                noisy += 1

        improvements: list[str] = []
        if genuine_gaps:
            folded = await consolidator.ainvoke(
                [
                    ("system", CONSOLIDATE_SYSTEM_PROMPT),
                    ("user", "Verified wiki gaps:\n- " + "\n- ".join(genuine_gaps)),
                ]
            )
            improvements = [
                s.strip() for s in getattr(folded, "improvements", []) if s.strip()
            ]

        return AdjudicationResult(
            improvements=improvements,
            genuine_error_count=len(genuine_gaps),
            noisy_or_ambiguous=noisy,
        )

    return adjudicate
