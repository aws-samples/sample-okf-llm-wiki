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


def test_enrollment_round_trip_and_full_cleanup(aws):
    _, ddb = aws
    assert not ap.is_enrolled(_row(ddb))
    ap.set_enrolled(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)
    assert ap.is_enrolled(_row(ddb))
    ap.try_flip_building(
        ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, pending_hash="h"
    )
    ap.stamp_ready(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET, fingerprint="h")
    # Every attribute the feature stamps must be in the cleanup set — a stamp
    # outside AR_ROW_ATTRS would survive unenrollment as a zombie.
    stamped = {k for k in _row(ddb) if k.startswith("ar_")}
    assert stamped <= set(ap.AR_ROW_ATTRS)
    ap.clear_ar_attrs(ddb, TABLE, data_domain=DOMAIN, dataset=DATASET)
    row = _row(ddb)
    assert not any(k.startswith("ar_") for k in row)
    assert row.get("data_domain")  # the mapping row itself survives
    assert not ap.is_enrolled(row)


def test_author_prompt_speaks_yaml_and_id_stability():
    assert "policies:" in ap.POLICY_AUTHOR_PROMPT
    assert "STABLE" in ap.POLICY_AUTHOR_PROMPT
    assert "condition" in ap.POLICY_AUTHOR_PROMPT
    assert "action" in ap.POLICY_AUTHOR_PROMPT
    # Selectivity guidance survived the pivot.
    assert "BE SELECTIVE" in ap.POLICY_AUTHOR_PROMPT
