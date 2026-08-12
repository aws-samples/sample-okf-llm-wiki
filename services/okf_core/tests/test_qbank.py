"""okf_core.qbank — config validation, the deterministic allocator, CSV round-trip."""

from __future__ import annotations

import pytest

from okf_core import qbank
from okf_core.benchmark_questions import load_questions


# -- config validation ---------------------------------------------------------


def test_defaults_fill_in():
    cfg = qbank.validate_config({})
    assert cfg[qbank.FIELD_COUNT] == qbank.DEFAULT_COUNT
    assert cfg[qbank.FIELD_CHECKS] == ["sql", "behavior"]
    assert cfg[qbank.FIELD_SQL_SHARE] == qbank.DEFAULT_SQL_SHARE
    assert cfg[qbank.FIELD_DIMENSIONS] == list(qbank.DIMENSION_KEYS)


@pytest.mark.parametrize("count", [19, 101, "many", None])
def test_count_bounds(count):
    with pytest.raises(qbank.QbankConfigError):
        qbank.validate_config({"count": count})


def test_unknown_check_and_dimension_are_named():
    with pytest.raises(qbank.QbankConfigError, match="unknown check"):
        qbank.validate_config({"checks": ["sql", "vibes"]})
    with pytest.raises(qbank.QbankConfigError, match="unknown dimension"):
        qbank.validate_config({"dimensions": ["direct_retrieval", "telepathy"]})


def test_checks_and_dimensions_normalize_to_canonical_order():
    cfg = qbank.validate_config(
        {
            "checks": ["behavior", "sql"],
            "dimensions": ["unanswerable", "comparison", "direct_retrieval"],
        }
    )
    assert cfg[qbank.FIELD_CHECKS] == ["sql", "behavior"]
    assert cfg[qbank.FIELD_DIMENSIONS] == [
        "direct_retrieval",
        "comparison",
        "unanswerable",
    ]


def test_a_check_with_no_capable_dimension_is_a_config_error():
    # counterfactual/meta/unanswerable are behavior-only: an accuracy-only run
    # restricted to them has slots nobody can fill — fail NOW, not half-empty.
    with pytest.raises(qbank.QbankConfigError, match="no selected dimension"):
        qbank.validate_config(
            {"checks": ["sql"], "dimensions": ["counterfactual", "meta_introspection"]}
        )


# -- the allocator ---------------------------------------------------------------


def test_allocator_is_deterministic_and_exact():
    cfg = qbank.validate_config({"count": 41})
    a = qbank.allocate_slots(cfg)
    b = qbank.allocate_slots(cfg)
    assert a == b
    assert len(a) == 41


def test_allocator_respects_the_check_ratio():
    cfg = qbank.validate_config({"count": 40, "sql_share": 0.7})
    slots = qbank.allocate_slots(cfg)
    by_check = {c: sum(1 for s in slots if s["check"] == c) for c in ("sql", "behavior")}
    assert by_check == {"sql": 28, "behavior": 12}


def test_allocator_respects_the_tier_mix_per_check():
    cfg = qbank.validate_config({"count": 100, "checks": ["sql"]})
    slots = qbank.allocate_slots(cfg)
    tiers = {t: sum(1 for s in slots if s["tier"] == t) for t in qbank.TIERS}
    assert tiers == {"easy": 30, "medium": 40, "hard": 30}


def test_allocator_lands_slots_only_on_affine_dimensions():
    cfg = qbank.validate_config({"count": 30})
    for slot in qbank.allocate_slots(cfg):
        assert slot["check"] in qbank.dimension(slot["dimension"]).affinity


def test_allocator_spreads_across_eligible_dimensions():
    cfg = qbank.validate_config(
        {"count": 20, "checks": ["sql"], "dimensions": ["direct_retrieval", "comparison"]}
    )
    slots = qbank.allocate_slots(cfg)
    dims = {d: sum(1 for s in slots if s["dimension"] == d) for d in ("direct_retrieval", "comparison")}
    assert dims == {"direct_retrieval": 10, "comparison": 10}


def test_both_checks_always_get_at_least_one_slot():
    cfg = qbank.validate_config({"count": 20, "sql_share": 0.9})
    slots = qbank.allocate_slots(cfg)
    assert sum(1 for s in slots if s["check"] == "behavior") >= 1
    assert len(slots) == 20


# -- CSV round-trip ----------------------------------------------------------------


_QUESTIONS = [
    {
        "question": "Which team scored the most points in 2024?",
        "check": "sql",
        "gold_sql": 'SELECT name FROM "f1"."teams" ORDER BY points DESC LIMIT 1',
        "expected_behavior": "",
        "tier": "easy",
        "dimension": "direct_retrieval",
    },
    {
        "question": 'How long, in minutes, do pit stops take on "wet, cold" days?',
        "check": "behavior",
        "gold_sql": "",
        "expected_behavior": "Should say pit-stop durations are not tracked — not invent a number.",
        "tier": "medium",
        "dimension": "unanswerable",
    },
]


def test_csv_round_trips_through_the_studio_parser():
    # The load-bearing compatibility claim: the generated CSV IS a valid studio
    # question set today (tier/dimension ride as ignored extra columns).
    loaded = load_questions(qbank.render_csv(_QUESTIONS))
    assert [q.question for q in loaded.questions] == [q["question"] for q in _QUESTIONS]
    assert loaded.questions[0].gold_sql == _QUESTIONS[0]["gold_sql"]
    assert loaded.questions[1].expected_behavior == _QUESTIONS[1]["expected_behavior"]
    assert loaded.check_counts == {"sql": 1, "behavior": 1}
    # Commas and quotes inside cells survived (csv quoting, not string joins).
    assert '"wet, cold"' in loaded.questions[1].question


def test_summarize_counts_every_facet():
    s = qbank.summarize(_QUESTIONS)
    assert s["check"] == {"sql": 1, "behavior": 1}
    assert s["tier"] == {"easy": 1, "medium": 1}
    assert s["dimension"] == {"direct_retrieval": 1, "unanswerable": 1}


# -- identity ------------------------------------------------------------------------


def test_qbank_ids_mint_and_validate():
    qid = qbank.new_qbank_id(now_compact="20260812T101500", token="abcd1234")
    assert qid == "qb20260812t101500-abcd1234"
    assert qbank.is_valid_qbank_id(qid)
    assert not qbank.is_valid_qbank_id("r20260812t101500-abcd1234")  # a report id
    assert not qbank.is_valid_qbank_id("qb/../escape")
    assert qbank.qbank_sk(qid) == f"QBANK#{qid}"
    assert qbank.qbank_key("sales", "f1", qid) == f"benchmark/sales/f1/qbank/{qid}.json"


def test_explicit_empty_lists_error_instead_of_defaulting_to_everything():
    # Absent -> the full default set; an EXPLICIT [] is a request for nothing
    # and must 400 at the trust boundary, not silently run a full-taxonomy
    # generation at real model cost.
    import pytest

    from okf_core.qbank import QbankConfigError, validate_config

    assert validate_config({})["checks"]  # absent defaults survive
    with pytest.raises(QbankConfigError):
        validate_config({"checks": []})
    with pytest.raises(QbankConfigError):
        validate_config({"dimensions": []})
