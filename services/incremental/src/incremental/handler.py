"""Incremental orchestrator Lambda: Glue table-change events -> scoped re-harvest.

Flow (design §4):

  EventBridge "Glue Data Catalog Table State Change" (source aws.glue)
    -> rule -> SQS -> this Lambda

For each event ``detail`` ``{databaseName, tableName, typeOfChange,
changedPartitions}`` we:

1. Map ``databaseName`` -> ``(data_domain, dataset)`` via the registry table.
   Unmapped databases are ignored (logged + skipped) — the catalog holds many
   databases we don't manage.
2. Confirm a *real* change: compare the stored ``version_id`` (freshness table
   ``TABLE#...`` / ``VERSION``) against the current ``Table.VersionId``. If the
   version is unchanged AND there are no ``changedPartitions``, skip (dedup —
   Glue emits multiple events per logical change). A partition-only change (same
   version, non-empty ``changedPartitions``) still triggers a re-review because
   new partitions can surface data the catalog schema doesn't describe.
3. Compute a column diff between the two latest table versions.
4. InvokeAgentRuntime on the harvest runtime scoped to the changed table (the
   diff rides in the payload; the runtime writes ``.harvest/pending.json``
   itself, through the S3 Files mount), then record the new version and set the
   harvest status row to ``queued``.

``process_event`` is a pure function with every AWS client injected so it is
unit-testable with moto + fakes. ``lambda_handler`` is the thin SQS wrapper that
builds clients from the environment and returns partial-batch failures.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from okf_aws.s3_bundle import is_bundle_ready
from okf_core.session import runtime_session_id

from incremental import store
from incremental.diff import compute_column_diff

log = logging.getLogger("incremental.handler")


def _session_id(data_domain: str, dataset: str) -> str:
    """AgentCore runtimeSessionId — one deterministic session per dataset.

    Delegates to okf_core so the Control API and this path build the SAME id for
    a dataset (and it satisfies AgentCore's 33-256 char length constraint).
    """
    return runtime_session_id(data_domain, dataset)


def process_event(
    detail: dict[str, Any],
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
    """Handle one Glue table-change ``detail``. Returns a result dict for logging.

    ``result["action"]`` is one of ``skipped_unmapped``, ``skipped_no_table``,
    ``skipped_unchanged`` or ``invoked``. Raising propagates to the caller so the
    SQS record is retried; the returned dict never signals failure.
    """
    database = detail.get("databaseName")
    table = detail.get("tableName")
    changed_partitions = detail.get("changedPartitions") or []
    if not database or not table:
        log.warning("Event missing databaseName/tableName: %s", detail)
        return {"action": "skipped_unmapped", "reason": "missing db/table"}

    # 1. Map glue database -> (data_domain, dataset).
    mapping = store.resolve_domain(ddb, registry_table, database)
    if mapping is None:
        log.info("Ignoring unmapped Glue database: %s", database)
        return {"action": "skipped_unmapped", "database": database}
    data_domain, dataset = mapping

    # 1b. Require an existing (complete) bundle before doing incremental work.
    # An incremental re-harvest only makes sense if a FULL harvest has already
    # authored the bundle. Beyond being a no-op otherwise, staging pending.json
    # here (a raw s3.put_object) BEFORE the bundle exists materializes the
    # okf/<domain>/<dataset>/.harvest/ path as plain S3 prefixes that the harvest
    # runtime's S3 Files mount cannot then write into (EACCES) — which wedges the
    # subsequent full harvest. So if the bundle isn't ready, skip entirely and let
    # the full harvest (Control API / nightly reconcile) establish it via the mount.
    if not is_bundle_ready(s3, bundle_bucket, data_domain, dataset):
        log.info(
            "Bundle not ready for %s/%s; skipping incremental (a full harvest must "
            "author it first)",
            data_domain,
            dataset,
        )
        return {
            "action": "skipped_no_bundle",
            "data_domain": data_domain,
            "dataset": dataset,
            "table": table,
        }

    # 2. Confirm a real change.
    current = store.get_current_table(glue, database, table)
    if current is None:
        # Table was dropped between the event and now; nothing to re-harvest.
        log.info("Table %s.%s no longer exists; skipping", database, table)
        return {
            "action": "skipped_no_table",
            "data_domain": data_domain,
            "dataset": dataset,
            "table": table,
        }
    current_version = current.get("VersionId")
    current_update = store.iso(current.get("UpdateTime"))

    stored = store.get_stored_version(ddb, freshness_table, data_domain, dataset, table)
    version_unchanged = (
        stored.version_id is not None and stored.version_id == current_version
    )
    if version_unchanged and not changed_partitions:
        # Duplicate/no-op event: same version, no new partitions -> dedup.
        log.info(
            "No change for %s.%s (version %s); skipping",
            database,
            table,
            current_version,
        )
        return {
            "action": "skipped_unchanged",
            "data_domain": data_domain,
            "dataset": dataset,
            "table": table,
            "version_id": current_version,
        }

    # 3. Compute the column diff between the two latest versions.
    # Only a version bump can change columns; a partition-only change (same
    # version, non-empty changedPartitions) has an empty column diff but still
    # triggers a re-review via the invoke below.
    if version_unchanged:
        diff = {"added": [], "removed": [], "retyped": []}
    else:
        new_tbl, old_tbl = store.get_two_latest_versions(glue, database, table)
        diff = compute_column_diff(
            store.table_columns(old_tbl),
            store.table_columns(new_tbl) or store.table_columns(current),
        )

    # 4. Acquire the per-dataset harvest lease BEFORE staging/invoking. This is
    # the same lease the Control API's trigger_harvest takes, so an incremental
    # re-harvest can never collide with a full harvest (or another incremental)
    # of the same dataset — otherwise a full harvest's clean_authored_output
    # (rm -rf of the bundle root) would race this run's edits and corrupt the
    # bundle. If a harvest is already in flight we SKIP WITHOUT recording the new
    # version, so the change is re-detected (by the next event or the nightly
    # reconcile) once the in-flight harvest finishes — no update is lost.
    session_id = _session_id(data_domain, dataset)
    if not store.acquire_harvest_lease(
        ddb,
        registry_table,
        data_domain,
        dataset,
        mode="incremental",
        runtime_session_id=session_id,
        detail=f"incremental re-harvest of table '{table}'",
    ):
        log.info(
            "Harvest already in flight for %s/%s; deferring table=%s (will be "
            "re-detected)",
            data_domain,
            dataset,
            table,
        )
        return {
            "action": "skipped_locked",
            "data_domain": data_domain,
            "dataset": dataset,
            "table": table,
            "version_id": current_version,
        }

    # Lease held. The diff travels to the agent in the invoke payload below; the
    # harvest runtime itself writes `.harvest/pending.json` THROUGH the S3 Files
    # mount (see run_incremental_harvest). We deliberately do NOT stage it here
    # with a raw s3.put_object: that write bypasses the mount and materializes the
    # `.harvest/` dir owned by root, which the runtime's uid-1000 mount identity
    # then can't write into — an EACCES that wedges the harvest (finalize's
    # mark_in_progress). Delivering the diff in the payload keeps the mount the
    # sole writer of the bundle tree.

    # 5. Invoke the harvest runtime scoped to this table. On failure, release the
    # lease (mark the row failed) and DO NOT record the new version, so a retry
    # re-detects the change and isn't blocked by our own queued row.
    # Enrich with domain description/context so incremental re-authoring is
    # domain-aware (best-effort: empty strings if domain not declared).
    domain_ctx = store.get_domain_context(ddb, registry_table, data_domain)
    payload = {
        "data_domain": data_domain,
        "dataset": dataset,
        "mode": "incremental",
        "changed_table": table,
        "diff": diff,
    }
    if domain_ctx.get("domain_description"):
        payload["domain_description"] = domain_ctx["domain_description"]
    if domain_ctx.get("domain_context"):
        payload["domain_context"] = domain_ctx["domain_context"]
    try:
        agentcore.invoke_agent_runtime(
            agentRuntimeArn=harvest_runtime_arn,
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
            payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as e:  # noqa: BLE001 - release the lease, then re-raise
        try:
            store.put_harvest_status(
                ddb,
                registry_table,
                data_domain,
                dataset,
                status="failed",
                mode="incremental",
                detail=f"incremental invoke failed: {type(e).__name__}",
                runtime_session_id=session_id,
            )
        except Exception:  # noqa: BLE001 - best-effort lease release
            pass
        raise

    # 6. Record the new version ONLY after a successful invoke (so a failed run
    # doesn't dedup the change away). The lease row is already 'queued' from the
    # acquire above; the agent will advance it to running/complete/failed.
    store.put_stored_version(
        ddb,
        freshness_table,
        data_domain,
        dataset,
        table,
        version_id=current_version,
        update_time=current_update,
    )

    log.info(
        "Queued incremental re-harvest %s/%s table=%s version=%s",
        data_domain,
        dataset,
        table,
        current_version,
    )
    return {
        "action": "invoked",
        "data_domain": data_domain,
        "dataset": dataset,
        "table": table,
        "version_id": current_version,
        "diff": diff,
    }


# -- SQS record parsing ------------------------------------------------------


def _extract_detail(record_body: str) -> dict[str, Any]:
    """Pull the Glue event ``detail`` out of an SQS record body.

    EventBridge -> SQS delivers the full EventBridge envelope as the SQS message
    body: ``{"source", "detail-type", "detail": {...}, ...}``. Some setups wrap
    it further, so we accept a bare detail too (has ``databaseName``).
    """
    parsed = json.loads(record_body)
    if isinstance(parsed, dict) and "detail" in parsed:
        return parsed["detail"] or {}
    return parsed if isinstance(parsed, dict) else {}


def _client_factory():
    """Build the AWS clients from the environment (Lambda cold-start wiring)."""
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "glue": boto3.client("glue", region_name=region),
        "ddb": boto3.resource("dynamodb", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "agentcore": boto3.client("bedrock-agentcore", region_name=region),
    }


def lambda_handler(
    event, context=None, *, clients_factory: Callable[[], dict] | None = None
):
    """SQS entrypoint. Processes each record; returns partial-batch failures.

    We use ``ReportBatchItemFailures``: a record that raises is reported in
    ``batchItemFailures`` so only *it* is retried, not the whole batch. Records
    that skip (unmapped / unchanged) succeed — there is nothing to retry.
    """
    clients = (clients_factory or _client_factory)()
    bundle_bucket = os.environ["OKF_BUNDLE_BUCKET"]
    registry_table = os.environ.get("OKF_REGISTRY_TABLE", "okf-registry")
    freshness_table = os.environ.get("OKF_FRESHNESS_TABLE", "okf-freshness")
    harvest_runtime_arn = os.environ["OKF_HARVEST_RUNTIME_ARN"]

    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            detail = _extract_detail(record.get("body", "{}"))
            process_event(
                detail,
                glue=clients["glue"],
                ddb=clients["ddb"],
                s3=clients["s3"],
                agentcore=clients["agentcore"],
                bundle_bucket=bundle_bucket,
                registry_table=registry_table,
                freshness_table=freshness_table,
                harvest_runtime_arn=harvest_runtime_arn,
            )
        except Exception:  # noqa: BLE001 - one bad record must not fail the batch
            log.exception("Failed to process SQS record %s", message_id)
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
