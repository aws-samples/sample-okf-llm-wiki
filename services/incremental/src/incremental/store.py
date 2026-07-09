"""DynamoDB + Glue read/write helpers for the incremental path.

Centralizes the exact item shapes and key formats from docs/CONVENTIONS.md so
neither the event handler nor the reconcile pass hand-rolls (and drifts) a key.

DynamoDB clients here are *resources* (``boto3.resource("dynamodb").Table(...)``)
so tests can pass moto-backed tables. Glue is a plain boto3-style client and is
always injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from okf_core.session import HARVEST_LEASE_STALE_SECONDS
from okf_core.sources import normalize_source


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- key builders (docs/CONVENTIONS.md) --------------------------------------


def table_version_pk(data_domain: str, dataset: str, table: str) -> str:
    """Freshness-table PK for a table's stored Glue version."""
    return f"TABLE#{data_domain}#{dataset}#{table}"


def harvest_status_pk(data_domain: str, dataset: str) -> str:
    """Registry-table PK for a dataset's harvest status row."""
    return f"HARVEST#{data_domain}#{dataset}"


@dataclass(frozen=True)
class DatasetMapping:
    """A source (today: a Glue database) mapped to an OKF (data_domain, dataset).

    ``source`` is the first-class source descriptor (``{type, ...config}``, see
    ``okf_core.sources``); ``glue_database`` is retained as a convenience mirror
    of the glue source's config, so the change-event path (which resolves by
    Glue database name) keeps working unchanged.
    """

    data_domain: str
    dataset: str
    glue_database: str
    source: dict | None = None


# -- registry: domain <-> glue database mapping ------------------------------


def iter_dataset_mappings(registry_table) -> Iterator[DatasetMapping]:
    """Yield every DATASET item in the registry table.

    Registry DATASET items live at ``pk="DOMAIN#<domain>"`` /
    ``sk begins_with "DATASET#"`` with a ``glue_database`` attribute. We scan
    (the demo registry is tiny) and filter on the sk prefix + presence of a
    glue_database so partially-written rows are skipped.
    """
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": Attr("sk").begins_with("DATASET#")
        & Attr("glue_database").exists(),
    }
    while True:
        resp = registry_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            glue_db = item.get("glue_database")
            domain = item.get("data_domain")
            dataset = item.get("dataset")
            if glue_db and domain and dataset:
                # `source` is present on new rows (a plain dict via the resource
                # API); legacy rows have only the flat glue_database. Either way
                # normalize_source yields the canonical {type, ...config} shape.
                raw_source = item.get("source")
                source = normalize_source(
                    raw_source if isinstance(raw_source, dict) else None,
                    glue_database=glue_db,
                )
                yield DatasetMapping(
                    data_domain=domain,
                    dataset=dataset,
                    glue_database=glue_db,
                    source=source,
                )
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last


def resolve_domain(
    ddb, registry_table_name: str, database: str
) -> tuple[str, str] | None:
    """Map a Glue database name -> (data_domain, dataset), or None if unmapped.

    ``ddb`` is a ``boto3.resource("dynamodb")``. We look up the DATASET item whose
    ``glue_database`` equals ``database``. Returns the first match (databases map
    1:1 to datasets by convention).
    """
    table = ddb.Table(registry_table_name)
    for mapping in iter_dataset_mappings(table):
        if mapping.glue_database == database:
            return (mapping.data_domain, mapping.dataset)
    return None


def get_domain_context(
    ddb, registry_table_name: str, data_domain: str
) -> dict[str, str]:
    """Look up the declared domain's description/context from the DOMAIN#/META row.

    Returns ``{"domain_description": ..., "domain_context": ...}`` (both may be
    empty strings if the domain was not declared or has no context). Used to
    enrich the incremental harvest payload so re-authoring is domain-aware.
    """
    table = ddb.Table(registry_table_name)
    resp = table.get_item(
        Key={"pk": f"DOMAIN#{data_domain}", "sk": "META"},
    )
    item = resp.get("Item")
    if not item:
        return {"domain_description": "", "domain_context": ""}
    return {
        "domain_description": item.get("description", "") or "",
        "domain_context": item.get("context", "") or "",
    }


# -- freshness: stored table version -----------------------------------------


@dataclass(frozen=True)
class StoredVersion:
    version_id: str | None
    update_time: str | None


def get_stored_version(
    ddb, freshness_table_name: str, data_domain: str, dataset: str, table: str
) -> StoredVersion:
    """Read the TABLE#... / VERSION row; empty StoredVersion if absent."""
    tbl = ddb.Table(freshness_table_name)
    resp = tbl.get_item(
        Key={"pk": table_version_pk(data_domain, dataset, table), "sk": "VERSION"}
    )
    item = resp.get("Item")
    if not item:
        return StoredVersion(version_id=None, update_time=None)
    return StoredVersion(
        version_id=item.get("version_id"),
        update_time=item.get("update_time"),
    )


