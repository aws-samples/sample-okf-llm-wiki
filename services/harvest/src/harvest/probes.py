"""Deterministic verification probes — the SQL cores, callable without an agent.

Two callers share these (which is the point — their numbers must be
bit-identical):

* the ``check_grain`` / ``validate_join`` TOOLS (``source_tools.py``) — the
  live probes an agent runs on candidates the precompute didn't cover;
* the snapshot-time relationship precompute (``relationships.py``) — a plain
  loop that runs the same probes over mechanically-enumerated candidates and
  writes the evidence into ``.metadata/`` before the agent exists.

Every function takes an already-RESOLVED quoted table reference (the callers
own concept-id resolution) and returns a plain dict; engine failures come back
as a ``note`` — never an exception — because both callers treat a failed probe
as a recordable outcome, not a crash.
"""

from __future__ import annotations

from typing import Any


def quote_ident(name: str) -> str:
    """Double-quote an identifier, stripping embedded quotes (hygiene, not a
    security boundary — the same agent holds free-form ``run_sql`` anyway)."""
    return '"' + str(name).replace('"', "").strip() + '"'


def grain_stats(source: Any, ref_sql: str, key_columns: list[str]) -> dict[str, Any]:
    """Is ``key_columns`` unique in ``ref_sql``? One aggregate (+ a duplicate
    sample only when duplicates exist).

    Returns {``is_unique``, ``total_rows``, ``distinct_keys``,
    ``duplicate_key_groups``, ``max_rows_per_key``, ``sample_duplicates``,
    ``note``}; ``is_unique`` is None when the probe itself failed.
    """
    if not key_columns:
        return {"is_unique": None, "note": "key_columns is empty"}
    keys = ", ".join(quote_ident(c) for c in key_columns)
    try:
        agg = source.run_query(
            f"SELECT COUNT(*) AS key_groups, SUM(c) AS total_rows, "
            f"SUM(CASE WHEN c > 1 THEN 1 ELSE 0 END) AS dupe_groups, "
            f"MAX(c) AS max_per_key FROM ("
            f"SELECT COUNT(*) AS c FROM {ref_sql} GROUP BY {keys}) g"
        )
    except Exception as e:  # noqa: BLE001 — a failed probe is an outcome
        return {"is_unique": None, "note": f"Query failed: {e}"}
    row = agg[0] if agg else {}
    dupes = int(row.get("dupe_groups") or 0)
    out: dict[str, Any] = {
        "is_unique": dupes == 0,
        "total_rows": int(row.get("total_rows") or 0),
        "distinct_keys": int(row.get("key_groups") or 0),
        "duplicate_key_groups": dupes,
        "max_rows_per_key": int(row.get("max_per_key") or 0),
        "sample_duplicates": [],
        "note": "",
    }
    if dupes:
        try:
            out["sample_duplicates"] = source.run_query(
                f"SELECT {keys}, COUNT(*) AS n FROM {ref_sql} "
                f"GROUP BY {keys} HAVING COUNT(*) > 1 "
                f"ORDER BY COUNT(*) DESC LIMIT 5"
            )
        except Exception as e:  # noqa: BLE001
            out["note"] = f"Duplicate sample failed: {e}"
    return out


def _side_agg(source: Any, ref_sql: str, cols: list[str]) -> dict[str, Any]:
    """One side's own aggregate: rows / null keys / distinct keys."""
    quoted = [quote_ident(c) for c in cols]
    any_null = " OR ".join(f"{c} IS NULL" for c in quoted)
    sel = ", ".join(quoted)
    agg = source.run_query(
        f"SELECT COUNT(*) AS n, "
        f"SUM(CASE WHEN {any_null} THEN 1 ELSE 0 END) AS null_keys, "
        f"(SELECT COUNT(*) FROM (SELECT DISTINCT {sel} "
        f"FROM {ref_sql} WHERE NOT ({any_null})) d) AS distinct_keys "
        f"FROM {ref_sql}"
    )
    row = agg[0] if agg else {}
    n = int(row.get("n") or 0)
    nulls = int(row.get("null_keys") or 0)
    distinct = int(row.get("distinct_keys") or 0)
    eligible = n - nulls
    return {
        "rows": n,
        "null_key_rows": nulls,
        "distinct_keys": distinct,
        "_eligible": eligible,
        "_unique": distinct == eligible and eligible > 0,
    }


