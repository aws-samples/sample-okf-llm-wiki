"""Optional read-only SQL over live source data for the chat agent.

This is the one chat tool that touches SOURCE DATA (every other tool reads the
authored wiki bundle). It is therefore gated TWO ways, both server-side — a
client string can never turn it on by itself:

  1. **Deploy-time** — ``OKF_CHAT_SQL_ENABLED`` must be true. The chat IAM role
     only carries the source-data grants when ``var.enable_chat_sql`` is set, so
     with the flag off the tool would 403 anyway; we don't even offer it.
  2. **Per-conversation** — the browser opts in via ``features: ["sql"]`` on the
     run envelope (the composer's "+" menu). The runtime adds the tool only when
     BOTH the deploy flag AND the per-run opt-in are present.

Two engines, dispatched on the conversation's ``@``-scope (see
``server.make_agent_factory``):

* :class:`AthenaSQL` — the default. Catalog-wide over the Glue catalog: unlike
  harvest (pinned to one database per invocation via a scoped STS session), it
  can query ANY database — the model writes fully-qualified ``"db"."table"``
  references. A glue ``dataset_scope`` is used only as the DEFAULT database for
  unqualified names + advisory context; it is not a security boundary.
  Read-only is enforced by IAM (no write grants) AND the query guard below.
* :class:`RedshiftDataSQL` — used when the ``@``-scoped dataset's registry
  mapping is a REDSHIFT source (and ``OKF_REDSHIFT_ENABLED`` is on). Pinned to
  that mapping's cluster/workgroup + database + Secrets Manager secret (the
  self-describing source descriptor). NOTE the different read-only story: the
  statement executes with the SQL privileges of the SECRET'S DB USER — IAM
  cannot bound Redshift SQL the way it bounds Athena — so the guard here is the
  runtime check and the mapping's read-only DB user is the real boundary
  (docs/DATA_SOURCES.md). A Redshift-scoped run on a deployment without
  Redshift enabled gets NO SQL tool rather than silently querying the wrong
  backend via Athena.

The query guard (``SELECT``/``WITH``/… only, single statement) applies to both
engines so a non-read query gets a clean error instead of an opaque backend or
permission failure — defense in depth, not the sole boundary.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Protocol

log = logging.getLogger("chat.sql")

# Trailing-semicolon-tolerant single-statement read-only guard. We strip SQL
# comments first so a ``SELECT 1 -- ; DROP`` can't smuggle a second statement.
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_READ_ONLY_HEADS = ("select", "with", "show", "describe", "explain")

# The read verbs each ENGINE advertises — the guard accepts the union
# (_READ_ONLY_HEADS), but the error text and the tool description must agree per
# engine or the model is told to fix a query one way and rejected the other:
# Redshift has no DESCRIBE, so its description forbids it and its error text must
# not suggest it. Kept as the single source for both strings per engine.
_ATHENA_READ_VERBS = "SELECT / WITH / SHOW / DESCRIBE / EXPLAIN"
_REDSHIFT_READ_VERBS = "SELECT / WITH / SHOW / EXPLAIN"


def _read_only_error(verbs: str) -> str:
    """The guard's rejection text for an engine, naming only ITS read verbs."""
    return f"run_sql accepts a single read-only statement only ({verbs})."


# Athena terminal states — CANCELLED has two L's (matches harvest's glue_source).
_ATHENA_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}

# Redshift Data API terminal statement states (matches harvest's redshift_source).
_RS_TERMINAL = {"FINISHED", "FAILED", "ABORTED"}


class AthenaClient(Protocol):  # pragma: no cover - typing only
    def start_query_execution(self, **kwargs) -> dict: ...
    def get_query_execution(self, **kwargs) -> dict: ...
    def get_query_results(self, **kwargs) -> dict: ...


