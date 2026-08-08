"""``OKFGuardMiddleware`` — the deepagents adapter over ``OKFGuardEngine`` —
and ``ToolErrorMiddleware``, the tool-boundary safety net.

The guard intercepts ``write_file`` / ``edit_file`` on ``.md`` paths via
``wrap_tool_call``, consults the engine, and either short-circuits with an
error ToolMessage (no disk write — the model self-corrects) or lets the write
proceed (optionally with normalized frontmatter). Path containment is handled
by the ``FilesystemBackend``'s ``virtual_mode``, not here.

Two refusals sit in front of the engine checks:

* ``delete`` (the recursive filesystem tool deepagents ≥0.7 hands every agent
  whose backend supports it) is refused unless the guard was built with
  ``allow_delete=True`` — which the agent builder does for the FULL-harvest
  SUPERVISOR only. The supervisor owns bundle-level shape (it is the one told
  to fix the lint gate's ``stale-table-doc`` findings), so it can retire a doc
  whose source table is gone; an authoring sub-agent has no business deleting
  anything, and the read-only ones certainly don't. Even when allowed, a
  delete must be a single ``.md`` FILE outside every dot-dir (and inside the
  cross-mode writable subtree): a recursive directory delete is the blast
  radius this refusal originally existed to prevent, and ``.metadata/``,
  ``.context/``, ``.harvest/`` are inputs/state, never the agent's to remove.
* ``read_only=True`` builds a guard variant for the verify-and-report
  sub-agents (reviewer, context-extractor): every write/edit is refused, so
  "read-only" is enforced at the tool boundary rather than promised by the
  prompt (deepagents hands every sub-agent the backend's write tools).
* ``write_allowlist=`` builds the ``fix-author`` variant: writes are allowed
  ONLY to the exact paths the callable returns — run_review binds it to the
  dispatch's review cluster via a contextvar (see ``harvest.review``), which
  is what makes parallel fixers unable to touch each other's files. Fails
  closed: no binding = every write refused.

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

# Sub-agent types ONLY the run_review workflow may dispatch (and the refusal
# text) live in harvest.dispatch_policy — a dependency-free module, because
# the OTHER enforcement point (the harvest.subagent_io shim, covering the
# QuickJS task() path) must stay importable without okf_core/langchain.
from harvest.dispatch_policy import (  # noqa: E402
    WORKFLOW_ONLY_DISPATCH_MSG,
    WORKFLOW_ONLY_SUBAGENTS,
)

# The read-only Glue metadata snapshot (see metadata_export.py). Any write/edit
# whose path lands in this dir is refused: the snapshot is an INPUT the agent
# reads (like .context/), never authors. Matched on a path segment so a leading
# slash or nesting doesn't slip past.
_READONLY_DIR = ".metadata"

# The ONE dot-dir path an agent legitimately writes: the annotation run's
# verdict file. Mirrors harvest.runner.ANNOTATION_RESULTS_REL — the literal is
# repeated because runner imports agent imports this module (a runner import
# here would be circular); test_guard_middleware pins the two in sync.
_ANNOTATION_RESULTS_REL = ".harvest/annotation_results.json"


def _is_markdown(file_path: str | None) -> bool:
    return bool(file_path) and str(file_path).endswith(".md")


def _has_dot_segment(file_path: str | None) -> bool:
    """True if any path segment is dot-prefixed (``.metadata``, ``.context``,
    ``.harvest``) — the run's inputs and state, never an agent's to delete.
    Same rule ``fsutil.clean_authored_output`` uses to decide what to preserve."""
    if not file_path:
        return False
    return any(
        seg.startswith(".") and seg not in (".", "..")
        for seg in _normalized_rel(str(file_path)).split("/")
    )


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
        allow_delete: bool = False,
        write_allowlist: Callable[[], frozenset[str] | None] | None = None,
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
        # The full-harvest supervisor only (see the module docstring): may
        # retire a single stale .md doc. Never set for sub-agents.
        self._allow_delete = allow_delete and not read_only
        # The `fix-author` variant: a CALLABLE returning the exact set of
        # root-relative paths this dispatch may write (bound per dispatch by
        # run_review via a contextvar — see harvest.review). Evaluated on
        # every write so parallel dispatches each see their own cluster.
        # FAILS CLOSED: None/empty (no cluster bound to this call chain, e.g.
        # a manual task() dispatch) refuses every write.
        self._write_allowlist = write_allowlist

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
        # supports it, and it is RECURSIVE). Only the full-harvest supervisor's
        # guard allows it, and only for one .md file outside the dot-dirs —
        # everything else keeps the original blanket refusal.
        if name == "delete":
            if not self._allow_delete:
                # Tailor the guidance to what THIS agent can actually do: a
                # read-only agent told to "use edit_file instead" just earns
                # a second refusal from the read-only branch below.
                if self._read_only:
                    return self._refuse(
                        request,
                        f"Refused: `delete` is not available to this agent, "
                        f"and this agent is READ-ONLY. Report the stale doc "
                        f"in your reply instead — the supervisor owns "
                        f"removals; `{file_path}` was not touched.",
                    )
                return self._refuse(
                    request,
                    f"Refused: `delete` is not available to this agent. Correct "
                    f"the doc in place with `edit_file` (or report it to the "
                    f"supervisor, which owns bundle-level removals); "
                    f"`{file_path}` was not touched.",
                )
            if _has_dot_segment(file_path):
                return self._refuse(
                    request,
                    f"Refused: `{file_path}` is under a reserved dot-directory "
                    f"(`{_READONLY_DIR}/` snapshot, `.context/` uploads, "
                    f"`.harvest/` state). Those are this run's INPUTS and "
                    "state — never delete them.",
                )
            if not _is_markdown(file_path):
                return self._refuse(
                    request,
                    f"Refused: `delete` may remove ONE `.md` concept doc at a "
                    f"time, and `{file_path}` is not one — `delete` is "
                    f"RECURSIVE, so a directory path would take the whole "
                    f"subtree with it. Delete the individual stale doc(s) "
                    "instead; an emptied directory is harmless (its generated "
                    "index.md is dropped at finalize).",
                )
            # The name check alone doesn't prove "one .md FILE": the backend
            # mkdirs parents on write, so a path ending in .md can exist as a
            # DIRECTORY (a write to `tables/x.md/y.md` creates dir
            # `tables/x.md`) — and delete is recursive. Stat it.
            try:
                root = getattr(
                    getattr(self.engine, "link_graph", None), "root", None
                )
                if (
                    root is not None
                    and (Path(root) / _normalized_rel(file_path)).is_dir()
                ):
                    return self._refuse(
                        request,
                        f"Refused: `{file_path}` is a DIRECTORY, not a doc — "
                        "`delete` is recursive and would take the whole "
                        "subtree. Delete the individual stale `.md` docs "
                        "inside it instead.",
                    )
            except OSError:  # pragma: no cover - a stat error must not crash the guard
                pass
            if self._writable_prefix and not _normalized_rel(file_path).startswith(
                self._writable_prefix
            ):
                return self._refuse(
                    request,
                    f"Refused: `{file_path}` is outside this run's writable "
                    f"subtree `{self._writable_prefix}`.",
                )
            return None

        # Read-only agents (reviewer / context-extractor): they verify and
        # REPORT — findings go in the reply, never on disk.
        if self._read_only:
            return self._refuse(
                request,
                f"Refused: this agent is READ-ONLY — it verifies and reports, "
                f"it never writes. `{file_path}` was not touched; put the "
                "finding in your reply instead (the supervisor applies fixes).",
            )

        # EVERY dot-directory is off-limits to write/edit, regardless of
        # extension — same rule the delete branch enforces. `.metadata/` is
        # the read-only snapshot, `.context/` the user's uploads, and
        # `.harvest/` the run's own state (commit marker, review clustering,
        # recorded context digests): a write into any of them corrupts an
        # input or breaks a workflow contract (e.g. clobbering
        # `.harvest/review/clusters.json` silently kills the documented
        # cluster_ids retry path). ONE exception: the annotation run's verdict
        # file, which the annotation supervisor is REQUIRED to write (its
        # prompt says "write it via write_file to that exact path") and the
        # runner then reconciles to DynamoDB — refusing it would silently
        # revert every annotation back to open.
        if (
            _has_dot_segment(file_path)
            and _normalized_rel(file_path) != _ANNOTATION_RESULTS_REL
        ):
            return self._refuse(
                request,
                f"Refused: `{file_path}` is under a reserved dot-directory "
                f"(`{_READONLY_DIR}/` snapshot, `.context/` uploads, "
                "`.harvest/` run state). Those are this run's INPUTS and "
                "state — read-only for you. Author bundle docs under "
                "datasets/, tables/, references/ instead.",
            )

        # Fixer confinement: a run_review fix dispatch may write ONLY the doc
        # paths of its own cluster (bound per dispatch — parallel fixers can
        # never touch each other's files). Anything else — including when NO
        # cluster is bound to this call chain — is refused with instructions
        # to report the change instead.
        if self._write_allowlist is not None:
            allowed = self._write_allowlist() or frozenset()
            rel = _normalized_rel(file_path) if file_path else ""
            if rel not in allowed:
                scope = (
                    ", ".join(f"`{p}`" for p in sorted(allowed))
                    if allowed
                    else "none — no review cluster is bound to this dispatch"
                )
                return self._refuse(
                    request,
                    f"Refused: `{file_path}` is not in this fix dispatch's "
                    f"cluster (writable files: {scope}). Do NOT edit files "
                    "outside your cluster — finish your in-cluster fixes and "
                    "list the needed out-of-cluster change under a "
                    "`PROPAGATION NOTES` section in your reply; the "
                    "supervisor applies those serially.",
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


class SubagentDispatchGuard(AgentMiddleware):  # type: ignore[misc]
    """Refuse model-driven ``task`` dispatches of workflow-only sub-agent types.

    Attached to the MAIN agent's middleware in every mode. Covers the static
    ``task`` tool path (this middleware wraps the supervisor's ToolNode); the
    QuickJS ``task()`` path is covered by the same blocklist inside the
    ``harvest.subagent_io`` shim, because eval-borne dispatches never reach
    agent middleware. ``run_review``'s own dispatches call the task tool
    object directly and are intentionally NOT intercepted by either.
    """

    def wrap_tool_call(self, request, handler):  # type: ignore[override]
        refusal = self._check(request)
        if refusal is not None:
            return refusal
        return handler(request)

    async def awrap_tool_call(self, request, handler):  # type: ignore[override]
        refusal = self._check(request)
        if refusal is not None:
            return refusal
        return await handler(request)

    def _check(self, request):
        tool_call = request.tool_call
        if tool_call["name"] != "task":
            return None
        sub = (tool_call.get("args") or {}).get("subagent_type")
        if sub not in WORKFLOW_ONLY_SUBAGENTS:
            return None
        return ToolMessage(
            content=WORKFLOW_ONLY_DISPATCH_MSG.format(sub=sub),
            tool_call_id=tool_call["id"],
            status="error",
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
