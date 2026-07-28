"""Bundle version history — reconstructed on read from S3 object versions.

A bundle *version* is one ``status: "complete"`` write of the dataset's
``.harvest/state.json`` commit marker. ``finalize_bundle`` writes that marker
LAST (docs/CONVENTIONS.md), so the marker version's own ``LastModified`` is a
valid cut point: every object version belonging to that harvest has
``LastModified <=`` the marker's, and the next harvest's opening delete markers
(``clean_authored_output``) land strictly after it. A version's identity is the
marker object's S3 ``VersionId``; its label is the marker JSON's
``completed_at``.

Nothing is recorded at write time. History is reconstructed from
``list_object_versions``, which buys two properties a finalize-time manifest
would not have: every harvest already sitting in the versioned bundle bucket is
browsable retroactively, and versions removed by the noncurrent-version
lifecycle rule simply drop out of the list — there is no manifest to dangle.

Used by the Control API (versions / diff / repromote endpoints) and the
consumption ``get_bundle_diff`` tool. Raises ``ValueError`` for caller mistakes
(unknown version id, nothing published) so each surface can map it to its own
error convention (HTTP 400 / MCP tool error).
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from okf_aws.s3_bundle import bundle_prefix, parse_bundle_key, state_marker_key

# The `to` sentinel meaning "the live working state" (plain latest objects, no
# marker anchor). Lets a diff show what an interrupted (cancelled/crashed)
# harvest half-wrote, since those writes belong to no complete-marker version.
LIVE_SENTINEL = "live"

# Repromote guard: a bundle beyond this is not a Data Wiki bundle (real bundles
# are tens of files). Bounds the synchronous CopyObject loop far below the
# Lambda / API GW 30s budget.
MAX_RESTORE_FILES = 1000


@dataclass
class MarkerVersion:
    """One published bundle version (a ``complete`` commit-marker version)."""

    version_id: str
    completed_at: str
    last_modified: datetime
    tables: list[str]
    table_versions: dict[str, Any]
    repromoted_from: str  # marker VersionId this version restored ("" = harvest)
    repromoted_by: str  # caller identity for a repromote ("" = harvest)
    is_current: bool

    def descriptor(self) -> dict[str, Any]:
        """The wire shape shared by the versions list and diff endpoints."""
        return {
            "version_id": self.version_id,
            "completed_at": self.completed_at,
            "current": self.is_current,
            "repromoted_from": self.repromoted_from or None,
            "repromoted_by": self.repromoted_by or None,
        }


@dataclass
class FileAt:
    """One doc's resolved object version inside a snapshot.

    ``version_id`` is None for a live-snapshot entry (read without a
    ``VersionId``, i.e. whatever is current when fetched).
    """

    key: str
    version_id: str | None
    last_modified: datetime | None


def _is_doc_key(key: str, prefix: str) -> bool:
    """True for the ``.md`` files that make up the *served* bundle.

    Broader than ``parse_bundle_key`` on purpose: ``index.md``/``log.md`` are
    part of what consumers read (``list_directory`` serves index.md), so they
    version, diff, and restore with the bundle. Dot-prefixed dirs
    (``.harvest``/``.metadata``/``.context``) stay excluded — they are authoring
    state, not published content.
    """
    if not key.startswith(prefix) or not key.endswith(".md"):
        return False
    parts = key[len(prefix) :].split("/")
    return all(p and not p.startswith(".") for p in parts)


def _iter_versions(s3, bucket: str, prefix: str):
    """Yield ``(entry, is_delete_marker)`` for every version under ``prefix``.

    Preserves S3's returned order (per key: newest first), which the snapshot
    tie-break below relies on for same-timestamp writes of one key.
    """
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = s3.list_object_versions(**kwargs)
        for v in resp.get("Versions", []):
            yield v, False
        for d in resp.get("DeleteMarkers", []):
            yield d, True
        if not resp.get("IsTruncated"):
            break
        kwargs["KeyMarker"] = resp.get("NextKeyMarker", "")
        kwargs["VersionIdMarker"] = resp.get("NextVersionIdMarker", "")
        if not kwargs["KeyMarker"]:
            break


def list_complete_markers(
    s3, *, bucket: str, data_domain: str, dataset: str, limit: int = 50
) -> list[MarkerVersion]:
    """Enumerate the bundle's versions: ``complete`` marker versions, newest first.

    ``in_progress`` marker writes (harvest start / repromote in-flux) and
    unparseable versions are filtered out — they delimit nothing.

    ``is_current`` is true only for a complete marker that is ALSO the marker
    key's LIVE object version (S3's ``IsLatest``). The newest complete version
    is NOT automatically current: after an interrupted (cancelled/crashed)
    harvest the live marker is an ``in_progress`` write, the working files are
    an uncommitted state, and NO version is current — which is what lets that
    last good version be repromoted (a "current" version is refused as a
    no-op), i.e. the documented rollback path for interrupted harvests.
    """
    key = state_marker_key(data_domain, dataset)
    out: list[MarkerVersion] = []
    for entry, is_dm in _iter_versions(s3, bucket, key):
        if is_dm or entry.get("Key") != key or len(out) >= limit:
            continue
        version_id = entry.get("VersionId", "")
        try:
            obj = s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)
            state = json.loads(obj["Body"].read())
        except Exception:  # noqa: BLE001 - unreadable marker version -> not a version
            continue
        if not isinstance(state, dict) or state.get("status") != "complete":
            continue
        out.append(
            MarkerVersion(
                version_id=version_id,
                completed_at=str(state.get("completed_at") or ""),
                last_modified=entry.get("LastModified"),
                tables=list(state.get("tables") or []),
                table_versions=dict(state.get("table_versions") or {}),
                repromoted_from=str(state.get("repromoted_from") or ""),
                repromoted_by=str(state.get("repromoted_by") or ""),
                is_current=bool(entry.get("IsLatest")),
            )
        )
    return out


def snapshot_at(
    s3, *, bucket: str, data_domain: str, dataset: str, marker: MarkerVersion
) -> dict[str, FileAt]:
    """The file set of one bundle version: ``{s3_key: FileAt}``.

    For every doc key under the dataset prefix, the newest object version with
    ``LastModified <= marker.last_modified`` — absent if that entry is a delete
    marker or nothing qualifies. ``<=`` (not ``<``) covers docs written in the
    same second as the marker (S3 timestamps are second-granular); on an exact
    same-key timestamp tie a real version outranks a delete marker (in this
    system a delete-then-rewrite of one key never happens within a second), and
    otherwise first-seen wins, matching S3's newest-first per-key order.
    """
    prefix = bundle_prefix(data_domain, dataset)
    cut = marker.last_modified
    # key -> (last_modified, rank, is_delete_marker, version_id)
    best: dict[str, tuple[Any, int, bool, str]] = {}
    for entry, is_dm in _iter_versions(s3, bucket, prefix):
        key = entry.get("Key", "")
        if not _is_doc_key(key, prefix):
            continue
        lm = entry.get("LastModified")
        if lm is None or (cut is not None and lm > cut):
            continue
        cand = (lm, 0 if is_dm else 1, is_dm, entry.get("VersionId", ""))
        cur = best.get(key)
        if cur is None or cand[:2] > cur[:2]:
            best[key] = cand
    return {
        key: FileAt(key=key, version_id=vid, last_modified=lm)
        for key, (lm, _rank, is_dm, vid) in best.items()
        if not is_dm
    }


def live_snapshot(s3, *, bucket: str, data_domain: str, dataset: str) -> dict[str, FileAt]:
    """The bundle's live working state: the plain latest objects, no anchor.

    Unlike :func:`snapshot_at` this can describe a state no complete marker ever
    blessed — exactly what an interrupted harvest leaves behind.
    """
    prefix = bundle_prefix(data_domain, dataset)
    out: dict[str, FileAt] = {}
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if _is_doc_key(key, prefix):
                out[key] = FileAt(
                    key=key, version_id=None, last_modified=obj.get("LastModified")
                )
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if not token:
            break
    return out


def _get_text(s3, bucket: str, key: str, version_id: str | None) -> str:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    raw = s3.get_object(**kwargs)["Body"].read()
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def _doc_labels(text: str) -> tuple[str, str]:
    """Best-effort (title, type) from a doc's frontmatter for diff-list labels."""
    try:
        from okf_core.document import OKFDocument

        fm = OKFDocument.parse(text).frontmatter or {}
        return str(fm.get("title") or ""), str(fm.get("type") or "")
    except Exception:  # noqa: BLE001 - a malformed doc still diffs, just unlabeled
        return "", ""


