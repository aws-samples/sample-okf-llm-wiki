"""Control API recursive-improvement: off-mount presign, settings CRUD, payload."""

from __future__ import annotations

import json

import pytest

from control_api import app, handlers
from control_api.app import ApiError

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


def _dataset(cfg, domain="sales", dataset="orders", db="sales_curated"):
    handlers.upsert_domain_mapping(
        cfg.ddb, registry_table=REGISTRY,
        data_domain=domain, dataset=dataset, glue_database=db,
    )


# -- off-mount CSV presign ---------------------------------------------------


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


# -- settings CRUD + validation ----------------------------------------------


def test_get_ri_settings_default_disabled(cfg):
    _dataset(cfg)
    out = handlers.get_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders"
    )
    assert out["recursive_improvement"] == {"enabled": False}


def test_get_ri_settings_404_for_missing_dataset(cfg):
    with pytest.raises(ApiError) as ei:
        handlers.get_dataset_ri_settings(
            cfg.ddb, registry_table=REGISTRY, data_domain="x", dataset="y"
        )
    assert ei.value.status == 404


def test_set_and_get_ri_settings_roundtrip(cfg):
    _dataset(cfg)
    handlers.set_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        settings={"enabled": True, "max_iterations": 4},
    )
    out = handlers.get_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders"
    )["recursive_improvement"]
    assert out["enabled"] is True
    assert out["max_iterations"] == 4
    # The fixed target means no threshold/gate fields are stored.
    assert "ex_threshold" not in out
    assert "gate_kpis" not in out
    # A questions_key was defaulted to the off-mount canonical location.
    assert out["questions_key"] == "benchmark/sales/orders/questions.csv"


def test_set_ri_settings_clamps_max_iterations(cfg):
    _dataset(cfg)
    out = handlers.set_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        settings={"enabled": True, "max_iterations": 99},
    )["recursive_improvement"]
    assert out["max_iterations"] == 5  # clamped to MAX_ITERATIONS


def test_set_ri_settings_ignores_legacy_threshold_fields(cfg):
    # Old clients may still POST threshold/gate keys; they're ignored, not 400, and
    # not persisted (the target is fixed now).
    _dataset(cfg)
    out = handlers.set_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        settings={"enabled": True, "ex_threshold": 0.7, "gate_kpis": ["bogus"]},
    )["recursive_improvement"]
    assert "ex_threshold" not in out and "gate_kpis" not in out
    assert out["enabled"] is True


def test_set_ri_settings_disabled_stores_marker(cfg):
    _dataset(cfg)
    out = handlers.set_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        settings={"enabled": False},
    )["recursive_improvement"]
    assert out == {"enabled": False}


def test_set_ri_settings_404_for_missing_dataset(cfg):
    with pytest.raises(ApiError) as ei:
        handlers.set_dataset_ri_settings(
            cfg.ddb, registry_table=REGISTRY, data_domain="x", dataset="y",
            settings={"enabled": True},
        )
    assert ei.value.status == 404


# -- inspect uploaded questions (validation + count feedback) ----------------


def _put_csv(cfg, text, domain="sales", dataset="orders"):
    key = handlers.benchmark_questions_key(domain, dataset)
    cfg.s3.put_object(Bucket=BUCKET, Key=key, Body=text.encode("utf-8"))


