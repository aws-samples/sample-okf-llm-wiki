"""Titan V2 embedding + S3 Vectors helpers, shared by reindex and consumption.

These wrap the two most error-prone boto3 shapes so no service reimplements them
(and drifts): the Titan invoke_model body, and the S3 Vectors tagged-union
``data`` key. See docs/API_REFERENCE.md §3–4.
"""

from __future__ import annotations

import json
import time
from typing import Any

from okf_core.embedding import (
    DISTANCE_METRIC,
    EMBED_DIMENSIONS,
    EMBED_MODEL_ID,
    MAX_EMBED_CHARS,
    NON_FILTERABLE_METADATA_KEYS,
)


def embed_text(
    bedrock_runtime, text: str, *, dimensions: int = EMBED_DIMENSIONS
) -> list[float]:
    """Return the Titan V2 float embedding for ``text`` (retries throttling)."""
    body = json.dumps(
        {
            "inputText": text[:MAX_EMBED_CHARS],
            "dimensions": dimensions,  # only 1024|512|256
            "normalize": True,
        }
    )
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            resp = bedrock_runtime.invoke_model(
                modelId=EMBED_MODEL_ID,
                body=body,
                accept="application/json",
                contentType="application/json",
            )
            out = json.loads(resp["body"].read())
            return out["embedding"]  # present because float is the default type
        except Exception as e:  # noqa: BLE001 - retry only ThrottlingException
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code == "ThrottlingException" and attempt < 4:
                last_exc = e
                time.sleep(2**attempt)
                continue
            raise
    raise last_exc  # type: ignore[misc]


def create_index_if_absent(s3vectors, *, vector_bucket: str, index_name: str) -> bool:
    """Create the OKF vector index with the frozen params if it doesn't exist.

    Returns True if created, False if it already existed. Idempotent — safe to
    call at worker cold start.
    """
    try:
        s3vectors.get_index(vectorBucketName=vector_bucket, indexName=index_name)
        return False
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code not in ("NotFoundException", "ResourceNotFoundException", "404"):
            # Anything other than "missing" is a real error worth surfacing.
            raise
    s3vectors.create_index(
        vectorBucketName=vector_bucket,
        indexName=index_name,
        dataType="float32",
        dimension=EMBED_DIMENSIONS,
        distanceMetric=DISTANCE_METRIC,
        metadataConfiguration={
            "nonFilterableMetadataKeys": list(NON_FILTERABLE_METADATA_KEYS)
        },
    )
    return True


def put_vector(
    s3vectors,
    *,
    vector_bucket: str,
    index_name: str,
    key: str,
    embedding: list[float],
    metadata: dict[str, Any],
) -> None:
    """PutVectors one vector. ``data`` is the tagged union ``{"float32": [...]}``."""
    s3vectors.put_vectors(
        vectorBucketName=vector_bucket,
        indexName=index_name,
        vectors=[
            {
                "key": key,
                "data": {"float32": [float(x) for x in embedding]},
                "metadata": metadata,
            }
        ],
    )


def delete_vector(s3vectors, *, vector_bucket: str, index_name: str, key: str) -> None:
    s3vectors.delete_vectors(
        vectorBucketName=vector_bucket, indexName=index_name, keys=[key]
    )


def query_vectors(
    s3vectors,
    *,
    vector_bucket: str,
    index_name: str,
    query_embedding: list[float],
    top_k: int = 10,
    metadata_filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """QueryVectors and return ``[{key, distance, metadata}, ...]``.

    NOTE: because we ask for metadata (and often pass a filter), the caller's IAM
    role needs BOTH ``s3vectors:QueryVectors`` AND ``s3vectors:GetVectors``.
    """
    kwargs: dict[str, Any] = {
        "vectorBucketName": vector_bucket,
        "indexName": index_name,
        "topK": top_k,
        "queryVector": {"float32": [float(x) for x in query_embedding]},
        "returnMetadata": True,
        "returnDistance": True,
    }
    if metadata_filter:
        kwargs["filter"] = metadata_filter
    resp = s3vectors.query_vectors(**kwargs)
    return resp.get("vectors", [])


def build_hierarchy_filter(
    *,
    data_domain: str | None = None,
    dataset: str | None = None,
    table: str | None = None,
    type_: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Compose an S3 Vectors ``$and`` filter over the hierarchy knobs.

    Only exact-match ($eq) / membership ($in) operators — S3 Vectors has no
    prefix/substring operator (which is why domain/dataset/table are separate
    keys). Returns None if no constraints are given.
    """
    clauses: list[dict[str, Any]] = []
    if data_domain:
        clauses.append({"data_domain": {"$eq": data_domain}})
    if dataset:
        clauses.append({"dataset": {"$eq": dataset}})
    if table:
        clauses.append({"table": {"$eq": table}})
    if type_:
        clauses.append({"type": {"$eq": type_}})
    if tags:
        clauses.append({"tags": {"$in": list(tags)}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}
