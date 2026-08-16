"""Bundle version reconstruction, diffing, and restore (moto, versioned bucket).

Simulates the real write pattern end-to-end: harvest 1 publishes, harvest 2
opens with clean_authored_output-style delete markers then republishes, and a
repromote restores harvest 1 — asserting marker enumeration, snapshot cuts,
diff classification, the live sentinel, and restore mechanics.

Timing note: S3 (and moto) report ``LastModified`` at SECOND granularity, and
the snapshot cut is time-based, so phases that must be strictly ordered
(harvest N's marker vs harvest N+1's opening deletes) are separated by a >1s
``_tick()`` — mirroring reality, where they are minutes apart. Writes *within*
one phase share a second on purpose: the ``<=`` cut and the version-beats-
delete-marker tie rank must handle that, exactly as in production. To keep the
suite fast, read-only tests share one module-scoped history; the two mutating
tests build their own buckets.
"""

from __future__ import annotations

import json
import time

import boto3
import pytest
from moto import mock_aws

from okf_aws import s3_versions as sv
from okf_aws.s3_bundle import bundle_prefix, state_marker_key

DOMAIN = "sport"
DATASET = "formula_1"
PREFIX = bundle_prefix(DOMAIN, DATASET)
MARKER = state_marker_key(DOMAIN, DATASET)


def _doc(title: str, body: str) -> str:
    return (
        f"---\ntype: Glue Table\ntitle: {title}\ndescription: d\n"
        f"timestamp: t\n---\n\n{body}\n"
    )


def _put(s3, bucket: str, rel: str, text: str) -> str:
    resp = s3.put_object(Bucket=bucket, Key=f"{PREFIX}{rel}", Body=text.encode())
    return resp["VersionId"]


def _mark(s3, bucket: str, status: str, **extra) -> str:
    state = {"status": status, "data_domain": DOMAIN, "dataset": DATASET, **extra}
    resp = s3.put_object(Bucket=bucket, Key=MARKER, Body=json.dumps(state).encode())
    return resp["VersionId"]


def _tick() -> None:
    # Cross a LastModified second boundary between strictly-ordered phases.
    time.sleep(1.05)


def _make_bucket(s3, bucket: str) -> None:
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )


def _write_history(s3, bucket: str) -> tuple[str, str]:
    """Two published harvests. Returns the complete-marker VersionIds (v1, v2)."""
    # Harvest 1: in_progress -> docs -> complete (marker LAST, same second is
    # fine — the cut is `<=` on purpose).
    _mark(s3, bucket, "in_progress", started_at="2026-01-01T00:00:00+00:00")
    _put(s3, bucket, "tables/a.md", _doc("A", "alpha v1"))
    _put(s3, bucket, "tables/b.md", _doc("B", "bravo"))
    _put(s3, bucket, "index.md", "# Index v1\n")
    _put(s3, bucket, ".metadata/tables/a.md", "raw glue sheet")  # never a doc
    v1 = _mark(
        s3, bucket, "complete", completed_at="2026-01-01T01:00:00+00:00",
        tables=["a", "b"], table_versions={"a": "1", "b": "1"},
    )
    _tick()  # harvest 2 starts minutes later in reality
    # Harvest 2 opens by deleting all authored output (clean_authored_output),
    # then rewrites a, drops b, adds c — all within one phase (same second OK:
    # a same-key delete+rewrite tie resolves to the rewrite by rank).
    _mark(s3, bucket, "in_progress", started_at="2026-01-02T00:00:00+00:00")
    for rel in ("tables/a.md", "tables/b.md", "index.md"):
        s3.delete_object(Bucket=bucket, Key=f"{PREFIX}{rel}")
    _put(s3, bucket, "tables/a.md", _doc("A", "alpha v2"))
    _put(s3, bucket, "tables/c.md", _doc("C", "charlie"))
    _put(s3, bucket, "index.md", "# Index v2\n")
    v2 = _mark(
        s3, bucket, "complete", completed_at="2026-01-02T01:00:00+00:00",
        tables=["a", "c"], table_versions={"a": "2", "c": "1"},
    )
    return v1, v2


@pytest.fixture(scope="module")
def aws():
    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


@pytest.fixture(scope="module")
def shared(aws):
    """Read-only shared history: (s3, bucket, v1, v2). Do NOT mutate."""
    bucket = "test-bundles-shared"
    _make_bucket(aws, bucket)
    v1, v2 = _write_history(aws, bucket)
    return aws, bucket, v1, v2


