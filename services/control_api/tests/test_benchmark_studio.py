"""Benchmark Studio Control API: start/list/get/delete runs, aggregate, apply."""

from __future__ import annotations

import json

import pytest

from okf_core import benchmark_report as br

from control_api import app, handlers

from tests.conftest import ANNOTATIONS, BUCKET, REGISTRY


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


def _upload_csv(cfg, text=None):
    text = text if text is not None else (
        "question,gold_sql,expected_behavior\n"
        "Most wins?,SELECT 1,Should name the driver.\n"
        "Pit stops?,,Should say durations are not tracked.\n"
    )
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=handlers.benchmark_questions_key("sales", "orders"),
        Body=text.encode(),
    )


def _start(cfg, body=None):
    payload = {"checks": ["sql", "behavior"], "runs": 2}
    payload.update(body or {})
    return app.route(
        _event("POST", "/benchmark/sales/orders/runs", body=payload), cfg
    )


# -- start ---------------------------------------------------------------------


def test_start_run_writes_queued_row_and_invokes_runtime(cfg):
    _dataset(cfg)
    _upload_csv(cfg)
    resp = _start(cfg)
    assert resp["statusCode"] == 200
    out = json.loads(resp["body"])
    report_id = out["report_id"]
    assert br.is_valid_report_id(report_id)
    assert out["status"] == "queued"
    assert out["check_counts"] == {"sql": 1, "behavior": 2}

    # The QUEUED index row exists with flat config summary scalars.
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"},
             "sk": {"S": br.report_sk(report_id)}},
    )["Item"]
    assert item["status"]["S"] == "queued"
    assert item["checks"]["S"] == "sql,behavior"
    assert item["runs"]["N"] == "2"
    assert item["agg_status"]["S"] == "idle"

    # The runtime got mode=benchmark with the run config; NO harvest lease row.
    call = cfg.agentcore.calls[-1]
    payload = json.loads(call["payload"])
    assert payload["mode"] == "benchmark"
    assert payload["report_id"] == report_id
    assert payload["checks"] == ["sql", "behavior"]
    assert payload["questions_key"] == "benchmark/sales/orders/questions.csv"
    assert payload["source"]["glue_database"] == "sales_curated"
    status_row = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": "STATUS"}},
    )
    assert "Item" not in status_row  # no lease taken — concurrent with harvests


def test_start_run_validates_config_and_question_set(cfg):
    _dataset(cfg)
    # No CSV uploaded yet → 400 with an actionable message.
    resp = _start(cfg)
    assert resp["statusCode"] == 400
    assert "question set" in json.loads(resp["body"])["error"]

    _upload_csv(cfg)
    assert _start(cfg, {"checks": ["vibes"]})["statusCode"] == 400
    assert _start(cfg, {"checks": []})["statusCode"] == 400
    # The retired Answer Match check is now just an unknown check name.
    assert _start(cfg, {"checks": ["answer"]})["statusCode"] == 400
    # behavior-only run over a set with no behavior golds for it → 400.
    _upload_csv(cfg, "question,gold_sql\nQ,SELECT 1\n")
    resp = _start(cfg, {"checks": ["behavior"]})
    assert resp["statusCode"] == 400
    assert "participates" in json.loads(resp["body"])["error"]


def test_start_run_validates_models_against_catalog(cfg):
    _dataset(cfg)
    _upload_csv(cfg)
    resp = _start(cfg, {"solver_model": "made-up-model"})
    assert resp["statusCode"] == 400
    resp = _start(cfg, {"judge_effort": "xhigh"})  # effort without model
    assert resp["statusCode"] == 400
    resp = _start(
        cfg,
        {"solver_model": "global.anthropic.claude-sonnet-5", "solver_effort": "high",
         "judge_model": "global.anthropic.claude-opus-5"},
    )
    assert resp["statusCode"] == 200
    payload = json.loads(cfg.agentcore.calls[-1]["payload"])
    assert payload["solver_model"] == "global.anthropic.claude-sonnet-5"
    assert payload["solver_effort"] == "high"
    assert payload["judge_model"] == "global.anthropic.claude-opus-5"
    # Judge effort fell back to the model's catalog default.
    assert payload["judge_effort"] == "xhigh"


