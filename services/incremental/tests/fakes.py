"""In-memory fakes for Glue + AgentCore used by the incremental tests.

moto covers S3 + DynamoDB (real-ish AWS behavior); Glue table-version diffing
and AgentCore InvokeAgentRuntime need small hand-rolled fakes because moto's
coverage of get_table_versions / bedrock-agentcore is thin or absent.
"""

from __future__ import annotations

from typing import Any


def col(name: str, type_: str, comment: str | None = None) -> dict[str, Any]:
    """A Glue column dict ({Name, Type, Comment})."""
    return {"Name": name, "Type": type_, "Comment": comment}


def make_table(
    name: str,
    columns: list[dict[str, Any]],
    *,
    version_id: str = "1",
    update_time: str = "2026-06-01T00:00:00+00:00",
) -> dict[str, Any]:
    """A Glue Table dict with the columns wrapped in a StorageDescriptor."""
    return {
        "Name": name,
        "VersionId": version_id,
        "UpdateTime": update_time,
        "StorageDescriptor": {"Columns": list(columns)},
        "PartitionKeys": [],
    }


class _NotFound(Exception):
    """Mimics a botocore ClientError with an EntityNotFoundException code."""

    def __init__(self):
        super().__init__("not found")
        self.response = {"Error": {"Code": "EntityNotFoundException"}}


class FakeGlue:
    """Fake Glue client.

    ``tables`` maps ``database -> {table_name -> [Table-version, ...]}`` where the
    version list is ordered OLDEST-first (index -1 is the current version). This
    lets a single fake express multi-version histories for diffing.
    """

    def __init__(self, tables: dict[str, dict[str, list[dict[str, Any]]]]):
        self._tables = tables

    def _history(self, database: str, table: str) -> list[dict[str, Any]]:
        db = self._tables.get(database)
        if db is None or table not in db:
            raise _NotFound()
        return db[table]

    def get_table(self, *, DatabaseName: str, Name: str, **_) -> dict:
        history = self._history(DatabaseName, Name)
        return {"Table": history[-1]}

    def get_table_versions(self, *, DatabaseName: str, TableName: str, **_) -> dict:
        history = self._history(DatabaseName, TableName)
        # Glue returns newest-first.
        return {
            "TableVersions": [
                {"VersionId": t.get("VersionId"), "Table": t} for t in reversed(history)
            ]
        }

    def get_tables(self, *, DatabaseName: str, **_) -> dict:
        db = self._tables.get(DatabaseName, {})
        # Return the current version of each table (single page).
        return {"TableList": [history[-1] for history in db.values()]}


class FakeAgentCore:
    """Records InvokeAgentRuntime calls for assertions."""

    def __init__(self):
        self.invocations: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs) -> dict:
        self.invocations.append(kwargs)
        return {"statusCode": 200}