def test_list_complete_markers_filters_and_orders(shared):
    s3, bucket, v1, v2 = shared
    markers = sv.list_complete_markers(
        s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET
    )
    assert [m.version_id for m in markers] == [v2, v1]  # newest first
    assert markers[0].is_current and not markers[1].is_current
    assert markers[0].completed_at == "2026-01-02T01:00:00+00:00"
    assert markers[1].tables == ["a", "b"]
    assert markers[1].table_versions == {"a": "1", "b": "1"}
    # in_progress marker versions delimit nothing and never appear.
    assert all(m.completed_at for m in markers)


def test_snapshot_at_reconstructs_each_harvest(shared):
    s3, bucket, v1, v2 = shared
    m2, m1 = sv.list_complete_markers(
        s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET
    )
    snap1 = sv.snapshot_at(
        s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET, marker=m1
    )
    assert sorted(snap1) == [
        f"{PREFIX}index.md",
        f"{PREFIX}tables/a.md",
        f"{PREFIX}tables/b.md",
    ]
    snap2 = sv.snapshot_at(
        s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET, marker=m2
    )
    assert sorted(snap2) == [
        f"{PREFIX}index.md",
        f"{PREFIX}tables/a.md",
        f"{PREFIX}tables/c.md",
    ]
    # Snapshot 1 resolves a.md to its HARVEST-1 content, not the live rewrite.
    body = s3.get_object(
        Bucket=bucket,
        Key=f"{PREFIX}tables/a.md",
        VersionId=snap1[f"{PREFIX}tables/a.md"].version_id,
    )["Body"].read().decode()
    assert "alpha v1" in body
    # Dot-dirs (.metadata/.harvest) never enter a snapshot.
    assert not any(
        ".metadata" in k or ".harvest" in k for k in set(snap1) | set(snap2)
    )


def test_live_snapshot_matches_latest_state(shared):
    s3, bucket, _, _ = shared
    live = sv.live_snapshot(s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET)
    assert sorted(live) == [
        f"{PREFIX}index.md",
        f"{PREFIX}tables/a.md",
        f"{PREFIX}tables/c.md",
    ]
    assert all(fv.version_id is None for fv in live.values())


def test_bundle_diff_defaults_to_last_harvest(shared):
    s3, bucket, v1, v2 = shared
    result = sv.bundle_diff(s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET)
    assert result["from"]["version_id"] == v1
    assert result["to"]["version_id"] == v2 and result["to"]["current"]
    assert result["summary"] == {"added": 1, "removed": 1, "modified": 2, "unchanged": 0}
    by_status = {f["key"]: f["status"] for f in result["files"]}
    assert by_status[f"{PREFIX}tables/c.md"] == "added"
    assert by_status[f"{PREFIX}tables/b.md"] == "removed"
    assert by_status[f"{PREFIX}tables/a.md"] == "modified"
    a_entry = next(f for f in result["files"] if f["key"] == f"{PREFIX}tables/a.md")
    assert a_entry["concept_id"] == "tables/a"
    assert a_entry["title"] == "A"
    assert "-alpha v1" in a_entry["diff"] and "+alpha v2" in a_entry["diff"]
    # index.md diffs too (it is served content) but carries no concept_id.
    idx = next(f for f in result["files"] if f["key"] == f"{PREFIX}index.md")
    assert idx["concept_id"] is None
    assert not result["truncated"]
    # Per-side version ids let a client fetch the full texts (rich view):
    # both sides on modified, None on the missing side of added/removed.
    assert a_entry["old_version_id"] and a_entry["new_version_id"]
    assert a_entry["old_version_id"] != a_entry["new_version_id"]
    added = next(f for f in result["files"] if f["status"] == "added")
    removed = next(f for f in result["files"] if f["status"] == "removed")
    assert added["old_version_id"] is None and added["new_version_id"]
    assert removed["old_version_id"] and removed["new_version_id"] is None
    # Per-file line stats, counted over the full (untruncated) diff.
    assert a_entry["lines_added"] >= 1 and a_entry["lines_removed"] >= 1
    assert added["lines_added"] > 0 and added["lines_removed"] == 0
    assert removed["lines_removed"] > 0 and removed["lines_added"] == 0


def test_bundle_diff_oldest_version_diffs_as_all_added(shared):
    s3, bucket, v1, _ = shared
    result = sv.bundle_diff(
        s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET, to_version=v1
    )
    assert result["from"]["empty"] is True
    assert result["summary"]["added"] == 3 and result["summary"]["removed"] == 0


