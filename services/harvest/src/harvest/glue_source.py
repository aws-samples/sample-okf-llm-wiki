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
from typing import Any, Protocol

from okf_core.concept_types import GLUE_DATABASE_TYPE, GLUE_TABLE_TYPE
from okf_core.hive_types import flatten_hive_type

# ConceptRef is the source-neutral concept-reference vocabulary; it lives in
# source_base (with the Source protocol) so every source implementation imports it
# from one home. Re-exported here for back-compat with existing importers.
from harvest.source_base import (
    ConceptRef,
    ResultCapExceeded,
    SourceMetadataProfile,
    SourcePromptProfile,
)

__all__ = ["ConceptRef", "GlueAthenaSource", "ResultCapExceeded"]

# Athena terminal states — note CANCELLED has two L's.
_ATHENA_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class GlueClient(Protocol):  # pragma: no cover - typing only
    def get_database(self, **kwargs) -> dict: ...
    def get_tables(self, **kwargs) -> dict: ...
    def get_table(self, **kwargs) -> dict: ...


class AthenaClient(Protocol):  # pragma: no cover - typing only
    def start_query_execution(self, **kwargs) -> dict: ...
    def get_query_execution(self, **kwargs) -> dict: ...
    def get_query_results(self, **kwargs) -> dict: ...
    def stop_query_execution(self, **kwargs) -> dict: ...


class GlueAthenaSource:
    """Reads one Glue database (a dataset) and samples rows via Athena."""

    name = "glue"

    #: Labels for the ``.metadata/`` snapshot (see metadata_export). Glue resources
    #: are ARNs; row counts are hinted by Glue crawler/ETL Parameters keys.
    metadata_profile = SourceMetadataProfile(
        label="Glue",
        catalog_name="Glue Data Catalog",
        resource_label="Resource (ARN)",
        rowcount_param_keys=("recordCount", "numRows", "rowCount"),
        bytesize_param_keys=("totalSize", "sizeKey", "rawDataSize"),
    )

    #: Source facts the harvest prompts state (see SourcePromptProfile). Reproduces
    #: the original Glue/Athena wording so the Glue harvest prompt is unchanged.
    prompt_profile = SourcePromptProfile(
        engine_sentence="a single AWS Glue database queried via Amazon Athena",
        label="Glue",
        adapter_file="athena-glue.md",
        dialect="Athena/Trino",
        database_type=GLUE_DATABASE_TYPE,
        table_type_note=f"`{GLUE_TABLE_TYPE}` for each table",
        resource_note=(
            "the Glue ARN from the table's `.metadata/tables/<table>.md` sheet"
        ),
        schema_type_term="Hive types",
    )

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
                type=GLUE_DATABASE_TYPE,
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
                    type=GLUE_TABLE_TYPE,
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
        return [r.id[1] for r in self.list_concepts() if r.type == GLUE_TABLE_TYPE]

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
        if ref.type == GLUE_DATABASE_TYPE:
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

        if ref.type == GLUE_TABLE_TYPE:
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
        if ref.type != GLUE_TABLE_TYPE or self.athena is None:
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

    # -- SQL capability atoms (column profiles + verification tools) -----

    #: Athena supports EXPLAIN — the explain_sql tool is registered when a
    #: source advertises this.
    supports_explain = True

    def sql_table_ref(self, table: str) -> str:
        """Fully-quoted Trino reference for one of this dataset's tables."""
        return f'"{self.database}"."{table}"'

    def sql_approx_distinct(self, col_sql: str) -> str:
        """Trino's approximate-distinct aggregate (cheap on wide profiles)."""
        return f"approx_distinct({col_sql})"

    def sql_sample_clause(self, percent: float) -> tuple[str, str]:
        """(FROM-suffix, WHERE-predicate) sampling ~percent% of the table."""
        return (f"TABLESAMPLE BERNOULLI ({percent:g})", "")

    def run_query(
        self,
        query: str,
        *,
        timeout_s: float = 60.0,
        poll_s: float = 1.0,
        max_rows: int | None = None,
        positional: bool = False,
        stats: dict[str, Any] | None = None,
        truncate_at: int | None = None,
    ) -> list[dict[str, str | None]] | tuple[list[str], list[list[str | None]]]:
        """Start an Athena query, poll to terminal state, return rows as dicts.

        Header-aware (row 0 of the first page is the column header). A SQL NULL
        cell is returned as ``None`` (distinct from an empty string ``""``).
        Raises on a non-SUCCEEDED terminal state or timeout (the query is
        best-effort cancelled on timeout so it doesn't hold a workgroup slot).

        ``positional=True`` returns ``(header, rows)`` with each row a
        POSITIONAL list instead of a dict — the benchmark grader's shape, where
        header-keyed dicts would collapse duplicate SELECT labels (e.g.
        ``SELECT r.name, c.name``) into one cell and mis-grade. ``max_rows``
        (default None = unbounded, the harvest callers' behavior) raises
        :class:`ResultCapExceeded` when the result outgrows the cap.

        ``stats`` (optional caller-owned dict) is filled with the execution's
        ``data_scanned_bytes`` / ``engine_ms`` from Athena's final poll — a
        sink parameter rather than a return-shape change so existing callers
        and concurrent sub-agents stay unaffected.

        ``truncate_at`` is the SOFT cap the agent-facing run_sql tool uses:
        collection stops at N rows and ``stats["truncated"] = True`` (vs
        ``max_rows``, the grading path's HARD cap, which raises — an equality
        check over a silently-truncated set would be meaningless).
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
                if stats is not None:
                    s = info.get("Statistics", {}) or {}
                    stats["data_scanned_bytes"] = s.get("DataScannedInBytes")
                    stats["engine_ms"] = s.get("EngineExecutionTimeInMillis")
                break
            if time.monotonic() > deadline:
                # Best-effort cancel so an abandoned query doesn't keep holding
                # a workgroup slot after we stop waiting for it.
                try:
                    self.athena.stop_query_execution(QueryExecutionId=qid)
                except Exception:  # noqa: BLE001 - the timeout is the real error
                    pass
                raise TimeoutError(f"Athena query {qid} timed out")
            time.sleep(poll_s)

        header, rows, truncated = self._collect_results(
            qid, max_rows=max_rows, truncate_at=truncate_at
        )
        if truncated and stats is not None:
            stats["truncated"] = True
        if positional:
            return header, rows
        return [dict(zip(header, vals)) for vals in rows]

    def _collect_results(
        self,
        qid: str,
        *,
        max_rows: int | None = None,
        truncate_at: int | None = None,
    ) -> tuple[list[str], list[list[str | None]], bool]:
        rows: list[list[str | None]] = []
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
                rows.append([c.get("VarCharValue") for c in r["Data"]])
                if max_rows is not None and len(rows) > max_rows:
                    raise ResultCapExceeded(f"result exceeds {max_rows} rows")
                if truncate_at is not None and len(rows) > truncate_at:
                    # Soft cap: flag truncation only once a row BEYOND the cap
                    # arrived — an exactly-cap-sized result is complete, and a
                    # false `truncated` makes the agent distrust and re-run it.
                    del rows[truncate_at:]
                    return header or [], rows, True
            token = res.get("NextToken")
            if not token:
                break
        return header or [], rows, False


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
