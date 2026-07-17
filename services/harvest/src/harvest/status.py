"""Report harvest lifecycle status back to the DynamoDB registry.

The Control API writes the ``HARVEST#<domain>#<dataset> / STATUS`` row as
``queued`` when it invokes the runtime, but it can't know when the agent
actually starts, finishes, or fails — the crawl runs here, inside AgentCore. So
the AGENT owns the lifecycle after ``queued``: ``running`` when it picks the job
up, then ``complete`` on success or ``failed`` (with a short detail) on error.
This is what the UI's ``GET /harvest`` polls (docs/CONVENTIONS.md item shape).

Two rules this module enforces:

* **UpdateItem, not PutItem.** The ``queued`` row already carries ``mode``,
  ``started_at`` and ``runtime_session_id``; we must touch only ``status`` /
  ``updated_at`` / ``detail`` so those aren't clobbered. (UpdateItem also creates
  the row if a harvest was triggered out-of-band without a ``queued`` row.)
* **Best-effort.** A registry write must NEVER crash a harvest — the S3 commit
  marker is the durable source of truth for consumability. Every failure here is
  swallowed and logged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Keep failure details bounded so a giant traceback can't bloat the item.
_DETAIL_MAX = 1024


def build_registry_client() -> tuple[Any, str] | None:
    """Build a (dynamodb client, table name) from env, or None if unconfigured.

    Returns None (rather than raising) when boto3 or ``OKF_REGISTRY_TABLE`` is
    absent, so status reporting degrades gracefully in tests / partial setups.
    """
    table = os.environ.get("OKF_REGISTRY_TABLE")
    if not table:
        return None
    try:
        import boto3

        region = os.environ.get("AWS_REGION", "us-east-1")
        return boto3.client("dynamodb", region_name=region), table
    except Exception:  # noqa: BLE001 - status reporting is best-effort
        log.warning(
            "Could not build DynamoDB client for status reporting", exc_info=True
        )
        return None


def report_status(
    registry: tuple[Any, str] | None,
    *,
    data_domain: str,
    dataset: str,
    status: str,
    detail: str | None = None,
    only_if_active: bool = False,
    model: str | None = None,
    effort: str | None = None,
) -> None:
    """Best-effort transition of the harvest status row to ``status``.

    ``registry`` is the (client, table) tuple from :func:`build_registry_client`
    (or None — then this is a no-op). Only ``status`` / ``updated_at`` (/ optional
    ``detail`` / ``model`` / ``effort``) are written; ``mode`` / ``started_at`` /
    ``runtime_session_id`` set at ``queued`` time are preserved. Never raises.

    ``model``/``effort`` record the RESOLVED LLM config actually used for this run
    (override or deploy-time default) so the UI can show what a harvest ran on.
    Written on the ``running`` transition (the runner knows the resolved config by
    then); passing them on other transitions just re-stamps the same values.

    ``only_if_active`` guards the write with a condition that the row is still
    ``queued`` or ``running`` — used for the terminal ``complete``/``failed``
    transitions so they never CLOBBER a ``cancelled`` row. When an operator
    cancels, the Control API stops the AgentCore session; the crawl thread then
    typically dies with an exception (e.g. ``cannot schedule new futures after
    shutdown`` from the torn-down QuickJS worker) and the runner tries to report
    ``failed`` — this guard makes that a no-op so the status stays ``cancelled``.
    A rejected conditional write is expected here, not an error.
    """
    if registry is None:
        return
    client, table = registry
    try:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        expr = "SET #s = :s, updated_at = :u"
        names = {"#s": "status"}  # "status" is a DynamoDB reserved word
        values = {":s": {"S": status}, ":u": {"S": now}}
        if detail is not None:
            expr += ", detail = :d"
            values[":d"] = {"S": detail[:_DETAIL_MAX]}
        if model:
            # Alias via ExpressionAttributeNames — cheap insurance against a
            # DynamoDB reserved word collision on the attribute name.
            expr += ", #m = :m"
            names["#m"] = "model"
            values[":m"] = {"S": model}
        if effort:
            expr += ", #e = :e"
            names["#e"] = "effort"
            values[":e"] = {"S": effort}
        kwargs: dict[str, Any] = {
            "TableName": table,
            "Key": {
                "pk": {"S": f"HARVEST#{data_domain}#{dataset}"},
                "sk": {"S": "STATUS"},
            },
            "UpdateExpression": expr,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        if only_if_active:
            # Row must still be in flight (or absent) to accept a terminal write.
            kwargs["ConditionExpression"] = (
                "attribute_not_exists(pk) OR #s = :queued OR #s = :running"
            )
            values[":queued"] = {"S": "queued"}
            values[":running"] = {"S": "running"}
        client.update_item(**kwargs)
        log.info("Harvest status -> %s (%s/%s)", status, data_domain, dataset)
    except Exception as e:  # noqa: BLE001 - never let a registry write break a harvest
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            # Row already terminal (e.g. cancelled) — intentionally not overwritten.
            log.info(
                "Harvest status=%s skipped for %s/%s (row already terminal)",
                status,
                data_domain,
                dataset,
            )
            return
        log.warning(
            "Failed to report harvest status=%s for %s/%s (continuing)",
            status,
            data_domain,
            dataset,
            exc_info=True,
        )


def stamp_guidance_applied(
    registry: tuple[Any, str] | None,
    *,
    data_domain: str,
    dataset: str,
    version: str | None,
) -> None:
    """Record that dataset guidance version ``version`` was applied by this harvest.

    Writes ``guidance_applied_version`` onto the dataset's mapping row
    (``pk="DOMAIN#<d>"``, ``sk="DATASET#<ds>"``) so the guidance clears its DIRTY
    state (okf_core.guidance.is_dirty compares this to ``guidance_updated_at``).
    Called ONLY after a successful ``finalize_bundle`` — a failed run never stamps,
    so dirty guidance stays dirty until it actually lands. Stamps the VERSION that
    ran (not "now"), so an edit made mid-run keeps the guidance dirty. No-op when
    ``version`` is falsy (the run carried no guidance) or the registry is
    unconfigured. Never raises — a stamp failure must not fail a finalized bundle.
    """
    if registry is None or not version:
        return
    client, table = registry
    try:
        client.update_item(
            TableName=table,
            Key={
                "pk": {"S": f"DOMAIN#{data_domain}"},
                "sk": {"S": f"DATASET#{dataset}"},
            },
            UpdateExpression="SET guidance_applied_version = :v",
            # Only stamp a row that still exists (mapping not deleted mid-run).
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":v": {"S": version}},
        )
        log.info(
            "Stamped guidance_applied_version=%s (%s/%s)", version, data_domain, dataset
        )
    except Exception:  # noqa: BLE001 - best-effort; never break a finalized bundle
        log.warning(
            "Failed to stamp guidance_applied_version for %s/%s (continuing)",
            data_domain,
            dataset,
            exc_info=True,
        )