def test_start_run_requires_registered_dataset(cfg):
    # No mapping row → fail fast with a 404 here, not minutes later on the row
    # when the runtime can't snapshot.
    _upload_csv(cfg)
    resp = _start(cfg)
    assert resp["statusCode"] == 404
    assert "no such dataset" in json.loads(resp["body"])["error"]


def test_start_run_rejects_oversize_csv(cfg, monkeypatch):
    # Start must REJECT an oversize CSV like inspect does — reading exactly the
    # cap silently truncated the set (and could split a row mid-line).
    _dataset(cfg)
    _upload_csv(cfg)
    monkeypatch.setattr(handlers, "_BENCHMARK_CSV_MAX_BYTES", 16)
    resp = _start(cfg)
    assert resp["statusCode"] == 400
    assert "exceeds" in json.loads(resp["body"])["error"]


def test_start_run_pins_questions_version(cfg):
    # The bundle bucket is versioned: the payload pins the CSV version counted
    # at start, so a re-upload between start and the runtime's fetch can't
    # swap the graded set.
    cfg.s3.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    _dataset(cfg)
    _upload_csv(cfg)
    v1 = cfg.s3.get_object(
        Bucket=BUCKET, Key=handlers.benchmark_questions_key("sales", "orders")
    )["VersionId"]
    assert _start(cfg)["statusCode"] == 200
    payload = json.loads(cfg.agentcore.calls[-1]["payload"])
    assert payload["questions_version_id"] == v1

    _upload_csv(cfg, "question,gold_sql\nDifferent?,SELECT 2\n")
    assert _start(cfg)["statusCode"] == 200
    payload = json.loads(cfg.agentcore.calls[-1]["payload"])
    assert payload["questions_version_id"] != v1


def test_start_run_omits_questions_version_on_unversioned_bucket(cfg):
    _dataset(cfg)
    _upload_csv(cfg)
    assert _start(cfg)["statusCode"] == 200
    payload = json.loads(cfg.agentcore.calls[-1]["payload"])
    assert "questions_version_id" not in payload


def test_start_run_rejected_ack_flips_row_failed_and_502(cfg):
    # invoke_agent_runtime returns 200 even when the runtime's synchronous ack
    # says "rejected" — the row must flip to failed, not rot as queued.
    _dataset(cfg)
    _upload_csv(cfg)
    cfg.agentcore.ack = {"status": "rejected", "error": "invalid report_id"}
    resp = _start(cfg)
    assert resp["statusCode"] == 502
    assert "invalid report_id" in json.loads(resp["body"])["error"]
    reports = json.loads(
        app.route(_event("GET", "/benchmark/sales/orders/runs"), cfg)["body"]
    )["reports"]
    assert reports[0]["status"] == "failed"
    assert "runtime rejected" in reports[0]["detail"]

    # An accepted ack passes through untouched.
    cfg.agentcore.ack = {"status": "accepted"}
    assert _start(cfg)["statusCode"] == 200


def test_start_run_rejects_unknown_version(cfg):
    _dataset(cfg)
    _upload_csv(cfg)
    resp = _start(cfg, {"version_id": "no-such-version"})
    assert resp["statusCode"] == 400
    assert "unknown bundle version" in json.loads(resp["body"])["error"]


def test_start_run_runs_are_clamped(cfg):
    _dataset(cfg)
    _upload_csv(cfg)
    out = json.loads(_start(cfg, {"runs": 99})["body"])
    assert out["runs"] == br.MAX_RUNS


# -- list / get / delete ---------------------------------------------------------


def _mint_report(cfg, status="complete", report_doc=None, **row_attrs):
    _dataset(cfg)
    _upload_csv(cfg)
    out = json.loads(_start(cfg)["body"])
    report_id = out["report_id"]
    attrs = {"status": {"S": status}}
    for k, v in row_attrs.items():
        attrs[k] = {"S": str(v)}
    expr = ", ".join(f"#k{i} = :v{i}" for i in range(len(attrs)))
    cfg.ddb.update_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"},
             "sk": {"S": br.report_sk(report_id)}},
        UpdateExpression=f"SET {expr}",
        ExpressionAttributeNames={f"#k{i}": k for i, k in enumerate(attrs)},
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(attrs.values())},
    )
    if report_doc is not None:
        cfg.s3.put_object(
            Bucket=BUCKET,
            Key=br.report_key("sales", "orders", report_id),
            Body=json.dumps(report_doc).encode(),
        )
    return report_id


