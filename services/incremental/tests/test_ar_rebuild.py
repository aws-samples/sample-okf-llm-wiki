"""The AR rebuild authority: event dispatch, build completion, hash-verify.

Three contracts are pinned here. (1) The ``policy_rebuild`` event lane never
disturbs the Glue lane and never retries malformed events. (2) Completion
stamps the row from the CARRIED pending fingerprint — verbatim, not recomputed
— so a wiki that moved mid-build yields a stale-on-arrival policy. (3) The
nightly pass rebuilds exactly the stale/mismatched datasets and skips the rest.
Bedrock is a hand fake (moto has no AR support); S3/DynamoDB run on moto.
"""

from __future__ import annotations

import json

import pytest

from conftest import BUNDLE_BUCKET, REGISTRY_TABLE, seed_mapping, seed_ready_bundle
from fakes import FakeAgentCore, FakeGlue
from incremental import ar_rebuild, handler, reconcile
from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_PENDING_SOURCE_HASH,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    BUILD_FAILED,
    BUILD_READY,
    BUILD_STALE,
    dataset_label,
    grounding_key,
    policy_name,
    registry_key,
    source_hash,
)

DOMAIN = "sales"
DATASET = "orders"
LABEL = dataset_label(DOMAIN, DATASET)
POLICY_ARN = "arn:aws:bedrock:us-east-1:1:automated-reasoning-policy/p1"


def _low():
    """A direct low-level DynamoDB client (inside the fixture's moto context).

    Deliberately NOT ``resource.meta.client`` — that one carries the resource's
    document transformations and mismatches the AR helpers' typed expressions
    (the exact production trap ``ar_rebuild.ddb_client`` exists to avoid).
    """
    import boto3

    return boto3.client("dynamodb", region_name="us-east-1")


class NotFoundError(Exception):
    """Shaped like botocore's ResourceNotFoundException for the asset calls."""

    def __init__(self):
        super().__init__("ResourceNotFoundException")
        self.response = {"Error": {"Code": "ResourceNotFoundException"}}


class FakeBedrock:
    """The AR control plane: enough of it for start AND completion paths.

    Models the LIVE build semantics: the built rules are staged as the
    POLICY_DEFINITION asset (never applied to the draft by AWS), and the
    FIDELITY_REPORT asset exists only when a test opts in (`fidelity=True`) —
    ingest builds don't produce one.
    """

    def __init__(
        self,
        *,
        workflow_status="COMPLETED",
        coverage=0.9,
        accuracy=0.95,
        fidelity=False,
    ):
        self.workflow_status = workflow_status
        self.coverage = coverage
        self.accuracy = accuracy
        self.fidelity = fidelity
        self.started: list[dict] = []
        self.updated: list[dict] = []
        self.versioned: list[dict] = []
        self.guardrails: list[dict] = []
        self.guardrail_updates: list[dict] = []
        self.built_rules = [
            {
                "id": "RABCDEFGHIJK",
                "expression": "(=> zeroRowsReturned (not commit))",
                "alternateExpression": (
                    "IF zeroRowsReturned THEN no figures "
                    "(references/usage_guardrails.md)"
                ),
            }
        ]

    def list_automated_reasoning_policies(self, **kw):
        return {
            "automatedReasoningPolicySummaries": [
                {"policyArn": POLICY_ARN, "name": policy_name(LABEL), "version": "DRAFT"}
            ]
        }

    def create_automated_reasoning_policy(self, **kw):
        raise AssertionError("policy already exists; create must not be called")

    def start_automated_reasoning_policy_build_workflow(self, **kw):
        self.started.append(kw)
        return {"policyArn": kw["policyArn"], "buildWorkflowId": "wf-9"}

    def get_automated_reasoning_policy_build_workflow(self, **kw):
        return {"status": self.workflow_status}

    def get_automated_reasoning_policy_build_workflow_result_assets(self, **kw):
        asset_type = kw.get("assetType")
        if asset_type == "POLICY_DEFINITION":
            return {
                "buildWorkflowAssets": {
                    "policyDefinition": {
                        "version": "1",
                        "types": [],
                        "variables": [],
                        "rules": self.built_rules,
                    }
                }
            }
        if asset_type == "FIDELITY_REPORT" and self.fidelity:
            return {
                "buildWorkflowAssets": {
                    "fidelityReport": {
                        "coverageScore": self.coverage,
                        "accuracyScore": self.accuracy,
                        "ruleReports": {},
                    }
                }
            }
        raise NotFoundError()

    def get_automated_reasoning_policy(self, **kw):
        return {"policyArn": POLICY_ARN, "definitionHash": "a" * 128}

    def update_automated_reasoning_policy(self, **kw):
        self.updated.append(kw)
        return {"policyArn": kw["policyArn"]}

    def create_automated_reasoning_policy_version(self, **kw):
        self.versioned.append(kw)
        return {"policyArn": f"{POLICY_ARN}:1", "version": "1"}

    def export_automated_reasoning_policy_version(self, **kw):
        return {
            "policyDefinition": {
                "rules": [
                    {
                        "id": "RABCDEFGHIJK",
                        "expression": "(=> zeroRowsReturned (not commit))",
                        "alternateExpression": (
                            "IF zeroRowsReturned THEN no figures "
                            "(references/usage_guardrails.md)"
                        ),
                    }
                ]
            }
        }

    def create_guardrail(self, **kw):
        self.guardrails.append(kw)
        return {"guardrailId": "g1", "guardrailArn": "arn:g1", "version": "DRAFT"}

    def update_guardrail(self, **kw):
        self.guardrail_updates.append(kw)
        return {"guardrailId": kw["guardrailIdentifier"], "version": "DRAFT"}

    def create_guardrail_version(self, **kw):
        return {"guardrailId": kw["guardrailIdentifier"], "version": "2"}


