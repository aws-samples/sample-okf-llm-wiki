"""Question-bank generation Control API: start/list/get/apply/delete."""

from __future__ import annotations

import json

import pytest

from okf_core import qbank as qb

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


def _dataset(cfg, domain="sales", dataset="orders", db="sales_curated"):
    handlers.upsert_domain_mapping(
        cfg.ddb, registry_table=REGISTRY,
        data_domain=domain, dataset=dataset, glue_database=db,
    )


def _start(cfg, body=None):
    payload = {"count": 24, "checks": ["sql", "behavior"], "sql_share": 0.75}
    payload.update(body or {})
    return app.route(
        _event("POST", "/benchmark/sales/orders/qbanks", body=payload), cfg
    )


_QUESTIONS = [
    {
        "question": "Which team won the most races?",
        "check": "sql",
        "gold_sql": "SELECT 1",
        "expected_behavior": "",
        "tier": "easy",
        "dimension": "direct_retrieval",
        "validation": {"executed": True, "row_count": 3},
    },
    {
        "question": "How long do pit stops take?",
        "check": "behavior",
        "gold_sql": "",
        "expected_behavior": "Should say durations are not tracked.",
        "tier": "medium",
        "dimension": "unanswerable",
        "validation": {},
    },
]


def _complete_bank(cfg, qbank_id, *, questions=None, status="complete"):
    """Seed a completed QBANK row + artifact, as the runtime would leave them."""
    cfg.ddb.update_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": qb.qbank_sk(qbank_id)}},
        UpdateExpression="SET #s = :s, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": {"S": status},
            ":u": {"S": "2026-08-12T10:00:00+00:00"},
        },
    )
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=qb.qbank_key("sales", "orders", qbank_id),
        Body=json.dumps(
            {
                "qbank_id": qbank_id,
                "questions": questions if questions is not None else _QUESTIONS,
                "dropped": [],
                "counts": {"requested": 24, "delivered": 2},
            }
        ).encode(),
    )


# -- start ---------------------------------------------------------------------


def test_start_writes_queued_row_and_invokes_the_runtime(cfg):
    _dataset(cfg)
    resp = _start(cfg)
    assert resp["statusCode"] == 200
    out = json.loads(resp["body"])
    qbank_id = out["qbank_id"]
    assert qb.is_valid_qbank_id(qbank_id)
    assert out["status"] == "queued"
    assert out["config"]["count"] == 24

    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": qb.qbank_sk(qbank_id)}},
    )["Item"]
    assert item["status"]["S"] == "queued"
    assert item["requested_count"]["N"] == "24"
    assert item["checks"]["S"] == "sql,behavior"

    call = cfg.agentcore.calls[-1]
    payload = json.loads(call["payload"])
    assert payload["mode"] == "generate_questions"
    assert payload["qbank_id"] == qbank_id
    assert payload["config"]["count"] == 24
    assert payload["config"]["dimensions"] == list(qb.DIMENSION_KEYS)
    assert payload["source"]["glue_database"] == "sales_curated"
    # No harvest lease taken — generations run concurrently with harvests.
    status_row = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
    )
    assert "Item" not in status_row


def test_start_validates_config_here_not_on_the_row(cfg):
    _dataset(cfg)
    assert _start(cfg, {"count": 5})["statusCode"] == 400
    assert _start(cfg, {"count": 500})["statusCode"] == 400
    assert _start(cfg, {"checks": ["vibes"]})["statusCode"] == 400
    assert _start(cfg, {"dimensions": ["telepathy"]})["statusCode"] == 400
    # An accuracy-only run over behavior-only dimensions has unfillable slots.
    resp = _start(cfg, {"checks": ["sql"], "dimensions": ["counterfactual"]})
    assert resp["statusCode"] == 400
    assert "no selected dimension" in json.loads(resp["body"])["error"]
    # Nothing above wrote a row.
    q = cfg.ddb.query(
        TableName=REGISTRY,
        KeyConditionExpression="pk = :pk AND begins_with(sk, :skp)",
        ExpressionAttributeValues={
            ":pk": {"S": "HARVEST#sales#orders"},
            ":skp": {"S": qb.QBANK_SK_PREFIX},
        },
    )
    assert q["Count"] == 0


def test_start_validates_models_against_catalog(cfg):
    _dataset(cfg)
    resp = _start(cfg, {"model": "made-up-model"})
    assert resp["statusCode"] == 400


def test_start_unregistered_dataset_is_404(cfg):
    resp = _start(cfg)
    assert resp["statusCode"] == 404


# -- list / get -------------------------------------------------------------------


