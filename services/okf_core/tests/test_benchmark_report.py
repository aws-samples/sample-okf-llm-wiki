"""Benchmark Studio pure invariants: run config, report ids/keys, score math."""

from __future__ import annotations

import pytest

from okf_core import benchmark_report as br
from okf_core.benchmark_questions import ALL_CHECKS


def test_validate_checks_normalizes_order_and_dedupes():
    assert br.validate_checks(["behavior", "sql", "SQL"]) == ["sql", "behavior"]
    assert br.validate_checks(list(ALL_CHECKS)) == list(ALL_CHECKS)


def test_validate_checks_rejects_empty_and_unknown():
    with pytest.raises(br.BenchmarkRunConfigError):
        br.validate_checks([])
    with pytest.raises(br.BenchmarkRunConfigError):
        br.validate_checks(["sql", "vibes"])
    with pytest.raises(br.BenchmarkRunConfigError):
        br.validate_checks("sql")  # a bare string is not a list


def test_coerce_runs_clamps_and_defaults():
    assert br.coerce_runs(None) == br.DEFAULT_RUNS
    assert br.coerce_runs("") == br.DEFAULT_RUNS
    assert br.coerce_runs(3) == 3
    assert br.coerce_runs("2") == 2
    assert br.coerce_runs(99) == br.MAX_RUNS
    assert br.coerce_runs(0) == br.MIN_RUNS
    with pytest.raises(br.BenchmarkRunConfigError):
        br.coerce_runs("lots")


def test_report_id_shape_and_sk():
    rid = br.new_report_id(now_compact="20260729T101500", token="a1b2c3d4")
    assert br.is_valid_report_id(rid)
    assert rid.startswith("r20260729t101500-")
    assert br.report_sk(rid) == f"REPORT#{rid}"
    assert br.report_sk_query_prefix() == "REPORT#"
    # URL/S3/DDB-hostile ids are refused.
    for bad in ("", "UPPER", "has/slash", "a" * 100, "sp ace", None, "short"):
        assert not br.is_valid_report_id(bad)


def test_s3_keys_are_off_mount_and_cohesive():
    key = br.report_key("sales", "orders", "r1-abc123ff")
    assert key == "benchmark/sales/orders/reports/r1-abc123ff/report.json"
    assert not key.startswith("okf/")  # gold-carrying → off the agent mount
    assert br.traces_key("sales", "orders", "r1-abc123ff").endswith(
        "r1-abc123ff/traces.json"
    )
    assert br.report_prefix("sales", "orders", "r1-abc123ff").endswith("/")


def test_score_arithmetic():
    assert br.score(0, 0) == 0.0
    assert br.score(3, 4) == 0.75
    assert br.adjusted_score(3, 1, 4) == 1.0
    assert br.adjusted_score(3, 0, 4) == 0.75
    assert br.adjusted_score(0, 0, 0) == 0.0
    # Forgiveness can't exceed 1.0 even with over-counting upstream.
    assert br.adjusted_score(4, 2, 4) == 1.0


def test_mean_and_spread_is_range_not_stddev():
    mean, spread = br.mean_and_spread([0.62, 0.71, 0.65])
    assert round(mean, 4) == round((0.62 + 0.71 + 0.65) / 3, 4)
    assert round(spread, 4) == round(0.71 - 0.62, 4)
    assert br.mean_and_spread([]) == (0.0, 0.0)
    assert br.mean_and_spread([0.5]) == (0.5, 0.0)