@pytest.fixture
def ar_env(monkeypatch):
    monkeypatch.setenv("OKF_POLICY_BUILD_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


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


def _row(ddb):
    return (
        _low().get_item(
            TableName=REGISTRY_TABLE, Key=registry_key(DOMAIN, DATASET)
        ).get("Item")
        or {}
    )


def _attr(ddb, name):
    return (_row(ddb).get(name) or {}).get("S", "")


# --- the event lane ---------------------------------------------------------------


def _sqs_event(body: dict) -> dict:
    return {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}


def _handler(aws, event, monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", BUNDLE_BUCKET)
    monkeypatch.setenv("OKF_HARVEST_RUNTIME_ARN", "arn:aws:bedrock-agentcore:::rt")
    clients = {
        "glue": FakeGlue({}),
        "ddb": aws["ddb"],
        "s3": aws["s3"],
        "agentcore": FakeAgentCore(),
    }
    return (
        handler.lambda_handler(event, clients_factory=lambda: clients),
        clients,
    )


def test_policy_rebuild_event_dispatches_to_run_rebuild(aws, ar_env, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        ar_rebuild, "run_rebuild", lambda dd, ds, **kw: calls.append((dd, ds)) or "started"
    )
    body = {
        "source": "okf.policy",
        "detail-type": "policy_rebuild",
        "detail": {"data_domain": DOMAIN, "dataset": DATASET},
    }
    result, clients = _handler(aws, _sqs_event(body), monkeypatch)
    assert result == {"batchItemFailures": []}
    assert calls == [(DOMAIN, DATASET)]
    assert clients["agentcore"].invocations == []  # the Glue lane never woke up


def test_policy_rebuild_event_ignored_when_disabled(aws, monkeypatch):
    monkeypatch.delenv("OKF_POLICY_BUILD_ENABLED", raising=False)
    calls: list = []
    monkeypatch.setattr(ar_rebuild, "run_rebuild", lambda *a, **kw: calls.append(a))
    body = {
        "detail-type": "policy_rebuild",
        "detail": {"data_domain": DOMAIN, "dataset": DATASET},
    }
    result, _ = _handler(aws, _sqs_event(body), monkeypatch)
    assert result == {"batchItemFailures": []}
    assert calls == []


def test_malformed_policy_rebuild_detail_is_dropped_not_retried(
    aws, ar_env, monkeypatch
):
    # Retrying can't fix a shape; correctness is the nightly reconcile's job.
    calls: list = []
    monkeypatch.setattr(ar_rebuild, "run_rebuild", lambda *a, **kw: calls.append(a))
    body = {"detail-type": "policy_rebuild", "detail": {"dataset": DATASET}}
    result, _ = _handler(aws, _sqs_event(body), monkeypatch)
    assert result == {"batchItemFailures": []}
    assert calls == []


def test_glue_envelope_still_takes_the_glue_lane(aws, ar_env, monkeypatch):
    monkeypatch.setattr(
        ar_rebuild,
        "run_rebuild",
        lambda *a, **kw: pytest.fail("glue event must not hit the AR lane"),
    )
    body = {
        "source": "aws.glue",
        "detail-type": handler.DETAIL_TYPE_GLUE_TABLE_CHANGE,
        "detail": {"databaseName": "unmapped_db", "tableName": "t"},
    }
    result, _ = _handler(aws, _sqs_event(body), monkeypatch)
    assert result == {"batchItemFailures": []}


# --- run_rebuild -------------------------------------------------------------------


def test_run_rebuild_dispatches_the_author_to_the_runtime(aws, ar_env):
    # A never-built source state needs the authoring agent, which lives on the
    # harvest runtime — the Lambda fires one mode="ar_rules" invocation and
    # does NOT flip the row (the runtime's own conditional flip is the dedup
    # point, so duplicate events cost a no-op session, never a second build).
    import json as _json

    s3, ddb = aws["s3"], aws["ddb"]
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _set_ar_attrs(ddb, ar_enrolled=True)
    _seed_sources(s3)
    agentcore = FakeAgentCore()
    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=s3, bucket=BUNDLE_BUCKET,
        bedrock=FakeBedrock(), agentcore=agentcore,
        harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
    )
    assert outcome == ar_rebuild.OUTCOME_INVOKED
    assert _attr(ddb, ATTR_BUILD_STATUS) == ""  # no local flip
    (call,) = agentcore.invocations
    assert _json.loads(call["payload"]) == {
        "mode": "ar_rules", "data_domain": DOMAIN, "dataset": DATASET,
    }


def test_each_dispatch_gets_a_fresh_runtime_session(aws, ar_env):
    # An AgentCore session is pinned to the runtime VERSION it started on: a
    # deterministic session id would reattach a post-deploy retry to a warm
    # microVM still running pre-deploy code (observed live: a retried build
    # failed on old code minutes after the fixed image shipped). Every
    # dispatch must mint a NEW session id; dedup is the runtime's conditional
    # flip, never session affinity.
    s3, ddb = aws["s3"], aws["ddb"]
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _set_ar_attrs(ddb, ar_enrolled=True)
    _seed_sources(s3)
    agentcore = FakeAgentCore()
    for _ in range(2):
        ar_rebuild.run_rebuild(
            DOMAIN, DATASET,
            ddb=_low(), table=REGISTRY_TABLE, s3=s3, bucket=BUNDLE_BUCKET,
            bedrock=FakeBedrock(), agentcore=agentcore,
            harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
        )
    first, second = (c["runtimeSessionId"] for c in agentcore.invocations)
    assert first != second
    # Both must still satisfy AgentCore's 33-char minimum.
    assert len(first) >= 33 and len(second) >= 33


def test_run_rebuild_restores_from_a_snapshot_without_dispatching(aws, ar_env):
    from okf_aws import ar_policy as ap

    s3, ddb = aws["s3"], aws["ddb"]
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _set_ar_attrs(ddb, ar_enrolled=True)
    _seed_sources(s3)
    fresh = source_hash(s3, BUNDLE_BUCKET, DOMAIN, DATASET)
    ap.write_snapshot(
        s3, bucket=BUNDLE_BUCKET, data_domain=DOMAIN, dataset=DATASET,
        snapshot={
            "fingerprint": fresh,
            "policy_definition": {"rules": [{"id": "R1", "expression": "(=> a b)"}]},
            "grounding": {}, "rules_text": "1. IF a THEN b",
            "fidelity_coverage": 0.9, "fidelity_accuracy": 0.95,
            "build_status": "ready",
        },
    )
    bedrock, agentcore = FakeBedrock(), FakeAgentCore()
    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=s3, bucket=BUNDLE_BUCKET,
        bedrock=bedrock, agentcore=agentcore,
        harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
    )
    assert outcome == ar_rebuild.OUTCOME_RESTORED
    assert agentcore.invocations == []  # deterministic path: no agent run
    assert bedrock.started == []  # and no ingest build
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_READY
    assert _attr(ddb, ATTR_SOURCE_HASH) == fresh


