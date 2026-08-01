"""The AR policy build trigger at harvest finalize.

The properties pinned here are the ones ``harvest.ar_build``'s docstring calls
load-bearing: byte-identical when the flag is off, rebuild iff the source
fingerprint moved, the ``building`` flip as the serialization point, and — above
all — NEVER failing a harvest whose bundle is already committed. The Bedrock
control plane is a hand fake (moto has no AR support); S3 and DynamoDB run on
moto so the conditional flip is tested against real ConditionExpression
semantics.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from harvest import ar_build
from harvest.finalize import finalize_bundle
from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_PENDING_SOURCE_HASH,
    ATTR_BUILD_WORKFLOW_ID,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    BUILD_READY,
    BUILD_UNSUPPORTED_REGION,
    ar_rules_key,
    registry_key,
    source_hash,
)

DOMAIN = "sport"
DATASET = "formula_1"
BUCKET = "okf-bundles"
TABLE = "okf-registry"


class Boto3Error(Exception):
    """A botocore-shaped error: the code lives under ``response.Error.Code``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBedrock:
    """The slice of the ``bedrock`` control plane the trigger + restore touch.

    ``workflow_status`` scripts what the post-start poll sees (a string, or a
    list consumed one poll at a time, last value sticky). The result assets
    model the LIVE behavior: POLICY_DEFINITION carries the built rules;
    everything else raises ``ResourceNotFoundException``.
    """

    def __init__(self, *, create_error: str = "", workflow_status="COMPLETED"):
        self.create_error = create_error
        self.workflow_status = workflow_status
        self.status_polls = 0
        self.created: list[dict] = []
        self.started: list[dict] = []
        self.updated: list[dict] = []
        self.versions: list[dict] = []
        self.exports: list[dict] = []
        self.guardrails_updated: list[dict] = []
        self.guardrails_created: list[dict] = []
        self.built_rules = [
            {
                "id": "R1",
                "expression": "(=> x y)",
                "alternateExpression": "if x is true, then y is true",
            }
        ]

    def list_automated_reasoning_policies(self, **kw):
        return {"automatedReasoningPolicySummaries": []}

    def create_automated_reasoning_policy(self, **kw):
        if self.create_error:
            raise Boto3Error(self.create_error)
        self.created.append(kw)
        return {
            "policyArn": "arn:aws:bedrock:us-east-1:1:automated-reasoning-policy/p1",
            "version": "DRAFT",
            "definitionHash": "0" * 128,
        }

    def start_automated_reasoning_policy_build_workflow(self, **kw):
        self.started.append(kw)
        return {"policyArn": kw["policyArn"], "buildWorkflowId": "wf-1"}

    # -- the post-start poll + in-session completion
    def get_automated_reasoning_policy_build_workflow(self, **kw):
        self.status_polls += 1
        status = self.workflow_status
        if isinstance(status, list):
            status = status[min(self.status_polls - 1, len(status) - 1)]
        return {"status": status}

    def get_automated_reasoning_policy_build_workflow_result_assets(self, **kw):
        if kw.get("assetType") == "POLICY_DEFINITION":
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
        raise Boto3Error("ResourceNotFoundException")

    def export_automated_reasoning_policy_version(self, **kw):
        self.exports.append(kw)
        if self.updated:
            return {"policyDefinition": self.updated[-1]["policyDefinition"]}
        return {
            "policyDefinition": {
                "rules": self.built_rules, "variables": [], "types": [],
            }
        }

    # -- the deterministic restore path
    def update_automated_reasoning_policy(self, **kw):
        self.updated.append(kw)
        return {"policyArn": kw["policyArn"]}

    def get_automated_reasoning_policy(self, **kw):
        return {"policyArn": kw["policyArn"], "definitionHash": "1" * 128}

    def create_automated_reasoning_policy_version(self, **kw):
        self.versions.append(kw)
        return {"policyArn": f"{kw['policyArn']}:1", "version": "1"}

    def create_guardrail(self, **kw):
        self.guardrails_created.append(kw)
        return {"guardrailId": "g1", "guardrailArn": "arn:g1", "version": "DRAFT"}

    def update_guardrail(self, **kw):
        self.guardrails_updated.append(kw)
        return {"guardrailId": kw["guardrailIdentifier"], "version": "DRAFT"}

    def create_guardrail_version(self, **kw):
        return {"guardrailId": kw["guardrailIdentifier"], "version": "2"}