def test_list_reports_newest_first(cfg):
    _dataset(cfg)
    _upload_csv(cfg)
    first = json.loads(_start(cfg)["body"])["report_id"]
    second = json.loads(_start(cfg)["body"])["report_id"]
    resp = app.route(_event("GET", "/benchmark/sales/orders/runs"), cfg)
    assert resp["statusCode"] == 200
    reports = json.loads(resp["body"])["reports"]
    ids = [r["report_id"] for r in reports]
    assert ids == sorted([first, second], reverse=True)
    assert reports[0]["status"] == "queued"
    assert reports[0]["runs"] == 2  # N attrs come back as plain ints


def test_get_report_merges_row_and_document(cfg):
    doc = {"report_id": "x", "scores": {"sql": {"raw": {"mean": 0.8}}}}
    report_id = _mint_report(cfg, status="complete", report_doc=doc)
    resp = app.route(
        _event("GET", f"/benchmark/sales/orders/runs/{report_id}"), cfg
    )
    body = json.loads(resp["body"])
    assert body["row"]["status"] == "complete"
    assert body["report"]["scores"]["sql"]["raw"]["mean"] == 0.8

    # While running, the document is absent — row only, no 404.
    running_id = _mint_report(cfg, status="running")
    body = json.loads(
        app.route(_event("GET", f"/benchmark/sales/orders/runs/{running_id}"), cfg)["body"]
    )
    assert body["row"]["status"] == "running" and body["report"] is None


def test_get_report_404_and_400(cfg):
    _dataset(cfg)
    assert app.route(
        _event("GET", "/benchmark/sales/orders/runs/r20990101t000000-deadbeef"), cfg
    )["statusCode"] == 404
    assert app.route(
        _event("GET", "/benchmark/sales/orders/runs/NOT_VALID"), cfg
    )["statusCode"] == 400


def test_delete_report_removes_prefix_and_row_but_refuses_running(cfg):
    report_id = _mint_report(cfg, status="running")
    resp = app.route(
        _event("DELETE", f"/benchmark/sales/orders/runs/{report_id}"), cfg
    )
    assert resp["statusCode"] == 409

    done_id = _mint_report(cfg, status="complete", report_doc={"report_id": "y"})
    cfg.s3.put_object(
        Bucket=BUCKET, Key=br.traces_key("sales", "orders", done_id), Body=b"{}"
    )
    resp = app.route(
        _event("DELETE", f"/benchmark/sales/orders/runs/{done_id}"), cfg
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["objects_deleted"] == 2
    assert app.route(
        _event("GET", f"/benchmark/sales/orders/runs/{done_id}"), cfg
    )["statusCode"] == 404
    listed = cfg.s3.list_objects_v2(
        Bucket=BUCKET, Prefix=br.report_prefix("sales", "orders", done_id)
    )
    assert listed.get("KeyCount", 0) == 0


def test_get_report_large_document_returns_presigned_url(cfg):
    # Past the inline cap the handler must degrade to a presigned GET (the doc
    # used to 502 forever — after hours of paid solving), never stream >4 MiB
    # through the Lambda (whose sync response tops out at 6 MB).
    big = {"report_id": "x", "pad": "x" * (5 * 1024 * 1024)}
    report_id = _mint_report(cfg, status="complete", report_doc=big)
    body = json.loads(
        app.route(_event("GET", f"/benchmark/sales/orders/runs/{report_id}"), cfg)["body"]
    )
    assert body["report"] is None
    assert body["report_url"] and body["report_url"].startswith("http")

    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=br.traces_key("sales", "orders", report_id),
        Body=json.dumps({"report_id": report_id, "pad": "y" * (5 * 1024 * 1024)}).encode(),
    )
    traces = json.loads(
        app.route(
            _event("GET", f"/benchmark/sales/orders/runs/{report_id}/traces"), cfg
        )["body"]
    )
    assert "traces" not in traces
    assert traces["traces_url"].startswith("http")


