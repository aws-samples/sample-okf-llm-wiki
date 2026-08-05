"""Materialize a wiki snapshot straight from S3 — no S3-Files mount needed.

A standalone benchmark run writes nothing to the bundle, so it doesn't mount
the filesystem; it GETs the docs it needs into two temp trees:

* the **solver tree** — the published ``.md`` docs only, at the requested
  version. The non-dot rule carries over from the RI-era snapshot: gold-
  blindness stays physical (``.metadata/``, ``.context/``, gold: absent from
  the solver's world).
* the **judge tree** — the same docs PLUS the ``.metadata/`` schema snapshot
  and the ``.context/`` uploaded source docs (always the latest objects; those
  are authoring inputs, not versioned content), so the judge can confirm
  ground truth the way the RI adjudicator could on the real mount.

Version selection rides :mod:`okf_aws.s3_versions`: ``version_id="`` " targets
the live bundle (``live_snapshot``); a marker VersionId targets that published
version (``snapshot_at``). **Caveat (documented in BENCHMARK_GUIDE):** pinning
pins the WIKI, not the DATA — grading always executes against live Athena.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from okf_aws.s3_bundle import bundle_prefix
from okf_aws.s3_versions import list_complete_markers, live_snapshot, snapshot_at

log = logging.getLogger("harvest.benchmark.s3_snapshot")

# The judge's extra subtrees (relative to the dataset prefix). Latest objects,
# text-decoded best-effort; binary .context files are copied as bytes so the
# code-interpreter extraction path keeps working when a sandbox is attached.
_JUDGE_EXTRA_DIRS = (".metadata/", ".context/")


class SnapshotError(RuntimeError):
    """The requested wiki version can't be materialized (loud report failure)."""


def _resolve_files(s3, *, bucket: str, data_domain: str, dataset: str, version_id: str):
    """The doc set for the requested version: ``{s3_key: FileAt}``."""
    if not version_id:
        files = live_snapshot(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset
        )
        if not files:
            raise SnapshotError("the dataset has no published wiki docs to benchmark")
        return files
    markers = list_complete_markers(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )
    marker = next((m for m in markers if m.version_id == version_id), None)
    if marker is None:
        raise SnapshotError(f"unknown bundle version: {version_id}")
    files = snapshot_at(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset, marker=marker
    )
    if not files:
        raise SnapshotError(f"bundle version {version_id} contains no docs")
    return files


def _safe_rel(key: str, prefix: str) -> str | None:
    """The key's path relative to ``prefix``, or None when it can't be laid
    out safely under the temp root (S3 keys are raw strings — a ``..`` segment
    or an absolute path would escape the snapshot dir)."""
    rel = key[len(prefix):]
    if not rel or rel.endswith("/") or rel.startswith("/"):
        return None
    if any(part in ("..", ".", "") for part in rel.split("/")):
        log.warning("Skipping unsafe bundle key %s (path escape)", key)
        return None
    return rel


def _write_object(s3, bucket: str, key: str, version_id, dest: Path) -> None:
    kwargs = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    body = s3.get_object(**kwargs)["Body"].read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))


def materialize_snapshots(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    version_id: str,
    solver_dir: str,
    judge_dir: str | None = None,
) -> int:
    """Fill ``solver_dir`` (and optionally ``judge_dir``) from S3; return the doc count.

    Docs are fetched ONCE (written to the solver tree, hard-link-copied into
    the judge tree via a plain byte copy — S3 GET is the cost, not the local
    write). Judge extras (``.metadata/``, ``.context/``) are best-effort: a
    dataset without them (or a GET failure on one object) degrades the judge's
    context, never the run. ``judge_dir=None`` skips the judge copy AND the
    extras sweep entirely — for callers like the annotation aggregator that
    only need the doc tree (materializing a throwaway judge tree used to leak
    a bundle-sized temp dir per aggregation and double the S3 GETs).
    """
    files = _resolve_files(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset,
        version_id=version_id,
    )
    prefix = bundle_prefix(data_domain, dataset)
    solver_root = Path(solver_dir)
    judge_root = Path(judge_dir) if judge_dir else None

    written = 0
    for key, fv in sorted(files.items()):
        rel = _safe_rel(key, prefix)
        if rel is None:
            continue
        _write_object(s3, bucket, key, fv.version_id, solver_root / rel)
        written += 1
        if judge_root is not None:
            judge_dest = judge_root / rel
            judge_dest.parent.mkdir(parents=True, exist_ok=True)
            judge_dest.write_bytes((solver_root / rel).read_bytes())

    if judge_root is not None:
        for extra in _JUDGE_EXTRA_DIRS:
            _copy_latest_tree(
                s3, bucket=bucket, prefix=prefix + extra,
                dest_root=judge_root / extra.rstrip("/"),
            )
    return written


def _copy_latest_tree(s3, *, bucket: str, prefix: str, dest_root: Path) -> None:
    """Best-effort copy of every latest object under ``prefix`` into ``dest_root``."""
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            resp = s3.list_objects_v2(**kwargs)
        except Exception:  # noqa: BLE001 - judge extras are best-effort
            log.warning("Could not list %s for the judge tree", prefix, exc_info=True)
            return
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            rel = _safe_rel(key, prefix)
            if rel is None:
                continue
            try:
                _write_object(s3, bucket, key, None, dest_root / rel)
            except Exception:  # noqa: BLE001 - one bad object must not kill the tree
                log.warning("Could not fetch %s for the judge tree", key, exc_info=True)
        if not resp.get("IsTruncated"):
            return
        token = resp.get("NextContinuationToken")
        if not token:
            return


def default_bucket() -> str:
    bucket = os.environ.get("OKF_BUNDLE_BUCKET", "")
    if not bucket:
        raise SnapshotError("OKF_BUNDLE_BUCKET is not configured")
    return bucket
