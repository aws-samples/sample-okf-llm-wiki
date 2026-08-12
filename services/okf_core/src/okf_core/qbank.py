"""Synthetic question-bank generation — the pure invariants shared across services.

A *question bank generation* is a standalone, human-triggered run on the
harvest runtime (``mode="generate_questions"`` — same posture as a benchmark:
no harvest, no lease, nothing written to the bundle) that authors a Benchmark
Studio question set from the dataset's GROUND TRUTH — the ``.metadata/``
catalog snapshot and the ``.context/`` uploaded source docs — never from the
authored wiki. That boundary is what keeps the resulting benchmark honest:
questions written from the wiki would be phrased in the wiki's own vocabulary
and test only what the wiki already says; questions written from the source
truth test whether the wiki *captured* it.

This module owns only the **pure invariants** — no AWS, no agent deps — so the
Control API (validator + qbank routes), the generation runtime (executor), and
the UI agree on exactly one shape:

- the dimension taxonomy (with per-dimension check affinities) and the
  complexity tiers;
- the run-config field names, bounds, and validator;
- the **deterministic slot allocator** (count × check ratio × tier mix ×
  dimensions → an explicit worklist) — the LLM's creativity goes into the
  questions, never the arithmetic;
- the ``QBANK#`` DynamoDB sort-key builders and the S3 artifact key (off-mount,
  gold-carrying: ``benchmark/<domain>/<dataset>/qbank/<id>.json``);
- the CSV rendering, guaranteed to round-trip through
  :func:`okf_core.benchmark_questions.load_questions` (the extra ``tier``/
  ``dimension`` columns are ignored by today's parser by documented contract).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any

from okf_core.benchmark_questions import (
    ALL_CHECKS,
    CHECK_BEHAVIOR,
    CHECK_SQL,
    MAX_QUESTIONS,
)

# -- dimensions -----------------------------------------------------------------


@dataclass(frozen=True)
class Dimension:
    """One question dimension: what capability the questions probe.

    ``affinity`` names the checks this dimension can author for — ``sql``
    questions need a deterministic gold result set, so dimensions whose point
    is judged reasoning (counterfactuals, honesty about missing data) are
    behavior-only. The allocator only lands a slot on a dimension whose
    affinity includes the slot's check.
    """

    key: str
    title: str
    affinity: tuple[str, ...]  # subset of ALL_CHECKS
    brief: str  # one-line prompt guidance for the author agent


#: The taxonomy, in canonical (allocation) order. Grounded in what the ground
#: truth can actually verify: profiles (values/nulls), relationship evidence
#: (joins/grains), the catalog (schema), and the uploaded context docs
#: (business vocabulary and formulas).
DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        "direct_retrieval",
        "Direct Retrieval",
        (CHECK_SQL,),
        "A single fact from one table — a lookup, a count, a max/min.",
    ),
    Dimension(
        "aggregation",
        "Aggregation & Decomposition",
        (CHECK_SQL,),
        "Grouped aggregates and their breakdowns — totals by category, "
        "shares, per-group extremes.",
    ),
    Dimension(
        "nl_disambiguation",
        "NL Resolution & Disambiguation",
        (CHECK_SQL, CHECK_BEHAVIOR),
        "Business phrasing that must be resolved to the right column/value "
        "(synonyms, coded values), or that is genuinely ambiguous — where the "
        "right behavior is to ask which reading is meant.",
    ),
    Dimension(
        "comparison",
        "Comparison",
        (CHECK_SQL,),
        "Compare two entities, groups, or periods on a measure.",
    ),
    Dimension(
        "derived_kpi",
        "Derived KPI Computation",
        (CHECK_SQL,),
        "A ratio/rate/derived measure — ideally one the context docs define "
        "in business terms.",
    ),
    Dimension(
        "multi_step",
        "Conditional & Multi-Step Reasoning",
        (CHECK_SQL,),
        "Answers requiring chained conditions or intermediate results "
        "(filter → aggregate → filter again, top-k of a derived set).",
    ),
    Dimension(
        "anomaly_detection",
        "Anomaly & Pattern Detection",
        (CHECK_SQL, CHECK_BEHAVIOR),
        "Outliers, gaps, and irregularities the profiles reveal — duplicate "
        "keys, spikes, suspicious sentinel values.",
    ),
    Dimension(
        "counterfactual",
        "Counterfactual & Projection",
        (CHECK_BEHAVIOR,),
        "What-if and extrapolation questions — the right behavior states the "
        "assumptions and what the data can/cannot support, never a bare "
        "invented number.",
    ),
    Dimension(
        "meta_introspection",
        "Meta / Introspection",
        (CHECK_BEHAVIOR,),
        "Questions about the dataset itself — what is tracked, at what grain, "
        "how fresh, what a table represents.",
    ),
    Dimension(
        "join_trap",
        "Grain & Join-Trap Safety",
        (CHECK_SQL, CHECK_BEHAVIOR),
        "Questions whose naive join double-counts or drops rows — grounded in "
        "the relationship evidence (fan-out cardinality, orphan rates). The "
        "gold uses the CORRECT formulation.",
    ),
    Dimension(
        "null_semantics",
        "Null & Sentinel Semantics",
        (CHECK_SQL, CHECK_BEHAVIOR),
        "Questions where NULLs or sentinel values change the answer — the "
        "profiles show the real null shares and suspicious values.",
    ),
    Dimension(
        "unanswerable",
        "Unanswerable / Out-of-Scope Honesty",
        (CHECK_BEHAVIOR,),
        "Plausible questions the data provably cannot answer — the right "
        "behavior says 'not tracked' instead of inventing a number. Verify "
        "the absence against the schema first.",
    ),
)

DIMENSION_KEYS: tuple[str, ...] = tuple(d.key for d in DIMENSIONS)
_DIMENSIONS_BY_KEY: dict[str, Dimension] = {d.key: d for d in DIMENSIONS}


def dimension(key: str) -> Dimension:
    return _DIMENSIONS_BY_KEY[key]


# -- tiers ------------------------------------------------------------------------

TIER_EASY = "easy"
TIER_MEDIUM = "medium"
TIER_HARD = "hard"
TIERS: tuple[str, ...] = (TIER_EASY, TIER_MEDIUM, TIER_HARD)

#: Default complexity mix. Deliberately not user-tunable in v1 — one more dial
#: than the feature needs; the CSV is editable after download.
TIER_MIX: dict[str, float] = {TIER_EASY: 0.3, TIER_MEDIUM: 0.4, TIER_HARD: 0.3}

#: What each tier means, mechanically — shared by the author prompt and the
#: docs so "Medium" is one thing everywhere.
TIER_BRIEFS: dict[str, str] = {
    TIER_EASY: "one table, direct lookup/filter/count — no joins",
    TIER_MEDIUM: "one join OR a grouped aggregation with non-trivial filters",
    TIER_HARD: (
        "multiple joins, nested/multi-step logic, disambiguation, or a "
        "known trap (fan-out, sentinel values)"
    ),
}

# -- run config -------------------------------------------------------------------

FIELD_COUNT = "count"
FIELD_CHECKS = "checks"
#: Share of the count that becomes Accuracy (SQL) questions when BOTH checks
#: are enabled; ignored otherwise.
FIELD_SQL_SHARE = "sql_share"
FIELD_DIMENSIONS = "dimensions"
FIELD_MODEL = "model"
FIELD_EFFORT = "effort"

MIN_COUNT = 20
#: The parser's hard cap — generating more than a bank can hold would silently
#: truncate at apply time.
MAX_COUNT = MAX_QUESTIONS
DEFAULT_COUNT = 40
DEFAULT_SQL_SHARE = 0.7
_SQL_SHARE_MIN, _SQL_SHARE_MAX = 0.1, 0.9


class QbankConfigError(ValueError):
    """A generation config is malformed (→ 400 at the Control API)."""


def validate_config(body: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize + validate a generation request → the canonical config dict.

    The SAME validator runs at the Control API (trust boundary, 400s) and in
    the runtime (defense in depth on the payload). The returned dict carries
    exactly the fields the allocator needs; model/effort ride separately (they
    are catalog-validated by the route adapter, like every other run config).
    """
    body = body or {}

    count = body.get(FIELD_COUNT, DEFAULT_COUNT)
    try:
        count = int(count)
    except (TypeError, ValueError) as e:
        raise QbankConfigError(f"{FIELD_COUNT} must be an integer, got {count!r}") from e
    if not MIN_COUNT <= count <= MAX_COUNT:
        raise QbankConfigError(
            f"{FIELD_COUNT} must be between {MIN_COUNT} and {MAX_COUNT}"
        )

    # ABSENT (or null) defaults to everything; an EXPLICIT empty list is a
    # request for nothing and must error — `or` would silently substitute the
    # full set, running a full-taxonomy generation the caller asked NOT to.
    checks = body.get(FIELD_CHECKS)
    if checks is None:
        checks = list(ALL_CHECKS)
    if not isinstance(checks, (list, tuple)) or not checks:
        raise QbankConfigError(
            f"{FIELD_CHECKS} must be a non-empty list drawn from {list(ALL_CHECKS)}"
        )
    requested = {str(c).strip().lower() for c in checks}
    unknown = requested - set(ALL_CHECKS)
    if unknown:
        raise QbankConfigError(
            f"unknown check(s) {sorted(unknown)}; offered: {list(ALL_CHECKS)}"
        )
    checks = [c for c in ALL_CHECKS if c in requested]

    sql_share = body.get(FIELD_SQL_SHARE, DEFAULT_SQL_SHARE)
    try:
        sql_share = float(sql_share)
    except (TypeError, ValueError) as e:
        raise QbankConfigError(
            f"{FIELD_SQL_SHARE} must be a number, got {sql_share!r}"
        ) from e
    if not _SQL_SHARE_MIN <= sql_share <= _SQL_SHARE_MAX:
        raise QbankConfigError(
            f"{FIELD_SQL_SHARE} must be between {_SQL_SHARE_MIN} and {_SQL_SHARE_MAX}"
        )

    # Same absent-vs-empty distinction as checks above.
    dims = body.get(FIELD_DIMENSIONS)
    if dims is None:
        dims = list(DIMENSION_KEYS)
    if not isinstance(dims, (list, tuple)) or not dims:
        raise QbankConfigError(
            f"{FIELD_DIMENSIONS} must be a non-empty list drawn from "
            f"{list(DIMENSION_KEYS)}"
        )
    requested_dims = [str(d).strip().lower() for d in dims]
    unknown_dims = sorted(set(requested_dims) - set(DIMENSION_KEYS))
    if unknown_dims:
        raise QbankConfigError(
            f"unknown dimension(s) {unknown_dims}; offered: {list(DIMENSION_KEYS)}"
        )
    dims = [k for k in DIMENSION_KEYS if k in set(requested_dims)]

    # Every enabled check must have at least one dimension able to author for
    # it — otherwise the allocator has slots nobody can fill, and that is a
    # config mistake the user should hear about NOW, not a half-empty bank.
    for check in checks:
        if not [k for k in dims if check in dimension(k).affinity]:
            raise QbankConfigError(
                f"no selected dimension can author `{check}` questions — "
                f"select at least one of: "
                + ", ".join(k for k in DIMENSION_KEYS if check in dimension(k).affinity)
            )

    return {
        FIELD_COUNT: count,
        FIELD_CHECKS: checks,
        FIELD_SQL_SHARE: sql_share,
        FIELD_DIMENSIONS: dims,
    }