class RedshiftDataClient(Protocol):  # pragma: no cover - typing only
    def execute_statement(self, **kwargs) -> dict: ...
    def describe_statement(self, **kwargs) -> dict: ...
    def get_statement_result(self, **kwargs) -> dict: ...
    def cancel_statement(self, **kwargs) -> dict: ...


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

    @property
    def tool_description(self) -> str:
        """The run_sql tool description the model sees for this engine."""
        return _RUN_SQL_DESC.format(max_rows=self.max_rows)

    def run(self, sql: str, *, default_database: str | None = None) -> dict[str, Any]:
        """Execute one read-only query; return ``{columns, rows, row_count, truncated}``.

        ``rows`` are dicts keyed by column name; a SQL ``NULL`` is ``None`` (kept
        distinct from an empty string, as in harvest). Rows beyond ``max_rows`` are
        dropped and ``truncated`` is set — the model still gets a representative
        sample without blowing up the turn's token budget. Raises ``ValueError``
        for a non-read query and ``RuntimeError``/``TimeoutError`` on Athena failure.
        """
        if not is_read_only(sql):
            raise ValueError(_read_only_error(_ATHENA_READ_VERBS))
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


class RedshiftDataSQL:
    """Runs a read-only query against ONE Redshift database via the Data API.

    Built when the conversation is ``@``-scoped to a Redshift-backed dataset:
    the connection (cluster/workgroup + database + Secrets Manager secret) comes
    from that mapping's self-describing source descriptor, so — unlike the
    catalog-wide Athena engine — this engine is PINNED to the scoped dataset's
    backend. Returns the same ``{columns, rows, row_count, truncated}`` shape as
    :class:`AthenaSQL`, with SQL ``NULL`` as ``None`` (distinct from ``""``).

    Read-only: the guard rejects non-read statements up front, but the statement
    ultimately executes with the SQL privileges of the secret's DB user — use a
    read-only user in the mapping's secret (docs/DATA_SOURCES.md).
    """

    def __init__(
        self,
        *,
        data: RedshiftDataClient,
        database: str,
        cluster_identifier: str | None = None,
        workgroup_name: str | None = None,
        secret_arn: str | None = None,
        max_rows: int = 200,
        timeout_s: float = 60.0,
        poll_s: float = 1.0,
    ) -> None:
        if not (cluster_identifier or workgroup_name):
            raise ValueError(
                "RedshiftDataSQL needs a cluster_identifier (provisioned) or "
                "workgroup_name (serverless)"
            )
        if not secret_arn:
            raise ValueError("RedshiftDataSQL needs the mapping's secret_arn")
        self.data = data
        self.database = database
        self.cluster_identifier = cluster_identifier
        self.workgroup_name = workgroup_name
        self.secret_arn = secret_arn
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    @property
    def tool_description(self) -> str:
        """The run_sql tool description the model sees (names the pinned DB)."""
        return _RUN_SQL_DESC_REDSHIFT.format(
            database=self.database, max_rows=self.max_rows
        )

    def run(self, sql: str, *, default_database: str | None = None) -> dict[str, Any]:
        """Execute one read-only statement; return ``{columns, rows, row_count,
        truncated}``.

        ``default_database`` is accepted for signature parity with
        :class:`AthenaSQL` and IGNORED — the connection is pinned to the scoped
        mapping's database. Raises ``ValueError`` for a non-read query and
        ``RuntimeError``/``TimeoutError`` on backend failure (a timed-out
        statement is best-effort cancelled first).
        """
        if not is_read_only(sql):
            # Redshift has no DESCRIBE — naming it here would tell the model to fix
            # its query with a verb this engine's description forbids.
            raise ValueError(_read_only_error(_REDSHIFT_READ_VERBS))
        params: dict[str, Any] = {
            "Sql": sql,
            "Database": self.database,
            "SecretArn": self.secret_arn,
        }
        if self.cluster_identifier:
            params["ClusterIdentifier"] = self.cluster_identifier
        else:
            params["WorkgroupName"] = self.workgroup_name

        sid = self.data.execute_statement(**params)["Id"]

        deadline = time.monotonic() + self.timeout_s
        while True:
            info = self.data.describe_statement(Id=sid)
            status = info.get("Status")
            if status in _RS_TERMINAL:
                if status != "FINISHED":
                    reason = info.get("Error", "")
                    raise RuntimeError(f"Redshift statement {status}: {reason}".strip())
                if not info.get("HasResultSet"):
                    return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
                break
            if time.monotonic() > deadline:
                # Best-effort cancel so an abandoned statement doesn't keep
                # burning cluster time after the chat turn stops waiting.
                try:
                    self.data.cancel_statement(Id=sid)
                except Exception:  # noqa: BLE001 - the timeout is the real error
                    pass
                raise TimeoutError(f"Redshift statement {sid} timed out")
            time.sleep(self.poll_s)

        return self._collect(sid)

    def _collect(self, sid: str) -> dict[str, Any]:
        columns: list[str] | None = None
        rows: list[dict[str, Any]] = []
        truncated = False
        token = None
        while True:
            params: dict[str, Any] = {"Id": sid}
            if token:
                params["NextToken"] = token
            res = self.data.get_statement_result(**params)
            if columns is None:
                columns = [c.get("name", "") for c in res.get("ColumnMetadata", [])]
            for rec in res.get("Records", []):
                if len(rows) >= self.max_rows:
                    truncated = True
                    break
                rows.append(
                    {columns[i]: _rs_cell(rec[i]) for i in range(len(columns))}
                )
            token = res.get("NextToken")
            if not token or truncated:
                break
        return {
            "columns": columns or [],
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }


def _rs_cell(datum: dict[str, Any]) -> str | None:
    """One Redshift Data API cell as ``str`` (or None for SQL NULL).

    A Field is a one-key dict: ``isNull`` for SQL NULL, else one of
    ``stringValue`` / ``longValue`` / ``doubleValue`` / ``booleanValue`` /
    ``blobValue``. Coerced to text (bool → ``true``/``false``) so downstream sees
    the same ``str | None`` shape the Athena engine yields.
    """
    if datum.get("isNull"):
        return None
    if "booleanValue" in datum:
        return "true" if datum["booleanValue"] else "false"
    for key in ("stringValue", "longValue", "doubleValue", "blobValue"):
        if key in datum:
            return str(datum[key])
    return None


# The tool description the model sees. Explicit about read-only + qualifying names
# so the agent uses it correctly (it can't see the guard/IAM, only this text).
# ``{max_rows}`` is filled from the ENGINE's configured cap so the number the model
# is told matches the row limit it will actually hit (OKF_CHAT_SQL_MAX_ROWS).
_RUN_SQL_DESC = (
    "Run a read-only SQL query against the organization's data catalog via Amazon "
    "Athena (Trino SQL) and return the result rows. Use this to answer questions "
    "that need LIVE data or aggregates the wiki docs don't state (counts, sums, "
    "distinct values, spot-checks) — NOT to rediscover schema, which you should "
    "read from the wiki first.\n"
    "Rules: exactly ONE statement, SELECT/WITH/SHOW/DESCRIBE/EXPLAIN only (no "
    "INSERT/UPDATE/DELETE/CREATE/DROP). Qualify tables as \"database\".\"table\" "
    "(the catalog spans many databases). Add your own LIMIT: at most {max_rows} "
    "rows come back, and any beyond that are dropped with `truncated: true` in the "
    "result — aggregate in SQL instead of paging. Ground the query in schema you "
    "read from the wiki."
)

# The Redshift variant: pinned to the @-scoped dataset's database, Postgres-
# derived dialect, schema-qualified names (a Redshift database holds many
# schemas — unqualified names resolve via the connection's search_path). NOTE the
# verb list omits DESCRIBE (Redshift has none) — _REDSHIFT_READ_VERBS keeps the
# guard's rejection text saying the same thing.
_RUN_SQL_DESC_REDSHIFT = (
    "Run a read-only SQL query against `{database}` — the Amazon Redshift "
    "database behind this conversation's @-mentioned dataset (amazon-redshift "
    "dialect, Postgres-derived; NOT Athena/Trino) — and return the result rows. "
    "Use this to answer questions that need LIVE data or aggregates the wiki "
    "docs don't state (counts, sums, distinct values, spot-checks) — NOT to "
    "rediscover schema, which you should read from the wiki first.\n"
    "Rules: exactly ONE statement, SELECT/WITH/SHOW/EXPLAIN only (no "
    "INSERT/UPDATE/DELETE/CREATE/DROP; Redshift has no DESCRIBE). The connection "
    "is pinned to `{database}`; qualify tables as \"schema\".\"table\" (the wiki's "
    "concept ids are already schema-qualified). Add your own LIMIT: at most "
    "{max_rows} rows come back, and any beyond that are dropped with "
    "`truncated: true` in the result — aggregate in SQL instead of paging. Ground "
    "the query in schema you read from the wiki."
)


