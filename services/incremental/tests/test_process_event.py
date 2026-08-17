"""Unit tests for the pure process_event orchestration."""

from __future__ import annotations

import json

from incremental import store
from incremental.handler import process_event
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


def _call(detail, aws, glue, agentcore):
    import boto3

    return process_event(
        detail,
        glue=glue,
        ddb=aws["ddb"],
        s3=aws["s3"],
        agentcore=agentcore,
        bundle_bucket=BUNDLE_BUCKET,
        registry_table=REGISTRY_TABLE,
        freshness_table=FRESHNESS_TABLE,
        harvest_runtime_arn=ARN,
        # The build-lock gate speaks typed attributes — a LOW-LEVEL client,
        # like production's clients["ddb_client"] (never resource.meta.client).
        ddb_client=boto3.client("dynamodb", region_name="us-east-1"),
    )


def test_unmapped_database_is_skipped(aws):
    # No registry mapping seeded.
    glue = FakeGlue(
        {"some_db": {"races": [make_table("races", [col("id", "bigint")])]}}
    )
    agentcore = FakeAgentCore()
    detail = {"databaseName": "some_db", "tableName": "races", "changedPartitions": []}

    result = _call(detail, aws, glue, agentcore)

    assert result["action"] == "skipped_unmapped"
    assert agentcore.invocations == []


def test_mapped_but_no_bundle_is_skipped(aws):
    # Mapping exists, but NO full harvest has authored the bundle (no state.json
    # marker). The incremental path must skip WITHOUT staging pending.json — a raw
    # put_object there would pollute the S3 Files tree and wedge the full harvest.
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    tbl = make_table("races", [col("id", "bigint")], version_id="1")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})
    agentcore = FakeAgentCore()
    detail = {"databaseName": "f1_db", "tableName": "races", "changedPartitions": []}

    result = _call(detail, aws, glue, agentcore)

    assert result["action"] == "skipped_no_bundle"
    assert agentcore.invocations == []
    # nothing staged under the bundle prefix
    listing = aws["s3"].list_objects_v2(Bucket=BUNDLE_BUCKET, Prefix="okf/sales/f1/")
    assert listing.get("KeyCount", 0) == 0


def test_unchanged_version_is_skipped(aws):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("races", [col("id", "bigint")], version_id="3")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})
    agentcore = FakeAgentCore()

    # Freshness already records version 3.
    store.put_stored_version(
        aws["ddb"],
        FRESHNESS_TABLE,
        "sales",
        "f1",
        "races",
        version_id="3",
        update_time=tbl["UpdateTime"],
    )

    detail = {"databaseName": "f1_db", "tableName": "races", "changedPartitions": []}
    result = _call(detail, aws, glue, agentcore)

    assert result["action"] == "skipped_unchanged"
    assert agentcore.invocations == []


def test_version_bump_computes_diff_invokes_and_updates_version(aws):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")

    v1 = make_table("races", [col("id", "bigint"), col("old", "int")], version_id="1")
    v2 = make_table(
        "races",
        [col("id", "bigint"), col("name", "string")],
        version_id="2",
        update_time="2026-07-01T00:00:00+00:00",
    )
    glue = FakeGlue({"f1_db": {"races": [v1, v2]}})  # oldest-first history
    agentcore = FakeAgentCore()

    # Freshness records the OLD version (1) -> a real change.
    store.put_stored_version(
        aws["ddb"],
        FRESHNESS_TABLE,
        "sales",
        "f1",
        "races",
        version_id="1",
        update_time=v1["UpdateTime"],
    )

    detail = {
        "databaseName": "f1_db",
        "tableName": "races",
        "typeOfChange": "UpdateTable",
        "changedPartitions": [],
    }
    result = _call(detail, aws, glue, agentcore)

    assert result["action"] == "invoked"
    # Diff: 'name' added, 'old' removed.
    assert [c["name"] for c in result["diff"]["added"]] == ["name"]
    assert [c["name"] for c in result["diff"]["removed"]] == ["old"]

    # AgentCore invoked with the correct payload + session id.
    assert len(agentcore.invocations) == 1
    inv = agentcore.invocations[0]
    assert inv["agentRuntimeArn"] == ARN
    assert inv["runtimeSessionId"] == runtime_session_id("sales", "f1")
    assert 33 <= len(inv["runtimeSessionId"]) <= 256
    payload = json.loads(inv["payload"].decode())
    assert payload["mode"] == "incremental"
    assert payload["data_domain"] == "sales"
    assert payload["dataset"] == "f1"
    assert payload["changed_table"] == "races"
    assert payload["diff"] == result["diff"]
    # This path is Glue-only: the source descriptor names the emitting database.
    assert payload["source"] == {"type": "glue", "glue_database": "f1_db"}

    # Version row updated to the new version id.
    stored = store.get_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "races"
    )
    assert stored.version_id == "2"
    assert stored.update_time == "2026-07-01T00:00:00+00:00"

    # pending.json is NOT staged in S3: the diff rides in the invoke payload and
    # the harvest runtime writes .harvest/pending.json through the S3 Files mount
    # itself. A raw put_object here would materialize .harvest/ owned by root and
    # wedge the runtime's uid-1000 mount writes (EACCES).
    import botocore.exceptions

    try:
        aws["s3"].get_object(
            Bucket=BUNDLE_BUCKET, Key="okf/sales/f1/.harvest/pending.json"
        )
        staged = True
    except botocore.exceptions.ClientError:
        staged = False
    assert staged is False

    # Harvest status row set to queued/incremental.
    status = (
        aws["ddb"]
        .Table(REGISTRY_TABLE)
        .get_item(Key={"pk": "HARVEST#sales#f1", "sk": "STATUS"})["Item"]
    )
    assert status["status"] == "queued"
    assert status["mode"] == "incremental"
    assert status["runtime_session_id"] == runtime_session_id("sales", "f1")


