"""The registry's ``by-entity`` GSI: the ONE shared read protocol.

Three services list registry rows by kind — the Control API's ``list_domains``,
the consumption MCP's paged ``list_domains``, and the chat policy check's
``_policy_glue_map``. The ``by-entity`` index lets them Query instead of Scan,
but a GSI only contains items that CARRY its key attributes, so a registry
deployed before the index (or backfilled halfway) satisfies a Query with a
PARTIAL row set — one freshly stamped row would make every pre-index dataset
silently vanish from the catalogs. "May I trust the index?" is therefore a
stored fact, not a result-shape heuristic: ``scripts/backfill_registry_entity.py``
writes a marker row when its walk completes, and readers Query the index ONLY
when that marker exists, falling back to the legacy filtered Scan otherwise.
(The Scan is always CORRECT — it reads live attributes — just table-sized
instead of entity-sized.)

Writers keep stamping ``entity``/``pair`` inline (two attributes on rows they
already write); the values live here so writers and readers can't drift. See
docs/CONVENTIONS.md "Registry entity index".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

INDEX_NAME = "by-entity"

# The `entity` hash-key vocabulary (the `pair` range key is
# "<domain>/<dataset>", or "<domain>" for declared-domain META rows).
ENTITY_DATASET = "dataset"
ENTITY_DOMAIN = "domain"
ENTITY_XREF = "xref"

# The backfill's completion marker row, written by
# scripts/backfill_registry_entity.py via mark_entity_index_ready().
MARKER_PK = "REGISTRY"
MARKER_SK = "ENTITY_INDEX_READY"


def entity_pair(data_domain: str, dataset: str | None = None) -> str:
    """The ``pair`` range-key value for a row."""
    return f"{data_domain}/{dataset}" if dataset else data_domain


def entity_index_ready(ddb, table: str = "", *, resource: bool = False) -> bool:
    """Whether readers may treat the by-entity index as COMPLETE.

    True iff the backfill marker row exists. ``resource=True`` reads via a
    boto3 Table resource (plain keys, table name implied); the default is the
    low-level client + ``table``. Fail-closed: an unreadable marker means the
    Scan fallback — correct, just slower.
    """
    try:
        if resource:
            item = ddb.get_item(Key={"pk": MARKER_PK, "sk": MARKER_SK}).get("Item")
        else:
            item = ddb.get_item(
                TableName=table,
                Key={"pk": {"S": MARKER_PK}, "sk": {"S": MARKER_SK}},
            ).get("Item")
        return bool(item)
    except Exception:  # noqa: BLE001 - fall back to the (correct) scan
        return False


def mark_entity_index_ready(ddb, table: str) -> None:
    """Write the readiness marker (the backfill's LAST step; low-level client).

    Must only be called once every pre-existing row carries its
    ``entity``/``pair`` attributes — the marker is readers' permission to
    trust the index as the complete catalog.
    """
    ddb.put_item(
        TableName=table,
        Item={
            "pk": {"S": MARKER_PK},
            "sk": {"S": MARKER_SK},
            "stamped_at": {
                "S": datetime.now(timezone.utc).isoformat(timespec="seconds")
            },
        },
    )


def query_entity_rows(ddb, table: str, entity: str) -> list[dict[str, Any]]:
    """Every index row for one ``entity`` value (low-level client, full drain).

    Raises on failure — the caller decides whether a Scan fallback is safe
    (it is for full-drain readers; it is NOT mid-pagination).
    """
    rows: list[dict[str, Any]] = []
    q: dict[str, Any] = {
        "TableName": table,
        "IndexName": INDEX_NAME,
        "KeyConditionExpression": "entity = :e",
        "ExpressionAttributeValues": {":e": {"S": entity}},
    }
    while True:
        resp = ddb.query(**q)
        rows.extend(resp.get("Items") or [])
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return rows
        q["ExclusiveStartKey"] = lek


def is_missing_index_error(e: Exception) -> bool:
    """A Query that failed because the GSI does not exist (yet).

    The one error that means "this deployment's terraform hasn't applied the
    index" (e.g. the backfill marker was stamped before the apply) — readers
    may fall back to the Scan for it. Anything else — a throttle
    mid-pagination, a cursor replayed against the wrong query — must
    propagate: a silent Scan there would hand the caller the WHOLE catalog
    again, duplicating the pages it already consumed.

    Real DynamoDB reports a missing GSI as ``ValidationException`` ("The
    table does not have the specified index"); moto as
    ``ResourceNotFoundException`` ("Invalid index: …"). Both name the index
    in the message — a missing TABLE does not, so it still propagates.
    """
    err = getattr(e, "response", {}).get("Error", {}) or {}
    return err.get("Code") in (
        "ValidationException",
        "ResourceNotFoundException",
    ) and "index" in str(err.get("Message") or "").lower()
