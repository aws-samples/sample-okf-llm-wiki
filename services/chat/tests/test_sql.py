"""Read-only SQL tool: the query guard, the Athena engine, and feature gating.

The guard (``is_read_only``) is the defense-in-depth check that a client string
can't turn into a write — IAM has no write grants, but a clean error beats an
opaque permission failure. The engine is driven by a fake Athena client (canned
pages) so no AWS is touched. Feature gating is proven at the factory level: the
``run_sql`` tool appears ONLY when the deploy flag AND the per-run opt-in are both
present.
"""

from __future__ import annotations

import pytest

from chat.sql import (
    KNOWN_FEATURES,
    AthenaSQL,
    is_read_only,
    make_sql_tool,
    normalize_features,
    strip_sql_comments,
)


# --- the read-only guard ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from t",
        "  SELECT a FROM \"db\".\"t\" LIMIT 5  ",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT 1;",  # single trailing semicolon tolerated
        "show tables",
        "DESCRIBE db.t",
        "EXPLAIN SELECT 1",
        "select 1 -- a trailing comment",
    ],
)
def test_read_only_accepts_single_read_statements(sql):
    assert is_read_only(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a=1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (a int)",
        "ALTER TABLE t ADD COLUMN b int",
        "MERGE INTO t ...",
        "SELECT 1; DROP TABLE t",  # second statement
        "SELECT 1; SELECT 2",  # two selects is still multi
        "",
        "   ",
        "-- just a comment",
    ],
)
def test_read_only_rejects_writes_and_multi(sql):
    assert is_read_only(sql) is False


def test_comment_smuggled_second_statement_is_rejected():
    # A comment that hides a second statement head must not fool the guard:
    # comments are stripped, THEN the embedded ; is detected.
    assert is_read_only("SELECT 1 /* ; DROP TABLE t */ ; DELETE FROM t") is False


def test_strip_sql_comments():
    assert "DROP" not in strip_sql_comments("SELECT 1 -- DROP\n")
    assert "DROP" not in strip_sql_comments("SELECT /* DROP */ 1")


# --- the Athena engine (fake client) ----------------------------------------


class _FakeAthena:
    """Canned start/poll/results. Records the QueryExecutionContext it was given."""

    def __init__(self, rows_pages, *, state="SUCCEEDED"):
        self._pages = rows_pages
        self._state = state
        self.started = []

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **kwargs):
        return {"QueryExecution": {"Status": {"State": self._state, "StateChangeReason": "boom"}}}

    def get_query_results(self, **kwargs):
        # Serve pages by NextToken; each page is already {"ResultSet":{"Rows":[…]}}.
        idx = 0 if "NextToken" not in kwargs else int(kwargs["NextToken"])
        page = self._pages[idx]
        out = {"ResultSet": {"Rows": page}}
        if idx + 1 < len(self._pages):
            out["NextToken"] = str(idx + 1)
        return out


def _row(*vals):
    # Athena Datum: a NULL cell has NO VarCharValue key; "" has VarCharValue="".
    return {"Data": [({} if v is None else {"VarCharValue": v}) for v in vals]}


def test_engine_returns_typed_rows_and_preserves_null():
    athena = _FakeAthena(
        [[_row("id", "name"), _row("1", "a"), _row("2", None)]]
    )
    eng = AthenaSQL(athena=athena, output_location="s3://x/", workgroup="wg")
    out = eng.run('SELECT id, name FROM "db"."t"', default_database="db")
    assert out["columns"] == ["id", "name"]
    assert out["rows"] == [
        {"id": "1", "name": "a"},
        {"id": "2", "name": None},  # NULL preserved (not "")
    ]
    assert out["row_count"] == 2
    assert out["truncated"] is False
    # the default database + workgroup + output flowed into the Athena call
    started = athena.started[0]
    assert started["QueryExecutionContext"]["Database"] == "db"
    assert started["QueryExecutionContext"]["Catalog"] == "AwsDataCatalog"
    assert started["WorkGroup"] == "wg"
    assert started["ResultConfiguration"]["OutputLocation"] == "s3://x/"


def test_engine_truncates_at_max_rows():
    header = _row("n")
    body = [_row(str(i)) for i in range(10)]
    athena = _FakeAthena([[header, *body]])
    eng = AthenaSQL(athena=athena, max_rows=3)
    out = eng.run("SELECT n FROM t")
    assert out["row_count"] == 3
    assert out["truncated"] is True