def test_failed_row_with_unchanged_wiki_heals_from_the_snapshot(aws, ar_env):
    # The Reasoning page's "Retry build" after a transient service failure:
    # the wiki never moved (stored hash == fresh hash) but the row reads
    # `failed`. The unchanged-skip admits only usable/building rows, so the
    # retry must proceed — and since an earlier era snapshotted this very
    # fingerprint, it heals deterministically: no agent run, no ingest.
    from okf_aws import ar_policy as ap

    s3 = aws["s3"]
    _seed_usable_row(aws, status=BUILD_FAILED)  # stored hash == fresh hash
    fresh = source_hash(s3, BUNDLE_BUCKET, DOMAIN, DATASET)
    ap.write_snapshot(
        s3, bucket=BUNDLE_BUCKET, data_domain=DOMAIN, dataset=DATASET,
        snapshot={
            "fingerprint": fresh,
            "policy_definition": {"rules": [{"id": "R1", "expression": "(=> a b)"}]},
            "grounding": {}, "rules_text": "1. IF a THEN b",
            "fidelity_coverage": 0.9, "fidelity_accuracy": 0.95,
            "build_status": "ready",
        },
    )
    bedrock, agentcore = FakeBedrock(), FakeAgentCore()
    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=s3, bucket=BUNDLE_BUCKET,
        bedrock=bedrock, agentcore=agentcore,
        harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
    )
    assert outcome == ar_rebuild.OUTCOME_RESTORED
    assert agentcore.invocations == []
    assert bedrock.started == []
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == BUILD_READY


