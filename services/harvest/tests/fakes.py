"""In-memory fakes for the Glue + Athena and Redshift Data API clients.

Modeled on the real F1 database so tests exercise realistic shapes (Hive types,
ARNs, an Athena SELECT with a header row; and the Redshift Data API's async
execute/describe/get-result surface over the SVV_* catalog views).
"""

from __future__ import annotations

from typing import Any, Callable


class FakeGlue:
    def __init__(self, database: str, tables: dict[str, dict[str, Any]]):
        self._database = database
        self._tables = tables  # name -> Table dict

    def get_database(self, **kwargs) -> dict:
        return {
            "Database": {
                "Name": self._database,
                "Description": "Fake F1 curated database",
                "LocationUri": f"s3://fake-{self._database}/",
                "Parameters": {},
            }
        }

    def get_tables(self, **kwargs) -> dict:
        # Single page; no NextToken.
        return {"TableList": list(self._tables.values())}

    def get_table(self, **kwargs) -> dict:
        name = kwargs["Name"]
        return {"Table": self._tables[name]}


class FakeAthena:
    """Returns a canned result set for any query (header row + N data rows)."""

    def __init__(
        self, rows: list[dict[str, str]] | None = None, state: str = "SUCCEEDED"
    ):
        self._rows = rows if rows is not None else [{"raceid": "1", "year": "2009"}]
        self._state = state

    def start_query_execution(self, **kwargs) -> dict:
        return {"QueryExecutionId": "qid-123"}

    def get_query_execution(self, **kwargs) -> dict:
        return {
            "QueryExecution": {
                "Status": {"State": self._state, "StateChangeReason": "boom"}
            }
        }

    def get_query_results(self, **kwargs) -> dict:
        if not self._rows:
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "col"}]}]}}
        header = list(self._rows[0].keys())
        rows = [{"Data": [{"VarCharValue": h} for h in header]}]
        for r in self._rows:
            # Mirror Athena: a SQL NULL cell is an EMPTY Datum ({}) with no
            # VarCharValue key; an empty string carries VarCharValue="".
            rows.append({"Data": [_datum(r[h]) for h in header]})
        return {"ResultSet": {"Rows": rows}}


def _datum(value: Any) -> dict[str, str]:
    """Render one cell as Athena would: {} for None, else {'VarCharValue': str}."""
    if value is None:
        return {}
    return {"VarCharValue": str(value)}


def _table(name: str, columns: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "Name": name,
        "Description": f"The {name} table",
        "TableType": "EXTERNAL_TABLE",
        "UpdateTime": "2025-10-23T00:00:00+00:00",
        "CreateTime": "2025-10-23T00:00:00+00:00",
        "VersionId": "1",
        "Parameters": {"recordCount": "976"},
        "StorageDescriptor": {
            "Location": f"s3://fake/{name}/",
            "Columns": [{"Name": n, "Type": t, "Comment": c} for (n, t, c) in columns],
        },
        "PartitionKeys": [],
    }


def f1_like_glue() -> FakeGlue:
    tables = {
        "races": _table(
            "races",
            [
                ("raceid", "bigint", "Unique id (PK)"),
                ("year", "bigint", "Season year"),
                ("circuitid", "bigint", "FK to circuits"),
                ("name", "string", "Race name"),
            ],
        ),
        "results": _table(
            "results",
            [
                ("resultid", "bigint", "PK"),
                ("raceid", "bigint", "FK to races"),
                ("driverid", "bigint", "FK to drivers"),
                ("positionorder", "bigint", "Finishing order"),
            ],
        ),
    }
    return FakeGlue("na_mi_formula_1_curated", tables)


# -- Redshift Data API -------------------------------------------------------


