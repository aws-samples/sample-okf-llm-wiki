"""Tests for the nightly reconcile drift-detection pass."""

from __future__ import annotations

from incremental import store
from incremental.reconcile import reconcile
from okf_core.session import runtime_session_id
from conftest import (
    BUNDLE_BUCKET,
    FRESHNESS_TABLE,
    REGISTRY_TABLE,
    seed_mapping,
    seed_ready_bundle,
)
from fakes import FakeAgentCore, FakeGlue, col, make_table

ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/harvest-abc"


def _reconcile(aws, glue, agentcore):
    return reconcile(
        glue=glue,
        ddb=aws["ddb"],
        s3=aws["s3"],
        agentcore=agentcore,
        bundle_bucket=BUNDLE_BUCKET,
        registry_table=REGISTRY_TABLE,
        freshness_table=FRESHNESS_TABLE,
        harvest_runtime_arn=ARN,
    )


def test_reconcile_enqueues_drifted_tables_only(aws):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")

    races = make_table("races", [col("id", "bigint")], version_id="2")
    results = make_table("results", [col("rid", "bigint")], version_id="1")
    glue = FakeGlue({"f1_db": {"races": [races], "results": [results]}})
    agentcore = FakeAgentCore()

    # races is up to date (v2), results drifted (stored v0 != current v1).
    store.put_stored_version(
        aws["ddb"],
        FRESHNESS_TABLE,
        "sales",
        "f1",
        "races",
        version_id="2",
        update_time=races["UpdateTime"],
    )
    store.put_stored_version(
        aws["ddb"],
        FRESHNESS_TABLE,
        "sales",
        "f1",
        "results",
        version_id="0",
        update_time="2026-01-01T00:00:00+00:00",
    )

    summary = _reconcile(aws, glue, agentcore)

    assert summary["scanned_datasets"] == 1
    assert summary["scanned_tables"] == 2
    assert summary["enqueued"] == 1
    assert summary["errors"] == 0
    assert summary["drifted"] == [{"database": "f1_db", "table": "results"}]
    assert len(agentcore.invocations) == 1
    assert agentcore.invocations[0]["runtimeSessionId"] == runtime_session_id(
        "sales", "f1"
    )

    # results version row updated to current.
    stored = store.get_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "results"
    )
    assert stored.version_id == "1"


def test_reconcile_catches_never_seen_table(aws):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("brand_new", [col("id", "bigint")], version_id="1")
    glue = FakeGlue({"f1_db": {"brand_new": [tbl]}})
    agentcore = FakeAgentCore()

    summary = _reconcile(aws, glue, agentcore)

    assert summary["enqueued"] == 1
    assert summary["drifted"] == [{"database": "f1_db", "table": "brand_new"}]


def test_reconcile_no_drift_no_invoke(aws):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("races", [col("id", "bigint")], version_id="7")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})
    agentcore = FakeAgentCore()

    store.put_stored_version(
        aws["ddb"],
        FRESHNESS_TABLE,
        "sales",
        "f1",
        "races",
        version_id="7",
        update_time=tbl["UpdateTime"],
    )

    summary = _reconcile(aws, glue, agentcore)
    assert summary["enqueued"] == 0
    assert agentcore.invocations == []
