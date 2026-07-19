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
    # The validated block carries ONLY enablement, key, and iteration budget — the
    # stop target is fixed (JUDGE_TARGET), so there are no threshold/gate fields.
    out = ri.validate({"enabled": True, "questions_key": "okf/d/ds/.benchmark/q.csv"})
    assert out == {
        "enabled": True,
        "questions_key": "okf/d/ds/.benchmark/q.csv",
        "max_iterations": ri.MAX_ITERATIONS,
    }


def test_validate_passes_through_valid_values():
    out = ri.validate(
        {
            "enabled": True,
            "questions_key": " k ",  # trimmed
            "max_iterations": 3,
        }
    )
    assert out["questions_key"] == "k"
    assert out["max_iterations"] == 3


def test_validate_ignores_legacy_threshold_fields():
    # Old configs / callers may still send threshold + gate keys; they are IGNORED,
    # not persisted, and never cause a 400 (the target is fixed now).
    out = ri.validate(
        {
            "enabled": True,
            "questions_key": "k",
            "ex_threshold": 0.7,
            "judge_threshold": 0.5,
            "gate_kpis": ["ex", "judge"],
        }
    )
    assert set(out) == {"enabled", "questions_key", "max_iterations"}
    assert "ex_threshold" not in out
    assert "judge_threshold" not in out
    assert "gate_kpis" not in out


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


# -- target_met (fixed judge >= JUDGE_TARGET, with an EX > 0 floor) -----------


def test_target_is_fixed_at_90_percent():
    assert ri.JUDGE_TARGET == 0.9


def test_target_met_gates_on_judge():
    # Judge (adjudicated) accuracy is the bar; raw EX doesn't gate (beyond the > 0
    # floor) because the feature measures wiki quality, not the raw solver score.
    assert ri.target_met(ex=0.2, judge=0.9) is True   # judge clears, ex > 0
    assert ri.target_met(ex=0.2, judge=0.95) is True
    assert ri.target_met(ex=0.85, judge=0.89) is False  # judge below bar
    # Boundary: exactly the target passes.
    assert ri.target_met(ex=0.5, judge=ri.JUDGE_TARGET) is True


def test_target_met_ex_floor_blocks_zero_ex_false_success():
    # THE FALSE-SUCCESS FIX: EX == 0 can NEVER be "target met", even at judge 1.0.
    # Regression guard for the EX 0% / judge 100% / "Target met" screenshot.
    assert ri.target_met(ex=0.0, judge=1.0) is False
    # A single real pass lifts the floor; then the judge bar applies normally.
    assert ri.target_met(ex=0.02, judge=0.95) is True
    assert ri.target_met(ex=0.02, judge=0.85) is False
