"""Tests for the lambda_handler wrapper: batch fan-out + partial-batch response.

We inject fakes into the module's cached client dict (and mark the index ready)
so the handler never builds live boto3 clients.
"""

from __future__ import annotations

import reindex.handler as h
from fakes import (
    BUNDLE_BUCKET,
    FRESHNESS_TABLE,
    VECTOR_BUCKET,
    VECTOR_INDEX,
    FakeBedrock,
    FakeS3Vectors,
    put_object,
    s3_event_record,
)


def _install_clients(aws, s3vectors, bedrock):
    h._CLIENTS = {
        "s3": aws["s3"],
        "s3vectors": s3vectors,
        "bedrock_runtime": bedrock,
        "ddb": aws["ddb"],
        "bundle_bucket": BUNDLE_BUCKET,
        "vector_bucket": VECTOR_BUCKET,
        "vector_index": VECTOR_INDEX,
        "freshness_table": FRESHNESS_TABLE,
    }
    h._INDEX_READY = True


def _reset_module_state():
    h._CLIENTS = None
    h._INDEX_READY = False


def test_handler_returns_no_failures_on_all_success(aws):
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v, br = FakeS3Vectors(), FakeBedrock()
    _install_clients(aws, s3v, br)
    try:
        event = {"Records": [s3_event_record(key, message_id="ok-1")]}
        resp = h.lambda_handler(event, None)
    finally:
        _reset_module_state()
    assert resp == {"batchItemFailures": []}
    assert len(s3v.put) == 1


def test_handler_reports_only_failed_records(aws):
    good_key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], good_key)
    s3v, br = FakeS3Vectors(), FakeBedrock()
    _install_clients(aws, s3v, br)
    try:
        event = {
            "Records": [
                s3_event_record(
                    good_key, message_id="good", sequencer="00000000000000A0"
                ),
                {"messageId": "bad", "body": "totally not json"},
                s3_event_record(
                    "okf/na_mi/formula_1/index.md", message_id="ignored"
                ),  # skipped, not a failure
            ]
        }
        resp = h.lambda_handler(event, None)
    finally:
        _reset_module_state()
    assert resp["batchItemFailures"] == [{"itemIdentifier": "bad"}]
    assert len(s3v.put) == 1  # only the good concept was embedded


def test_handler_creates_index_once_on_cold_start(aws):
    """When the index is absent at cold start, the guard creates it exactly once."""
    key = "okf/na_mi/formula_1/tables/races.md"
    put_object(aws["s3"], key)
    s3v = FakeS3Vectors(index_exists=False)
    br = FakeBedrock()
    # Install clients but DO NOT pre-mark the index ready.
    h._CLIENTS = {
        "s3": aws["s3"],
        "s3vectors": s3v,
        "bedrock_runtime": br,
        "ddb": aws["ddb"],
        "bundle_bucket": BUNDLE_BUCKET,
        "vector_bucket": VECTOR_BUCKET,
        "vector_index": VECTOR_INDEX,
        "freshness_table": FRESHNESS_TABLE,
    }
    h._INDEX_READY = False
    try:
        h.lambda_handler({"Records": [s3_event_record(key, message_id="a")]}, None)
        assert s3v.created is not None
        assert s3v.created["dimension"] == 512
        # second invocation on the same warm container must not re-create
        s3v.created = None
        h.lambda_handler(
            {
                "Records": [
                    s3_event_record(key, message_id="b", sequencer="00000000000000FF")
                ]
            },
            None,
        )
        assert s3v.created is None
    finally:
        _reset_module_state()