class FakeRedshiftData:
    """In-memory ``redshift-data`` client for RedshiftSource tests.

    Built from ``handlers``: an ORDERED list of ``(predicate, rows)`` where
    ``predicate(sql) -> bool`` picks the first matching rule and ``rows`` is a
    ``list[dict]`` (column name -> Python value, ``None`` for SQL NULL). Each
    ``execute_statement`` renders those rows into the Data API's
    ``ColumnMetadata`` / ``Records`` shape, keyed by a per-call statement id, so
    ``describe_statement`` / ``get_statement_result`` return the right result set.
    A SQL with no matching rule yields an empty (``HasResultSet=False``) result —
    the same as a metadata query against a table that has none.
    """

    def __init__(
        self,
        handlers: list[tuple[Callable[[str], bool], list[dict[str, Any]]]],
        *,
        status: str = "FINISHED",
    ):
        self._handlers = handlers
        self._status = status
        self._results: dict[str, list[dict[str, Any]]] = {}
        self.executed: list[str] = []
        self.cancelled: list[str] = []
        self._n = 0

    def _match(self, sql: str) -> list[dict[str, Any]]:
        for predicate, rows in self._handlers:
            if predicate(sql):
                return rows
        return []

    def execute_statement(self, **kwargs) -> dict:
        sql = kwargs["Sql"]
        self.executed.append(sql)
        self._n += 1
        sid = f"stmt-{self._n}"
        self._results[sid] = self._match(sql)
        return {"Id": sid}

    def describe_statement(self, **kwargs) -> dict:
        sid = kwargs["Id"]
        rows = self._results.get(sid, [])
        return {
            "Status": self._status,
            "Error": "boom",
            "HasResultSet": bool(rows),
        }

    def get_statement_result(self, **kwargs) -> dict:
        sid = kwargs["Id"]
        rows = self._results.get(sid, [])
        if not rows:
            return {"ColumnMetadata": [], "Records": []}
        columns = list(rows[0].keys())
        return {
            "ColumnMetadata": [{"name": c} for c in columns],
            "Records": [[_field(r[c]) for c in columns] for r in rows],
        }

    def cancel_statement(self, **kwargs) -> dict:
        self.cancelled.append(kwargs["Id"])
        return {"Status": True}


def _field(value: Any) -> dict[str, Any]:
    """Render one cell as a Redshift Data API Field.

    ``None`` -> ``{"isNull": True}``; ``bool`` -> ``booleanValue``; ``int`` ->
    ``longValue``; ``float`` -> ``doubleValue``; else ``stringValue`` (an empty
    string stays a stringValue, distinct from NULL — the None-vs-"" contract).
    """
    if value is None:
        return {"isNull": True}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"longValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def f1_like_redshift() -> FakeRedshiftData:
    """A Redshift Data API fake mirroring the F1 dataset: one native + one external
    table, across the SVV_* metadata queries RedshiftSource issues."""

    def has(*subs: str) -> Callable[[str], bool]:
        return lambda sql: all(s in sql for s in subs)

    all_tables = [
        {"schema_name": "public", "table_name": "races", "table_type": "TABLE"},
        {
            "schema_name": "spectrum",
            "table_name": "results_ext",
            "table_type": "EXTERNAL TABLE",
        },
    ]
    races_cols = [
        {
            "column_name": "raceid",
            "data_type": "bigint",
            "character_maximum_length": None,
            "numeric_precision": 64,
            "numeric_scale": 0,
            "is_nullable": "NO",
            "remarks": "Unique id (PK)",
        },
        {
            "column_name": "name",
            "data_type": "character varying",
            "character_maximum_length": 255,
            "numeric_precision": None,
            "numeric_scale": None,
            "is_nullable": "YES",
            "remarks": "Race name",
        },
    ]
    ext_cols = [
        {
            "column_name": "resultid",
            "data_type": "bigint",
            "character_maximum_length": None,
            "numeric_precision": 64,
            "numeric_scale": 0,
            "is_nullable": "YES",
            "remarks": "",
        },
        {
            "column_name": "season",
            "data_type": "integer",
            "character_maximum_length": None,
            "numeric_precision": 32,
            "numeric_scale": 0,
            "is_nullable": "YES",
            "remarks": "",
        },
    ]
    return FakeRedshiftData(
        [
            (has("svv_all_tables"), all_tables),
            (has("svv_all_columns", "'races'"), races_cols),
            (has("svv_all_columns", "'results_ext'"), ext_cols),
            (
                has("svv_table_info", "'races'"),
                [
                    {
                        "diststyle": "KEY(raceid)",
                        "sortkey1": "year",
                        "tbl_rows": 976,
                        "estimated_visible_rows": 976,
                    }
                ],
            ),
            (
                has("svv_external_tables", "'results_ext'"),
                [{"location": "s3://fake-f1/results/"}],
            ),
            (
                has("svv_external_columns", "'results_ext'"),
                [{"columnname": "season", "external_type": "int"}],
            ),
        ]
    )