def test_failed_first_build_dispatches_a_retry(aws, ar_env):
    # A first-ever build that died before completion: no stored hash, no
    # snapshot, status `failed`. The manual sync must dispatch a fresh
    # authoring run — never OUTCOME_UNCHANGED, never a stranded dataset.
    s3, ddb = aws["s3"], aws["ddb"]
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _set_ar_attrs(
        ddb,
        **{
            "ar_enrolled": True,
            ATTR_BUILD_STATUS: BUILD_FAILED,
            "ar_build_detail": "ValidationException: type not defined",
        },
    )
    _seed_sources(s3)
    agentcore = FakeAgentCore()
    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=s3, bucket=BUNDLE_BUCKET,
        bedrock=FakeBedrock(), agentcore=agentcore,
        harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
    )
    assert outcome == ar_rebuild.OUTCOME_INVOKED
    (call,) = agentcore.invocations
    assert json.loads(call["payload"])["mode"] == "ar_rules"
    # No local flip: the runtime's own conditional flip is the dedup point.
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_FAILED


def test_run_rebuild_skips_an_unenrolled_dataset(aws, ar_env):
    # A late/duplicate event that raced an unenroll: one GetItem, no S3 walk,
    # no build. The broken S3 seam proves nothing past the gate ran.
    s3, ddb = aws["s3"], aws["ddb"]
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")

    class BrokenS3:
        def get_paginator(self, *a, **kw):
            raise AssertionError("unenrolled dataset must not touch S3")

    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=BrokenS3(), bucket=BUNDLE_BUCKET,
    )
    assert outcome == ar_rebuild.OUTCOME_NOT_ENROLLED


