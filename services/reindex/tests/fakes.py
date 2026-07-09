"""In-memory fakes + shared test constants/builders.

s3vectors + bedrock-runtime are faked here (mirroring
services/okf_aws/tests/test_embeddings.py); moto backs the real S3 + DynamoDB
clients (built in conftest.py). Constants and the event builder live here rather
than in conftest so they can be imported explicitly (`from tests.fakes import ...`).
"""

from __future__ import annotations

import json
from typing import Any

# --- shared constants --------------------------------------------------------

BUNDLE_BUCKET = "okf-bundle-test"
VECTOR_BUCKET = "okf-vectors-test"
VECTOR_INDEX = "okf-index"
FRESHNESS_TABLE = "okf-freshness"
REGION = "us-east-1"

# A minimal but valid concept doc (all required frontmatter keys present).
CONCEPT_MD = """---
type: Glue Table
title: Races
description: One row per Formula 1 race.
timestamp: 2026-07-01T00:00:00Z
tags:
  - f1
  - races
---

# Overview

The races table has one row per grand prix.
"""


def put_object(s3, key: str, body: str = CONCEPT_MD) -> None:
    s3.put_object(Bucket=BUNDLE_BUCKET, Key=key, Body=body.encode("utf-8"))


def s3_event_record(
    key: str,
    *,
    detail_type: str = "Object Created",
    sequencer: str = "00000000000000AAAA",
    message_id: str = "m1",
    bucket: str = BUNDLE_BUCKET,
) -> dict[str, Any]:
    """Build an SQS record whose body wraps an S3 EventBridge event."""
    detail = {
        "bucket": {"name": bucket},
        "object": {"key": key, "size": 123, "sequencer": sequencer},
    }
    eb_event = {
        "version": "0",
        "source": "aws.s3",
        "detail-type": detail_type,
        "detail": detail,
    }
    return {"messageId": message_id, "body": json.dumps(eb_event)}


# --- AWS client fakes --------------------------------------------------------


class FakeBody:
    def __init__(self, payload: Any):
        self._b = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._b


class FakeBedrock:
    """Titan V2 stub — returns a fixed 512-dim embedding and records calls.

    ``fail_times`` makes the first N invoke_model calls raise (simulating a
    Bedrock throttle) so tests can drive the reindex retry path: the vector must
    NOT be written and the dedup marker must NOT advance on a failed embed.
    """

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_times = fail_times

    def invoke_model(self, **kwargs) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("simulated Bedrock throttle")
        body = json.loads(kwargs["body"])
        assert body["dimensions"] == 512
        assert body["normalize"] is True
        return {"body": FakeBody({"embedding": [0.1] * 512})}


class NotFound(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "NotFoundException"}}


class FakeS3Vectors:
    """Records put/delete/create calls; the index starts as already-present."""

    def __init__(self, index_exists: bool = True) -> None:
        self.index_exists = index_exists
        self.created: dict[str, Any] | None = None
        self.put: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    def get_index(self, **kwargs) -> dict[str, Any]:
        if not self.index_exists:
            raise NotFound()
        return {"index": {}}

    def create_index(self, **kwargs) -> None:
        self.created = kwargs
        self.index_exists = True

    def put_vectors(self, **kwargs) -> None:
        self.put.append(kwargs)

    def delete_vectors(self, **kwargs) -> None:
        self.deleted.append(kwargs)
