"""okf_aws.computation_run: the verification overlay round-trip and the
S3-facing loaders (the receipt/execution paths are covered end-to-end in the
consumption MCP's test_computation_tools)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from okf_aws import computation_run as cr

BUCKET = "okf-bundles"
DOMAIN, DATASET = "sales", "f1"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def test_overlay_key_is_off_mount():
    # The Verify click must NEVER land inside the mounted bundle prefix
    # (okf/...) — a raw Lambda put there materializes a root-owned path the
    # runtime's mount identity can't write (the pending.json EACCES).
    key = cr.verification_overlay_key(DOMAIN, DATASET)
    assert key == f"verification/{DOMAIN}/{DATASET}.json"
    assert not key.startswith("okf/")


def test_overlay_round_trip_and_degradation(s3):
    assert cr.load_overlay(s3, BUCKET, DOMAIN, DATASET) == {}
    entries = {
        "season_points": {
            "slug": "season_points",
            "sha256": "a" * 64,
            "verified": "2026-08-14T09:30:00Z",
            "verified_by": "analyst@example.com",
        }
    }
    cr.save_overlay(s3, BUCKET, DOMAIN, DATASET, entries)
    assert cr.load_overlay(s3, BUCKET, DOMAIN, DATASET) == entries
    # Corrupt overlay degrades to {} (unverified), never a crash.
    s3.put_object(
        Bucket=BUCKET,
        Key=cr.verification_overlay_key(DOMAIN, DATASET),
        Body=b"not json",
    )
    assert cr.load_overlay(s3, BUCKET, DOMAIN, DATASET) == {}


def test_load_computation_missing_and_traversal(s3):
    comp, errors = cr.load_computation(s3, BUCKET, DOMAIN, DATASET, "nope")
    assert comp is None and "no computation named" in errors[0]
    comp, errors = cr.load_computation(s3, BUCKET, DOMAIN, DATASET, "../x")
    assert comp is None and "invalid computation name" in errors[0]
    with pytest.raises(ValueError):
        cr.computation_doc_key(DOMAIN, DATASET, "a/b")


def test_run_computation_render_only_receipt():
    from okf_core.computations import parse_computation_text

    doc = (
        "---\ntype: Attested Computation\ntitle: T\ndescription: d\n"
        "runtime: athena\n"
        "parameters:\n"
        "  - {name: n, type: integer, required: true, example: 1}\n"
        "timestamp: t\n---\n\n# Computation\n\n```sql\nSELECT @n AS n\n```\n"
    )
    comp, errors = parse_computation_text("references/computations/t.md", doc)
    assert errors == []
    receipt = cr.run_computation(comp, {"n": 5}, execute=False)
    assert receipt["executed"] is False
    assert receipt["executed_sql"] == "SELECT 5 AS n"
    assert receipt["verification"] == "unverified"
    assert receipt["computation_sha256"] == comp.sha256
