"""LangChain tools exposing LIVE query access to the harvest agent.

Static catalog metadata is snapshotted ONCE to the read-only ``.metadata/``
directory at harvest start (see ``metadata_export`` — including the per-table
column profiles under ``.metadata/profile/``), and the agent explores it with
the built-in filesystem tools. What lives here is the LIVE half a snapshot
cannot capture:

* ``sample_rows`` / ``run_sql`` — see real values, verify hypotheses. Results
  are COMPACT (one ``columns`` list + positional ``rows``, not repeated
  per-row dicts), row-capped (``OKF_HARVEST_SQL_MAX_ROWS``, soft — the tool
  reports ``truncated: true``), and carry the engine's execution stats where
  available, so the agent can document expensive query patterns.
* ``check_grain`` / ``validate_join`` — the two verification probes the
  authoring methodology mandates for every table/join doc, as ONE deterministic
  call each instead of a hand-written query series. Registered only when the
  source provides the ``sql_table_ref`` capability.
* ``explain_sql`` — engine-side validation of a query WITHOUT scanning data
  (Athena/Redshift ``EXPLAIN``). Registered only when the source advertises
  ``supports_explain``; use it on every ``sql`` fence a doc ships.

Tools close over one source instance per session. All identifiers interpolated
into SQL are double-quoted with embedded quotes stripped — hygiene, not a
security boundary: the same agent holds free-form ``run_sql`` anyway, and the
engine session is read-only (see clients.py / DATA_SOURCES.md).
"""

from __future__ import annotations

import os
from typing import Any

from harvest.source_base import Source
from okf_core.paths import parse_concept_id

_NULL_NOTE = (
    "A SQL NULL cell is null (Python None); an empty string is \"\". These are "
    "DIFFERENT — document NULL/IS NULL for missing values and reserve = '' / "
    "<> '' for genuinely empty strings."
)


def _max_rows() -> int:
    try:
        return int(os.environ.get("OKF_HARVEST_SQL_MAX_ROWS", "") or 200)
    except ValueError:
        return 200


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', "").strip() + '"'