def test_partition_only_change_still_invokes(aws):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("races", [col("id", "bigint")], version_id="5")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})
    agentcore = FakeAgentCore()

    # Same version stored -> version unchanged, but partitions changed.
    store.put_stored_version(
        aws["ddb"],
        FRESHNESS_TABLE,
        "sales",
        "f1",
        "races",
        version_id="5",
        update_time=tbl["UpdateTime"],
    )

    detail = {
        "databaseName": "f1_db",
        "tableName": "races",
        "typeOfChange": "BatchCreatePartition",
        "changedPartitions": ["year=2026/round=11"],
    }
    result = _call(detail, aws, glue, agentcore)

    assert result["action"] == "invoked"
    # Version unchanged -> empty column diff, but still re-reviewed.
    assert result["diff"] == {"added": [], "removed": [], "retyped": []}
    assert len(agentcore.invocations) == 1
    payload = json.loads(agentcore.invocations[0]["payload"].decode())
    assert payload["mode"] == "incremental"


def test_first_time_table_is_invoked(aws):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("races", [col("id", "bigint")], version_id="1")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})
    agentcore = FakeAgentCore()

    # No stored version -> real (first) change.
    detail = {"databaseName": "f1_db", "tableName": "races", "changedPartitions": []}
    result = _call(detail, aws, glue, agentcore)

    assert result["action"] == "invoked"
    assert [c["name"] for c in result["diff"]["added"]] == ["id"]
    assert len(agentcore.invocations) == 1


def test_dropped_table_is_skipped(aws):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    glue = FakeGlue({"f1_db": {}})  # database exists but table is gone
    agentcore = FakeAgentCore()

    detail = {"databaseName": "f1_db", "tableName": "gone", "changedPartitions": []}
    result = _call(detail, aws, glue, agentcore)

    assert result["action"] == "skipped_no_table"
    assert agentcore.invocations == []


def test_missing_db_or_table_field_skipped(aws):
    glue = FakeGlue({})
    agentcore = FakeAgentCore()
    result = _call({"tableName": "races"}, aws, glue, agentcore)
    assert result["action"] == "skipped_unmapped"
    assert agentcore.invocations == []


# --- per-dataset harvest lease (no collision with an in-flight harvest) ------


def _detail(db="f1_db", table="races"):
    return {"databaseName": db, "tableName": table, "changedPartitions": []}