def test_reconcile_skips_unenrolled_datasets_entirely(aws, ar_env):
    # An unenrolled dataset with sources + a ready bundle is the steady state,
    # not drift: no backfill, not even counted (the cap is a user budget).
    s3, ddb = aws["s3"], aws["ddb"]
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(s3)
    seed_ready_bundle(s3, data_domain=DOMAIN, dataset=DATASET)
    summary = _reconcile_policies(aws, bedrock=FakeBedrock())
    assert summary["datasets"] == 0 and summary["rebuilt"] == 0


def test_run_rebuild_disabled_is_a_no_op(aws, monkeypatch):
    monkeypatch.delenv("OKF_POLICY_BUILD_ENABLED", raising=False)
    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE,
        s3=aws["s3"], bucket=BUNDLE_BUCKET,
    )
    assert outcome == ar_rebuild.OUTCOME_DISABLED


def test_run_rebuild_never_raises(aws, ar_env):
    seed_mapping(aws["ddb"], data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _set_ar_attrs(aws["ddb"], ar_enrolled=True)

    class BrokenS3:
        def get_paginator(self, *a, **kw):
            raise RuntimeError("s3 down")

    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE,
        s3=BrokenS3(), bucket=BUNDLE_BUCKET,
    )
    assert outcome == ar_rebuild.OUTCOME_ERROR


# --- completion --------------------------------------------------------------------


def _seed_building_row(ddb, *, workflow="wf-9", pending="pend-hash", started_at=""):
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    attrs = {
        "ar_enrolled": True,
        ATTR_BUILD_STATUS: BUILD_BUILDING,
        ATTR_PENDING_SOURCE_HASH: pending,
    }
    if workflow:
        attrs["ar_build_workflow_id"] = workflow
    if started_at:
        attrs["ar_build_started_at"] = started_at
    _set_ar_attrs(ddb, **attrs)


def _reconcile_policies(aws, bedrock=None, agentcore=None):
    return ar_rebuild.reconcile_policies(
        ddb=_low(), table=REGISTRY_TABLE,
        s3=aws["s3"], bucket=BUNDLE_BUCKET, bedrock=bedrock,
        agentcore=agentcore if agentcore is not None else FakeAgentCore(),
        harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
    )


def test_sync_completes_a_stuck_building_row(aws, ar_env):
    # THE live incident: the harvest runtime started a build whose in-session
    # completion never ran; the row sat at `building` while the AWS workflow
    # was COMPLETED, and the UI showed Building… forever (the nightly
    # reconcile is opt-in and absent). A manual Sync fires run_rebuild — it
    # must finish the stamp, not skip the row as "unchanged".
    s3, ddb = aws["s3"], aws["ddb"]
    _seed_building_row(ddb, pending="pend-hash")
    _seed_sources(s3)
    bedrock = FakeBedrock()  # workflow COMPLETED, results staged as assets
    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=s3, bucket=BUNDLE_BUCKET,
        bedrock=bedrock, agentcore=FakeAgentCore(),
        harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
    )
    assert outcome == ar_rebuild.OUTCOME_COMPLETED
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_READY
    assert _attr(ddb, ATTR_SOURCE_HASH) == "pend-hash"
    # The staged definition was APPLIED to the draft before versioning.
    (applied,) = bedrock.updated
    assert applied["policyDefinition"]["rules"] == bedrock.built_rules


def test_sync_on_a_genuinely_running_build_reports_in_flight(aws, ar_env):
    s3, ddb = aws["s3"], aws["ddb"]
    _seed_building_row(ddb)
    _seed_sources(s3)
    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=s3, bucket=BUNDLE_BUCKET,
        bedrock=FakeBedrock(workflow_status="TESTING"), agentcore=FakeAgentCore(),
        harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
    )
    assert outcome == ar_rebuild.OUTCOME_IN_FLIGHT
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_BUILDING