class FakeAuthor:
    """The rules-author seam: records every call, returns the scripted doc."""

    def __init__(self, reply="1. IF x THEN y (references/usage_guardrails.md)"):
        self.reply = reply
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture()
def aws(monkeypatch):
    """moto S3 + DynamoDB, a seeded bundle, the flag ON, and the env wired."""
    monkeypatch.setenv("OKF_POLICY_BUILD_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", BUCKET)
    # Fire-and-forget by default: the trigger-path tests assert the started
    # state (row `building`, workflow id stamped). The in-session completion
    # tests opt back in with their own OKF_POLICY_BUILD_WAIT_SECONDS.
    monkeypatch.setenv("OKF_POLICY_BUILD_WAIT_SECONDS", "0")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        prefix = f"okf/{DOMAIN}/{DATASET}/"
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{prefix}references/usage_guardrails.md",
            Body=b"never sum booked and billed",
        )
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{prefix}references/enums/status.md",
            Body=b"-1 means unknown",
        )
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}tables/results.md", Body=b"not a source")
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
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
        # The mapping row exists from dataset registration; every stamp is
        # conditioned on it (attribute_exists(pk)) so a build can never
        # resurrect a deleted dataset. Enrolled: reasoning is per-dataset
        # OPT-IN, and everything below the enrollment gate is what these
        # tests exercise.
        ddb.put_item(
            TableName=TABLE,
            Item={
                **registry_key(DOMAIN, DATASET),
                "data_domain": {"S": DOMAIN},
                "dataset": {"S": DATASET},
                "ar_enrolled": {"BOOL": True},
            },
        )
        yield s3, ddb


def _trigger(s3, ddb, *, bedrock=None, author=None):
    return ar_build.maybe_build_policy(
        data_domain=DOMAIN,
        dataset=DATASET,
        registry=(ddb, TABLE),
        s3=s3,
        bedrock=bedrock or FakeBedrock(),
        author=author or FakeAuthor(),
    )


def _row(ddb) -> dict:
    return ddb.get_item(TableName=TABLE, Key=registry_key(DOMAIN, DATASET)).get(
        "Item", {}
    )


def _attr(ddb, name) -> str:
    return (_row(ddb).get(name) or {}).get("S", "")


# --- the flag ------------------------------------------------------------------


def test_disabled_flag_is_a_pure_no_op(monkeypatch):
    # No env beyond the flag, no clients: with the flag off nothing may be
    # touched — the injected seams would raise if constructed or called.
    monkeypatch.delenv("OKF_POLICY_BUILD_ENABLED", raising=False)
    outcome = ar_build.maybe_build_policy(data_domain=DOMAIN, dataset=DATASET)
    assert outcome == ar_build.OUTCOME_DISABLED


def test_enabled_without_registry_table_degrades(monkeypatch):
    monkeypatch.setenv("OKF_POLICY_BUILD_ENABLED", "true")
    monkeypatch.delenv("OKF_REGISTRY_TABLE", raising=False)
    outcome = ar_build.maybe_build_policy(data_domain=DOMAIN, dataset=DATASET)
    assert outcome == ar_build.OUTCOME_UNCONFIGURED


# --- region + sources gates ------------------------------------------------------


def test_unsupported_region_stamps_once(aws, monkeypatch):
    s3, ddb = aws
    monkeypatch.setenv("AWS_REGION", "eu-north-1")
    assert _trigger(s3, ddb) == ar_build.OUTCOME_UNSUPPORTED_REGION
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_UNSUPPORTED_REGION

    # Second call reads the same status and must not write again.
    writes: list = []
    real_update = ddb.update_item
    monkeypatch.setattr(
        ddb, "update_item", lambda **kw: (writes.append(kw), real_update(**kw))[1]
    )
    assert _trigger(s3, ddb) == ar_build.OUTCOME_UNSUPPORTED_REGION
    assert writes == []


