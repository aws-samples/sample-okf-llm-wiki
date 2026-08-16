"""The derived cross-dataset reference signal (XREF# registry rows).

Cross-dataset pair docs live ONLY in the initiating bundle, so the referenced
dataset needs a discovery signal. The reindex worker derives it from the SAME
object events that drive the vector index: a doc under
``okf/<sd>/<sds>/external/<td>/<tds>/`` upserts ``pk="DOMAIN#<td>",
sk="XREF#<tds>#<sd>#<sds>"``; the row is removed once the pair subtree holds no
concept docs. Being event-derived, the signal survives full-harvest wipes and
repromotes and is rebuildable by replay (no writer has to remember it).

moto backs S3 + DynamoDB; s3vectors + bedrock-runtime are faked. No live AWS.
"""

from __future__ import annotations

from reindex.handler import process_record
from fakes import (
    BUNDLE_BUCKET,
    FRESHNESS_TABLE,
    REGISTRY_TABLE,
    VECTOR_BUCKET,
    VECTOR_INDEX,
    FakeBedrock,
    FakeS3Vectors,
    put_object,
    s3_event_record,
)

# A cross-dataset pair doc: sales/orders documents its relationship to
# crm/customers, so the doc lives in sales/orders' bundle.
PAIR_PREFIX = "okf/sales/orders/external/crm/customers"
OVERVIEW_KEY = f"{PAIR_PREFIX}/overview.md"
JOIN_KEY = f"{PAIR_PREFIX}/joins/orders__customers.md"

CROSS_MD = """---
type: Cross-Dataset Reference
title: orders x customers
description: How sales/orders relates to crm/customers.
timestamp: 2026-07-28T00:00:00Z
cross_dataset:
  source:
    data_domain: sales
    dataset: orders
  target:
    data_domain: crm
    dataset: customers
tags:
  - cross-dataset
---

# Overview

`"orders_db"."orders"` joins `"customers_db"."customers"` on `customer_id`.
"""


def _process(record, aws, seq="00000000000000AAAA"):
    return process_record(
        record,
        s3=aws["s3"],
        s3vectors=FakeS3Vectors(),
        bedrock_runtime=FakeBedrock(),
        ddb=aws["ddb"],
        bundle_bucket=BUNDLE_BUCKET,
        vector_bucket=VECTOR_BUCKET,
        vector_index=VECTOR_INDEX,
        freshness_table=FRESHNESS_TABLE,
        registry_table=REGISTRY_TABLE,
    )


def _xref_rows(aws, *, target_domain="crm"):
    from boto3.dynamodb.conditions import Key

    tbl = aws["ddb"].Table(REGISTRY_TABLE)
    resp = tbl.query(
        KeyConditionExpression=Key("pk").eq(f"DOMAIN#{target_domain}")
        & Key("sk").begins_with("XREF#")
    )
    return resp.get("Items", [])


def _created(key, seq="00000000000000AAAA"):
    return s3_event_record(key, sequencer=seq)


def _deleted(key, seq="00000000000000BBBB"):
    return s3_event_record(
        key,
        detail_type="Object Deleted",
        sequencer=seq,
        deletion_type="Delete Marker Created",
    )


# --- create ------------------------------------------------------------------


def test_cross_doc_creates_xref_row_on_the_target(aws):
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    assert _process(_created(OVERVIEW_KEY), aws) == "upserted"

    rows = _xref_rows(aws)
    assert len(rows) == 1
    row = rows[0]
    assert row["pk"] == "DOMAIN#crm"
    assert row["sk"] == "XREF#customers#sales#orders"
    assert row["target_data_domain"] == "crm"
    assert row["target_dataset"] == "customers"
    assert row["source_data_domain"] == "sales"
    assert row["source_dataset"] == "orders"
    assert row["updated_at"]


def test_ordinary_doc_writes_no_xref_row(aws):
    key = "okf/sales/orders/tables/orders.md"
    put_object(aws["s3"], key)
    assert _process(_created(key), aws) == "upserted"
    assert _xref_rows(aws) == []
    assert _xref_rows(aws, target_domain="sales") == []