def test_engine_paginates_result_pages():
    athena = _FakeAthena(
        [
            [_row("n"), _row("1")],  # page 0: header + 1 row
            [_row("2"), _row("3")],  # page 1: 2 rows
        ]
    )
    eng = AthenaSQL(athena=athena)
    out = eng.run("SELECT n FROM t")
    assert [r["n"] for r in out["rows"]] == ["1", "2", "3"]


def test_engine_rejects_non_read_query_before_calling_athena():
    athena = _FakeAthena([])
    eng = AthenaSQL(athena=athena)
    with pytest.raises(ValueError):
        eng.run("DELETE FROM t")
    assert athena.started == []  # never reached Athena


def test_engine_raises_on_failed_state():
    athena = _FakeAthena([[_row("n")]], state="FAILED")
    eng = AthenaSQL(athena=athena)
    with pytest.raises(RuntimeError):
        eng.run("SELECT 1")


# --- the LangChain tool wrapper ---------------------------------------------


def test_make_sql_tool_uses_scope_dataset_as_default_db():
    athena = _FakeAthena([[_row("n"), _row("1")]])
    eng = AthenaSQL(athena=athena)
    tool = make_sql_tool(eng, dataset_scope={"data_domain": "sales", "dataset": "orders"})
    assert tool.name == "run_sql"
    tool.invoke({"sql": "SELECT n FROM t"})
    assert athena.started[0]["QueryExecutionContext"]["Database"] == "orders"


def test_make_sql_tool_prefers_scope_glue_database_over_dataset_id():
    # The Glue DB name can differ from the dataset id; when the scope carries the
    # resolved glue_database it MUST win as the default DB (else an unqualified
    # query resolves against a non-existent database).
    athena = _FakeAthena([[_row("n"), _row("1")]])
    eng = AthenaSQL(athena=athena)
    tool = make_sql_tool(
        eng,
        dataset_scope={
            "data_domain": "sales",
            "dataset": "orders",
            "glue_database": "sales_prod_orders",
        },
    )
    tool.invoke({"sql": "SELECT n FROM t"})
    assert athena.started[0]["QueryExecutionContext"]["Database"] == "sales_prod_orders"


def test_make_sql_tool_no_scope_has_no_default_db():
    athena = _FakeAthena([[_row("n"), _row("1")]])
    eng = AthenaSQL(athena=athena)
    tool = make_sql_tool(eng)
    tool.invoke({"sql": "SELECT n FROM t"})
    assert "Database" not in athena.started[0]["QueryExecutionContext"]


# --- feature normalization --------------------------------------------------


def test_normalize_features_keeps_known_drops_unknown():
    assert normalize_features(["sql", "canvas", "browser"]) == {"sql"}
    assert normalize_features(["sql"]) == {"sql"}
    assert normalize_features([]) == set()
    assert normalize_features(None) == set()
    assert normalize_features("sql") == set()  # must be a list, not a bare string
    assert "sql" in KNOWN_FEATURES


# --- tool-level error conversion: failures return as results, never raise ----


class _FailingEngine:
    def __init__(self, exc):
        self._exc = exc

    def run(self, sql, *, default_database=None):
        raise self._exc


def _tool_fn(engine):
    return make_sql_tool(engine).func


def test_run_sql_athena_failure_returned_not_raised():
    fn = _tool_fn(_FailingEngine(RuntimeError(
        "Athena query FAILED: COLUMN_NOT_FOUND: line 1:44: Column 'mc.x' cannot be resolved"
    )))
    out = fn(sql="SELECT mc.x FROM t")  # no raise
    assert isinstance(out, str) and out.startswith("Error: run_sql failed:")
    assert "COLUMN_NOT_FOUND" in out  # Athena's text passes through verbatim


def test_run_sql_guard_valueerror_is_concise():
    fn = _tool_fn(_FailingEngine(ValueError("run_sql accepts a single read-only statement only")))
    out = fn(sql="DROP TABLE t")
    assert out == "Error: run_sql accepts a single read-only statement only"


def test_run_sql_timeout_returned_not_raised():
    fn = _tool_fn(_FailingEngine(TimeoutError("Athena query q1 timed out")))
    out = fn(sql="SELECT 1")
    assert "TimeoutError" in out and out.startswith("Error: run_sql failed:")