def test_list_and_get_round_trip(cfg):
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]

    listed = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/qbanks"), cfg)["body"]
    )
    assert [r["qbank_id"] for r in listed["qbanks"]] == [qbank_id]
    assert listed["qbanks"][0]["status"] == "queued"

    # Queued: row only, no bank yet.
    got = json.loads(
        app.route(
            _event("GET", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg
        )["body"]
    )
    assert got["row"]["status"] == "queued"
    assert got["bank"] is None and got["csv"] is None

    # Complete: the artifact + the canonical CSV rendering ride along.
    _complete_bank(cfg, qbank_id)
    got = json.loads(
        app.route(
            _event("GET", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg
        )["body"]
    )
    assert [q["question"] for q in got["bank"]["questions"]] == [
        q["question"] for q in _QUESTIONS
    ]
    assert got["csv"].splitlines()[0] == "question,gold_sql,expected_behavior,tier,dimension"


def test_get_unknown_or_invalid_id(cfg):
    _dataset(cfg)
    assert (
        app.route(_event("GET", "/benchmark/sales/orders/qbanks/qb20260101t000000-aaaa1111"), cfg)[
            "statusCode"
        ]
        == 404
    )
    assert (
        app.route(_event("GET", "/benchmark/sales/orders/qbanks/not-a-qbank-id"), cfg)[
            "statusCode"
        ]
        == 400
    )


# -- apply --------------------------------------------------------------------------


def test_apply_writes_the_canonical_questions_csv(cfg):
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]
    _complete_bank(cfg, qbank_id)

    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/qbanks/{qbank_id}/apply"), cfg
    )
    assert resp["statusCode"] == 200
    out = json.loads(resp["body"])
    assert out == {
        "applied": True,
        "qbank_id": qbank_id,
        "question_count": 2,
        "check_counts": {"sql": 1, "behavior": 1},
    }
    # The canonical key now holds the rendered bank — the exact bytes the
    # download offers, parseable by the studio's own parser (inspect route).
    body = cfg.s3.get_object(
        Bucket=BUCKET, Key=handlers.benchmark_questions_key("sales", "orders")
    )["Body"].read().decode()
    assert "Which team won the most races?" in body
    inspect = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/questions"), cfg)["body"]
    )
    assert inspect["check_counts"] == {"sql": 1, "behavior": 1}


def test_apply_refuses_incomplete_or_empty_banks(cfg):
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]
    # Still queued → 409.
    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/qbanks/{qbank_id}/apply"), cfg
    )
    assert resp["statusCode"] == 409
    # Complete but zero questions (everything dropped) → 409, not an empty CSV.
    _complete_bank(cfg, qbank_id, questions=[])
    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/qbanks/{qbank_id}/apply"), cfg
    )
    assert resp["statusCode"] == 409
    assert "no questions" in json.loads(resp["body"])["error"]


# -- cancel -------------------------------------------------------------------------


