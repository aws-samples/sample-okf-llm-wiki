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
    RedshiftDataSQL,
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


# --- the Redshift Data API engine (fake client) ------------------------------


class _FakeRedshiftData:
    """Canned execute/describe/result pages. Records calls for assertions."""

    def __init__(self, pages, *, status="FINISHED"):
        # pages: list of {"ColumnMetadata": [...], "Records": [...]} result pages.
        self._pages = pages
        self._status = status
        self.executed = []
        self.cancelled = []

    def execute_statement(self, **kwargs):
        self.executed.append(kwargs)
        return {"Id": "stmt-1"}

    def describe_statement(self, **kwargs):
        return {
            "Status": self._status,
            "Error": "boom",
            "HasResultSet": bool(self._pages),
        }

    def get_statement_result(self, **kwargs):
        idx = 0 if "NextToken" not in kwargs else int(kwargs["NextToken"])
        page = dict(self._pages[idx])
        if idx + 1 < len(self._pages):
            page["NextToken"] = str(idx + 1)
        return page

    def cancel_statement(self, **kwargs):
        self.cancelled.append(kwargs["Id"])
        return {"Status": True}


def _rs_page(columns, records):
    return {
        "ColumnMetadata": [{"name": c} for c in columns],
        "Records": records,
    }


def _rs_engine(data, **kw):
    kw.setdefault("cluster_identifier", "prod-cluster")
    kw.setdefault("secret_arn", "arn:aws:secretsmanager:eu-west-1:1:secret:okf-x")
    return RedshiftDataSQL(data=data, database="warehouse", **kw)


def test_redshift_engine_requires_target_and_secret():
    with pytest.raises(ValueError):
        RedshiftDataSQL(data=_FakeRedshiftData([]), database="warehouse")
    with pytest.raises(ValueError):
        RedshiftDataSQL(
            data=_FakeRedshiftData([]), database="warehouse", workgroup_name="wg"
        )


def test_redshift_engine_returns_typed_rows_and_preserves_null():
    data = _FakeRedshiftData(
        [
            _rs_page(
                ["id", "name"],
                [
                    [{"longValue": 1}, {"stringValue": "a"}],
                    [{"longValue": 2}, {"isNull": True}],
                    [{"booleanValue": True}, {"stringValue": ""}],
                ],
            )
        ]
    )
    out = _rs_engine(data).run('SELECT id, name FROM "public"."t"')
    assert out["columns"] == ["id", "name"]
    assert out["rows"] == [
        {"id": "1", "name": "a"},
        {"id": "2", "name": None},  # NULL preserved (not "")
        {"id": "true", "name": ""},  # bool -> "true"; empty string stays ""
    ]
    assert out["row_count"] == 3 and out["truncated"] is False
    # The pinned connection flowed into the Data API call.
    exe = data.executed[0]
    assert exe["Database"] == "warehouse"
    assert exe["ClusterIdentifier"] == "prod-cluster"
    assert exe["SecretArn"].endswith(":secret:okf-x")
    assert "WorkgroupName" not in exe


def test_redshift_engine_workgroup_connection():
    data = _FakeRedshiftData([_rs_page(["n"], [[{"longValue": 1}]])])
    eng = RedshiftDataSQL(
        data=data,
        database="warehouse",
        workgroup_name="analytics-wg",
        secret_arn="arn:aws:secretsmanager:eu-west-1:1:secret:okf-x",
    )
    eng.run("SELECT n FROM t")
    assert data.executed[0]["WorkgroupName"] == "analytics-wg"
    assert "ClusterIdentifier" not in data.executed[0]


def test_redshift_engine_paginates_and_truncates():
    data = _FakeRedshiftData(
        [
            _rs_page(["n"], [[{"longValue": 1}], [{"longValue": 2}]]),
            _rs_page(["n"], [[{"longValue": 3}], [{"longValue": 4}]]),
        ]
    )
    out = _rs_engine(data, max_rows=3).run("SELECT n FROM t")
    assert [r["n"] for r in out["rows"]] == ["1", "2", "3"]
    assert out["truncated"] is True


def test_redshift_engine_rejects_non_read_before_calling_backend():
    data = _FakeRedshiftData([])
    with pytest.raises(ValueError):
        _rs_engine(data).run("DROP TABLE t")
    assert data.executed == []  # never reached the Data API


