"""The bundle-blind solver — a ReAct agent that answers one question from the wiki.

Simulates the real text-to-SQL consumer: given a question and READ-ONLY access to
the authored bundle (a temp snapshot — no ``.metadata/``/``.context/``/gold, see
:mod:`.snapshot`), it explores the docs and returns candidate SQL. It does NOT
execute SQL (that's the grader's job — letting it run queries would let it iterate
empirically to the answer, measuring persistence not the wiki) and it has no raw
schema (that would let it bypass the wiki).

The SQL is requested as a fenced ```sql block and extracted with a plain-text
parser (see :mod:`.extract`), NOT via ``with_structured_output`` — the harvest
model runs adaptive thinking, and Bedrock Converse rejects the assistant-message
prefill that structured output uses ("conversation must end with a user message").
A correct answer wrapped in a fence still parses, so a solver isn't scored 0 for
formatting. Deferred agent-framework imports keep this module importable where
deepagents/langchain aren't installed.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from harvest.benchmark.extract import extract_sql, message_text

# Recursion budget for a single solver's explore loop — generous enough to read a
# few pages + follow links, bounded so a confused solver can't spin.
_SOLVER_RECURSION_LIMIT = 40

SOLVER_SYSTEM_PROMPT = """\
You are answering ONE analytics question by writing a single SQL query, using \
ONLY the knowledge in this data wiki. The wiki is a set of markdown docs about \
the dataset's tables, columns, joins, metrics, and gotchas.

Your only knowledge source is the wiki — you have read-only tools (`read_file`, \
`glob`, `grep`, `ls`) over it and nothing else. You CANNOT query the database, \
see its raw schema, or sample data; if the wiki doesn't say something, you must \
infer it from what the wiki does say. This mirrors a real agent that has only \
the wiki to go on — so a good wiki should let you succeed.

Method:
1. Find the relevant table/reference docs (`glob`/`grep` for table names, \
columns, metrics named in the question).
2. Read them. Note the exact table + column names, the join keys, any coded \
values / units / filters the docs call out (e.g. "status is an int code, \
1=active", "revenue excludes refunds").
3. Write ONE Athena/Trino SQL query that answers the question, using the exact \
names and semantics the docs specify.

When you have the answer, output the final query as a single fenced SQL block:
```sql
SELECT ...
```
Output nothing after the block."""


def make_solver(
    chat_model: Any, snapshot_root: str, emit: Callable[[dict], None] | None = None
) -> Callable[[str], Awaitable[str]]:
    """Build an async ``solve(question) -> sql`` bound to a bundle snapshot.

    ``chat_model`` is the shared instrumented harvest model (so solver tokens meter
    into the run total for free). ``snapshot_root`` is the bundle-only temp dir the
    solver's read tools are confined to. ``emit`` (best-effort) receives a compact
    per-question observability event — a ReAct solver is an ISOLATED graph whose
    turns don't reach the run's StepEmitter, so without this the solver is a black
    box (exactly the gap that made "why is EX 0?" un-diagnosable from logs). We do
    NOT log the question or the answer's meaning — just tool-call/read counts,
    turn count, whether SQL came out, an error if any, and a short SQL preview.
    Returns an async callable the round orchestrator fans out under its semaphore.
    """
    from deepagents.backends import FilesystemBackend
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

    backend = FilesystemBackend(root_dir=snapshot_root, virtual_mode=True)

    # Read-only bundle tools over the snapshot. No run_sql, no sandbox, no write.
    @tool
    def read_file(file_path: str) -> str:
        """Read a wiki markdown file by its bundle-relative path (e.g. 'tables/races.md')."""
        return backend.read_file(file_path).content or ""

    @tool
    def glob(pattern: str) -> list[str]:
        """Find wiki files matching a glob (e.g. '**/*.md', 'tables/*')."""
        return [m.path for m in backend.glob(pattern).matches]

    @tool
    def grep(pattern: str) -> list[str]:
        """Search wiki file contents for a literal string; returns matching locations."""
        res = backend.grep(pattern)
        return [f"{m.path}:{m.line_number}: {m.line}" for m in (res.matches or [])]

    @tool
    def ls(path: str = "/") -> list[str]:
        """List entries in a wiki directory."""
        return [e.path for e in (backend.ls(path).entries or [])]

    agent = create_react_agent(
        chat_model,
        tools=[read_file, glob, grep, ls],
        prompt=SOLVER_SYSTEM_PROMPT,
    )

    async def solve(question: str) -> str:
        # One ReAct run: the agent explores the wiki with the read tools and ends
        # with a fenced ```sql block. We parse the SQL out of its final message —
        # no structured-output prefill (rejected under adaptive thinking).
        error = ""
        messages: list = []
        try:
            out = await agent.ainvoke(
                {"messages": [("user", question)]},
                config={"recursion_limit": _SOLVER_RECURSION_LIMIT},
            )
            messages = out.get("messages", [])
        except Exception as e:  # noqa: BLE001 - a stuck solver is a miss, captured here
            error = f"{type(e).__name__}: {e}"
        sql = extract_sql(_last_ai_text(messages)) if messages else ""
        _emit_solver_debug(emit, messages=messages, sql=sql, error=error)
        return sql

    return solve


def _emit_solver_debug(
    emit: Callable[[dict], None] | None,
    *,
    messages: list,
    sql: str,
    error: str,
) -> None:
    """Emit a compact benchmark_solver observability event (best-effort)."""
    if emit is None:
        return
    tool_calls = 0
    for m in messages:
        tc = getattr(m, "tool_calls", None)
        if tc:
            tool_calls += len(tc)
    try:
        emit(
            {
                "kind": "benchmark_solver",
                "turns": len(messages),
                "tool_calls": tool_calls,
                "sql_len": len(sql),
                "sql_preview": (sql[:200] if sql else ""),
                "error": error,
            }
        )
    except Exception:  # noqa: BLE001 - observability must never break a solve
        pass


def _last_ai_text(messages: list) -> str:
    """The text content of the last AI message (the solver's settled answer)."""
    for msg in reversed(messages):
        if getattr(msg, "type", "") in ("ai", "assistant"):
            text = message_text(msg)
            if text.strip():
                return text
    return ""
