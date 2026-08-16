"""Run ONE read-only SELECT on Athena and return bounded rows.

Shared execution helper for callers that compile their own SQL (the
Attested Computations runner — see ``okf_aws.computation_run``) — start, poll, page results, cap rows.
The SQL handed in is expected to be compiler-generated or otherwise already
validated as a SELECT; this module still refuses anything whose first token
isn't SELECT/WITH as defense in depth, because the caller's IAM grant is
read-only by construction and a DDL statement should fail loudly HERE, not
as a confusing engine error.
"""

from __future__ import annotations

import re
import time
from typing import Any

_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


def run_select(
    athena,
    *,
    sql: str,
    database: str,
    workgroup: str | None = None,
    output_location: str | None = None,
    catalog: str = "AwsDataCatalog",
    max_rows: int = 200,
    poll_interval: float = 0.5,
    timeout_s: float = 120.0,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Execute ``sql`` and return ``{columns, rows, row_count, truncated,
    query_execution_id, stats}``. Raises RuntimeError on engine failure or
    timeout (message carries Athena's state-change reason)."""
    # Tokenize like the shape gate ([A-Za-z]+, not whitespace-split): the
    # guard/lint accept `SELECT*FROM t`, and this defense-in-depth check must
    # never permanently refuse a statement they attested.
    m = re.match(r"[A-Za-z]+", sql.lstrip())
    token = m.group(0).upper() if m else ""
    if token not in ("SELECT", "WITH"):
        raise RuntimeError(f"refusing non-SELECT statement (first token {token!r})")

    kwargs: dict[str, Any] = {
        "QueryString": sql,
        "QueryExecutionContext": {"Database": database, "Catalog": catalog},
    }
    if workgroup:
        kwargs["WorkGroup"] = workgroup
    if output_location:
        kwargs["ResultConfiguration"] = {"OutputLocation": output_location}
    qid = athena.start_query_execution(**kwargs)["QueryExecutionId"]

    deadline = time.monotonic() + timeout_s
    while True:
        info = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = info.get("Status", {}).get("State")
        if state in _TERMINAL:
            break
        if time.monotonic() >= deadline:
            try:
                athena.stop_query_execution(QueryExecutionId=qid)
            except Exception:  # noqa: BLE001 - best-effort cancel on our way out
                pass
            raise RuntimeError(f"query timed out after {timeout_s:.0f}s (id {qid})")
        sleep(poll_interval)
    if state != "SUCCEEDED":
        reason = info.get("Status", {}).get("StateChangeReason", "no reason given")
        raise RuntimeError(f"query {state}: {reason}")

    columns: list[str] = []
    rows: list[list[str | None]] = []
    truncated = False
    params: dict[str, Any] = {"QueryExecutionId": qid}
    header_pending = True
    while True:
        res = athena.get_query_results(**params)
        if not columns:
            columns = [
                c.get("Label") or c.get("Name") or ""
                for c in res.get("ResultSet", {})
                .get("ResultSetMetadata", {})
                .get("ColumnInfo", [])
            ]
        for raw in res.get("ResultSet", {}).get("Rows", []):
            values = [d.get("VarCharValue") for d in raw.get("Data", [])]
            # Athena's first result row of a SELECT repeats the column labels.
            if header_pending:
                header_pending = False
                if values == columns:
                    continue
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append(values)
        token_next = res.get("NextToken")
        if truncated or not token_next:
            break
        params["NextToken"] = token_next

    stats = info.get("Statistics", {}) or {}
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "query_execution_id": qid,
        "stats": {
            "data_scanned_bytes": stats.get("DataScannedInBytes"),
            "engine_execution_ms": stats.get("EngineExecutionTimeInMillis"),
        },
    }