def put_stored_version(
    ddb,
    freshness_table_name: str,
    data_domain: str,
    dataset: str,
    table: str,
    *,
    version_id: str | None,
    update_time: str | None,
) -> None:
    """Upsert the TABLE#... / VERSION row to the newly observed Glue version."""
    tbl = ddb.Table(freshness_table_name)
    item: dict[str, Any] = {
        "pk": table_version_pk(data_domain, dataset, table),
        "sk": "VERSION",
        "last_seen_at": _now_iso(),
    }
    if version_id is not None:
        item["version_id"] = version_id
    if update_time is not None:
        item["update_time"] = update_time
    tbl.put_item(Item=item)


# -- registry: harvest status ------------------------------------------------


def put_harvest_status(
    ddb,
    registry_table_name: str,
    data_domain: str,
    dataset: str,
    *,
    status: str,
    mode: str,
    detail: str | None = None,
    runtime_session_id: str | None = None,
) -> None:
    """Upsert the HARVEST#... / STATUS row (docs/CONVENTIONS.md item shape)."""
    tbl = ddb.Table(registry_table_name)
    now = _now_iso()
    item: dict[str, Any] = {
        "pk": harvest_status_pk(data_domain, dataset),
        "sk": "STATUS",
        "status": status,
        "mode": mode,
        "updated_at": now,
    }
    if status in ("queued", "running"):
        item["started_at"] = now
    if detail is not None:
        item["detail"] = detail
    if runtime_session_id is not None:
        item["runtime_session_id"] = runtime_session_id
    tbl.put_item(Item=item)


def acquire_harvest_lease(
    ddb,
    registry_table_name: str,
    data_domain: str,
    dataset: str,
    *,
    mode: str,
    runtime_session_id: str,
    detail: str | None = None,
) -> bool:
    """Take the per-dataset harvest lease as ``queued``, or return False if busy.

    The resource-API twin of the Control API's ``acquire_harvest_lease`` (same
    HARVEST#.../STATUS row, same conditional). The lease lands only if there is
    no row, the last harvest is terminal (not queued/running), or the in-flight
    lease is STALE (``started_at`` older than ``HARVEST_LEASE_STALE_SECONDS`` — a
    dead job past AgentCore's 8h session cap). Returning False means a harvest is
    already in flight for this dataset, so the incremental orchestrator must NOT
    invoke a colliding second run on the shared bundle directory.
    """
    tbl = ddb.Table(registry_table_name)
    now = _now_iso()
    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=HARVEST_LEASE_STALE_SECONDS)
    ).isoformat()
    item: dict[str, Any] = {
        "pk": harvest_status_pk(data_domain, dataset),
        "sk": "STATUS",
        "status": "queued",
        "mode": mode,
        "started_at": now,
        "updated_at": now,
        "runtime_session_id": runtime_session_id,
    }
    if detail is not None:
        item["detail"] = detail
    try:
        tbl.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(pk) "
                "OR NOT (#s = :queued OR #s = :running) "
                "OR started_at < :stale"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":queued": "queued",
                ":running": "running",
                ":stale": stale_cutoff,
            },
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


# -- glue: current table version + version diff ------------------------------


def get_current_table(glue, database: str, table: str) -> dict[str, Any] | None:
    """glue.get_table -> the Table dict, or None if it no longer exists."""
    try:
        return glue.get_table(DatabaseName=database, Name=table)["Table"]
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("EntityNotFoundException", "ResourceNotFoundException", "404"):
            return None
        raise


def iso(value: Any) -> str | None:
    """Normalize a Glue timestamp (datetime or str) to an ISO string."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def get_two_latest_versions(
    glue, database: str, table: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (new_table, old_table) from the two most recent Glue versions.

    ``get_table_versions`` returns versions newest-first. If only one version
    exists (brand-new table), ``old_table`` is None so every column reads as
    "added". Each element is the ``Table`` dict from a ``TableVersion``.
    """
    resp = glue.get_table_versions(DatabaseName=database, TableName=table)
    versions = resp.get("TableVersions", [])
    if not versions:
        return (None, None)

    # Glue documents newest-first; sort defensively by VersionId (numeric).
    def _vkey(v: dict[str, Any]) -> int:
        try:
            return int(v.get("VersionId", 0))
        except (TypeError, ValueError):
            return 0

    versions = sorted(versions, key=_vkey, reverse=True)
    new_tbl = versions[0].get("Table")
    old_tbl = versions[1].get("Table") if len(versions) > 1 else None
    return (new_tbl, old_tbl)


def table_columns(table: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract StorageDescriptor.Columns[] from a Glue Table dict (or [])."""
    if not table:
        return []
    sd = table.get("StorageDescriptor") or {}
    return sd.get("Columns") or []
