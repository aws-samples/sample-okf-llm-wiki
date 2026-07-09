"""Unit tests for compute_column_diff."""

from __future__ import annotations

from incremental.diff import compute_column_diff
from fakes import col


def test_added_removed_retyped():
    old = [col("id", "bigint"), col("name", "string"), col("legacy", "int")]
    new = [col("id", "bigint"), col("name", "varchar"), col("email", "string")]
    diff = compute_column_diff(old, new)

    assert [c["name"] for c in diff["added"]] == ["email"]
    assert [c["name"] for c in diff["removed"]] == ["legacy"]
    assert len(diff["retyped"]) == 1
    retyped = diff["retyped"][0]
    assert retyped["name"] == "name"
    assert retyped["old_type"] == "string"
    assert retyped["new_type"] == "varchar"


def test_no_change_is_empty_diff():
    cols = [col("id", "bigint"), col("name", "string")]
    diff = compute_column_diff(cols, list(cols))
    assert diff == {"added": [], "removed": [], "retyped": []}


def test_case_only_type_change_is_not_a_retype():
    old = [col("id", "BIGINT")]
    new = [col("id", "bigint")]
    diff = compute_column_diff(old, new)
    assert diff["retyped"] == []


def test_none_old_means_everything_added():
    new = [col("id", "bigint"), col("name", "string")]
    diff = compute_column_diff(None, new)
    assert [c["name"] for c in diff["added"]] == ["id", "name"]
    assert diff["removed"] == []
    assert diff["retyped"] == []


def test_columns_without_name_are_ignored():
    old = [{"Type": "int"}, col("id", "bigint")]
    new = [col("id", "bigint")]
    diff = compute_column_diff(old, new)
    assert diff == {"added": [], "removed": [], "retyped": []}