def test_delete_stale_running_report_is_allowed(cfg):
    # A runtime killed mid-run leaves a 'running' row whose terminal write was
    # lost; once the heartbeat is older than the lease-stale cutoff it must be
    # deletable, not a 409-forever zombie.
    report_id = _mint_report(
        cfg, status="running", updated_at="2020-01-01T00:00:00+00:00"
    )
    resp = app.route(
        _event("DELETE", f"/benchmark/sales/orders/runs/{report_id}"), cfg
    )
    assert resp["statusCode"] == 200


def test_delete_refused_while_aggregation_running(cfg):
    # A fresh agg_status=running blocks deletion (the aggregator would rewrite
    # the report artifact mid-delete); a stale one doesn't.
    report_id = _mint_report(cfg, status="complete", agg_status="running")
    assert app.route(
        _event("DELETE", f"/benchmark/sales/orders/runs/{report_id}"), cfg
    )["statusCode"] == 409

    stale_id = _mint_report(
        cfg,
        status="complete",
        agg_status="running",
        updated_at="2020-01-01T00:00:00+00:00",
    )
    assert app.route(
        _event("DELETE", f"/benchmark/sales/orders/runs/{stale_id}"), cfg
    )["statusCode"] == 200


def test_delete_report_purges_all_versions(cfg):
    # The bundle bucket is versioned: a plain delete only stacks a delete
    # marker, leaving the gold-carrying report readable as a noncurrent
    # version. Deletion means purge — every version and marker goes.
    cfg.s3.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    done_id = _mint_report(cfg, status="complete", report_doc={"v": 1})
    cfg.s3.put_object(  # a second version of the report document
        Bucket=BUCKET,
        Key=br.report_key("sales", "orders", done_id),
        Body=b'{"v": 2}',
    )
    resp = app.route(
        _event("DELETE", f"/benchmark/sales/orders/runs/{done_id}"), cfg
    )
    assert resp["statusCode"] == 200
    versions = cfg.s3.list_object_versions(
        Bucket=BUCKET, Prefix=br.report_prefix("sales", "orders", done_id)
    )
    assert not versions.get("Versions") and not versions.get("DeleteMarkers")


def test_delete_dataset_purges_benchmark_versions(cfg):
    cfg.s3.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    _mint_report(cfg, status="complete", report_doc={"x": 1})
    handlers.delete_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        s3=cfg.s3,
        bundle_bucket=BUCKET,
    )
    versions = cfg.s3.list_object_versions(
        Bucket=BUCKET, Prefix="benchmark/sales/orders/"
    )
    # questions.csv + report.json: no versions, no delete markers left behind.
    assert not versions.get("Versions") and not versions.get("DeleteMarkers")


def test_delete_dataset_purges_benchmark_state(cfg):
    # Dataset deletion must take the gold-carrying benchmark/ prefix and the
    # REPORT# rows with it — re-registering the same names must NOT resurrect
    # the previous owner's reports or answer key.
    report_id = _mint_report(cfg, status="complete", report_doc={"report_id": "x"})
    res = handlers.delete_domain_mapping(
        cfg.ddb,
        registry_table=REGISTRY,
        data_domain="sales",
        dataset="orders",
        s3=cfg.s3,
        bundle_bucket=BUCKET,
    )
    assert res["purged_report_rows"] == 1
    listed = cfg.s3.list_objects_v2(Bucket=BUCKET, Prefix="benchmark/sales/orders/")
    assert listed.get("KeyCount", 0) == 0  # questions.csv + report.json gone

    _dataset(cfg)  # re-register the same names
    resp = app.route(_event("GET", "/benchmark/sales/orders/runs"), cfg)
    assert json.loads(resp["body"])["reports"] == []
    assert app.route(
        _event("GET", f"/benchmark/sales/orders/runs/{report_id}"), cfg
    )["statusCode"] == 404


# -- aggregate -----------------------------------------------------------------


