"""Amazon Redshift source for the harvest agent (Redshift Data API).

The AWS counterpart of :class:`~harvest.glue_source.GlueAthenaSource` for Redshift:
one Redshift **database** (in a provisioned cluster or a Serverless workgroup) is a
*dataset*; each table / view is a *table* concept, addressed as ``<schema>.<table>``
so a database's many schemas never collide. Both metadata AND row samples go
through the **Redshift Data API** (``redshift-data``) — one async surface
(``execute_statement`` → poll ``describe_statement`` → ``get_statement_result``),
so there is no separate metadata client the way Glue+Athena split the work.

Metadata comes from the Redshift system catalog views the ``redshift.md`` source
adapter documents: ``SVV_ALL_TABLES`` (enumerate), ``SVV_ALL_COLUMNS`` (schema,
spanning native + external + late-binding views), ``SVV_TABLE_INFO`` (native
design + a scan-free row-count hint), and ``SVV_EXTERNAL_*`` (Spectrum/Glue-backed
externals). Catalog metadata can be wrong/stale, so — exactly like the Glue source
— the live ``sample_rows`` / ``run_sql`` tools stay the way an authored claim is
verified against real data.

The ``redshift-data`` client is injected so this is unit-testable with an in-memory
fake and so the AgentCore execution/data role stays the single source of creds.
Redshift SQL is Postgres-derived (identifiers double-quoted, string literals
single-quoted); the type vocabulary and gotchas live in the ``redshift.md`` adapter.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from okf_core.concept_types import (
    REDSHIFT_DATABASE_TYPE,
    REDSHIFT_EXTERNAL_TABLE_TYPE,
    REDSHIFT_TABLE_TYPE,
)

from harvest.source_base import (
    ConceptRef,
    SourceMetadataProfile,
    SourcePromptProfile,
)

__all__ = ["RedshiftSource"]

# Redshift Data API terminal statement states.
_RS_TERMINAL = {"FINISHED", "FAILED", "ABORTED"}

# System schemas that are never dataset content.
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_internal")


class RedshiftDataClient(Protocol):  # pragma: no cover - typing only
    def execute_statement(self, **kwargs) -> dict: ...
    def describe_statement(self, **kwargs) -> dict: ...
    def get_statement_result(self, **kwargs) -> dict: ...


class RedshiftSource:
    """Reads one Redshift database (a dataset) via the Redshift Data API."""

    name = "redshift"

    #: Labels for the ``.metadata/`` snapshot (see metadata_export). Redshift has no
    #: REST ARN for tables, so the resource is a connection URI; the scan-free
    #: row-count hint comes from SVV_TABLE_INFO's ``tbl_rows`` (stashed in the
    #: per-table ``parameters`` alongside diststyle/sortkey).
    metadata_profile = SourceMetadataProfile(
        label="Redshift",
        catalog_name="Redshift system catalog (SVV_* views)",
        resource_label="Resource (URI)",
        rowcount_param_keys=("tbl_rows", "estimated_visible_rows"),
    )

    #: Source facts the harvest prompts state (see SourcePromptProfile). Redshift
    #: has its own concept types, dialect, adapter, and a connection-URI resource
    #: (no table ARN) — so the prompt narration reads correctly for Redshift.
    prompt_profile = SourcePromptProfile(
        engine_sentence=(
            "a single Amazon Redshift database (schema-qualified tables) queried "
            "via the Redshift Data API"
        ),
        label="Redshift",
        adapter_file="redshift.md",
        dialect="amazon-redshift",
        database_type=REDSHIFT_DATABASE_TYPE,
        table_type_note=(
            f"`{REDSHIFT_TABLE_TYPE}` for a native table / view / materialized "
            f"view, `{REDSHIFT_EXTERNAL_TABLE_TYPE}` for a Spectrum / external table"
        ),
        resource_note=(
            "the `redshift://<endpoint>:5439/<database>#<schema>.<table>` "
            "connection URI from the table's `.metadata/tables/<table>.md` sheet"
        ),
        schema_type_term="Redshift column types",
    )

    def __init__(
        self,
        database: str,
        *,
        data: RedshiftDataClient,
        cluster_identifier: str | None = None,
        workgroup_name: str | None = None,
        db_user: str | None = None,
        secret_arn: str | None = None,
        endpoint: str | None = None,
        region: str = "us-east-1",
        account_id: str = "",
    ):
        if not (cluster_identifier or workgroup_name):
            raise ValueError(
                "RedshiftSource needs a cluster_identifier (provisioned) or "
                "workgroup_name (serverless)"
            )
        self.database = database
        self.data = data
        self.cluster_identifier = cluster_identifier
        self.workgroup_name = workgroup_name
        self.db_user = db_user
        self.secret_arn = secret_arn
        self.endpoint = endpoint
        self.region = region
        self.account_id = account_id
        self._concepts_cache: list[ConceptRef] | None = None

    # -- resource URIs (connection URI + dotted path; no per-table ARN) --

    def _host(self) -> str:
        """The host component of the connection URI.

        Prefer an explicit endpoint; else derive the conventional Serverless
        hostname, or fall back to the provisioned cluster identifier.
        """
        if self.endpoint:
            return self.endpoint
        if self.workgroup_name:
            return (
                f"{self.workgroup_name}.{self.account_id}.{self.region}"
                ".redshift-serverless.amazonaws.com"
            )
        return self.cluster_identifier or ""

    def _database_uri(self) -> str:
        return f"redshift://{self._host()}:5439/{self.database}"

    def _table_uri(self, schema: str, table: str) -> str:
        return f"{self._database_uri()}#{schema}.{table}"

    # -- concept enumeration --------------------------------------------

    def list_concepts(self) -> list[ConceptRef]:
        if self._concepts_cache is not None:
            return self._concepts_cache
        concepts: list[ConceptRef] = [
            ConceptRef(
                id=("datasets", self.database),
                type=REDSHIFT_DATABASE_TYPE,
                resource=self._database_uri(),
                hint={"database": self.database},
            )
        ]
        for row in self._run_sql(
            "SELECT schema_name, table_name, table_type "
            "FROM svv_all_tables "
            f"WHERE database_name = '{_q(self.database)}' "
            f"AND schema_name NOT IN ({_in_list(_SYSTEM_SCHEMAS)}) "
            "ORDER BY schema_name, table_name"  # nosec B608 - catalog identifiers, single-quoted; read-only
        ):
            schema = row.get("schema_name") or ""
            table = row.get("table_name") or ""
            if not schema or not table:
                continue
            is_external = "EXTERNAL" in (row.get("table_type") or "").upper()
            concepts.append(
                ConceptRef(
                    id=("tables", f"{schema}.{table}"),
                    type=(
                        REDSHIFT_EXTERNAL_TABLE_TYPE
                        if is_external
                        else REDSHIFT_TABLE_TYPE
                    ),
                    resource=self._table_uri(schema, table),
                    hint={
                        "schema": schema,
                        "table": table,
                        "external": is_external,
                    },
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
        return [
            r.id[1]
            for r in self.list_concepts()
            if r.type in (REDSHIFT_TABLE_TYPE, REDSHIFT_EXTERNAL_TABLE_TYPE)
        ]

    # -- metadata --------------------------------------------------------

    def read_concept(self, ref: ConceptRef) -> dict[str, Any]:
        if ref.type == REDSHIFT_DATABASE_TYPE:
            return {
                "database": self.database,
                "region": self.region,
                "account_id": self.account_id,
                "table_count": len(self.table_names()),
                "resource": self._database_uri(),
            }

        if ref.type in (REDSHIFT_TABLE_TYPE, REDSHIFT_EXTERNAL_TABLE_TYPE):
            schema = ref.hint["schema"]
            table = ref.hint["table"]
            is_external = bool(ref.hint.get("external"))

            flat_schema = self._columns(schema, table)
            # Native tables carry scan-free design + row-count in SVV_TABLE_INFO;
            # externals carry S3 location + partition keys in SVV_EXTERNAL_*.
            parameters: dict[str, Any] = {}
            location: str | None = None
            flat_partition_schema: list[dict[str, Any]] = []
            if is_external:
                location = self._external_location(schema, table)
                flat_partition_schema = self._external_partition_keys(schema, table)
            else:
                parameters = self._table_info(schema, table)

            return {
                "database": self.database,
                "table": f"{schema}.{table}",
                "resource": self._table_uri(schema, table),
                "table_type": (
                    "EXTERNAL TABLE" if is_external else "TABLE"
                ),
                "location": location,
                "columns": [
                    {"name": f["name"], "type": f["type"], "comment": f["comment"]}
                    for f in flat_schema
                ],
                "parameters": parameters,
                "flat_schema": flat_schema,
                "flat_partition_schema": flat_partition_schema,
            }

        raise ValueError(f"Unknown concept type: {ref.type}")

    def _columns(self, schema: str, table: str) -> list[dict[str, Any]]:
        """Flat schema rows from SVV_ALL_COLUMNS (spans native + external + views)."""
        rows = self._run_sql(
            "SELECT column_name, data_type, character_maximum_length, "
            "numeric_precision, numeric_scale, is_nullable, remarks "
            "FROM svv_all_columns "
            f"WHERE database_name = '{_q(self.database)}' "
            f"AND schema_name = '{_q(schema)}' "
            f"AND table_name = '{_q(table)}' "
            "ORDER BY ordinal_position"  # nosec B608 - catalog identifiers, single-quoted; read-only
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "name": r.get("column_name") or "",
                    "type": _format_type(
                        r.get("data_type"),
                        r.get("character_maximum_length"),
                        r.get("numeric_precision"),
                        r.get("numeric_scale"),
                    ),
                    "depth": 0,  # Redshift columns are flat (SUPER is opaque here)
                    "comment": (r.get("remarks") or "").strip(),
                }
            )
        return out

    def _table_info(self, schema: str, table: str) -> dict[str, Any]:
        """Best-effort design + scan-free row-count from SVV_TABLE_INFO.

        Superuser/GRANT-gated and returns nothing for empty tables, so a failure or
        empty result is normal — we just omit the params (never wedge a harvest).
        """
        try:
            rows = self._run_sql(
                'SELECT diststyle, sortkey1, tbl_rows, estimated_visible_rows '
                "FROM svv_table_info "
                f"WHERE \"schema\" = '{_q(schema)}' "
                f"AND \"table\" = '{_q(table)}'"  # nosec B608 - catalog identifiers, single-quoted; read-only
            )
        except Exception:  # noqa: BLE001 - metadata is best-effort
            return {}
        if not rows:
            return {}
        r = rows[0]
        params: dict[str, Any] = {}
        for key in ("diststyle", "sortkey1", "tbl_rows", "estimated_visible_rows"):
            val = r.get(key)
            if val not in (None, ""):
                params[key] = val
        return params

    def _external_location(self, schema: str, table: str) -> str | None:
        try:
            rows = self._run_sql(
                "SELECT location FROM svv_external_tables "
                f"WHERE schemaname = '{_q(schema)}' "
                f"AND tablename = '{_q(table)}'"  # nosec B608 - catalog identifiers, single-quoted; read-only
            )
        except Exception:  # noqa: BLE001 - metadata is best-effort
            return None
        return rows[0].get("location") if rows else None

    def _external_partition_keys(
        self, schema: str, table: str
    ) -> list[dict[str, Any]]:
        try:
            rows = self._run_sql(
                "SELECT columnname, external_type FROM svv_external_columns "
                f"WHERE schemaname = '{_q(schema)}' "
                f"AND tablename = '{_q(table)}' AND part_key > 0 "
                "ORDER BY part_key"  # nosec B608 - catalog identifiers, single-quoted; read-only
            )
        except Exception:  # noqa: BLE001 - metadata is best-effort
            return []
        return [
            {
                "name": r.get("columnname") or "",
                "type": r.get("external_type") or "",
                "depth": 0,
                "comment": "",
            }
            for r in rows
        ]

    # -- live row sampling / verification -------------------------------

    def sample_rows(
        self, ref: ConceptRef, n: int = 5, *, timeout_s: float = 60.0
    ) -> list[dict[str, str | None]] | None:
        if ref.type not in (REDSHIFT_TABLE_TYPE, REDSHIFT_EXTERNAL_TABLE_TYPE):
            return None
        schema = ref.hint["schema"]
        table = ref.hint["table"]
        # nosec B608 - not user input: schema/table come from the Redshift catalog
        # (system-authored metadata), double-quoted as Postgres identifiers; n is
        # coerced with int(). Access is read-only under the scoped session.
        query = f'SELECT * FROM "{schema}"."{table}" LIMIT {int(n)}'  # nosec B608
        try:
            return self.run_query(query, timeout_s=timeout_s)
        except Exception:  # noqa: BLE001
            return None

    def run_query(
        self, query: str, *, timeout_s: float = 60.0, poll_s: float = 1.0
    ) -> list[dict[str, str | None]]:
        """Run a read-only Redshift statement and return rows as dicts.

        A SQL NULL cell is returned as ``None`` (distinct from an empty string
        ``""``), matching ``GlueAthenaSource.run_query``. Raises on a non-FINISHED
        terminal state or timeout.
        """
        return self._run_sql(query, timeout_s=timeout_s, poll_s=poll_s)

    # -- Redshift Data API plumbing -------------------------------------

    def _run_sql(
        self, sql: str, *, timeout_s: float = 60.0, poll_s: float = 1.0
    ) -> list[dict[str, str | None]]:
        params: dict[str, Any] = {"Sql": sql, "Database": self.database}
        if self.cluster_identifier:
            params["ClusterIdentifier"] = self.cluster_identifier
        if self.workgroup_name:
            params["WorkgroupName"] = self.workgroup_name
        if self.db_user:
            params["DbUser"] = self.db_user
        if self.secret_arn:
            params["SecretArn"] = self.secret_arn

        sid = self.data.execute_statement(**params)["Id"]

        deadline = time.monotonic() + timeout_s
        while True:
            info = self.data.describe_statement(Id=sid)
            status = info.get("Status")
            if status in _RS_TERMINAL:
                if status != "FINISHED":
                    reason = info.get("Error", "")
                    raise RuntimeError(f"Redshift statement {status}: {reason}")
                if not info.get("HasResultSet"):
                    return []
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"Redshift statement {sid} timed out")
            time.sleep(poll_s)

        return self._collect_results(sid)

    def _collect_results(self, sid: str) -> list[dict[str, str | None]]:
        rows: list[dict[str, str | None]] = []
        columns: list[str] | None = None
        token = None
        while True:
            kwargs: dict[str, Any] = {"Id": sid}
            if token:
                kwargs["NextToken"] = token
            res = self.data.get_statement_result(**kwargs)
            if columns is None:
                columns = [c.get("name", "") for c in res.get("ColumnMetadata", [])]
            for rec in res.get("Records", []):
                rows.append({columns[i]: _cell(rec[i]) for i in range(len(columns))})
            token = res.get("NextToken")
            if not token:
                break
        return rows


# -- helpers -----------------------------------------------------------------


def _cell(datum: dict[str, Any]) -> str | None:
    """Render one Redshift Data API cell as ``str`` (or None for SQL NULL).

    A Data API Field is a one-key dict: ``isNull`` for SQL NULL, else one of
    ``stringValue`` / ``longValue`` / ``doubleValue`` / ``booleanValue`` /
    ``blobValue``. We coerce to text (bool → lowercase ``true``/``false`` to match
    SQL rendering) so downstream sees the same ``str | None`` shape Athena yields.
    """
    if datum.get("isNull"):
        return None
    if "booleanValue" in datum:
        return "true" if datum["booleanValue"] else "false"
    for key in ("stringValue", "longValue", "doubleValue", "blobValue"):
        if key in datum:
            return str(datum[key])
    return None


def _q(literal: str) -> str:
    """Escape a single-quoted SQL string literal (double any embedded quote)."""
    return str(literal).replace("'", "''")


def _in_list(values: tuple[str, ...]) -> str:
    """Render a tuple of strings as a SQL ``IN`` value list of quoted literals."""
    return ", ".join(f"'{_q(v)}'" for v in values)


def _format_type(
    data_type: Any,
    char_max_len: Any,
    num_precision: Any,
    num_scale: Any,
) -> str:
    """A readable column type from SVV_ALL_COLUMNS parts (length/precision suffix).

    ``character varying`` + length 255 -> ``character varying(255)``; ``numeric`` +
    precision 10 scale 2 -> ``numeric(10,2)``. Other types pass through unchanged.
    """
    dt = str(data_type or "").strip()
    low = dt.lower()
    char_types = (
        "character varying",
        "varchar",
        "character",
        "char",
        "nchar",
        "nvarchar",
        "bpchar",
    )
    if char_max_len not in (None, "", "0", 0) and low in char_types:
        return f"{dt}({char_max_len})"
    if num_precision not in (None, "", "0", 0) and low in ("numeric", "decimal"):
        if num_scale not in (None, "", "0", 0):
            return f"{dt}({num_precision},{num_scale})"
        return f"{dt}({num_precision})"
    return dt