def test_sync_after_a_failed_workflow_immediately_reauthors(aws, ar_env):
    # Completion stamps `failed` and the SAME run falls through to a fresh
    # decision — dispatching a new authoring run rather than making the user
    # click twice.
    import json as _json

    s3, ddb = aws["s3"], aws["ddb"]
    _seed_building_row(ddb)
    _seed_sources(s3)
    agentcore = FakeAgentCore()
    outcome = ar_rebuild.run_rebuild(
        DOMAIN, DATASET,
        ddb=_low(), table=REGISTRY_TABLE, s3=s3, bucket=BUNDLE_BUCKET,
        bedrock=FakeBedrock(workflow_status="FAILED"), agentcore=agentcore,
        harvest_runtime_arn="arn:aws:bedrock-agentcore:::rt",
    )
    assert outcome == ar_rebuild.OUTCOME_INVOKED
    (call,) = agentcore.invocations
    assert _json.loads(call["payload"])["mode"] == "ar_rules"


def test_completion_stamps_the_carried_fingerprint_verbatim(aws, ar_env):
    s3, ddb = aws["s3"], aws["ddb"]
    _seed_building_row(ddb, pending="pend-hash")
    # The wiki moved mid-build (sources now hash differently) — the stamp must
    # still be the gather-time value, leaving the policy stale on arrival.
    _seed_sources(s3)
    bedrock = FakeBedrock()
    summary = _reconcile_policies(aws, bedrock=bedrock)
    assert summary["completed"] == 1
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_READY
    assert _attr(ddb, ATTR_SOURCE_HASH) == "pend-hash"
    assert _attr(ddb, "ar_policy_version") == "1"
    assert _attr(ddb, "ar_guardrail_id") == "g1"
    assert (bedrock.versioned[0]["lastUpdatedDefinitionHash"]) == "a" * 128
    grounding = json.loads(
        s3.get_object(Bucket=BUNDLE_BUCKET, Key=grounding_key(DOMAIN, DATASET))[
            "Body"
        ].read()
    )
    assert "RABCDEFGHIJK" in grounding


def test_completion_low_fidelity_marks_degraded(aws, ar_env):
    # Degrading requires a MEASURED report (fidelity=True): unmeasured
    # fidelity — the live default for ingest builds — stays `ready`.
    _seed_building_row(aws["ddb"])
    summary = _reconcile_policies(aws, bedrock=FakeBedrock(coverage=0.3, fidelity=True))
    assert summary["completed"] == 1
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == "degraded"


def test_completion_failed_workflow_releases_the_lease(aws, ar_env):
    _seed_building_row(aws["ddb"])
    summary = _reconcile_policies(aws, bedrock=FakeBedrock(workflow_status="FAILED"))
    assert summary["errors"] == 1
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == "failed"


def test_completion_in_flight_workflow_is_left_alone(aws, ar_env):
    _seed_building_row(aws["ddb"])
    summary = _reconcile_policies(aws, bedrock=FakeBedrock(workflow_status="TESTING"))
    assert summary["in_flight"] == 1
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == BUILD_BUILDING


def test_abandoned_building_row_is_reaped(aws, ar_env):
    # Flipped to building, crashed before Start: no workflow id, stale timestamp.
    _seed_building_row(
        aws["ddb"], workflow="", started_at="2026-01-01T00:00:00+00:00"
    )
    summary = _reconcile_policies(aws, bedrock=FakeBedrock())
    assert summary["errors"] == 1
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == "failed"


def test_just_flipped_row_without_workflow_is_inside_the_grace_period(aws, ar_env):
    from datetime import datetime, timezone

    _seed_building_row(
        aws["ddb"], workflow="", started_at=datetime.now(timezone.utc).isoformat()
    )
    summary = _reconcile_policies(aws, bedrock=FakeBedrock())
    assert summary["in_flight"] == 1
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == BUILD_BUILDING


