"""The deterministic verification tools: check_grain / validate_join /
explain_sql, plus the run_sql cap+stats contract.

These test at the Source-fake level (the tools only touch find/sample_rows/
run_query and the SQL capability atoms), with a predicate-dispatching fake so
each canned result is matched by what the SQL *does*, not by byte-exact query
strings.
"""

from __future__ import annotations

import pytest

from harvest import source_tools as st
from harvest.source_base import ConceptRef


class _ProbeSource:
    """Fake source: known tables + predicate-keyed run_query results."""

    supports_explain = True

    def __init__(self, responders):
        # responders: ordered list of (predicate(sql) -> bool, result-or-raiser)
        self.responders = list(responders)
        self.queries: list[str] = []

    def sql_table_ref(self, table: str) -> str:
        return f'"db"."{table}"'

    def find(self, concept_id):
        if concept_id[0] != "tables":
            return None
        if concept_id[1] not in ("races", "results"):
            return None
        return ConceptRef(id=concept_id, type="Glue Table")

    def sample_rows(self, ref, n=5):
        return [{"a": "1", "b": None}]

    def run_query(self, query, **kwargs):
        self.queries.append(query)
        for predicate, result in self.responders:
            if predicate(query):
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"no responder for: {query}")


def _tools(responders):
    src = _ProbeSource(responders)
    return src, {t.name: t for t in st.make_source_tools(src)}


# -- check_grain ---------------------------------------------------------------


def test_check_grain_unique_key_needs_one_query():
    src, tools = _tools(
        [(lambda q: "GROUP BY" in q, [
            {"key_groups": "100", "total_rows": "100",
             "dupe_groups": "0", "max_per_key": "1"},
        ])]
    )
    out = tools["check_grain"].invoke(
        {"concept_id": "tables/races", "key_columns": ["raceid"]}
    )
    assert out["is_unique"] is True
    assert out["total_rows"] == 100
    assert out["distinct_keys"] == 100
    assert out["sample_duplicates"] == []
    assert len(src.queries) == 1  # no duplicate-sample query when unique
    assert '"db"."races"' in src.queries[0] and '"raceid"' in src.queries[0]


def test_check_grain_duplicates_fetches_a_sample():
    src, tools = _tools([
        (lambda q: "HAVING" in q, [
            {"raceid": "7", "n": "3"}, {"raceid": "9", "n": "2"},
        ]),
        (lambda q: "GROUP BY" in q, [
            {"key_groups": "98", "total_rows": "103",
             "dupe_groups": "2", "max_per_key": "3"},
        ]),
    ])
    out = tools["check_grain"].invoke(
        {"concept_id": "tables/races", "key_columns": ["raceid"]}
    )
    assert out["is_unique"] is False
    assert out["duplicate_key_groups"] == 2
    assert out["max_rows_per_key"] == 3
    assert out["sample_duplicates"][0]["raceid"] == "7"


def test_check_grain_bad_inputs_are_notes_not_raises():
    _src, tools = _tools([])
    out = tools["check_grain"].invoke(
        {"concept_id": "tables/nope", "key_columns": ["k"]}
    )
    assert out["is_unique"] is None and "Unknown concept" in out["note"]
    out = tools["check_grain"].invoke(
        {"concept_id": "tables/races", "key_columns": []}
    )
    assert out["is_unique"] is None and "empty" in out["note"]


# -- validate_join ---------------------------------------------------------------


def _join_responders():
    # Left (races): 100 rows, 0 null keys, 100 distinct -> unique side.
    # Right (results): 1000 rows, 10 null keys, 100 distinct over 990 -> N side.
    def agg_for(table, n, nulls, distinct):
        return (
            lambda q, t=table: "DISTINCT" in q and f'"{t}"' in q
            and "EXISTS" not in q,
            [{"n": str(n), "null_keys": str(nulls), "distinct_keys": str(distinct)}],
        )

    def match_for(table, m):
        return (
            lambda q, t=table: "EXISTS" in q and q.index(f'"{t}"') < q.index("EXISTS"),
            [{"m": str(m)}],
        )

    return [
        agg_for("races", 100, 0, 100),
        agg_for("results", 1000, 10, 100),
        match_for("races", 99),
        match_for("results", 980),
    ]


def test_validate_join_reports_both_sides_and_cardinality():
    _src, tools = _tools(_join_responders())
    out = tools["validate_join"].invoke({
        "left_concept_id": "tables/races",
        "left_columns": ["raceid"],
        "right_concept_id": "tables/results",
        "right_columns": ["raceid"],
    })
    assert out["cardinality"] == "1:N"
    assert out["left"] == {
        "rows": 100, "null_key_rows": 0, "distinct_keys": 100,
        "matched_rows": 99, "match_rate": 0.99,
    }
    assert out["right"]["null_key_rows"] == 10
    # match_rate excludes null-key rows: 980 / (1000 - 10).
    assert out["right"]["match_rate"] == round(980 / 990, 4)


def test_validate_join_rejects_mismatched_key_lists():
    _src, tools = _tools([])
    out = tools["validate_join"].invoke({
        "left_concept_id": "tables/races",
        "left_columns": ["a", "b"],
        "right_concept_id": "tables/results",
        "right_columns": ["a"],
    })
    assert "same length" in out["note"]


def test_validate_join_query_failure_is_a_note():
    _src, tools = _tools([(lambda q: True, RuntimeError("boom"))])
    out = tools["validate_join"].invoke({
        "left_concept_id": "tables/races",
        "left_columns": ["raceid"],
        "right_concept_id": "tables/results",
        "right_columns": ["raceid"],
    })
    assert "Query failed" in out["note"]


# -- explain_sql ---------------------------------------------------------------


def test_explain_sql_valid_returns_plan():
    _src, tools = _tools(
        [(lambda q: q.startswith("EXPLAIN "), [
            {"Query Plan": "Fragment 0 [SINGLE]"},
            {"Query Plan": " Output[year]"},
        ])]
    )
    out = tools["explain_sql"].invoke({"query": "SELECT year FROM races"})
    assert out["valid"] is True
    assert "Fragment 0" in out["plan"] and "Output[year]" in out["plan"]


def test_explain_sql_invalid_is_a_note():
    _src, tools = _tools(
        [(lambda q: True, RuntimeError("COLUMN_NOT_FOUND: yearz"))]
    )
    out = tools["explain_sql"].invoke({"query": "SELECT yearz FROM races"})
    assert out["valid"] is False
    assert "yearz" in out["note"]


# -- run_sql cap + stats ---------------------------------------------------------


def test_run_sql_reports_truncation_and_stats(monkeypatch):
    class _CappedSource(_ProbeSource):
        def run_query(self, query, *, positional=False, stats=None,
                      truncate_at=None, **kw):
            assert truncate_at == 7  # the env knob reached the source call
            if stats is not None:
                stats["truncated"] = True
                stats["data_scanned_bytes"] = 1234
                stats["engine_ms"] = 55
            return ["a"], [["1"]] * truncate_at

    monkeypatch.setenv("OKF_HARVEST_SQL_MAX_ROWS", "7")
    src = _CappedSource([])
    tools = {t.name: t for t in st.make_source_tools(src)}
    out = tools["run_sql"].invoke({"query": "SELECT * FROM big"})
    assert out["truncated"] is True
    assert len(out["rows"]) == 7
    # The internal truncated flag is surfaced as the top-level bool, not
    # duplicated inside stats.
    assert out["stats"] == {"data_scanned_bytes": 1234, "engine_ms": 55}