def test_incremental_defers_while_guardrails_author_runs(aws):
    """A fresh `building` flip on the mapping row defers the re-harvest.

    The harvest status row is terminal while the follow-on policy author
    reads the committed wiki, so the lease alone can't see it. Same
    no-version-recorded semantics as skipped_locked.
    """
    from datetime import datetime, timezone

    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("races", [col("id", "bigint")], version_id="7")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})
    agentcore = FakeAgentCore()

    aws["ddb"].Table(REGISTRY_TABLE).update_item(
        Key={"pk": "DOMAIN#sales", "sk": "DATASET#f1"},
        UpdateExpression="SET ar_build_status = :b, ar_build_started_at = :t",
        ExpressionAttributeValues={
            ":b": "building",
            ":t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )

    result = _call(_detail(), aws, glue, agentcore)
    assert result["action"] == "skipped_guardrails_building"
    assert agentcore.invocations == []

    # Version NOT recorded -> the change survives for re-detection.
    stored = store.get_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "races"
    )
    assert stored.version_id is None

    # A STALE building row (dead author) no longer defers.
    old = "2020-01-01T00:00:00+00:00"
    aws["ddb"].Table(REGISTRY_TABLE).update_item(
        Key={"pk": "DOMAIN#sales", "sk": "DATASET#f1"},
        UpdateExpression="SET ar_build_started_at = :t",
        ExpressionAttributeValues={":t": old},
    )
    result = _call(_detail(), aws, glue, agentcore)
    assert result["action"] != "skipped_guardrails_building"


def test_incremental_defers_when_harvest_already_in_flight(aws):
    """A running full harvest holds the lease; the incremental event defers.

    It must NOT invoke a colliding second harvest and must NOT record the new
    version (so the change is re-detected once the running harvest finishes).
    """
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("races", [col("id", "bigint")], version_id="7")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})
    agentcore = FakeAgentCore()

    # Simulate a full harvest currently running (holds the lease, fresh).
    from datetime import datetime, timezone

    aws["ddb"].Table(REGISTRY_TABLE).put_item(
        Item={
            "pk": "HARVEST#sales#f1",
            "sk": "STATUS",
            "status": "running",
            "mode": "full",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    result = _call(_detail(), aws, glue, agentcore)
    assert result["action"] == "skipped_locked"
    assert agentcore.invocations == []  # no colliding invoke

    # Version NOT recorded -> the change survives for re-detection.
    stored = store.get_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "races"
    )
    assert stored.version_id is None

    # No pending.json staged (we bailed before staging).
    import botocore.exceptions

    try:
        aws["s3"].get_object(
            Bucket=BUNDLE_BUCKET, Key="okf/sales/f1/.harvest/pending.json"
        )
        staged = True
    except botocore.exceptions.ClientError:
        staged = False
    assert staged is False


def test_incremental_takes_over_a_stale_lease(aws):
    """A lease older than the 8h AgentCore session cap is dead -> taken over."""
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("races", [col("id", "bigint")], version_id="7")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})
    agentcore = FakeAgentCore()

    from datetime import datetime, timezone, timedelta

    stale = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    aws["ddb"].Table(REGISTRY_TABLE).put_item(
        Item={
            "pk": "HARVEST#sales#f1",
            "sk": "STATUS",
            "status": "running",  # but started 9h ago -> dead
            "mode": "full",
            "started_at": stale,
        }
    )

    result = _call(_detail(), aws, glue, agentcore)
    assert result["action"] == "invoked"
    assert len(agentcore.invocations) == 1


def test_incremental_invoke_failure_releases_lease_and_keeps_version(aws):
    """A failed invoke marks the row failed and does NOT record the version."""
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    tbl = make_table("races", [col("id", "bigint")], version_id="7")
    glue = FakeGlue({"f1_db": {"races": [tbl]}})

    class BoomAgentCore:
        def __init__(self):
            self.invocations = []

        def invoke_agent_runtime(self, **kwargs):
            self.invocations.append(kwargs)
            raise RuntimeError("runtime unavailable")

    boom = BoomAgentCore()
    import pytest

    with pytest.raises(RuntimeError):
        _call(_detail(), aws, glue, boom)

    # Version not recorded (change survives), lease released to 'failed'.
    stored = store.get_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "races"
    )
    assert stored.version_id is None
    status = (
        aws["ddb"]
        .Table(REGISTRY_TABLE)
        .get_item(Key={"pk": "HARVEST#sales#f1", "sk": "STATUS"})["Item"]
    )
    assert status["status"] == "failed"


# ---------------------------------------------------------------------------
# Iceberg data commits — absorbed, not re-harvested (step 3b)
# ---------------------------------------------------------------------------

ICEBERG = {"table_type": "ICEBERG", "metadata_location": "s3://b/metadata/v2.json"}


def _seed_iceberg_dataset(aws, history):
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    return FakeGlue({"f1_db": {"orders": history}})


