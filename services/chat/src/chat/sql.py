"""Optional read-only SQL over the Glue catalog for the chat agent (Athena).

This is the one chat tool that touches SOURCE DATA (every other tool reads the
authored wiki bundle). It is therefore gated TWO ways, both server-side — a
client string can never turn it on by itself:

  1. **Deploy-time** — ``OKF_CHAT_SQL_ENABLED`` must be true. The chat IAM role
     only carries Glue/Athena grants when ``var.enable_chat_sql`` is set, so with
     the flag off the tool would 403 anyway; we don't even offer it to the model.
  2. **Per-conversation** — the browser opts in via ``features: ["sql"]`` on the
     run envelope (the composer's "+" menu). The runtime adds the tool only when
     BOTH the deploy flag AND the per-run opt-in are present.

Read-only is enforced by IAM (the role has no Glue/S3 write grants) AND by a
query guard here (``SELECT``/``WITH`` only, single statement) so a non-read query
gets a clean error instead of an opaque Athena/permission failure — defense in
depth, not the sole boundary.

Catalog-wide: unlike harvest (pinned to one database per invocation via a scoped
STS session), the chat SQL tool can query ANY database — the model writes
fully-qualified ``"db"."table"`` references. A ``dataset_scope`` (the ``@``-mention)
is used only as the DEFAULT database for unqualified names + advisory context; it
is not a security boundary.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Protocol

# Trailing-semicolon-tolerant single-statement read-only guard. We strip SQL
# comments first so a ``SELECT 1 -- ; DROP`` can't smuggle a second statement.
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_READ_ONLY_HEADS = ("select", "with", "show", "describe", "explain")

# Athena terminal states — CANCELLED has two L's (matches harvest's glue_source).
_ATHENA_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class AthenaClient(Protocol):  # pragma: no cover - typing only
    def start_query_execution(self, **kwargs) -> dict: ...
    def get_query_execution(self, **kwargs) -> dict: ...
    def get_query_results(self, **kwargs) -> dict: ...


def strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line and ``/* */`` block comments (so the guard can't be fooled)."""
    return _SQL_COMMENT_RE.sub(" ", sql)


def is_read_only(sql: str) -> bool:
    """True iff ``sql`` is a SINGLE read-only statement (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN).

    Comments are stripped, one trailing ``;`` is tolerated, and any REMAINING
    ``;`` (a second statement) fails. The first keyword must be a read verb —
    which structurally excludes INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/MERGE/etc.
    IAM has no write grants regardless; this is for a clean error + defense depth.
    """
    s = strip_sql_comments(sql).strip()
    # Tolerate exactly one trailing semicolon; anything else (embedded ;) is multi.
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if not s or ";" in s:
        return False
    head = s.split(None, 1)[0].lower()
    return head in _READ_ONLY_HEADS


