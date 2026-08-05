"""The bundle-blind solver — a ReAct agent that answers one question from the wiki.

Simulates the real text-to-SQL consumer: given a question and READ-ONLY access to
the authored bundle (a temp snapshot — no ``.metadata/``/``.context/``/gold, see
:mod:`.s3_snapshot`), it explores the docs and returns candidate SQL. It does NOT
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
from harvest.benchmark.trace import SolveResult, build_trace

# Recursion budget for a single solver's explore loop — generous enough to read a
# few pages + follow links, bounded so a confused solver can't spin.
_SOLVER_RECURSION_LIMIT = 40

# The canonical per-check solver prompts live in harvest.benchmark.checks; this
# alias keeps the historical name (and the SQL EX default) for existing callers.
from harvest.benchmark.checks import SQL_SOLVER_PROMPT as SOLVER_SYSTEM_PROMPT
from harvest.benchmark.checks import with_run_date


def make_solver(
    chat_model: Any,
    snapshot_root: str,
    emit: Callable[[dict], None] | None = None,
    *,
    system_prompt: str = SOLVER_SYSTEM_PROMPT,
    parse: Callable[[Any], str] = extract_sql,
    extra_tools: list[Any] | None = None,
) -> Callable[[str], Awaitable[SolveResult]]:
    """Build an async ``solve(question) -> SolveResult`` bound to a bundle snapshot.

    ``chat_model`` is the shared instrumented model (so solver tokens meter into
    the run total for free). ``snapshot_root`` is the bundle-only temp dir the
    solver's read tools are confined to. ``system_prompt``/``parse`` select the
    CHECK's protocol (see :mod:`.checks`) — the default pair is the SQL EX
    protocol, so existing callers are unchanged. ``emit`` (best-effort) receives
    a compact per-question observability event — a ReAct solver is an ISOLATED
    graph whose turns don't reach the run's StepEmitter, so without this the
    solver is a black box (exactly the gap that made "why is EX 0?"
    un-diagnosable from logs). We do NOT log the question or the answer's
    meaning — just tool-call/read counts, turn count, the files it opened,
    whether a prediction came out, an error if any, and a short preview.

    The returned :class:`~harvest.benchmark.trace.SolveResult` carries the
    prediction plus the bounded TRACE of how the solver got there (reasoning,
    tool calls, results) — fed to the judge and persisted in the report for the
    human review. Returns an async callable the round orchestrator fans out
    under its semaphore.

    ``extra_tools`` extends the read-only wiki toolset for protocols that hold
    more than the wiki — today the Behavior check's opt-in live ``run_sql``
    (``behavior_live_sql``); the prompt selection (:func:`.checks.
    solver_protocol`) and the tool grant travel together so a solver is never
    told about a tool it doesn't hold, or handed one its prompt disclaims.
    """
    from harvest.benchmark.react import is_recursion_limit, make_react_agent

    agent = make_react_agent(
        chat_model,
        # read_me first: its description says to call it before exploring, and
        # the solver prompts' method step 1 points at it — the primer is how a
        # solver knows the layout, the trap locations (guardrails/known issues),
        # and that grep wants ONE literal token.
        [
            *make_readonly_file_tools(snapshot_root),
            make_read_me_tool(),
            *(extra_tools or []),
        ],
        # Every solver knows the run date — benchmark questions lean on
        # relative time references; see checks.with_run_date.
        with_run_date(system_prompt),
    )

    async def solve(question: str) -> SolveResult:
        # One ReAct run: the agent explores the wiki with the read tools and ends
        # with its check's final payload (a fenced ```sql block / JSON action).
        # We parse it out of the final message — no structured-output prefill
        # (rejected under adaptive thinking). Streamed as VALUES snapshots (not
        # ainvoke) so the message history survives a mid-run exception: a
        # budget-blown or crashed solve still carries its trace to the judge
        # and the report — the error itself is the most diagnosable part.
        import time

        error = ""
        messages: list = []
        started = time.monotonic()
        try:
            async for state in agent.astream(
                {"messages": [("user", question)]},
                config={"recursion_limit": _SOLVER_RECURSION_LIMIT},
                stream_mode="values",
            ):
                if isinstance(state, dict) and state.get("messages"):
                    messages = state["messages"]
        except Exception as e:  # noqa: BLE001 - a stuck solver is a miss, captured here
            if is_recursion_limit(e):
                # create_agent RAISES at the step budget (no apology message);
                # record it as a run error, never as the solver's answer.
                error = (
                    f"hit the solve step budget (recursion_limit="
                    f"{_SOLVER_RECURSION_LIMIT}) before settling on an answer"
                )
            else:
                error = f"{type(e).__name__}: {e}"
        wall_ms = int((time.monotonic() - started) * 1000)
        if error:
            prediction = ""
        else:
            # An ask_human call is the run's outcome, structurally — it wins
            # over whatever text follows (see _extract_ask).
            asked = _extract_ask(messages)
            prediction = (
                render_ask(asked)
                if asked is not None
                else parse(_last_ai_text(messages) if messages else "")
            )
        # Capture BEFORE emitting so a trace exists even for a run that errored (the
        # error itself is the most useful thing to show). Never let it break a solve.
        try:
            trace = build_trace(messages, sql=prediction, error=error)
        except Exception:  # noqa: BLE001 - a trace is observability, not the answer
            trace = None
        _emit_solver_debug(
            emit, messages=messages, sql=prediction, error=error, trace=trace
        )
        return SolveResult(
            sql=prediction,
            trace=trace,
            usage=fold_usage(messages),
            wall_ms=wall_ms,
        )

    return solve


def fold_usage(messages: list) -> dict:
    """Sum ``usage_metadata`` over a solve's AI messages → token telemetry.

    The same per-message fold chat's history stats use. Returns ``{}`` when no
    message carried usage (a fake model, or an errored run) so callers can
    treat the telemetry as best-effort.
    """
    input_tokens = output_tokens = 0
    seen = False
    for m in messages or []:
        usage = getattr(m, "usage_metadata", None)
        if not isinstance(usage, dict):
            continue
        seen = True
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
    if not seen:
        return {}
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


# The canonical rendering of an ask-ended run: the prediction the judge grades
# and the report shows. The PREFIX is a stable sentinel (the judge prompt names
# it), followed by the numbered questions.
ASKED_PREFIX = "[The agent ended the run by asking the user for clarification]"


def render_ask(questions: list[str]) -> str:
    """Render an ask-ended run's questions as the canonical prediction string."""
    lines = [ASKED_PREFIX]
    if questions:
        lines.extend(f"{i}. {q}" for i, q in enumerate(questions, 1))
    else:
        lines.append("(no questions given)")
    return "\n".join(lines)


