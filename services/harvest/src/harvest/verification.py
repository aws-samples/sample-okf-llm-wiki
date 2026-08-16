"""Fold the off-mount verification overlay into computation docs — through the mount.

The human Verify/Unverify clicks land in ``verification/<domain>/<dataset>.json``
(Control API, off-mount — a raw Lambda ``put_object`` into the bundle tree would
materialize a root-owned path the runtime's uid-1000 mount identity can't write:
the ``pending.json`` EACCES incident). This module is the second half of that
contract: at the END of a harvest run — after all authoring, before the commit
marker — the runtime folds each entry into its doc's frontmatter triple via the
canonical serializer, so the bundle ends up CARRYING its verification (portable,
spec-shaped) and the overlay shrinks back toward empty.

End-of-run rather than start-of-run on purpose: a full harvest wipes authored
output at start, so a stamp folded (and removed from the overlay) before the
wipe would be lost minutes later. Folding after authoring sees the run's final
docs; the hash binding decides correctness either way.

Entry dispositions (all decided by :func:`okf_core.computations.
fold_verification_entry`):

* hash matches the doc      -> triple written into frontmatter, entry removed;
* hash no longer matches    -> entry KEPT — serving keeps surfacing ``stale``
                               until a human re-verifies or unverifies;
* ``revoked`` tombstone     -> the doc's triple is nulled (hash-independent)
                               and the tombstone removed;
* the doc is gone           -> entry dropped (a stamp with no doc is
                               meaningless; a tombstone with no doc is
                               satisfied).

Platform code only — the write bypasses the agent guard by design (the guard's
verification-field rule polices AGENT writes; this is the one sanctioned
writer). Best-effort throughout: a fold failure must never fail a finished
multi-hour run.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from harvest.fsutil import write_text
from okf_core.computations import (
    COMPUTATIONS_PREFIX,
    find_computations,
    fold_verification_entry,
    is_frozen,
)

log = logging.getLogger(__name__)


def frozen_computation_paths(
    dataset_root: str | Path,
    *,
    data_domain: str,
    dataset: str,
    s3=None,
    bucket: str | None = None,
) -> frozenset[str]:
    """Root-relative doc paths of the VERIFIED (and therefore FROZEN)
    computations — the set the guard refuses agent writes to and the
    full-harvest wipe preserves.

    Two sources, unioned, because verification lives in two places between
    runs (docs/ATTESTED_COMPUTATIONS.md §4): the doc's own folded triple
    (hash-checked — an already-diverged doc reads ``stale`` and is NOT
    frozen: it needs repair, which requires being writable), and the
    off-mount overlay for clicks not yet folded in (again only when the
    signed hash matches the doc on disk). ``revoked`` tombstones never
    freeze. Best-effort on the overlay side: no bucket/creds degrades to
    folded-only, never to a crash.
    """
    frozen: set[str] = set()
    docs = {rel: comp for rel, comp, _errs in find_computations(dataset_root)}
    for rel, comp in docs.items():
        if comp is not None and is_frozen(comp):
            frozen.add(rel)
    bucket = bucket or os.environ.get("OKF_BUNDLE_BUCKET", "")
    if bucket:
        try:
            from okf_aws import computation_run as cr

            if s3 is None:
                import boto3

                s3 = boto3.client(
                    "s3", region_name=os.environ.get("AWS_REGION", "us-east-1")
                )
            for slug, entry in cr.load_overlay(
                s3, bucket, data_domain, dataset
            ).items():
                if entry.get("revoked") or not entry.get("verified"):
                    continue
                rel = f"{COMPUTATIONS_PREFIX}{slug}.md"
                comp = docs.get(rel)
                if comp is not None and entry.get("sha256") == comp.sha256:
                    frozen.add(rel)
        except Exception:  # noqa: BLE001 - folded stamps still freeze
            log.warning(
                "could not read the verification overlay for %s/%s; freezing "
                "folded-verified computations only",
                data_domain,
                dataset,
                exc_info=True,
            )
    return frozenset(frozen)


def fold_verification_overlay(
    dataset_root: str | Path,
    *,
    data_domain: str,
    dataset: str,
    s3=None,
    bucket: str | None = None,
) -> dict[str, int]:
    """Fold every foldable overlay entry; return ``{folded, dropped, kept}``.

    No bucket configured (offline tests, local runs) or no overlay -> all
    zeros, no clients built.
    """
    out = {"folded": 0, "dropped": 0, "kept": 0}
    bucket = bucket or os.environ.get("OKF_BUNDLE_BUCKET", "")
    if not bucket:
        return out
    from okf_aws import computation_run as cr

    if s3 is None:
        import boto3

        s3 = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
    # STRICT load: this function read-modify-writes the whole overlay object,
    # so a transient S3 error misread as "empty" would wipe every entry.
    entries = cr.load_overlay(s3, bucket, data_domain, dataset, strict=True)
    if not entries:
        return out
    root = Path(dataset_root)
    resolved: set[str] = set()  # slugs whose entries this run consumed
    for slug, entry in entries.items():
        doc_path = root / COMPUTATIONS_PREFIX / f"{slug}.md"
        if not doc_path.is_file():
            resolved.add(slug)
            out["dropped"] += 1
            continue
        try:
            text = doc_path.read_text(encoding="utf-8")
        except OSError:
            out["kept"] += 1  # transient mount read error — retry next run
            continue
        folded = fold_verification_entry(text, entry)
        if folded is None:
            if entry.get("revoked"):
                # Nothing to null — the tombstone already did its job.
                resolved.add(slug)
                out["dropped"] += 1
            else:
                # The doc changed since the click: keep the entry so serving
                # keeps saying ``stale`` until a human re-verifies/unverifies.
                out["kept"] += 1
            continue
        write_text(doc_path, folded)  # through the mount — uid-1000 ownership
        resolved.add(slug)
        out["folded"] += 1
    if resolved:
        # RE-LOAD before saving: the doc-write loop above takes seconds on the
        # NFS mount, and a Verify/Unverify click landing inside that window
        # writes the overlay concurrently. Saving our stale snapshot would
        # ERASE that click (the UI would report success, then read
        # unverified). So the save keeps everything the latest overlay holds
        # and removes ONLY the entries this run consumed that are UNCHANGED
        # since our snapshot — a changed entry means a human acted mid-fold,
        # and their action wins (it folds next run). The residual race is the
        # milliseconds between this re-load and the PUT, down from the whole
        # fold; a click lost there still self-heals via re-verify.
        latest = cr.load_overlay(s3, bucket, data_domain, dataset, strict=True)
        removed_any = False
        for slug in resolved:
            if latest.get(slug) == entries.get(slug):
                latest.pop(slug, None)
                removed_any = True
        if removed_any:
            cr.save_overlay(s3, bucket, data_domain, dataset, latest)
    if out["folded"] or out["dropped"]:
        log.info(
            "verification overlay for %s/%s: folded %d, dropped %d, kept %d",
            data_domain,
            dataset,
            out["folded"],
            out["dropped"],
            out["kept"],
        )
    return out
