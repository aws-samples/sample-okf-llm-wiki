"""SQS-triggered reindex worker.

Flow (OKF_DESIGN §5 "Freshness"):

    S3 (eventbridge=true) -> default bus -> EventBridge rule -> SQS -> this Lambda

Each SQS record's ``body`` is a JSON string wrapping an EventBridge event whose
``detail`` carries ``bucket.name``, ``object.key`` and ``object.sequencer``, and
whose ``detail-type`` is ``"Object Created"`` or ``"Object Deleted"``. We:

1. Parse the key with :func:`okf_aws.parse_bundle_key`. Non-concept keys
   (``index.md``, ``.context/``, ``.harvest/`` ...) parse to ``None`` and are
   skipped cleanly.
2. Dedup / order on ``object.sequencer`` using the ``okf-freshness`` table
   (``pk="VEC#<vector_key>"``, ``sk="SEQ"``). Sequencers are hex strings that
   compare lexicographically per key, so an event whose sequencer is ``<=`` the
   stored ``last_sequencer`` is a duplicate or out-of-order replay and is
   ignored. This is a read-only PRE-CHECK; the marker is advanced only in step 4.
3. Apply: an Object Created/Updated event re-embeds the doc and PutVectors
   (overwrites by key — idempotent); an Object Deleted event DeleteVectors by
   key.
4. Advance ``last_sequencer`` (conditional PutItem) ONLY after step 3 succeeds,
   so a transient failure in step 3 leaves the marker untouched and the SQS
   redelivery re-processes the record instead of skipping it as a "duplicate".

The Lambda handler is a thin wrapper that builds boto3 clients from env vars and
returns an SQS partial-batch response so only the records that raised are
retried (the event source mapping is configured with
``function_response_types=["ReportBatchItemFailures"]``).

Every dependency that touches AWS (s3, s3vectors, bedrock-runtime, dynamodb) is
injected into :func:`process_record` so tests use moto / fakes and never hit live
AWS.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from okf_aws import (
    ConceptLocation,
    create_index_if_absent,
    delete_vector,
    embed_text,
    parse_bundle_key,
    put_vector,
)
from okf_core.document import OKFDocument
from okf_core.embedding import (
    ConceptCoordinates,
    build_embed_text,
    build_filterable_metadata,
    build_non_filterable_metadata,
    vector_key,
)

# EventBridge detail-type values for S3 events (see docs/API_REFERENCE.md §4).
_CREATED = "Object Created"
_DELETED = "Object Deleted"


class SkippedRecord(Exception):
    """Raised internally to signal a record was intentionally not processed.

    Distinct from a failure: a skip (non-concept key, duplicate/older sequencer,
    unknown detail-type) is a success from SQS's point of view — the message
    should be deleted, not retried.
    """


@dataclass
class ParsedEvent:
    """The bits of an S3 EventBridge event the worker actually needs."""

    bucket: str
    key: str
    sequencer: str
    detail_type: str  # "Object Created" | "Object Deleted"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_sqs_record(record: dict[str, Any]) -> ParsedEvent:
    """Extract the S3 event fields from an SQS record body.

    Raises ``ValueError`` (a real failure -> retried) if the body is not the
    expected EventBridge-over-SQS envelope, so a malformed message surfaces as a
    batchItemFailure instead of being silently dropped.
    """
    body = record["body"]
    event = json.loads(body) if isinstance(body, str) else body
    detail_type = event.get("detail-type")
    if detail_type not in (_CREATED, _DELETED):
        raise ValueError(f"Unexpected detail-type: {detail_type!r}")
    detail = event.get("detail") or {}
    bucket = (detail.get("bucket") or {}).get("name")
    obj = detail.get("object") or {}
    key = obj.get("key")
    sequencer = obj.get("sequencer")
    if not bucket or not key or not sequencer:
        raise ValueError("S3 event missing bucket.name / object.key / object.sequencer")
    return ParsedEvent(
        bucket=bucket, key=key, sequencer=sequencer, detail_type=detail_type
    )


def _sequencer_is_new(ddb, *, freshness_table: str, vkey: str, sequencer: str) -> bool:
    """True iff ``sequencer`` is newer than the stored dedup marker for ``vkey``.

    A cheap read-only pre-check so a duplicate / out-of-order replay is dropped
    BEFORE any expensive work (S3 GET, embed, PutVectors). Sequencers are S3's
    hex strings and compare lexicographically per key, so "newer" == strictly
    greater than the stored ``last_sequencer``. A ConsistentRead avoids a stale
    read re-doing work that a concurrent worker already committed.

    NOTE: this only *reads*; the marker is advanced by :func:`_advance_sequencer`
    AFTER the work succeeds. Committing the advance up front (as this code used
    to) permanently dropped the update on any transient failure: the SQS retry
    saw the already-advanced marker, treated the record as a duplicate, and
    skipped it without ever writing the vector. Advancing only on success makes
    a failed record fully re-processable (re-embeds are idempotent — PutVectors
    overwrites by key).
    """
    table = ddb.Table(freshness_table)
    resp = table.get_item(Key={"pk": f"VEC#{vkey}", "sk": "SEQ"}, ConsistentRead=True)
    item = resp.get("Item")
    if not item:
        return True
    return item.get("last_sequencer", "") < sequencer


def _advance_sequencer(ddb, *, freshness_table: str, vkey: str, sequencer: str) -> None:
    """Advance the dedup marker to ``sequencer`` for ``vkey`` (after work succeeds).

    Uses a conditional PutItem so a concurrent worker holding a *newer* event
    can't be clobbered by an older one: the write only lands if there is no
    existing row or the stored ``last_sequencer`` is strictly less than this one.
    Losing the condition (a newer event already advanced past us) is not an
    error — that event owns the marker now — so we swallow it.
    """
    table = ddb.Table(freshness_table)
    try:
        table.put_item(
            Item={
                "pk": f"VEC#{vkey}",
                "sk": "SEQ",
                "last_sequencer": sequencer,
                "updated_at": _now_iso(),
            },
            ConditionExpression="attribute_not_exists(pk) OR last_sequencer < :seq",
            ExpressionAttributeValues={":seq": sequencer},
        )
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            return
        raise


def _get_object_text(s3, *, bucket: str, key: str) -> str:
    obj = s3.get_object(Bucket=bucket, Key=key)
    raw = obj["Body"].read()
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return raw


def _coords_for(location: ConceptLocation) -> ConceptCoordinates:
    """Map a bundle ConceptLocation onto embedding ConceptCoordinates.

    ``concept_path`` is the vector key (``<domain>/<dataset>/<concept_id>``) so
    the vector deterministically overwrites on re-embed; ``s3_key`` is the object
    key (the source of truth) and ``table`` is carried through for the filterable
    ``table`` metadata knob.
    """
    return ConceptCoordinates(
        data_domain=location.data_domain,
        dataset=location.dataset,
        concept_path=location.vector_key,
        s3_key=location.s3_key,
        table=location.table,
    )


def _upsert_vector(
    location: ConceptLocation,
    text: str,
    *,
    s3vectors,
    bedrock_runtime,
    vector_bucket: str,
    vector_index: str,
) -> None:
    """Parse the doc, embed it, and PutVectors (overwrite by key)."""
    doc = OKFDocument.parse(text)
    fm = doc.frontmatter
    coords = _coords_for(location)
    embedding = embed_text(bedrock_runtime, build_embed_text(fm, doc.body))
    metadata = {
        **build_filterable_metadata(coords, fm),
        **build_non_filterable_metadata(coords, fm),
    }
    put_vector(
        s3vectors,
        vector_bucket=vector_bucket,
        index_name=vector_index,
        key=vector_key(coords),
        embedding=embedding,
        metadata=metadata,
    )


def process_record(
    record: dict[str, Any],
    *,
    s3,
    s3vectors,
    bedrock_runtime,
    ddb,
    bundle_bucket: str,
    vector_bucket: str,
    vector_index: str,
    freshness_table: str,
) -> str:
    """Process one SQS record. Pure w.r.t. AWS — all clients are injected.

    Returns a short status string (``"skipped"``, ``"upserted"``, ``"deleted"``)
    for logging/tests. Raises on any real failure so the caller can add the
    record to ``batchItemFailures`` and let SQS redrive it.
    """
    event = _parse_sqs_record(record)

    location = parse_bundle_key(event.key)
    if location is None:
        # index.md / log.md / .context / .harvest / non-.md -> not a concept.
        return "skipped"

    vkey = location.vector_key

    # Read-only dedup/order pre-check: drop a duplicate / out-of-order replay
    # before any expensive work. The marker is NOT advanced here — see below.
    if not _sequencer_is_new(
        ddb, freshness_table=freshness_table, vkey=vkey, sequencer=event.sequencer
    ):
        return "skipped"

    # Do the work FIRST, then advance the dedup marker only once it has
    # succeeded. If any step here raises, the marker is left untouched so the
    # SQS redelivery re-processes the record cleanly (both PutVectors and
    # DeleteVectors are idempotent by key). Advancing before the work would make
    # a transient failure permanent: the retry would see the advanced marker,
    # treat the record as a duplicate, and skip it forever.
    if event.detail_type == _DELETED:
        delete_vector(
            s3vectors, vector_bucket=vector_bucket, index_name=vector_index, key=vkey
        )
        _advance_sequencer(
            ddb, freshness_table=freshness_table, vkey=vkey, sequencer=event.sequencer
        )
        return "deleted"

    # Object Created / Updated.
    text = _get_object_text(s3, bucket=bundle_bucket, key=event.key)
    _upsert_vector(
        location,
        text,
        s3vectors=s3vectors,
        bedrock_runtime=bedrock_runtime,
        vector_bucket=vector_bucket,
        vector_index=vector_index,
    )
    _advance_sequencer(
        ddb, freshness_table=freshness_table, vkey=vkey, sequencer=event.sequencer
    )
    return "upserted"


# --- Lambda wiring -----------------------------------------------------------

# Clients + one-time index creation are cached across warm invocations of the
# same container. Guarded so create_index_if_absent runs once per cold start.
_CLIENTS: dict[str, Any] | None = None
_INDEX_READY = False


def _build_clients() -> dict[str, Any]:
    """Build boto3 clients from env vars. Called once per warm container."""
    import boto3

    region = os.environ.get("AWS_REGION")
    return {
        "s3": boto3.client("s3", region_name=region),
        "s3vectors": boto3.client("s3vectors", region_name=region),
        "bedrock_runtime": boto3.client("bedrock-runtime", region_name=region),
        "ddb": boto3.resource("dynamodb", region_name=region),
        "bundle_bucket": os.environ["OKF_BUNDLE_BUCKET"],
        "vector_bucket": os.environ["OKF_VECTOR_BUCKET"],
        "vector_index": os.environ["OKF_VECTOR_INDEX"],
        "freshness_table": os.environ.get("OKF_FRESHNESS_TABLE", "okf-freshness"),
    }


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """SQS entrypoint. Returns an SQS partial-batch failure response.

    Every record is processed independently; a record that raises is added to
    ``batchItemFailures`` by ``messageId`` so SQS retries only that message (the
    successful ones are deleted). Requires the event source mapping to declare
    ``function_response_types=["ReportBatchItemFailures"]``.
    """
    global _CLIENTS, _INDEX_READY
    if _CLIENTS is None:
        _CLIENTS = _build_clients()
    if not _INDEX_READY:
        # Cold-start guard: ensure the (immutable-params) index exists once.
        create_index_if_absent(
            _CLIENTS["s3vectors"],
            vector_bucket=_CLIENTS["vector_bucket"],
            index_name=_CLIENTS["vector_index"],
        )
        _INDEX_READY = True

    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            process_record(record, **_CLIENTS)
        except Exception:  # noqa: BLE001 - failed record -> retried via SQS
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
