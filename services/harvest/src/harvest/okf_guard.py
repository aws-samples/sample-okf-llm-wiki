"""``OKFGuardMiddleware`` — the deepagents adapter over ``OKFGuardEngine``.

Intercepts ``write_file`` / ``edit_file`` on ``.md`` paths via ``wrap_tool_call``,
consults the engine, and either short-circuits with an error ToolMessage (no
disk write — the model self-corrects) or lets the write proceed (optionally
with normalized frontmatter). Path containment is handled by the
``FilesystemBackend``'s ``virtual_mode``, not here.

Imports of ``langchain``/``deepagents`` are deferred so this module can be
imported (and the engine tested) without those packages installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from harvest.guard_engine import OKFGuardEngine

try:  # deepagents / langchain are only present in the runtime image
    from langchain.agents.middleware import AgentMiddleware
    from langchain.messages import ToolMessage

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - exercised only when langchain is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]
    ToolMessage = None  # type: ignore[assignment]
    _HAVE_LANGCHAIN = False

_GUARDED_TOOLS = {"write_file", "edit_file"}

# The recursive-improvement benchmark tool. When RI is enabled the guard counts
# calls to it and refuses once the per-run iteration budget is spent — a backstop
# against a runaway loop, independent of the supervisor prompt's own budget. When
# RI is disabled this tool isn't even registered, so the counter never moves.
_BENCHMARK_TOOL = "run_benchmark"

# The read-only Glue metadata snapshot (see metadata_export.py). Any write/edit
# whose path lands in this dir is refused: the snapshot is an INPUT the agent
# reads (like .context/), never authors. Matched on a path segment so a leading
# slash or nesting doesn't slip past.
_READONLY_DIR = ".metadata"


def _is_markdown(file_path: str | None) -> bool:
    return bool(file_path) and str(file_path).endswith(".md")


def _is_readonly_path(file_path: str | None) -> bool:
    if not file_path:
        return False
    parts = str(file_path).replace("\\", "/").split("/")
    return _READONLY_DIR in parts


class OKFGuardMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Enforce OKF correctness on filesystem writes.

    ``read_current`` maps a tool's ``file_path`` argument to the current on-disk
    text (or None). It's injected so the middleware doesn't need to know how the
    backend resolves virtual paths — the agent builder wires it to the dataset
    root. ``resolve_path`` optionally rewrites the file_path for reading (e.g.
    joining the dataset root).
    """

    def __init__(
        self,
        engine: OKFGuardEngine,
        *,
        read_current: Callable[[str], str | None],
        benchmark_budget: int | None = None,
    ):
        super().__init__()
        self.engine = engine
        self._read_current = read_current
        # Recursive-improvement backstop. When set (RI enabled), the guard allows
        # at most this many run_benchmark calls per run and refuses the rest. None
        # (a normal harvest) means the tool isn't registered, so this is inert.
        self._benchmark_budget = benchmark_budget
        self._benchmark_calls = 0

    def wrap_tool_call(self, request, handler):  # type: ignore[override]
        """Sync path (invoke/stream)."""
        refusal = self._prepare(request)
        if refusal is not None:
            return refusal
        return handler(request)

    async def awrap_tool_call(self, request, handler):  # type: ignore[override]
        """Async path (ainvoke/astream).

        The dynamic-subagent ``task()`` fan-out runs subagents concurrently,
        which drives deepagents down the ASYNC tool path — so this middleware
        MUST implement the async variant too, or the first guarded write raises
        ``NotImplementedError``. The guard decision itself is pure/sync (string
        checks + a small on-disk read), so we reuse ``_prepare`` and only await
        the downstream handler.
        """
        refusal = self._prepare(request)
        if refusal is not None:
            return refusal
        return await handler(request)

    def _prepare(self, request):
        """Run the guard decision; return a refusal ToolMessage or None to proceed.

        Shared by the sync + async wrappers. On an allowed ``write_file`` this
        also rewrites the mutable ``content`` arg in place (timestamp auto-fill /
        canonical key order) so the downstream handler writes the normalized doc.
        """
        name = request.tool_call["name"]
        args = request.tool_call["args"]
        file_path = args.get("file_path")

        # Recursive-improvement iteration backstop: count run_benchmark calls and
        # refuse once the budget is spent. Checked before the write-tool gate
        # because run_benchmark is not a write tool. Inert when budget is None.
        if name == _BENCHMARK_TOOL and self._benchmark_budget is not None:
            if self._benchmark_calls >= self._benchmark_budget:
                return self._refuse(
                    request,
                    f"Refused: the recursive-improvement benchmark budget of "
                    f"{self._benchmark_budget} iteration(s) is spent. Stop looping "
                    f"and let the run finalize — the wiki ships exactly as you have "
                    f"left it (there is no rollback), so make sure it's in your best "
                    f"state before you finish.",
                )
            self._benchmark_calls += 1
            return None

        if name not in _GUARDED_TOOLS:
            return None

        # The .metadata/ snapshot is read-only: refuse any write into it,
        # regardless of extension, before the .md-only OKF checks below.
        if _is_readonly_path(file_path):
            return self._refuse(
                request,
                f"Refused: `{file_path}` is under the read-only `{_READONLY_DIR}/` "
                "Glue metadata snapshot. It is an input to READ (via read_file / "
                "grep / glob), never to write. Author bundle docs under "
                "datasets/, tables/, references/ instead.",
            )

        if not _is_markdown(file_path):
            return None

        existing = self._read_current(file_path)

        if name == "write_file":
            decision = self.engine.guard_write_file(args.get("content", ""), existing)
            if not decision.allow:
                return self._refuse(request, decision.message)
            if decision.new_content is not None:
                args["content"] = decision.new_content
            return None

        # edit_file
        decision = self.engine.guard_edit_file(
            args.get("old_string", ""), args.get("new_string", ""), existing
        )
        if not decision.allow:
            return self._refuse(request, decision.message)
        return None

    def _refuse(self, request, message: str | None):
        msg = message or "Refused by OKF guard."
        return ToolMessage(content=msg, tool_call_id=request.tool_call["id"])
