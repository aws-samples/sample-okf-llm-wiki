"""Author the dataset's policy document at harvest finalize (and on dispatch).

The policy document (``policies.yaml``) is a DERIVED artifact of the bundle
(like the vector index), so the moment a bundle changes is the moment to
re-author it — and harvest finalize is the only place that knows a bundle just
changed. Authoring is minutes-scale agent work whose value is advisory, so
this whole step sits AFTER the commit marker and is best-effort in the
strongest sense: it never raises, and every failure path leaves the registry
row in a state a later trigger can retry from.

The v2 (LLM-judge) lifecycle is deliberately short: gather sources →
fingerprint-skip → flip ``building`` (the lease) → run the author agent →
persist the document + author state → stamp ``ready`` with the gather-time
fingerprint. No Bedrock policy, no build workflow, no completion authority —
authoring IS completion. There is no region gate either: judges run wherever
the chat model runs.

Load-bearing properties (unchanged from v1):

* **Byte-identical when off.** With ``OKF_POLICY_BUILD_ENABLED`` unset nothing
  here reads S3, DynamoDB, or the environment beyond that one flag.
* **Enrollment is per-dataset opt-in** (``ar_enrolled`` on the mapping row).
  An unenrolled dataset costs one GetItem per harvest and nothing else.
* **Re-author iff the sources changed** (or the document is missing — the
  self-heal for datasets enrolled before v2). Most harvests cost one GetItem
  plus one S3 walk.
* **The flip to ``building`` is the serialization point** with no stale escape
  hatch: once this function owns the lease, EVERY exit stamps a terminal
  status — a row abandoned at ``building`` can never re-author (the reconcile
  reaps it).
* **The fingerprint is captured at gather time** and stamped verbatim, so a
  wiki that moves while the author runs yields a document that is stale on
  arrival rather than one mislabelled as current.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from harvest.status import build_registry_client
from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    USABLE_BUILD_STATUSES,
    gather_sources,
    hash_sources,
    persist_author_state,
    read_policy_doc,
    read_source_copy,
    read_sources_manifest,
    registry_key,
    stamp_build_failed,
    stamp_ready,
    try_flip_building,
)

log = logging.getLogger(__name__)

#: Statuses that make authoring redundant when the fingerprint also matches
#: AND the document actually exists: a usable document, or one already being
#: authored for this exact source set.
_SKIP_STATUSES: frozenset[str] = USABLE_BUILD_STATUSES | {BUILD_BUILDING}

#: ``stamp_build_failed`` reason when the author returned no document.
#: Terminal rather than silent so the lease is released and the operator can
#: see that the wiki produced nothing checkable.
REASON_NO_RULES = "no_rules"

# Outcomes, returned (never raised) so a caller can log or assert on the exact
# reason authoring did or did not run.
OUTCOME_DISABLED = "disabled"
OUTCOME_UNCONFIGURED = "unconfigured"
OUTCOME_NOT_ENROLLED = "not_enrolled"
OUTCOME_NO_SOURCES = "no_sources"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_LOCKED = "locked"
OUTCOME_AUTHORED = "authored"
OUTCOME_NO_RULES = "no_rules"
OUTCOME_ERROR = "error"


def build_enabled() -> bool:
    """True when this deployment authors policy documents (default: false).

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
    bucket: str = "",
    author: Any = None,
) -> str:
    """Bring the dataset's policy document up to date with the wiki. NEVER raises.

    Returns the ``OUTCOME_*`` describing what happened — ``OUTCOME_AUTHORED``
    when the author ran and the row was stamped ready, one of the no-op
    outcomes otherwise, and ``OUTCOME_ERROR`` when a step failed (loudly
    logged, lease released).

    Every AWS seam is injectable (``registry`` as the ``(dynamodb client,
    table)`` tuple harvest uses everywhere, ``s3``, and ``author`` — the
    document-authoring callable, defaulting to the :mod:`harvest.ar_author`
    agent) so the offline tests never construct a client. Unset seams are
    built from the environment on first use, i.e. only once the flag,
    enrollment and fingerprint checks have all decided work is warranted.
    """
    if not build_enabled():
        return OUTCOME_DISABLED
    try:
        return _author_and_stamp(
            data_domain=data_domain,
            dataset=dataset,
            registry=registry,
            s3=s3,
            bucket=bucket,
            author=author,
        )
    except Exception:  # noqa: BLE001 - authoring must never fail a finished harvest
        log.error(
            "policy authoring failed for %s/%s (bundle already committed; "
            "a Sync or the nightly reconcile retries)",
            data_domain,
            dataset,
            exc_info=True,
        )
        return OUTCOME_ERROR


