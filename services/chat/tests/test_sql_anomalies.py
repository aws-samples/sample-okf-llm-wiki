"""SQL anomaly detection: the pure checks, the composer, and the tool budget.

The detector must be TOTAL (0/1-row results are no-ops or the zero-rows
finding, junk values never raise) and the reminder must stay an observation —
the anti-result-shopping closer ("report them plainly") is pinned here so a
future edit can't quietly turn the reminder into "re-run until it looks right".
The tool-wrapper tests prove the per-turn discipline: at most
MAX_INJECTIONS_PER_TURN reminders, deduped by finding kind, fail-open on a
detector bug, and clean results pass through byte-identical.
"""

from __future__ import annotations

import json

import pytest

from chat import sql_anomalies as sa
from chat.sql import make_sql_tool


def _result(rows, columns=None, truncated=False):
    if columns is None:
        columns = sorted({k for r in rows for k in r}) if rows else []
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def _kinds(result):
    return {f.kind for f in sa.detect(result)}


# --- the pure checks ---------------------------------------------------------


def test_zero_rows_yields_exactly_the_zero_rows_finding():
    findings = sa.detect(_result([], columns=["a", "b"]))
    assert [f.kind for f in findings] == ["zero_rows"]


def test_single_row_count_star_is_a_no_op():
    # The most common result shape — SELECT COUNT(*) — must never trigger
    # (every check has a minimum-support floor; no median-of-one).
    assert _kinds(_result([{"cnt": "42"}])) == set()


def test_disproportion_fires_on_ratio():
    rows = [{"k": str(i), "v": v} for i, v in enumerate(["2", "1", "3", "2", "5000000"])]
    findings = sa.detect(_result(rows, columns=["k", "v"]))
    disp = [f for f in findings if f.kind == "disproportion"]
    assert disp and '"v"' in disp[0].observation


def test_disproportion_fires_on_top_share_without_ratio():
    # max/median = 100 (< ratio threshold) but one value is ≥95% of the total.
    rows = [{"k": str(i), "v": v} for i, v in enumerate(["2", "2", "2", "2", "200"])]
    assert "disproportion" in _kinds(_result(rows, columns=["k", "v"]))


def test_disproportion_skipped_on_truncated_results():
    rows = [{"k": str(i), "v": v} for i, v in enumerate(["2", "1", "3", "2", "5000000"])]
    assert "disproportion" not in _kinds(
        _result(rows, columns=["k", "v"], truncated=True)
    )


def test_duplicate_rows_fire_on_multi_column_results_only():
    dup = {"a": "1", "b": "x"}
    rows = [dup, dup, dup, {"a": "2", "b": "y"}]
    assert "duplicate_rows" in _kinds(_result(rows, columns=["a", "b"]))
    # A single-column result legitimately repeats values (SELECT status FROM t).
    rows1 = [{"s": "open"}] * 10
    assert "duplicate_rows" not in _kinds(_result(rows1, columns=["s"]))


def test_sentinel_fires_on_repeated_all_nines_and_minus_one():
    rows = [{"a": str(i), "inc": "9999"} for i in range(3)] + [
        {"a": "9", "inc": "52000"}
    ]
    assert "sentinel" in _kinds(_result(rows, columns=["a", "inc"]))
    rows_neg = [{"a": str(i), "code": "-1"} for i in range(3)]
    assert "sentinel" in _kinds(_result(rows_neg, columns=["a", "code"]))


def test_sentinel_needs_repeats_and_three_digits():
    # Two occurrences: below the repeat floor. "99" could be a real age.
    rows = [{"a": "1", "v": "9999"}, {"a": "2", "v": "9999"}, {"a": "3", "v": "1"}]
    assert "sentinel" not in _kinds(_result(rows, columns=["a", "v"]))
    rows99 = [{"a": str(i), "v": "99"} for i in range(5)]
    assert "sentinel" not in _kinds(_result(rows99, columns=["a", "v"]))


def test_null_heavy_needs_enough_rows():
    rows = [{"a": str(i), "ship": None} for i in range(9)] + [
        {"a": "9", "ship": "2024-01-01"}
    ]
    assert "null_heavy" in _kinds(_result(rows, columns=["a", "ship"]))
    assert "null_heavy" not in _kinds(_result(rows[:5], columns=["a", "ship"]))