def test_inspect_not_uploaded(cfg):
    out = handlers.inspect_benchmark_questions(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    assert out == {"uploaded": False, "key": "benchmark/sales/orders/questions.csv"}


def test_inspect_valid_counts_questions(cfg):
    _put_csv(cfg, "question,gold_sql\nQ0,SELECT 0\nQ1,SELECT 1\nQ2,SELECT 2\n")
    out = handlers.inspect_benchmark_questions(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    assert out["uploaded"] is True and out["valid"] is True
    assert out["count"] == 3
    assert out["total_in_csv"] == 3
    assert out["dropped"] == 0
    assert out["capped"] is False
    assert out["max_questions"] == 100


def test_inspect_skips_blank_rows_in_count(cfg):
    _put_csv(cfg, "question,gold_sql\nQ0,SELECT 0\n,SELECT 1\nQ2,\nQ3,SELECT 3\n")
    out = handlers.inspect_benchmark_questions(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    assert out["count"] == 2  # blank question and blank gold both skipped


def test_inspect_reports_cap(cfg):
    rows = "\n".join(f"Q{i},SELECT {i}" for i in range(130))
    _put_csv(cfg, "question,gold_sql\n" + rows + "\n")
    out = handlers.inspect_benchmark_questions(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    assert out["count"] == 100
    assert out["total_in_csv"] == 130
    assert out["dropped"] == 30
    assert out["capped"] is True


def test_inspect_bad_header_is_invalid(cfg):
    _put_csv(cfg, "question,notes\nQ0,hello\n")
    out = handlers.inspect_benchmark_questions(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    assert out["uploaded"] is True and out["valid"] is False
    assert "gold" in out["error"].lower()


def test_inspect_all_blank_is_invalid(cfg):
    _put_csv(cfg, "question,gold_sql\n,\n,\n")
    out = handlers.inspect_benchmark_questions(
        cfg.s3, bucket=BUCKET, data_domain="sales", dataset="orders"
    )
    assert out["uploaded"] is True and out["valid"] is False
    assert "no valid rows" in out["error"]


def test_inspect_route(cfg):
    _put_csv(cfg, "question,gold_sql\nQ0,SELECT 0\n")
    resp = app.route(_event("GET", "/benchmark/sales/orders/questions"), cfg)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["count"] == 1
    # The /questions suffix must NOT be captured as the settings GET.
    assert "recursive_improvement" not in body


# -- payload population on trigger -------------------------------------------


def test_trigger_harvest_includes_ri_block_when_enabled(cfg):
    _dataset(cfg)
    handlers.set_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        settings={"enabled": True, "max_iterations": 4},
    )
    handlers.trigger_harvest(
        cfg.agentcore, cfg.ddb, registry_table=REGISTRY,
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/h",
        data_domain="sales", dataset="orders", mode="full",
    )
    payload = cfg.agentcore.last_payload()
    assert "recursive_improvement" in payload
    ri_block = payload["recursive_improvement"]
    assert ri_block["enabled"] is True
    assert ri_block["questions_key"] == "benchmark/sales/orders/questions.csv"


def test_trigger_harvest_omits_ri_block_when_disabled(cfg):
    _dataset(cfg)
    # No RI settings set at all → block omitted → normal harvest.
    handlers.trigger_harvest(
        cfg.agentcore, cfg.ddb, registry_table=REGISTRY,
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/h",
        data_domain="sales", dataset="orders", mode="full",
    )
    payload = cfg.agentcore.last_payload()
    assert "recursive_improvement" not in payload


# -- routes ------------------------------------------------------------------


def test_benchmark_presign_route(cfg):
    resp = app.route(
        _event("POST", "/benchmark/sales/orders/presign", body={"content_type": "text/csv"}),
        cfg,
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["key"] == "benchmark/sales/orders/questions.csv"


def test_benchmark_settings_routes_roundtrip(cfg):
    _dataset(cfg)
    put = app.route(
        _event("PUT", "/benchmark/sales/orders",
               body={"enabled": True, "max_iterations": 3}),
        cfg,
    )
    assert put["statusCode"] == 200
    get = app.route(_event("GET", "/benchmark/sales/orders"), cfg)
    out = json.loads(get["body"])["recursive_improvement"]
    assert out["enabled"] is True
    assert out["max_iterations"] == 3


def test_benchmark_settings_route_validation_400(cfg):
    _dataset(cfg)
    # A non-integer max_iterations can't be coerced → 400 (the one remaining
    # validation error now that the target is fixed).
    resp = app.route(
        _event("PUT", "/benchmark/sales/orders",
               body={"enabled": True, "max_iterations": "lots"}),
        cfg,
    )
    assert resp["statusCode"] == 400


def test_benchmark_review_route_reads_off_mount_json(cfg):
    # The review endpoint reads the off-mount per-round JSON the harvest runtime
    # wrote (gold-carrying, human-facing) and returns it verbatim.
    import json as _json

    key = handlers.benchmark_review_key("sales", "orders", "sess-1", 0)
    doc = {
        "iteration": 0,
        "counts": {"passed": 1, "genuine_error": 1},
        "questions": [
            {"q_id": 0, "bucket": "passed", "question": "Q0", "gold_sql": "G0",
             "predicted_sql": "P0", "note": "", "reason": "match"},
            {"q_id": 1, "bucket": "genuine_error", "question": "Q1", "gold_sql": "G1",
             "predicted_sql": "W1", "note": "docs miss X", "reason": "differ"},
        ],
    }
    cfg.s3.put_object(Bucket=cfg.bucket, Key=key, Body=_json.dumps(doc).encode())
    resp = app.route(
        _event("GET", "/benchmark/sales/orders/reviews/sess-1/0"), cfg
    )
    assert resp["statusCode"] == 200
    body = _json.loads(resp["body"])
    assert body["counts"]["passed"] == 1
    assert {q["q_id"] for q in body["questions"]} == {0, 1}
    # The gold IS present here — this endpoint is human-facing, off the agent path.
    assert any(q["gold_sql"] == "G1" for q in body["questions"])


def test_benchmark_review_route_404_when_absent(cfg):
    resp = app.route(
        _event("GET", "/benchmark/sales/orders/reviews/sess-x/9"), cfg
    )
    assert resp["statusCode"] == 404
