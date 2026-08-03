"""Policy rebuild authority: fingerprint-verify, dispatch authoring, reap stalls.

The harvest finalize hook can only act while a harvest is running — this module
is the other half of the policy-document lifecycle, reached by
``policy_rebuild`` events (EventBridge -> the same SQS queue as the Glue
events; published by the Control API's repromote/sync paths and by the
chat runtime's stale discovery) and by the nightly reconcile.

The Lambda itself makes only DETERMINISTIC decisions; the authoring agent
(minutes of reasoning work with LangChain deps this Lambda doesn't carry) runs
on the harvest runtime:

* **Dispatch** — a changed/missing/stale document means one
  ``mode="ar_rules"`` invocation of the harvest runtime with a FRESH session
  id. No local ``building`` flip: the runtime's own conditional flip is the
  dedup point, so N duplicate events collapse to one authoring run.
* **Reap** — a row stuck at ``building`` past the grace period (the runtime
  died mid-authoring) is stamped ``failed`` so the fingerprint check can
  re-dispatch it; a young ``building`` row is left alone.

Everything is gated on ``OKF_POLICY_BUILD_ENABLED`` (default off); there is no
per-dataset opt-in — the policy document is an always-on derived artifact.
What keeps that from turning into a deploy-time fleet-wide backfill is the
TRIGGER set, not a flag: events only arrive for datasets someone acted on (a
manual Sync, a repromote, the chat check's stale discovery), and the nightly
reconcile re-verifies only datasets whose policy lifecycle has BEGUN
(``ar_build_status`` present) — a pre-existing dataset authors its first
document on its first Sync/harvest/repromote, never spontaneously. Per-dataset
try/except so one broken dataset never blocks the rest.
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
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    USABLE_BUILD_STATUSES,
    gather_sources,
    hash_sources,
    lifecycle_begun,
    read_policy_doc,
    read_sources_manifest,
    registry_key,
    stamp_build_failed,
    stamp_ready,
)
from okf_aws.s3_bundle import is_bundle_ready

log = logging.getLogger("incremental.ar_rebuild")

#: A row flipped to ``building`` whose authoring run died before any terminal
#: stamp. After this grace period the reaper moves it to ``failed`` so the
#: fingerprint check can re-dispatch it.
REASON_ABANDONED = "abandoned_build"
_ABANDONED_AFTER = timedelta(hours=1)

# run_rebuild outcomes — grep-compatible with the harvest trigger's vocabulary.
OUTCOME_DISABLED = "disabled"
OUTCOME_UNREGISTERED = "unregistered"
OUTCOME_NO_WIKI = "no_wiki"
OUTCOME_NO_SOURCES = "no_sources"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_RECOVERED = "recovered"  # artifact was current; only the stamp was lost
OUTCOME_IN_FLIGHT = "in_flight"  # a young authoring run holds the row
OUTCOME_INVOKED = "invoked"  # an authoring run was dispatched to the runtime
OUTCOME_ERROR = "error"


def rebuild_enabled() -> bool:
    """True when this deployment authors policy documents (default: false)."""
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
    agentcore: Any = None,
    harvest_runtime_arn: str = "",
) -> str:
    """Bring one dataset's policy document up to date. NEVER raises.

    ``ddb`` is the LOW-LEVEL DynamoDB client (the registry stamp helpers speak
    typed attributes).
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
            agentcore=agentcore,
            harvest_runtime_arn=harvest_runtime_arn,
        )
    except Exception:  # noqa: BLE001 - a rebuild is advisory, never a Lambda error
        log.error(
            "policy rebuild failed for %s/%s (the nightly reconcile will retry)",
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
    agentcore: Any,
    harvest_runtime_arn: str,
) -> str:
    # Registration first: a rebuild event for a dataset with no mapping row —
    # a late event, a duplicate that raced a dataset delete — must cost one
    # GetItem and change nothing.
    item = _read_row(ddb, table, data_domain=data_domain, dataset=dataset)
    if not item:
        return OUTCOME_UNREGISTERED
    status = _s(item, ATTR_BUILD_STATUS)
    stored_hash = _s(item, ATTR_SOURCE_HASH)

    # An in-flight authoring run holds the row; a stalled one is reaped so the
    # decision below can re-dispatch (the manual Sync's unstick path).
    if status == BUILD_BUILDING:
        if _started_recently(_s(item, "ar_build_started_at")):
            return OUTCOME_IN_FLIGHT
        stamp_build_failed(
            ddb, table, data_domain=data_domain, dataset=dataset,
            reason=REASON_ABANDONED,
        )
        status = ""

    sources = gather_sources(s3, bucket, data_domain, dataset)
    fresh_hash = hash_sources(sources)
    if not fresh_hash:
        return OUTCOME_NO_SOURCES

    doc = read_policy_doc(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )
    doc_ok = _doc_parses(doc)
    if stored_hash == fresh_hash and status in USABLE_BUILD_STATUSES:
        # The artifact must actually exist AND parse for "unchanged" to hold —
        # a ready row without its document (a pre-v2 dataset, a lost write)
        # OR with a schema-invalid one (a pre-v3 document missing `type`
        # fields) re-authors. This is the whole v3 migration path: same
        # sources, but the FORMAT moved on, so fingerprint equality alone
        # must not veto the re-author.
        if doc_ok:
            return OUTCOME_UNCHANGED

    # Deterministic self-heal: the document on disk was authored from exactly
    # the live sources (the manifest fingerprint proves it) but the row's
    # status is unusable — a reaped stall, a stamp that never landed.
    # Re-stamping is free; an authoring run would only reproduce the artifact.
    # Never recovers an unparseable document: re-stamping it ready would just
    # bounce the next check off the parse gate again.
    if doc_ok and read_sources_manifest(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    ).get("fingerprint") == fresh_hash:
        stamp_ready(
            ddb, table, data_domain=data_domain, dataset=dataset,
            fingerprint=fresh_hash,
        )
        log.info("policy document recovered for %s/%s", data_domain, dataset)
        return OUTCOME_RECOVERED

    # Only bundles a full harvest has committed get policies. This is also
    # what a first-authoring event (a manual Sync on a never-authored
    # dataset, a repromote) rides through — no lifecycle gate here — while an
    # event landing mid-(re)harvest defers to the finalize hook, which
    # authors at commit anyway.
    if not is_bundle_ready(s3, bucket, data_domain, dataset):
        return OUTCOME_NO_WIKI

    return _dispatch_authoring(
        data_domain, dataset,
        agentcore=agentcore, harvest_runtime_arn=harvest_runtime_arn,
    )