def test_negative_count_like_column_fires_softly():
    rows = [{"order_count": v} for v in ["3", "-5", "7", "2", "1"]]
    assert "negative_measure" in _kinds(_result(rows, columns=["order_count"]))
    # amount/total legitimately go negative — deliberately not matched.
    rows_amt = [{"amount": v} for v in ["3", "-5", "7", "2", "1"]]
    assert "negative_measure" not in _kinds(_result(rows_amt, columns=["amount"]))


def test_detector_is_total_on_junk():
    # Unparseable values, missing keys, mixed None — never raises.
    rows = [
        {"a": "abc", "b": None},
        {"a": "", "b": "∞"},
        {"a": None, "b": "1e309"},
        {"b": "x"},
    ]
    sa.detect(_result(rows, columns=["a", "b"]))
    sa.detect({"columns": [], "rows": [], "row_count": 0, "truncated": False})


# --- the composer ------------------------------------------------------------


def test_compose_lists_only_fired_findings_with_their_hints():
    findings = [
        sa.Finding("duplicate_rows", "14 of 200 rows are exact duplicates", "check X"),
    ]
    text = sa.compose(findings)
    assert text.startswith("<system-reminder>") and text.endswith("</system-reminder>")
    assert "14 of 200 rows are exact duplicates — check X" in text
    assert "sentinel" not in text  # no recitation of checks that passed


def test_compose_pins_the_anti_result_shopping_closer():
    # The closer licenses a verified surprising number — without it the
    # reminder reads as "re-run until it looks right".
    text = sa.compose([sa.Finding("sentinel", "obs", "hint")])
    assert "report them plainly" in text


def test_compose_zero_rows_uses_its_own_template():
    text = sa.compose([sa.Finding("zero_rows", "the query returned zero rows", "")])
    assert "zero rows" in text and "report it plainly" in text


def test_compose_caps_listed_findings():
    findings = [sa.Finding(f"k{i}", f"obs{i}", "h") for i in range(8)]
    text = sa.compose(findings)
    assert "obs3" in text and "obs4" not in text


# --- the tool wrapper: per-turn budget + fail-open ----------------------------


class _FakeEngine:
    """Yields a scripted sequence of results; mirrors the engines' surface."""

    tool_description = "fake run_sql"

    def __init__(self, results):
        self._results = list(results)

    def run(self, sql, *, default_database=None):
        return self._results.pop(0)


_DUP_ROWS = [{"a": "1", "b": "x"}] * 5 + [{"a": "2", "b": "y"}]
_CLEAN = [{"a": str(i), "b": str(i)} for i in range(6)]
_SENTINEL_ROWS = [{"a": str(i), "v": "9999"} for i in range(4)]


def test_reminder_appended_and_kind_deduped_and_capped():
    tool = make_sql_tool(
        _FakeEngine(
            [
                _result(_DUP_ROWS, columns=["a", "b"]),  # dup → injection 1
                _result(_DUP_ROWS, columns=["a", "b"]),  # same kind → deduped
                _result([], columns=["a"]),  # zero rows → injection 2
                _result(_SENTINEL_ROWS, columns=["a", "v"]),  # cap hit → clean
            ]
        )
    )
    first = tool.func("SELECT 1")
    assert isinstance(first, str) and "<system-reminder>" in first
    # The payload survives intact ahead of the reminder.
    payload = json.loads(first.split("\n\n")[0])
    assert payload["row_count"] == len(_DUP_ROWS)

    second = tool.func("SELECT 2")
    assert isinstance(second, dict)  # same kind again → no reminder

    third = tool.func("SELECT 3")
    assert isinstance(third, str) and "zero rows" in third

    fourth = tool.func("SELECT 4")
    assert isinstance(fourth, dict)  # budget (2) exhausted


def test_clean_results_pass_through_unchanged():
    tool = make_sql_tool(_FakeEngine([_result(_CLEAN, columns=["a", "b"])]))
    out = tool.func("SELECT 1")
    assert out == _result(_CLEAN, columns=["a", "b"])


def test_detector_failure_is_fail_open(monkeypatch):
    tool = make_sql_tool(_FakeEngine([_result(_DUP_ROWS, columns=["a", "b"])]))
    monkeypatch.setattr(
        sa, "detect", lambda result: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    out = tool.func("SELECT 1")
    assert out == _result(_DUP_ROWS, columns=["a", "b"])  # plain result, no crash
