"""Trigger the dataset's Automated Reasoning policy build at harvest finalize.

The policy is a DERIVED artifact of the bundle (like the vector index), so the
moment a bundle changes is the moment to rebuild it — and harvest finalize is
the only place that knows a bundle just changed. But a build is minutes-scale
Bedrock work whose value is advisory, so this whole step sits AFTER the commit
marker and is best-effort in the strongest sense: it never raises, and every
failure path leaves the registry row in a state a later trigger can retry from.

Five properties are load-bearing and easy to break:

* **Byte-identical when off.** With ``OKF_POLICY_BUILD_ENABLED`` unset nothing
  here reads S3, DynamoDB, Bedrock or the environment beyond that one flag.
* **Enrollment is per-dataset opt-in** (``ar_enrolled`` on the mapping row, set
  from the UI's Reasoning page). An unenrolled dataset costs one GetItem per
  harvest and nothing else — the 100-policy account cap is a budget the user
  spends deliberately, never a race between datasets.
* **Rebuild iff the sources changed.** A re-harvest that never touched a §7.1
  source file must cost zero LLM tokens and zero API calls beyond one GetItem —
  most harvests are exactly that.
* **The flip to ``building`` is the serialization point**, and it has no stale
  escape hatch (see ``okf_aws.ar_policy.try_flip_building``). So once this
  function owns the lease, EVERY exit path must either start a build or stamp a
  terminal status — a row abandoned at ``building`` can never be rebuilt.
* **The fingerprint is captured at gather time** and parked on the row verbatim,
  so a wiki that moves while the build runs yields a policy that is stale on
  arrival rather than one mislabelled as current.

After Start, the session POLLS the workflow to terminal and completes it
in-place (apply the staged definition → version → guardrail → snapshot →
stamp, via the shared ``finish_completed_build``): the session that started
the build is alive anyway (a build is minutes; AgentCore allows 8h), so
completion needs no schedule. The poll window is bounded
(``OKF_POLICY_BUILD_WAIT_SECONDS``; ``0`` restores fire-and-forget), and every
overrun or completion hiccup leaves the row ``building`` WITH the workflow id
stamped — the Reasoning page's Sync and the nightly reconcile run the same
completion, so nothing is ever lost, only delayed.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from harvest.status import build_registry_client
from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    BUILD_WORKFLOW_COMPLETED,
    env_guardrail_profile,
    REASON_UNSUPPORTED_REGION,
    TERMINAL_WORKFLOW_STATUSES,
    USABLE_BUILD_STATUSES,
    PolicyCapError,
    dataset_label,
    ensure_policy,
    finish_completed_build,
    gather_sources,
    get_build_status,
    hash_sources,
    persist_author_state,
    policy_name,
    read_ar_rules,
    read_snapshot,
    read_source_copy,
    read_sources_manifest,
    region_supported,
    registry_key,
    restore_snapshot,
    stamp_build_failed,
    stamp_build_started,
    start_build,
    try_flip_building,
)

log = logging.getLogger(__name__)

#: Statuses that make a build redundant when the fingerprint also matches:
#: a usable policy, or one already being built for this exact source set.
_SKIP_STATUSES: frozenset[str] = USABLE_BUILD_STATUSES | {BUILD_BUILDING}

#: ``stamp_build_failed`` reason for the account's 100-policy cap. Not a
#: transient failure — a nightly retry will hit the same wall.
REASON_POLICY_CAP = "policy_cap"

#: ``stamp_build_failed`` reason when the preprocess pass returned no rules.
#: Terminal rather than silent so the lease is released and the operator can see
#: that the wiki produced nothing checkable.
REASON_NO_RULES = "no_rules"

# Outcomes, returned (never raised) so a caller can log or assert on the exact
# reason a build did or did not start.
OUTCOME_DISABLED = "disabled"
OUTCOME_UNCONFIGURED = "unconfigured"
OUTCOME_NOT_ENROLLED = "not_enrolled"
OUTCOME_UNSUPPORTED_REGION = "unsupported_region"
OUTCOME_NO_SOURCES = "no_sources"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_LOCKED = "locked"
OUTCOME_STARTED = "started"
OUTCOME_COMPLETED = "completed"
OUTCOME_RESTORED = "restored"
OUTCOME_POLICY_CAP = "policy_cap"
OUTCOME_NO_RULES = "no_rules"
OUTCOME_ERROR = "error"


def build_enabled() -> bool:
    """True when this deployment builds AR policies (default: false).

    Read at call time (not at import) so a test's ``monkeypatch.setenv`` takes
    effect, matching every other harvest env read.
    """
    return os.environ.get("OKF_POLICY_BUILD_ENABLED", "").lower() in (
        "true",
        "1",
        "yes",
    )


def maybe_build_policy(
    *,
    data_domain: str,
    dataset: str,
    registry: tuple[Any, str] | None = None,
    s3: Any = None,
    bedrock: Any = None,
    bucket: str = "",
    author: Any = None,
) -> str:
    """Bring the dataset's AR policy up to date with the wiki. NEVER raises.

    Returns the ``OUTCOME_*`` describing what happened — ``OUTCOME_RESTORED``
    when a content-addressed snapshot covered the current fingerprint (a
    deterministic, model-free restore), ``OUTCOME_STARTED`` when the authoring
    agent ran and a Bedrock build was started, one of the no-op outcomes
    otherwise, and ``OUTCOME_ERROR`` when a step failed (loudly logged, lease
    released).

    Every AWS seam is injectable (``registry`` as the ``(dynamodb client,
    table)`` tuple harvest uses everywhere, ``s3``, ``bedrock``, and ``author``
    — the rules-authoring callable, defaulting to the
    :mod:`harvest.ar_author` agent) so the offline tests never construct a
    client. Unset seams are built from the environment on first use, i.e. only
    once the flag, enrollment, region and fingerprint checks have all decided
    work is warranted.
    """
    if not build_enabled():
        return OUTCOME_DISABLED
    try:
        return _trigger_build(
            data_domain=data_domain,
            dataset=dataset,
            registry=registry,
            s3=s3,
            bedrock=bedrock,
            bucket=bucket,
            author=author,
        )
    except Exception:  # noqa: BLE001 - a build must never fail a finished harvest
        log.error(
            "AR policy build trigger failed for %s/%s (bundle already committed; "
            "the nightly reconcile will retry)",
            data_domain,
            dataset,
            exc_info=True,
        )
        return OUTCOME_ERROR


def _trigger_build(
    *,
    data_domain: str,
    dataset: str,
    registry: tuple[Any, str] | None,
    s3: Any,
    bedrock: Any,
    bucket: str,
    author: Any,
) -> str:
    """The build pipeline proper. Raises only into :func:`maybe_build_policy`."""
    registry = registry or build_registry_client()
    if registry is None:
        log.info(
            "AR policy build skipped for %s/%s: no registry table configured",
            data_domain,
            dataset,
        )
        return OUTCOME_UNCONFIGURED
    ddb, table = registry

    # Enrollment is the user's per-dataset opt-in (the Reasoning page), checked
    # FIRST — before the region stamp and before any S3 walk — so a harvest of
    # an unenrolled dataset costs exactly one GetItem and writes nothing.
    enrolled, status, stored_hash, guardrail_id = _read_build_state(
        ddb, table, data_domain=data_domain, dataset=dataset
    )
    if not enrolled:
        return OUTCOME_NOT_ENROLLED

    region = os.environ.get("AWS_REGION", "us-east-1")
    if not region_supported(region):
        return _stamp_unsupported_region(
            ddb, table, data_domain=data_domain, dataset=dataset, region=region,
            status=status,
        )

    bucket = bucket or os.environ.get("OKF_BUNDLE_BUCKET", "")
    if not bucket:
        log.warning(
            "AR policy build skipped for %s/%s: OKF_BUNDLE_BUCKET is not configured",
            data_domain,
            dataset,
        )
        return OUTCOME_UNCONFIGURED

    s3 = s3 or _s3_client(region)
    # Read from S3, not from the mount: the fingerprint must describe what the
    # rebuild authority and the check-time verifier will see.
    sources = gather_sources(s3, bucket, data_domain, dataset)
    fresh_hash = hash_sources(sources)
    if not fresh_hash:
        log.info(
            "No AR policy sources for %s/%s — nothing to build", data_domain, dataset
        )
        return OUTCOME_NO_SOURCES

    if stored_hash == fresh_hash and status in _SKIP_STATUSES:
        log.info(
            "AR policy for %s/%s already %s at this source fingerprint — skipping",
            data_domain,
            dataset,
            status,
        )
        return OUTCOME_UNCHANGED

    if not try_flip_building(
        ddb, table, data_domain=data_domain, dataset=dataset, pending_hash=fresh_hash
    ):
        log.info(
            "AR policy build for %s/%s not started: another build holds the row",
            data_domain,
            dataset,
        )
        return OUTCOME_LOCKED

    # From here the lease is HELD and the flip has no stale escape hatch, so
    # every exit must stamp a terminal status or the dataset can never rebuild.

    # Content-addressed fast path: this exact source state was built before
    # (a repromote, or any A→B→A edit cycle) — push its EXACT solver rules
    # back. Deterministic, model-free, seconds; fidelity restores verbatim
    # and is deliberately not re-measured (same inputs, same policy).
    snapshot = read_snapshot(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        fingerprint=fresh_hash,
    )
    if snapshot is not None:
        try:
            bedrock = bedrock or _bedrock_client(region)
            restore_snapshot(
                bedrock, ddb, s3,
                table=table, bucket=bucket,
                data_domain=data_domain, dataset=dataset,
                snapshot=snapshot,
                guardrail_profile=env_guardrail_profile(),
                guardrail_id=guardrail_id or None,
            )
            # The restored doc becomes the next authoring run's diff base.
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

    try:
        rules_text = _author_rules(
            author, s3=s3, bucket=bucket,
            data_domain=data_domain, dataset=dataset, sources=sources,
        )
        if not rules_text.strip():
            log.error(
                "AR author produced no rules for %s/%s — marking the build failed",
                data_domain,
                dataset,
            )
            stamp_build_failed(
                ddb,
                table,
                data_domain=data_domain,
                dataset=dataset,
                reason=REASON_NO_RULES,
            )
            return OUTCOME_NO_RULES
        # Rules + the exact sources they were authored from: the next run's
        # diff base, written even if the Bedrock build below fails.
        persist_author_state(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
            sources=sources, rules_text=rules_text,
        )
        label = dataset_label(data_domain, dataset)
        bedrock = bedrock or _bedrock_client(region)
        policy_arn, _definition_hash = ensure_policy(
            bedrock,
            name=policy_name(label),
            description=(
                "Automated Reasoning policy derived from the OKF Data Wiki "
                f"reference docs for {label}."
            ),
        )
        workflow_id = start_build(bedrock, policy_arn=policy_arn, rules_text=rules_text)
    except PolicyCapError:
        # The account's 100-policy cap: not transient, so say so loudly rather
        # than letting a nightly retry rediscover it every night in silence.
        log.error(
            "AR policy build refused for %s/%s: the account's policy quota is "
            "reached. Delete unused policies to re-enable builds.",
            data_domain,
            dataset,
            exc_info=True,
        )
        stamp_build_failed(
            ddb,
            table,
            data_domain=data_domain,
            dataset=dataset,
            reason=REASON_POLICY_CAP,
        )
        return OUTCOME_POLICY_CAP
    except Exception as e:  # noqa: BLE001 - release the lease, then report
        stamp_build_failed(
            ddb,
            table,
            data_domain=data_domain,
            dataset=dataset,
            reason=f"{type(e).__name__}: {e}",
        )
        raise

    stamp_build_started(
        ddb, table, data_domain=data_domain, dataset=dataset, workflow_id=workflow_id
    )
    log.info(
        "AR policy build started for %s/%s: workflow=%s policy=%s",
        data_domain,
        dataset,
        workflow_id,
        policy_arn,
    )
    return _await_and_finish(
        ddb, table, s3=s3, bedrock=bedrock, bucket=bucket,
        data_domain=data_domain, dataset=dataset,
        policy_arn=policy_arn, workflow_id=workflow_id,
        pending_hash=fresh_hash, guardrail_id=guardrail_id,
    )


def _await_and_finish(
    ddb: Any,
    table: str,
    *,
    s3: Any,
    bedrock: Any,
    bucket: str,
    data_domain: str,
    dataset: str,
    policy_arn: str,
    workflow_id: str,
    pending_hash: str,
    guardrail_id: str,
) -> str:
    """Poll the started workflow to terminal, then complete it in-session.

    The session that started the build is alive anyway, so finishing here
    makes completion automatic — no reconcile schedule required. Failure
    posture: a poll overrun or a transient completion error leaves the row
    ``building`` with the workflow id stamped (the manual Sync and the
    nightly reconcile run the SAME ``finish_completed_build``, so the durable
    workflow result gets stamped later — retrying the completion, never the
    build); only a terminally FAILED/CANCELLED workflow, or a result that is
    deterministically unusable (rule-free), releases the lease as ``failed``.
    """
    wait = _env_int("OKF_POLICY_BUILD_WAIT_SECONDS", 1800)
    if wait <= 0:
        return OUTCOME_STARTED  # fire-and-forget: the backstops complete it
    poll = max(1, _env_int("OKF_POLICY_BUILD_POLL_SECONDS", 20))
    deadline = time.monotonic() + wait
    while True:
        status = get_build_status(
            bedrock, policy_arn=policy_arn, workflow_id=workflow_id
        )
        if status in TERMINAL_WORKFLOW_STATUSES:
            break
        if time.monotonic() >= deadline:
            log.info(
                "AR build for %s/%s still %s after %ss — completion falls to "
                "the Sync/reconcile backstops",
                data_domain, dataset, status, wait,
            )
            return OUTCOME_STARTED
        time.sleep(poll)
    if status != BUILD_WORKFLOW_COMPLETED:
        stamp_build_failed(
            ddb, table, data_domain=data_domain, dataset=dataset,
            reason=f"build_{status.lower()}",
        )
        return OUTCOME_ERROR
    try:
        finish_completed_build(
            bedrock, ddb, s3,
            table=table, bucket=bucket,
            data_domain=data_domain, dataset=dataset,
            policy_arn=policy_arn, workflow_id=workflow_id,
            pending_hash=pending_hash, guardrail_id=guardrail_id or None,
            guardrail_profile=env_guardrail_profile(),
        )
    except ValueError as e:
        # Deterministic verdict on the RESULT (e.g. it contains no rules):
        # retrying the completion cannot change it, so release the lease.
        stamp_build_failed(
            ddb, table, data_domain=data_domain, dataset=dataset,
            reason=f"complete: {e}",
        )
        return OUTCOME_ERROR
    except Exception:  # noqa: BLE001 - transient: leave `building`, backstops retry
        log.error(
            "AR build completion failed for %s/%s (row left `building`; Sync or "
            "the nightly reconcile retries the completion, not the build)",
            data_domain, dataset, exc_info=True,
        )
        return OUTCOME_ERROR
    return OUTCOME_COMPLETED


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _stamp_unsupported_region(
    ddb: Any, table: str, *, data_domain: str, dataset: str, region: str, status: str
) -> str:
    """Record that AR does not exist here — once, not on every harvest.

    The status is a deployment fact, so re-stamping it on every finalize would
    be a pure write per harvest with nothing to say. ``status`` is the row's
    current value (already read by the caller's enrollment gate).
    """
    if status != REASON_UNSUPPORTED_REGION:
        stamp_build_failed(
            ddb,
            table,
            data_domain=data_domain,
            dataset=dataset,
            reason=REASON_UNSUPPORTED_REGION,
        )
    log.info(
        "AR policy build skipped for %s/%s: Automated Reasoning checks are not "
        "offered in %s",
        data_domain,
        dataset,
        region,
    )
    return OUTCOME_UNSUPPORTED_REGION


def _read_build_state(
    ddb: Any, table: str, *, data_domain: str, dataset: str
) -> tuple[bool, str, str, str]:
    """``(enrolled, ar_build_status, ar_source_hash, ar_guardrail_id)``.

    ``(False, "", "", "")`` when the row is absent. The enrollment gate and the
    iff-changed skip are the whole reason harvest reads this row (its other
    registry writes are blind UpdateItems), so the runtime role needs GetItem
    on the registry table as well as UpdateItem.
    """
    from okf_aws.ar_policy import is_enrolled

    item = (
        ddb.get_item(TableName=table, Key=registry_key(data_domain, dataset)).get("Item")
        or {}
    )
    return (
        is_enrolled(item),
        str((item.get(ATTR_BUILD_STATUS) or {}).get("S") or ""),
        str((item.get(ATTR_SOURCE_HASH) or {}).get("S") or ""),
        str((item.get("ar_guardrail_id") or {}).get("S") or ""),
    )


def _author_rules(
    author: Any, *, s3: Any, bucket: str, data_domain: str, dataset: str,
    sources: list[tuple[str, bytes]],
) -> str:
    """Run the rules author (the :mod:`harvest.ar_author` agent by default).

    The previous authoring run's document + source copies become its update
    context: a prior document means a diff-driven surgical edit, keeping the
    rules consistent with the wiki without a from-scratch rewrite.
    """
    if author is None:
        from harvest.ar_author import author_rules as author  # noqa: PLR1704

    prior_rules = read_ar_rules(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    ) or ""
    prior_manifest = read_sources_manifest(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )
    return author(
        sources=sources,
        prior_rules=prior_rules,
        prior_manifest=prior_manifest,
        fetch_old=lambda rel: read_source_copy(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset, rel_path=rel
        ),
    )


def _s3_client(region: str) -> Any:
    import boto3

    return boto3.client("s3", region_name=region)


def _bedrock_client(region: str) -> Any:
    """The AR CONTROL plane (``bedrock``), not ``bedrock-runtime``."""
    import boto3

    return boto3.client("bedrock", region_name=region)