def _matched_count(
    source: Any,
    ref_sql: str,
    cols: list[str],
    other_ref: str,
    other_cols: list[str],
) -> int:
    quoted = [quote_ident(c) for c in cols]
    o_quoted = [quote_ident(c) for c in other_cols]
    not_null = " AND ".join(f"t.{c} IS NOT NULL" for c in quoted)
    on = " AND ".join(f"o.{oc} = t.{c}" for c, oc in zip(quoted, o_quoted))
    matched = source.run_query(
        f"SELECT COUNT(*) AS m FROM {ref_sql} t WHERE {not_null} "
        f"AND EXISTS (SELECT 1 FROM {other_ref} o WHERE {on})"
    )
    return int((matched[0] if matched else {}).get("m") or 0)


def _side_stats(
    source: Any,
    ref_sql: str,
    cols: list[str],
    other_ref: str,
    other_cols: list[str],
) -> dict[str, Any]:
    """One side of a join probe: rows / null keys / distinct keys / match rate."""
    side = _side_agg(source, ref_sql, cols)
    m = _matched_count(source, ref_sql, cols, other_ref, other_cols)
    eligible = side.pop("_eligible")
    side["matched_rows"] = m
    side["match_rate"] = round(m / eligible, 4) if eligible else None
    return side


def join_stats(
    source: Any,
    left_ref: str,
    left_columns: list[str],
    right_ref: str,
    right_columns: list[str],
) -> dict[str, Any]:
    """Verify one candidate join: match rate BOTH ways + cardinality class.

    Returns {``left``, ``right`` (each {rows, null_key_rows, distinct_keys,
    matched_rows, match_rate}), ``cardinality`` (1:1/1:N/N:1/M:N), ``note``} —
    or {``note``: ...} alone when a query failed. Four aggregate scans total.
    """
    if not left_columns or len(left_columns) != len(right_columns):
        return {
            "note": "left_columns/right_columns must be non-empty and the "
            "same length"
        }
    try:
        l_stats = _side_stats(source, left_ref, left_columns, right_ref, right_columns)
        r_stats = _side_stats(source, right_ref, right_columns, left_ref, left_columns)
    except Exception as e:  # noqa: BLE001 — a failed probe is an outcome
        return {"note": f"Query failed: {e}"}
    l_unique = l_stats.pop("_unique")
    r_unique = r_stats.pop("_unique")
    card = (
        "1:1" if l_unique and r_unique
        else "1:N" if l_unique
        else "N:1" if r_unique
        else "M:N"
    )
    return {"left": l_stats, "right": r_stats, "cardinality": card, "note": ""}


