"""Shared fixtures: moto-backed S3 + DynamoDB.

Constants and event builders live in tests/fakes.py (importable); this module
only holds pytest fixtures.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from fakes import BUNDLE_BUCKET, FRESHNESS_TABLE, REGION, REGISTRY_TABLE


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def aws(_aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUNDLE_BUCKET)

        ddb = boto3.resource("dynamodb", region_name=REGION)
        for name in (FRESHNESS_TABLE, REGISTRY_TABLE):
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