def test_bundle_diff_rejects_bad_selectors(shared):
    s3, bucket, _, _ = shared
    with pytest.raises(ValueError, match="unknown bundle version"):
        sv.bundle_diff(
            s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET, to_version="nope"
        )
    with pytest.raises(ValueError, match="only valid as the `to`"):
        sv.bundle_diff(
            s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET, from_version="live"
        )


def test_bundle_diff_no_versions_raises(aws):
    _make_bucket(aws, "test-bundles-empty")
    with pytest.raises(ValueError, match="no published versions"):
        sv.bundle_diff(
            aws, bucket="test-bundles-empty", data_domain=DOMAIN, dataset=DATASET
        )


def test_diff_truncation_caps(shared):
    s3, bucket, _, _ = shared
    result = sv.bundle_diff(
        s3, bucket=bucket, data_domain=DOMAIN, dataset=DATASET,
        max_files=1, max_lines_per_file=2,
    )
    assert len(result["files"]) == 1
    assert result["truncated"] is True
    # Summary still counts everything, including files beyond the cap.
    assert sum(result["summary"].values()) == 4
    assert result["files"][0]["diff_truncated"] is True
    assert len(result["files"][0]["diff"].splitlines()) == 2
    # Line stats reflect the FULL diff, not the truncated text: the 2 surviving
    # lines are just the ---/+++ headers, yet the counts still see the change.
    entry = result["files"][0]
    assert entry["lines_added"] == 1 and entry["lines_removed"] == 1
    assert all(
        l.startswith(("---", "+++")) for l in entry["diff"].splitlines()
    )


def test_bundle_diff_live_sentinel_shows_interrupted_writes(aws):
    bucket = "test-bundles-interrupted"
    _make_bucket(aws, bucket)
    v1, v2 = _write_history(aws, bucket)
    _tick()  # the interrupted harvest starts strictly after v2 published
    # Interrupted harvest: in_progress marker, partial delete + partial write,
    # no complete marker (cancel/crash before finalize).
    _mark(aws, bucket, "in_progress", started_at="2026-01-03T00:00:00+00:00")
    aws.delete_object(Bucket=bucket, Key=f"{PREFIX}tables/c.md")
    _put(aws, bucket, "tables/a.md", _doc("A", "alpha HALF-WRITTEN"))
    result = sv.bundle_diff(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET, to_version="live"
    )
    # Base defaults to the current (last good) version.
    assert result["from"]["version_id"] == v2
    assert result["to"]["live"] is True
    by_status = {f["key"]: f["status"] for f in result["files"]}
    assert by_status[f"{PREFIX}tables/c.md"] == "removed"
    assert by_status[f"{PREFIX}tables/a.md"] == "modified"
    # And the versions list is unchanged — the interrupted job earned no entry.
    markers = sv.list_complete_markers(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET
    )
    assert [m.version_id for m in markers] == [v2, v1]


def test_restore_snapshot_makes_old_version_live_again(aws):
    bucket = "test-bundles-restore"
    _make_bucket(aws, bucket)
    v1, v2 = _write_history(aws, bucket)
    m1 = sv.list_complete_markers(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET
    )[1]
    snap = sv.snapshot_at(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET, marker=m1
    )
    live = sv.live_snapshot(aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET)
    _tick()  # the repromote happens strictly after harvest 2 published
    copied, deleted = sv.restore_snapshot(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET,
        snapshot=snap, live=live,
    )
    assert sorted(copied) == sorted(snap)
    assert deleted == [f"{PREFIX}tables/c.md"]
    # The live prefix now equals harvest 1's content...
    now = sv.live_snapshot(aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET)
    assert sorted(now) == sorted(snap)
    a_body = aws.get_object(Bucket=bucket, Key=f"{PREFIX}tables/a.md")[
        "Body"
    ].read().decode()
    assert "alpha v1" in a_body
    # ...via NEW version ids (append-only): the restored current version of a.md
    # is not harvest 1's original VersionId.
    head = aws.head_object(Bucket=bucket, Key=f"{PREFIX}tables/a.md")
    assert head["VersionId"] != snap[f"{PREFIX}tables/a.md"].version_id
    # A fresh complete marker (what the repromote handler writes) becomes the
    # new current version, carrying repromote provenance.
    v3 = _mark(
        aws, bucket, "complete", completed_at="2026-01-04T01:00:00+00:00",
        tables=m1.tables, table_versions=m1.table_versions,
        repromoted_from=v1, repromoted_by="user@example.com",
    )
    markers = sv.list_complete_markers(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET
    )
    assert [m.version_id for m in markers] == [v3, v2, v1]
    assert markers[0].repromoted_from == v1
    assert markers[0].repromoted_by == "user@example.com"
    # And the new head diffs empty against the version it restored.
    result = sv.bundle_diff(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET,
        from_version=v1, to_version=v3,
    )
    assert result["summary"]["modified"] == 0
    assert result["summary"]["added"] == 0 and result["summary"]["removed"] == 0
    assert result["summary"]["unchanged"] == 3


