"""The Attested Computation endpoints behind the UI's Run modal and Verify
screen: list/get, run (gated execution), and the off-mount verification
overlay flips (verify writes an entry; unverify writes a REVOKED tombstone)."""

from __future__ import annotations

import json

import pytest

from control_api import handlers
from control_api.handlers import ApiError

from tests.conftest import BUCKET, REGISTRY

DOMAIN, DATASET = "sales", "orders"
_PREFIX = f"okf/{DOMAIN}/{DATASET}/"

_COMP_DOC = (
    "---\n"
    "type: Attested Computation\n"
    "title: Revenue for a region\n"
    "description: Recognized revenue for one region.\n"
    "runtime: athena\n"
    "parameters:\n"
    '  - {name: region, type: string, required: true, example: "EMEA",\n'
    "     enum: [EMEA, NA]}\n"
    "verified: null\n"
    "verified_by: null\n"
    "timestamp: t\n"
    "---\n\n"
    "Reads orders.\n\n"
    "# Computation\n\n"
    "```sql\nSELECT SUM(amount) AS revenue FROM orders WHERE region = @region\n```\n"
)


def _seed(s3, ddb=None) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{_PREFIX}references/computations/revenue_by_region.md",
        Body=_COMP_DOC.encode(),
    )
    if ddb is not None:
        ddb.put_item(
            TableName=REGISTRY,
            Item={
                "pk": {"S": f"DOMAIN#{DOMAIN}"},
                "sk": {"S": f"DATASET#{DATASET}"},
                "glue_database": {"S": "shop"},
            },
        )


class FakeAthena:
    def __init__(self):
        self.started = []

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}, "Statistics": {}}}

    def get_query_results(self, **kwargs):
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Label": "revenue"}]},
                "Rows": [
                    {"Data": [{"VarCharValue": "revenue"}]},
                    {"Data": [{"VarCharValue": "42.5"}]},
                ],
            }
        }


def test_list_and_get(cfg):
    _seed(cfg.s3)
    out = handlers.list_bundle_computations(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert [c["computation"] for c in out["computations"]] == ["revenue_by_region"]
    assert out["computations"][0]["verification"] == "unverified"
    one = handlers.get_bundle_computation(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        slug="revenue_by_region",
    )
    assert "@region" in one["sql"]
    assert one["path"] == "references/computations/revenue_by_region.md"
    with pytest.raises(ApiError) as ei:
        handlers.get_bundle_computation(
            cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET, slug="nope"
        )
    assert ei.value.status == 404


def test_run_disabled_returns_rendered_receipt(cfg):
    _seed(cfg.s3, cfg.ddb)
    out = handlers.run_bundle_computation(
        cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
        data_domain=DOMAIN, dataset=DATASET, slug="revenue_by_region",
        values={"region": "EMEA"}, enabled=False,
    )
    assert out["executed"] is False and "not enabled" in out["note"]
    assert "region = 'EMEA'" in out["executed_sql"]


def test_run_executes_and_refuses_constraint_violations(cfg):
    _seed(cfg.s3, cfg.ddb)
    athena = FakeAthena()
    out = handlers.run_bundle_computation(
        cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
        data_domain=DOMAIN, dataset=DATASET, slug="revenue_by_region",
        values={"region": "EMEA"}, athena=athena, enabled=True,
    )
    assert out["executed"] is True and out["rows"] == [["42.5"]]
    assert athena.started[0]["QueryExecutionContext"]["Database"] == "shop"
    # Declared enum is CONTRACT — a violation is a 400, the query never runs.
    with pytest.raises(ApiError) as ei:
        handlers.run_bundle_computation(
            cfg.s3, cfg.ddb, bucket=BUCKET, registry_table=REGISTRY,
            data_domain=DOMAIN, dataset=DATASET, slug="revenue_by_region",
            values={"region": "MOON"}, athena=athena, enabled=True,
        )
    assert ei.value.status == 400 and "declared values" in str(ei.value)
    assert len(athena.started) == 1


def test_verify_unverify_round_trip_via_overlay(cfg):
    _seed(cfg.s3)
    out = handlers.verify_computation(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        slug="revenue_by_region", verified_by="analyst@example.com",
    )
    assert out["verification"] == "verified"
    assert out["verified_by"] == "analyst@example.com"
    # The flip landed OFF-MOUNT — the doc object is untouched (the mount
    # stays the bundle tree's sole writer).
    doc = cfg.s3.get_object(
        Bucket=BUCKET, Key=f"{_PREFIX}references/computations/revenue_by_region.md"
    )["Body"].read().decode()
    assert doc == _COMP_DOC
    overlay = json.loads(
        cfg.s3.get_object(
            Bucket=BUCKET, Key=f"verification/{DOMAIN}/{DATASET}.json"
        )["Body"].read()
    )
    entry = overlay["entries"]["revenue_by_region"]
    assert entry["verified_by"] == "analyst@example.com"
    assert entry["sha256"] == out["sha256"]
    # Serving now reads verified.
    listed = handlers.list_bundle_computations(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert listed["computations"][0]["verification"] == "verified"
    # Unverify writes a tombstone (the doc may carry a folded stamp later).
    out = handlers.unverify_computation(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        slug="revenue_by_region", revoked_by="analyst@example.com",
    )
    assert out["verification"] == "unverified"
    listed = handlers.list_bundle_computations(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert listed["computations"][0]["verification"] == "unverified"


def test_verify_requires_identity_and_existing_doc(cfg):
    _seed(cfg.s3)
    with pytest.raises(ApiError) as ei:
        handlers.verify_computation(
            cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
            slug="revenue_by_region", verified_by="",
        )
    assert ei.value.status == 401
    with pytest.raises(ApiError) as ei:
        handlers.verify_computation(
            cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
            slug="ghost", verified_by="analyst@example.com",
        )
    assert ei.value.status == 404


def test_stale_after_edit_then_reverify(cfg):
    _seed(cfg.s3)
    handlers.verify_computation(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        slug="revenue_by_region", verified_by="analyst@example.com",
    )
    # The doc changes after the click (a maintenance run edited the fence):
    # the signed hash no longer matches -> STALE, surfaced, never hidden.
    edited = _COMP_DOC.replace("SUM(amount)", "SUM(amount * 1.0)")
    cfg.s3.put_object(
        Bucket=BUCKET,
        Key=f"{_PREFIX}references/computations/revenue_by_region.md",
        Body=edited.encode(),
    )
    listed = handlers.list_bundle_computations(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert listed["computations"][0]["verification"] == "stale"
    # Re-verifying signs the NEW hash.
    out = handlers.verify_computation(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        slug="revenue_by_region", verified_by="analyst@example.com",
    )
    assert out["verification"] == "verified"


def test_verify_refuses_when_doc_changed_since_review(cfg):
    # The click signs the hash the human REVIEWED: a writer racing the click
    # must produce a 409, never a stamp over unseen content.
    from okf_core.computations import parse_computation_text

    _seed(cfg.s3)
    comp, _ = parse_computation_text(
        "references/computations/revenue_by_region.md", _COMP_DOC
    )
    with pytest.raises(ApiError) as ei:
        handlers.verify_computation(
            cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
            slug="revenue_by_region", verified_by="a@x",
            expected_sha256="b" * 64,
        )
    assert ei.value.status == 409 and "changed since" in str(ei.value)
    # The reviewed hash verifies; a legacy call with no expectation still works.
    out = handlers.verify_computation(
        cfg.s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        slug="revenue_by_region", verified_by="a@x",
        expected_sha256=comp.sha256,
    )
    assert out["verification"] == "verified"
