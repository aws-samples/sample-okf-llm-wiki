"""Judge-era ar_policy invariants: artifacts, author state, gathering, stamps.

The module's Bedrock AR half was removed in the v2 pivot (LLM-judge engine);
what remains — and what these tests pin — is the S3 artifact layout, the
author-state diff base, source gathering/fingerprinting, and the registry
row lease + stamps that serialize authoring runs.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from okf_aws import ar_policy as ap

DOMAIN = "sport"
DATASET = "formula_1"
BUCKET = "okf-bundles"
TABLE = "okf-registry"


@pytest.fixture()
def aws():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
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
        ddb.put_item(
            TableName=TABLE,
            Item={**ap.registry_key(DOMAIN, DATASET), "data_domain": {"S": DOMAIN}},
        )
        yield s3, ddb


def _row(ddb) -> dict:
    return ddb.get_item(
        TableName=TABLE, Key=ap.registry_key(DOMAIN, DATASET)
    ).get("Item", {})


def _attr(ddb, name: str) -> str:
    return (_row(ddb).get(name) or {}).get("S", "")


# --- artifacts -------------------------------------------------------------------


def test_policy_doc_round_trip(aws):
    s3, _ = aws
    key = ap.put_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        doc_text="policies:\n  - id: P001\n",
    )
    assert key == f"policy/{DOMAIN}/{DATASET}/policies.yaml"
    assert ap.read_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    ) == "policies:\n  - id: P001\n"
    # Absent artifact is a normal state, never an error.
    assert (
        ap.read_policy_doc(s3, bucket=BUCKET, data_domain=DOMAIN, dataset="none")
        is None
    )


def test_persist_author_state_writes_doc_copies_and_manifest(aws):
    s3, _ = aws
    sources = [
        ("references/enums/status.md", b"-1 means unknown"),
        ("references/usage_guardrails.md", b"never sum booked and billed"),
    ]
    ap.persist_author_state(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        sources=sources, doc_text="policies: []\n",
    )
    assert ap.read_policy_doc(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    ) == "policies: []\n"
    manifest = ap.read_sources_manifest(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    )
    assert set(manifest["files"]) == {
        "references/enums/status.md", "references/usage_guardrails.md",
    }
    assert manifest["fingerprint"] == ap.hash_sources(sources)
    assert ap.read_source_copy(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        rel_path="references/enums/status.md",
    ) == b"-1 means unknown"
    assert ap.read_source_copy(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        rel_path="references/never_seen.md",
    ) is None


# --- source gathering --------------------------------------------------------------


def _seed_bundle(s3):
    prefix = f"okf/{DOMAIN}/{DATASET}/"
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{prefix}references/usage_guardrails.md",
        Body=b"never sum booked and billed",
    )
    s3.put_object(
        Bucket=BUCKET, Key=f"{prefix}references/enums/status.md", Body=b"-1 unknown"
    )
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}tables/races.md", Body=b"not a source")


def test_gather_sources_filters_and_sorts(aws):
    s3, _ = aws
    _seed_bundle(s3)
    pairs = ap.gather_sources(s3, BUCKET, DOMAIN, DATASET)
    assert [rel for rel, _ in pairs] == [
        "references/enums/status.md",
        "references/usage_guardrails.md",
    ]


def test_hash_sources_none_on_empty_and_stable_on_content(aws):
    s3, _ = aws
    assert ap.hash_sources([]) is None
    assert ap.source_hash(s3, BUCKET, DOMAIN, DATASET) is None  # nothing seeded
    _seed_bundle(s3)
    first = ap.source_hash(s3, BUCKET, DOMAIN, DATASET)
    assert first and first == ap.source_hash(s3, BUCKET, DOMAIN, DATASET)


# --- the usability gate --------------------------------------------------------------


@pytest.mark.parametrize(
    "status,stored,live,expected",
    [
        ("ready", "h1", "h1", True),
        ("ready", "h1", "h2", False),  # wiki moved: only the latest state is truth
        ("building", "h1", "h1", False),
        ("failed", "h1", "h1", False),
        ("stale", "h1", "h1", False),
        ("ready", "", "h1", False),
        ("ready", "h1", None, False),
    ],
)
def test_policy_usable(status, stored, live, expected):
    assert (
        ap.policy_usable(build_status=status, stored_hash=stored, live_hash=live)
        is expected
    )


# --- registry stamps ------------------------------------------------------------------


def test_flip_building_is_the_serialization_point(aws):
    _, ddb = aws
    assert ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h1"
    )
    assert _attr(ddb, ap.ATTR_BUILD_STATUS) == ap.BUILD_BUILDING
    assert _attr(ddb, ap.ATTR_PENDING_SOURCE_HASH) == "h1"
    assert _attr(ddb, "ar_build_started_at")
    # Second claimant loses; N duplicate triggers collapse to one run.
    assert not ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h2"
    )
    # An absent mapping row can never be resurrected by a flip.
    assert not ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset="ghost", pending_hash="h1"
    )


def test_stamp_ready_carries_the_pending_fingerprint_verbatim(aws):
    _, ddb = aws
    ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="gathered"
    )
    assert (
        ap.stamp_ready(
            ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, fingerprint="gathered"
        )
        == ap.BUILD_READY
    )
    # The stored hash describes what was AUTHORED FROM, so a wiki that moved
    # mid-authoring yields a stale-on-arrival document, never a mislabelled one.
    assert _attr(ddb, ap.ATTR_SOURCE_HASH) == "gathered"
    assert _attr(ddb, ap.ATTR_BUILD_STATUS) == ap.BUILD_READY
    assert _attr(ddb, "ar_built_at")


def test_stamp_failed_records_the_reason(aws):
    _, ddb = aws
    assert (
        ap.stamp_build_failed(
            ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, reason="x" * 600
        )
        == ap.BUILD_FAILED
    )
    assert len(_attr(ddb, "ar_build_detail")) <= 512


def test_stamps_never_resurrect_a_deleted_row(aws):
    _, ddb = aws
    ddb.delete_item(TableName=TABLE, Key=ap.registry_key(DOMAIN, DATASET))
    with pytest.raises(Exception):
        ap.stamp_ready(
            ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, fingerprint="h"
        )
    assert _row(ddb) == {}


def test_flag_stale_only_moves_usable_rows(aws):
    _, ddb = aws
    # Nothing authored yet: nothing to stale.
    assert not ap.flag_stale(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)
    ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
    )
    # An in-flight authoring run must not be clobbered.
    assert not ap.flag_stale(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)
    ap.stamp_ready(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, fingerprint="h")
    assert ap.flag_stale(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)
    assert _attr(ddb, ap.ATTR_BUILD_STATUS) == ap.BUILD_STALE
    # Idempotent re-flag.
    assert ap.flag_stale(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)
    # A failed row has nothing to invalidate.
    ap.stamp_build_failed(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, reason="r")
    assert not ap.flag_stale(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)


def test_lifecycle_begun_reads_the_build_status(aws):
    # The single "has this dataset started the policy lifecycle?" reader: the
    # nightly reconcile's no-backfill skip and the chat check's dataset
    # discovery both key off exactly this shape.
    _, ddb = aws
    assert not ap.lifecycle_begun(None)
    assert not ap.lifecycle_begun({})
    assert not ap.lifecycle_begun(_row(ddb))  # registered, never authored
    ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
    )
    assert ap.lifecycle_begun(_row(ddb))
    ap.stamp_ready(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, fingerprint="h")
    assert ap.lifecycle_begun(_row(ddb))
    ap.stamp_build_failed(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, reason="r")
    assert ap.lifecycle_begun(_row(ddb))  # failed still counts: it retries


def test_author_prompt_speaks_yaml_and_id_stability():
    assert "policies:" in ap.POLICY_AUTHOR_PROMPT
    assert "STABLE" in ap.POLICY_AUTHOR_PROMPT
    assert "condition" in ap.POLICY_AUTHOR_PROMPT
    assert "action" in ap.POLICY_AUTHOR_PROMPT
    # Selectivity guidance survived the pivot.
    assert "BE SELECTIVE" in ap.POLICY_AUTHOR_PROMPT


# --- the guardrails build lock (bundle-writing work defers to a live author) -----


def test_build_lock_active_only_for_a_fresh_building_row(aws):
    _, ddb = aws
    # Registered, never authored — free.
    assert not ap.build_lock_active(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)
    # A fresh `building` flip (stamps ar_build_started_at = now) holds it.
    ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
    )
    assert ap.build_lock_active(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)
    # Terminal states free it again.
    ap.stamp_ready(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, fingerprint="h")
    assert not ap.build_lock_active(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)


def test_build_lock_goes_stale_after_the_escape_window(aws):
    from datetime import datetime, timedelta, timezone

    _, ddb = aws
    ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
    )
    # Backdate the flip past the escape window — an abandoned `building` row
    # (dead author) must not wedge harvests until the reconcile reaps it.
    old = (
        datetime.now(timezone.utc)
        - timedelta(seconds=ap.BUILD_LOCK_STALE_SECONDS + 60)
    ).isoformat(timespec="seconds")
    ddb.update_item(
        TableName=TABLE,
        Key=ap.registry_key(DOMAIN, DATASET),
        UpdateExpression="SET ar_build_started_at = :t",
        ExpressionAttributeValues={":t": {"S": old}},
    )
    assert not ap.build_lock_active(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)


def test_build_lock_fails_open_on_read_errors():
    class _Boom:
        def get_item(self, **kw):
            raise RuntimeError("ddb down")

    assert not ap.build_lock_active(
        _Boom(), TABLE, data_domain=DOMAIN, dataset=DATASET
    )


def test_flip_building_takes_over_a_stale_building_row(aws):
    from datetime import datetime, timedelta, timezone

    _, ddb = aws
    assert ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h1"
    )
    # A FRESH building row still wins against a second flip.
    assert not ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h2"
    )
    # Past the escape window the row is a dead author: readers already treat
    # it as free, so the flip must take it over too (asymmetry would make
    # every post-harvest build silently lose to the corpse until the reap).
    old = (
        datetime.now(timezone.utc)
        - timedelta(seconds=ap.BUILD_LOCK_STALE_SECONDS + 60)
    ).isoformat(timespec="seconds")
    ddb.update_item(
        TableName=TABLE,
        Key=ap.registry_key(DOMAIN, DATASET),
        UpdateExpression="SET ar_build_started_at = :t",
        ExpressionAttributeValues={":t": {"S": old}},
    )
    assert ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h3"
    )
    assert _attr(ddb, ap.ATTR_PENDING_SOURCE_HASH) == "h3"


# --- rules-schema sidecar + attestation overlay -------------------------------------


def test_rules_schema_roundtrip_and_absence(aws):
    s3, _ddb = aws
    assert (
        ap.read_rules_schema(s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET)
        is None
    )
    databases = {"f1": {"Results": ["RaceId", "points"]}}
    key = ap.put_rules_schema(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET, databases=databases
    )
    assert key == f"policy/{DOMAIN}/{DATASET}/rules_schema.json"
    # Read side lowercases everything (the evaluator's namespace).
    assert ap.read_rules_schema(
        s3, bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
    ) == {"f1": {"results": ["raceid", "points"]}}


def test_read_rules_schema_raises_on_transient_failures():
    # Only a genuinely MISSING object reads None — folding a throttle/5xx
    # into None made the chat trace misdiagnose an outage as "never
    # authored, re-author to fix", which no re-authoring can fix.
    class _Throttling:
        def get_object(self, **kw):
            raise RuntimeError("throttled")

    with pytest.raises(RuntimeError, match="throttled"):
        ap.read_rules_schema(
            _Throttling(), bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET
        )