def _extract_ask(messages: list) -> list[str] | None:
    """The questions from the run's LAST ``ask_human`` call, or None.

    STRUCTURAL: the tool CALL is the ask signal — authoritative over any prose
    the model emits after it, so a solver that asks and then rambles is still
    recorded as having asked (the same principle as the judge's submit tools).
    Only the Behavior protocol holds the tool, so other checks never match.
    """
    for msg in reversed(messages or []):
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name != "ask_human":
                continue
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
            qs = args.get("questions") if isinstance(args, dict) else None
            return [str(q).strip() for q in (qs or []) if str(q).strip()]
    return None


# Gold-blind and identical for every question (no per-row signal): the tool
# mirrors the escalation path a production agent has, so "should ask" behaviors
# become a clear-cut structural outcome instead of judge-interpreted prose.
_ASK_HUMAN_DESC = (
    "Ask the user clarifying question(s) and END the run — the escalation path "
    "a production agent has. Use it ONLY when the docs cannot settle the "
    "reading: a required dimension (period, region, grain, scope) is missing "
    "from the request, a term resolves to more than one documented thing, or "
    "the wiki's guardrails direct agents to ASK before answering this kind of "
    "request. `questions` is a short list of specific questions. There is no "
    "user here to reply: calling this records the ask as your final answer. "
    "Never use it to avoid the reading, and never when the wiki supports a "
    "direct answer."
)


def make_ask_human_tool() -> Any:
    """The Behavior solver's terminal ``ask_human`` escalation tool.

    ``return_direct=True`` ends the ReAct loop at the call (no further model
    turns where supported); the tool result says the run is complete as a
    belt-and-braces stop for frameworks that keep going. Either way the
    recorded outcome comes from :func:`_extract_ask` — the CALL is the signal,
    not the loop's shutdown behavior.
    """
    from langchain_core.tools import tool

    @tool(return_direct=True)
    def ask_human(questions: list[str]) -> str:
        """Ask the user clarifying question(s); calling this ends the run."""
        return (
            "Your clarification request has been recorded as this run's final "
            "answer. There is no user available to reply — the run is complete; "
            "output nothing further."
        )

    ask_human.description = _ASK_HUMAN_DESC
    return ask_human


