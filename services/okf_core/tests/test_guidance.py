"""okf_core.guidance — the pure dataset-guidance invariants (normalize + dirty)."""

from __future__ import annotations

from okf_core import guidance as gd


def test_normalize_trims_and_caps():
    assert gd.normalize("  hi  ") == "hi"
    assert gd.normalize("") == ""
    assert gd.normalize(None) == ""
    assert gd.normalize("   ") == ""
    long = "x" * (gd.MAX_LEN + 500)
    assert len(gd.normalize(long)) == gd.MAX_LEN


def test_is_dirty_empty_guidance_never_dirty():
    # Clearing guidance is not a reason to re-harvest.
    assert gd.is_dirty("", "t2", "t1") is False
    assert gd.is_dirty(None, "t2", "") is False
    assert gd.is_dirty("   ", "t2", "t1") is False


def test_is_dirty_never_applied():
    # Non-empty guidance with no applied version → dirty (never harvested).
    assert gd.is_dirty("focus on races", "t1", "") is True
    assert gd.is_dirty("focus on races", "t1", None) is True


def test_is_dirty_edited_since_apply():
    # Applied version lags the current updated_at → edited since → dirty.
    assert gd.is_dirty("focus on races", "t2", "t1") is True


def test_not_dirty_when_applied_matches_current():
    # The live version has been harvested → clean.
    assert gd.is_dirty("focus on races", "t1", "t1") is False
