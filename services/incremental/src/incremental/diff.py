"""Column-level schema diffing between two Glue table versions.

When a Glue table changes we want the harvest agent to re-review only what
actually moved, not re-crawl the whole dataset. We diff the two latest table
versions' column lists into three buckets:

- ``added``   — columns present in the new version but not the old
- ``removed`` — columns present in the old version but not the new
- ``retyped`` — columns present in both whose Hive type changed

The diff is intentionally shape-simple (plain dicts / lists of dicts) because it
is serialized to ``.harvest/pending.json`` and into the AgentCore invoke payload
(see docs/CONVENTIONS.md — incremental payload ``diff`` field).
"""

from __future__ import annotations

from typing import Any


def _columns_by_name(columns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index a Glue ``Columns[]`` list by column name.

    Glue column dicts are ``{Name, Type, Comment}`` (Hive type string). We drop
    columns with no ``Name`` (malformed) so a bad entry can't shadow a real one.
    """
    out: dict[str, dict[str, Any]] = {}
    for col in columns or []:
        name = col.get("Name")
        if name:
            out[name] = col
    return out


def _col_summary(col: dict[str, Any]) -> dict[str, Any]:
    """A stable, JSON-serializable summary of one column for the diff payload."""
    return {
        "name": col.get("Name"),
        "type": col.get("Type"),
        "comment": col.get("Comment"),
    }


def compute_column_diff(
    old_cols: list[dict[str, Any]] | None,
    new_cols: list[dict[str, Any]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Diff two Glue ``Columns[]`` lists into added / removed / retyped buckets.

    ``old_cols`` / ``new_cols`` are Glue column dicts (``{Name, Type, Comment}``).
    Returns ``{"added": [...], "removed": [...], "retyped": [...]}`` where each
    entry is a column summary dict. ``retyped`` entries additionally carry
    ``old_type`` and ``new_type`` so the agent sees the transition.

    Type comparison is case-insensitive on the Hive type string (Glue may
    normalize casing between versions; a pure casing change is not a real
    retype).
    """
    old_by_name = _columns_by_name(old_cols or [])
    new_by_name = _columns_by_name(new_cols or [])

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    retyped: list[dict[str, Any]] = []

    for name, new_col in new_by_name.items():
        if name not in old_by_name:
            added.append(_col_summary(new_col))
            continue
        old_col = old_by_name[name]
        old_type = (old_col.get("Type") or "").strip().lower()
        new_type = (new_col.get("Type") or "").strip().lower()
        if old_type != new_type:
            entry = _col_summary(new_col)
            entry["old_type"] = old_col.get("Type")
            entry["new_type"] = new_col.get("Type")
            retyped.append(entry)

    for name, old_col in old_by_name.items():
        if name not in new_by_name:
            removed.append(_col_summary(old_col))

    return {"added": added, "removed": removed, "retyped": retyped}
