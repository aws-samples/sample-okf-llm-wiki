"""Tests for the SQS lambda_handler wrapper: envelope parsing + partial batch."""

from __future__ import annotations

import json

from incremental.handler import lambda_handler
from conftest import (
    BUNDLE_BUCKET,
    FRESHNESS_TABLE,
    REGISTRY_TABLE,
    seed_mapping,
    seed_ready_bundle,
)
from fakes import FakeAgentCore, FakeGlue, col, make_table

ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/harvest-abc"


def _env(monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", BUNDLE_BUCKET)
    monkeypatch.setenv("OKF_REGISTRY_TABLE", REGISTRY_TABLE)
    monkeypatch.setenv("OKF_FRESHNESS_TABLE", FRESHNESS_TABLE)
    monkeypatch.setenv("OKF_HARVEST_RUNTIME_ARN", ARN)


def _eventbridge_record(message_id, detail):
    envelope = {
        "source": "aws.glue",
        "detail-type": "Glue Data Catalog Table State Change",
        "detail": detail,
    }
    return {"messageId": message_id, "body": json.dumps(envelope)}


def test_batch_processes_records_and_reports_no_failures(aws, monkeypatch):
    _env(monkeypatch)
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    glue = FakeGlue({"f1_db": {"races": [make_table("races", [col("id", "bigint")])]}})
    agentcore = FakeAgentCore()

    event = {
        "Records": [
            _eventbridge_record(
                "m1",
                {
                    "databaseName": "f1_db",
                    "tableName": "races",
                    "changedPartitions": [],
                },
            ),
            # Unmapped -> skipped, no failure.
            _eventbridge_record(
                "m2",
                {"databaseName": "other_db", "tableName": "x", "changedPartitions": []},
            ),
        ]
    }

    result = lambda_handler(
        event,
        clients_factory=lambda: {
            "glue": glue,
            "ddb": aws["ddb"],
            "s3": aws["s3"],
            "agentcore": agentcore,
        },
    )

    assert result == {"batchItemFailures": []}
    assert len(agentcore.invocations) == 1  # only the mapped record invoked


def test_bad_record_reported_as_partial_failure(aws, monkeypatch):
    _env(monkeypatch)
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    glue = FakeGlue({"f1_db": {"races": [make_table("races", [col("id", "bigint")])]}})
    agentcore = FakeAgentCore()

    event = {
        "Records": [
            {"messageId": "bad", "body": "not-json"},
            _eventbridge_record(
                "good",
                {
                    "databaseName": "f1_db",
                    "tableName": "races",
                    "changedPartitions": [],
                },
            ),
        ]
    }

    result = lambda_handler(
        event,
        clients_factory=lambda: {
            "glue": glue,
            "ddb": aws["ddb"],
            "s3": aws["s3"],
            "agentcore": agentcore,
        },
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    # The good record still processed despite the bad one.
    assert len(agentcore.invocations) == 1


def test_bare_detail_body_is_accepted(aws, monkeypatch):
    _env(monkeypatch)
    seed_mapping(aws["ddb"], data_domain="sales", dataset="f1", glue_database="f1_db")
    seed_ready_bundle(aws["s3"], data_domain="sales", dataset="f1")
    glue = FakeGlue({"f1_db": {"races": [make_table("races", [col("id", "bigint")])]}})
    agentcore = FakeAgentCore()

    # Body is the bare detail (no EventBridge envelope).
    body = json.dumps(
        {"databaseName": "f1_db", "tableName": "races", "changedPartitions": []}
    )
    event = {"Records": [{"messageId": "m1", "body": body}]}

    result = lambda_handler(
        event,
        clients_factory=lambda: {
            "glue": glue,
            "ddb": aws["ddb"],
            "s3": aws["s3"],
            "agentcore": agentcore,
        },
    )
    assert result == {"batchItemFailures": []}
    assert len(agentcore.invocations) == 1
