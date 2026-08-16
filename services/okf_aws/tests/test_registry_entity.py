"""The by-entity GSI read protocol: readiness marker + shared Query helpers.

The load-bearing property: readers may trust the index ONLY once the backfill
marker exists — a GSI over a partially-stamped registry Queries back a partial
catalog, and a result-shape heuristic ("non-empty means complete") silently
hides every pre-index row the moment ONE fresh row is stamped.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from okf_aws import registry_entity as re_

REGION = "us-east-1"
TABLE = "okf-registry"


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "entity", "AttributeType": "S"},
                {"AttributeName": "pair", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": re_.INDEX_NAME,
                    "KeySchema": [
                        {"AttributeName": "entity", "KeyType": "HASH"},
                        {"AttributeName": "pair", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _mapping(client, domain: str, dataset: str, *, stamped: bool = True) -> None:
    item = {
        "pk": {"S": f"DOMAIN#{domain}"},
        "sk": {"S": f"DATASET#{dataset}"},
        "data_domain": {"S": domain},
        "dataset": {"S": dataset},
    }
    if stamped:
        item["entity"] = {"S": re_.ENTITY_DATASET}
        item["pair"] = {"S": re_.entity_pair(domain, dataset)}
    client.put_item(TableName=TABLE, Item=item)


def test_index_not_ready_until_the_marker_is_written(ddb):
    assert re_.entity_index_ready(ddb, TABLE) is False
    re_.mark_entity_index_ready(ddb, TABLE)
    assert re_.entity_index_ready(ddb, TABLE) is True


def test_marker_matches_the_backfill_scripts_literals(ddb):
    # scripts/backfill_registry_entity.py writes the marker standalone (it is
    # deliberately boto3-only) — the duplicated literals must stay in sync.
    ddb.put_item(
        TableName=TABLE,
        Item={"pk": {"S": "REGISTRY"}, "sk": {"S": "ENTITY_INDEX_READY"}},
    )
    assert re_.entity_index_ready(ddb, TABLE) is True


def test_ready_reads_via_a_table_resource_too(ddb):
    re_.mark_entity_index_ready(ddb, TABLE)
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    assert re_.entity_index_ready(table, resource=True) is True


def test_ready_fails_closed_on_read_errors():
    class _Broken:
        def get_item(self, **kw):
            raise RuntimeError("ddb down")

    # Unreadable marker => the scan fallback (correct, just slower).
    assert re_.entity_index_ready(_Broken(), TABLE) is False


def test_query_entity_rows_drains_one_entity(ddb):
    _mapping(ddb, "sales", "orders")
    _mapping(ddb, "ops", "logs")
    ddb.put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": "DOMAIN#sales"},
            "sk": {"S": "META"},
            "entity": {"S": re_.ENTITY_DOMAIN},
            "pair": {"S": re_.entity_pair("sales")},
        },
    )
    rows = re_.query_entity_rows(ddb, TABLE, re_.ENTITY_DATASET)
    assert {r["pair"]["S"] for r in rows} == {"sales/orders", "ops/logs"}
    domains = re_.query_entity_rows(ddb, TABLE, re_.ENTITY_DOMAIN)
    assert [d["pair"]["S"] for d in domains] == ["sales"]


def test_partially_stamped_index_is_visibly_partial(ddb):
    # The scenario the marker exists to defuse: one stamped row, one legacy
    # row. The GSI sees only the stamped one — so a reader trusting a
    # non-empty Query would hide the legacy dataset entirely.
    _mapping(ddb, "fresh", "newds", stamped=True)
    _mapping(ddb, "legacy", "oldds", stamped=False)
    rows = re_.query_entity_rows(ddb, TABLE, re_.ENTITY_DATASET)
    assert [r["pair"]["S"] for r in rows] == ["fresh/newds"]
    # ...which is exactly why readers must gate on the marker:
    assert re_.entity_index_ready(ddb, TABLE) is False


def test_missing_index_error_is_recognized_and_narrow():
    class _Err(Exception):
        def __init__(self, code, message):
            super().__init__(message)
            self.response = {"Error": {"Code": code, "Message": message}}

    # Real DynamoDB and moto shapes for a missing GSI: fallback allowed.
    assert re_.is_missing_index_error(
        _Err("ValidationException", "The table does not have the specified index: by-entity")
    )
    assert re_.is_missing_index_error(
        _Err("ResourceNotFoundException", "Invalid index: by-entity for table: t")
    )
    # Everything else must propagate (no silent full-scan mid-pagination).
    assert not re_.is_missing_index_error(
        _Err("ThrottlingException", "Rate exceeded")
    )
    assert not re_.is_missing_index_error(
        _Err("ResourceNotFoundException", "Requested resource not found")
    )
    assert not re_.is_missing_index_error(RuntimeError("boom"))