# -- the deterministic slot allocator ---------------------------------------------


def _largest_remainder(total: int, weights: dict[str, float], order: tuple[str, ...]) -> dict[str, int]:
    """Split ``total`` across ``order`` proportional to ``weights`` — exact sum,
    deterministic ties (earlier key wins the leftover units)."""
    shares = {k: total * weights.get(k, 0.0) for k in order}
    counts = {k: int(shares[k]) for k in order}
    leftover = total - sum(counts.values())
    by_remainder = sorted(order, key=lambda k: (-(shares[k] - counts[k]), order.index(k)))
    for k in by_remainder[:leftover]:
        counts[k] += 1
    return counts


def split_checks(count: int, checks: list[str], sql_share: float) -> dict[str, int]:
    """Per-check question counts. Both checks enabled → the ratio splits the
    total, clamped so each gets at least one."""
    if len(checks) == 1:
        return {checks[0]: count}
    sql_n = max(1, min(count - 1, round(count * sql_share)))
    return {CHECK_SQL: sql_n, CHECK_BEHAVIOR: count - sql_n}


def allocate_slots(config: dict[str, Any]) -> list[dict[str, str]]:
    """The explicit worklist: ``[{"dimension", "tier", "check"}, ...]``.

    Pure and deterministic — the same config always yields the same slots, so
    the runtime recomputes them from the payload's config instead of shipping
    them. Per check: eligible dimensions rotate round-robin (canonical order)
    while tiers fill by largest-remainder over the check's count, so every
    dimension sees a spread of tiers.
    """
    per_check = split_checks(
        config[FIELD_COUNT], list(config[FIELD_CHECKS]), config[FIELD_SQL_SHARE]
    )
    slots: list[dict[str, str]] = []
    for check in config[FIELD_CHECKS]:
        n = per_check.get(check, 0)
        eligible = [k for k in config[FIELD_DIMENSIONS] if check in dimension(k).affinity]
        if not eligible or n <= 0:
            continue
        tier_counts = _largest_remainder(n, TIER_MIX, TIERS)
        tier_seq: list[str] = []
        for tier in TIERS:
            tier_seq.extend([tier] * tier_counts[tier])
        for i in range(n):
            slots.append(
                {
                    "dimension": eligible[i % len(eligible)],
                    "tier": tier_seq[i],
                    "check": check,
                }
            )
    return slots