def diff_snapshots(
    s3,
    *,
    bucket: str,
    from_snap: dict[str, FileAt],
    to_snap: dict[str, FileAt],
    max_files: int = 200,
    max_lines_per_file: int = 400,
) -> dict[str, Any]:
    """Classify + unified-diff two snapshots.

    ETags cannot shortcut content equality here (the bundle bucket is SSE-KMS),
    and a full harvest rewrites every file with new VersionIds anyway — so every
    key whose VersionIds differ is fetched on both sides and compared as text;
    byte-identical pairs count as ``unchanged`` and emit no entry. Caps follow
    the consumption-tool truncation convention: at most ``max_files`` diff
    entries (``truncated: true`` beyond — remaining files still count in
    ``summary``) and ``max_lines_per_file`` diff lines per file
    (``diff_truncated: true``).
    """
    summary = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
    files: list[dict[str, Any]] = []
    truncated = False
    for key in sorted(set(from_snap) | set(to_snap)):
        a = from_snap.get(key)
        b = to_snap.get(key)
        if a and b and a.version_id is not None and a.version_id == b.version_id:
            summary["unchanged"] += 1
            continue
        old_text = _get_text(s3, bucket, key, a.version_id) if a else ""
        new_text = _get_text(s3, bucket, key, b.version_id) if b else ""
        if a and b and old_text == new_text:
            summary["unchanged"] += 1
            continue
        status = "added" if not a else ("removed" if not b else "modified")
        summary[status] += 1
        if len(files) >= max_files:
            truncated = True
            continue
        title, type_ = _doc_labels(new_text or old_text)
        diff_lines = list(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=f"a/{key}",
                tofile=f"b/{key}",
                lineterm="",
            )
        )
        loc = parse_bundle_key(key)
        # Counted over the FULL diff (before the line cap) so the header stats
        # stay accurate even when the diff text itself is truncated.
        lines_added = sum(
            1 for l in diff_lines if l.startswith("+") and not l.startswith("+++")
        )
        lines_removed = sum(
            1 for l in diff_lines if l.startswith("-") and not l.startswith("---")
        )
        files.append(
            {
                "key": key,
                "concept_id": loc.concept_id if loc else None,
                "status": status,
                "title": title,
                "type": type_,
                "lines_added": lines_added,
                "lines_removed": lines_removed,
                "diff": "\n".join(diff_lines[:max_lines_per_file]),
                "diff_truncated": len(diff_lines) > max_lines_per_file,
                # Per-side object versions so a client can fetch the FULL texts
                # (the UI's rendered "rich" diff). None on the missing side of
                # added/removed files AND for live-snapshot sides (fetch latest).
                "old_version_id": a.version_id if a else None,
                "new_version_id": b.version_id if b else None,
            }
        )
    return {"summary": summary, "files": files, "truncated": truncated}


