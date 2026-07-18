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
        settings={"enabled": True, "max_iterations": 4, "ex_threshold": 0.7,
                  "gate_kpis": ["ex", "judge"]},
    )
    out = handlers.get_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders"
    )["recursive_improvement"]
    assert out["enabled"] is True
    assert out["max_iterations"] == 4
    assert out["ex_threshold"] == 0.7
    assert out["gate_kpis"] == ["ex", "judge"]
    # A questions_key was defaulted to the off-mount canonical location.
    assert out["questions_key"] == "benchmark/sales/orders/questions.csv"


def test_set_ri_settings_clamps_max_iterations(cfg):
    _dataset(cfg)
    out = handlers.set_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        settings={"enabled": True, "max_iterations": 99},
    )["recursive_improvement"]
    assert out["max_iterations"] == 5  # clamped to MAX_ITERATIONS


def test_set_ri_settings_rejects_bad_gate_kpi(cfg):
    _dataset(cfg)
    with pytest.raises(ApiError) as ei:
        handlers.set_dataset_ri_settings(
            cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
            settings={"enabled": True, "gate_kpis": ["bogus"]},
        )
    assert ei.value.status == 400


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


# -- payload population on trigger -------------------------------------------


def test_trigger_harvest_includes_ri_block_when_enabled(cfg):
    _dataset(cfg)
    handlers.set_dataset_ri_settings(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        settings={"enabled": True, "ex_threshold": 0.8},
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
    resp = app.route(
        _event("PUT", "/benchmark/sales/orders",
               body={"enabled": True, "gate_kpis": ["nope"]}),
        cfg,
    )
    assert resp["statusCode"] == 400