def make_read_me_tool() -> Any:
    """The ``read_me`` primer tool — the first call a solver should make.

    Static text from ``okf_core.wiki_primer`` (the SOLVER rendering: literal
    grep, index-first, no ``get_backlinks``/``semantic_search`` — naming tools
    the solver doesn't have would teach it to make failing calls). Deferred
    import keeps this module importable without langchain installed.
    """
    from langchain_core.tools import tool

    from okf_core.wiki_primer import READ_ME_DESCRIPTION, SOLVER_PRIMER

    @tool
    def read_me() -> str:
        """Read the wiki-usage primer (structure, gotchas, navigation)."""
        return SOLVER_PRIMER

    read_me.description = READ_ME_DESCRIPTION
    return read_me


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
    ``.matches`` / LsResult ``.entries`` / GrepResult ``.matches``, each with
    ``error``/``truncated`` fields) whose ITEMS are TypedDicts (FileInfo
    ``{"path",...}``, GrepMatch ``{"path","line","text"}``), and ``read()``
    returns a ``ReadResult`` (``error`` or ``file_data{"content","encoding"}``
    plus ``total_lines``/``end_line``/``next_offset``) since deepagents 0.7 —
    rendered to numbered text by :func:`_read_text` (a pre-0.7 str passes
    through). Getting shapes wrong crashed every solver on turn 0 with "'dict'
    object has no attribute 'path'" (EX 0). :func:`_field` is dict-or-attr
    tolerant so a backend shape change degrades gracefully instead of crashing.

    Two contract points these wrappers must uphold (both burned us):

    * **A raising tool must not abort the solve.** These tools sit inside plain
      ReAct agent graphs with NO ToolErrorMiddleware, and the
      backend RAISES ``ValueError`` on any '..' in a path/pattern — while wiki
      docs legitimately contain ``../`` relative links the solver will follow.
      Every wrapper catches and returns the error as text, so a bad call costs
      the solver one turn, not the whole (graded!) run.
    * **Partial evidence must say it is partial.** Reads page at 2000 lines
      (offset/limit exposed, precise continuation notice), binary (base64)
      content is refused instead of dumped into the context, and grep/glob
      surface the backend's ``error``/``truncated`` fields — a silently
      truncated search otherwise reads as "the wiki never documents this",
      the exact evidence judge verdicts turn on.
    """
    from deepagents.backends import FilesystemBackend
    from langchain_core.tools import tool

    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    noun = "dataset" if scope == "dataset" else "wiki"

    # NOTE: each tool MUST carry a docstring — @tool raises at decoration time
    # ("Function must have a docstring if description not provided") without one.
    # We then refine the wording per-scope by overriding `.description` below.
    @tool
    def read_file(file_path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a file by its path (e.g. 'tables/races.md'). Long files are
        paged: a truncated read says so and gives the offset to continue from."""
        try:
            return _read_text(backend, _vpath(file_path), offset=offset, limit=limit)
        except Exception as e:  # noqa: BLE001 - a bad call is one lost turn, not a lost run
            return f"Error: {e}"

    @tool
    def glob(pattern: str) -> list[str]:
        """Find files matching a glob (e.g. '**/*.md', 'tables/*')."""
        try:
            res = backend.glob(pattern)
        except Exception as e:  # noqa: BLE001
            return [f"Error: {e}"]
        if getattr(res, "error", None):
            return [f"Error: {res.error}"]
        out = [_field(m, "path") for m in (res.matches or [])]
        if getattr(res, "truncated", False):
            out.append("… (result truncated — narrow the pattern)")
        return out

    @tool
    def grep(pattern: str) -> list[str]:
        """Search file contents for a literal string; returns matching locations."""
        try:
            res = backend.grep(pattern)
        except Exception as e:  # noqa: BLE001
            return [f"Error: {e}"]
        if getattr(res, "error", None):
            return [f"Error: {res.error}"]
        out = [
            f"{_field(m, 'path')}:{_field(m, 'line')}: {_field(m, 'text')}"
            for m in (res.matches or [])
        ]
        if getattr(res, "truncated", False):
            out.append(
                "… (result truncated — matches beyond this point were dropped; "
                "narrow the pattern before concluding something isn't documented)"
            )
        return out

    @tool
    def ls(path: str = "/") -> list[str]:
        """List entries in a directory."""
        try:
            res = backend.ls(_vpath(path))
        except Exception as e:  # noqa: BLE001
            return [f"Error: {e}"]
        if getattr(res, "error", None):
            return [f"Error: {res.error}"]
        return [_field(e, "path") for e in (res.entries or [])]

    # Refine the description per-scope so the same mechanics read correctly for the
    # wiki-only solver and the whole-dataset adjudicator (the docstrings above are
    # the scope-neutral fallback that satisfies the @tool decorator).
    read_file.description = (
        f"Read a {noun} file by its path (e.g. 'tables/races.md'). Long files "
        "are paged; a truncated read gives the offset to continue from."
    )
    glob.description = f"Find {noun} files matching a glob (e.g. '**/*.md', 'tables/*')."
    grep.description = (
        f"Search {noun} file contents for a literal string; returns matching locations."
    )
    ls.description = f"List entries in a {noun} directory."
    return [read_file, glob, grep, ls]


