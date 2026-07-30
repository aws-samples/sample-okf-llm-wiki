"""Benchmark question-set upload: off-mount presign + per-check inspect feedback."""

from __future__ import annotations

import json

from control_api import app, handlers

from tests.conftest import BUCKET, REGISTRY


def _event(method, path, *, body=None):
    evt = {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": {"sub": "u1", "email": "u@x.com"}}},
        },
    }
    if body is not None:
        evt["body"] = json.dumps(body)
    return evt


def test_benchmark_key_is_off_the_okf_mount():
    key = handlers.benchmark_questions_key("sales", "orders")
    assert key == "benchmark/sales/orders/questions.csv"
    # The load-bearing property: NOT under okf/ (the harvest mount root).
    assert not key.startswith("okf/")


def test_presign_benchmark_pins_off_mount_key(cfg):
    out = handlers.presign_benchmark_upload(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders",
        content_type="text/csv",
    )
    assert out["key"] == "benchmark/sales/orders/questions.csv"
    assert out["max_bytes"] == handlers.CONTEXT_UPLOAD_MAX_BYTES
    assert "url" in out and "fields" in out


def test_inspect_reports_not_uploaded(cfg):
    resp = app.route(_event("GET", "/benchmark/sales/orders/questions"), cfg)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["uploaded"] is False


def _put_csv(cfg, text):
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=handlers.benchmark_questions_key("sales", "orders"),
        Body=text.encode(),
    )


def test_inspect_reports_per_check_counts(cfg):
    _put_csv(
        cfg,
        "question,gold_sql,expected_behavior\n"
        "Q0,SELECT 1,Should name the driver.\n"
        "Q1,,Should say durations are not tracked.\n",
    )
    out = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/questions"), cfg)["body"]
    )
    assert out["valid"] is True and out["count"] == 2
    assert out["check_counts"] == {"sql": 1, "behavior": 2}


def test_inspect_flags_retired_answer_only_csv(cfg):
    # gold_answer no longer resolves as a gold column (Answer Match is
    # retired) — a CSV that only carries it has no gold at all.
    _put_csv(cfg, "question,gold_answer\nQ0,28\n")
    out = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/questions"), cfg)["body"]
    )
    assert out["valid"] is False


def test_inspect_flags_missing_columns(cfg):
    _put_csv(cfg, "question,notes\nQ0,hello\n")
    out = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/questions"), cfg)["body"]
    )
    assert out["valid"] is False


def test_settings_routes_are_retired(cfg):
    # The RI settings surface is gone: GET/PUT /benchmark/{d}/{ds} no longer route.
    assert app.route(_event("GET", "/benchmark/sales/orders"), cfg)["statusCode"] == 404
    assert app.route(
        _event("PUT", "/benchmark/sales/orders", body={"enabled": True}), cfg
    )["statusCode"] == 404
    # And so is the per-round review surface.
    assert app.route(
        _event("GET", "/benchmark/sales/orders/reviews/sess-1/0"), cfg
    )["statusCode"] == 404