def test_iceberg_data_commit_is_absorbed_without_invoke(aws):
    """Version +1, empty column diff, Iceberg -> record version, no invoke."""
    cols = [col("id", "bigint"), col("amount", "double")]
    v1 = make_table("orders", cols, version_id="1", parameters=ICEBERG)
    v2 = make_table(
        "orders", cols, version_id="2",
        update_time="2026-07-01T00:00:00+00:00", parameters=ICEBERG,
    )
    glue = _seed_iceberg_dataset(aws, [v1, v2])
    agentcore = FakeAgentCore()
    store.put_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "orders",
        version_id="1", update_time=v1["UpdateTime"],
    )

    result = _call(_detail(table="orders"), aws, glue, agentcore)

    assert result["action"] == "skipped_iceberg_commit"
    assert agentcore.invocations == []
    # Version recorded, so the nightly reconcile won't re-detect this commit.
    stored = store.get_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "orders"
    )
    assert stored.version_id == "2"


def test_iceberg_schema_change_still_invokes(aws):
    v1 = make_table(
        "orders", [col("id", "bigint")], version_id="1", parameters=ICEBERG
    )
    v2 = make_table(
        "orders", [col("id", "bigint"), col("status", "string")],
        version_id="2", parameters=ICEBERG,
    )
    glue = _seed_iceberg_dataset(aws, [v1, v2])
    agentcore = FakeAgentCore()
    store.put_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "orders",
        version_id="1", update_time=v1["UpdateTime"],
    )

    result = _call(_detail(table="orders"), aws, glue, agentcore)

    assert result["action"] == "invoked"
    assert result["diff"]["added"] and len(agentcore.invocations) == 1


def test_iceberg_version_gap_is_conservative_and_invokes(aws):
    """stored=1, current=3: the two-latest diff can't see v1->v2, so a dropped
    schema-change event could hide behind an empty diff — must invoke."""
    cols = [col("id", "bigint")]
    history = [
        make_table("orders", cols, version_id=str(v), parameters=ICEBERG)
        for v in (1, 2, 3)
    ]
    glue = _seed_iceberg_dataset(aws, history)
    agentcore = FakeAgentCore()
    store.put_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "orders",
        version_id="1", update_time=history[0]["UpdateTime"],
    )

    result = _call(_detail(table="orders"), aws, glue, agentcore)

    assert result["action"] == "invoked"
    assert len(agentcore.invocations) == 1


def test_iceberg_never_seen_table_invokes(aws):
    """No stored version -> the gap is unknowable -> invoke (old behavior)."""
    v2 = make_table(
        "orders", [col("id", "bigint")], version_id="2", parameters=ICEBERG
    )
    glue = _seed_iceberg_dataset(aws, [v2])
    agentcore = FakeAgentCore()

    result = _call(_detail(table="orders"), aws, glue, agentcore)

    assert result["action"] == "invoked"
    assert len(agentcore.invocations) == 1


def test_iceberg_commit_review_override_invokes(aws, monkeypatch):
    monkeypatch.setenv("OKF_INCREMENTAL_ICEBERG_COMMITS", "review")
    cols = [col("id", "bigint")]
    v1 = make_table("orders", cols, version_id="1", parameters=ICEBERG)
    v2 = make_table("orders", cols, version_id="2", parameters=ICEBERG)
    glue = _seed_iceberg_dataset(aws, [v1, v2])
    agentcore = FakeAgentCore()
    store.put_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "orders",
        version_id="1", update_time=v1["UpdateTime"],
    )

    result = _call(_detail(table="orders"), aws, glue, agentcore)

    assert result["action"] == "invoked"
    assert len(agentcore.invocations) == 1


def test_non_iceberg_empty_diff_bump_still_invokes(aws):
    """Hive semantics untouched: a property-only version bump re-reviews."""
    cols = [col("id", "bigint")]
    v1 = make_table("races", cols, version_id="1")
    v2 = make_table("races", cols, version_id="2")
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    glue = FakeGlue({"f1_db": {"races": [v1, v2]}})
    agentcore = FakeAgentCore()
    store.put_stored_version(
        aws["ddb"], FRESHNESS_TABLE, "sales", "f1", "races",
        version_id="1", update_time=v1["UpdateTime"],
    )

    result = _call(_detail(), aws, glue, agentcore)

    assert result["action"] == "invoked"
    assert len(agentcore.invocations) == 1