def _read_text(backend: Any, vpath: str, *, offset: int = 0, limit: int = 2000) -> str:
    """Render ``backend.read()`` for the model, across deepagents versions.

    deepagents ≥0.7 returns a ``ReadResult`` (``error`` set, or
    ``file_data{"content", "encoding"}`` plus ``total_lines``/``end_line``/
    ``next_offset``); older versions returned the rendered ``cat -n``-style
    string directly. Errors come back as plain text — these tools sit behind
    ``ToolErrorMiddleware``-free ReAct agents, so a raising read would
    otherwise end the whole solve. Base64 (binary) content is refused rather
    than dumped — a single uploaded PDF under ``.context/`` would otherwise
    inject megabytes of noise into one ToolMessage — and a read that stopped
    at the line limit says so precisely, so the model pages instead of
    mistaking the visible prefix for the whole file.
    """
    result = backend.read(vpath, offset=offset, limit=limit)
    if isinstance(result, str):
        return result
    error = getattr(result, "error", None)
    if error:
        return f"Error: {error}"
    file_data = getattr(result, "file_data", None) or {}
    content = file_data.get("content", "") if isinstance(file_data, dict) else ""
    encoding = str(file_data.get("encoding", "") if isinstance(file_data, dict) else "")
    if encoding and encoding.lower() not in ("utf-8", "utf8", "text"):
        return (
            f"Error: {vpath.lstrip('/')} is a binary file ({encoding}-encoded) — "
            "these tools read text files only."
        )
    if not isinstance(content, str):
        return str(content)
    try:
        # The numbered-gutter rendering the built-in read_file tool shows, so
        # grep's path:line hits line up with what the model reads (the gutter
        # starts at the read's real first line, not 1, on paged reads).
        from deepagents.backends.utils import format_content_with_line_numbers

        start_line = int(getattr(result, "start_line", None) or offset + 1)
        rendered = format_content_with_line_numbers(content, start_line=start_line)
    except Exception:  # noqa: BLE001 - rendering sugar, never worth a crash
        rendered = content
    total = getattr(result, "total_lines", None)
    end = getattr(result, "end_line", None)
    next_offset = getattr(result, "next_offset", None)
    if (
        isinstance(total, int)
        and isinstance(end, int)
        and end < total
        and next_offset is not None
    ):
        rendered += (
            f"\n… truncated: showing lines through {end} of {total}. "
            f"Call read_file again with offset={next_offset} to continue."
        )
    return rendered


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
    trace: Any = None,
) -> None:
    """Emit a compact benchmark_solver observability event (best-effort).

    Stays COUNTS-ONLY plus the files opened — the full step-by-step trace goes to
    the off-mount review artifact, not into the CloudWatch feed. ``files_read`` is
    the cheap signal that answers the first diagnostic question ("did this solver
    even open the right doc?") straight from the logs.
    """
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
                "files_read": list(getattr(trace, "files_read", None) or []),
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
