"""The verification overlay fold-in (harvest.verification): entries fold into
doc frontmatter through the mount at finalize time, tombstones null, stale
entries persist, and the whole thing no-ops without a bucket."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from harvest.verification import fold_verification_overlay
from okf_core.computations import (
    COMPUTATIONS_PREFIX,
    parse_computation_text,
)

BUCKET = "okf-bundles"
DOMAIN, DATASET = "sales", "orders"

_DOC = (
    "---\n"
    "type: Attested Computation\n"
    "title: T\n"
    "description: d\n"
    "runtime: athena\n"
    "parameters:\n"
    "  - {name: n, type: integer, required: true, example: 1}\n"
    "verified: null\n"
    "verified_by: null\n"
    "timestamp: t\n"
    "---\n\n# Computation\n\n```sql\nSELECT @n AS n\n```\n"
)


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _overlay(s3, entries):
    s3.put_object(
        Bucket=BUCKET,
        Key=f"verification/{DOMAIN}/{DATASET}.json",
        Body=json.dumps({"version": 1, "entries": entries}).encode(),
    )


def _read_overlay(s3):
    body = s3.get_object(
        Bucket=BUCKET, Key=f"verification/{DOMAIN}/{DATASET}.json"
    )["Body"].read()
    return json.loads(body)["entries"]


def _write_doc(root, slug="t", text=_DOC):
    d = root / COMPUTATIONS_PREFIX
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(text)
    return d / f"{slug}.md"


def test_matching_entry_folds_and_is_removed(tmp_path, s3):
    doc_path = _write_doc(tmp_path)
    comp, _ = parse_computation_text("references/computations/t.md", _DOC)
    _overlay(
        s3,
        {
            "t": {
                "slug": "t",
                "sha256": comp.sha256,
                "verified": "2026-08-14T09:30:00Z",
                "verified_by": "analyst@example.com",
            }
        },
    )
    out = fold_verification_overlay(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert out == {"folded": 1, "dropped": 0, "kept": 0}
    folded, errors = parse_computation_text(
        "references/computations/t.md", doc_path.read_text()
    )
    assert errors == []
    assert folded.verified_by == "analyst@example.com"
    assert folded.verified_sha256 == comp.sha256
    assert folded.sha256 == comp.sha256  # folding never changes the hash
    assert _read_overlay(s3) == {}


def test_stale_entry_is_kept_for_serving(tmp_path, s3):
    doc_path = _write_doc(tmp_path)
    entry = {
        "slug": "t",
        "sha256": "b" * 64,  # the doc changed since the click
        "verified": "2026-08-14T09:30:00Z",
        "verified_by": "analyst@example.com",
    }
    _overlay(s3, {"t": entry})
    out = fold_verification_overlay(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert out == {"folded": 0, "dropped": 0, "kept": 1}
    assert doc_path.read_text() == _DOC  # untouched
    assert _read_overlay(s3) == {"t": entry}  # still surfacing `stale`


def test_revoked_tombstone_nulls_a_folded_stamp(tmp_path, s3):
    comp, _ = parse_computation_text("references/computations/t.md", _DOC)
    stamped = _DOC.replace(
        "verified: null\nverified_by: null\n",
        "verified: 2026-08-14T09:30:00Z\nverified_by: a@x\n"
        f"verified_sha256: {comp.sha256}\n",
    )
    doc_path = _write_doc(tmp_path, text=stamped)
    _overlay(s3, {"t": {"slug": "t", "revoked": True, "revoked_by": "a@x"}})
    out = fold_verification_overlay(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert out["folded"] == 1
    folded, _ = parse_computation_text(
        "references/computations/t.md", doc_path.read_text()
    )
    assert folded.verified is None and folded.verified_by is None
    assert _read_overlay(s3) == {}


def test_tombstone_with_nothing_to_null_is_dropped(tmp_path, s3):
    _write_doc(tmp_path)  # unstamped doc
    _overlay(s3, {"t": {"slug": "t", "revoked": True}})
    out = fold_verification_overlay(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert out == {"folded": 0, "dropped": 1, "kept": 0}
    assert _read_overlay(s3) == {}


def test_entry_for_a_retired_doc_is_dropped(tmp_path, s3):
    (tmp_path / COMPUTATIONS_PREFIX).mkdir(parents=True)
    _overlay(s3, {"gone": {"slug": "gone", "sha256": "a" * 64, "verified": "t"}})
    out = fold_verification_overlay(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert out == {"folded": 0, "dropped": 1, "kept": 0}
    assert _read_overlay(s3) == {}


def test_no_bucket_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("OKF_BUNDLE_BUCKET", raising=False)
    out = fold_verification_overlay(tmp_path, data_domain=DOMAIN, dataset=DATASET)
    assert out == {"folded": 0, "dropped": 0, "kept": 0}


def test_finalize_bundle_runs_the_fold(tmp_path, s3, monkeypatch):
    # End-to-end through finalize: the fold happens before the commit marker.
    from harvest.finalize import finalize_bundle

    monkeypatch.setenv("OKF_BUNDLE_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    doc_path = _write_doc(tmp_path)
    comp, _ = parse_computation_text("references/computations/t.md", _DOC)
    _overlay(
        s3,
        {
            "t": {
                "slug": "t",
                "sha256": comp.sha256,
                "verified": "2026-08-14T09:30:00Z",
                "verified_by": "analyst@example.com",
            }
        },
    )
    state = finalize_bundle(
        tmp_path,
        data_domain=DOMAIN,
        dataset=DATASET,
        tables=[],
        timestamp="2026-08-14T10:00:00Z",
    )
    assert state["status"] == "complete"
    folded, _ = parse_computation_text(
        "references/computations/t.md", doc_path.read_text()
    )
    assert folded.verified_by == "analyst@example.com"


# -- frozen_computation_paths (the guard's + the wipe's freeze set) -----------


def test_frozen_from_folded_stamp(tmp_path, s3):
    from harvest.verification import frozen_computation_paths
    from okf_core.computations import parse_computation_text

    comp, _ = parse_computation_text("references/computations/t.md", _DOC)
    stamped = _DOC.replace(
        "verified: null\nverified_by: null\n",
        "verified: 2026-08-14T09:30:00Z\nverified_by: a@x\n"
        f"verified_sha256: {comp.sha256}\n",
    )
    _write_doc(tmp_path, text=stamped)
    _write_doc(tmp_path, slug="draft")  # unverified sibling stays unfrozen
    frozen = frozen_computation_paths(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert frozen == {"references/computations/t.md"}


def test_frozen_from_overlay_click_not_yet_folded(tmp_path, s3):
    from harvest.verification import frozen_computation_paths
    from okf_core.computations import parse_computation_text

    comp, _ = parse_computation_text("references/computations/t.md", _DOC)
    _write_doc(tmp_path)
    _overlay(
        s3,
        {
            "t": {
                "slug": "t",
                "sha256": comp.sha256,
                "verified": "2026-08-14T09:30:00Z",
                "verified_by": "a@x",
            },
            # A STALE click (doc changed since) must NOT freeze — the doc
            # needs repair, which requires being writable.
            "gone_stale": {
                "slug": "gone_stale",
                "sha256": "b" * 64,
                "verified": "t",
                "verified_by": "a@x",
            },
            # A revoked tombstone never freezes.
            "revoked_one": {"slug": "revoked_one", "revoked": True},
        },
    )
    _write_doc(tmp_path, slug="gone_stale")
    _write_doc(tmp_path, slug="revoked_one")
    frozen = frozen_computation_paths(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert frozen == {"references/computations/t.md"}


def test_frozen_without_bucket_uses_folded_only(tmp_path, monkeypatch):
    from harvest.verification import frozen_computation_paths

    monkeypatch.delenv("OKF_BUNDLE_BUCKET", raising=False)
    _write_doc(tmp_path)  # unverified
    assert (
        frozen_computation_paths(tmp_path, data_domain=DOMAIN, dataset=DATASET)
        == frozenset()
    )


def test_full_harvest_wipe_takes_verified_computations_too(tmp_path):
    """A full harvest is the DESTRUCTIVE mode: the wipe is unconditional, so
    even a human-verified computation goes and is re-authored from source (its
    verification returns to the human's queue). Freezing here would also
    deadlock — a wiped doc whose path is frozen could never be re-authored."""
    from harvest.fsutil import clean_authored_output
    from okf_core.computations import parse_computation_text

    comp, _ = parse_computation_text("references/computations/t.md", _DOC)
    stamped = _DOC.replace(
        "verified: null\nverified_by: null\n",
        "verified: 2026-08-14T09:30:00Z\nverified_by: a@x\n"
        f"verified_sha256: {comp.sha256}\n",
    )
    _write_doc(tmp_path, text=stamped)
    (tmp_path / ".harvest").mkdir()
    (tmp_path / ".harvest" / "state.json").write_text("{}")
    (tmp_path / ".context").mkdir()
    (tmp_path / ".context" / "dict.csv").write_text("x")

    removed = clean_authored_output(tmp_path)

    assert not (tmp_path / "references").exists()  # verified doc gone with it
    assert removed == ["references"]
    # Inputs + run state still preserved (the only keep-rule there is).
    assert (tmp_path / ".harvest" / "state.json").is_file()
    assert (tmp_path / ".context" / "dict.csv").is_file()


def test_full_harvest_path_does_not_freeze(tmp_path):
    """The freeze applies to the IN-PLACE modes only. Source-pinned: the full
    path must wipe unconditionally and build its agent without a freeze set,
    while every scoped/cross/annotation path resolves and passes one."""
    import inspect

    from harvest import runner

    full = inspect.getsource(runner.run_full_harvest)
    assert "clean_authored_output(dataset_root)" in full
    assert "frozen_paths" not in full
    for fn in (
        runner.run_incremental_harvest,
        runner.run_cross_harvest,
        runner.run_annotation_harvest,
    ):
        src = inspect.getsource(fn)
        assert "frozen_computation_paths(" in src, fn.__name__
        assert "frozen_paths=frozen" in src, fn.__name__


def test_revoked_tombstone_nulls_even_a_shape_invalid_doc(tmp_path, s3):
    """Revocation is unconditional: a doc that went shape-INVALID while
    carrying a folded stamp must still get its triple nulled — dropping the
    tombstone as 'satisfied' would let a later repair to equivalent content
    resurrect a verification the human explicitly revoked."""
    from okf_core.computations import parse_computation_text

    comp, _ = parse_computation_text("references/computations/t.md", _DOC)
    stamped_invalid = _DOC.replace(
        "verified: null\nverified_by: null\n",
        "verified: 2026-08-14T09:30:00Z\nverified_by: a@x\n"
        f"verified_sha256: {comp.sha256}\n",
    ).replace("SELECT @n AS n", "SELECT @n, @ghost AS n")  # undeclared hole
    doc_path = _write_doc(tmp_path, text=stamped_invalid)
    _overlay(s3, {"t": {"slug": "t", "revoked": True, "revoked_by": "a@x"}})
    out = fold_verification_overlay(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert out["folded"] == 1
    from okf_core.document import OKFDocument

    fm = OKFDocument.parse(doc_path.read_text()).frontmatter
    assert fm["verified"] is None and fm["verified_sha256"] is None
    assert _read_overlay(s3) == {}


def test_fold_preserves_a_click_landing_mid_fold(tmp_path, s3, monkeypatch):
    """The save must re-load the LATEST overlay: a Verify click landing while
    the fold rewrites docs would otherwise be erased by the stale snapshot."""
    from okf_core.computations import parse_computation_text
    import harvest.verification as hv

    comp, _ = parse_computation_text("references/computations/t.md", _DOC)
    _write_doc(tmp_path)
    entry = {
        "slug": "t", "sha256": comp.sha256,
        "verified": "2026-08-14T09:30:00Z", "verified_by": "a@x",
    }
    _overlay(s3, {"t": entry})

    # Simulate the mid-fold click: the first doc write also lands a NEW
    # overlay entry for another slug.
    real_write = hv.write_text

    def write_and_click(path, text):
        real_write(path, text)
        _overlay(s3, {"t": entry, "late_click": {"slug": "late_click",
                 "sha256": "c" * 64, "verified": "t2", "verified_by": "b@x"}})

    monkeypatch.setattr(hv, "write_text", write_and_click)
    out = fold_verification_overlay(
        tmp_path, data_domain=DOMAIN, dataset=DATASET, s3=s3, bucket=BUCKET
    )
    assert out["folded"] == 1
    # The folded entry is gone; the mid-fold click SURVIVES.
    assert _read_overlay(s3) == {
        "late_click": {"slug": "late_click", "sha256": "c" * 64,
                       "verified": "t2", "verified_by": "b@x"}
    }