def _compact(
    columns: list[str],
    rows: list[list[Any]],
    *,
    truncated: bool = False,
    stats: dict[str, Any] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """The tool result shape: columnar (header once), never per-row dicts."""
    out: dict[str, Any] = {
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "note": note,
    }
    clean_stats = {k: v for k, v in (stats or {}).items()
                   if k != "truncated" and v is not None}
    if clean_stats:
        out["stats"] = clean_stats
    return out


def _fail(note: str) -> dict[str, Any]:
    return {"columns": [], "rows": [], "truncated": False, "note": note}


def make_source_tools(source: Source) -> list[Any]:
    from langchain_core.tools import tool

    table_ref = getattr(source, "sql_table_ref", None)

    def _resolve_table(concept_id: str) -> tuple[str, str] | str:
        """(table_token, quoted_ref) for a tables/<t> concept id, or an error note."""
        try:
            parsed = parse_concept_id(concept_id)
        except ValueError as e:
            return f"Invalid concept id {concept_id!r}: {e}"
        ref = source.find(parsed)
        if ref is None:
            return f"Unknown concept: {concept_id}"
        token = ref.id[-1]
        return token, table_ref(token)  # type: ignore[misc]

    @tool
    def sample_rows(concept_id: str, n: int = 5) -> dict[str, Any]:
        """Pull a small sample of rows from a table via the live query engine.

        Returns {`columns`: [...], `rows`: [[...], ...], `truncated`, `note`} —
        `rows` are positional (aligned with `columns`). Use this to see real
        values, confirm grain, and spot obfuscation or type surprises the
        `.metadata/` snapshot doesn't reveal (check the table's
        `.metadata/profile/<table>.md` FIRST — it already answers most
        null/enum/range questions). `concept_id` is the slash-joined id (e.g.
        `tables/races`), matching the snapshot layout.

        A SQL NULL cell is `null` (Python None); an empty string is `""`. These
        are DIFFERENT — document `NULL`/`IS NULL` for missing values and reserve
        `= ''` / `<> ''` for genuinely empty strings.
        """
        # A malformed id (e.g. a `.metadata/...` snapshot path, or any segment the
        # concept-id grammar rejects) is recoverable model input, not a crash:
        # return a note so the agent self-corrects, mirroring run_sql below.
        try:
            parsed = parse_concept_id(concept_id)
        except ValueError as e:
            return _fail(f"Invalid concept id {concept_id!r}: {e}")
        ref = source.find(parsed)
        if ref is None:
            return _fail(f"Unknown concept: {concept_id}")
        try:
            rows = source.sample_rows(ref, n=n)
        except Exception as e:  # noqa: BLE001
            return _fail(f"Sampling failed: {e}")
        if rows is None:
            return _fail("Sampling is not supported for this concept.")
        columns = list(rows[0].keys()) if rows else []
        return _compact(columns, [[r.get(c) for c in columns] for r in rows])

    @tool
    def run_sql(query: str) -> dict[str, Any]:
        """Execute a read-only SQL query against this dataset and return rows.

        Use this to VALIDATE the query patterns you put in `# Common query
        patterns` and to confirm join keys, type casts, and known gotchas
        actually work — the `.metadata/` snapshot is catalog metadata, which is
        not always trustworthy. Returns {`columns`, `rows` (positional),
        `truncated`, `stats`, `note`}. On failure `columns`/`rows` are empty and
        `note` carries the engine error (a failing query is itself signal worth
        documenting as a known issue).

        `truncated: true` means the result was cut at the row cap — add a
        LIMIT or aggregate instead of re-running wide-open queries. When
        `stats` is present, `data_scanned_bytes` is what the query billed:
        if a pattern scans heavily, document the cheaper form (e.g. "always
        filter on the partition column") in the doc's query guidance.

        A SQL NULL cell is `null` (Python None), distinct from an empty string
        `""`. Use `IS NULL` / `IS NOT NULL` for missing values; `= ''` / `<> ''`
        only match genuinely empty strings.
        """
        stats: dict[str, Any] = {}
        try:
            header, rows = source.run_query(
                query, positional=True, stats=stats, truncate_at=_max_rows()
            )
        except Exception as e:  # noqa: BLE001
            return _fail(f"Query failed: {e}")
        return _compact(
            header, rows, truncated=bool(stats.get("truncated")), stats=stats
        )

    tools: list[Any] = [sample_rows, run_sql]

    if callable(table_ref):

        @tool
        def check_grain(concept_id: str, key_columns: list[str]) -> dict[str, Any]:
            """Verify a claimed grain: is `key_columns` unique in this table?

            ONE call replaces the hand-written duplicate-count probe. Returns
            {`is_unique`, `total_rows`, `distinct_keys`, `duplicate_key_groups`,
            `max_rows_per_key`, `sample_duplicates`, `note`}. `sample_duplicates`
            (top 5 by count) is only fetched when duplicates exist. State the
            verified grain in the doc ("one row per race"), NOT the counts you
            saw. NULLs in key columns group together — check the profile sheet's
            null share if a key column is nullable.
            """
            resolved = _resolve_table(concept_id)
            if isinstance(resolved, str):
                return {"is_unique": None, "note": resolved}
            _, ref_sql = resolved
            if not key_columns:
                return {"is_unique": None, "note": "key_columns is empty"}
            keys = ", ".join(_quote_ident(c) for c in key_columns)
            try:
                agg = source.run_query(
                    f"SELECT COUNT(*) AS key_groups, SUM(c) AS total_rows, "
                    f"SUM(CASE WHEN c > 1 THEN 1 ELSE 0 END) AS dupe_groups, "
                    f"MAX(c) AS max_per_key FROM ("
                    f"SELECT COUNT(*) AS c FROM {ref_sql} GROUP BY {keys}) g"
                )
            except Exception as e:  # noqa: BLE001
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
                    sample = source.run_query(
                        f"SELECT {keys}, COUNT(*) AS n FROM {ref_sql} "
                        f"GROUP BY {keys} HAVING COUNT(*) > 1 "
                        f"ORDER BY COUNT(*) DESC LIMIT 5"
                    )
                    out["sample_duplicates"] = sample
                except Exception as e:  # noqa: BLE001
                    out["note"] = f"Duplicate sample failed: {e}"
            return out

        @tool
        def validate_join(
            left_concept_id: str,
            left_columns: list[str],
            right_concept_id: str,
            right_columns: list[str],
        ) -> dict[str, Any]:
            """Verify a candidate join: key match rate BOTH ways + cardinality.

            ONE call replaces the hand-written fan-out probes. For each side
            returns {`rows`, `null_key_rows`, `distinct_keys`, `matched_rows`,
            `match_rate`} (match_rate excludes null-key rows), plus
            `cardinality` (`1:1`/`1:N`/`N:1`/`M:N` — a side is "1" when its
            key is unique over its non-null rows) and `note`. Put the evidence
            in the join doc ("99.2% of results.race_id resolve; 1:N ≈ 20").
            Costs a few aggregate scans per side — use it on CANDIDATE joins
            surfaced by the `.metadata/columns.tsv` grep, not exhaustively.
            """
            if not left_columns or len(left_columns) != len(right_columns):
                return {
                    "note": "left_columns/right_columns must be non-empty and "
                    "the same length"
                }
            left = _resolve_table(left_concept_id)
            right = _resolve_table(right_concept_id)
            if isinstance(left, str):
                return {"note": left}
            if isinstance(right, str):
                return {"note": right}

            def side_stats(
                ref_sql: str, cols: list[str], other_ref: str, other_cols: list[str]
            ) -> dict[str, Any]:
                quoted = [_quote_ident(c) for c in cols]
                o_quoted = [_quote_ident(c) for c in other_cols]
                not_null = " AND ".join(f"t.{c} IS NOT NULL" for c in quoted)
                any_null = " OR ".join(f"{c} IS NULL" for c in quoted)
                sel = ", ".join(quoted)
                on = " AND ".join(f"o.{oc} = t.{c}"
                                  for c, oc in zip(quoted, o_quoted))
                agg = source.run_query(
                    f"SELECT COUNT(*) AS n, "
                    f"SUM(CASE WHEN {any_null} THEN 1 ELSE 0 END) AS null_keys, "
                    f"(SELECT COUNT(*) FROM (SELECT DISTINCT {sel} "
                    f"FROM {ref_sql} WHERE NOT ({any_null})) d) AS distinct_keys "
                    f"FROM {ref_sql}"
                )
                matched = source.run_query(
                    f"SELECT COUNT(*) AS m FROM {ref_sql} t WHERE {not_null} "
                    f"AND EXISTS (SELECT 1 FROM {other_ref} o WHERE {on})"
                )
                row = agg[0] if agg else {}
                n = int(row.get("n") or 0)
                nulls = int(row.get("null_keys") or 0)
                m = int((matched[0] if matched else {}).get("m") or 0)
                eligible = n - nulls
                return {
                    "rows": n,
                    "null_key_rows": nulls,
                    "distinct_keys": int(row.get("distinct_keys") or 0),
                    "matched_rows": m,
                    "match_rate": round(m / eligible, 4) if eligible else None,
                    "_unique": int(row.get("distinct_keys") or 0) == eligible
                    and eligible > 0,
                }

            try:
                l_stats = side_stats(
                    left[1], left_columns, right[1], right_columns
                )
                r_stats = side_stats(
                    right[1], right_columns, left[1], left_columns
                )
            except Exception as e:  # noqa: BLE001
                return {"note": f"Query failed: {e}"}
            l_unique = l_stats.pop("_unique")
            r_unique = r_stats.pop("_unique")
            card = (
                "1:1" if l_unique and r_unique
                else "1:N" if l_unique
                else "N:1" if r_unique
                else "M:N"
            )
            return {
                "left": l_stats,
                "right": r_stats,
                "cardinality": card,
                "note": "",
            }

        tools += [check_grain, validate_join]

    if getattr(source, "supports_explain", False):

        @tool
        def explain_sql(query: str) -> dict[str, Any]:
            """Validate a query via the engine's EXPLAIN — no data is scanned.

            Returns {`valid`, `plan`, `note`}. A failing EXPLAIN means the query
            is INVALID against the live schema (`note` carries the engine
            error) — run every ```sql fence you put in a doc through this
            before shipping it; a doc with a broken example is worse than no
            example. EXPLAIN validates syntax and name resolution; it does NOT
            prove the query returns what the prose claims — use `run_sql` for
            semantic verification.
            """
            try:
                rows = source.run_query(f"EXPLAIN {query}")
            except Exception as e:  # noqa: BLE001
                return {"valid": False, "plan": "", "note": f"EXPLAIN failed: {e}"}
            plan = "\n".join(
                str(next(iter(r.values()), "")) for r in rows if r
            )
            return {"valid": True, "plan": plan, "note": ""}

        tools.append(explain_sql)

    return tools