def test_restore_snapshot_refuses_oversized_bundles(aws):
    snap = {
        f"{PREFIX}tables/t{i}.md": sv.FileAt(
            key=f"{PREFIX}tables/t{i}.md", version_id="v", last_modified=None
        )
        for i in range(sv.MAX_RESTORE_FILES + 1)
    }
    with pytest.raises(ValueError, match="refusing to restore"):
        sv.restore_snapshot(
            aws, bucket="unused", data_domain=DOMAIN, dataset=DATASET,
            snapshot=snap, live={},
        )


def test_interrupted_harvest_leaves_no_current_version(aws):
    # A cancelled/crashed harvest writes in_progress and never restores the
    # complete marker: the working files are an UNCOMMITTED state, so the
    # newest complete version must NOT be labeled current — that label would
    # both lie in the UI and block the documented rollback (repromote refuses
    # a "current" version as a no-op).
    bucket = "test-bundles-cancelled"
    _make_bucket(aws, bucket)
    v1, v2 = _write_history(aws, bucket)
    _tick()
    # Harvest 3 starts (marker -> in_progress, some docs churn) then is
    # cancelled — no fresh complete marker is ever written.
    _mark(aws, bucket, "in_progress", started_at="2026-01-03T00:00:00+00:00")
    _put(aws, bucket, "tables/a.md", _doc("A", "alpha v3 half-written"))

    markers = sv.list_complete_markers(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET
    )
    # Both published versions still enumerate, newest first — but none is
    # current: the live marker version is the in_progress write.
    assert [m.version_id for m in markers] == [v2, v1]
    assert not any(m.is_current for m in markers)


def test_restore_recreates_directory_markers_for_the_mount(aws):
    """A restored tree must come back MOUNT-WRITABLE: the S3 Files NFS derives
    a directory's POSIX ownership from its zero-byte marker object, and a
    bundle whose markers were lost (a full-harvest wipe that died, then a
    repromote) presents every authored dir read-only — every later in-place
    write of a NEW doc/dir fails EACCES (seen live on generic/fpl)."""
    bucket = "test-bundles-restore-markers"
    _make_bucket(aws, bucket)
    # One published harvest with a NESTED reference doc (two dir levels).
    _mark(aws, bucket, "in_progress", started_at="2026-01-01T00:00:00+00:00")
    _put(aws, bucket, "tables/a.md", _doc("A", "alpha"))
    _put(aws, bucket, "references/joins/a__b.md", _doc("AB", "join"))
    _mark(
        aws, bucket, "complete", completed_at="2026-01-01T01:00:00+00:00",
        tables=["a"], table_versions={"a": "1"},
    )
    # An existing marker with its own (mount-written) metadata must survive.
    aws.put_object(
        Bucket=bucket,
        Key=f"{PREFIX}tables/",
        Body=b"",
        Metadata={"user-agent": "aws-s3-files", "file-owner": "1000",
                  "file-group": "1000", "file-permissions": "0040755",
                  "fs-id": "fs-test:42:1", "file-mtime": "1ns"},
    )
    m1 = sv.list_complete_markers(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET
    )[0]
    snap = sv.snapshot_at(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET, marker=m1
    )
    live = sv.live_snapshot(aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET)
    sv.restore_snapshot(
        aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET,
        snapshot=snap, live=live,
    )
    # Every directory the restored keys imply has a marker — the dataset root
    # included — carrying the aws-s3-files POSIX metadata (owner 1000, 0755).
    for d in (PREFIX, f"{PREFIX}references/", f"{PREFIX}references/joins/"):
        meta = aws.head_object(Bucket=bucket, Key=d)["Metadata"]
        assert meta["file-owner"] == "1000", d
        assert meta["file-permissions"] == "0040755", d
        assert meta["user-agent"] == "aws-s3-files", d
    # The pre-existing marker was left untouched (its fs-id survives).
    kept = aws.head_object(Bucket=bucket, Key=f"{PREFIX}tables/")["Metadata"]
    assert kept.get("fs-id") == "fs-test:42:1"
    assert kept.get("file-mtime") == "1ns"
    # Marker objects never leak into the doc-set views.
    now = sv.live_snapshot(aws, bucket=bucket, data_domain=DOMAIN, dataset=DATASET)
    assert all(k.endswith(".md") for k in now)
