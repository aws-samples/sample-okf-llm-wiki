"""Glue Data Catalog + Athena source for the harvest agent.

Ports the reference agent's ``BigQuerySource`` onto AWS: one Glue database is a
*dataset*, each Glue table is a *table* concept. Metadata comes from the Glue
Data Catalog; row samples come from Athena (we *run* the query patterns, not
just read schema, because catalog metadata lies — see the F1 bundle's
``known_issues.md``).

boto3 clients are injected so this is unit-testable with a fake/moto stub and
so the AgentCore execution role stays the single source of credentials.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from okf_core.hive_types import flatten_hive_type

# Athena terminal states — note CANCELLED has two L's.
_ATHENA_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True)
class ConceptRef:
    """A source-advertised concept. Mirrors the reference agent's ConceptRef."""

    id: tuple[str, ...]
    type: str
    resource: str | None = None
    hint: dict[str, Any] = field(default_factory=dict)

    @property
    def id_str(self) -> str:
        return "/".join(self.id)


class GlueClient(Protocol):  # pragma: no cover - typing only
    def get_database(self, **kwargs) -> dict: ...
    def get_tables(self, **kwargs) -> dict: ...
    def get_table(self, **kwargs) -> dict: ...


class AthenaClient(Protocol):  # pragma: no cover - typing only
    def start_query_execution(self, **kwargs) -> dict: ...
    def get_query_execution(self, **kwargs) -> dict: ...
    def get_query_results(self, **kwargs) -> dict: ...


