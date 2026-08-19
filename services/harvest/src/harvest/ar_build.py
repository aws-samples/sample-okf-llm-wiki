"""Author the dataset's policy document after a harvest (and on dispatch).

The policy document (``policies.yaml``) is a DERIVED artifact of the bundle
(like the vector index), so the moment a bundle changes is the moment to
re-author it — the RUNNER calls this as a follow-on step right after the
terminal status write (the harvest itself is DONE at the commit marker; see
CONVENTIONS.md "Harvest status"). Authoring is minutes-scale agent work whose
value is advisory, so it is best-effort in the strongest sense: it never
raises, and every failure path leaves the registry row in a state a later
trigger can retry from.

The v2 (LLM-judge) lifecycle is deliberately short: gather sources →
fingerprint-skip → flip ``building`` (the lease) → run the author agent →
persist the document + author state → stamp ``ready`` with the gather-time
fingerprint. No Bedrock policy, no build workflow, no completion authority —
authoring IS completion. There is no region gate either: judges run wherever
the chat model runs.

Load-bearing properties (unchanged from v1):

* **Byte-identical when off.** With ``OKF_POLICY_BUILD_ENABLED`` unset nothing
  here reads S3, DynamoDB, or the environment beyond that one flag.
* **Always on per dataset** (the ``ar_enrolled`` opt-in is retired): every
  committed harvest — full, incremental, annotation — brings the policy
  document along with it. This follow-on build is exactly why a pre-existing
  dataset needs no backfill sweep: its first wiki change authors the first
  document.
* **Re-author iff the sources changed** (or the document is missing — a
  dataset's first harvest under this feature, or one predating v2). An
  unchanged harvest costs one GetItem plus one S3 walk.
* **The flip to ``building`` is the serialization point**: once this function
  owns the lease, EVERY exit stamps a terminal status. A row abandoned at
  ``building`` (a dead author) stops deferring work after
  ``BUILD_LOCK_STALE_SECONDS`` — readers treat it as free and the flip itself
  takes it over (symmetric; see ``okf_aws.ar_policy``) — and the reconcile
  reaps it.
* **The fingerprint is captured at gather time** and stamped verbatim, so a
  wiki that moves while the author runs yields a document that is stale on
  arrival rather than one mislabelled as current.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from harvest.status import build_registry_client
from okf_aws.ar_policy import (
    ATTR_BUILD_STATUS,
    ATTR_SOURCE_HASH,
    BUILD_BUILDING,
    USABLE_BUILD_STATUSES,
    flag_stale,
    gather_sources,
    hash_sources,
    persist_author_state,
    put_rules_schema,
    read_policy_doc,
    read_source_copy,
    read_sources_manifest,
    registry_key,
    stamp_build_failed,
    stamp_ready,
    try_flip_building,
)
from okf_aws.s3_bundle import bundle_prefix
from okf_core.lint import parse_columns_tsv

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
OUTCOME_UNREGISTERED = "unregistered"
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


def publish_rebuild_event(
    data_domain: str, dataset: str, *, reason: str, events: Any = None
) -> None:
    """Best-effort ``policy_rebuild`` publish. Never raises.

    The runner's LOCKED fallback: a follow-on build that loses the flip race
    (typically to the PREVIOUS harvest's still-running author) must leave a
    trigger behind, or the newer wiki's re-author silently waits for the
    nightly reconcile. The rebuild authority collapses duplicates via the
    same flip, so an extra event is always safe.
    """
    try:
        from okf_core import policy_rebuild

        if events is None:
            import boto3

            events = boto3.client(
                "events", region_name=os.environ.get("AWS_REGION", "us-east-1")
            )
        resp = events.put_events(
            Entries=[
                {
                    "Source": policy_rebuild.EVENT_SOURCE,
                    "DetailType": policy_rebuild.DETAIL_TYPE_POLICY_REBUILD,
                    "Detail": json.dumps(
                        policy_rebuild.build_detail(
                            data_domain, dataset, reason=reason
                        )
                    ),
                }
            ]
        )
        # put_events reports per-entry rejections IN-BAND (no exception) —
        # without this check a rejected event reads as published and the
        # re-author silently waits for the nightly reconcile.
        if resp.get("FailedEntryCount"):
            entry = (resp.get("Entries") or [{}])[0]
            log.warning(
                "policy_rebuild publish rejected for %s/%s: %s %s (non-fatal)",
                data_domain,
                dataset,
                entry.get("ErrorCode", ""),
                entry.get("ErrorMessage", ""),
            )
    except Exception:  # noqa: BLE001 - the nightly reconcile is the safety net
        log.warning("policy_rebuild publish failed (non-fatal)", exc_info=True)


def maybe_build_policy(
    *,
    data_domain: str,
    dataset: str,
    registry: tuple[Any, str] | None = None,
    s3: Any = None,
    bucket: str = "",
    author: Any = None,
    force: bool = False,
) -> str:
    """Bring the dataset's policy document up to date with the wiki. NEVER raises.

    Returns the ``OUTCOME_*`` describing what happened — ``OUTCOME_AUTHORED``
    when the author ran and the row was stamped ready, one of the no-op
    outcomes otherwise, and ``OUTCOME_ERROR`` when a step failed (loudly
    logged, lease released).

    ``force`` (a manual Sync's dispatch) re-authors even at an unchanged
    source fingerprint — the sources may be identical while the authoring
    itself moved on (model/effort/prompt). The building lease still wins.

    Every AWS seam is injectable (``registry`` as the ``(dynamodb client,
    table)`` tuple harvest uses everywhere, ``s3``, and ``author`` — the
    document-authoring callable, defaulting to the :mod:`harvest.ar_author`
    agent) so the offline tests never construct a client. Unset seams are
    built from the environment on first use, i.e. only once the flag,
    registration and fingerprint checks have all decided work is warranted.
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
            force=force,
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
    force: bool = False,
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

    # Registration first — before any S3 walk — so authoring can never
    # resurrect state for a dataset whose mapping row is gone (a delete that
    # raced the follow-on build costs exactly one GetItem and writes nothing).
    registered, status, stored_hash, glue_database = _read_build_state(
        ddb, table, data_domain=data_domain, dataset=dataset
    )
    if not registered:
        return OUTCOME_UNREGISTERED

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

    if not force and stored_hash == fresh_hash and status in _SKIP_STATUSES:
        # The document must exist AND PARSE for "unchanged" to hold — a ready
        # row without its artifact (a pre-v2 dataset, a lost write) or with a
        # schema-invalid one (a pre-v3, type-less document) re-authors. The
        # dispatchers that route re-author runs here (the chat check gate,
        # the rebuild authority) both gate on parseability, so an
        # existence-only skip would ping-pong an invalid document between
        # "rebuild!" and "unchanged" forever — the documented migration
        # ("a pre-split document fails the parse … and re-authors") ends
        # HERE, and only a parse check closes the loop.
        if status == BUILD_BUILDING or _stored_doc_parses(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset
        ):
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

    # From here the lease is HELD, so every exit must stamp a terminal status
    # or the dataset stays `building` — which now also 409s harvest starts
    # and repromotes until the staleness escape / the reconcile's reaper. The
    # ready stamp lives INSIDE this try for exactly that reason: a throttled
    # stamp_ready after a successful authoring must still release the lease
    # (as `failed`, which the rebuild authority retries), never wedge the row.
    try:
        # Snapshot the qualification schema for `rules:` blocks from the
        # harvest's own `.metadata/columns.tsv` — the schema the wiki (and so
        # the policies) describe, deliberately NOT live Glue. Persisted next
        # to policies.yaml whenever available; its absence just means the
        # author gate refuses rules blocks (policies stay prose-only).
        rules_schema = _build_rules_schema(
            s3, bucket, data_domain, dataset, glue_database
        )
        if rules_schema is not None:
            put_rules_schema(
                s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
                databases=rules_schema,
            )
        doc_text = _author_doc(
            author, s3=s3, bucket=bucket,
            data_domain=data_domain, dataset=dataset, sources=sources,
            force=force, rules_schema=rules_schema,
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
        stamp_ready(
            ddb, table, data_domain=data_domain, dataset=dataset,
            fingerprint=fresh_hash,
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

    log.info(
        "policy document authored for %s/%s (fingerprint %s)",
        data_domain,
        dataset,
        fresh_hash[:12],
    )
    # Stale-on-arrival self-heal, INLINE: the fingerprint was captured at
    # gather time, so a wiki that moved while the author ran (a fast second
    # harvest is the common cause — its own follow-on build lost the flip to
    # THIS run, and a rebuild event delivered mid-build is dropped as
    # in-flight by the rebuild authority) leaves the fresh document already
    # outdated. Re-check here and leave a durable trigger, instead of the
    # dataset waiting for the chat gate or the nightly reconcile to notice.
    try:
        moved = hash_sources(gather_sources(s3, bucket, data_domain, dataset))
        if moved and moved != fresh_hash:
            flag_stale(ddb, table, data_domain=data_domain, dataset=dataset)
            publish_rebuild_event(data_domain, dataset, reason="stale_on_arrival")
            log.info(
                "wiki moved during authoring for %s/%s — flagged stale and "
                "queued a rebuild",
                data_domain,
                dataset,
            )
    except Exception:  # noqa: BLE001 - advisory; the reconcile is the backstop
        log.warning(
            "post-build freshness re-check failed (non-fatal)", exc_info=True
        )
    return OUTCOME_AUTHORED


def _stored_doc_parses(
    s3: Any, *, bucket: str, data_domain: str, dataset: str
) -> bool:
    """Whether the persisted policies.yaml exists and passes the schema check."""
    from okf_core import policy_doc as pdoc

    doc = read_policy_doc(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )
    if doc is None:
        return False
    try:
        pdoc.parse_policies(doc)
    except pdoc.PolicyDocError:
        return False
    return True


def _build_rules_schema(
    s3: Any, bucket: str, data_domain: str, dataset: str, glue_database: str
) -> dict[str, dict[str, list[str]]] | None:
    """``{db: {table: [columns]}}`` from the snapshot's columns.tsv, or None.

    The database name follows the same contract as every other consumer of
    the mapping row: ``glue_database`` when stamped, the dataset id as the
    fallback. Partition-key rows are columns too (queryable in SQL), so they
    stay in. None (no snapshot, unreadable, empty) means "no schema" — the
    author gate then refuses ``rules:`` blocks rather than binding blind.
    """
    key = f"{bundle_prefix(data_domain, dataset)}.metadata/columns.tsv"
    try:
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:  # noqa: BLE001 - absent snapshot is a normal state
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    # The one header-driven parser for this format — the snapshot's column
    # order varies by fork (the multi-database fork leads with `database`),
    # so a positional split here would silently build a garbage schema.
    parsed = parse_columns_tsv(text)
    if not parsed:
        return None
    db = (glue_database or dataset).lower()
    return {db: {t: sorted(cols) for t, cols in parsed.items()}}


def _author_doc(
    author: Any, *, s3: Any, bucket: str, data_domain: str, dataset: str,
    sources: list[tuple[str, bytes]], force: bool = False,
    rules_schema: dict[str, dict[str, list[str]]] | None = None,
) -> str:
    """Run the document author with the previous run's diff base wired in.

    A FORCED run (manual Sync) authors FROM SCRATCH: the prior document is
    withheld, so the agent never sees update mode. Feeding it back would
    defeat the operator's re-roll — with unchanged sources, update mode tells
    the agent to "minimally edit" and it hands the old document straight
    back, whatever model/effort/prompt improvements the force was meant to
    exercise. Fresh ids are the accepted cost (id stability is an
    update-mode courtesy, not a cross-version contract). Automatic rebuilds
    keep update mode: their sources actually changed, and minimal diffs with
    stable ids are exactly right there.
    """
    from harvest.ar_author import author_policy_doc

    author = author or author_policy_doc
    prior_doc = "" if force else (read_policy_doc(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    ) or "")
    prior_manifest = None if force else read_sources_manifest(
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
        rules_schema=rules_schema,
    )


def _read_build_state(
    ddb: Any, table: str, *, data_domain: str, dataset: str
) -> tuple[bool, str, str, str]:
    """``(registered, ar_build_status, ar_source_hash, glue_database)``.

    ``(False, "", "", "")`` when the mapping row is absent. The registration
    gate and the iff-changed skip are the whole reason harvest reads this row
    (its other registry writes are blind UpdateItems), so the runtime role
    needs GetItem on the registry table as well as UpdateItem.
    ``glue_database`` feeds the rules-schema sidecar (dataset id fallback is
    applied by the consumer — same contract as the chat scope enrichment).
    """
    item = (
        ddb.get_item(TableName=table, Key=registry_key(data_domain, dataset)).get("Item")
        or {}
    )
    return (
        bool(item),
        ((item.get(ATTR_BUILD_STATUS) or {}).get("S")) or "",
        ((item.get(ATTR_SOURCE_HASH) or {}).get("S")) or "",
        ((item.get("glue_database") or {}).get("S")) or "",
    )


def _s3_client() -> Any:
    import boto3

    return boto3.client("s3")