def test_unenrolled_dataset_is_skipped_before_any_work(aws):
    # Reasoning is opt-in: an unenrolled dataset costs one GetItem — no S3
    # walk, no model call, no writes. Broken seams prove nothing else ran.
    s3, ddb = aws
    ddb.update_item(
        TableName=TABLE,
        Key=registry_key(DOMAIN, DATASET),
        UpdateExpression="REMOVE ar_enrolled",
    )

    class BrokenS3:
        def get_paginator(self, *a, **kw):
            raise AssertionError("unenrolled dataset must not touch S3")

    outcome = ar_build.maybe_build_policy(
        data_domain=DOMAIN,
        dataset=DATASET,
        registry=(ddb, TABLE),
        s3=BrokenS3(),
        bedrock=object(),
        author=object(),
    )
    assert outcome == ar_build.OUTCOME_NOT_ENROLLED
    assert ATTR_BUILD_STATUS not in _row(ddb)


def test_dataset_without_sources_builds_nothing(aws):
    s3, ddb = aws
    for key in (
        f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        f"okf/{DOMAIN}/{DATASET}/references/enums/status.md",
    ):
        s3.delete_object(Bucket=BUCKET, Key=key)
    assert _trigger(s3, ddb) == ar_build.OUTCOME_NO_SOURCES
    assert ATTR_BUILD_STATUS not in _row(ddb)


# --- the iff-changed skip ---------------------------------------------------------


def test_unchanged_fingerprint_with_usable_policy_skips(aws):
    s3, ddb = aws
    fresh = source_hash(s3, BUCKET, DOMAIN, DATASET)
    ddb.put_item(
        TableName=TABLE,
        Item={
            **registry_key(DOMAIN, DATASET),
            "ar_enrolled": {"BOOL": True},
            ATTR_BUILD_STATUS: {"S": BUILD_READY},
            ATTR_SOURCE_HASH: {"S": fresh},
        },
    )
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_UNCHANGED
    assert author.calls == []  # zero LLM tokens on the common re-harvest path


def test_changed_fingerprint_rebuilds_even_when_ready(aws):
    s3, ddb = aws
    ddb.put_item(
        TableName=TABLE,
        Item={
            **registry_key(DOMAIN, DATASET),
            "ar_enrolled": {"BOOL": True},
            ATTR_BUILD_STATUS: {"S": BUILD_READY},
            ATTR_SOURCE_HASH: {"S": "stale-hash"},
        },
    )
    assert _trigger(s3, ddb) == ar_build.OUTCOME_STARTED


def test_in_flight_build_at_same_fingerprint_skips_before_the_flip(aws):
    s3, ddb = aws
    fresh = source_hash(s3, BUCKET, DOMAIN, DATASET)
    ddb.put_item(
        TableName=TABLE,
        Item={
            **registry_key(DOMAIN, DATASET),
            "ar_enrolled": {"BOOL": True},
            ATTR_BUILD_STATUS: {"S": BUILD_BUILDING},
            ATTR_SOURCE_HASH: {"S": fresh},
        },
    )
    assert _trigger(s3, ddb) == ar_build.OUTCOME_UNCHANGED


def test_lost_flip_race_returns_locked(aws):
    s3, ddb = aws
    # A concurrent build holds the row (different fingerprint, so the skip
    # doesn't fire) — the conditional flip is what must reject us.
    ddb.put_item(
        TableName=TABLE,
        Item={
            **registry_key(DOMAIN, DATASET),
            "ar_enrolled": {"BOOL": True},
            ATTR_BUILD_STATUS: {"S": BUILD_BUILDING},
            ATTR_SOURCE_HASH: {"S": "other"},
        },
    )
    assert _trigger(s3, ddb) == ar_build.OUTCOME_LOCKED


# --- the happy path ----------------------------------------------------------------


