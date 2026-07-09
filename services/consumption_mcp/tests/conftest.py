"""Shared fixtures: moto S3 + DynamoDB, a synthetic F1-like bundle, and a
wired ConsumptionTools instance with fake s3vectors/bedrock clients.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

from .fakes import FakeBedrock, FakeS3Vectors

REGION = "us-east-1"
BUNDLE_BUCKET = "okf-bundles"
REGISTRY_TABLE = "okf-registry"

DOMAIN = "sales"
DATASET = "f1"

# A small synthetic bundle: an index, a dataset doc, two table docs. The
# references/joins doc links to both tables (so it is a backlink source), and
# tables/results links to tables/races.
_BUNDLE_FILES = {
    f"okf/{DOMAIN}/{DATASET}/index.md": (
        "---\ntype: Index\ntitle: F1\ndescription: root\ntimestamp: t\n---\n\n"
        "# F1 bundle\n\nSee [races](tables/races.md).\n"
    ),
    f"okf/{DOMAIN}/{DATASET}/datasets/f1.md": (
        "---\ntype: Glue Database\ntitle: F1 DB\ndescription: db\ntimestamp: t\n---\n\n"
        "# Overview\n\nThe F1 curated database.\n"
    ),
    f"okf/{DOMAIN}/{DATASET}/tables/races.md": (
        "---\ntype: Glue Table\ntitle: Races\ndescription: race rows\ntimestamp: t\n---\n\n"
        "# Overview\n\nRaces table.\n\n"
        + "\n".join(f"line {i}" for i in range(1, 21))
        + "\n"
    ),
    f"okf/{DOMAIN}/{DATASET}/tables/results.md": (
        "---\ntype: Glue Table\ntitle: Results\ndescription: result rows\ntimestamp: t\n---\n\n"
        "# Overview\n\nResults table.\n\n"
        "## Joins\n\nJoins to [races](races.md) on raceid.\n"
    ),
    f"okf/{DOMAIN}/{DATASET}/references/joins/races__results.md": (
        "---\ntype: Reference\ntitle: races-results join\ndescription: j\ntimestamp: t\n---\n\n"
        "# Detail\n\nJoin [races](../../tables/races.md) to "
        "[results](../../tables/results.md).\n"
    ),
    # dot-prefixed + reserved artifacts that MUST be ignored.
    f"okf/{DOMAIN}/{DATASET}/.harvest/state.json": '{"status": "complete"}',
    f"okf/{DOMAIN}/{DATASET}/.context/source.md": "secret source doc",
    f"okf/{DOMAIN}/{DATASET}/tables/index.md": (
        "---\ntype: Index\ntitle: Tables\ndescription: tables\ntimestamp: t\n---\n\n"
        "# Tables\n\n- [races](races.md)\n- [results](results.md)\n"
    ),
}


@pytest.fixture
def aws():
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUNDLE_BUCKET)
        for key, body in _BUNDLE_FILES.items():
            s3.put_object(Bucket=BUNDLE_BUCKET, Key=key, Body=body.encode())

        ddb_resource = boto3.resource("dynamodb", region_name=REGION)
        table = ddb_resource.create_table(
            TableName=REGISTRY_TABLE,
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
        table.wait_until_exists()
        # Domain mappings.
        table.put_item(
            Item={
                "pk": f"DOMAIN#{DOMAIN}",
                "sk": f"DATASET#{DATASET}",
                "data_domain": DOMAIN,
                "dataset": DATASET,
                "glue_database": "na_mi_formula_1_curated",
                "created_at": "t",
            }
        )
        table.put_item(
            Item={
                "pk": "DOMAIN#ops",
                "sk": "DATASET#logs",
                "data_domain": "ops",
                "dataset": "logs",
                "glue_database": "ops_logs",
                "created_at": "t",
            }
        )
        # A non-domain item that must NOT appear in list_domains.
        table.put_item(
            Item={
                "pk": f"HARVEST#{DOMAIN}#{DATASET}",
                "sk": "STATUS",
                "status": "complete",
            }
        )
        yield {"s3": s3, "table": table}


@pytest.fixture
def config():
    return ConsumptionConfig(
        bundle_bucket=BUNDLE_BUCKET,
        vector_bucket="okf-vectors",
        vector_index="okf-index",
        registry_table=REGISTRY_TABLE,
    )


@pytest.fixture
def tools(aws, config):
    return ConsumptionTools(
        s3=aws["s3"],
        s3vectors=FakeS3Vectors(),
        bedrock_runtime=FakeBedrock(),
        ddb=aws["table"],
        config=config,
    )
