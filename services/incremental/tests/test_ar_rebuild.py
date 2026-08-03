"""The policy rebuild authority: dispatch decisions, the reaper, the nightly pass.

v2 (LLM-judge era): the Lambda makes only deterministic decisions — authoring
runs on the harvest runtime via ``mode="ar_rules"`` dispatches with FRESH
session ids (an AgentCore session is pinned to the runtime version it started
on — live-observed trap). S3/DynamoDB run on moto; AgentCore is a hand fake.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from conftest import BUNDLE_BUCKET, REGISTRY_TABLE, seed_mapping, seed_ready_bundle
from fakes import FakeAgentCore
from incremental import ar_rebuild
from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    BUILD_READY,
    BUILD_STALE,
    policy_doc_key,
    put_policy_doc,
    registry_key,
    source_hash,
)

DOMAIN = "sales"
DATASET = "orders"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:::rt"


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("OKF_POLICY_BUILD_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUNDLE_BUCKET)
        ddb_resource = boto3.resource("dynamodb", region_name="us-east-1")
        ddb_resource.create_table(
            TableName=REGISTRY_TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {"s3": s3, "ddb": ddb_resource}


def _low():
    """A direct low-level DynamoDB client (inside the fixture's moto context).

    Deliberately NOT ``resource.meta.client`` — that one carries the resource's
    document transformations and mismatches the policy helpers' typed
    expressions (the exact production trap ``ar_rebuild.ddb_client`` avoids).
    """
    return boto3.client("dynamodb", region_name="us-east-1")


def _seed_sources(s3):
    s3.put_object(
        Bucket=BUNDLE_BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        Body=b"never sum booked and billed",
    )


def _set_ar_attrs(ddb, **attrs):
    ddb.Table(REGISTRY_TABLE).update_item(
        Key={"pk": f"DOMAIN#{DOMAIN}", "sk": f"DATASET#{DATASET}"},
        UpdateExpression="SET " + ", ".join(f"#{i} = :{i}" for i in range(len(attrs))),
        ExpressionAttributeNames={f"#{i}": k for i, k in enumerate(attrs)},
        ExpressionAttributeValues={f":{i}": v for i, v in enumerate(attrs.values())},
    )


def _attr(ddb, name) -> str:
    item = _low().get_item(
        TableName=REGISTRY_TABLE, Key=registry_key(DOMAIN, DATASET)
    ).get("Item", {})
    return (item.get(name) or {}).get("S", "")


def _put_doc(s3):
    put_policy_doc(
        s3, bucket=BUNDLE_BUCKET, data_domain=DOMAIN, dataset=DATASET,
        doc_text="policies:\n  - id: P001\n    type: computational\n"
        "    condition: c\n    action: a\n"
        "    source: references/usage_guardrails.md\n",
    )


def _run(aws, agentcore=None, force=False):
    return ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE,
        s3=aws["s3"], bucket=BUNDLE_BUCKET,
        agentcore=agentcore if agentcore is not None else FakeAgentCore(),
        harvest_runtime_arn=RUNTIME_ARN,
        force=force,
    )


# --- run_rebuild ----------------------------------------------------------------


def test_disabled_flag_is_a_no_op(monkeypatch):
    monkeypatch.delenv("OKF_POLICY_BUILD_ENABLED", raising=False)
    assert (
        ar_rebuild.run_rebuild(
            "d", "s", ddb=None, table="t", s3=None, bucket="b"
        )
        == ar_rebuild.OUTCOME_DISABLED
    )


def test_unregistered_dataset_costs_one_get_item(aws):
    # No mapping row at all (a late event, a duplicate that raced a dataset
    # delete): one GetItem, no S3 touch, nothing dispatched or written.
    class BrokenS3:
        def __getattr__(self, name):
            raise AssertionError("an unregistered dataset must never touch S3")

    out = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=BrokenS3(), bucket=BUNDLE_BUCKET,
        agentcore=FakeAgentCore(), harvest_runtime_arn=RUNTIME_ARN,
    )
    assert out == ar_rebuild.OUTCOME_UNREGISTERED


def test_never_authored_dataset_dispatches_a_first_authoring(aws):
    # THE manual-Sync backfill path: a registered dataset with a committed
    # wiki but no policy state at all (predates the feature) must author on
    # its first policy_rebuild event — always-on has no enrollment gate.
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    seed_ready_bundle(aws["s3"], data_domain=DOMAIN, dataset=DATASET)
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_INVOKED
    assert len(agentcore.invocations) == 1


def test_uncommitted_bundle_defers_to_the_finalize_hook(aws):
    # Source files exist but no complete marker (a first harvest mid-write):
    # dispatching would race the run — its own finalize hook authors at
    # commit, so the event is a clean no-op.
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_NO_WIKI
    assert agentcore.invocations == []


def test_changed_fingerprint_dispatches_with_a_fresh_session_each_time(aws):
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    seed_ready_bundle(aws["s3"], data_domain=DOMAIN, dataset=DATASET)
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_INVOKED
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_INVOKED
    first, second = agentcore.invocations
    assert json.loads(first["payload"]) == {
        "mode": "ar_rules", "data_domain": DOMAIN, "dataset": DATASET,
    }
    # No local flip: the runtime's own conditional flip is the dedup point.
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == ""
    # Fresh session ids: a reused id would pin the run to a pre-deploy
    # runtime version (live-observed).
    assert first["runtimeSessionId"] != second["runtimeSessionId"]
    assert len(first["runtimeSessionId"]) >= 33


def test_unchanged_fingerprint_with_a_live_document_skips(aws):
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    _put_doc(aws["s3"])
    fresh = source_hash(aws["s3"], BUNDLE_BUCKET, DOMAIN, DATASET)
    _set_ar_attrs(
        aws["ddb"],
        **{ATTR_BUILD_STATUS: BUILD_READY, ATTR_SOURCE_HASH: fresh},
    )
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_UNCHANGED
    assert agentcore.invocations == []


def test_forced_rebuild_reauthors_an_unchanged_ready_dataset(aws):
    # The manual Sync: sources identical, row ready, document live — force
    # dispatches anyway (the authoring config may have moved on), and the
    # payload carries the flag so the harvest-side skip is bypassed too.
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    seed_ready_bundle(aws["s3"], data_domain=DOMAIN, dataset=DATASET)
    _put_doc(aws["s3"])
    fresh = source_hash(aws["s3"], BUNDLE_BUCKET, DOMAIN, DATASET)
    _set_ar_attrs(
        aws["ddb"],
        **{ATTR_BUILD_STATUS: BUILD_READY, ATTR_SOURCE_HASH: fresh},
    )
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore, force=True) == ar_rebuild.OUTCOME_INVOKED
    (invocation,) = agentcore.invocations
    assert json.loads(invocation["payload"]) == {
        "mode": "ar_rules", "data_domain": DOMAIN, "dataset": DATASET,
        "force": True,
    }


def test_forced_rebuild_still_respects_an_in_flight_build(aws):
    # Force bypasses freshness, never the lease: a young building row wins.
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    _set_ar_attrs(
        aws["ddb"],
        **{
            ATTR_BUILD_STATUS: BUILD_BUILDING,
            "ar_build_started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore, force=True) == ar_rebuild.OUTCOME_IN_FLIGHT
    assert agentcore.invocations == []


def test_forced_rebuild_without_a_wiki_is_refused(aws):
    # Force is "author now", not "author from nothing": no complete marker,
    # no dispatch (same rule as the unforced path).
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore, force=True) == ar_rebuild.OUTCOME_NO_WIKI
    assert agentcore.invocations == []


def test_missing_document_reauthors_even_at_a_matching_hash(aws):
    # The self-heal: a ready row whose artifact is gone (pre-v2 dataset, lost
    # write) must dispatch, not skip as unchanged.
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    seed_ready_bundle(aws["s3"], data_domain=DOMAIN, dataset=DATASET)
    fresh = source_hash(aws["s3"], BUNDLE_BUCKET, DOMAIN, DATASET)
    _set_ar_attrs(
        aws["ddb"],
        **{ATTR_BUILD_STATUS: BUILD_READY, ATTR_SOURCE_HASH: fresh},
    )
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_INVOKED
    assert len(agentcore.invocations) == 1


def test_young_building_row_is_in_flight(aws):
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _set_ar_attrs(
        aws["ddb"],
        **{
            ATTR_BUILD_STATUS: BUILD_BUILDING,
            "ar_build_started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_IN_FLIGHT
    assert agentcore.invocations == []
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == BUILD_BUILDING


def test_stalled_building_row_is_reaped_then_redispatched(aws):
    # The Sync unstick path: an authoring run that died mid-flight left the
    # row `building`; past the grace period the row is reaped to failed and
    # the SAME call dispatches a fresh authoring run.
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    seed_ready_bundle(aws["s3"], data_domain=DOMAIN, dataset=DATASET)
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    _set_ar_attrs(
        aws["ddb"],
        **{
            ATTR_BUILD_STATUS: BUILD_BUILDING,
            "ar_build_started_at": stale.isoformat(),
        },
    )
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_INVOKED
    assert len(agentcore.invocations) == 1
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == "failed"
    assert _attr(aws["ddb"], "ar_build_detail") == "abandoned_build"


def test_current_artifact_with_a_lost_stamp_recovers_without_authoring(aws):
    # persist_author_state ran but the ready stamp never landed (crash between
    # the two): the manifest proves the document matches the live sources, so
    # recovery is a free re-stamp — no authoring dispatch.
    from okf_aws.ar_policy import persist_author_state

    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    _set_ar_attrs(aws["ddb"], **{ATTR_BUILD_STATUS: "failed"})
    from okf_aws.ar_policy import gather_sources

    persist_author_state(
        aws["s3"], bucket=BUNDLE_BUCKET, data_domain=DOMAIN, dataset=DATASET,
        sources=gather_sources(aws["s3"], BUNDLE_BUCKET, DOMAIN, DATASET),
        doc_text="policies:\n  - id: P001\n    type: computational\n"
        "    condition: c\n    action: a\n"
        "    source: references/usage_guardrails.md\n",
    )
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_RECOVERED
    assert agentcore.invocations == []
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == BUILD_READY
    fresh = source_hash(aws["s3"], BUNDLE_BUCKET, DOMAIN, DATASET)
    assert _attr(aws["ddb"], ATTR_SOURCE_HASH) == fresh


def test_pre_v3_document_reauthors_despite_matching_fingerprint(aws):
    # The v3 migration path: sources UNCHANGED (fingerprint matches, status
    # ready) but the stored document predates the `type` field — it no longer
    # parses, so it must count as missing: neither "unchanged" nor the
    # lost-stamp recovery may keep it; the run re-authors.
    from okf_aws.ar_policy import gather_sources, persist_author_state, source_hash

    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    seed_ready_bundle(aws["s3"], data_domain=DOMAIN, dataset=DATASET)
    fresh = source_hash(aws["s3"], BUNDLE_BUCKET, DOMAIN, DATASET)
    _set_ar_attrs(
        aws["ddb"],
        **{ATTR_BUILD_STATUS: "ready", ATTR_SOURCE_HASH: fresh},
    )
    persist_author_state(
        aws["s3"], bucket=BUNDLE_BUCKET, data_domain=DOMAIN, dataset=DATASET,
        sources=gather_sources(aws["s3"], BUNDLE_BUCKET, DOMAIN, DATASET),
        doc_text="policies:\n  - id: P001\n    condition: c\n    action: a\n"
        "    source: references/usage_guardrails.md\n",  # no `type` = pre-v3
    )
    agentcore = FakeAgentCore()
    assert _run(aws, agentcore) == ar_rebuild.OUTCOME_INVOKED
    assert len(agentcore.invocations) == 1


def test_no_runtime_configured_is_an_error_not_a_crash(aws):
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    seed_ready_bundle(aws["s3"], data_domain=DOMAIN, dataset=DATASET)
    out = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=aws["s3"], bucket=BUNDLE_BUCKET,
        agentcore=None, harvest_runtime_arn="",
    )
    assert out == ar_rebuild.OUTCOME_ERROR


# --- the nightly pass -------------------------------------------------------------


def _reconcile(aws, agentcore=None):
    return ar_rebuild.reconcile_policies(
        ddb=_low(), table=REGISTRY_TABLE,
        s3=aws["s3"], bucket=BUNDLE_BUCKET,
        agentcore=agentcore if agentcore is not None else FakeAgentCore(),
        harvest_runtime_arn=RUNTIME_ARN,
    )


def _seed_policy_ready(aws, *, stored_hash=None, status=BUILD_READY):
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(aws["s3"])
    seed_ready_bundle(aws["s3"], data_domain=DOMAIN, dataset=DATASET)
    _put_doc(aws["s3"])
    fresh = source_hash(aws["s3"], BUNDLE_BUCKET, DOMAIN, DATASET)
    _set_ar_attrs(
        aws["ddb"],
        **{
            ATTR_BUILD_STATUS: status,
            ATTR_SOURCE_HASH: stored_hash or fresh,
        },
    )


def test_reconcile_skips_current_and_never_authored_datasets(aws):
    # hr/people has a registered row but no ar_build_status: the nightly pass
    # must NOT backfill it (its first document is a Sync/harvest/repromote
    # decision, never a silent fleet-wide sweep) — it isn't even counted.
    _seed_policy_ready(aws)
    seed_mapping(aws["ddb"], data_domain="hr", dataset="people", glue_database="db2")
    summary = _reconcile(aws)
    assert summary == {
        "datasets": 1, "rebuilt": 0, "recovered": 0, "in_flight": 0,
        "reaped": 0, "skipped": 1, "errors": 0,
    }


def test_reconcile_dispatches_moved_stale_and_docless_datasets(aws):
    _seed_policy_ready(aws, stored_hash="stale-fingerprint")
    agentcore = FakeAgentCore()
    summary = _reconcile(aws, agentcore)
    assert summary["rebuilt"] == 1
    (call,) = agentcore.invocations
    assert json.loads(call["payload"])["mode"] == "ar_rules"


def test_reconcile_flags_stale_status_even_at_matching_hash(aws):
    _seed_policy_ready(aws, status=BUILD_STALE)
    summary = _reconcile(aws)
    assert summary["rebuilt"] == 1


def test_reconcile_reauthors_when_the_document_is_missing(aws):
    _seed_policy_ready(aws)
    aws["s3"].delete_object(
        Bucket=BUNDLE_BUCKET, Key=policy_doc_key(DOMAIN, DATASET)
    )
    summary = _reconcile(aws)
    assert summary["rebuilt"] == 1


def test_reconcile_reaps_abandoned_building_rows(aws):
    _seed_policy_ready(aws)
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    _set_ar_attrs(
        aws["ddb"],
        **{
            ATTR_BUILD_STATUS: BUILD_BUILDING,
            "ar_build_started_at": stale.isoformat(),
        },
    )
    summary = _reconcile(aws)
    assert summary["reaped"] == 1
    # After the reap the SAME pass re-checks the hash; doc + hash match here,
    # but the status is no longer usable, so it dispatches a rebuild.
    assert summary["rebuilt"] == 1


def test_reconcile_leaves_young_building_rows_alone(aws):
    _seed_policy_ready(aws)
    _set_ar_attrs(
        aws["ddb"],
        **{
            ATTR_BUILD_STATUS: BUILD_BUILDING,
            "ar_build_started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    summary = _reconcile(aws)
    assert summary["in_flight"] == 1 and summary["rebuilt"] == 0


def test_reconcile_never_dispatches_over_an_unready_bundle(aws):
    # Lifecycle begun (a failed first build) but the bundle lost its commit
    # marker (a re-harvest mid-write): the sweep must not race the run.
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _set_ar_attrs(aws["ddb"], **{ATTR_BUILD_STATUS: "failed"})
    _seed_sources(aws["s3"])  # sources exist but no commit marker
    summary = _reconcile(aws)
    assert summary["skipped"] == 1 and summary["rebuilt"] == 0