def test_redshift_engine_raises_on_failed_statement():
    data = _FakeRedshiftData([_rs_page(["n"], [])], status="FAILED")
    with pytest.raises(RuntimeError):
        _rs_engine(data).run("SELECT 1")


def test_redshift_engine_timeout_cancels_statement():
    data = _FakeRedshiftData([_rs_page(["n"], [])], status="STARTED")
    with pytest.raises(TimeoutError):
        _rs_engine(data, timeout_s=0).run("SELECT 1")
    assert data.cancelled == ["stmt-1"]


def test_redshift_engine_ignores_default_database():
    # Signature parity with AthenaSQL: the connection stays pinned to the
    # mapping's database regardless of any scope-derived default.
    data = _FakeRedshiftData([_rs_page(["n"], [[{"longValue": 1}]])])
    _rs_engine(data).run("SELECT n FROM t", default_database="something_else")
    assert data.executed[0]["Database"] == "warehouse"


def test_make_sql_tool_uses_engine_description():
    # The tool description must match the ENGINE (backend + dialect), not a fixed
    # Athena blurb — the model writes SQL based on this text.
    athena_tool = make_sql_tool(AthenaSQL(athena=_FakeAthena([])))
    assert "Athena" in athena_tool.description
    rs_tool = make_sql_tool(
        _rs_engine(_FakeRedshiftData([])),
        dataset_scope={"data_domain": "sales", "dataset": "orders_analytics"},
    )
    assert "Redshift" in rs_tool.description
    assert "`warehouse`" in rs_tool.description  # names the pinned database
    assert "Trino" not in rs_tool.description or "NOT Athena/Trino" in rs_tool.description


def test_tool_descriptions_own_the_statement_rules_the_prompt_dropped():
    # graph.SQL_BLOCK no longer restates the verb allowlist / qualification /
    # LIMIT mechanics — they are per-engine, so the DESCRIPTION is authoritative.
    athena = make_sql_tool(AthenaSQL(athena=_FakeAthena([]))).description
    redshift = make_sql_tool(_rs_engine(_FakeRedshiftData([]))).description
    for desc in (athena, redshift):
        assert "exactly ONE statement" in desc
        assert "INSERT/UPDATE/DELETE/CREATE/DROP" in desc
        assert "Add your own LIMIT" in desc
    assert '"database"."table"' in athena  # catalog-wide: qualify the database
    assert '"schema"."table"' in redshift  # pinned db: qualify the schema


def test_guard_error_matches_each_engines_advertised_verbs():
    """The guard's rejection text must not advertise a verb its own engine's
    description forbids — Redshift has NO DESCRIBE, so telling the model to try
    DESCRIBE there sends it into a second, equally doomed query.
    """
    athena = AthenaSQL(athena=_FakeAthena([]))
    redshift = _rs_engine(_FakeRedshiftData([]))

    with pytest.raises(ValueError) as ath_err:
        athena.run("DELETE FROM t")
    with pytest.raises(ValueError) as rs_err:
        redshift.run("DELETE FROM t")

    assert "DESCRIBE" in str(ath_err.value)
    assert "DESCRIBE" in athena.tool_description
    # Redshift: absent from BOTH the error and the description, and the dialect.
    assert "DESCRIBE" not in str(rs_err.value)
    assert "SELECT / WITH / SHOW / EXPLAIN" in str(rs_err.value)
    assert "SELECT/WITH/SHOW/EXPLAIN only" in redshift.tool_description


def test_descriptions_state_the_actual_row_cap_and_truncated_flag():
    # "large results are truncated" told the model nothing actionable; the real
    # cap comes from the engine (OKF_CHAT_SQL_MAX_ROWS, default 200) and the
    # response carries a `truncated` flag.
    athena = AthenaSQL(athena=_FakeAthena([]), max_rows=200)
    assert "at most 200 rows" in athena.tool_description
    assert "`truncated: true`" in athena.tool_description
    # A non-default cap is reflected, not hardcoded — both engines interpolate.
    assert "at most 25 rows" in AthenaSQL(
        athena=_FakeAthena([]), max_rows=25
    ).tool_description
    assert "at most 25 rows" in _rs_engine(
        _FakeRedshiftData([]), max_rows=25
    ).tool_description


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
    # tool_description is part of the engine contract make_sql_tool reads (real
    # engines: AthenaSQL / RedshiftDataSQL). The value is irrelevant to these
    # error-path tests, but the attribute must exist.
    tool_description = "run_sql (test double)"

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