class AthenaSQL:
    """Runs a read-only Athena query catalog-wide and returns typed rows.

    boto3 client injected (live or fake) so this is unit-testable and the
    AgentCore execution role stays the single source of credentials. Not pinned
    to a database — the model qualifies tables — but an optional ``default_database``
    (from the conversation's ``@``-scope) lets unqualified names resolve.
    """

    def __init__(
        self,
        *,
        athena: AthenaClient,
        catalog: str = "AwsDataCatalog",
        output_location: str | None = None,
        workgroup: str | None = None,
        max_rows: int = 200,
        timeout_s: float = 60.0,
        poll_s: float = 1.0,
    ) -> None:
        self.athena = athena
        self.catalog = catalog
        self.output_location = output_location
        self.workgroup = workgroup
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    def run(self, sql: str, *, default_database: str | None = None) -> dict[str, Any]:
        """Execute one read-only query; return ``{columns, rows, row_count, truncated}``.

        ``rows`` are dicts keyed by column name; a SQL ``NULL`` is ``None`` (kept
        distinct from an empty string, as in harvest). Rows beyond ``max_rows`` are
        dropped and ``truncated`` is set — the model still gets a representative
        sample without blowing up the turn's token budget. Raises ``ValueError``
        for a non-read query and ``RuntimeError``/``TimeoutError`` on Athena failure.
        """
        if not is_read_only(sql):
            raise ValueError(
                "run_sql accepts a single read-only statement only "
                "(SELECT / WITH / SHOW / DESCRIBE / EXPLAIN)."
            )
        ctx: dict[str, Any] = {"Catalog": self.catalog}
        if default_database:
            ctx["Database"] = default_database
        kwargs: dict[str, Any] = {
            "QueryString": sql,
            "QueryExecutionContext": ctx,
        }
        if self.workgroup:
            kwargs["WorkGroup"] = self.workgroup
        if self.output_location:
            kwargs["ResultConfiguration"] = {"OutputLocation": self.output_location}

        qid = self.athena.start_query_execution(**kwargs)["QueryExecutionId"]

        deadline = time.monotonic() + self.timeout_s
        while True:
            info = self.athena.get_query_execution(QueryExecutionId=qid)[
                "QueryExecution"
            ]
            state = info["Status"]["State"]
            if state in _ATHENA_TERMINAL:
                if state != "SUCCEEDED":
                    reason = info["Status"].get("StateChangeReason", "")
                    raise RuntimeError(f"Athena query {state}: {reason}".strip())
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"Athena query {qid} timed out")
            time.sleep(self.poll_s)

        return self._collect(qid)

    def _collect(self, qid: str) -> dict[str, Any]:
        header: list[str] | None = None
        rows: list[dict[str, Any]] = []
        truncated = False
        token = None
        while True:
            params: dict[str, Any] = {"QueryExecutionId": qid}
            if token:
                params["NextToken"] = token
            res = self.athena.get_query_results(**params)
            page = res["ResultSet"]["Rows"]
            if header is None:
                header = [c.get("VarCharValue", "") for c in page[0]["Data"]]
                page = page[1:]
            for r in page:
                if len(rows) >= self.max_rows:
                    truncated = True
                    break
                # NULL = Datum with no VarCharValue key; "" = VarCharValue="".
                vals = [c.get("VarCharValue") for c in r["Data"]]
                rows.append(dict(zip(header, vals)))
            token = res.get("NextToken")
            if not token or truncated:
                break
        return {
            "columns": header or [],
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }


# The tool description the model sees. Explicit about read-only + qualifying names
# so the agent uses it correctly (it can't see the guard/IAM, only this text).
_RUN_SQL_DESC = (
    "Run a read-only SQL query against the organization's data catalog via Amazon "
    "Athena (Trino SQL) and return the result rows. Use this to answer questions "
    "that need LIVE data or aggregates the wiki docs don't state (counts, sums, "
    "distinct values, spot-checks) — NOT to rediscover schema, which you should "
    "read from the wiki first.\n"
    "Rules: exactly ONE statement, SELECT/WITH/SHOW/DESCRIBE/EXPLAIN only (no "
    "INSERT/UPDATE/DELETE/CREATE/DROP). Qualify tables as \"database\".\"table\" "
    "(the catalog spans many databases). Add your own LIMIT; large results are "
    "truncated. Ground the query in schema you read from the wiki."
)


def make_sql_tool(
    engine: AthenaSQL, *, dataset_scope: dict[str, str] | None = None
) -> Any:
    """Wrap an :class:`AthenaSQL` as a LangChain ``run_sql`` StructuredTool.

    When the conversation is ``@``-scoped, that dataset's Glue database is used as
    the DEFAULT database for unqualified table names (advisory — the model may
    still query other databases by qualifying them). The scope's ``glue_database``
    (the real Glue DB name, resolved from the registry) is preferred; the
    ``dataset`` id is only a fallback for when it wasn't resolved, and the two can
    differ (a dataset id need not equal its Glue database name), so an unqualified
    query would hit a non-existent database if we defaulted to the dataset id.
    """
    from langchain_core.tools import StructuredTool

    default_db = None
    if dataset_scope:
        default_db = dataset_scope.get("glue_database") or dataset_scope.get("dataset")

    def run_sql(sql: str) -> dict[str, Any]:
        return engine.run(sql, default_database=default_db)

    return StructuredTool.from_function(
        func=run_sql,
        name="run_sql",
        description=_RUN_SQL_DESC,
    )


# The known, server-recognized optional features (the browser may request a
# subset via the run envelope's ``features``). Kept here so config/server share it.
KNOWN_FEATURES: frozenset[str] = frozenset({"sql"})


def normalize_features(raw: Any) -> set[str]:
    """Coerce a client-sent ``features`` value to the recognized subset (a set)."""
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(f) for f in raw if str(f) in KNOWN_FEATURES}
