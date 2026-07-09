"""In-memory fakes for the Glue + Athena clients used by GlueAthenaSource tests.

Modeled on the real F1 database so tests exercise realistic shapes (Hive types,
ARNs, an Athena SELECT with a header row).
"""

from __future__ import annotations

from typing import Any


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