def sampled_join_stats(
    source: Any,
    sampled_ref: str,
    sampled_cols: list[str],
    full_ref: str,
    full_cols: list[str],
) -> dict[str, Any]:
    """The one-direction join probe for a pair whose big side must be sampled.

    Statistics survive sampling in exactly ONE direction: a uniform sample of
    the big side probed against the FULL small side yields an unbiased match
    rate (each sampled row had equal draw probability), while the reverse —
    the full small side against a sampled big side — collapses toward the
    sample fraction on a perfect join. So this measures ONLY sampled→full,
    plus the full side's own aggregate (its key uniqueness is exact — it was
    never sampled). The sampled side's distinct count describes the SAMPLE
    and its uniqueness is unknowable (a clean sample proves nothing).

    Every sampled-side number comes from ONE query — one TABLESAMPLE draw.
    Each reference to a sampled relation draws an INDEPENDENT sample, so
    splitting rows/eligible/matched across queries would divide numbers from
    different samples (match rates over 100% on a perfect join). The matched
    count therefore rides a LEFT JOIN against the full side's distinct keys
    inside the same scan instead of a separate EXISTS probe.

    Returns {``sampled``, ``full`` (side dicts), ``full_unique``, ``note``}.
    """
    if not sampled_cols or len(sampled_cols) != len(full_cols):
        return {
            "note": "sampled_cols/full_cols must be non-empty and the "
            "same length"
        }
    quoted = [quote_ident(c) for c in sampled_cols]
    o_quoted = [quote_ident(c) for c in full_cols]
    any_null = " OR ".join(f"t.{c} IS NULL" for c in quoted)
    on = " AND ".join(f"o.{oc} = t.{c}" for c, oc in zip(quoted, o_quoted))
    key_expr = (
        f"t.{quoted[0]}"
        if len(quoted) == 1
        else "ROW(" + ", ".join(f"t.{c}" for c in quoted) + ")"
    )
    try:
        agg = source.run_query(
            f"SELECT COUNT(*) AS n, "
            f"SUM(CASE WHEN {any_null} THEN 1 ELSE 0 END) AS null_keys, "
            f"COUNT(DISTINCT CASE WHEN NOT ({any_null}) THEN {key_expr} END)"
            f" AS distinct_keys, "
            f"SUM(CASE WHEN NOT ({any_null}) AND o.{o_quoted[0]} IS NOT NULL "
            f"THEN 1 ELSE 0 END) AS m "
            f"FROM {sampled_ref} t "
            f"LEFT JOIN (SELECT DISTINCT {', '.join(o_quoted)} "
            f"FROM {full_ref}) o ON {on}"
        )
        full = _side_agg(source, full_ref, full_cols)
    except Exception as e:  # noqa: BLE001 — a failed probe is an outcome
        return {"note": f"Query failed: {e}"}
    row = agg[0] if agg else {}
    n = int(row.get("n") or 0)
    nulls = int(row.get("null_keys") or 0)
    m = int(row.get("m") or 0)
    eligible = n - nulls
    side = {
        "rows": n,
        "null_key_rows": nulls,
        "distinct_keys": int(row.get("distinct_keys") or 0),
        "matched_rows": m,
        "match_rate": round(m / eligible, 4) if eligible else None,
    }
    full.pop("_eligible")
    full_unique = full.pop("_unique")
    return {
        "sampled": side,
        "full": full,
        "full_unique": full_unique,
        "note": "",
    }


def sample_orphans(
    source: Any,
    ref_sql: str,
    cols: list[str],
    other_ref: str,
    other_cols: list[str],
    n: int = 5,
) -> list[dict[str, Any]]:
    """A few rows on ``ref_sql``'s side whose key has NO match on the other side.

    The interpretable half of an orphan analysis: the precompute puts these in
    the evidence sheet so an author can explain WHAT the orphans are ("future
    races, no results yet" → left-join advice) without another engine call.
    Empty list on failure (the match-rate numbers already carry the finding).

    Fine to call with a SAMPLED ref even though it draws a fresh sample: a
    returned row provably has no match on the (full) other side, so it is a
    genuine orphan of the underlying table no matter which draw surfaced it.
    Only ratios must share a draw; examples don't.
    """
    quoted = [quote_ident(c) for c in cols]
    o_quoted = [quote_ident(c) for c in other_cols]
    not_null = " AND ".join(f"t.{c} IS NOT NULL" for c in quoted)
    on = " AND ".join(f"o.{oc} = t.{c}" for c, oc in zip(quoted, o_quoted))
    try:
        return source.run_query(
            f"SELECT * FROM {ref_sql} t WHERE {not_null} "
            f"AND NOT EXISTS (SELECT 1 FROM {other_ref} o WHERE {on}) "
            f"LIMIT {int(n)}"
        )
    except Exception:  # noqa: BLE001 — the sample is a bonus, never a blocker
        return []