# --- hash-verify -------------------------------------------------------------------


def _seed_usable_row(aws, *, status=BUILD_READY, stored_hash=None):
    s3, ddb = aws["s3"], aws["ddb"]
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _seed_sources(s3)
    seed_ready_bundle(s3, data_domain=DOMAIN, dataset=DATASET)
    fresh = source_hash(s3, BUNDLE_BUCKET, DOMAIN, DATASET)
    _set_ar_attrs(
        ddb,
        **{
            "ar_enrolled": True,
            ATTR_BUILD_STATUS: status,
            ATTR_SOURCE_HASH: stored_hash or fresh,
        },
    )
    return fresh


def test_matching_usable_policy_is_skipped(aws, ar_env):
    _seed_usable_row(aws)
    agentcore = FakeAgentCore()
    summary = _reconcile_policies(aws, bedrock=FakeBedrock(), agentcore=agentcore)
    assert summary["skipped"] == 1 and summary["rebuilt"] == 0
    assert agentcore.invocations == []  # nothing dispatched, zero LLM cost


def test_hash_mismatch_dispatches_an_authoring_run(aws, ar_env):
    import json as _json

    _seed_usable_row(aws, stored_hash="stale-fingerprint")
    agentcore = FakeAgentCore()
    summary = _reconcile_policies(aws, bedrock=FakeBedrock(), agentcore=agentcore)
    assert summary["rebuilt"] == 1
    (call,) = agentcore.invocations
    assert _json.loads(call["payload"])["mode"] == "ar_rules"
    # No local flip: the runtime owns the lease, so the row is untouched until
    # the authoring run lands (the usability gate already refuses the stale
    # hash in the meantime).
    assert _attr(aws["ddb"], ATTR_BUILD_STATUS) == BUILD_READY


def test_stale_flag_triggers_a_rebuild_even_at_matching_hash(aws, ar_env):
    _seed_usable_row(aws, status=BUILD_STALE)
    summary = _reconcile_policies(aws, bedrock=FakeBedrock())
    assert summary["rebuilt"] == 1


def test_unready_bundle_is_never_backfilled(aws, ar_env):
    s3, ddb = aws["s3"], aws["ddb"]
    seed_mapping(ddb, data_domain=DOMAIN, dataset=DATASET, glue_database="db")
    _set_ar_attrs(ddb, ar_enrolled=True)
    _seed_sources(s3)  # sources exist but no commit marker
    summary = _reconcile_policies(aws, bedrock=FakeBedrock())
    assert summary["skipped"] == 1 and summary["rebuilt"] == 0


# --- the nightly entrypoint ----------------------------------------------------------


def _run_reconcile(aws, **kw):
    return reconcile.reconcile(
        glue=FakeGlue({}),
        ddb=aws["ddb"],
        s3=aws["s3"],
        agentcore=FakeAgentCore(),
        bundle_bucket=BUNDLE_BUCKET,
        registry_table=REGISTRY_TABLE,
        freshness_table="okf-freshness",
        harvest_runtime_arn="arn:rt",
        **kw,
    )


def test_reconcile_runs_the_ar_pass_when_enabled(aws, ar_env):
    _seed_usable_row(aws)
    summary = _run_reconcile(aws, bedrock=FakeBedrock())
    assert summary["ar"]["datasets"] == 1


def test_reconcile_has_no_ar_pass_when_disabled(aws, monkeypatch):
    monkeypatch.delenv("OKF_POLICY_BUILD_ENABLED", raising=False)
    assert "ar" not in _run_reconcile(aws)


def test_ar_pass_failure_never_fails_the_sweep(aws, ar_env, monkeypatch):
    monkeypatch.setattr(
        ar_rebuild,
        "reconcile_policies",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    summary = _run_reconcile(aws)
    assert summary["ar"] == {"error": True}
    assert summary["scanned_datasets"] == 0  # the table sweep itself completed