# -- generated-question records + CSV ----------------------------------------------

#: The CSV header. The first three columns are the studio's documented contract
#: (question + one gold column per check); ``tier``/``dimension`` are the
#: generator's extra metadata, which load_questions ignores by documented
#: contract ("Unrecognized columns are ignored") — so an applied bank works
#: with today's parser unchanged, and a future parser can start carrying them
#: into reports for per-dimension score slicing.
CSV_HEADER: tuple[str, ...] = (
    "question",
    "gold_sql",
    "expected_behavior",
    "tier",
    "dimension",
)


def render_csv(questions: list[dict[str, Any]]) -> str:
    """The canonical CSV rendering of a generated bank (one implementation —
    the download and the apply must ship identical bytes)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for q in questions:
        writer.writerow(
            [
                str(q.get("question") or ""),
                str(q.get("gold_sql") or ""),
                str(q.get("expected_behavior") or ""),
                str(q.get("tier") or ""),
                str(q.get("dimension") or ""),
            ]
        )
    return buf.getvalue()


def summarize(questions: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Counts by check/tier/dimension — the row KPIs and the UI's chips."""
    out: dict[str, dict[str, int]] = {"check": {}, "tier": {}, "dimension": {}}
    for q in questions:
        for facet in out:
            key = str(q.get(facet) or "")
            if key:
                out[facet][key] = out[facet].get(key, 0) + 1
    return out


