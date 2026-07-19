"""LangChain tools exposing LIVE Athena access to the harvest agent.

Static Glue metadata (schemas, Hive types, partitions, storage location, ARNs)
is no longer fetched through per-call tools. It is snapshotted ONCE to the
read-only ``.metadata/`` directory at harvest start (see ``metadata_export``),
and the agent explores it with the built-in filesystem tools
(``read_file``/``glob``/``grep``) — a single ``grep`` across ``.metadata/tables/``
answers "which tables have column X?", the core move for join and near-synonym
discovery, which the old one-concept-at-a-time ``read_concept_raw`` could not.

What remains here is the LIVE half a snapshot cannot capture: ``sample_rows`` (a
small Athena sample of real values) and ``run_sql`` (execute a query to VERIFY
grain, joins, casts, and gotchas against real data — a failing query is itself
signal). Tools close over one source instance per session.
"""

from __future__ import annotations

from typing import Any

from harvest.glue_source import GlueAthenaSource
from okf_core.paths import parse_concept_id


def make_source_tools(source: GlueAthenaSource) -> list[Any]:
    from langchain_core.tools import tool

    @tool
    def sample_rows(concept_id: str, n: int = 5) -> dict[str, Any]:
        """Pull a small sample of rows from a table via Athena.

        Returns {`rows`: [ {col: value, ...}, ... ], `note`: str}. `rows` is
        empty (with an explanatory `note`) if sampling is unsupported or fails.
        Use this to see real values, confirm grain, and spot obfuscation or
        type surprises the `.metadata/` snapshot doesn't reveal. `concept_id` is
        the slash-joined id (e.g. `tables/races`), matching the snapshot layout.

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
            return {"rows": [], "note": f"Invalid concept id {concept_id!r}: {e}"}
        ref = source.find(parsed)
        if ref is None:
            return {"rows": [], "note": f"Unknown concept: {concept_id}"}
        try:
            rows = source.sample_rows(ref, n=n)
        except Exception as e:  # noqa: BLE001
            return {"rows": [], "note": f"Sampling failed: {e}"}
        if rows is None:
            return {"rows": [], "note": "Sampling is not supported for this concept."}
        return {"rows": rows, "note": ""}

    @tool
    def run_sql(query: str) -> dict[str, Any]:
        """Execute a read-only Athena SQL query against this dataset and return
        the rows.

        Use this to VALIDATE the query patterns you put in `# Common query
        patterns` and to confirm join keys, type casts, and known gotchas
        actually work — the `.metadata/` snapshot is catalog metadata, which is
        not always trustworthy. Returns {`rows`: [...], `note`: str}. On failure
        `rows` is empty and `note` carries the Athena error (a failing query is
        itself signal worth documenting as a known issue).

        A SQL NULL cell is `null` (Python None), distinct from an empty string
        `""`. Use `IS NULL` / `IS NOT NULL` for missing values; `= ''` / `<> ''`
        only match genuinely empty strings.
        """
        try:
            rows = source.run_query(query)
        except Exception as e:  # noqa: BLE001
            return {"rows": [], "note": f"Query failed: {e}"}
        return {"rows": rows, "note": ""}

    return [sample_rows, run_sql]
