"""Off-mount persistence of the per-round human-facing benchmark review.

The review carries gold + predicted SQL — the transparency artifact a HUMAN
inspects to verify each verdict. It must live where the questions CSV lives: under
the **off-mount** ``benchmark/`` S3 prefix (NOT the ``okf/`` mount), so no LLM role
can read it, and it is served to the UI only via the Cognito-authed Control API.

One JSON object per round at
``benchmark/<domain>/<dataset>/reviews/<runtime_session_id>/<iteration>.json``.
The key shape is shared verbatim with the Control API reader (see
``control_api.handlers.benchmark_review_key``) — keep the two in sync.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Any

log = logging.getLogger("harvest.benchmark.review_store")


def review_prefix(data_domain: str, dataset: str) -> str:
    """The off-mount S3 prefix for a dataset's benchmark review artifacts."""
    return f"benchmark/{data_domain}/{dataset}/reviews/"


def review_key(data_domain: str, dataset: str, session_id: str, iteration: int) -> str:
    """The S3 key for one round's review JSON (off-mount, gold-carrying)."""
    return f"{review_prefix(data_domain, dataset)}{session_id}/{iteration}.json"


def _bucket_counts(review: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in review:
        counts[r.bucket] = counts.get(r.bucket, 0) + 1
    return counts


def build_review_document(iteration: int, review: list[Any]) -> dict[str, Any]:
    """Serialize a round's review into the persisted/served JSON shape.

    ``review`` is a list of ``tool.QuestionReview``. The document carries the full
    per-question rows (incl. gold + predicted SQL) plus per-bucket counts so the UI
    can render tab badges without re-tallying."""
    return {
        "iteration": iteration,
        "counts": _bucket_counts(review),
        "questions": [asdict(r) for r in review],
    }


def make_review_persister(
    *, data_domain: str, dataset: str, session_id: str, put_object: Any = None
):
    """Return ``persist_review(iteration, review)`` that writes the off-mount JSON.

    ``put_object(bucket, key, body_bytes)`` is injectable for tests; defaults to the
    boto3 S3 PUT. Returns a no-op persister (logs once) when the bundle bucket env
    is missing, so a misconfiguration degrades gracefully instead of failing rounds.
    """
    bucket = os.environ.get("OKF_BUNDLE_BUCKET")
    if not bucket:
        log.warning("OKF_BUNDLE_BUCKET unset; benchmark reviews will not be persisted.")

        def _noop(_iteration: int, _review: list[Any]) -> None:
            return None

        return _noop

    # A blank session id would produce a double-slash key (reviews//<n>.json) whose
    # empty {session} segment the Control API route can't match — the review would
    # be written but unfetchable. Guard it: with no session, don't persist (the UI
    # also can't offer a review link without a session id).
    if not (session_id or "").strip():
        log.warning("Benchmark review has no runtime_session_id; skipping persistence.")

        def _noop(_iteration: int, _review: list[Any]) -> None:
            return None

        return _noop

    put = put_object or _default_put_object

    def persist_review(iteration: int, review: list[Any]) -> None:
        key = review_key(data_domain, dataset, session_id, iteration)
        body = json.dumps(build_review_document(iteration, review)).encode("utf-8")
        put(bucket, key, body)
        log.info("Persisted benchmark review round %d → s3://%s/%s", iteration, bucket, key)

    return persist_review


def _default_put_object(bucket: str, key: str, body: bytes) -> None:
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