def test_happy_path_starts_a_build_and_stamps_the_lease(aws):
    s3, ddb = aws
    bedrock, author = FakeBedrock(), FakeAuthor()
    assert _trigger(s3, ddb, bedrock=bedrock, author=author) == ar_build.OUTCOME_STARTED

    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_BUILDING
    assert _attr(ddb, ATTR_BUILD_WORKFLOW_ID) == "wf-1"
    # The fingerprint is parked as PENDING (gather-time capture) — never as the
    # live ar_source_hash, which only a completed build may set.
    fresh = source_hash(s3, BUCKET, DOMAIN, DATASET)
    assert _attr(ddb, ATTR_PENDING_SOURCE_HASH) == fresh
    assert _attr(ddb, ATTR_SOURCE_HASH) == ""

    body = s3.get_object(
        Bucket=BUCKET, Key=ar_rules_key(DOMAIN, DATASET)
    )["Body"].read()
    assert b"usage_guardrails" in body
    assert len(author.calls) == 1
    (start,) = bedrock.started
    docs = start["sourceContent"]["workflowContent"]["documents"]
    assert len(docs) == 1 and docs[0]["documentContentType"] == "txt"


# --- in-session completion (the post-start poll) -----------------------------------


def test_build_completes_in_session_when_the_workflow_finishes(aws, monkeypatch):
    # The session that started the build is alive anyway, so it polls the
    # workflow to terminal and completes in place: staged definition applied
    # to the draft, version frozen AFTER the apply, guardrail pointed,
    # snapshot written at the pending fingerprint, row READY. No reconcile
    # schedule, no Sync click needed.
    from okf_aws import ar_policy as ap

    monkeypatch.setenv("OKF_POLICY_BUILD_WAIT_SECONDS", "60")
    s3, ddb = aws
    bedrock = FakeBedrock()  # COMPLETED on the first poll
    assert _trigger(s3, ddb, bedrock=bedrock) == ar_build.OUTCOME_COMPLETED

    (applied,) = bedrock.updated
    assert applied["policyDefinition"]["rules"] == bedrock.built_rules
    assert bedrock.versions  # frozen after the apply, not before
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_READY
    fresh = source_hash(s3, BUCKET, DOMAIN, DATASET)
    assert _attr(ddb, ATTR_SOURCE_HASH) == fresh
    # The snapshot at this fingerprint is what makes the next restore free —
    # and it records the frozen VERSION for the version-first restore.
    snap = ap.read_snapshot(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET, fingerprint=fresh
    )
    assert snap is not None
    assert snap["policy_version_arn"].endswith(":1")
    assert snap["policy_definition"]["rules"] == bedrock.built_rules


def test_poll_timeout_leaves_the_row_building_for_the_backstops(aws, monkeypatch):
    monkeypatch.setenv("OKF_POLICY_BUILD_WAIT_SECONDS", "1")
    monkeypatch.setenv("OKF_POLICY_BUILD_POLL_SECONDS", "1")
    s3, ddb = aws
    bedrock = FakeBedrock(workflow_status="BUILDING")  # never terminal
    assert _trigger(s3, ddb, bedrock=bedrock) == ar_build.OUTCOME_STARTED
    # `building` + the workflow id is exactly the state Sync / reconcile
    # complete from — nothing is lost, only delayed.
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_BUILDING
    assert _attr(ddb, ATTR_BUILD_WORKFLOW_ID) == "wf-1"
    assert bedrock.updated == []  # completion never ran


def test_failed_workflow_stamps_failed_in_session(aws, monkeypatch):
    monkeypatch.setenv("OKF_POLICY_BUILD_WAIT_SECONDS", "60")
    s3, ddb = aws
    bedrock = FakeBedrock(workflow_status="FAILED")
    assert _trigger(s3, ddb, bedrock=bedrock) == ar_build.OUTCOME_ERROR
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    assert _attr(ddb, "ar_build_detail") == "build_failed"


