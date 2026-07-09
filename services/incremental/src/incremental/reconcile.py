"""Nightly reconcile pass: catch missed Glue change events.

EventBridge delivery is best-effort and Glue events can be dropped or arrive
during a Lambda outage. The reconcile pass is the safety net: it walks every
mapped dataset, and for every table in that dataset's Glue database compares the
stored ``version_id`` / ``update_time`` (freshness table) against Glue's current
value. Any drift is turned into a synthetic change ``detail`` and run through the
same :func:`incremental.handler.process_event` path, so the reconcile pass reuses
all of the dedup, diffing, staging and invoke logic.

``reconcile`` takes injected clients (same as ``process_event``) so it is
unit-testable; ``reconcile_handler`` is the thin scheduled-event wrapper.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from incremental import store
from incremental.handler import _client_factory, process_event

log = logging.getLogger("incremental.reconcile")


def _iter_tables(glue, database: str):
    """Yield every Glue table Name in ``database`` (paginated, 100/page)."""
    kwargs: dict[str, Any] = {"DatabaseName": database}
    token = None
    while True:
        if token:
            kwargs["NextToken"] = token
        resp = glue.get_tables(**kwargs)
        for tbl in resp.get("TableList", []):
            name = tbl.get("Name")
            if name:
                yield name, tbl
        token = resp.get("NextToken")
        if not token:
            break


def reconcile(
    *,
    glue,
    ddb,
    s3,
    agentcore,
    bundle_bucket: str,
    registry_table: str,
    freshness_table: str,
    harvest_runtime_arn: str,
) -> dict[str, Any]:
    """Scan all mapped datasets and enqueue incrementals for drifted tables.

    Returns a summary ``{scanned_datasets, scanned_tables, enqueued, drifted}``.
    Per-table errors are logged and counted but never abort the whole pass — a
    single broken table must not block reconciliation of the rest.
    """
    reg = ddb.Table(registry_table)
    scanned_datasets = 0
    scanned_tables = 0
    enqueued = 0
    errors = 0
    drifted: list[dict[str, str]] = []

    for mapping in store.iter_dataset_mappings(reg):
        scanned_datasets += 1
        database = mapping.glue_database
        for table_name, current in _iter_tables(glue, database):
            scanned_tables += 1
            current_version = current.get("VersionId")
            stored = store.get_stored_version(
                ddb, freshness_table, mapping.data_domain, mapping.dataset, table_name
            )
            # Drift = the stored version doesn't match Glue's current version
            # (including never-seen tables, where stored.version_id is None). This
            # mirrors process_event's version-based dedup so a drifted table is
            # guaranteed to pass through to an invoke rather than get re-skipped.
            # Glue's VersionId is monotonic, so any real schema change bumps it.
            if stored.version_id == current_version:
                continue
            drifted.append({"database": database, "table": table_name})
            # Synthesize the same detail an EventBridge event would carry so the
            # regular process_event path handles dedup + diff + invoke.
            detail = {
                "databaseName": database,
                "tableName": table_name,
                "typeOfChange": "UpdateTable",
                "changedPartitions": [],
            }
            try:
                result = process_event(
                    detail,
                    glue=glue,
                    ddb=ddb,
                    s3=s3,
                    agentcore=agentcore,
                    bundle_bucket=bundle_bucket,
                    registry_table=registry_table,
                    freshness_table=freshness_table,
                    harvest_runtime_arn=harvest_runtime_arn,
                )
                if result.get("action") == "invoked":
                    enqueued += 1
            except Exception:  # noqa: BLE001 - keep reconciling the rest
                errors += 1
                log.exception("Reconcile failed for %s.%s", database, table_name)

    summary = {
        "scanned_datasets": scanned_datasets,
        "scanned_tables": scanned_tables,
        "enqueued": enqueued,
        "errors": errors,
        "drifted": drifted,
    }
    log.info("Reconcile complete: %s", summary)
    return summary


def reconcile_handler(
    event=None, context=None, *, clients_factory: Callable[[], dict] | None = None
):
    """Scheduled (nightly) entrypoint. Builds clients from env and reconciles."""
    clients = (clients_factory or _client_factory)()
    return reconcile(
        glue=clients["glue"],
        ddb=clients["ddb"],
        s3=clients["s3"],
        agentcore=clients["agentcore"],
        bundle_bucket=os.environ["OKF_BUNDLE_BUCKET"],
        registry_table=os.environ.get("OKF_REGISTRY_TABLE", "okf-registry"),
        freshness_table=os.environ.get("OKF_FRESHNESS_TABLE", "okf-freshness"),
        harvest_runtime_arn=os.environ["OKF_HARVEST_RUNTIME_ARN"],
    )