def _doc_parses(doc: str | None) -> bool:
    """Whether a stored ``policies.yaml`` is usable under the CURRENT schema.

    The format is the contract, not the bytes: a document that no longer
    parses (e.g. pre-v3, before the required ``type`` field) must be treated
    exactly like a missing one, or the fingerprint-equality skip would pin a
    dataset to an obsolete document forever.
    """
    if doc is None:
        return False
    from okf_core import policy_doc as pdoc

    try:
        pdoc.parse_policies(doc)
        return True
    except pdoc.PolicyDocError:
        return False


def _dispatch_authoring(
    data_domain: str,
    dataset: str,
    *,
    agentcore: Any,
    harvest_runtime_arn: str,
) -> str:
    """Fire one ``mode="ar_rules"`` authoring run at the harvest runtime."""
    if agentcore is None or not harvest_runtime_arn:
        log.warning(
            "policy rebuild for %s/%s needs the authoring agent but no harvest "
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
    log.info("policy authoring dispatched for %s/%s", data_domain, dataset)
    return OUTCOME_INVOKED


def reconcile_policies(
    *,
    ddb,
    table: str,
    s3,
    bucket: str,
    agentcore: Any = None,
    harvest_runtime_arn: str = "",
) -> dict[str, Any]:
    """The nightly pass: reap stalls, then hash-verify every policy-bearing dataset.

    Walks every ``DATASET#`` registry row whose policy lifecycle has BEGUN
    (``ar_build_status`` present; ALL sources, not just Glue — a
    Redshift-backed dataset gets a policy too). Never-authored datasets are
    deliberately not backfilled — their first document comes from a manual
    Sync, the next harvest/increment's finalize hook, or a repromote; this
    sweep only keeps already-authored documents honest. Per-dataset errors
    are counted and logged, never propagated: one broken dataset must not
    block the rest. Returns ``{datasets, rebuilt, recovered, in_flight,
    reaped, skipped, errors}``.
    """
    summary = {
        "datasets": 0,
        "rebuilt": 0,
        "recovered": 0,
        "in_flight": 0,
        "reaped": 0,
        "skipped": 0,
        "errors": 0,
    }
    for item in _iter_dataset_items(ddb, table):
        data_domain = _s(item, "data_domain")
        dataset = _s(item, "dataset")
        if not data_domain or not dataset:
            continue
        if not lifecycle_begun(item):
            continue  # never authored: no silent backfill
        summary["datasets"] += 1
        try:
            status = _s(item, ATTR_BUILD_STATUS)
            if status == BUILD_BUILDING:
                if _started_recently(_s(item, "ar_build_started_at")):
                    summary["in_flight"] += 1
                    continue
                stamp_build_failed(
                    ddb, table, data_domain=data_domain, dataset=dataset,
                    reason=REASON_ABANDONED,
                )
                summary["reaped"] += 1
                status = ""

            fresh = hash_sources(gather_sources(s3, bucket, data_domain, dataset))
            if not fresh:
                summary["skipped"] += 1
                continue
            stored = _s(item, ATTR_SOURCE_HASH)
            doc = read_policy_doc(
                s3, bucket=bucket, data_domain=data_domain, dataset=dataset
            )
            # Parse gate mirrors run_rebuild: a schema-invalid document (a
            # pre-v3 one without `type` fields) counts as missing, so the
            # nightly sweep migrates every policy-bearing dataset to the
            # current format without anyone clicking anything.
            doc_ok = _doc_parses(doc)
            if (
                status in USABLE_BUILD_STATUSES
                and stored == fresh
                and doc_ok
            ):
                summary["skipped"] += 1
                continue
            # Deterministic self-heal (see run_rebuild): current artifact,
            # lost stamp — re-stamp instead of re-authoring.
            if doc_ok and read_sources_manifest(
                s3, bucket=bucket, data_domain=data_domain, dataset=dataset
            ).get("fingerprint") == fresh:
                stamp_ready(
                    ddb, table, data_domain=data_domain, dataset=dataset,
                    fingerprint=fresh,
                )
                summary["recovered"] += 1
                continue
            # Only bundles a full harvest has committed get policies — never
            # race a (re)harvest mid-write; its finalize hook re-authors.
            if not is_bundle_ready(s3, bucket, data_domain, dataset):
                summary["skipped"] += 1
                continue
            outcome = _dispatch_authoring(
                data_domain, dataset,
                agentcore=agentcore, harvest_runtime_arn=harvest_runtime_arn,
            )
            if outcome == OUTCOME_INVOKED:
                summary["rebuilt"] += 1
            else:
                summary["errors"] += 1
        except Exception:  # noqa: BLE001 - keep reconciling the rest
            summary["errors"] += 1
            log.exception(
                "policy reconcile failed for %s/%s", data_domain, dataset
            )

    log.info("policy reconcile complete: %s", summary)
    return summary


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


def _read_row(
    ddb, table: str, *, data_domain: str, dataset: str
) -> dict[str, Any]:
    return (
        ddb.get_item(
            TableName=table, Key=registry_key(data_domain, dataset)
        ).get("Item")
        or {}
    )


def _started_recently(started_at: str) -> bool:
    """True while a ``building`` row is inside the authoring grace period."""
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return False  # no/broken timestamp: nothing will ever stamp it — reap
    return datetime.now(timezone.utc) - started < _ABANDONED_AFTER


def ddb_client() -> Any:
    """A LOW-LEVEL DynamoDB client for the typed registry helpers.

    Never substitute a resource's ``.meta.client``: boto3 attaches the
    resource's document transformations to that client, so the typed
    ``ExpressionAttributeValues`` the policy helpers send silently match
    nothing.
    """
    import boto3

    return boto3.client(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
    )