def bundle_diff(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    from_version: str = "",
    to_version: str = "",
    max_files: int = 200,
    max_lines_per_file: int = 400,
) -> dict[str, Any]:
    """Resolve two version selectors and diff them. The one-call entry point.

    Defaults answer "what changed in the last harvest": ``to`` omitted -> the
    current version; ``from`` omitted -> the version before ``to`` (an empty
    set when ``to`` is the oldest — a first harvest diffs as all-added).
    ``to`` may be :data:`LIVE_SENTINEL` to diff against the live working state
    (interrupted-harvest inspection); ``from`` may not (there is nothing newer
    than live to compare it to). Raises ``ValueError`` on unknown ids.
    """
    markers = list_complete_markers(
        s3, bucket=bucket, data_domain=data_domain, dataset=dataset
    )

    def _find(version_id: str) -> MarkerVersion:
        for m in markers:
            if m.version_id == version_id:
                return m
        raise ValueError(f"unknown bundle version: {version_id}")

    if from_version == LIVE_SENTINEL:
        raise ValueError("'live' is only valid as the `to` side of a diff")

    to_marker: MarkerVersion | None = None
    if to_version == LIVE_SENTINEL:
        to_snap = live_snapshot(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset
        )
        to_desc: dict[str, Any] = {"live": True, "version_id": None, "completed_at": None}
    else:
        if to_version:
            to_marker = _find(to_version)
        elif markers:
            to_marker = markers[0]
        else:
            raise ValueError("no published versions for this dataset")
        to_snap = snapshot_at(
            s3, bucket=bucket, data_domain=data_domain, dataset=dataset, marker=to_marker
        )
        to_desc = to_marker.descriptor()

    if from_version:
        from_marker: MarkerVersion | None = _find(from_version)
    elif to_marker is not None:
        idx = markers.index(to_marker)
        from_marker = markers[idx + 1] if idx + 1 < len(markers) else None
    else:  # to == live: default base is the current (last good) version
        from_marker = markers[0] if markers else None

    if from_marker is not None:
        from_snap = snapshot_at(
            s3,
            bucket=bucket,
            data_domain=data_domain,
            dataset=dataset,
            marker=from_marker,
        )
        from_desc: dict[str, Any] = from_marker.descriptor()
    else:
        from_snap = {}
        from_desc = {"version_id": None, "completed_at": None, "empty": True}

    result = diff_snapshots(
        s3,
        bucket=bucket,
        from_snap=from_snap,
        to_snap=to_snap,
        max_files=max_files,
        max_lines_per_file=max_lines_per_file,
    )
    result["from"] = from_desc
    result["to"] = to_desc
    return result


