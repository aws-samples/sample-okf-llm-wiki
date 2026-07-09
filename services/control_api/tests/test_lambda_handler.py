"""lambda_handler wiring: builds clients from env, routes, maps failures to 500."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from control_api import app
from tests.conftest import BUCKET, FRESHNESS, HARVEST_ARN, REGION, REGISTRY
from tests.fakes import FakeGlue


def _fake_glue_client(real_client, *databases):
    """Wrap ``boto3.client`` so ``client("glue", ...)`` returns a FakeGlue.

    Every other service ("s3", "dynamodb", ...) is delegated to the real
    (moto-backed) factory unchanged.
    """

    def factory(service, *args, **kwargs):
        if service == "glue":
            return FakeGlue(list(databases))
        return real_client(service, *args, **kwargs)

    return factory


def test_lambda_handler_missing_bucket_env_500(monkeypatch):
    # OKF_BUNDLE_BUCKET is required with no default -> config build fails -> 500.
    monkeypatch.delenv("OKF_BUNDLE_BUCKET", raising=False)
    resp = app.lambda_handler(
        {"rawPath": "/domains", "requestContext": {"http": {"method": "GET"}}}
    )
    assert resp["statusCode"] == 500


def test_lambda_handler_end_to_end_with_env(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=REGISTRY,
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

        # Glue is faked in this repo (moto covers only S3 + DynamoDB), so make
        # the client that Config.from_env builds return our in-memory FakeGlue.
        # The dataset name is the Glue database name and the upsert adapter
        # verifies it exists, so the fake must know about "orders".
        monkeypatch.setattr(
            boto3, "client", _fake_glue_client(boto3.client, {"Name": "orders"})
        )

        monkeypatch.setenv("AWS_REGION", REGION)
        monkeypatch.setenv("OKF_BUNDLE_BUCKET", BUCKET)
        monkeypatch.setenv("OKF_REGISTRY_TABLE", REGISTRY)
        monkeypatch.setenv("OKF_FRESHNESS_TABLE", FRESHNESS)
        monkeypatch.setenv("OKF_HARVEST_RUNTIME_ARN", HARVEST_ARN)

        # Declare the domain first (required before mapping).
        declare_event = {
            "rawPath": "/domain-defs/sales",
            "requestContext": {"http": {"method": "PUT"}},
            "body": json.dumps({"description": "test", "context": ""}),
        }
        declare_resp = app.lambda_handler(declare_event)
        assert declare_resp["statusCode"] == 200

        event = {
            "rawPath": "/domains/sales/datasets/orders",
            "requestContext": {"http": {"method": "PUT"}},
            "body": json.dumps({"glue_database": "orders"}),
        }
        resp = app.lambda_handler(event)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["glue_database"] == "orders"

        # Verify it persisted through the real (moto) client the handler built.
        item = ddb.get_item(
            TableName=REGISTRY,
            Key={"pk": {"S": "DOMAIN#sales"}, "sk": {"S": "DATASET#orders"}},
        )["Item"]
        assert item["glue_database"]["S"] == "orders"