def test_multiple_pair_docs_collapse_to_one_row(aws):
    # Every doc of the pair upserts the SAME row (idempotent) — the signal is
    # per-pair, not per-doc.
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    put_object(aws["s3"], JOIN_KEY, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    _process(_created(JOIN_KEY, seq="00000000000000AAAB"), aws)
    assert len(_xref_rows(aws)) == 1


def test_two_sources_referencing_one_target_are_separate_rows(aws):
    other = "okf/support/tickets/external/crm/customers/overview.md"
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    put_object(aws["s3"], other, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    _process(_created(other, seq="00000000000000AAAC"), aws)

    rows = sorted(r["sk"] for r in _xref_rows(aws))
    assert rows == [
        "XREF#customers#sales#orders",
        "XREF#customers#support#tickets",
    ]


# --- delete ------------------------------------------------------------------


def test_deleting_the_last_pair_doc_removes_the_row(aws):
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    assert len(_xref_rows(aws)) == 1

    # The bundle write-through deletes the object, then the event arrives.
    aws["s3"].delete_object(Bucket=BUNDLE_BUCKET, Key=OVERVIEW_KEY)
    assert _process(_deleted(OVERVIEW_KEY), aws) == "deleted"
    assert _xref_rows(aws) == []


def test_deleting_one_of_several_pair_docs_keeps_the_row(aws):
    # Decided from CURRENT S3 truth: the overview still exists, so the pair is
    # still documented and the signal must stay.
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    put_object(aws["s3"], JOIN_KEY, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    _process(_created(JOIN_KEY, seq="00000000000000AAAB"), aws)

    aws["s3"].delete_object(Bucket=BUNDLE_BUCKET, Key=JOIN_KEY)
    assert _process(_deleted(JOIN_KEY), aws) == "deleted"
    assert len(_xref_rows(aws)) == 1


def test_leftover_generated_index_does_not_keep_the_row(aws):
    # A pair subtree holding only a generated index.md counts as empty: index.md
    # is not a concept (parse_bundle_key rejects it).
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    put_object(aws["s3"], f"{PAIR_PREFIX}/index.md", "# Cross-Dataset Reference\n")

    aws["s3"].delete_object(Bucket=BUNDLE_BUCKET, Key=OVERVIEW_KEY)
    _process(_deleted(OVERVIEW_KEY), aws)
    assert _xref_rows(aws) == []


def test_full_harvest_wipe_of_the_source_clears_the_signal(aws):
    # The scenario that motivated deriving this from events: the SOURCE bundle
    # is fully re-harvested (clean_authored_output removes external/), so the
    # target's "cross-referenced by" signal must disappear with no writer
    # remembering to maintain it.
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    put_object(aws["s3"], JOIN_KEY, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    _process(_created(JOIN_KEY, seq="00000000000000AAAB"), aws)
    assert len(_xref_rows(aws)) == 1

    for i, key in enumerate((OVERVIEW_KEY, JOIN_KEY)):
        aws["s3"].delete_object(Bucket=BUNDLE_BUCKET, Key=key)
        _process(_deleted(key, seq=f"00000000000000BBB{i}"), aws)
    assert _xref_rows(aws) == []


def test_ordinary_doc_delete_touches_no_xref_row(aws):
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    key = "okf/sales/orders/tables/orders.md"
    put_object(aws["s3"], key)
    _process(_created(key, seq="00000000000000AAAB"), aws)

    aws["s3"].delete_object(Bucket=BUNDLE_BUCKET, Key=key)
    _process(_deleted(key), aws)
    # The pair's row is untouched by an unrelated dataset-doc delete.
    assert len(_xref_rows(aws)) == 1


def test_clear_loses_to_a_concurrent_upsert(aws):
    # LIST-then-DELETE is not atomic: a stalled delete-path worker must not
    # erase a row a concurrent create-path worker just (re-)upserted for newly
    # authored docs. The delete is conditional on updated_at being OLDER than
    # the clear's listing start — simulate the race by giving the row an
    # updated_at in the future (the concurrent upsert) while the pair prefix is
    # empty (what the stalled worker's listing sees).
    from datetime import datetime, timedelta, timezone

    from reindex.handler import _clear_xref_if_pair_empty

    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    aws["ddb"].Table(REGISTRY_TABLE).update_item(
        Key={"pk": "DOMAIN#crm", "sk": "XREF#customers#sales#orders"},
        UpdateExpression="SET updated_at = :u",
        ExpressionAttributeValues={":u": future},
    )
    aws["s3"].delete_object(Bucket=BUNDLE_BUCKET, Key=OVERVIEW_KEY)

    from okf_aws import parse_bundle_key

    _clear_xref_if_pair_empty(
        aws["s3"],
        aws["ddb"],
        bundle_bucket=BUNDLE_BUCKET,
        registry_table=REGISTRY_TABLE,
        location=parse_bundle_key(OVERVIEW_KEY),
    )
    # The "newer" row survived the stale clear.
    assert len(_xref_rows(aws)) == 1


def test_xref_skips_unsafe_pair_segments(aws):
    # A '#' in a pair component would collide two pairs onto one XREF sort key
    # — such keys produce NO signal rather than a corrupt one.
    key = "okf/sales/orders/external/crm/cust#omers/overview.md"
    put_object(aws["s3"], key, CROSS_MD)
    assert _process(_created(key), aws) == "upserted"  # vector still indexed
    assert _xref_rows(aws) == []


def test_permanent_version_delete_does_not_clear_the_signal(aws):
    # Lifecycle expiry of a NONCURRENT version leaves the live doc alone — the
    # worker skips those before any work (same rule that protects vectors).
    put_object(aws["s3"], OVERVIEW_KEY, CROSS_MD)
    _process(_created(OVERVIEW_KEY), aws)
    record = s3_event_record(
        OVERVIEW_KEY,
        detail_type="Object Deleted",
        sequencer=None,
        deletion_type="Permanently Deleted",
    )
    assert _process(record, aws) == "skipped"
    assert len(_xref_rows(aws)) == 1