# -- identity, row, artifact --------------------------------------------------------

#: Qbank ids share the report-id charset (S3 path segment, DDB sort key, URL
#: segment) with a ``qb`` prefix so a QBANK# row can never be mistaken for a
#: REPORT# row in logs.
_QBANK_ID_RE = re.compile(r"^qb[a-z0-9][a-z0-9-]{7,61}$")


def is_valid_qbank_id(qbank_id: Any) -> bool:
    return isinstance(qbank_id, str) and bool(_QBANK_ID_RE.fullmatch(qbank_id))


def new_qbank_id(*, now_compact: str, token: str) -> str:
    """``qb<UTC compact timestamp>-<token>`` — time-prefixed, so QBANK# rows
    sort chronologically in the sk range."""
    qid = f"qb{now_compact}-{token}".lower()
    if not is_valid_qbank_id(qid):
        raise QbankConfigError(f"generated qbank id {qid!r} is invalid")
    return qid


QBANK_SK_PREFIX = "QBANK#"


def qbank_sk(qbank_id: str) -> str:
    return f"{QBANK_SK_PREFIX}{qbank_id}"


def qbank_sk_query_prefix() -> str:
    return QBANK_SK_PREFIX


def qbank_key(data_domain: str, dataset: str, qbank_id: str) -> str:
    """The generated bank's artifact document (off-mount — it carries gold, so
    no LLM role may read it; served only via the Cognito-authed Control API)."""
    return f"benchmark/{data_domain}/{dataset}/qbank/{qbank_id}.json"
