"""Reconcile annotation verdicts back to DynamoDB after an annotation harvest.

The harvest AGENT has zero DynamoDB tools — it writes its per-annotation verdicts
to a results file on the mount (``.harvest/annotation_results.json``). The RUNNER
(this module) reads that file and flips each annotation's row to ``resolved`` with
the agent's outcome + comment, mirroring ``harvest.status``:

* **UpdateItem, not PutItem** — the Control API owns the row; we touch only
  ``status`` / ``outcome`` / ``resolution`` / ``updated_at`` / ``expires_at`` and
  never clobber the anchor fields (quote, note, author).
* **``expires_at`` (epoch seconds) is set HERE, at resolution** — an open/in_review
  annotation carries none and never expires; a resolved one lives 7 days as
  history (DynamoDB TTL).
* **Best-effort** — a DDB write must never crash a finished harvest; the S3 bundle
  is already the durable source of truth. Failures are swallowed and logged.

Item key (okf_core.annotations): pk ``ANNO#<domain>#<dataset>#<user_sub>``,
sk ``<concept_id>#<annotation_id>``. The runner has ``user_sub`` from the payload
and each verdict carries ``concept_id`` + ``annotation_id``, so the key is fully
reconstructable without reading the row first.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from okf_core import annotations as anno

log = logging.getLogger(__name__)

_COMMENT_MAX = 4000


def build_annotations_client() -> tuple[Any, str] | None:
    """Build a (dynamodb client, table name) from env, or None if unconfigured.

    Returns None (rather than raising) when boto3 or ``OKF_ANNOTATIONS_TABLE`` is
    absent, so the write-back degrades gracefully in tests / partial setups —
    exactly like ``status.build_registry_client``.
    """
    table = os.environ.get("OKF_ANNOTATIONS_TABLE")
    if not table:
        return None
    try:
        import boto3

        region = os.environ.get("AWS_REGION", "us-east-1")
        return boto3.client("dynamodb", region_name=region), table
    except Exception:  # noqa: BLE001 - write-back is best-effort
        log.warning(
            "Could not build DynamoDB client for annotation write-back", exc_info=True
        )
        return None


def _valid_outcome(outcome: str | None) -> str:
    """Coerce an agent-reported outcome to APPLIED or REJECTED.

    ORPHANED is never a valid agent verdict (orphans are resolved in the Control
    API pre-flight and never reach the agent). Anything unrecognized falls back to
    REJECTED — a conservative default: it keeps the note in history with the
    agent's comment rather than claiming an edit that may not have happened.
    """
    if outcome == anno.OUTCOME_APPLIED:
        return anno.OUTCOME_APPLIED
    return anno.OUTCOME_REJECTED


def resolve_annotation(
    client_table: tuple[Any, str] | None,
    *,
    data_domain: str,
    dataset: str,
    user_sub: str,
    concept_id: str,
    annotation_id: str,
    outcome: str,
    comment: str,
    now: datetime | None = None,
) -> bool:
    """Flip one annotation to resolved with the agent's verdict + comment. Never raises.

    Returns True on a successful write, False otherwise (unconfigured, or the
    write failed / was skipped). Conditioned on the row existing so a stale id
    from a malformed results file is a silent no-op, not a fresh ghost item.
    """
    if client_table is None:
        return False
    if not (user_sub and concept_id and annotation_id):
        return False
    client, table = client_table
    outcome = _valid_outcome(outcome)
    now = now or datetime.now(timezone.utc)
    expires_at = int((now + timedelta(seconds=anno.HISTORY_TTL_SECONDS)).timestamp())
    try:
        client.update_item(
            TableName=table,
            Key={
                "pk": {"S": anno.annotation_pk(data_domain, dataset, user_sub)},
                "sk": {"S": anno.annotation_sk(concept_id, annotation_id)},
            },
            UpdateExpression=(
                "SET #s = :s, outcome = :o, resolution = :r, "
                "updated_at = :u, expires_at = :e"
            ),
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": anno.STATUS_RESOLVED},
                ":o": {"S": outcome},
                ":r": {"S": (comment or "")[:_COMMENT_MAX]},
                ":u": {"S": now.isoformat(timespec="seconds")},
                ":e": {"N": str(expires_at)},
            },
        )
        return True
    except Exception as e:  # noqa: BLE001 - never let a write-back break a harvest
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            log.info(
                "Annotation %s/%s no longer exists; skipping resolution",
                concept_id,
                annotation_id,
            )
            return False
        log.warning(
            "Failed to resolve annotation %s/%s (continuing)",
            concept_id,
            annotation_id,
            exc_info=True,
        )
        return False


def revert_to_open(
    client_table: tuple[Any, str] | None,
    *,
    data_domain: str,
    dataset: str,
    user_sub: str,
    concept_id: str,
    annotation_id: str,
    now: datetime | None = None,
) -> None:
    """Return an in_review annotation to open (the agent never ruled on it).

    Used for survivors the agent left unaddressed — reverting (rather than
    force-resolving) means the feedback isn't lost: a later run picks it up. Only
    flips rows still ``in_review`` so a concurrently-resolved row isn't reopened.
    Best-effort; never raises.
    """
    if client_table is None or not (user_sub and concept_id and annotation_id):
        return
    client, table = client_table
    now = now or datetime.now(timezone.utc)
    try:
        client.update_item(
            TableName=table,
            Key={
                "pk": {"S": anno.annotation_pk(data_domain, dataset, user_sub)},
                "sk": {"S": anno.annotation_sk(concept_id, annotation_id)},
            },
            UpdateExpression="SET #s = :o, updated_at = :u",
            ConditionExpression="#s = :inr",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":o": {"S": anno.STATUS_OPEN},
                ":inr": {"S": anno.STATUS_IN_REVIEW},
                ":u": {"S": now.isoformat(timespec="seconds")},
            },
        )
    except Exception:  # noqa: BLE001 - best-effort; a failed revert isn't fatal
        # A rejected condition just means the row wasn't in_review anymore
        # (already resolved by the agent, or concurrently changed) — expected.
        log.info("Could not revert annotation %s/%s to open", concept_id, annotation_id)
