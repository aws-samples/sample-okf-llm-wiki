"""In-memory fakes for the s3vectors + bedrock-runtime clients.

S3 and DynamoDB use moto (real API surface); s3vectors and bedrock-runtime are
not covered by moto, so we hand-roll minimal fakes mirroring the shapes in
docs/API_REFERENCE.md §3–4 (and the okf_aws test fakes).
"""

from __future__ import annotations

import json
from typing import Any


class FakeBody:
    def __init__(self, payload: dict[str, Any]):
        self._b = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._b


class FakeBedrock:
    """Records invoke_model calls and returns a fixed 512-dim embedding."""

    def __init__(self, embedding: list[float] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._embedding = embedding if embedding is not None else [0.1] * 512

    def invoke_model(self, **kwargs) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"body": FakeBody({"embedding": self._embedding})}


class FakeS3Vectors:
    """Records query_vectors calls and returns canned hits."""

    def __init__(self, hits: list[dict[str, Any]] | None = None):
        self.queries: list[dict[str, Any]] = []
        self._hits = hits if hits is not None else []

    def query_vectors(self, **kwargs) -> dict[str, Any]:
        self.queries.append(kwargs)
        return {"vectors": list(self._hits)}