def test_aggregate_requires_complete_report_and_flips_agg_status(cfg):
    running_id = _mint_report(cfg, status="running")
    assert app.route(
        _event("POST", f"/benchmark/sales/orders/runs/{running_id}/aggregate"), cfg
    )["statusCode"] == 409

    done_id = _mint_report(cfg, status="complete")
    n_before = len(cfg.agentcore.calls)
    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/runs/{done_id}/aggregate"), cfg
    )
    assert resp["statusCode"] == 200
    payload = json.loads(cfg.agentcore.calls[-1]["payload"])
    assert payload["mode"] == "aggregate_annotations"
    assert payload["report_id"] == done_id
    assert len(cfg.agentcore.calls) == n_before + 1
    row = json.loads(
        app.route(_event("GET", f"/benchmark/sales/orders/runs/{done_id}"), cfg)["body"]
    )["row"]
    assert row["agg_status"] == "running"

    # A second aggregate while one runs → 409.
    assert app.route(
        _event("POST", f"/benchmark/sales/orders/runs/{done_id}/aggregate"), cfg
    )["statusCode"] == 409


def test_aggregate_stale_running_can_be_retried(cfg):
    # A dead aggregator (killed container, lost terminal write) leaves
    # agg_status=running forever; past the heartbeat cutoff it must be
    # retryable, mirroring the delete route's stale escape.
    report_id = _mint_report(
        cfg, status="complete", agg_status="running",
        updated_at="2020-01-01T00:00:00+00:00",
    )
    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/runs/{report_id}/aggregate"), cfg
    )
    assert resp["statusCode"] == 200
    assert json.loads(cfg.agentcore.calls[-1]["payload"])["mode"] == (
        "aggregate_annotations"
    )


def test_aggregate_flip_is_conditional_against_the_race(cfg, monkeypatch):
    # Two concurrent POSTs race the read-side check; the UpdateItem itself
    # re-asserts "not already running unless stale". Simulate the lost race by
    # making the read-side check say GO while the row is fresh-running.
    report_id = _mint_report(cfg, status="complete", agg_status="running")
    monkeypatch.setattr(handlers, "_report_row_is_stale", lambda item: True)
    n_before = len(cfg.agentcore.calls)
    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/runs/{report_id}/aggregate"), cfg
    )
    assert resp["statusCode"] == 409
    assert len(cfg.agentcore.calls) == n_before  # no second aggregator started


def test_aggregate_clears_stale_agg_detail_and_writes_it_on_reject(cfg):
    report_id = _mint_report(
        cfg, status="complete", agg_status="failed", agg_detail="old failure"
    )
    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/runs/{report_id}/aggregate"), cfg
    )
    assert resp["statusCode"] == 200
    row = json.loads(
        app.route(_event("GET", f"/benchmark/sales/orders/runs/{report_id}"), cfg)["body"]
    )["row"]
    assert row["agg_status"] == "running" and row["agg_detail"] == ""

    # A runtime-rejected ack reverts to failed with the reason on agg_detail.
    stale = _mint_report(
        cfg, status="complete", agg_status="idle",
    )
    cfg.agentcore.ack = {"status": "rejected", "error": "invalid report_id"}
    resp = app.route(
        _event("POST", f"/benchmark/sales/orders/runs/{stale}/aggregate"), cfg
    )
    cfg.agentcore.ack = None
    assert resp["statusCode"] == 502
    row = json.loads(
        app.route(_event("GET", f"/benchmark/sales/orders/runs/{stale}"), cfg)["body"]
    )["row"]
    assert row["agg_status"] == "failed"
    assert "runtime rejected" in row["agg_detail"]


# -- apply annotations ------------------------------------------------------------


