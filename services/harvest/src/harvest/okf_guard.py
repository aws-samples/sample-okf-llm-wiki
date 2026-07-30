"""``OKFGuardMiddleware`` — the deepagents adapter over ``OKFGuardEngine`` —
and ``ToolErrorMiddleware``, the tool-boundary safety net.

The guard intercepts ``write_file`` / ``edit_file`` on ``.md`` paths via
``wrap_tool_call``, consults the engine, and either short-circuits with an
error ToolMessage (no disk write — the model self-corrects) or lets the write
proceed (optionally with normalized frontmatter). Path containment is handled
by the ``FilesystemBackend``'s ``virtual_mode``, not here.

Two blanket refusals sit in front of the engine checks:

* ``delete`` (the recursive filesystem tool deepagents ≥0.7 hands every agent
  whose backend supports it) is ALWAYS refused — nothing in a bundle is ever
  deleted by an agent; stale or wrong docs are corrected or annotated, and
  deletions are a human decision outside the run.
* ``read_only=True`` builds a guard variant for the verify-and-report
  sub-agents (reviewer, context-extractor): every write/edit is refused, so
  "read-only" is enforced at the tool boundary rather than promised by the
  prompt (deepagents hands every sub-agent the backend's write tools).

``ToolErrorMiddleware`` converts any exception a tool RAISES into a
``ToolMessage(status="error")`` so a single failing call (a ``PermissionError``
from the S3 Files mount mid-write, a transient ``OSError``) surfaces to the
model as a recoverable tool error instead of aborting the whole agent graph
and failing the harvest.

Imports of ``langchain``/``deepagents`` are deferred so this module can be
imported (and the engine tested) without those packages installed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from harvest.guard_engine import OKFGuardEngine

log = logging.getLogger(__name__)

try:  # deepagents / langchain are only present in the runtime image
    from langchain.agents.middleware import AgentMiddleware
    from langchain.messages import ToolMessage

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - exercised only when langchain is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]
    ToolMessage = None  # type: ignore[assignment]
    _HAVE_LANGCHAIN = False

_GUARDED_TOOLS = {"write_file", "edit_file", "delete"}

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


def _normalized_rel(file_path: str) -> str:
    """Normalize a tool ``file_path`` to a root-relative POSIX path.

    Strips the optional leading slash the virtual backend accepts and resolves
    ``.``/``..`` segments textually, so a ``external/../tables/x.md`` can't slip
    past a prefix check. (Escapes beyond the root are the backend's job — this
    just canonicalizes for comparison.)
    """
    import posixpath

    return posixpath.normpath(str(file_path).replace("\\", "/").lstrip("/"))


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
        writable_prefix: str | None = None,
        read_only: bool = False,
    ):
        super().__init__()
        self.engine = engine
        self._read_current = read_current
        # Cross-dataset mode: confine EVERY write/edit to this root-relative
        # subtree (e.g. "external/<domain>/<dataset>/"). The rest of the bundle
        # is read-only context for the run. None (all other modes) = inert.
        self._writable_prefix = writable_prefix.strip("/") + "/" if writable_prefix else None
        # Verify-and-report sub-agents: refuse every write/edit outright.
        self._read_only = read_only

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

        if name not in _GUARDED_TOOLS:
            return None

        # The delete tool (deepagents ≥0.7 exposes it whenever the backend
        # supports it, and it is RECURSIVE) is never part of authoring: stale
        # or wrong docs are corrected in place or annotated — removing bundle
        # content is a human decision outside the run. Refused unconditionally,
        # before any path/extension logic.
        if name == "delete":
            return self._refuse(
                request,
                f"Refused: `delete` is not available in this run. Nothing in "
                f"the bundle is ever deleted by an agent — correct the doc in "
                f"place with `edit_file` (or supersede it) instead; `{file_path}` "
                "was not touched.",
            )

        # Read-only agents (reviewer / context-extractor): they verify and
        # REPORT — findings go in the reply, never on disk.
        if self._read_only:
            return self._refuse(
                request,
                f"Refused: this agent is READ-ONLY — it verifies and reports, "
                f"it never writes. `{file_path}` was not touched; put the "
                "finding in your reply instead (the supervisor applies fixes).",
            )

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

        # Cross-dataset confinement: this run may write ONLY under its pair
        # subtree; everything else in the bundle is read-only context.
        if self._writable_prefix and file_path:
            rel = _normalized_rel(file_path)
            if not rel.startswith(self._writable_prefix):
                return self._refuse(
                    request,
                    f"Refused: `{file_path}` is outside this run's writable "
                    f"subtree `{self._writable_prefix}`. A cross-dataset run "
                    "authors ONLY the cross-dataset reference docs under that "
                    "folder; the rest of the bundle is read-only context here.",
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
        # status="error" so the model treats it as a failed call to self-correct
        # (and the step feed's tool_result row shows ok=False).
        return ToolMessage(
            content=msg, tool_call_id=request.tool_call["id"], status="error"
        )


def _is_control_flow(exc: Exception) -> bool:
    """True for LangGraph control-flow exceptions (interrupts, Command routing).

    Those are not failures — converting one to a ToolMessage would break the
    graph's own mechanics, so they must keep propagating. Matched by module so
    we don't import langgraph here (kept import-free for the test venv).
    """
    mod = getattr(type(exc), "__module__", "") or ""
    return mod.startswith("langgraph")


class ToolErrorMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Convert tool-raised exceptions into ``ToolMessage(status="error")``.

    A tool that raises (a ``PermissionError`` from the S3 Files mount mid-write,
    a transient mount ``OSError``, an SDK error a tool forgot to catch) would
    otherwise propagate out of the tool node, abort the (sub-)agent graph, and
    fail the whole harvest — hours of authoring lost to one bad call. This
    middleware is the safety net at the tool boundary: the exception becomes an
    error ToolMessage the model SEES and can react to — retry, route around it,
    or report the failure — and the step feed marks the call ``ok=False``
    (``StepEmitter.on_tool_end`` classifies ``status == "error"``).

    Attach it to the MAIN agent AND to EVERY sub-agent's middleware list —
    sub-agent middleware REPLACES rather than inherits (the same footgun as the
    guard), and the read-only sub-agents (reviewer / context-extractor) carry
    no guard, so each spec names this explicitly.

    LangGraph control-flow exceptions are re-raised (see ``_is_control_flow``);
    ``BaseException`` (cancellation, shutdown) is never caught.
    """

    def wrap_tool_call(self, request, handler):  # type: ignore[override]
        try:
            return handler(request)
        except Exception as e:  # noqa: BLE001 - the conversion IS the feature
            if _is_control_flow(e):
                raise
            return self._error_message(request, e)

    async def awrap_tool_call(self, request, handler):  # type: ignore[override]
        try:
            return await handler(request)
        except Exception as e:  # noqa: BLE001 - the conversion IS the feature
            if _is_control_flow(e):
                raise
            return self._error_message(request, e)

    def _error_message(self, request, exc: Exception):
        name = request.tool_call.get("name", "?")
        # Keep the full traceback in the runtime log for diagnosis; the model
        # gets the one-line cause.
        log.warning(
            "Tool %s raised %s; returning error ToolMessage",
            name,
            type(exc).__name__,
            exc_info=True,
        )
        return ToolMessage(
            content=(
                f"Tool `{name}` failed: {type(exc).__name__}: {exc}. The call had "
                "no effect. Adjust and retry, or work around it; if it keeps "
                "failing, continue with the rest of your work and report the "
                "failure in your summary instead of silently dropping the work."
            ),
            tool_call_id=request.tool_call["id"],
            status="error",
        )
