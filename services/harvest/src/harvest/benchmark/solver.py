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
    from langgraph.prebuilt import create_react_agent

    agent = create_react_agent(
        chat_model,
        tools=make_readonly_file_tools(snapshot_root),
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


def make_readonly_file_tools(root: str, *, scope: str = "wiki") -> list[Any]:
    """Read-only ``read_file``/``glob``/``grep``/``ls`` tools over a filesystem root.

    Shared by the bundle-blind SOLVER (rooted at a wiki-only snapshot) and the
    ADJUDICATOR (rooted at the real dataset mount, so its file tools additionally
    reach ``.metadata/`` and ``.context/``). ``scope`` only tunes the docstrings'
    wording ("wiki" vs "dataset") — the CONFINEMENT is physical: the
    ``FilesystemBackend`` is rooted at ``root`` and cannot reach outside it however
    the tools are called (see :mod:`.snapshot` for why the solver's root omits the
    dot-dirs). No write tool, no ``run_sql``, no sandbox here — those are added
    separately for the roles that get them.

    IMPORTANT: deepagents' backend returns dataclass *containers* (GlobResult
    ``.matches`` / LsResult ``.entries`` / GrepResult ``.matches``) whose ITEMS are
    TypedDicts (FileInfo ``{"path",...}``, GrepMatch ``{"path","line","text"}``),
    and read is ``read(file_path) -> str`` (cat -n text), NOT
    ``read_file().content``. Getting this wrong crashed every solver on turn 0 with
    "'dict' object has no attribute 'path'" (EX 0). :func:`_field` is dict-or-attr
    tolerant so a backend shape change degrades gracefully instead of crashing.
    """
    from deepagents.backends import FilesystemBackend
    from langchain_core.tools import tool

    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    noun = "dataset" if scope == "dataset" else "wiki"

    # NOTE: each tool MUST carry a docstring — @tool raises at decoration time
    # ("Function must have a docstring if description not provided") without one.
    # We then refine the wording per-scope by overriding `.description` below.
    @tool
    def read_file(file_path: str) -> str:
        """Read a file by its path (e.g. 'tables/races.md')."""
        return backend.read(_vpath(file_path))

    @tool
    def glob(pattern: str) -> list[str]:
        """Find files matching a glob (e.g. '**/*.md', 'tables/*')."""
        return [_field(m, "path") for m in (backend.glob(pattern).matches or [])]

    @tool
    def grep(pattern: str) -> list[str]:
        """Search file contents for a literal string; returns matching locations."""
        res = backend.grep(pattern)
        return [
            f"{_field(m, 'path')}:{_field(m, 'line')}: {_field(m, 'text')}"
            for m in (res.matches or [])
        ]

    @tool
    def ls(path: str = "/") -> list[str]:
        """List entries in a directory."""
        return [_field(e, "path") for e in (backend.ls(_vpath(path)).entries or [])]

    # Refine the description per-scope so the same mechanics read correctly for the
    # wiki-only solver and the whole-dataset adjudicator (the docstrings above are
    # the scope-neutral fallback that satisfies the @tool decorator).
    read_file.description = f"Read a {noun} file by its path (e.g. 'tables/races.md')."
    glob.description = f"Find {noun} files matching a glob (e.g. '**/*.md', 'tables/*')."
    grep.description = (
        f"Search {noun} file contents for a literal string; returns matching locations."
    )
    ls.description = f"List entries in a {noun} directory."
    return [read_file, glob, grep, ls]


def _field(item: Any, key: str) -> str:
    """Read ``key`` off a backend result item, tolerating dict OR object shape.

    deepagents backend items are TypedDicts (``item["path"]``); older/other
    backends might expose attributes. Return "" if absent so a tool never crashes
    the solver on a shape mismatch (the bug that turned EX to 0)."""
    if isinstance(item, dict):
        return str(item.get(key, ""))
    return str(getattr(item, key, "") or "")


def _vpath(path: str) -> str:
    """Normalize a bundle-relative path to the leading-slash virtual path the
    FilesystemBackend expects (its API requires paths to start with '/')."""
    p = (path or "").strip()
    if not p or p == "/":
        return "/"
    return p if p.startswith("/") else "/" + p


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