def test_apply_annotations_batch_creates_with_benchmark_provenance(cfg):
    report_id = _mint_report(cfg, status="complete")
    resp = app.route(
        _event(
            "POST",
            f"/benchmark/sales/orders/runs/{report_id}/annotations",
            body={"annotations": [
                {"note": "document the status int code", "concept_id": "tables/results"},
                {"note": "state that pit stops are not tracked"},
            ]},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200
    out = json.loads(resp["body"])
    assert out["count"] == 2
    assert all(a["submitted_via"] == "benchmark" for a in out["created"])
    # No concept target → the dataset-wide sentinel.
    assert {a["concept_id"] for a in out["created"]} == {"tables/results", "_dataset"}
    # The items landed in the annotations table under the CALLER's sub.
    from okf_core import annotations as anno

    items = cfg.ddb.query(
        TableName=ANNOTATIONS,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": anno.annotation_pk("sales", "orders", "u1")}},
    )["Items"]
    assert len(items) == 2
    assert all(i["status"]["S"] == "open" for i in items)


def test_apply_annotations_validates_report_and_stamps_provenance(cfg):
    report_id = _mint_report(cfg, status="complete")
    resp = app.route(
        _event(
            "POST",
            f"/benchmark/sales/orders/runs/{report_id}/annotations",
            body={"annotations": [{"note": "document the join key"}]},
        ),
        cfg,
    )
    assert resp["statusCode"] == 200
    created = json.loads(resp["body"])["created"]
    assert created[0]["report_id"] == report_id  # provenance stamp

    # A nonexistent report row must not mint annotations (stale/foreign id).
    missing = "r20990101t000000-deadbeef"
    resp = app.route(
        _event(
            "POST",
            f"/benchmark/sales/orders/runs/{missing}/annotations",
            body={"annotations": [{"note": "a note"}]},
        ),
        cfg,
    )
    assert resp["statusCode"] == 404
    # An invalid id shape is a 400.
    resp = app.route(
        _event(
            "POST",
            "/benchmark/sales/orders/runs/NOT_VALID/annotations",
            body={"annotations": [{"note": "a note"}]},
        ),
        cfg,
    )
    assert resp["statusCode"] == 400


def test_apply_annotations_validates_body(cfg):
    report_id = _mint_report(cfg, status="complete")
    path = f"/benchmark/sales/orders/runs/{report_id}/annotations"
    assert app.route(_event("POST", path, body={}), cfg)["statusCode"] == 400
    assert app.route(
        _event("POST", path, body={"annotations": [{"note": ""}]}), cfg
    )["statusCode"] == 400
    too_many = {"annotations": [{"note": f"n{i}"} for i in range(51)]}
    assert app.route(_event("POST", path, body=too_many), cfg)["statusCode"] == 400


def test_apply_annotations_all_or_nothing_on_bad_entry(cfg):
    # Validation runs over the WHOLE batch before anything is written: a bad
    # concept_id later in the list must not leave earlier annotations silently
    # committed (a retry would then file duplicates).
    report_id = _mint_report(cfg, status="complete")
    path = f"/benchmark/sales/orders/runs/{report_id}/annotations"
    resp = app.route(
        _event(
            "POST",
            path,
            body={"annotations": [
                {"note": "a valid note", "concept_id": "tables/results"},
                {"note": "traversal", "concept_id": "tables/../tables/results"},
            ]},
        ),
        cfg,
    )
    assert resp["statusCode"] == 400
    from okf_core import annotations as anno

    items = cfg.ddb.query(
        TableName=ANNOTATIONS,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={
            ":pk": {"S": anno.annotation_pk("sales", "orders", "u1")}
        },
    )["Items"]
    assert items == []  # nothing committed


def test_start_run_threads_behavior_live_sql(cfg):
    _dataset(cfg)
    _upload_csv(cfg)
    # Default: OFF — absent from both the payload and the row.
    resp = _start(cfg, {"checks": ["behavior"]})
    assert resp["statusCode"] == 200
    payload = json.loads(cfg.agentcore.calls[-1]["payload"])
    assert "behavior_live_sql" not in payload
    report_id = json.loads(resp["body"])["report_id"]
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": br.report_sk(report_id)}},
    )["Item"]
    assert "behavior_live_sql" not in item

    # ON: rides the payload AND the row's config summary (the list's badge +
    # the comparability marker).
    resp = _start(cfg, {"checks": ["behavior"], "behavior_live_sql": True})
    assert resp["statusCode"] == 200
    payload = json.loads(cfg.agentcore.calls[-1]["payload"])
    assert payload["behavior_live_sql"] is True
    report_id = json.loads(resp["body"])["report_id"]
    item = cfg.ddb.get_item(
        TableName=REGISTRY,
        Key={"pk": {"S": "HARVEST#sales#orders"}, "sk": {"S": br.report_sk(report_id)}},
    )["Item"]
    assert item["behavior_live_sql"]["BOOL"] is True