def restore_snapshot(
    s3,
    *,
    bucket: str,
    data_domain: str,
    dataset: str,
    snapshot: dict[str, FileAt],
    live: dict[str, FileAt],
) -> tuple[list[str], list[str]]:
    """Make the live prefix equal ``snapshot``. Returns ``(copied, deleted)`` keys.

    Append-only by construction: each file is ``CopyObject``-ed from its source
    ``VersionId`` onto the same key (S3 mints a NEW current version — old ids
    are never resurrected), and live docs absent from the snapshot get a delete
    marker. Every snapshot file is copied even if content-identical, so the
    resulting Object Created events refresh every vector uniformly (this is
    what makes the freshness-row convergence check sufficient). Idempotent: a
    retry over a half-completed restore converges to the same state.

    The caller (Control API repromote) is responsible for the harvest lease and
    the surrounding state-marker writes.
    """
    if len(snapshot) > MAX_RESTORE_FILES:
        raise ValueError(
            f"bundle version has {len(snapshot)} files; refusing to restore more "
            f"than {MAX_RESTORE_FILES}"
        )
    copied: list[str] = []
    deleted: list[str] = []
    for key in sorted(snapshot):
        fv = snapshot[key]
        s3.copy_object(
            Bucket=bucket,
            Key=key,
            CopySource={"Bucket": bucket, "Key": key, "VersionId": fv.version_id},
        )
        copied.append(key)
    for key in sorted(set(live) - set(snapshot)):
        s3.delete_object(Bucket=bucket, Key=key)
        deleted.append(key)
    return copied, deleted
