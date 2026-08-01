"""AR policy rebuild authority: restore snapshots, dispatch authoring, finish builds.

The harvest finalize hook can only act while a harvest is running — this module
is the other half of the policy lifecycle, reached by ``policy_rebuild`` events
(EventBridge -> the same SQS queue as the Glue events; published by the Control
API's repromote/enroll/sync paths and by the chat runtime's stale discovery)
and by the nightly reconcile.

The Lambda itself does only DETERMINISTIC work; anything needing a model runs
on the harvest runtime:

* **Restore** — when a content-addressed snapshot covers the live wiki's
  fingerprint (a repromote, any A→B→A edit cycle), the EXACT solver rules are
  pushed back via the Bedrock control plane in seconds: no agent, no ingest
  build, fidelity restored verbatim (never re-measured — operator decision).
* **Author dispatch** — a never-built source state needs the rules-authoring
  agent, which is minutes of reasoning work with LangChain deps this Lambda
  doesn't carry: it INVOKES the harvest runtime with ``mode="ar_rules"`` (the
  same fire-and-forget call the incremental re-harvest path makes) and lets
  the runtime own the ``building`` flip, so N duplicate events still collapse
  to one authoring run.
* **Completion** — the nightly reconcile polls ``building`` workflows; a
  COMPLETED one is versioned, wired to its guardrail, stamped — and SNAPSHOTTED,
  which is what makes every later restore possible.

Everything is gated on ``OKF_POLICY_BUILD_ENABLED`` (default off) and on the
per-dataset ``ar_enrolled`` opt-in, with per-dataset try/except so one broken
dataset never blocks the rest.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_BUILD_WORKFLOW_ID,
    ATTR_PENDING_SOURCE_HASH,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    BUILD_STALE,
    BUILD_WORKFLOW_CANCELLED,
    BUILD_WORKFLOW_COMPLETED,
    BUILD_WORKFLOW_FAILED,
    env_guardrail_profile,
    USABLE_BUILD_STATUSES,
    dataset_label,
    ensure_policy,
    finish_completed_build,
    gather_sources,
    get_build_status,
    hash_sources,
    persist_author_state,
    policy_name,
    read_snapshot,
    region_supported,
    restore_snapshot,
    stamp_build_failed,
    try_flip_building,
)
from okf_aws.s3_bundle import is_bundle_ready

log = logging.getLogger("incremental.ar_rebuild")

#: A row flipped to ``building`` whose Start call never landed (crash between
#: the flip and the workflow id stamp). After this grace period the reconcile
#: reaps it to ``failed`` so the fingerprint check can rebuild it.
REASON_ABANDONED = "abandoned_build"
_ABANDONED_AFTER = timedelta(hours=1)

# run_rebuild outcomes — grep-compatible with the harvest trigger's vocabulary.
OUTCOME_DISABLED = "disabled"
OUTCOME_NOT_ENROLLED = "not_enrolled"
OUTCOME_UNSUPPORTED_REGION = "unsupported_region"
OUTCOME_NO_SOURCES = "no_sources"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_LOCKED = "locked"
OUTCOME_RESTORED = "restored"
OUTCOME_INVOKED = "invoked"
OUTCOME_COMPLETED = "completed"  # a terminal workflow was stamped onto the row
OUTCOME_IN_FLIGHT = "in_flight"  # the workflow is genuinely still running
OUTCOME_ERROR = "error"


def rebuild_enabled() -> bool:
    """True when this deployment builds AR policies (default: false)."""
    return os.environ.get("OKF_POLICY_BUILD_ENABLED", "").lower() in (
        "true",
        "1",
        "yes",
    )


def run_rebuild(
    data_domain: str,
    dataset: str,
    *,
    ddb,
    table: str,
    s3,
    bucket: str,
    bedrock: Any = None,
    agentcore: Any = None,
    harvest_runtime_arn: str = "",
) -> str:
    """Bring one dataset's policy up to date. NEVER raises.

    Restore in-process when a snapshot covers the live fingerprint; otherwise
    dispatch the authoring agent to the harvest runtime. ``ddb`` is the
    LOW-LEVEL DynamoDB client (the registry stamp helpers speak typed
    attributes).
    """
    if not rebuild_enabled():
        return OUTCOME_DISABLED
    try:
        return _run_rebuild(
            data_domain,
            dataset,
            ddb=ddb,
            table=table,
            s3=s3,
            bucket=bucket,
            bedrock=bedrock,
            agentcore=agentcore,
            harvest_runtime_arn=harvest_runtime_arn,
        )
    except Exception:  # noqa: BLE001 - a rebuild is advisory, never a Lambda error
        log.error(
            "AR policy rebuild failed for %s/%s (the nightly reconcile will retry)",
            data_domain,
            dataset,
            exc_info=True,
        )
        return OUTCOME_ERROR


def _run_rebuild(
    data_domain: str,
    dataset: str,
    *,
    ddb,
    table: str,
    s3,
    bucket: str,
    bedrock: Any,
    agentcore: Any,
    harvest_runtime_arn: str,
) -> str:
    # Enrollment first (the per-dataset opt-in from the Reasoning page): a
    # rebuild event for an unenrolled dataset — a late event, a duplicate that
    # raced an unenroll — must cost one GetItem and change nothing.
    enrolled, status, stored_hash, guardrail_id, item = _read_build_state(
        ddb, table, data_domain=data_domain, dataset=dataset
    )
    if not enrolled:
        return OUTCOME_NOT_ENROLLED

    region = os.environ.get("AWS_REGION", "us-east-1")
    if not region_supported(region):
        log.info(
            "AR rebuild skipped for %s/%s: Automated Reasoning is not offered in %s",
            data_domain,
            dataset,
            region,
        )
        return OUTCOME_UNSUPPORTED_REGION

    # A `building` row is checked for COMPLETION first: the harvest runtime
    # normally finishes its own build in-session, but a poll overrun, a
    # transient completion error, or a killed session leaves the row here
    # with the workflow id stamped — and the workflow result is durable, so
    # the manual Sync (this path) finishes the stamp rather than skipping as
    # "unchanged" (which would strand the row until the opt-in nightly
    # reconcile — the exact stuck-at-building state observed live).
    if status == BUILD_BUILDING:
        result = _complete_one(
            item, ddb=ddb, table=table, s3=s3, bucket=bucket,
            bedrock=bedrock or _bedrock_client(),
        )
        if result == "in_flight":
            return OUTCOME_IN_FLIGHT
        if result == "completed":
            return OUTCOME_COMPLETED
        # failed / reaped: the lease was released — fall through to a fresh
        # decision (a Sync after a failed workflow immediately re-authors).
        _, status, stored_hash, guardrail_id, item = _read_build_state(
            ddb, table, data_domain=data_domain, dataset=dataset
        )

    sources = gather_sources(s3, bucket, data_domain, dataset)
    fresh_hash = hash_sources(sources)
    if not fresh_hash:
        return OUTCOME_NO_SOURCES

    if stored_hash == fresh_hash and status in USABLE_BUILD_STATUSES:
        return OUTCOME_UNCHANGED

    snapshot = read_snapshot(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        fingerprint=fresh_hash,
    )
    if snapshot is not None:
        # Deterministic restore, in-process: control-plane calls + S3 copies,
        # seconds. The flip is still the serialization point.
        if not try_flip_building(
            ddb, table, data_domain=data_domain, dataset=dataset,
            pending_hash=fresh_hash,
        ):
            return OUTCOME_LOCKED
        try:
            restore_snapshot(
                bedrock or _bedrock_client(),
                ddb,
                s3,
                table=table,
                bucket=bucket,
                data_domain=data_domain,
                dataset=dataset,
                snapshot=snapshot,
                guardrail_profile=env_guardrail_profile(),
                guardrail_id=guardrail_id or None,
            )
            persist_author_state(
                s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
                sources=sources, rules_text=str(snapshot.get("rules_text") or ""),
            )
        except Exception as e:  # noqa: BLE001 - release the lease, then report
            stamp_build_failed(
                ddb, table, data_domain=data_domain, dataset=dataset,
                reason=f"restore: {type(e).__name__}: {e}",
            )
            raise
        log.info(
            "AR policy for %s/%s restored from snapshot %s",
            data_domain, dataset, fresh_hash[:12],
        )
        return OUTCOME_RESTORED

    # Never-built state: the authoring agent runs on the harvest runtime (it
    # owns the model factory + LangChain; this Lambda deliberately doesn't).
    # No flip here — the runtime's own conditional flip is the dedup point, so
    # duplicate events cost one no-op session, never a duplicate build.
    if agentcore is None or not harvest_runtime_arn:
        log.warning(
            "AR rebuild for %s/%s needs the authoring agent but no harvest "
            "runtime is configured",
            data_domain,
            dataset,
        )
        return OUTCOME_ERROR
    from okf_core.session import runtime_session_id

    # FRESH session per dispatch (unique_token), never deterministic: an
    # AgentCore session is pinned to the runtime VERSION it started on, so a
    # reused id within the idle window reattaches to a warm microVM running
    # PRE-DEPLOY code — which silently defeats the Reasoning page's
    # retry-after-a-fix flow. Dedup doesn't need the session: the runtime's
    # conditional `building` flip already collapses duplicate events.
    agentcore.invoke_agent_runtime(
        agentRuntimeArn=harvest_runtime_arn,
        runtimeSessionId=runtime_session_id(
            data_domain, f"{dataset}--ar-rules", unique_token=uuid.uuid4().hex
        ),
        qualifier="DEFAULT",
        payload=json.dumps(
            {"mode": "ar_rules", "data_domain": data_domain, "dataset": dataset}
        ).encode("utf-8"),
    )
    log.info("AR authoring dispatched for %s/%s", data_domain, dataset)
    return OUTCOME_INVOKED


def reconcile_policies(
    *,
    ddb,
    table: str,
    s3,
    bucket: str,
    bedrock: Any = None,
    agentcore: Any = None,
    harvest_runtime_arn: str = "",
) -> dict[str, Any]:
    """The nightly pass: complete in-flight builds, then hash-verify everything.

    Walks every ENROLLED ``DATASET#`` registry row (ALL sources, not just Glue
    — a Redshift-backed dataset gets a policy too; unenrolled datasets are the
    steady state and aren't even counted). Per-dataset errors are counted and
    logged, never propagated: one broken dataset must not block the rest.
    Returns ``{datasets, completed, restored, rebuilt, in_flight, skipped, errors}``.
    """
    summary = {
        "datasets": 0,
        "completed": 0,
        "restored": 0,
        "rebuilt": 0,
        "in_flight": 0,
        "skipped": 0,
        "errors": 0,
    }
    from okf_aws.ar_policy import is_enrolled

    for item in _iter_dataset_items(ddb, table):
        data_domain = _s(item, "data_domain")
        dataset = _s(item, "dataset")
        if not data_domain or not dataset:
            continue
        # Only enrolled datasets are maintained (and only enrolled ones can be
        # `building` — unenroll is refused mid-build, so completion never runs
        # for an unenrolled row either). Skipping silently: an unenrolled
        # dataset having no policy is the steady state, not a condition.
        if not is_enrolled(item):
            continue
        summary["datasets"] += 1
        try:
            status = _s(item, ATTR_BUILD_STATUS)
            if status == BUILD_BUILDING:
                outcome = _complete_one(
                    item,
                    ddb=ddb,
                    table=table,
                    s3=s3,
                    bucket=bucket,
                    bedrock=bedrock or _bedrock_client(),
                )
                if outcome == "completed":
                    summary["completed"] += 1
                elif outcome == "in_flight":
                    summary["in_flight"] += 1
                else:  # failed / reaped — terminal without a usable policy
                    summary["errors"] += 1
                continue

            fresh = hash_sources(gather_sources(s3, bucket, data_domain, dataset))
            stored = _s(item, ATTR_SOURCE_HASH)
            needs_rebuild = bool(fresh) and (fresh != stored or status == BUILD_STALE)
            if not needs_rebuild:
                summary["skipped"] += 1
                continue
            # Only bundles a full harvest has committed get policies — the
            # backfill path must not race a first harvest mid-author.
            if not is_bundle_ready(s3, bucket, data_domain, dataset):
                summary["skipped"] += 1
                continue
            outcome = run_rebuild(
                data_domain,
                dataset,
                ddb=ddb,
                table=table,
                s3=s3,
                bucket=bucket,
                bedrock=bedrock,
                agentcore=agentcore,
                harvest_runtime_arn=harvest_runtime_arn,
            )
            if outcome == OUTCOME_RESTORED:
                summary["restored"] += 1
            elif outcome == OUTCOME_INVOKED:
                summary["rebuilt"] += 1
            elif outcome == OUTCOME_ERROR:
                summary["errors"] += 1
            else:
                summary["skipped"] += 1
        except Exception:  # noqa: BLE001 - keep reconciling the rest
            summary["errors"] += 1
            log.exception("AR reconcile failed for %s/%s", data_domain, dataset)

    log.info("AR policy reconcile complete: %s", summary)
    return summary


def _complete_one(
    item: dict[str, Any], *, ddb, table: str, s3, bucket: str, bedrock
) -> str:
    """Advance one ``building`` row: poll, and on a terminal workflow, stamp.

    Returns ``completed`` / ``failed`` / ``in_flight`` / ``reaped``. The
    fingerprint stamped on completion is the row's PENDING hash, carried
    verbatim from gather time — never recomputed here (a wiki that moved during
    the build must yield a policy that is stale on arrival). Completion also
    writes the content-addressed SNAPSHOT — the artifact every later
    deterministic restore reads.
    """
    data_domain = _s(item, "data_domain")
    dataset = _s(item, "dataset")
    workflow_id = _s(item, ATTR_BUILD_WORKFLOW_ID)
    if not workflow_id:
        if _started_recently(_s(item, "ar_build_started_at")):
            return "in_flight"  # the trigger may be between the flip and Start
        stamp_build_failed(
            ddb, table, data_domain=data_domain, dataset=dataset,
            reason=REASON_ABANDONED,
        )
        return "reaped"

    label = dataset_label(data_domain, dataset)
    policy_arn, _hash = ensure_policy(
        bedrock,
        name=policy_name(label),
        description=(
            "Automated Reasoning policy derived from the OKF Data Wiki "
            f"reference docs for {label}."
        ),
    )
    status = get_build_status(bedrock, policy_arn=policy_arn, workflow_id=workflow_id)
    if status in (BUILD_WORKFLOW_FAILED, BUILD_WORKFLOW_CANCELLED):
        stamp_build_failed(
            ddb, table, data_domain=data_domain, dataset=dataset,
            reason=f"build_{status.lower()}",
        )
        return "failed"
    if status != BUILD_WORKFLOW_COMPLETED:
        return "in_flight"

    try:
        status_written = finish_completed_build(
            bedrock, ddb, s3,
            table=table, bucket=bucket,
            data_domain=data_domain, dataset=dataset,
            policy_arn=policy_arn, workflow_id=workflow_id,
            pending_hash=_s(item, ATTR_PENDING_SOURCE_HASH),
            guardrail_id=_s(item, "ar_guardrail_id") or None,
            guardrail_profile=env_guardrail_profile(),
        )
    except ValueError as e:
        # Deterministic verdict on the RESULT (e.g. rule-free): retrying the
        # completion cannot change it, so release the lease.
        stamp_build_failed(
            ddb, table, data_domain=data_domain, dataset=dataset,
            reason=f"complete: {e}",
        )
        return "failed"
    log.info(
        "AR policy build completed for %s/%s (%s)",
        data_domain,
        dataset,
        status_written,
    )
    return "completed"


# -- row plumbing ---------------------------------------------------------------


def _iter_dataset_items(ddb, table: str):
    """Yield every ``DATASET#`` registry row as a raw (typed-attribute) item."""
    paginator = ddb.get_paginator("scan")
    for page in paginator.paginate(
        TableName=table,
        FilterExpression="begins_with(sk, :p)",
        ExpressionAttributeValues={":p": {"S": "DATASET#"}},
    ):
        yield from page.get("Items", [])