# --- the query-time soft policy check wiring ----------------------------------


class _StubEngine:
    tool_description = "run_sql (test double)"

    def __init__(self, result=None):
        self._result = result if result is not None else {
            "columns": ["n"], "rows": [{"n": "1"}],
            "row_count": 1, "truncated": False,
        }

    def run(self, sql, *, default_database=None):
        return self._result


class _StubChecker:
    """Scripted PolicyChecker double: an immediate (or never-resolving)
    Future, mirroring only the surface make_sql_tool reads."""

    wait_budget_s = 0.5

    def __init__(self, note="", resolve=True, raise_on_submit=False, wait=True):
        self._note = note
        self._resolve = resolve
        self._raise = raise_on_submit
        self._wait = wait
        self.submitted: list[str] = []

    def should_wait(self, sql):
        return self._wait

    def submit(self, sql):
        from concurrent.futures import Future

        self.submitted.append(sql)
        if self._raise:
            raise RuntimeError("boom")
        fut = Future()
        if self._resolve:
            fut.set_result(self._note)
        return fut


def test_policy_reminder_rides_after_the_result():
    import json as _json

    checker = _StubChecker(note="<system-reminder>policy note</system-reminder>")
    tool = make_sql_tool(_StubEngine(), policy_checker=checker)
    out = tool.func("SELECT SUM(x) FROM t GROUP BY y")
    assert isinstance(out, str) and out.endswith("policy note</system-reminder>")
    # The payload survives intact ahead of the reminder, and the checker saw
    # the exact SQL (submitted BEFORE the engine ran).
    assert _json.loads(out.split("\n\n")[0])["rows"] == [{"n": "1"}]
    assert checker.submitted == ["SELECT SUM(x) FROM t GROUP BY y"]


def test_policy_clean_verdict_leaves_the_result_untouched():
    tool = make_sql_tool(_StubEngine(), policy_checker=_StubChecker(note=""))
    assert isinstance(tool.func("SELECT SUM(x) FROM t"), dict)


def test_policy_verdict_timeout_is_fail_open():
    checker = _StubChecker(resolve=False)  # the verdict never arrives
    checker.wait_budget_s = 0.05
    tool = make_sql_tool(_StubEngine(), policy_checker=checker)
    assert isinstance(tool.func("SELECT SUM(x) FROM t"), dict)


def test_policy_submit_failure_is_fail_open():
    tool = make_sql_tool(
        _StubEngine(), policy_checker=_StubChecker(raise_on_submit=True)
    )
    assert isinstance(tool.func("SELECT SUM(x) FROM t"), dict)


def test_exploration_queries_never_wait_on_the_verdict():
    # should_wait=False (an exploration probe): the tool returns immediately
    # even though the checker's future never resolves — the probe merely warms
    # the checker's caches in the background.
    checker = _StubChecker(resolve=False, wait=False)
    checker.wait_budget_s = 30  # would hang the test if the tool waited
    tool = make_sql_tool(_StubEngine(), policy_checker=checker)
    assert isinstance(tool.func("SELECT * FROM t LIMIT 5"), dict)
    assert checker.submitted == ["SELECT * FROM t LIMIT 5"]  # still submitted


# --- the policy opt-in feature vocabulary -------------------------------------


def test_normalize_features_policy_requires_sql():
    from chat.sql import policy_tracks

    # policy:* values are valid only alongside sql — orphaned ones drop.
    assert normalize_features(["sql", "policy:strict"]) == {"sql", "policy:strict"}
    assert normalize_features(["policy:strict"]) == set()
    assert normalize_features(["policy:computational", "policy:behavioural"]) == set()
    assert normalize_features(["sql", "policy:computational", "canvas"]) == {
        "sql", "policy:computational",
    }
    # Track derivation: each value arms its own; strict arms both.
    assert policy_tracks({"sql", "policy:computational"}) == {"computational"}
    assert policy_tracks({"sql", "policy:behavioural"}) == {"behavioural"}
    assert policy_tracks({"sql", "policy:strict"}) == {
        "computational", "behavioural",
    }
    assert policy_tracks({"sql"}) == frozenset()
    assert policy_tracks(None) == frozenset()
