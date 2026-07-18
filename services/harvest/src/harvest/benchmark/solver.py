"""The bundle-blind solver — a ReAct agent that answers one question from the wiki.

Simulates the real text-to-SQL consumer: given a question and READ-ONLY access to
the authored bundle (a temp snapshot — no ``.metadata/``/``.context/``/gold, see
:mod:`.snapshot`), it explores the docs and returns candidate SQL. It does NOT
execute SQL (that's the grader's job — letting it run queries would let it iterate
empirically to the answer, measuring persistence not the wiki) and it has no raw
schema (that would let it bypass the wiki).

The SQL is extracted by a TERMINAL structured-output call after the ReAct loop
settles, so a correct answer wrapped in prose still parses (a regex over free text
would score it 0 — measuring the parser, not the wiki). Deferred agent-framework
imports keep this module importable where deepagents/langchain aren't installed.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

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

Return the final SQL. Do not explain it — just the query."""

# The terminal structured-output schema: one field. Built lazily (pydantic is
# present in the runtime, but keep import local for symmetry with the rest).
_SQL_FIELD_DESC = "The single SQL query that answers the question. SQL only, no prose."


def _sql_answer_model():
    from pydantic import BaseModel, Field

    class SqlAnswer(BaseModel):
        sql: str = Field(description=_SQL_FIELD_DESC)

    return SqlAnswer


def make_solver(chat_model: Any, snapshot_root: str) -> Callable[[str], Awaitable[str]]:
    """Build an async ``solve(question) -> sql`` bound to a bundle snapshot.

    ``chat_model`` is the shared instrumented harvest model (so solver tokens meter
    into the run total for free). ``snapshot_root`` is the bundle-only temp dir the
    solver's read tools are confined to. Returns an async callable the round
    orchestrator fans out under its concurrency semaphore.
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
    structured = chat_model.with_structured_output(_sql_answer_model())

    async def solve(question: str) -> str:
        # Phase 1: explore the wiki with read-only tools (no schema binding yet).
        out = await agent.ainvoke(
            {"messages": [("user", question)]},
            config={"recursion_limit": _SOLVER_RECURSION_LIMIT},
        )
        messages = out.get("messages", [])
        transcript = _last_ai_text(messages)
        # Phase 2: terminal coercion to clean SQL — a separate call so the schema
        # never competes with the read tools during exploration.
        answer = await structured.ainvoke(
            [
                (
                    "system",
                    "Extract the final SQL query the assistant settled on. "
                    "Return it verbatim as the `sql` field, nothing else.",
                ),
                ("user", f"Question: {question}\n\nAssistant's work:\n{transcript}"),
            ]
        )
        return (getattr(answer, "sql", "") or "").strip()

    return solve


def _last_ai_text(messages: list) -> str:
    """The text content of the last AI message (the solver's settled answer)."""
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content and getattr(msg, "type", "") in ("ai", "assistant"):
            if isinstance(content, str):
                return content
            # content may be a list of blocks
            return "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
    return ""
