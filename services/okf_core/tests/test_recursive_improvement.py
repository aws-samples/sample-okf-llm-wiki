"""okf_core.recursive_improvement — pure config validation + KPI shape invariants."""

from __future__ import annotations

import pytest

from okf_core import recursive_improvement as ri


# -- is_enabled --------------------------------------------------------------


def test_is_enabled():
    assert ri.is_enabled(None) is False
    assert ri.is_enabled({}) is False
    assert ri.is_enabled({"enabled": False, "questions_key": "k"}) is False
    assert ri.is_enabled({"enabled": True, "questions_key": "k"}) is True


# -- validate: disabled short-circuits to None -------------------------------


def test_validate_disabled_returns_none():
    # Not enabled ⇒ None, so the caller omits the block and runs a normal harvest.
    assert ri.validate(None) is None
    assert ri.validate({}) is None
    assert ri.validate({"enabled": False, "questions_key": "k"}) is None


# -- validate: happy path + defaults -----------------------------------------


def test_validate_fills_defaults():
    out = ri.validate({"enabled": True, "questions_key": "okf/d/ds/.benchmark/q.csv"})
    assert out == {
        "enabled": True,
        "questions_key": "okf/d/ds/.benchmark/q.csv",
        "max_iterations": ri.MAX_ITERATIONS,
        "ex_threshold": ri.DEFAULT_EX_THRESHOLD,
        "judge_threshold": ri.DEFAULT_JUDGE_THRESHOLD,
        "gate_kpis": list(ri.DEFAULT_GATE_KPIS),
    }


def test_validate_passes_through_valid_values():
    out = ri.validate(
        {
            "enabled": True,
            "questions_key": " k ",  # trimmed
            "max_iterations": 3,
            "ex_threshold": 0.7,
            "judge_threshold": 0.95,
            "gate_kpis": ["ex", "judge"],
        }
    )
    assert out["questions_key"] == "k"
    assert out["max_iterations"] == 3
    assert out["ex_threshold"] == 0.7
    assert out["judge_threshold"] == 0.95
    assert out["gate_kpis"] == ["ex", "judge"]


# -- validate: questions_key is required when enabled ------------------------


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_validate_missing_questions_key_raises(bad):
    with pytest.raises(ri.RecursiveImprovementConfigError):
        ri.validate({"enabled": True, "questions_key": bad})


# -- validate: max_iterations clamps (never rejects a benign over-ask) -------


@pytest.mark.parametrize(
    "given,expected",
    [
        (10, ri.MAX_ITERATIONS),  # over-ask clamps down to 5
        (5, 5),
        (2, 2),
        (0, ri.MIN_ITERATIONS),  # under floor clamps up to 2
        (-3, ri.MIN_ITERATIONS),
    ],
)
def test_validate_clamps_max_iterations(given, expected):
    out = ri.validate({"enabled": True, "questions_key": "k", "max_iterations": given})
    assert out["max_iterations"] == expected


def test_validate_non_integer_iterations_raises():
    with pytest.raises(ri.RecursiveImprovementConfigError):
        ri.validate({"enabled": True, "questions_key": "k", "max_iterations": "lots"})


# -- validate: thresholds clamp to [0,1], reject non-numeric -----------------


@pytest.mark.parametrize(
    "given,expected",
    [(1.2, 1.0), (-0.5, 0.0), (0.0, 0.0), (1.0, 1.0), (0.42, 0.42)],
)
def test_validate_clamps_thresholds(given, expected):
    out = ri.validate({"enabled": True, "questions_key": "k", "ex_threshold": given})
    assert out["ex_threshold"] == expected


def test_validate_non_numeric_threshold_raises():
    with pytest.raises(ri.RecursiveImprovementConfigError):
        ri.validate({"enabled": True, "questions_key": "k", "judge_threshold": "high"})


# -- validate: gate_kpis subset check ----------------------------------------


def test_validate_gate_kpis_default_when_omitted():
    out = ri.validate({"enabled": True, "questions_key": "k"})
    assert out["gate_kpis"] == list(ri.DEFAULT_GATE_KPIS)


def test_validate_gate_kpis_dedupes_preserving_order():
    out = ri.validate(
        {"enabled": True, "questions_key": "k", "gate_kpis": ["judge", "ex", "judge"]}
    )
    assert out["gate_kpis"] == ["judge", "ex"]


@pytest.mark.parametrize("bad", [[], "ex", ["ex", "bogus"], ["nope"]])
def test_validate_bad_gate_kpis_raises(bad):
    with pytest.raises(ri.RecursiveImprovementConfigError):
        ri.validate({"enabled": True, "questions_key": "k", "gate_kpis": bad})


# -- bench_sk (KPI row sort key) ---------------------------------------------


def test_bench_sk_iteration_and_final():
    assert ri.bench_sk("okf-sales-orders-abc", 0) == "BENCH#okf-sales-orders-abc#0"
    assert ri.bench_sk("okf-sales-orders-abc", 3) == "BENCH#okf-sales-orders-abc#3"
    assert (
        ri.bench_sk("okf-sales-orders-abc", ri.FINAL_ITERATION)
        == "BENCH#okf-sales-orders-abc#final"
    )


def test_bench_sk_query_prefix_scopes_to_one_session():
    pfx = ri.bench_sk_query_prefix("sess-A")
    assert pfx == "BENCH#sess-A#"
    # A row for this session matches; a row for another session does not.
    assert ri.bench_sk("sess-A", 2).startswith(pfx)
    assert not ri.bench_sk("sess-B", 2).startswith(pfx)


# -- ex_score (discards excluded) --------------------------------------------


def test_ex_score():
    assert ri.ex_score(34, 48) == pytest.approx(34 / 48)
    assert ri.ex_score(0, 0) == 0.0  # all-discarded round grades nothing
    assert ri.ex_score(10, 10) == 1.0
    assert ri.ex_score(0, 5) == 0.0


# -- thresholds_met (only gated KPIs must clear) -----------------------------


def test_thresholds_met_ex_only_gate_ignores_judge():
    cfg = ri.validate(
        {"enabled": True, "questions_key": "k", "ex_threshold": 0.8, "gate_kpis": ["ex"]}
    )
    # judge is terrible but not gated → still passes on EX alone.
    assert ri.thresholds_met(cfg, ex=0.85, judge=0.1) is True
    assert ri.thresholds_met(cfg, ex=0.79, judge=1.0) is False


def test_thresholds_met_both_gated():
    cfg = ri.validate(
        {
            "enabled": True,
            "questions_key": "k",
            "ex_threshold": 0.8,
            "judge_threshold": 0.9,
            "gate_kpis": ["ex", "judge"],
        }
    )
    assert ri.thresholds_met(cfg, ex=0.8, judge=0.9) is True
    assert ri.thresholds_met(cfg, ex=0.8, judge=0.89) is False
    assert ri.thresholds_met(cfg, ex=0.79, judge=0.9) is False


def test_thresholds_met_empty_gate_never_satisfied():
    # Defensive: an empty gate can't be cleared (validate prevents this, but the
    # function must not vacuously return True).
    assert ri.thresholds_met({"gate_kpis": []}, ex=1.0, judge=1.0) is False
