"""The policy authoring trigger: gates, the lease, persistence, and stamps.

v2 (LLM-judge era): authoring IS completion — the pipeline is gather →
fingerprint-skip → flip → author → persist → stamp ready. moto provides
S3/DynamoDB; the author is an injected callable.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from harvest import ar_build
from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_PENDING_SOURCE_HASH,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    BUILD_READY,
    policy_doc_key,
    read_policy_doc,
    read_sources_manifest,
    registry_key,
    source_hash,
)

DOMAIN = "sport"
DATASET = "formula_1"
BUCKET = "okf-bundles"
TABLE = "okf-registry"

GOOD_DOC = """\
policies:
  - id: P001
    type: behavioural
    condition: figures are requested from an empty result
    action: never state figures derived from that query
    source: references/usage_guardrails.md
"""


class FakeAuthor:
    """The document-author seam: records every call, returns the scripted doc."""

    def __init__(self, reply=GOOD_DOC):
        self.reply = reply
        self.calls: list[dict] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else self.reply(**kw)


@pytest.fixture()
def aws(monkeypatch):
    """moto S3 + DynamoDB, a seeded bundle, the flag ON, and the env wired."""
    monkeypatch.setenv("OKF_POLICY_BUILD_ENABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", BUCKET)
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
        # conditioned on it. Enrolled: reasoning is per-dataset OPT-IN.
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


def _trigger(s3, ddb, *, author=None):
    return ar_build.maybe_build_policy(
        data_domain=DOMAIN,
        dataset=DATASET,
        registry=(ddb, TABLE),
        s3=s3,
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
    # touched — passing no seams at all proves it.
    monkeypatch.delenv("OKF_POLICY_BUILD_ENABLED", raising=False)
    assert (
        ar_build.maybe_build_policy(data_domain="d", dataset="s")
        == ar_build.OUTCOME_DISABLED
    )


# --- gates ---------------------------------------------------------------------


def test_unenrolled_dataset_costs_one_get_item(aws):
    s3, ddb = aws
    ddb.update_item(
        TableName=TABLE,
        Key=registry_key(DOMAIN, DATASET),
        UpdateExpression="REMOVE ar_enrolled",
    )

    class BrokenS3:
        def __getattr__(self, name):
            raise AssertionError("an unenrolled dataset must never touch S3")

    author = FakeAuthor()
    out = ar_build.maybe_build_policy(
        data_domain=DOMAIN, dataset=DATASET,
        registry=(ddb, TABLE), s3=BrokenS3(), author=author,
    )
    assert out == ar_build.OUTCOME_NOT_ENROLLED
    assert author.calls == []


def test_no_sources_is_a_clean_no_op(aws):
    s3, ddb = aws
    for key in (
        f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        f"okf/{DOMAIN}/{DATASET}/references/enums/status.md",
    ):
        s3.delete_object(Bucket=BUCKET, Key=key)
    assert _trigger(s3, ddb) == ar_build.OUTCOME_NO_SOURCES
    assert _attr(ddb, ATTR_BUILD_STATUS) == ""


def test_unchanged_fingerprint_skips_when_the_document_exists(aws):
    s3, ddb = aws
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_UNCHANGED
    assert len(author.calls) == 1  # the re-harvest cost zero model calls


def test_ready_row_without_its_document_reauthors(aws):
    # The self-heal for datasets stamped ready before v2 (or a lost write):
    # a matching fingerprint is NOT enough — the artifact must exist.
    s3, ddb = aws
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED
    s3.delete_object(Bucket=BUCKET, Key=policy_doc_key(DOMAIN, DATASET))
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert len(author.calls) == 1
    assert read_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    ) == GOOD_DOC.strip() or read_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )


def test_building_row_locks_out_a_second_run(aws):
    s3, ddb = aws
    ddb.update_item(
        TableName=TABLE,
        Key=registry_key(DOMAIN, DATASET),
        UpdateExpression="SET ar_build_status = :b",
        ExpressionAttributeValues={":b": {"S": BUILD_BUILDING}},
    )
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_LOCKED
    assert author.calls == []


# --- the happy paths -----------------------------------------------------------------


def test_full_authoring_persists_everything_and_stamps_ready(aws):
    s3, ddb = aws
    author = FakeAuthor()
    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED

    (call,) = author.calls
    assert call["prior_doc"] == ""  # first run: nothing to update
    assert call["prior_manifest"] == {}
    assert [rel for rel, _ in call["sources"]] == [
        "references/enums/status.md",
        "references/usage_guardrails.md",
    ]

    assert read_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    ).startswith("policies:")
    manifest = read_sources_manifest(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert set(manifest["files"]) == {
        "references/enums/status.md",
        "references/usage_guardrails.md",
    }
    fresh = source_hash(s3, BUCKET, DOMAIN, DATASET)
    assert _attr(ddb, ATTR_SOURCE_HASH) == fresh
    assert _attr(ddb, ATTR_BUILD_STATUS) == BUILD_READY


def test_update_run_hands_the_author_the_diff_base(aws):
    s3, ddb = aws
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED

    # The wiki moves; the next run must carry the PRIOR doc, the PRIOR
    # manifest, and a fetch_old that returns the OLD copy — sampled DURING
    # authoring, before persist refreshes the diff base.
    s3.put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        Body=b"NEW guardrails text",
    )
    seen: dict = {}

    def author(**kw):
        seen["prior_doc"] = kw["prior_doc"]
        seen["old_copy"] = kw["fetch_old"]("references/usage_guardrails.md")
        return GOOD_DOC

    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert seen["prior_doc"].startswith("policies:")
    assert seen["old_copy"] == b"never sum booked and billed"


def test_stamp_carries_the_gather_time_fingerprint(aws):
    # The wiki mutates while the author runs: the stamp must describe what was
    # AUTHORED FROM (stale on arrival), never the post-mutation state.
    s3, ddb = aws
    gathered = source_hash(s3, BUCKET, DOMAIN, DATASET)

    def author(**kw):
        s3.put_object(
            Bucket=BUCKET,
            Key=f"okf/{DOMAIN}/{DATASET}/references/enums/status.md",
            Body=b"mutated mid-authoring",
        )
        return GOOD_DOC

    assert _trigger(s3, ddb, author=author) == ar_build.OUTCOME_AUTHORED
    assert _attr(ddb, ATTR_SOURCE_HASH) == gathered
    assert _attr(ddb, ATTR_SOURCE_HASH) != source_hash(s3, BUCKET, DOMAIN, DATASET)


# --- failure posture -----------------------------------------------------------------


def test_empty_document_stamps_failed_and_releases_the_lease(aws):
    s3, ddb = aws
    assert _trigger(s3, ddb, author=FakeAuthor(reply="   ")) == ar_build.OUTCOME_NO_RULES
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    assert _attr(ddb, "ar_build_detail") == "no_rules"
    # Retryable: the lease was released, a later trigger authors normally.
    assert _trigger(s3, ddb) == ar_build.OUTCOME_AUTHORED


def test_author_exception_stamps_failed_with_the_reason(aws):
    s3, ddb = aws
    out = _trigger(s3, ddb, author=FakeAuthor(reply=RuntimeError("model down")))
    assert out == ar_build.OUTCOME_ERROR
    assert _attr(ddb, ATTR_BUILD_STATUS) == "failed"
    assert "model down" in _attr(ddb, "ar_build_detail")


def test_pending_hash_is_parked_at_flip_time(aws):
    s3, ddb = aws
    seen: dict = {}

    def author(**kw):
        seen["pending"] = _attr(ddb, ATTR_PENDING_SOURCE_HASH)
        seen["status"] = _attr(ddb, ATTR_BUILD_STATUS)
        return GOOD_DOC

    _trigger(s3, ddb, author=author)
    assert seen["status"] == BUILD_BUILDING
    assert seen["pending"] == _attr(ddb, ATTR_SOURCE_HASH)
