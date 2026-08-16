"""Shared pytest fixtures: moto-backed S3 + DynamoDB, seeded registry mappings."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

BUNDLE_BUCKET = "okf-bundle-test"
REGISTRY_TABLE = "okf-registry"
FRESHNESS_TABLE = "okf-freshness"
REGION = "us-east-1"


@pytest.fixture
def aws(monkeypatch):
    """Start moto and yield the S3 client + DynamoDB resource with tables made."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUNDLE_BUCKET)

        ddb = boto3.resource("dynamodb", region_name=REGION)
        for name in (REGISTRY_TABLE, FRESHNESS_TABLE):
            ddb.create_table(
                TableName=name,
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
        yield {"s3": s3, "ddb": ddb}


def seed_mapping(ddb, *, data_domain, dataset, glue_database):
    """Insert a DATASET registry item mapping a glue database to a dataset."""
    ddb.Table(REGISTRY_TABLE).put_item(
        Item={
            "pk": f"DOMAIN#{data_domain}",
            "sk": f"DATASET#{dataset}",
            "data_domain": data_domain,
            "dataset": dataset,
            "glue_database": glue_database,
            "created_at": "2026-06-01T00:00:00+00:00",
        }
    )


def seed_ready_bundle(s3, *, data_domain, dataset, bucket=BUNDLE_BUCKET):
    """Write the .harvest/state.json completion marker so is_bundle_ready() is True.

    The incremental handler skips events for datasets whose bundle a full harvest
    hasn't authored yet; tests that exercise the re-harvest path must first mark
    the bundle complete.
    """
    import json

    from okf_aws.s3_bundle import state_marker_key

    s3.put_object(
        Bucket=bucket,
        Key=state_marker_key(data_domain, dataset),
        Body=json.dumps(
            {"status": "complete", "data_domain": data_domain, "dataset": dataset}
        ).encode("utf-8"),
    )