def test_cancel_stops_the_session_flips_the_row_and_purges_the_artifact(cfg):
    _dataset(cfg)
    started = json.loads(_start(cfg)["body"])
    qbank_id = started["qbank_id"]
    # Simulate the race the purge exists for: the runtime PUT a partial
    # artifact just before the kill landed.
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=qb.qbank_key("sales", "orders", qbank_id),
        Body=json.dumps({"questions": [{"question": "partial"}]}).encode(),
    )

    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/qbanks/{qbank_id}/cancel"), cfg
    )
    assert resp["statusCode"] == 200
    out = json.loads(resp["body"])
    assert out["cancelled"] is True and out["status"] == "cancelled"
    # The EXACT session invoked at start was stopped.
    invoked_session = cfg.agentcore.calls[-1]["runtimeSessionId"]
    assert cfg.agentcore.stop_calls[-1]["runtimeSessionId"] == invoked_session
    # Row is terminal-cancelled; the partial artifact did NOT survive.
    row = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": qb.qbank_sk(qbank_id)}},
    )["Item"]
    assert row["status"]["S"] == "cancelled"
    with pytest.raises(Exception):
        cfg.s3.get_object(Bucket=BUCKET, Key=qb.qbank_key("sales", "orders", qbank_id))
    # A cancelled bank is unreachable through get (bank None) and apply (409),
    # and deletable without the active-run 409.
    got = json.loads(
        app.route(
            _event("GET", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg
        )["body"]
    )
    assert got["row"]["status"] == "cancelled" and got["bank"] is None
    assert (
        app.route(
            _event("POST", f"/benchmark/sales/orders/qbanks/{qbank_id}/apply"), cfg
        )["statusCode"]
        == 409
    )
    assert (
        app.route(_event("DELETE", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg)[
            "statusCode"
        ]
        == 200
    )


def test_cancel_a_finished_generation_reports_the_terminal_state(cfg):
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]
    _complete_bank(cfg, qbank_id)
    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/qbanks/{qbank_id}/cancel"), cfg
    )
    assert resp["statusCode"] == 409
    # The completed bank is untouched — cancel never clobbers terminal work.
    got = json.loads(
        app.route(
            _event("GET", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg
        )["body"]
    )
    assert got["row"]["status"] == "complete" and got["bank"] is not None


def test_a_partial_artifact_behind_a_non_complete_row_is_unreachable(cfg):
    # Defense in depth for any orphan that survives every other layer: get
    # serves the artifact for COMPLETE rows only.
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=qb.qbank_key("sales", "orders", qbank_id),
        Body=json.dumps({"questions": [{"question": "orphan"}]}).encode(),
    )
    got = json.loads(
        app.route(
            _event("GET", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg
        )["body"]
    )
    assert got["row"]["status"] == "queued"
    assert got["bank"] is None and got["csv"] is None


# -- delete -------------------------------------------------------------------------


def test_delete_purges_artifact_and_row_with_active_guard(cfg):
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]
    # Queued with a fresh heartbeat → 409 (mirrors report deletion).
    resp = app.route(
        _event("DELETE", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg
    )
    assert resp["statusCode"] == 409

    _complete_bank(cfg, qbank_id)
    resp = app.route(
        _event("DELETE", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["deleted"] is True
    listed = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/qbanks"), cfg)["body"]
    )
    assert listed["qbanks"] == []
    # The gold-carrying artifact is gone (all versions purged).
    with pytest.raises(Exception):
        cfg.s3.get_object(
            Bucket=BUCKET, Key=qb.qbank_key("sales", "orders", qbank_id)
        )


def test_routes_are_registered():
    from control_api import app as app_mod

    paths = {(m, t) for m, t, _fn in app_mod._ROUTES}
    for route in (
        ("POST", "/benchmark/{domain}/{dataset}/qbanks"),
        ("GET", "/benchmark/{domain}/{dataset}/qbanks"),
        ("GET", "/benchmark/{domain}/{dataset}/qbanks/{qbank_id}"),
        ("POST", "/benchmark/{domain}/{dataset}/qbanks/{qbank_id}/apply"),
        ("POST", "/benchmark/{domain}/{dataset}/qbanks/{qbank_id}/cancel"),
        ("DELETE", "/benchmark/{domain}/{dataset}/qbanks/{qbank_id}"),
    ):
        assert route in paths


# -- the fail-row guard: cancel and delete outrank a late failure ----------------


def test_fail_qbank_row_cannot_overwrite_a_cancel_or_resurrect_a_deleted_row(cfg):
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]
    key = {"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": qb.qbank_sk(qbank_id)}}
    # Operator cancels while the (hung) invoke is still in flight...
    cfg.ddb.update_item(
        TableName=REGISTRY, Key=key,
        UpdateExpression="SET #s = :c",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":c": {"S": "cancelled"}},
    )
    # ...then the invoke finally raises and tries to flip the row to failed.
    handlers._fail_qbank_row(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        qbank_id=qbank_id, detail="invoke failed: timeout",
    )
    item = cfg.ddb.get_item(TableName=REGISTRY, Key=key)["Item"]
    assert item["status"]["S"] == "cancelled"  # the cancel verdict stands

    # Deleted row: the conditional update must NOT upsert a keyless ghost.
    cfg.ddb.delete_item(TableName=REGISTRY, Key=key)
    handlers._fail_qbank_row(
        cfg.ddb, registry_table=REGISTRY, data_domain="sales", dataset="orders",
        qbank_id=qbank_id, detail="invoke failed: timeout",
    )
    assert "Item" not in cfg.ddb.get_item(TableName=REGISTRY, Key=key)
    listed = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/qbanks"), cfg)["body"]
    )
    assert listed["qbanks"] == []


def test_deleting_the_dataset_purges_qbank_rows_too(cfg):
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]
    _complete_bank(cfg, qbank_id)
    resp = app.route(_event("DELETE", "/domains/sales/datasets/orders"), cfg)
    assert resp["statusCode"] == 200
    # Re-registering must not resurrect ghost banks whose artifacts are gone.
    _dataset(cfg)
    listed = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/qbanks"), cfg)["body"]
    )
    assert listed["qbanks"] == []


def test_an_oversized_bank_degrades_to_a_presigned_url_with_inline_csv(cfg, monkeypatch):
    # The artifact outgrew the Lambda-response inline cap: `bank` ships as a
    # short-lived URL (never a 502), while the much smaller canonical CSV is
    # still rendered server-side so Download keeps working.
    _dataset(cfg)
    qbank_id = json.loads(_start(cfg)["body"])["qbank_id"]
    _complete_bank(cfg, qbank_id)
    # Pad the artifact (outside `questions`) past a lowered cap; the CSV —
    # questions only — stays under it.
    doc = json.loads(
        cfg.s3.get_object(
            Bucket=BUCKET, Key=qb.qbank_key("sales", "orders", qbank_id)
        )["Body"].read()
    )
    doc["telemetry"] = {"padding": "x" * 4096}
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=qb.qbank_key("sales", "orders", qbank_id),
        Body=json.dumps(doc).encode(),
    )
    monkeypatch.setattr(handlers, "_BENCHMARK_INLINE_MAX_BYTES", 2048)
    out = json.loads(
        app.route(
            _event("GET", f"/benchmark/sales/orders/qbanks/{qbank_id}"), cfg
        )["body"]
    )
    assert out["bank"] is None
    assert out["bank_url"] and "http" in out["bank_url"]
    assert out["csv"] and out["csv"].splitlines()[0] == ",".join(qb.CSV_HEADER)