def test_transient_completion_error_leaves_the_row_building(aws, monkeypatch):
    # The workflow result is DURABLE: a throttle during completion must not
    # burn the build — the row stays `building` with the workflow id, and the
    # backstops retry the COMPLETION, never the build.
    monkeypatch.setenv("OKF_POLICY_BUILD_WAIT_SECONDS", "60")
    s3, ddb = aws
    bedrock = FakeBedrock()

    def _throttle(**_kw):
        raise Boto3Error("ThrottlingException")

    bedrock.get_automated_reasoning_policy = _throttle  # breaks the hash read
    assert _trigger(s3, ddb, bedrock=bedrock) == ar_build.OUTCOME_ERROR
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_BUILDING
    assert _attr(ddb, ATTR_BUILD_WORKFLOW_ID) == "wf-1"


def test_rule_free_result_releases_the_lease_as_failed(aws, monkeypatch):
    # A rule-free POLICY_DEFINITION asset is a deterministic verdict on the
    # result — retrying the completion cannot change it, so the lease must be
    # released (a row stuck at `building` could never rebuild).
    monkeypatch.setenv("OKF_POLICY_BUILD_WAIT_SECONDS", "60")
    s3, ddb = aws
    bedrock = FakeBedrock()
    bedrock.built_rules = []
    assert _trigger(s3, ddb, bedrock=bedrock) == ar_build.OUTCOME_ERROR
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    assert "no rules" in _attr(ddb, "ar_build_detail")
    assert bedrock.updated == []  # the empty result was never applied


# --- failure posture ----------------------------------------------------------------


def test_policy_cap_stamps_failed_and_releases_the_lease(aws):
    s3, ddb = aws
    bedrock = FakeBedrock(create_error="ServiceQuotaExceededException")
    assert _trigger(s3, ddb, bedrock=bedrock) == ar_build.OUTCOME_POLICY_CAP
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    # A later trigger may retry: the row is not stuck at `building`.
    assert _trigger(s3, ddb, bedrock=FakeBedrock()) == ar_build.OUTCOME_STARTED


def test_empty_rules_document_stamps_failed(aws):
    s3, ddb = aws
    assert (
        _trigger(s3, ddb, author=FakeAuthor(reply="   "))
        == ar_build.OUTCOME_NO_RULES
    )
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"


def test_any_exception_is_swallowed_and_the_lease_released(aws):
    s3, ddb = aws
    assert (
        _trigger(s3, ddb, author=FakeAuthor(reply=RuntimeError("author died")))
        == ar_build.OUTCOME_ERROR
    )
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    assert _trigger(s3, ddb) == ar_build.OUTCOME_STARTED


def test_gather_failure_never_raises(aws):
    s3, ddb = aws

    class BrokenS3:
        def list_objects_v2(self, **kw):
            raise RuntimeError("s3 down")

        def get_paginator(self, *a, **kw):
            raise RuntimeError("s3 down")

    assert _trigger(BrokenS3(), ddb) == ar_build.OUTCOME_ERROR


# --- the finalize hook ---------------------------------------------------------------


def test_finalize_triggers_the_build_after_the_marker(tmp_path, monkeypatch):
    calls: list[dict] = []

    def _record(**kw):
        # The commit marker must already be on disk when the hook runs.
        assert json.loads(
            (tmp_path / ".harvest" / "state.json").read_text()
        )["status"] == "complete"
        calls.append(kw)
        return ar_build.OUTCOME_DISABLED

    monkeypatch.setattr("harvest.finalize.maybe_build_policy", _record)
    finalize_bundle(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, tables=[], timestamp="t"
    )
    assert calls == [{"data_domain": DOMAIN, "dataset": DATASET}]


def test_finalize_skips_the_build_for_a_cross_dataset_run(tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        "harvest.finalize.maybe_build_policy", lambda **kw: calls.append(kw)
    )
    finalize_bundle(
        tmp_path,
        data_domain=DOMAIN,
        dataset=DATASET,
        tables=[],
        timestamp="t",
        extra={"cross_target": "other/target"},
    )
    assert calls == []


# --- the content-addressed restore fast path ------------------------------------


