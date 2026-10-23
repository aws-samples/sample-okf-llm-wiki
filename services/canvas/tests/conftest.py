"""Shared fixtures: moto S3 + DynamoDB, a seeded bundle with one computation,
and a FakeAthena — the canvas runtime's whole AWS surface."""

from __future__ import annotations

import os
import sys

_SVC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SVC_ROOT not in sys.path:
    sys.path.insert(0, _SVC_ROOT)

import boto3
import pytest
from moto import mock_aws

BUCKET = "okf-bundle-test"
REGISTRY = "okf-registry"
CANVAS = "okf-canvas"
DOMAIN, DATASET = "sales", "orders"
SUB = "user-1"

COMP_DOC = (
    "---\n"
    "type: Attested Computation\n"
    "title: Revenue for a region\n"
    "description: Recognized revenue for one region.\n"
    "runtime: athena\n"
    "parameters:\n"
    '  - {name: region, type: string, required: true, example: "EMEA",\n'
    "     enum: [EMEA, NA]}\n"
    "verified: null\n"
    "verified_by: null\n"
    "timestamp: t\n"
    "---\n\n"
    "Reads orders.\n\n"
    "# Computation\n\n"
    "```sql\nSELECT region AS label, SUM(amount) AS revenue FROM orders\n"
    "WHERE region = @region GROUP BY region\n```\n"
)


class FakeAthena:
    """Succeeds immediately with a two-column, one-row result."""

    def __init__(self, rows=None):
        self.started = []
        self.rows = rows if rows is not None else [["EMEA", "42.5"]]

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}, "Statistics": {}}}

    def get_query_results(self, **kwargs):
        header = [{"VarCharValue": "label"}, {"VarCharValue": "revenue"}]
        return {
            "ResultSet": {
                "ResultSetMetadata": {
                    "ColumnInfo": [{"Label": "label"}, {"Label": "revenue"}]
                },
                "Rows": [{"Data": header}]
                + [
                    {"Data": [{"VarCharValue": str(v)} for v in row]}
                    for row in self.rows
                ],
            }
        }


@pytest.fixture
def aws():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(
            Bucket=BUCKET,
            Key=f"okf/{DOMAIN}/{DATASET}/references/computations/revenue_by_region.md",
            Body=COMP_DOC.encode(),
        )
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        for name in (REGISTRY, CANVAS):
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
        ddb.put_item(
            TableName=REGISTRY,
            Item={
                "pk": {"S": f"DOMAIN#{DOMAIN}"},
                "sk": {"S": f"DATASET#{DATASET}"},
                "glue_database": {"S": "shop"},
            },
        )
        yield {"s3": s3, "ddb": ddb}


@pytest.fixture
def consumption(aws):
    from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

    # ConsumptionTools takes a RESOURCE-style Table (plain-value keys), unlike
    # the low-level client the row helpers use.
    registry = boto3.resource("dynamodb", region_name="us-east-1").Table(REGISTRY)
    return ConsumptionTools(
        s3=aws["s3"],
        s3vectors=None,
        bedrock_runtime=None,
        ddb=registry,
        config=ConsumptionConfig(
            bundle_bucket=BUCKET,
            vector_bucket="",
            vector_index="",
            registry_table=REGISTRY,
            computations_enabled=True,
        ),
        athena=FakeAthena(),
    )


@pytest.fixture
def tool_ctx(aws, consumption):
    from okf_aws import computation_run

    from canvas.tools import ToolContext

    def load_computation(slug):
        comp, _ = computation_run.load_computation(
            aws["s3"], BUCKET, DOMAIN, DATASET, slug
        )
        return comp

    def execute(comp, values):
        # The runner's object-based executor, on the same fakes: bundle
        # computations AND synthesized ad-hoc tiles run identically.
        receipt = computation_run.run_computation(
            comp, values, athena=FakeAthena(),
            source={"type": "glue", "glue_database": "shop"}, execute=True,
        )
        if not receipt.get("executed"):
            raise RuntimeError(receipt.get("note") or "execution refused")
        return {"columns": receipt["columns"], "rows": receipt["rows"]}

    return ToolContext(
        ddb=aws["ddb"],
        canvas_table=CANVAS,
        user_sub=SUB,
        data_domain=DOMAIN,
        dataset=DATASET,
        consumption=consumption,
        load_computation=load_computation,
        execute=execute,
    )


VALID_CONFIG = {
    "canvas_version": 1,
    "title": "Revenue",
    "data_domain": DOMAIN,
    "dataset": DATASET,
    "dials": [{"name": "region", "label": "Region", "default": "EMEA"}],
    "tiles": [
        {
            "id": "rev",
            "title": "Revenue by region",
            "widget": "bar",
            "data": {
                "computation": "revenue_by_region",
                "bind": {"region": {"dial": "region"}},
            },
            "encoding": {
                "x": {"field": "label"},
                "y": {"field": "revenue", "format": "currency:EUR"},
            },
            "layout": {"x": 0, "y": 0, "w": 6, "h": 4},
        }
    ],
}
