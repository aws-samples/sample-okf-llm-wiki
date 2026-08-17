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
        s3: Any | None = None,
    ):
        self.database = database
        self.glue = glue
        self.athena = athena
        # Optional (tests/fakes omit it): used ONLY by estimate_table_bytes,
        # the size-gate fallback for tables whose Glue Parameters carry no
        # byte hint (DDL-created tables — only crawlers/ETL write totalSize).
        self.s3 = s3
        self.region = region
        self.account_id = account_id
        self.athena_output_location = athena_output_location
        self.athena_workgroup = athena_workgroup
        self.catalog_id = catalog_id
        self._concepts_cache: list[ConceptRef] | None = None
        self._table_cache: dict[str, dict[str, Any]] = {}
        # iceberg_data_bytes memo — the profile pass and both relationship
        # sizing paths (gate + sample percent) ask for the same table; the
        # $files answer is exact, so one query serves them all.
        self._iceberg_bytes_cache: dict[str, int | None] = {}

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

    def estimate_table_bytes(
        self, table: str, *, stop_at: int | None
    ) -> int | None:
        """Actual on-S3 byte size of a table's data, by listing its location.

        The relationship precompute's size-gate fallback: DDL-created tables
        carry no ``totalSize`` Parameter (only crawlers/ETL write those), and
        assuming them large would skip every probe on a hand-registered
        catalog. Listing is cheap (no query, no scan; LIST calls only).

        ``stop_at`` picks the contract. With an int, the listing EARLY-EXITS
        once the total exceeds it and returns that running total — a LOWER
        BOUND good only for "over or under the gate", never for sizing a
        sample. With None, the listing runs to completion and the figure is
        exact (the sampled-probe percent needs this: a lower bound would
        inflate the sample fraction on a huge table by orders of magnitude).

        Returns None when it cannot tell (no s3 client injected, a VIEW or
        non-S3 location, listing denied/failed, or the page cap hit before
        the listing finished) — the caller then assumes large, as before.
        A partitioned table whose root location lists as EMPTY is also None:
        its partitions live elsewhere (ADD PARTITION with external
        locations), so zero is not evidence of small.

        Iceberg tables short-circuit to :meth:`iceberg_data_bytes` before any
        listing: their Glue Parameters never carry Hive stats, and an S3
        listing OVERCOUNTS them (it sums every retained snapshot's data files
        plus manifests until VACUUM/expire). The ``$files`` sum is exact for
        the current snapshot regardless of ``stop_at``, so it serves the gate
        and the sample-percent contract alike; only when that query fails
        does the table fall through to the listing, whose overcount is at
        least conservative for the gate.
        """
        try:
            tbl = self._get_table_raw(table)
        except Exception:  # noqa: BLE001 - can't tell -> assume large
            return None
        if _is_iceberg_table(tbl):
            size = self.iceberg_data_bytes(table)
            if size is not None:
                return size
        if self.s3 is None:
            return None
        loc = str((tbl.get("StorageDescriptor") or {}).get("Location") or "")
        if not loc.startswith("s3://"):
            return None  # a view / non-S3 table has nothing listable
        bucket, _, prefix = loc[len("s3://") :].partition("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"  # 'data/orders' must not also sum 'data/orders_v2'
        total = 0
        token: str | None = None
        try:
            for _ in range(50):  # ≤ 50k objects — bounds the API call count
                kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = self.s3.list_objects_v2(**kwargs)
                total += sum(
                    int(o.get("Size") or 0) for o in resp.get("Contents") or []
                )
                if stop_at is not None and total > stop_at:
                    return total  # over the gate — no need to keep listing
                if not resp.get("IsTruncated"):
                    if total == 0 and tbl.get("PartitionKeys"):
                        return None  # partition data lives outside the root
                    return total
                token = resp.get("NextContinuationToken")
        except Exception:  # noqa: BLE001 - can't tell -> assume large
            return None
        return None  # page cap hit before the listing finished: cannot be sure

    def iceberg_data_bytes(self, table: str) -> int | None:
        """Exact byte size of an Iceberg table's CURRENT snapshot, or None.

        Sums ``file_size_in_bytes`` over the ``"<table>$files"`` metadata
        table — a manifests-only scan (no data bytes billed) that answers
        through the query engine's own permissions, so it also works where
        the S3 location cannot be listed (LF-governed / cross-account
        tables). None for non-Iceberg tables, sources without Athena, or any
        query failure — callers fall through to their next sizing rung. A
        NULL sum means an empty current snapshot, which really is 0 bytes
        (unlike an empty S3 root, which proves nothing about a partitioned
        table).
        """
        if self.athena is None:
            return None
        try:
            tbl = self._get_table_raw(table)
        except Exception:  # noqa: BLE001 - can't tell
            return None
        if not _is_iceberg_table(tbl):
            return None
        if table in self._iceberg_bytes_cache:
            return self._iceberg_bytes_cache[table]
        size: int | None
        try:
            rows = self.run_query(
                f"SELECT sum(file_size_in_bytes) AS b "
                f'FROM "{self.database}"."{table}$files"'  # nosec B608 - catalog-authored identifiers, quoted
            )
            val = rows[0].get("b") if rows else None
            size = int(str(val)) if val is not None else 0
        except Exception:  # noqa: BLE001 - a failed metadata query is "can't tell"
            size = None
        self._iceberg_bytes_cache[table] = size
        return size

    def sql_sampled_ref(self, ref_sql: str, percent: float) -> str:
        """A table reference sampled at ~``percent``% for the relationship
        probes' big-fact-side path.

        ``TABLESAMPLE SYSTEM``, deliberately NOT the profiler's BERNOULLI:
        SYSTEM skips whole splits, so it cuts the bytes Athena BILLS —
        BERNOULLI reads everything and filters rows after, which would make
        a "sampled" probe of a TB fact table cost the same as a full one.
        The trade-off is coarseness: SYSTEM samples storage splits, so data
        clustered by the join key (e.g. time-partitioned facts where orphans
        concentrate in recent files) can skew the rate — the evidence sheet
        carries an INDICATIVE banner for exactly that reason.

        Returned as a parenthesized subquery, not a bare ``ref TABLESAMPLE``
        suffix: Trino's grammar nests the alias INSIDE the sampled relation
        (``orders o TABLESAMPLE SYSTEM (10)``), so callers composing
        ``FROM {ref} t`` — every probe in probes.py does — would produce a
        parse error with the suffix form. A derived table aliases normally."""
        return f"(SELECT * FROM {ref_sql} TABLESAMPLE SYSTEM ({percent:g}))"

    def sql_bottomk_sketch(self, col_sql: str, k: int) -> str:
        """Aggregate expression: the ``k`` smallest xxhash64 values of a
        column's distinct values (a KMV/bottom-k sketch, as array<bigint>).

        Values are hashed through a varchar cast so an int ``42`` and a
        varchar ``'42'`` sketch identically — cast-requiring joins are real
        joins. NULLs vanish (to_utf8(NULL) is NULL; min ignores it). One
        SELECT can carry this expression for EVERY column of a table, so a
        whole table sketches in a single (columnar) scan.

        The DISTINCT is load-bearing: Trino's ``min(x, n)`` is an order
        statistic over input ROWS, not distinct values. Without it a value
        repeated m times fills m sketch slots, so any column where rows ≫
        distinct (every fact-side FK, average fanout f) collapses to ~k/f
        usable hashes — wrecking the cardinality estimate and tripping the
        enum-domain suppression on exactly the columns sketches exist for.

        The from_big_endian_64 is equally load-bearing: Trino's xxhash64
        returns VARBINARY, which renders as unparseable bytes in the result
        set. Decoding to a signed bigint here is what makes the sketch cells
        plain int arrays (and matches the signed 64-bit normalization the
        KMV estimator assumes)."""
        return (
            "min(DISTINCT from_big_endian_64(xxhash64(to_utf8("
            f"cast({col_sql} as varchar)))), {int(k)})"
        )

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


def _is_iceberg_table(tbl: dict[str, Any]) -> bool:
    """Glue marks Iceberg tables with a ``table_type`` Parameter.

    That's the open-table-format marker (written by Athena and Iceberg's
    ``GlueCatalog`` alike), distinct from the top-level ``TableType`` field
    (``EXTERNAL_TABLE``/``VIRTUAL_VIEW``).
    """
    params = tbl.get("Parameters") or {}
    return str(params.get("table_type") or "").upper() == "ICEBERG"


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