def _s(item: dict[str, Any], name: str) -> str:
    return str((item.get(name) or {}).get("S") or "")


def _read_build_state(
    ddb, table: str, *, data_domain: str, dataset: str
) -> tuple[bool, str, str, str, dict[str, Any]]:
    from okf_aws.ar_policy import is_enrolled, registry_key

    item = (
        ddb.get_item(
            TableName=table, Key=registry_key(data_domain, dataset)
        ).get("Item")
        or {}
    )
    return (
        is_enrolled(item),
        _s(item, ATTR_BUILD_STATUS),
        _s(item, ATTR_SOURCE_HASH),
        _s(item, "ar_guardrail_id"),
        item,
    )


def _started_recently(started_at: str) -> bool:
    """True while a workflow-less ``building`` row is inside the start grace period."""
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return False  # no/broken timestamp: nothing will ever stamp it — reap
    return datetime.now(timezone.utc) - started < _ABANDONED_AFTER


def _bedrock_client() -> Any:
    """The AR CONTROL plane (``bedrock``), not ``bedrock-runtime``."""
    import boto3

    return boto3.client(
        "bedrock", region_name=os.environ.get("AWS_REGION", "us-east-1")
    )


def ddb_client() -> Any:
    """A LOW-LEVEL DynamoDB client for the typed registry helpers.

    Never substitute a resource's ``.meta.client``: boto3 attaches the
    resource's document transformations to that client, so the typed
    ``ExpressionAttributeValues`` the AR helpers send silently match nothing.
    """
    import boto3

    return boto3.client(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
    )