def _cancel_policy_check(future: Any) -> None:
    """Best-effort cancel of a racing policy check whose query FAILED.

    A query the engine rejected never produces results the model can act on,
    so its verdict is worthless — and letting the queued check run anyway
    would burn one of the turn's few judged-query budget units (and a fleet's
    tokens) on SQL that never executed, starving the eventual successful
    query. cancel() only stops a check that hasn't started; one already
    mid-fleet keeps its (legitimately spent) budget unit.
    """
    if future is None:
        return
    try:
        future.cancel()
    except Exception:  # noqa: BLE001 - advisory, never fatal
        pass


def _await_policy_note(future: Any, checker: Any) -> str:
    """Resolve the soft policy verdict within the residual wait budget.

    A verdict that isn't ready in time is dropped (the check is advisory);
    the future keeps running in the checker's pool, so a re-run of the same
    SQL later in the turn hits its cache instantly.
    """
    from concurrent.futures import TimeoutError as FuturesTimeout

    try:
        return future.result(timeout=getattr(checker, "wait_budget_s", 20)) or ""
    except FuturesTimeout:
        log.info("query policy verdict not ready in time; skipped (advisory)")
        return ""
    except Exception:  # noqa: BLE001 - advisory, never fatal
        log.warning("query policy check failed (non-fatal)", exc_info=True)
        return ""


def make_sql_tool(
    engine: AthenaSQL | RedshiftDataSQL,
    *,
    dataset_scope: dict[str, str] | None = None,
    policy_checker: Any = None,
) -> Any:
    """Wrap a SQL engine as a LangChain ``run_sql`` StructuredTool.

    The tool description comes from the ENGINE (``engine.tool_description``), so
    the model is told the right backend, dialect, and qualification rules for the
    run — an Athena description on a Redshift run would produce wrong SQL.

    For the catalog-wide Athena engine, an ``@``-scoped dataset's Glue database is
    the DEFAULT database for unqualified table names (advisory — the model may
    still query other databases by qualifying them). The scope's ``glue_database``
    (the real Glue DB name, resolved from the registry) is preferred; the
    ``dataset`` id is only a fallback for when it wasn't resolved, and the two can
    differ (a dataset id need not equal its Glue database name), so an unqualified
    query would hit a non-existent database if we defaulted to the dataset id.
    The Redshift engine is pinned to its mapping's database and ignores the
    default.

    Every successful result additionally passes through the anomaly detector
    (``chat.sql_anomalies``): findings are always logged, and — within a
    per-turn budget — a ``<system-reminder>`` is appended after the result
    payload so the model applies its ``<result_skepticism>`` procedure at the
    moment the evidence is in front of it. The budget state lives in this
    closure, which is per-turn because ``server.build_agent`` constructs the
    tool fresh on every run.

    ``policy_checker`` (a ``policy_check.PolicyChecker`` with the
    computational track armed — per-run opt-in on policy-enabled deployments)
    is the query-time SOFT policy check: the check is submitted BEFORE the
    engine runs so the judges race the query, and after the results are back
    the tool waits at most ``policy_checker.wait_budget_s`` for the verdict.
    Flagged violations ride back as a second ``<system-reminder>`` after the
    result — the model decides the correction. Every checker failure mode
    (error, timeout) is fail-open: the results return untouched.
    """
    from langchain_core.tools import StructuredTool

    from chat import sql_anomalies

    default_db = None
    if dataset_scope:
        default_db = dataset_scope.get("glue_database") or dataset_scope.get("dataset")

    # Per-turn injection budget: at most MAX_INJECTIONS_PER_TURN reminders, and
    # a finding KIND that was already injected this turn never re-injects (the
    # model has been told; repeating it is nagging).
    injected_kinds: set[str] = set()
    injection_count = 0

    def run_sql(sql: str) -> Any:
        # A failed query must come back to the model as a tool RESULT it can
        # react to — fix the column name, qualify the table, report the denied
        # permission — NOT propagate out and kill the whole run (same convention
        # as chat.tools._make_tool). Athena's own error text (COLUMN_NOT_FOUND:
        # line N:M ..., Insufficient Lake Formation permission(s) ...) is exactly
        # the feedback the model needs, so it passes through verbatim.
        nonlocal injection_count
        # The soft policy check races the query: submit BEFORE the engine so
        # the judges spend the query's own execution time. Failures fail open.
        # Only ANALYTICAL queries are worth blocking on (should_wait) —
        # exploration probes still submit (they warm the checker's caches in
        # the worker) but return without waiting on a guaranteed-empty verdict.
        policy_future = None
        policy_wait = False
        if policy_checker is not None:
            try:
                policy_future = policy_checker.submit(sql)
                should_wait = getattr(policy_checker, "should_wait", None)
                policy_wait = bool(should_wait(sql)) if should_wait else True
            except Exception:  # noqa: BLE001 - the check is advisory
                log.warning("policy check submit failed (non-fatal)", exc_info=True)
                policy_future = None
        try:
            result = engine.run(sql, default_database=default_db)
        except ValueError as e:  # the read-only guard — concise, actionable
            _cancel_policy_check(policy_future)
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 - a tool error is feedback, not a crash
            log.warning("run_sql failed", exc_info=True)
            _cancel_policy_check(policy_future)
            return f"Error: run_sql failed: {type(e).__name__}: {e}"
        reminders: list[str] = []
        # Fail-open anomaly scan: a detector bug must never fail a successful
        # query. Detection runs on EVERY result (the log feeds threshold
        # calibration); injection respects the per-turn budget above.
        try:
            findings = sql_anomalies.detect(result)
        except Exception:  # noqa: BLE001 - detection is advisory, never fatal
            log.warning("sql anomaly detection failed", exc_info=True)
            findings = []
        if findings:
            log.info(
                "run_sql anomalies detected: %s",
                sorted({f.kind for f in findings}),
            )
            novel = [f for f in findings if f.kind not in injected_kinds]
            if novel and injection_count < sql_anomalies.MAX_INJECTIONS_PER_TURN:
                injection_count += 1
                injected_kinds.update(f.kind for f in novel)
                reminders.append(sql_anomalies.compose(novel))
        if policy_future is not None and policy_wait:
            note = _await_policy_note(policy_future, policy_checker)
            if note:
                reminders.append(note)
        if reminders:
            return json.dumps(result) + "\n\n" + "\n\n".join(reminders)
        return result

    return StructuredTool.from_function(
        func=run_sql,
        name="run_sql",
        description=engine.tool_description,
    )


