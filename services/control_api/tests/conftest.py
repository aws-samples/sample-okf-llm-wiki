"""Shared pytest fixtures: moto-backed S3 + DynamoDB and a wired Config."""

from __future__ import annotations

import os
import sys

# Make ``tests`` importable as a top-level package (``from tests.fakes import``)
# no matter which directory pytest is launched from — the service root is the
# parent of this ``tests/`` dir. Mirrors the harvest service's test layout.
_SVC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SVC_ROOT not in sys.path:
    sys.path.insert(0, _SVC_ROOT)

import boto3
import pytest
from moto import mock_aws

from control_api.app import Config
from tests.fakes import FakeAgentCore, FakeCognito, FakeGlue, FakeLogs

REGION = "us-east-1"
BUCKET = "okf-bundle-test"
REGISTRY = "okf-registry"
FRESHNESS = "okf-freshness"
HARVEST_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/harvest-abc"
USER_POOL_ID = "us-east-1_testpool"
MCP_SCOPE = "okf-mcp/invoke"
HARVEST_LOG_GROUP = "/aws/bedrock-agentcore/runtimes/harvest-abc-DEFAULT"


@pytest.fixture
def aws():
    """Bring up moto S3 + DynamoDB with the two OKF tables and the bundle bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name in (REGISTRY, FRESHNESS):
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


@pytest.fixture
def glue():
    # ``orders`` mirrors the dataset name used across the router tests: a dataset
    # is registered/harvested against a Glue database of the SAME name, so the
    # existence check in the upsert/harvest adapters needs it to be present.
    return FakeGlue(
        [
            {"Name": "sales_curated", "Description": "sales"},
            {"Name": "f1_curated", "Description": None},
            {"Name": "orders", "Description": "orders"},
        ]
    )


@pytest.fixture
def agentcore():
    return FakeAgentCore()


@pytest.fixture
def cognito():
    return FakeCognito()


@pytest.fixture
def logs():
    return FakeLogs()


@pytest.fixture
def cfg(aws, glue, agentcore, cognito, logs):
    return Config(
        bucket=BUCKET,
        registry_table=REGISTRY,
        freshness_table=FRESHNESS,
        harvest_runtime_arn=HARVEST_ARN,
        s3=aws["s3"],
        ddb=aws["ddb"],
        glue=glue,
        agentcore=agentcore,
        cognito=cognito,
        user_pool_id=USER_POOL_ID,
        mcp_scope=MCP_SCOPE,
        logs=logs,
        harvest_log_group=HARVEST_LOG_GROUP,
    )
