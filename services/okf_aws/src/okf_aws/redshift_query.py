"""Run ONE read-only SELECT on Redshift via the Data API and return bounded rows.

The Redshift twin of :mod:`okf_aws.athena_query`, for callers that compile
their own SQL (the Attested
Computations runner): execute, poll,
page results, cap rows. The connection is the mapping row's self-describing
source descriptor (``cluster_identifier`` XOR ``workgroup_name`` +
``secret_arn`` + ``redshift_database`` — see ``okf_core.sources``), so — like
the harvest and the chat's pinned engine — there is no deploy-time connection
config. Same defense-in-depth posture as the Athena runner: anything whose
first token isn't SELECT/WITH is refused here, loudly.

Result shape matches :func:`okf_aws.athena_query.run_select` exactly
(``columns`` + POSITIONAL ``rows``), so the computation runner's consumers render
both engines identically.
"""

from __future__ import annotations

import re
import time
from typing import Any

_TERMINAL = ("FINISHED", "FAILED", "ABORTED")


def run_select(
    redshift_data,
    *,
    sql: str,
    database: str,
    cluster_identifier: str | None = None,
    workgroup_name: str | None = None,
    secret_arn: str | None = None,
    max_rows: int = 200,
    poll_interval: float = 0.5,
    timeout_s: float = 120.0,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Execute ``sql`` and return ``{columns, rows, row_count, truncated,
    query_execution_id, stats}``. Raises RuntimeError on engine failure or
    timeout (a timed-out statement is best-effort cancelled first)."""
    # Tokenize like the shape gate ([A-Za-z]+, not whitespace-split): the
    # guard/lint accept `SELECT*FROM t`, and this defense-in-depth check must
    # never permanently refuse a statement they attested.
    m = re.match(r"[A-Za-z]+", sql.lstrip())
    token = m.group(0).upper() if m else ""
    if token not in ("SELECT", "WITH"):
        raise RuntimeError(f"refusing non-SELECT statement (first token {token!r})")
    if not (cluster_identifier or workgroup_name):
        raise RuntimeError(
            "the mapping's source descriptor names no cluster_identifier or "
            "workgroup_name — it cannot be queried"
        )
    if not secret_arn:
        raise RuntimeError("the mapping's source descriptor has no secret_arn")

    params: dict[str, Any] = {
        "Sql": sql,
        "Database": database,
        "SecretArn": secret_arn,
    }
    if cluster_identifier:
        params["ClusterIdentifier"] = cluster_identifier
    else:
        params["WorkgroupName"] = workgroup_name
    sid = redshift_data.execute_statement(**params)["Id"]

    deadline = time.monotonic() + timeout_s
    while True:
        info = redshift_data.describe_statement(Id=sid)
        status = info.get("Status")
        if status in _TERMINAL:
            break
        if time.monotonic() >= deadline:
            # Best-effort cancel so an abandoned statement doesn't keep
            # burning cluster time after the caller stops waiting.
            try:
                redshift_data.cancel_statement(Id=sid)
            except Exception:  # noqa: BLE001 - the timeout is the real error
                pass
            raise RuntimeError(f"query timed out after {timeout_s:.0f}s (id {sid})")
        sleep(poll_interval)
    if status != "FINISHED":
        reason = info.get("Error", "no reason given")
        raise RuntimeError(f"query {status}: {reason}")

    stats = {
        "data_scanned_bytes": None,  # the Data API reports no scan bytes
        "engine_execution_ms": (
            info.get("Duration") // 1_000_000
            if isinstance(info.get("Duration"), int)
            else None
        ),
    }
    if not info.get("HasResultSet"):
        return {
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "query_execution_id": sid,
            "stats": stats,
        }

    columns: list[str] = []
    rows: list[list[str | None]] = []
    truncated = False
    result_params: dict[str, Any] = {"Id": sid}
    while True:
        res = redshift_data.get_statement_result(**result_params)
        if not columns:
            columns = [
                c.get("label") or c.get("name") or ""
                for c in res.get("ColumnMetadata", [])
            ]
        for rec in res.get("Records", []):
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append([_cell(d) for d in rec])
        token_next = res.get("NextToken")
        if truncated or not token_next:
            break
        result_params["NextToken"] = token_next

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "query_execution_id": sid,
        "stats": stats,
    }


def _cell(datum: dict[str, Any]) -> str | None:
    """One Data API Field as text (or None for SQL NULL) — bool as
    ``true``/``false``, numbers stringified, matching the Athena runner's
    ``str | None`` cell shape."""
    if datum.get("isNull"):
        return None
    if "booleanValue" in datum:
        return "true" if datum["booleanValue"] else "false"
    for key in ("stringValue", "longValue", "doubleValue", "blobValue"):
        if key in datum:
            return str(datum[key])
    return None