class GlueAthenaSource:
    """Reads one Glue database (a dataset) and samples rows via Athena."""

    name = "glue"

    def __init__(
        self,
        database: str,
        *,
        glue: GlueClient,
        athena: AthenaClient | None = None,
        region: str = "us-east-1",
        account_id: str = "",
        athena_output_location: str | None = None,
        athena_workgroup: str | None = None,
        catalog_id: str | None = None,
    ):
        self.database = database
        self.glue = glue
        self.athena = athena
        self.region = region
        self.account_id = account_id
        self.athena_output_location = athena_output_location
        self.athena_workgroup = athena_workgroup
        self.catalog_id = catalog_id
        self._concepts_cache: list[ConceptRef] | None = None
        self._table_cache: dict[str, dict[str, Any]] = {}

    # -- resource URIs (Glue ARNs, matching the golden bundle) -----------

    def _database_arn(self) -> str:
        return f"arn:aws:glue:{self.region}:{self.account_id}:database/{self.database}"

    def _table_arn(self, table: str) -> str:
        return (
            f"arn:aws:glue:{self.region}:{self.account_id}:"
            f"table/{self.database}/{table}"
        )

    # -- concept enumeration --------------------------------------------

    def _iter_tables(self):
        kwargs: dict[str, Any] = {"DatabaseName": self.database}
        if self.catalog_id:
            kwargs["CatalogId"] = self.catalog_id
        token = None
        while True:
            if token:
                kwargs["NextToken"] = token
            resp = self.glue.get_tables(**kwargs)
            for tbl in resp.get("TableList", []):
                yield tbl
            token = resp.get("NextToken")
            if not token:
                break

    def list_concepts(self) -> list[ConceptRef]:
        if self._concepts_cache is not None:
            return self._concepts_cache
        concepts: list[ConceptRef] = [
            ConceptRef(
                id=("datasets", self.database),
                type="Glue Database",
                resource=self._database_arn(),
                hint={"database": self.database},
            )
        ]
        for tbl in self._iter_tables():
            name = tbl["Name"]
            self._table_cache[name] = tbl
            concepts.append(
                ConceptRef(
                    id=("tables", name),
                    type="Glue Table",
                    resource=self._table_arn(name),
                    hint={"table": name},
                )
            )
        self._concepts_cache = concepts
        return concepts

    def find(self, concept_id: tuple[str, ...]) -> ConceptRef | None:
        for ref in self.list_concepts():
            if ref.id == concept_id:
                return ref
        return None

    def table_names(self) -> list[str]:
        return [r.id[1] for r in self.list_concepts() if r.type == "Glue Table"]

    # -- metadata --------------------------------------------------------

    def _get_table_raw(self, table: str) -> dict[str, Any]:
        if table in self._table_cache:
            return self._table_cache[table]
        kwargs: dict[str, Any] = {"DatabaseName": self.database, "Name": table}
        if self.catalog_id:
            kwargs["CatalogId"] = self.catalog_id
        tbl = self.glue.get_table(**kwargs)["Table"]
        self._table_cache[table] = tbl
        return tbl

    def read_concept(self, ref: ConceptRef) -> dict[str, Any]:
        if ref.type == "Glue Database":
            kwargs: dict[str, Any] = {"Name": self.database}
            if self.catalog_id:
                kwargs["CatalogId"] = self.catalog_id
            db = self.glue.get_database(**kwargs).get("Database", {})
            return {
                "database": self.database,
                "region": self.region,
                "account_id": self.account_id,
                "description": db.get("Description"),
                "location_uri": db.get("LocationUri"),
                "parameters": db.get("Parameters", {}),
                "create_time": _iso(db.get("CreateTime")),
                "table_count": len(self.table_names()),
                "resource": self._database_arn(),
            }

        if ref.type == "Glue Table":
            table = ref.hint["table"]
            tbl = self._get_table_raw(table)
            sd = tbl.get("StorageDescriptor", {}) or {}
            columns = sd.get("Columns", []) or []
            partition_keys = tbl.get("PartitionKeys", []) or []
            return {
                "database": self.database,
                "table": table,
                "resource": self._table_arn(table),
                "description": tbl.get("Description"),
                "table_type": tbl.get("TableType"),
                "location": sd.get("Location"),
                "columns": [_column_dict(c) for c in columns],
                "partition_keys": [_column_dict(c) for c in partition_keys],
                "parameters": tbl.get("Parameters", {}),
                "update_time": _iso(tbl.get("UpdateTime")),
                "create_time": _iso(tbl.get("CreateTime")),
                "version_id": tbl.get("VersionId"),
                # Flattened, readable schema rows the agent drops into # Schema.
                "flat_schema": _flat_schema(columns),
                "flat_partition_schema": _flat_schema(partition_keys),
            }

        raise ValueError(f"Unknown concept type: {ref.type}")

    # -- Athena row sampling --------------------------------------------

    def sample_rows(
        self, ref: ConceptRef, n: int = 5, *, timeout_s: float = 60.0
    ) -> list[dict[str, str | None]] | None:
        if ref.type != "Glue Table" or self.athena is None:
            return None
        table = ref.hint["table"]
        # nosec B608 - not user input: self.database/table come from the Glue
        # catalog (system-authored metadata, not request data) and are wrapped in
        # double quotes as Trino identifiers; n is coerced with int(). Athena also
        # runs read-only under the per-invocation scoped session (see clients.py).
        query = f'SELECT * FROM "{self.database}"."{table}" LIMIT {int(n)}'  # nosec B608
        try:
            return self.run_query(query, timeout_s=timeout_s)
        except Exception:
            return None

    def run_query(
        self, query: str, *, timeout_s: float = 60.0, poll_s: float = 1.0
    ) -> list[dict[str, str | None]]:
        """Start an Athena query, poll to terminal state, return rows as dicts.

        Header-aware (row 0 of the first page is the column header). A SQL NULL
        cell is returned as ``None`` (distinct from an empty string ``""``).
        Raises on a non-SUCCEEDED terminal state or timeout.
        """
        if self.athena is None:
            raise RuntimeError("Athena client not configured")
        kwargs: dict[str, Any] = {
            "QueryString": query,
            "QueryExecutionContext": {"Database": self.database},
        }
        if self.catalog_id:
            kwargs["QueryExecutionContext"]["Catalog"] = self.catalog_id
        if self.athena_workgroup:
            kwargs["WorkGroup"] = self.athena_workgroup
        if self.athena_output_location:
            kwargs["ResultConfiguration"] = {
                "OutputLocation": self.athena_output_location
            }
        qid = self.athena.start_query_execution(**kwargs)["QueryExecutionId"]

        deadline = time.monotonic() + timeout_s
        while True:
            info = self.athena.get_query_execution(QueryExecutionId=qid)[
                "QueryExecution"
            ]
            state = info["Status"]["State"]
            if state in _ATHENA_TERMINAL:
                if state != "SUCCEEDED":
                    reason = info["Status"].get("StateChangeReason", "")
                    raise RuntimeError(f"Athena query {state}: {reason}")
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"Athena query {qid} timed out")
            time.sleep(poll_s)

        return self._collect_results(qid)

    def _collect_results(self, qid: str) -> list[dict[str, str | None]]:
        rows: list[dict[str, str | None]] = []
        header: list[str] | None = None
        token = None
        while True:
            params: dict[str, Any] = {"QueryExecutionId": qid}
            if token:
                params["NextToken"] = token
            res = self.athena.get_query_results(**params)
            page = res["ResultSet"]["Rows"]
            if header is None:
                # Column names are always present; keep them as plain strings.
                header = [c.get("VarCharValue", "") for c in page[0]["Data"]]
                page = page[1:]
            for r in page:
                # A SQL NULL comes back as a Datum with NO VarCharValue key; an
                # empty string comes back as VarCharValue="". Preserve that
                # distinction (None vs ""). Collapsing both to "" — the old
                # `.get("VarCharValue", "")` — misled the authoring model into
                # empty-string semantics and wrong `= ''` / `<> ''` idioms.
                vals = [c.get("VarCharValue") for c in r["Data"]]
                rows.append(dict(zip(header, vals)))
            token = res.get("NextToken")
            if not token:
                break
        return rows


# -- helpers -----------------------------------------------------------------


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _column_dict(col: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": col.get("Name"),
        "type": col.get("Type"),
        "comment": col.get("Comment"),
    }


def _flat_schema(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten every column (incl. nested structs) into readable rows."""
    out: list[dict[str, Any]] = []
    for col in columns:
        name = col.get("Name") or ""
        hive_type = col.get("Type") or ""
        comment = col.get("Comment") or ""
        flat = flatten_hive_type(name, hive_type)
        for i, f in enumerate(flat):
            out.append(
                {
                    "name": f.name,
                    "type": f.type,
                    "depth": f.depth,
                    # attach the column comment only to the top-level row
                    "comment": comment if i == 0 else "",
                }
            )
    return out