# The known, server-recognized optional features (the browser may request a
# subset via the run envelope's ``features``). Kept here so config/server share
# it. The ``policy:*`` values are the composer's Policy field (Computational /
# Behavioural / Strict = both) — they arm the mid-turn policy checks and are
# valid ONLY alongside ``sql`` (the checks judge SQL conduct; without the SQL
# tool there is nothing to check), a dependency the UI mirrors but the server
# enforces independently here.
POLICY_FEATURES: frozenset[str] = frozenset(
    {"policy:computational", "policy:behavioural", "policy:strict"}
)
KNOWN_FEATURES: frozenset[str] = frozenset({"sql"}) | POLICY_FEATURES


def normalize_features(raw: Any) -> set[str]:
    """Coerce a client-sent ``features`` value to the recognized subset (a set).

    Unknown values are dropped; ``policy:*`` values arriving WITHOUT ``sql``
    are dropped too (orphaned opt-in — the hard dependency, enforced
    server-side regardless of what the UI sent).
    """
    if not isinstance(raw, (list, tuple, set)):
        return set()
    out = {str(f) for f in raw if str(f) in KNOWN_FEATURES}
    if "sql" not in out:
        out -= POLICY_FEATURES
    return out


def policy_tracks(features: Any) -> frozenset[str]:
    """The policy check tracks a normalized feature set arms.

    ``policy:computational`` / ``policy:behavioural`` each arm their own
    track; ``policy:strict`` arms both. Empty when nothing was opted in —
    the server then constructs no checker at all (zero cost).
    """
    feats = features if isinstance(features, (set, frozenset)) else set(features or ())
    tracks: set[str] = set()
    if "policy:computational" in feats:
        tracks.add("computational")
    if "policy:behavioural" in feats:
        tracks.add("behavioural")
    if "policy:strict" in feats:
        tracks.update(("computational", "behavioural"))
    return frozenset(tracks)