def _seed_snapshot(s3, ddb, fingerprint):
    from okf_aws import ar_policy as ap

    ap.write_snapshot(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        snapshot={
            "fingerprint": fingerprint,
            "policy_definition": {"rules": [{"id": "R1", "expression": "(=> a b)"}]},
            "grounding": {"R1": {"rule_text": "IF a THEN b",
                                  "rule_source_page": "references/usage_guardrails.md"}},
            "rules_text": "1. IF a THEN b (references/usage_guardrails.md)",
            "fidelity_coverage": 0.7,
            "fidelity_accuracy": 0.85,
            "build_status": "ready",
        },
    )


def test_snapshot_covering_the_fresh_fingerprint_restores_without_a_model(aws):
    # A previously-built source state (repromote, A->B->A edit): the EXACT
    # solver rules come back deterministically — the author never runs, no
    # Bedrock ingest starts, and the row is immediately usable (era fingerprint
    # == live wiki, so the check-time gate opens with no dark window).
    from okf_aws import ar_policy as ap

    s3, ddb = aws
    fresh = source_hash(s3, BUCKET, DOMAIN, DATASET)
    _seed_snapshot(s3, ddb, fresh)
    bedrock, author = FakeBedrock(), FakeAuthor()
    assert _trigger(s3, ddb, bedrock=bedrock, author=author) == (
        ar_build.OUTCOME_RESTORED
    )
    assert author.calls == []
    assert bedrock.started == []  # no ingest build
    (update,) = bedrock.updated
    assert update["policyDefinition"]["rules"][0]["id"] == "R1"
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_READY
    assert _attr(ddb, ATTR_SOURCE_HASH) == fresh
    body = s3.get_object(Bucket=BUCKET, Key=ar_rules_key(DOMAIN, DATASET))[
        "Body"
    ].read()
    assert b"IF a THEN b" in body
    # The restored doc is the next authoring run's diff base.
    manifest = ap.read_sources_manifest(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert manifest.get("fingerprint") == fresh


def test_restore_failure_releases_the_lease(aws):
    s3, ddb = aws
    fresh = source_hash(s3, BUCKET, DOMAIN, DATASET)
    _seed_snapshot(s3, ddb, fresh)

    class BrokenRestoreBedrock(FakeBedrock):
        def update_automated_reasoning_policy(self, **kw):
            raise RuntimeError("bedrock down")

    assert _trigger(s3, ddb, bedrock=BrokenRestoreBedrock()) == ar_build.OUTCOME_ERROR
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    assert _trigger(s3, ddb) == ar_build.OUTCOME_RESTORED  # retryable


# --- the update context ------------------------------------------------------------


def test_second_authoring_run_gets_the_prior_document_and_diff_base(aws):
    s3, ddb = aws
    first = FakeAuthor(reply="1. IF a THEN b (references/usage_guardrails.md)")
    assert _trigger(s3, ddb, author=first) == ar_build.OUTCOME_STARTED
    (call,) = first.calls
    assert call["prior_rules"] == "" and call["prior_manifest"] == {}

    # The wiki moves; the build lease from run 1 must first be released (a real
    # deployment gets there via completion/reap — tests stamp failed directly).
    from okf_aws.ar_policy import stamp_build_failed

    stamp_build_failed(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, reason="test-reset"
    )
    s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        Body=b"never sum booked and billed; horizon starts 2018",
    )
    # fetch_old must be sampled DURING authoring (that's when the agent diffs);
    # after the run, persist_author_state refreshes the diff base to the new
    # content — so capture inside the author callable.
    seen: dict = {}

    def second(**kw):
        seen.update(kw)
        seen["old_guardrails"] = kw["fetch_old"]("references/usage_guardrails.md")
        seen["old_ghost"] = kw["fetch_old"]("references/ghost.md")
        return "1. IF a THEN b2 (references/usage_guardrails.md)"

    assert _trigger(s3, ddb, author=second) == ar_build.OUTCOME_STARTED
    assert seen["prior_rules"].startswith("1. IF a THEN b")
    assert "references/usage_guardrails.md" in seen["prior_manifest"]["files"]
    # The diff base is run 1's copy, NOT the live (already-updated) wiki.
    assert seen["old_guardrails"] == b"never sum booked and billed"
    assert seen["old_ghost"] is None