def _author_and_stamp(
    *,
    data_domain: str,
    dataset: str,
    registry: tuple[Any, str] | None,
    s3: Any,
    bucket: str,
    author: Any,
) -> str:
    """The authoring pipeline proper. Raises only into :func:`maybe_build_policy`."""
    registry = registry or build_registry_client()
    if registry is None:
        log.info(
            "policy authoring skipped for %s/%s: no registry table configured",
            data_domain,
            dataset,
        )
        return OUTCOME_UNCONFIGURED
    ddb, table = registry

    # Enrollment is the user's per-dataset opt-in (the Reasoning page), checked
    # FIRST — before any S3 walk — so a harvest of an unenrolled dataset costs
    # exactly one GetItem and writes nothing.
    enrolled, status, stored_hash = _read_build_state(
        ddb, table, data_domain=data_domain, dataset=dataset
    )
    if not enrolled:
        return OUTCOME_NOT_ENROLLED

    bucket = bucket or os.environ.get("OKF_BUNDLE_BUCKET", "")
    if not bucket:
        log.warning(
            "policy authoring skipped for %s/%s: OKF_BUNDLE_BUCKET is not "
            "configured",
            data_domain,
            dataset,
        )
        return OUTCOME_UNCONFIGURED

    s3 = s3 or _s3_client()
    # Read from S3, not from the mount: the fingerprint must describe what the
    # rebuild authority and the check-time verifier will see.
    sources = gather_sources(s3, bucket, data_domain, dataset)
    fresh_hash = hash_sources(sources)
    if not fresh_hash:
        log.info(
            "no policy sources for %s/%s — nothing to author", data_domain, dataset
        )
        return OUTCOME_NO_SOURCES

    if stored_hash == fresh_hash and status in _SKIP_STATUSES:
        # The document must actually exist for "unchanged" to hold — a ready
        # row without its artifact (a pre-v2 dataset, a lost write) re-authors.
        if status == BUILD_BUILDING or read_policy_doc(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset
        ) is not None:
            log.info(
                "policy document for %s/%s already %s at this source "
                "fingerprint — skipping",
                data_domain,
                dataset,
                status,
            )
            return OUTCOME_UNCHANGED

    if not try_flip_building(
        ddb, table, data_domain=data_domain, dataset=dataset, pending_hash=fresh_hash
    ):
        log.info(
            "policy authoring for %s/%s not started: another run holds the row",
            data_domain,
            dataset,
        )
        return OUTCOME_LOCKED

    # From here the lease is HELD and the flip has no stale escape hatch, so
    # every exit must stamp a terminal status or the dataset can never
    # re-author (the reconcile's reaper is the last-resort backstop).
    try:
        doc_text = _author_doc(
            author, s3=s3, bucket=bucket,
            data_domain=data_domain, dataset=dataset, sources=sources,
        )
        if not doc_text.strip():
            log.error(
                "policy author produced no document for %s/%s — marking failed",
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
        # Document + the exact sources it was authored from: the next run's
        # diff base AND the artifact the judges read.
        persist_author_state(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
            sources=sources, doc_text=doc_text,
        )
    except Exception as e:  # noqa: BLE001 - release the lease, then report
        stamp_build_failed(
            ddb,
            table,
            data_domain=data_domain,
            dataset=dataset,
            reason=f"{type(e).__name__}: {e}",
        )
        raise

    stamp_ready(
        ddb, table, data_domain=data_domain, dataset=dataset, fingerprint=fresh_hash
    )
    log.info(
        "policy document authored for %s/%s (fingerprint %s)",
        data_domain,
        dataset,
        fresh_hash[:12],
    )
    return OUTCOME_AUTHORED


def _author_doc(
    author: Any, *, s3: Any, bucket: str, data_domain: str, dataset: str,
    sources: list[tuple[str, bytes]],
) -> str:
    """Run the document author with the previous run's diff base wired in."""
    from harvest.ar_author import author_policy_doc

    author = author or author_policy_doc
    prior_doc = read_policy_doc(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    ) or ""
    prior_manifest = read_sources_manifest(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )
    return author(
        sources=sources,
        prior_doc=prior_doc,
        prior_manifest=prior_manifest,
        fetch_old=lambda rel: read_source_copy(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
            rel_path=rel,
        ),
    )


def _read_build_state(
    ddb: Any, table: str, *, data_domain: str, dataset: str
) -> tuple[bool, str, str]:
    """``(enrolled, ar_build_status, ar_source_hash)``.

    ``(False, "", "")`` when the row is absent. The enrollment gate and the
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
        ((item.get(ATTR_BUILD_STATUS) or {}).get("S")) or "",
        ((item.get(ATTR_SOURCE_HASH) or {}).get("S")) or "",
    )


def _s3_client() -> Any:
    import boto3

    return boto3.client("s3")
